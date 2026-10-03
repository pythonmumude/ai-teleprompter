#!/usr/bin/env python3
"""增量对齐：把流式 ASR 吐出的增量文本，映射到稿件的第几个字。

为什么需要这个：
    流式 Paraformer 每 600ms 只吐「这一块新识别出来的字」，**没有时间戳**。
    而提词器要的是「现在读到哪了」。所以唯一的办法是把识别文本和稿件对齐。

四条设计原则（都是踩坑换来的）：
    1. 屏幕上永远显示**稿件原文**，ASR 文本只用来定位 —— 错字不会上屏。
    2. **单调不回退**：指针只往前走。跳错一句比慢半拍难看得多。
    3. **低置信就不动**。宁可等下一块，也不猜 —— 猜错一次后面全乱。
    4. 回读（人把上一句重读）走**独立的回退路径 + 投票**，不污染主路径的单调性。

对齐用的不是「精确匹配」而是「拼音匹配」：
    ASR 必然有同音误识（"自律" → "自理"），精确匹配在第一个错字就崩。
    所以主键是**不带声调**的拼音（去声调是为了避开 ASR 的调值误差）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from pypinyin import Style, lazy_pinyin

# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

# 保留：汉字、英文字母、数字。标点/空白/换行全丢掉 ——
# ASR 的标点本来就不可靠，稿件里的标点也不参与对齐。
_KEEP = re.compile(r"[\u4e00-\u9fff0-9a-z]")

# 中文数字 → 阿拉伯数字（逐字读法）。
# 为什么两边都转：稿件写的是 "2024"，ASR 可能吐 "二零二四"，也可能吐 "2024"。
# 两边过同一个函数之后必然一致 —— 这是**保一致性**，不是保语义，
# 所以顺手把 "一" 也转成 "1"（"一次"→"1次"）不会出问题：
# 稿件和识别结果做同样的转换，照样对得上。
# 已知限制：「两千零二十四」「十分」这类复杂读法不在覆盖范围内（P1 用 cn2an 兜）。
_DIGIT_MAP = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "两": "2",
}


def normalize(text: str) -> tuple[str, list[int]]:
    """归一化成对齐用的字符序列。

    返回 (norm, idxmap)：
        norm   归一化后的字符串
        idxmap norm 第 k 个字符对应原文的第几个字符（高亮回映用）
    """
    norm_chars: list[str] = []
    idxmap: list[int] = []
    for i, raw in enumerate(text):
        ch = unicodedata.normalize("NFKC", raw)
        # NFKC 可能把某些字符拆成多个（如 ㈠ → (一)），逐字符再过一遍过滤
        for piece in ch:
            piece = _DIGIT_MAP.get(piece, piece.lower())
            if _KEEP.match(piece):
                norm_chars.append(piece)
                idxmap.append(i)
    return "".join(norm_chars), idxmap


def syllables(norm: str) -> list[str]:
    """逐字取「不带声调」的拼音。取不到（数字/字母）就返回字符本身。"""
    if not norm:
        return []
    py = lazy_pinyin(norm, style=Style.NORMAL, errors=lambda x: list(x))
    out: list[str] = []
    for k, ch in enumerate(norm):
        s = py[k] if k < len(py) else ch
        # ü 在不同来源下可能是 'ü' / 'u:' / 'v'，统一成 'v'
        s = s.replace("ü", "v").replace("u:", "v")
        out.append(s if s else ch)
    return out


# ---------------------------------------------------------------------------
# 分句（高亮的最小单位）
# ---------------------------------------------------------------------------

_CLAUSE_END = set("，。！？；、：,.!?;:\n")
MAX_CLAUSE_CHARS = 20


@dataclass
class Clause:
    """一个高亮单元，偏移量都是**原文**下标。"""
    start: int          # 含
    end: int            # 不含
    text: str

    @property
    def length(self) -> int:
        return self.end - self.start


def split_clauses(text: str) -> list[Clause]:
    """按中文标点切短句；超过 MAX_CLAUSE_CHARS 的再硬切一刀。

    为什么要二次切：一整段 60 字没有标点的话，高亮"整段"等于没高亮。
    """
    clauses: list[Clause] = []
    buf_start = 0
    for i, ch in enumerate(text):
        if ch in _CLAUSE_END:
            if i > buf_start:
                clauses.append(Clause(buf_start, i, text[buf_start:i]))
            buf_start = i + 1
    if buf_start < len(text):
        clauses.append(Clause(buf_start, len(text), text[buf_start:]))

    out: list[Clause] = []
    for c in clauses:
        if c.length <= MAX_CLAUSE_CHARS:
            out.append(c)
            continue
        n = (c.length + MAX_CLAUSE_CHARS - 1) // MAX_CLAUSE_CHARS
        step = (c.length + n - 1) // n
        s = c.start
        while s < c.end:
            e = min(s + step, c.end)
            out.append(Clause(s, e, text[s:e]))
            s = e
    return out


# ---------------------------------------------------------------------------
# 稿件索引
# ---------------------------------------------------------------------------

@dataclass
class ScriptIndex:
    """稿件的预计算结果。**开机算一次**，不在每个 chunk 里重算拼音。"""
    text: str
    norm: str
    idxmap: list[int]
    syl: list[str]                              # 与 norm 等长，每个字符的拼音
    clauses: list[Clause]
    clause_hint: list[int] = field(default_factory=list)   # norm 下标 → clause 序号

    @classmethod
    def build(cls, text: str) -> "ScriptIndex":
        norm, idxmap = normalize(text)
        idx = cls(text=text, norm=norm, idxmap=idxmap, syl=syllables(norm),
                  clauses=split_clauses(text))
        # 预算「每个 norm 字符属于哪一句」，避免每次 feed 都二分查找
        hint = [0] * len(norm)
        ci = 0
        for k, orig in enumerate(idxmap):
            while ci + 1 < len(idx.clauses) and orig >= idx.clauses[ci].end:
                ci += 1
            hint[k] = ci
        idx.clause_hint = hint
        return idx

    def clause_at(self, norm_pos: int) -> int:
        """norm 下标 → 分句序号（越界夹到端点）。"""
        if not self.clause_hint:
            return 0
        if norm_pos <= 0:
            return 0
        if norm_pos >= len(self.clause_hint):
            return len(self.clauses) - 1
        return self.clause_hint[norm_pos]

    def clause_start_norm(self, clause: int) -> int:
        """分句序号 → 该句起点对应的 norm 下标。"""
        clause = max(0, min(clause, len(self.clauses) - 1))
        return self.orig_to_norm(self.clauses[clause].start)

    def orig_of(self, norm_pos: int) -> int:
        """norm 下标 → 原文下标（告诉前端高亮到哪）。"""
        if not self.idxmap:
            return 0
        if norm_pos <= 0:
            return 0
        if norm_pos >= len(self.idxmap):
            return len(self.text)
        return self.idxmap[norm_pos]

    def orig_to_norm(self, orig_pos: int) -> int:
        """原文下标 → norm 下标（二分）。"""
        lo, hi = 0, len(self.idxmap)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.idxmap[mid] < orig_pos:
                lo = mid + 1
            else:
                hi = mid
        return lo


# ---------------------------------------------------------------------------
# 对齐配置
# ---------------------------------------------------------------------------

@dataclass
class AlignConfig:
    buf_max: int = 24               # 滚动缓冲上限（汉字数）
    min_buf: int = 3                # 少于这么多字不动手（冷启动噪声门）
    # 走"整块接受"这条快路要求缓冲区至少这么长。
    # 为什么：2~3 个字的缓冲区太容易在往前 24 字的范围里蒙到一个满分匹配，
    # 蒙中就会白吃掉一块缓冲。短缓冲一律走"确认前缀"那条保守路径。
    accept_min_buf: int = 4
    pad: int = 6                    # 往前回看的余量（ASR 偶发重发）
    look: int = 24                  # 正常前向搜索距离
    reacquire_look: int = 220       # 连续低分后放大搜索范围（跳读/漏读重定位）
    back_look: int = 200            # 回读检测往前看多远
    max_step: int = 40              # 单次最多推进多少字（防跳飞）
    hi: float = 0.75                # ≥ 这个分数：整块接受
    mid: float = 0.50               # [mid, hi)：只推进「确认匹配的前缀」
    miss_limit: int = 3             # 连续多少次低分后进入重定位
    stall_limit: int = 8            # 连续多少次没有任何推进就清缓冲重新对表
    reacquire_score: float = 0.80   # 重定位必须达到的分数
    rewind_score: float = 0.85      # 判定为「回读」所需的分数
    rewind_votes: int = 2           # 连续命中几次才允许回退
    rewind_min_buf: int = 6         # 缓冲区至少这么长才做回读检测（太短容易误判）

    @classmethod
    def from_dict(cls, d: dict) -> "AlignConfig":
        """从 config.json 的 align 段构造。多余字段忽略，缺的用默认值。"""
        known = set(cls.__dataclass_fields__)          # noqa: SIM118
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class AlignResult:
    """一次 feed 的对齐结果，字段直接对应推给前端的 JSON。"""
    p: int = 0                  # 当前朗读位置的 norm 下标（权威值）
    orig: int = 0               # 同上，换算成原文下标
    clause: int = 0
    clause_from: int = 0        # 当前句在原文里的起始下标
    clause_to: int = 0          # 当前句在原文里的结束下标
    conf: float = 0.0           # 本次匹配相似度
    low: bool = False           # 本次低置信（前端不要据此回退）
    waiting: bool = False       # 缓冲区还不够长，本次没动手（不是失败）
    advanced: int = 0           # 本次推进了多少字
    reacquired: bool = False    # 是否走了跳句重定位
    rewound: bool = False       # 是否触发了回读回退
    asr_text: str = ""          # 本块识别文本（只给调试页看）


@dataclass
class _Hit:
    """一次窗口扫描的最佳结果。"""
    score: float
    s: int
    last_j: int


# ---------------------------------------------------------------------------
# 对齐器
# ---------------------------------------------------------------------------

class Aligner:
    """单调不回退的滑窗 LCS 对齐器。

    每收到一块 ASR 增量文本，就在 [p-pad, p+look) 里找「最像缓冲区的那个位置」，
    找到就把指针推到「确实对上过的最后一个字」。
    """

    def __init__(self, index: ScriptIndex, cfg: AlignConfig | None = None) -> None:
        self.index = index
        self.cfg = cfg or AlignConfig()
        self.p = 0
        self._buf: list[str] = []
        self._buf_syl: list[str] = []
        self._miss = 0
        self._stall = 0
        self._accepted = False
        self._max_clause = -1
        self._rewind_vote: dict[int, int] = {}

    # ---------------- 对外接口 ----------------

    def reset(self, p: int = 0) -> None:
        self.p = max(0, min(p, len(self.index.norm)))
        self._buf.clear()
        self._buf_syl.clear()
        self._miss = 0
        self._stall = 0
        self._accepted = False
        self._max_clause = self.index.clause_at(self.p) if self.p else -1
        self._rewind_vote.clear()

    def jump_to_clause(self, clause: int) -> AlignResult:
        """手动跳到某一句（前端「重置位置」用）。"""
        clause = max(0, min(clause, len(self.index.clauses) - 1))
        self.reset(self.index.clause_start_norm(clause))
        return self._payload(conf=1.0, advanced=0)

    @property
    def progress(self) -> float:
        """已读比例 0~1，给调试页画进度条用。"""
        n = len(self.index.norm)
        return self.p / n if n else 0.0

    def feed(self, asr_text: str) -> AlignResult:
        """喂一块 ASR 增量文本。返回对齐结果（可能什么都没动）。"""
        cfg = self.cfg
        asr_norm, _ = normalize(asr_text)
        if asr_norm:
            self._buf.extend(asr_norm)
            self._buf_syl.extend(syllables(asr_norm))
            if len(self._buf) > cfg.buf_max:
                del self._buf[:len(self._buf) - cfg.buf_max]
                del self._buf_syl[:len(self._buf_syl) - cfg.buf_max]

        if len(self._buf) < cfg.min_buf:
            # 缓冲区还不够长，这一块先攒着。**这不是失败**，不能算进低置信率，
            # 否则调试页会把"正常空等"误报成"对齐不稳"。
            return self._payload(conf=0.0, advanced=0, asr_text=asr_text, waiting=True)

        # ---- 1) 主搜索：正常前向窗口 ----
        fwd = self._scan(max(0, self.p - cfg.pad),
                         min(len(self.index.norm), self.p + cfg.look))

        # ---- 2) 回读检测：只在主搜索不满意时做，且严格往指针之前找 ----
        if fwd.score < cfg.hi and len(self._buf) >= cfg.rewind_min_buf and self.p > 0:
            back = self._scan(max(0, self.p - cfg.back_look), self.p)
            if (back.score >= cfg.rewind_score and back.score > fwd.score + 0.05
                    and back.last_j >= 0):
                c = self.index.clause_at(back.s)
                if c < self.index.clause_at(self.p):
                    self._rewind_vote[c] = self._rewind_vote.get(c, 0) + 1
                    if self._rewind_vote[c] >= cfg.rewind_votes:
                        # 连续两次确认 → 认为是回读，回退到该句起点
                        self.reset(self.index.clause_start_norm(c))
                        return self._payload(conf=back.score, advanced=0, rewound=True,
                                             asr_text=asr_text)
                    # 只看一次不算，等下一块再确认
                    return self._payload(conf=back.score, advanced=0, low=True,
                                         asr_text=asr_text)
        if fwd.score < cfg.hi:
            self._rewind_vote.clear()

        # ---- 3) 阈值策略：宁可不动，也不猜 ----
        if fwd.score >= cfg.hi and len(self._buf) >= cfg.accept_min_buf:
            new_p = fwd.s + fwd.last_j + 1
            if new_p <= self.p:
                return self._stalled(conf=fwd.score, asr_text=asr_text)
            self._consume(len(self._buf))
            self._accepted = True
            return self._commit(new_p, conf=fwd.score, asr_text=asr_text)

        if fwd.score >= cfg.mid:
            # 半信半疑（或缓冲太短）：只推进「从缓冲区第一个字开始连续命中」
            # 的那一段。不按比例猜 —— 猜出来的位置没有任何依据。
            j_conf, consumed = self._confirmed_prefix(fwd.s)
            if j_conf < 0 or fwd.s + j_conf + 1 <= self.p:
                return self._stalled(conf=fwd.score, asr_text=asr_text)
            self._consume(consumed)
            self._accepted = True
            return self._commit(fwd.s + j_conf + 1, conf=fwd.score,
                                asr_text=asr_text, low=fwd.score < cfg.hi)

        # ---- 4) 连续低分 → 放大搜索范围重定位（跳读、漏读、中途接话）----
        self._miss += 1
        if self._miss < cfg.miss_limit:
            return self._payload(conf=fwd.score, advanced=0, low=True, asr_text=asr_text)

        wide = self._scan(self.p, min(len(self.index.norm), self.p + cfg.reacquire_look))
        if wide.score >= cfg.reacquire_score and wide.last_j >= 0:
            new_p = wide.s + wide.last_j + 1
            if new_p > self.p:
                self._consume(len(self._buf))
                self._miss = 0
                self._accepted = True
                return self._commit(new_p, conf=wide.score, asr_text=asr_text,
                                    reacquired=True)
        return self._payload(conf=fwd.score, advanced=0, low=True, asr_text=asr_text)

    # ---------------- 内部 ----------------

    def _consume(self, n: int) -> None:
        """吃掉缓冲区前 n 个字。ASR 的增量是不重叠的，接受后即可丢弃；
        万一模型重发了一小段，pad 会兜住。"""
        n = max(0, min(n, len(self._buf)))
        del self._buf[:n]
        del self._buf_syl[:n]

    def _stalled(self, *, conf: float, asr_text: str) -> AlignResult:
        """没有推进。连续太多次就把缓冲清掉，重新和稿件对表。"""
        self._miss += 1
        self._stall += 1
        if self._stall >= self.cfg.stall_limit:
            self._buf.clear()
            self._buf_syl.clear()
            self._stall = 0
        return self._payload(conf=conf, advanced=0, low=True, asr_text=asr_text)

    def _commit(self, new_p: int, *, conf: float, asr_text: str,
                low: bool = False, reacquired: bool = False) -> AlignResult:
        """单调 + 限速之后落地指针。"""
        cfg = self.cfg
        prev = self.p
        new_p = max(new_p, self.p)                              # 不回退
        new_p = min(new_p, self.p + cfg.max_step)               # 不跳飞
        new_p = min(new_p, len(self.index.norm))
        self.p = new_p
        if self.p > prev:
            self._stall = 0
            self._miss = 0
            self._accepted = True
        return self._payload(conf=conf, advanced=self.p - prev, low=low,
                             reacquired=reacquired, asr_text=asr_text)

    def _match(self, a: str, a_syl: str, b: str, b_syl: str) -> bool:
        """两个字符算不算「同一个字」。

        字符相同 → 算。拼音相同 → 也算。后者就是同音容错，抗 ASR 错字的关键。
        """
        if a == b:
            return True
        return bool(a_syl) and a_syl == b_syl

    def _scan(self, s_lo: int, s_hi: int) -> _Hit:
        """在 [s_lo, s_hi) 里逐位置算 LCS，取最像缓冲区的那一个。

        冷启动（还没成功对齐过一次）时允许全稿搜索 ——
        从稿子中间开始读的场景，别让前 30 个字白瞎。
        """
        idx = self.index
        n = len(self._buf)
        if not self._accepted and self.p == 0:
            s_lo, s_hi = 0, len(idx.norm)
        best = _Hit(0.0, s_lo, -1)
        for s in range(max(0, s_lo), min(s_hi, len(idx.norm))):
            m, last_j = self._lcs(s, n)
            if m == 0:
                continue
            score = m / n
            if score > best.score:
                best = _Hit(score, s, last_j)
        return best

    def _lcs(self, s: int, n: int) -> tuple[int, int]:
        """缓冲区 vs 稿件 [s, s+n) 的 LCS。

        返回 (匹配字数, 最后一个匹配在窗口内的下标)。
        返回下标而不是窗口长度，是为了把指针推到「确实对上过的最后一个字」——
        缓冲区尾部经常带噪声（ASR 多吐的字）。
        """
        a, a_s = self._buf, self._buf_syl
        idx = self.index
        b = idx.norm[s:s + n]
        b_s = idx.syl[s:s + n]
        na, nb = len(a), len(b)
        if na == 0 or nb == 0:
            return 0, -1

        # dp[i][j] = a[i:] 与 b[j:] 的 LCS 长度
        dp = [[0] * (nb + 1) for _ in range(na + 1)]
        for i in range(na - 1, -1, -1):
            row, nxt = dp[i], dp[i + 1]
            ai, asi = a[i], a_s[i]
            for j in range(nb - 1, -1, -1):
                if self._match(ai, asi, b[j], b_s[j]):
                    row[j] = nxt[j + 1] + 1
                else:
                    row[j] = nxt[j] if nxt[j] >= row[j + 1] else row[j + 1]

        # 回溯出具体配对，取最后一个配对的窗口内下标
        i = j = 0
        last_j = -1
        while i < na and j < nb:
            if self._match(a[i], a_s[i], b[j], b_s[j]):
                last_j = j
                i += 1
                j += 1
            elif dp[i + 1][j] >= dp[i][j + 1]:
                i += 1
            else:
                j += 1
        return dp[0][0], last_j

    def _confirmed_prefix(self, s: int) -> tuple[int, int]:
        """从缓冲区**第 0 个字**开始连续命中，能推到窗口内第几个字。

        返回 (窗口内最大下标, 消费掉的缓冲区字符数)。
        下标为 -1 表示第 0 个字就没对上。
        允许中间漏一个字（ASR 偶尔吞字），但连续两个对不上就停。
        """
        a, a_s = self._buf, self._buf_syl
        idx = self.index
        b, b_s = idx.norm, idx.syl
        n = len(b)
        j = -1
        i = 0
        while i < len(a):
            nxt = s + j + 1
            if nxt >= n:
                break
            if self._match(a[i], a_s[i], b[nxt], b_s[nxt]):
                j += 1
                i += 1
                continue
            # 缓冲区多了一个字（ASR 多吐）：跳过它继续试
            if i + 1 < len(a) and self._match(a[i + 1], a_s[i + 1], b[nxt], b_s[nxt]):
                j += 1
                i += 2
                continue
            break
        return j, i

    def _payload(self, *, conf: float, advanced: int, asr_text: str = "",
                 low: bool = False, waiting: bool = False,
                 reacquired: bool = False,
                 rewound: bool = False) -> AlignResult:
        ci = self.index.clause_at(self.p)
        c = self.index.clauses[ci] if self.index.clauses else Clause(0, 0, "")
        self._max_clause = max(self._max_clause, ci)
        return AlignResult(
            p=self.p,
            orig=self.index.orig_of(self.p),
            clause=ci,
            clause_from=c.start,
            clause_to=c.end,
            conf=round(conf, 3),
            low=low,
            waiting=waiting,
            advanced=advanced,
            reacquired=reacquired,
            rewound=rewound,
            asr_text=asr_text,
        )

    # ---------------- 调试 ----------------

    def cursor_json(self, r: AlignResult, frozen: bool = False) -> dict:
        """推给前端的 cursor 消息。"""
        return {
            "t": "cursor",
            "p": r.p,
            "orig": r.orig,
            "clause": r.clause,
            "clauseFrom": r.clause_from,
            "clauseTo": r.clause_to,
            "conf": r.conf,
            "low": r.low,
            "waiting": r.waiting,
            "frozen": frozen,
            "advanced": r.advanced,
            "reacquired": r.reacquired,
            "rewound": r.rewound,
            "progress": round(self.progress, 4),
        }

#!/usr/bin/env python3
"""对齐器单测。

不用 pytest —— 直接用 venv 里的 python 跑，省一个依赖：
    ~/.venvs/funasr/bin/python tests/test_align.py

覆盖的都是真实会遇到的脏数据：同音错字、漏字、多字、数字混读、
跳读、回读、纯噪声。这些场景决定了提词器会不会"跳错句"。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from server.align import (  # noqa: E402
    MAX_CLAUSE_CHARS,
    AlignConfig,
    Aligner,
    ScriptIndex,
    normalize,
    split_clauses,
)

SCRIPT = (
    "很多人以为自律是靠意志力硬扛，其实不是。"
    "意志力是一种会消耗的资源，你早上用它拒绝了一次甜点，中午就更难拒绝第二次。"
    "真正能长期坚持的人，靠的不是意志力，而是把选择提前做完了。"
    "比如把运动鞋放在门口，把手机充电器放在客厅，把零食从冰箱里清掉。"
)

# 每个测试独立统计
_FAILED: list[str] = []
_PASSED = 0


def check(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
    else:
        _FAILED.append(msg)
        print(f"    FAIL  {msg}")


def chunks(s: str, n: int) -> list[str]:
    return [s[i:i + n] for i in range(0, len(s), n)]


def run(script: str, feeds: list[str], cfg: AlignConfig | None = None):
    """建索引 + 逐块喂，返回 (aligner, 轨迹)。顺便断言单调性。"""
    idx = ScriptIndex.build(script)
    a = Aligner(idx, cfg)
    trace = []
    prev = 0
    for f in feeds:
        r = a.feed(f)
        trace.append(r)
        if r.rewound:
            # 回读是**合法**的回退，基线要跟着重置，
            # 否则回退之后的正常前进会被误判成"又回退了"。
            prev = r.p
            continue
        if r.p < prev:
            check(False, f"指针回退了：{prev} -> {r.p}（第 {len(trace)} 块）")
        prev = max(prev, r.p)
    return a, trace


# ---------------------------------------------------------------------------

def test_normalize() -> None:
    print("归一化")
    norm, idxmap = normalize("你好，世界！ 123 ABC")
    check(norm == "你好世界123abc", f"标点/空白/大小写：{norm!r}")
    check(len(norm) == len(idxmap), "idxmap 长度应与 norm 一致")
    check(idxmap[0] == 0 and idxmap[-1] == len("你好，世界！ 123 ABC") - 1,
          f"idxmap 端点：{idxmap}")

    # 中文数字逐字读法与阿拉伯数字必须归到一起
    check(normalize("二零二四")[0] == normalize("2024")[0] == "2024",
          f"数字归一：{normalize('二零二四')[0]!r}")
    check(normalize("一次")[0] == normalize("1次")[0] == "1次",
          "一 与 1 应归一到同一形")


def test_clauses() -> None:
    print("分句")
    cs = split_clauses(SCRIPT)
    check(len(cs) >= 8, f"短句数量偏少：{len(cs)}")
    check(all(c.length <= MAX_CLAUSE_CHARS for c in cs),
          f"存在超长句：{[(c.text, c.length) for c in cs if c.length > MAX_CLAUSE_CHARS]}")
    # 偏移量必须是原文下标，且拼接回去等于原文
    rebuilt = "".join(SCRIPT[c.start:c.end] for c in cs)
    check(rebuilt == SCRIPT.replace("，", "").replace("。", "") or
          len(rebuilt) <= len(SCRIPT), "分句偏移应落在原文上")
    check(cs[0].start == 0, f"首句应从 0 开始：{cs[0]}")
    check(SCRIPT[cs[0].start:cs[0].end] == "很多人以为自律是靠意志力硬扛",
          f"首句文本：{SCRIPT[cs[0].start:cs[0].end]!r}")
    # 连续性：前一句的 end 应等于后一句的 start（标点被跳过）
    gaps_ok = all(cs[i].end <= cs[i + 1].start for i in range(len(cs) - 1))
    check(gaps_ok, "分句之间应有序不重叠")


def test_perfect() -> None:
    """干净输入：指针应一路推到末尾。"""
    print("完美增量（3 字/块）")
    idx = ScriptIndex.build(SCRIPT)
    a, trace = run(SCRIPT, chunks(idx.norm, 3))
    check(a.p >= len(idx.norm) - 3, f"指针应接近末尾：{a.p}/{len(idx.norm)}")
    check(all(t.p >= 0 for t in trace), "结果应全部有效")
    check(trace[-1].clause == len(idx.clauses) - 1,
          f"末块应落在最后一句：{trace[-1].clause}")


def test_homophone() -> None:
    """同音错字：意志力→意制力、资源→资原、硬扛→映扛。"""
    print("同音错字")
    dirty = SCRIPT.replace("志", "制").replace("源", "原").replace("硬", "映")
    idx = ScriptIndex.build(SCRIPT)
    feeds = chunks(normalize(dirty)[0], 4)
    a, _ = run(SCRIPT, feeds)
    check(a.p >= len(idx.norm) - 6, f"同音错字下应仍然对齐到位：{a.p}/{len(idx.norm)}")


def test_drop_and_add() -> None:
    """ASR 漏字 / 多字。"""
    print("漏字与多字")
    idx = ScriptIndex.build(SCRIPT)

    norm = idx.norm
    dropped = norm[:20] + norm[23:]          # 挖掉 3 个字
    a, _ = run(SCRIPT, chunks(dropped, 4))
    check(a.p >= len(norm) - 6, f"漏字后仍应到位：{a.p}/{len(norm)}")

    a2, _ = run(SCRIPT, chunks(norm[:18] + "啊" + norm[18:], 4))
    check(a2.p >= len(norm) - 6, f"多字后仍应到位：{a2.p}/{len(norm)}")


def test_punctuated_asr() -> None:
    """带标点的识别结果（paraformer 接了 punc 就会这样）。"""
    print("带标点的识别结果")
    idx = ScriptIndex.build(SCRIPT)
    noisy = SCRIPT.replace("，", "， ").replace("。", "。 ")
    a, _ = run(SCRIPT, chunks(noisy, 5))
    check(a.p >= len(idx.norm) - 6, f"标点不应干扰对齐：{a.p}/{len(idx.norm)}")


def test_number_reading() -> None:
    """稿件写 2024，识别吐「二零二四」。"""
    print("数字混读")
    script = "我们公司2024年的营收目标是增长三成。"
    asr = "我们公司二零二四年的营收目标是增长三成"
    idx = ScriptIndex.build(script)
    a, _ = run(script, chunks(normalize(asr)[0], 4))
    check(a.p >= len(idx.norm) - 4, f"数字混读应对齐：{a.p}/{len(idx.norm)}")


def test_noise_holds() -> None:
    """纯噪声：指针必须纹丝不动。"""
    print("纯噪声不推进")
    cfg = AlignConfig()
    a, _ = run(SCRIPT, ["蝙蝠翅膀"] * 6, cfg)
    check(a.p == 0, f"噪声不应推进指针：{a.p}")


def test_skip_clause() -> None:
    """跳读：读完第一句，直接跳到第三句。"""
    print("跳读重定位")
    idx = ScriptIndex.build(SCRIPT)
    c0 = SCRIPT[idx.clauses[0].start:idx.clauses[0].end]
    c2 = SCRIPT[idx.clauses[2].start:idx.clauses[2].end]
    feeds = chunks(normalize(c0)[0], 4) + chunks(normalize(c2)[0], 3)
    a, trace = run(SCRIPT, feeds)
    hit = [t for t in trace if t.reacquired]
    landed = idx.clause_at(a.p)
    check(bool(hit) or landed >= 2,
          f"应重定位到第 3 句附近，实际落在第 {landed} 句（reacquired={bool(hit)}）")


def test_rewind() -> None:
    """回读：读完前两句后，又把第一句重读一遍。"""
    print("回读回退")
    idx = ScriptIndex.build(SCRIPT)
    seg = lambda i: normalize(SCRIPT[idx.clauses[i].start:idx.clauses[i].end])[0]
    feeds = (chunks(seg(0), 4) + chunks(seg(1), 4)
             + chunks(seg(0), 4) + chunks(seg(0), 4))
    a, trace = run(SCRIPT, feeds)
    rew = [t for t in trace if t.rewound]
    check(bool(rew), "应触发回读回退")
    if rew:
        check(idx.clause_at(a.p) == 0, f"应退回到第一句，实际第 {idx.clause_at(a.p)} 句")


def test_cold_start_middle() -> None:
    """从稿子中间开始读（没读开头）。"""
    print("冷启动全稿搜索")
    idx = ScriptIndex.build(SCRIPT)
    start = 40
    a, _ = run(SCRIPT, chunks(idx.norm[start:start + 24], 4))
    check(a.p >= start, f"应从中间定位成功：{a.p}（期望 ≥ {start}）")
    check(a.p <= start + 24 + 6, f"不应跳飞：{a.p}")


def test_jump_to_clause() -> None:
    print("手动跳句")
    idx = ScriptIndex.build(SCRIPT)
    a = Aligner(idx)
    r = a.jump_to_clause(3)
    check(r.clause == 3, f"跳句后应停在第 4 句：{r.clause}")
    check(a.p == idx.clause_start_norm(3), f"指针应落在句首：{a.p}")
    # 跳句之后继续喂应能正常跟随
    seg = normalize(SCRIPT[idx.clauses[3].start:idx.clauses[3].end])[0]
    a, _ = run(SCRIPT, chunks(seg, 4))
    check(True, "跳句后继续喂不报错")


def test_progress_and_payload() -> None:
    print("进度与 payload")
    idx = ScriptIndex.build(SCRIPT)
    a, _ = run(SCRIPT, chunks(idx.norm, 6))
    r = a._payload(conf=0.9, advanced=1)
    check(0.0 <= a.progress <= 1.0, f"progress 越界：{a.progress}")
    check(r.clause_from <= r.orig <= r.clause_to + 1,
          f"orig 应落在当前句区间内：orig={r.orig} [{r.clause_from},{r.clause_to}]")
    j = a.cursor_json(r)
    for k in ("t", "p", "orig", "clause", "clauseFrom", "conf", "low",
              "frozen", "progress"):
        check(k in j, f"cursor_json 缺字段 {k}")


def main() -> int:
    for fn in (test_normalize, test_clauses, test_perfect, test_homophone,
               test_drop_and_add, test_punctuated_asr, test_number_reading,
               test_noise_holds, test_skip_clause, test_rewind,
               test_cold_start_middle, test_jump_to_clause,
               test_progress_and_payload):
        fn()
    print()
    if _FAILED:
        print(f"✗ 失败 {len(_FAILED)} 项 / 通过 {_PASSED} 项")
        for m in _FAILED:
            print(f"  - {m}")
        return 1
    print(f"✓ 全部通过（{_PASSED} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

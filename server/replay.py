#!/usr/bin/env python3
"""离线回放：不用 iPhone，用一段 wav 跑通整条后端链路。

为什么这是最省时间的工具：
    对齐参数（阈值、窗口、lead）和门限如果每次都要掏出手机、对着麦克风念一遍
    才能调，一轮就是十分钟。用已知稿件的 wav 回放，一轮十秒，
    而且**真值已知**（稿子就是我们写的），可以精确判断"第几块应该推到第几个字"。

跑法：
    # 全速回放（比实时快 4~5 倍），打完整个链路的耗时与对齐轨迹
    ~/.venvs/funasr/bin/python server/replay.py

    # 按实时速度回放，模拟手机端 100ms 一帧的节奏
    ~/.venvs/funasr/bin/python server/replay.py --realtime

    # 存下每块的原始记录，方便事后拿别的参数重放比对
    ~/.venvs/funasr/bin/python server/replay.py --dump tests/fixtures/trace.jsonl

    # 换 chunk 大小做对比
    ~/.venvs/funasr/bin/python server/replay.py --chunk-ms 480
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from server.align import AlignConfig, Aligner, ScriptIndex  # noqa: E402
from server.asr import AsrConfig, StreamingAsr  # noqa: E402
from server.audio import AudioBuffer, AudioConfig  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def load_config() -> dict:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_wav(path: str) -> tuple[np.ndarray, int]:
    import soundfile as sf
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return np.asarray(audio, dtype="float32"), sr


def build_truth_pos(index, meta: dict):
    """把逐句真值变成一条「时刻 → 应该读到稿子第几个字」的曲线。

    句内按字数线性插值（TTS 匀速，误差很小）；句间停顿期间保持上一句末尾。
    """
    n = len(index.norm)
    segs = []
    for t in meta["clauses"]:
        ci = t["clause"]
        s_norm = index.clause_start_norm(ci)
        e_norm = (index.clause_start_norm(ci + 1)
                  if ci + 1 < len(index.clauses) else n)
        segs.append((t["start_ms"], t["end_ms"], s_norm, e_norm))
    if not segs:
        return lambda _t: 0.0

    def pos(tms: float) -> float:
        if tms <= segs[0][0]:
            return 0.0
        last = 0.0
        for a, b, sa, sb in segs:
            if tms < a:
                return float(last)
            if tms <= b:
                f = (tms - a) / (b - a) if b > a else 1.0
                return sa + f * (sb - sa)
            last = sb
        return float(last)

    return pos


def report_lag(recs: list[dict], index, meta: dict) -> None:
    """光标滞后 = 真值位置 − 光标位置。这是唯一能回答"跟得上嘴吗"的数字。

    按 100ms 网格采样（而不是在每次 ASR 出字的瞬间采样）——
    出字瞬间恰好是光标要跳的前一刻，在那些点上采样会系统性高估滞后。
    """
    pos = build_truth_pos(index, meta)
    total_s = meta["total_s"]
    rate = meta["chars_total"] / total_s          # 字/秒

    tl = [(r["wall_ms"], r["p"]) for r in recs]
    if not tl:
        return
    # 光标时间线 → 查询函数
    def cursor_at(tms: float) -> float:
        v = 0
        for w, p in tl:
            if w <= tms:
                v = p
            else:
                break
        return float(v)

    lags: list[float] = []
    grid = int(total_s * 1000)
    for tms in range(1000, grid - 500, 100):
        lag = pos(tms) - cursor_at(tms)
        lags.append(lag)

    good = [x for x in lags if x > -2]            # 排除开头没对齐时的负数噪声
    if not good:
        return
    good.sort()

    def pct(v: float) -> float:
        return good[min(len(good) - 1, int(len(good) * v))]

    print("-" * 72)
    print(f"光标滞后（真值 − 光标，n={len(good)} 个采样点，稿速 {rate:.2f} 字/秒）")
    print(f"  中位 {pct(0.5):.1f} 字 ≈ {pct(0.5) / rate * 1000:.0f}ms　"
          f"p90 {pct(0.9):.1f} 字 ≈ {pct(0.9) / rate * 1000:.0f}ms　"
          f"最大 {good[-1]:.1f} 字 ≈ {good[-1] / rate * 1000:.0f}ms")
    lead = sum(1 for x in lags if x < 0)
    print(f"  光标跑到说话人前面的采样点 {lead}/{len(lags)}"
          f"（{lead / len(lags) * 100:.0f}%，偏多是好事）")

    # 逐句：这句开始说之后多久，光标才进到这一句
    print("-" * 72)
    print("逐句命中延迟（句子开口 → 光标进入该句）")
    hits: list[tuple[int, int]] = []
    for t in meta["clauses"]:
        ci = t["clause"]
        s_norm = index.clause_start_norm(ci)
        w = next((r["wall_ms"] for r in recs if r["p"] >= s_norm), None)
        if w is None:
            continue
        hits.append((ci, w - t["start_ms"]))
    if hits:
        d = sorted(x for _, x in hits)
        print(f"  中位 {d[len(d) // 2]}ms　p90 {d[int(len(d) * 0.9)]}ms　"
              f"最小 {d[0]}ms　最大 {d[-1]}ms")
        for ci, ms in hits[:8]:
            t = meta["clauses"][ci]
            print(f"    句{ci:>2}  {ms:>5}ms  {t['text'][:22]}")


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=os.path.join(ROOT, "tests/fixtures/say_sample.wav"))
    ap.add_argument("--script", default=os.path.join(ROOT, "tests/fixtures/script_sample.txt"))
    ap.add_argument("--chunk-ms", type=int, default=cfg["audio"]["chunk_ms"])
    ap.add_argument("--realtime", action="store_true", help="按 100ms 实时节奏喂")
    ap.add_argument("--dump", default="", help="把每块记录写成 jsonl")
    ap.add_argument("--trace", action="store_true", help="打印每一块的识别文本")
    ap.add_argument("--truth", default="", help="逐句真值 json（用 tools/make_truth_fixture.py 生成）")
    ap.add_argument("--device", default=cfg["asr"]["device"])
    args = ap.parse_args()

    with open(args.script, encoding="utf-8") as fh:
        script = fh.read().strip()

    audio, sr = load_wav(args.wav)
    if sr != 16000:
        print(f"素材必须是 16k，当前 {sr}")
        return 1
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    acfg = AudioConfig.from_dict({**cfg["audio"], "chunk_ms": args.chunk_ms})
    index = ScriptIndex.build(script)
    aligner = Aligner(index, AlignConfig.from_dict(cfg["align"]))

    records: list[dict] = []
    lock = threading.Lock()
    t_start = time.perf_counter()          # 模型加载完再重置（见下）

    def on_text(text: str, meta: dict) -> None:
        r = aligner.feed(text)
        wall_ms = (time.perf_counter() - t_start) * 1000
        with lock:
            records.append({
                "seq": len(records),
                "wall_ms": round(wall_ms),
                "audio_ms": meta.get("audio_ms", 0),
                # 实测端到端延迟 = 高亮发生的墙上时间 - 这句话在音频里的时间。
                # 只有 --realtime 模式下这个数才有意义（全速回放会算出负数）。
                "lat_ms": round(wall_ms - meta.get("audio_ms", 0)),
                "asr": text,
                "infer_ms": round(meta["ms"], 1),
                "p": r.p,
                "clause": r.clause,
                "conf": r.conf,
                "low": r.low,
                "waiting": r.waiting,
                "advanced": r.advanced,
                "reacquired": r.reacquired,
                "rewound": r.rewound,
            })

    asr_cfg = AsrConfig.from_dict({**cfg["asr"], "chunk_ms": args.chunk_ms})
    asr_cfg.device = args.device
    eng = StreamingAsr(asr_cfg, on_text=on_text)
    eng.start()

    # 等模型加载完（加载是异步的，先喂会被队列攒着，但日志会乱）
    while not eng.stats().loaded and not eng.stats().error:
        time.sleep(0.05)

    buf = AudioBuffer(acfg.chunk_samples)
    fsz = acfg.frame_samples
    fed = 0
    # 计时从"模型就绪"开始。否则模型加载那 3 秒会被算进第一块的延迟里，
    # 得出一个虚高的端到端数字。
    t_start = time.perf_counter()
    t0 = t_start
    for i in range(0, len(pcm) - fsz, fsz):
        for ch in buf.push(pcm[i:i + fsz]):
            eng.feed(ch, blocking=True)
            fed += 1
        if args.realtime:
            expect = (i + fsz) / 16000
            slip = expect - (time.perf_counter() - t0)
            if slip > 0:
                time.sleep(slip)
    eng.flush(buf.flush(), blocking=True)
    eng.drain()
    wall = time.perf_counter() - t0
    s = eng.stats()
    eng.stop()

    # ---------------- 报告 ----------------
    audio_s = len(pcm) / 16000
    with lock:
        recs = list(records)
    text_recs = [r for r in recs if r["asr"]]
    low_recs = [r for r in text_recs if r["low"]]
    wait_recs = [r for r in text_recs if r["waiting"]]
    acted = [r for r in text_recs if not r["waiting"]]      # 真正做了判断的块
    reacq = [r for r in recs if r["reacquired"]]
    rew = [r for r in recs if r["rewound"]]
    infer = [r["infer_ms"] for r in recs]

    print("=" * 72)
    print(f"素材 {os.path.basename(args.wav)}  {audio_s:.1f}s　"
          f"chunk {args.chunk_ms}ms　device {s.device}")
    print(f"块数 {fed}（出文本 {len(text_recs)}）　"
          f"模型 RTF {s.rtf:.3f}　丢弃 {s.n_dropped}")
    print(f"回放墙上耗时 {wall:.1f}s（{audio_s / wall:.1f}x 实时）")
    print(f"推理单块 均值 {statistics.mean(infer):.0f}ms　"
          f"p95 {sorted(infer)[int(len(infer) * 0.95)]:.0f}ms")

    print("-" * 72)
    print(f"稿件 {len(index.text)} 字 / 归一化 {len(index.norm)} 字　"
          f"分 {len(index.clauses)} 句")
    print(f"最终指针 {aligner.p}/{len(index.norm)}　"
          f"覆盖率 {aligner.progress * 100:.1f}%　"
          f"停在第 {aligner.index.clause_at(aligner.p)} 句")
    if text_recs:
        print(f"出文本块 {len(text_recs)}：空等 {len(wait_recs)}"
              f"（缓冲未攒够，正常）　实际判断 {len(acted)}")
        if acted:
            print(f"低置信 {len(low_recs)}/{len(acted)}"
                  f"（{len(low_recs) / len(acted) * 100:.0f}%）　"
                  f"重定位 {len(reacq)} 次　回读回退 {len(rew)} 次")

    # 逐句到达时刻 —— 这是"跟读到哪"最直观的证据
    print("-" * 72)
    print("各句首次高亮（audio = 这句话在音频里的位置；lat = 实测延迟）"
          if args.realtime else
          "各句首次高亮（全速回放，audio 为音频内位置）")
    first: dict[int, dict] = {}
    for r in recs:
        if r["asr"] and r["clause"] not in first:
            first[r["clause"]] = r
    for ci in sorted(first)[:14]:
        c = index.clauses[ci]
        r = first[ci]
        lat = f"　lat {r['lat_ms']:>5}ms" if args.realtime else ""
        print(f"  第{ci:>2}句  audio≈{r['audio_ms'] / 1000:>5.1f}s{lat}  "
              f"{c.text[:24]}")

    # 端到端延迟 —— 实测口径
    print("-" * 72)
    mean_infer = statistics.mean(infer) if infer else 0
    if args.truth and not args.realtime:
        print("⚠️  --truth 只在 --realtime 下有意义的，已跳过滞后计算")
        args.truth = ""
    if args.truth:
        with open(args.truth, encoding="utf-8") as fh:
            tmeta = json.load(fh)
        report_lag(recs, index, tmeta)
    elif args.realtime:
        lats = sorted(r["lat_ms"] for r in text_recs)
        if lats:
            print("管道余量（墙上时刻 − 音频已消耗位置；≈0 表示正好跟上）")
            print(f"  均值 {statistics.mean(lats):.0f}ms　"
                  f"p50 {lats[len(lats) // 2]}ms　"
                  f"p95 {lats[int(len(lats) * 0.95)]}ms")
    else:
        print("端到端延迟估算（全速回放测不出真实延迟，下面按拆解估算）")
        print(f"  帧打包 100ms + chunk 累积 {args.chunk_ms}ms + 推理 {mean_infer:.0f}ms"
              f" + 传输/渲染 ~30ms")
        print(f"  ≈ {100 + args.chunk_ms + mean_infer + 30:.0f}ms"
              f"　·　加 --realtime --truth 可测出实测滞后")

    if args.trace:
        print("-" * 72)
        for r in recs:
            if not r["asr"]:
                continue
            flag = ("REACQ " if r["reacquired"] else
                    "REWIND" if r["rewound"] else
                    "wait  " if r["waiting"] else
                    "low   " if r["low"] else "      ")
            print(f"  #{r['seq']:>3} {flag} p={r['p']:>4} cl={r['clause']:>2} "
                  f"conf={r['conf']:.2f} → {r['asr']}")

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n已写入 {args.dump}（{len(recs)} 行）")

    # 判定：全程跟住 = 覆盖率 ≥ 97% 且没有丢弃
    ok = aligner.progress >= 0.97 and s.n_dropped == 0 and not s.error
    print("\n回放判定:", "✓ 全程跟住" if ok else "✗ 未跟住，需要调参")
    if s.error:
        print("  错误：", s.error)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

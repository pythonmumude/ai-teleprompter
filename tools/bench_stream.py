#!/usr/bin/env python3
"""流式模型性能基准：这台机器到底跟不跟得上实时。

为什么必须单独测这个：
    RTF（real-time factor）= 推理耗时 / 音频时长。
    RTF < 1 才叫"跟得上"；RTF ≈ 1 意味着队列只进不出，
    延迟会一路涨到天上去 —— 整个提词器方案就不成立。

    而 funasr 第一次 forward 带着 lazy init 和线程池初始化，非常慢，
    直接拿第一块的数据会得出错误结论。所以必须**丢掉预热块**取稳态值。

跑法：
    ~/.venvs/funasr/bin/python tools/bench_stream.py            # 默认 cpu
    ~/.venvs/funasr/bin/python tools/bench_stream.py --device mps
    ~/.venvs/funasr/bin/python tools/bench_stream.py --all      # 跑一遍对比矩阵
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "tests", "fixtures", "say_sample.wav")
MODEL = "paraformer-zh-streaming"
WARMUP = 5          # 丢掉前 N 块（预热）


def load_wav(path: str):
    import numpy as np
    import soundfile as sf
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000:
        raise SystemExit(f"素材必须是 16kHz，当前 {sr}")
    return np.asarray(audio, dtype="float32")


def bench(device: str, threads: int | None, chunk_size: list[int],
          audio, quiet: bool = False) -> dict:
    import numpy as np
    import torch
    from funasr import AutoModel

    if threads:
        torch.set_num_threads(threads)

    stride = chunk_size[1] * 960            # 每块多少采样点
    enc_lb, dec_lb = 4, 1

    t0 = time.perf_counter()
    model = AutoModel(model=MODEL, device=device, disable_update=True)
    load_s = time.perf_counter() - t0

    n_chunks = (len(audio) - 1) // stride + 1
    cache: dict = {}
    times: list[float] = []
    finals: list[float] = []
    texts: list[str] = []

    for i in range(n_chunks):
        chunk = audio[i * stride:(i + 1) * stride]
        if len(chunk) < stride:
            chunk = np.pad(chunk, (0, stride - len(chunk)))
        is_final = i == n_chunks - 1
        t = time.perf_counter()
        res = model.generate(
            input=chunk, cache=cache, is_final=is_final,
            chunk_size=chunk_size, encoder_chunk_look_back=enc_lb,
            decoder_chunk_look_back=dec_lb,
        )
        dt = (time.perf_counter() - t) * 1000
        (finals if is_final else times).append(dt)
        txt = res[0].get("text", "") if res else ""
        texts.append(txt)

    steady = times[WARMUP:] if len(times) > WARMUP else times
    audio_s = len(audio) / 16000
    mean_ms = statistics.mean(steady) if steady else 0.0
    p95_ms = (sorted(steady)[int(len(steady) * 0.95)] if len(steady) > 1 else mean_ms)
    rtf = mean_ms / (stride / 16000 * 1000)

    out = {
        "device": device,
        "threads": threads or "默认",
        "chunk_ms": chunk_size[1] * 60,
        "load_s": round(load_s, 1),
        "n": len(steady),
        "warmup_ms": round(times[0], 0) if times else 0,
        "mean_ms": round(mean_ms, 0),
        "p95_ms": round(p95_ms, 0),
        "rtf": round(rtf, 3),
        "audio_s": round(audio_s, 1),
        "text": "".join(texts),
    }
    if not quiet:
        print(f"\n=== device={device}  threads={threads or '默认'}  "
              f"chunk={chunk_size[1] * 60}ms ===")
        print(f"模型加载 {load_s:.1f}s　预热首块 {out['warmup_ms']:.0f}ms")
        print(f"稳态 {len(steady)} 块：均值 {mean_ms:.0f}ms　p95 {p95_ms:.0f}ms　"
              f"RTF {rtf:.3f}")
        verdict = ("✓ 跟得上，有余量" if rtf < 0.5 else
                   "△ 勉强跟得上，余量很小" if rtf < 0.85 else
                   "✗ 跟不上实时，队列会堆积")
        print(f"结论：{verdict}")
        # 打印全量拼接结果 —— 用来验证「每块返回的是增量文本」这个核心假设：
        # 如果返回的是累计文本，拼起来会看到大量重复。
        print(f"拼接识别结果：{out['text']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu", choices=["cpu", "mps"])
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--chunk-ms", type=int, default=600, choices=[600, 480, 300])
    ap.add_argument("--all", action="store_true", help="跑对比矩阵")
    args = ap.parse_args()

    audio = load_wav(os.path.abspath(WAV))
    print(f"素材 {os.path.basename(WAV)}：{len(audio) / 16000:.1f}s @16k")

    cmap = {600: [0, 10, 5], 480: [0, 8, 4], 300: [0, 5, 3]}

    if not args.all:
        bench(args.device, args.threads or None, cmap[args.chunk_ms], audio)
        return 0

    results = []
    for device in ("cpu", "mps"):
        for threads in (None, 8):
            for ms in (600, 480):
                try:
                    results.append(bench(device, threads, cmap[ms], audio, quiet=True))
                except Exception as exc:  # noqa: BLE001
                    print(f"  {device}/threads={threads}/{ms}ms 失败："
                          f"{type(exc).__name__}: {str(exc)[:120]}")
    print("\n" + "=" * 74)
    print(f"{'device':<6}{'threads':<9}{'chunk':<8}{'均值ms':<9}{'p95ms':<8}"
          f"{'RTF':<8}{'结论'}")
    print("-" * 74)
    for r in sorted(results, key=lambda x: x["rtf"]):
        v = ("✓ 有余量" if r["rtf"] < 0.5 else
             "△ 余量小" if r["rtf"] < 0.85 else "✗ 跟不上")
        print(f"{r['device']:<6}{str(r['threads']):<9}{str(r['chunk_ms']) + 'ms':<8}"
              f"{r['mean_ms']:<9.0f}{r['p95_ms']:<8.0f}{r['rtf']:<8.3f}{v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

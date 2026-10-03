#!/usr/bin/env python3
"""WebSocket 集成测试客户端：不用浏览器、不用手机，把服务端整条链路验一遍。

验的是「服务端那半条」：
    连接 → hello → 推稿件 → 按 100ms 喂音频 → 收 cursor 消息 → 指针是否跟到稿末

为什么需要它：
    浏览器里出问题，你分不清是前端写错了还是后端没推。
    这个客户端只跟服务端说话，一旦它通过，问题就锁定在网页那一侧。

跑法（先起服务，再跑这个）：
    ~/.venvs/funasr/bin/python server/main.py &
    ~/.venvs/funasr/bin/python tools/ws_test_client.py --realtime
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402
import websockets  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


async def run(args) -> int:
    import soundfile as sf
    audio, sr = sf.read(args.wav, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    assert sr == 16000, sr
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with open(args.script, encoding="utf-8") as fh:
        script = fh.read().strip()

    frame_bytes = 1600 * 2          # 100ms @16k int16
    total_ms = len(pcm) // 2 * 1000 // 16000

    cursors: list[dict] = []
    states: list[dict] = []
    chunks: list[str] = []
    ready: dict = {}

    async with websockets.connect(args.url, max_size=8 * 1024 * 1024,
                                  ping_interval=20) as ws:
        async def reader():
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    m = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = m.get("t")
                if t == "cursor":
                    cursors.append(m)
                elif t == "state":
                    states.append(m)
                elif t == "chunk":
                    chunks.append(m.get("asr", ""))
                elif t == "ready":
                    ready.update(m)
                elif t == "script":
                    ready["clauses"] = len(m.get("clauses", []))
                elif t == "error":
                    print("服务端报错：", m.get("msg"))

        rtask = asyncio.create_task(reader())
        await ws.send(json.dumps({"t": "hello", "sid": "test",
                                  "mime": "video/mp4",
                                  "video": {"width": 1440, "height": 1080}}))
        await ws.send(json.dumps({"t": "script", "text": script}))
        await asyncio.sleep(0.4)
        print(f"服务端就绪：{ready}")

        t0 = time.perf_counter()
        n = 0
        for i in range(0, len(pcm), frame_bytes):
            await ws.send(pcm[i:i + frame_bytes])
            n += 1
            if args.realtime:
                slip = n * 0.1 - (time.perf_counter() - t0)
                if slip > 0:
                    await asyncio.sleep(slip)
        await ws.send(json.dumps({"t": "end"}))
        # 等最后几块回来
        await asyncio.sleep(2.0 if not args.realtime else 1.0)
        wall = time.perf_counter() - t0
        rtask.cancel()
        try:
            await rtask
        except asyncio.CancelledError:
            pass

    # ---------------- 报告 ----------------
    ok = True
    print("-" * 66)
    print(f"音频 {total_ms / 1000:.1f}s　帧 {n} 个（100ms/帧）　"
          f"实测耗时 {wall:.1f}s")
    print(f"收到 cursor {len(cursors)}　chunk {len(chunks)}　state {len(states)}")

    if cursors:
        last = cursors[-1]
        print(f"末次游标 p={last.get('p')} / 进度 {last.get('progress', 0) * 100:.1f}% "
              f"句={last.get('clause')}")
        low = sum(1 for c in cursors if c.get("low"))
        reacq = sum(1 for c in cursors if c.get("reacquired"))
        rew = sum(1 for c in cursors if c.get("rewound"))
        print(f"低置信 {low}　重定位 {reacq}　回读 {rew}")
        ok &= last.get("progress", 0) >= 0.97
        ok &= low < len(cursors) * 0.2
    else:
        print("✗ 一条 cursor 都没收到 —— 服务端的 ASR 或对齐没在跑")
        ok = False

    if states:
        a = states[-1].get("asr", {})
        print(f"服务端 RTF {a.get('rtf')}　单块 {a.get('meanMs')}ms　"
              f"丢弃 {a.get('dropped')}　device={a.get('device')}")
        ok &= (a.get("dropped") or 0) == 0
        if a.get("error"):
            print("服务端错误：", a["error"])
            ok = False

    text = "".join(chunks)
    if text:
        print(f"识别拼接（前 60 字）：{text[:60]}")

    print("\n判定:", "✓ 服务端整条链路通过" if ok else "✗ 有问题，看上面")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8900/")
    ap.add_argument("--wav", default=os.path.join(ROOT, "tests/fixtures/say_clauses.wav"))
    ap.add_argument("--script", default=os.path.join(ROOT, "tests/fixtures/script_sample.txt"))
    ap.add_argument("--realtime", action="store_true", help="按 100ms 实时节奏喂")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

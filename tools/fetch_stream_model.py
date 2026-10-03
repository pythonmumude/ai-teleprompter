#!/usr/bin/env python3
"""拉取并验证 FunASR 流式模型 paraformer-zh-streaming。

为什么要单独一个脚本：
    流式模型没在本地缓存里（本地只有离线模型），首次要下载几百 MB。
    把下载和「能不能用」分开验收 —— 下载完立刻用 3 个 chunk 的静音跑一遍
    真流式推理，确认 cache 跨 chunk 复用这条路是通的，再进主程序。

跑法：
    ~/.venvs/funasr/bin/python tools/fetch_stream_model.py
"""
from __future__ import annotations

import sys
import time

MODEL = "paraformer-zh-streaming"
CHUNK_SIZE = [0, 10, 5]          # 600ms chunk，与主程序默认一致
ENCODER_LOOK_BACK = 4
DECODER_LOOK_BACK = 1
STRIDE = CHUNK_SIZE[1] * 960     # 600ms @ 16kHz


def main() -> int:
    print(f"[1/3] 加载模型 {MODEL}（首次会从 ModelScope 下载，之后走本地缓存）")
    t0 = time.perf_counter()
    try:
        from funasr import AutoModel
    except ImportError as exc:
        print(f"FAIL 导入 funasr 失败：{exc}")
        print("     确认用的是 ~/.venvs/funasr/bin/python")
        return 1

    model = AutoModel(model=MODEL, disable_update=True)
    print(f"      加载完成，耗时 {time.perf_counter() - t0:.1f}s")

    print(f"[2/3] 定位模型目录（确认已落盘、之后可离线）")
    for attr in ("model_path", "model", "kwargs"):
        val = getattr(model, attr, None)
        if isinstance(val, str) and val:
            print(f"      {attr} = {val}")
            break
    else:
        print("      （没能直接读出路径，看下面 AutoModel 的日志）")

    print(f"[3/3] 空跑 3 个 chunk，验证 cache 跨 chunk 复用不报错")
    import numpy as np

    cache: dict = {}
    silent = np.zeros(STRIDE, dtype=np.float32)
    total_ms = 0.0
    for i in range(3):
        is_final = i == 2
        t = time.perf_counter()
        res = model.generate(
            input=silent,
            cache=cache,
            is_final=is_final,
            chunk_size=CHUNK_SIZE,
            encoder_chunk_look_back=ENCODER_LOOK_BACK,
            decoder_chunk_look_back=DECODER_LOOK_BACK,
        )
        dt = (time.perf_counter() - t) * 1000
        total_ms += dt
        text = res[0].get("text", "") if res else ""
        keys = sorted(res[0].keys()) if res else []
        print(f"      chunk{i}  {dt:6.0f} ms  rtf={dt / 600:.3f}  text={text!r}")
        if i == 0:
            print(f"      返回字段：{keys}  <- 这里就能看出有没有 timestamp")

    print(f"\n静音 3 chunk 平均 {total_ms / 3:.0f} ms/chunk（RTF {total_ms / 3 / 600:.3f}）")
    print("注意：静音 chunk 通常比有声快，真实 RTF 要等回放测。")
    print("\n模型就绪，可以离线运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

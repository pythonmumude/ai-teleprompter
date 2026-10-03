#!/usr/bin/env python3
"""造一份**带真值**的回放素材。

为什么需要真值：
    回放只能告诉我们"最终对上了"，但回答不了"光标落后说话人多少毫秒"。
    没有真值就只能靠估算，而延迟评估正是这个项目最需要的硬数字。

做法：
    用系统语音合成**逐句**生成音频（每句一个文件），中间插固定静音，
    拼接成整段。这样每一句在音频里的**精确起始时间是我们自己定的**，
    真值 100% 准确，不依赖任何模型的二次对齐。

    切句直接复用 server/align.py 的 split_clauses —— 保证真值的句子边界
    和对齐器的高亮单元**完全一致**，否则比出来的数没意义。

    顺带一个好处：句间有自然停顿，比一口气念完更接近真实口播，
    也顺便测了停顿后的重新起音。

跑法：
    ~/.venvs/funasr/bin/python tools/make_truth_fixture.py
产出：
    tests/fixtures/say_clauses.wav   整段音频（16k 单声道）
    tests/fixtures/truth.json        每句的真值时间
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402

from server.align import split_clauses  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
FIX = os.path.join(ROOT, "tests", "fixtures")
VOICE = "Tingting"
RATE = 175              # 词/分钟，接近正常口播
GAP_MS = 300            # 句间停顿（换气）
SR = 16000
SILENCE = np.zeros(SR * GAP_MS // 1000, dtype=np.int16)


def synth(text: str, workdir: str, idx: int) -> np.ndarray:
    aiff = os.path.join(workdir, f"c{idx:03d}.aiff")
    wav = os.path.join(workdir, f"c{idx:03d}.wav")
    subprocess.run(["/usr/bin/say", "-v", VOICE, "-r", str(RATE),
                    "-o", aiff, text], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", aiff,
        "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", wav,
    ], check=True)
    import soundfile as sf
    data, sr = sf.read(wav, dtype="int16")
    assert sr == SR, sr
    return np.asarray(data, dtype=np.int16)


def main() -> int:
    with open(os.path.join(FIX, "script_sample.txt"), encoding="utf-8") as fh:
        script = fh.read().strip()

    clauses = split_clauses(script)
    print(f"稿件 {len(script)} 字 → {len(clauses)} 句（与对齐器同一套切法）")

    pieces: list[np.ndarray] = []
    truth: list[dict] = []
    pos = 0                     # 当前累计采样点数
    with tempfile.TemporaryDirectory() as td:
        for i, c in enumerate(clauses):
            audio = synth(c.text, td, i)
            start_ms = pos * 1000 // SR
            pieces.append(audio)
            pos += len(audio)
            end_ms = pos * 1000 // SR
            truth.append({
                "clause": i, "start_ms": start_ms, "end_ms": end_ms,
                "chars": len(c.text), "text": c.text,
                "origStart": c.start, "origEnd": c.end,
            })
            pieces.append(SILENCE)
            pos += len(SILENCE)
            print(f"  句{i:>2} {start_ms / 1000:>6.2f}s "
                  f"({end_ms - start_ms:>4}ms) {c.text[:26]}")

    full = np.concatenate(pieces)
    wav = os.path.join(FIX, "say_clauses.wav")
    import soundfile as sf
    sf.write(wav, full, SR, subtype="PCM_16")
    total_s = len(full) / SR

    meta = {
        "wav": os.path.basename(wav),
        "voice": VOICE, "rate": RATE, "gap_ms": GAP_MS,
        "total_s": round(total_s, 2),
        "chars_total": sum(t["chars"] for t in truth),
        "clauses": truth,
    }
    with open(os.path.join(FIX, "truth.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)

    cps = meta["chars_total"] / total_s
    print(f"\n整段 {total_s:.1f}s　{meta['chars_total']} 字　"
          f"平均 {cps:.2f} 字/秒")
    print(f"已写入 {os.path.relpath(wav, ROOT)} 与 tests/fixtures/truth.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

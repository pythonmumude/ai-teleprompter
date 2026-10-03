#!/usr/bin/env python3
"""音频缓冲与电平/静音门控。

两块东西：

    AudioBuffer —— 前端每 100ms 推一帧 16k int16，这里攒到 600ms 才吐一块给 ASR。
                    为什么要攒：流式模型是按 chunk 解码的，喂进去不足一块它也不会出字。

    LevelMeter  —— 逐帧算 dBFS，并用**自适应噪声底**判断"人是不是停了"。
                    前端也会算同一套（为了最快响应），两边阈值定义保持一致，
                    免得出现"前端说停了、后端还在算"这种自相矛盾。

自适应门限为什么必要：
    固定 -45dBFS 在安静的房间里会一直判成"有声"（环境噪声就到 -42），
    在有底噪的房间里又会一直判成"无声"。所以取**最近 3 秒的最小声**当噪声底，
    门限 = 噪声底 + 9dB。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AudioConfig:
    sample_rate: int = 16000
    frame_ms: int = 100
    chunk_ms: int = 600
    # 固定门限（自适应关闭时用，也作为自适应取值的下界参考）
    gate_db: float = -45.0
    gate_adaptive: bool = True
    gate_margin_db: float = 9.0
    gate_floor_min_db: float = -50.0
    gate_floor_max_db: float = -32.0
    gate_floor_window_frames: int = 30     # 3 秒
    stop_frames: int = 3                   # 连续几帧静音算"停了"
    resume_frames: int = 2                 # 连续几帧有声算"回来了"
    queue_max: int = 40                    # 音频队列上限（10 秒）

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def chunk_samples(self) -> int:
        return self.sample_rate * self.chunk_ms // 1000

    @classmethod
    def from_dict(cls, d: dict) -> "AudioConfig":
        known = {f for f in cls.__dataclass_fields__}          # noqa: SIM118
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


# ---------------------------------------------------------------------------
# 攒块
# ---------------------------------------------------------------------------

class AudioBuffer:
    """把任意长度 int16 数据流攒成固定长度的 chunk。

    最后一块用零补齐 —— 流式模型对输入长度敏感，短块会报错或解码异常。
    """

    def __init__(self, chunk_samples: int) -> None:
        self.chunk_samples = chunk_samples
        self._acc = np.zeros(0, dtype=np.int16)
        self.total_samples = 0          # 累计吃进来的样本数（算音频时间轴用）

    @property
    def pending(self) -> int:
        return len(self._acc)

    @property
    def buffered_ms(self) -> int:
        return len(self._acc) * 1000 // 16000

    def push(self, pcm: np.ndarray) -> list[np.ndarray]:
        """喂一帧，返回所有「已经攒满」的 chunk（通常是 0 或 1 个）。"""
        if pcm.size:
            self._acc = np.concatenate([self._acc, pcm.reshape(-1)])
            self.total_samples += int(pcm.size)

        out: list[np.ndarray] = []
        while len(self._acc) >= self.chunk_samples:
            out.append(self._acc[:self.chunk_samples].copy())
            self._acc = self._acc[self.chunk_samples:]
        return out

    def flush(self) -> np.ndarray | None:
        """收工：把残余补齐成最后一块。"""
        if not len(self._acc):
            return None
        pad = self.chunk_samples - len(self._acc)
        chunk = np.pad(self._acc, (0, pad)).astype(np.int16)
        self._acc = np.zeros(0, dtype=np.int16)
        return chunk

    def reset(self) -> None:
        self._acc = np.zeros(0, dtype=np.int16)
        self.total_samples = 0


# ---------------------------------------------------------------------------
# 电平 / 静音门控
# ---------------------------------------------------------------------------

def dbfs(frame: np.ndarray) -> float:
    """一帧的 dBFS。全零返回 -96（静音底）。"""
    if frame.size == 0:
        return -96.0
    x = frame.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 1e-9:
        return -96.0
    return 20.0 * math.log10(rms)


@dataclass
class LevelState:
    """一帧电平分析结果。前端 rms 部分用同一套阈值，字段名也保持一致。"""
    dbfs: float = -96.0
    threshold_db: float = -45.0
    noise_floor_db: float = -96.0
    speech: bool = False        # 这一帧是否算"有声"
    stopped: bool = False       # 是否刚刚判定"人停了"（边沿触发）
    resumed: bool = False       # 是否刚刚判定"人又开口了"（边沿触发）
    silent_run: int = 0         # 连续静音帧数


class LevelMeter:
    """自适应噪声底的静音门控（带迟滞）。

    为什么要迟滞：不然换气的那一瞬间就会被判成停顿，游标会一抖一抖的。
    """

    def __init__(self, cfg: AudioConfig) -> None:
        self.cfg = cfg
        self._hist: deque[float] = deque(maxlen=cfg.gate_floor_window_frames)
        self._speech_run = 0
        self._silent_run = 0
        self._speaking = False
        self.noise_floor_db = -96.0

    @property
    def speaking(self) -> bool:
        return self._speaking

    @property
    def threshold_db(self) -> float:
        if not self.cfg.gate_adaptive:
            return self.cfg.gate_db
        t = self.noise_floor_db + self.cfg.gate_margin_db
        return max(self.cfg.gate_floor_min_db, min(t, self.cfg.gate_floor_max_db))

    def push(self, frame: np.ndarray) -> LevelState:
        db = dbfs(frame)
        self._hist.append(db)
        # 噪声底 = 最近 3 秒的最小电平。有停顿就必然采到真正的底噪。
        self.noise_floor_db = min(self._hist)
        thr = self.threshold_db

        is_speech = db > thr
        stopped = resumed = False

        if is_speech:
            self._speech_run += 1
            self._silent_run = 0
            if not self._speaking and self._speech_run >= self.cfg.resume_frames:
                self._speaking = True
                resumed = True
        else:
            self._silent_run += 1
            self._speech_run = 0
            if self._speaking and self._silent_run >= self.cfg.stop_frames:
                self._speaking = False
                stopped = True

        return LevelState(
            dbfs=round(db, 1),
            threshold_db=round(thr, 1),
            noise_floor_db=round(self.noise_floor_db, 1),
            speech=is_speech,
            stopped=stopped,
            resumed=resumed,
            silent_run=self._silent_run,
        )

    def reset(self) -> None:
        self._hist.clear()
        self._speech_run = self._silent_run = 0
        self._speaking = False
        self.noise_floor_db = -96.0


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def _selftest() -> int:
    cfg = AudioConfig()
    ok = True

    print("AudioBuffer")
    buf = AudioBuffer(cfg.chunk_samples)          # 9600
    out = buf.push(np.zeros(1600, dtype=np.int16))    # 100ms
    ok &= (out == [] and buf.buffered_ms == 100)
    print(f"  一帧 100ms -> {len(out)} 块，缓冲 {buf.buffered_ms}ms  {'OK' if not out else 'FAIL'}")

    out = buf.push(np.zeros(1600 * 5, dtype=np.int16))   # 再来 500ms -> 满 600ms
    ok &= (len(out) == 1 and out[0].size == 9600)
    print(f"  再 500ms -> {len(out)} 块，每块 {out[0].size if out else 0} 样本  "
          f"{'OK' if len(out) == 1 and out[0].size == 9600 else 'FAIL'}")

    leftover = buf.push(np.zeros(800, dtype=np.int16))
    tail = buf.flush()
    ok &= (leftover == [] and tail is not None and tail.size == 9600)
    print(f"  残余 50ms 补齐 -> {tail.size if tail is not None else 0} 样本  "
          f"{'OK' if tail is not None and tail.size == 9600 else 'FAIL'}")

    print("LevelMeter")
    m = LevelMeter(cfg)
    # 先给一段"底噪"（约 -60dBFS），让自适应噪声底落到低位
    rng = np.random.default_rng(7)
    noise = (rng.normal(0, 30, 16000).astype(np.int16))
    for _ in range(30):
        m.push(noise[:1600])
    floor = m.noise_floor_db
    print(f"  底噪学习后 noise_floor={floor:.1f}dB  门限={m.threshold_db:.1f}dB")

    # 说话：幅度拉满
    loud = (np.sin(np.arange(1600) * 0.3) * 20000).astype(np.int16)
    st = LevelState()
    for _ in range(2):
        st = m.push(loud)
    ok &= (st.resumed and m.speaking)
    print(f"  连续 2 帧有声 -> speaking={m.speaking} resumed={st.resumed}  "
          f"{'OK' if m.speaking else 'FAIL'}")

    # 停下来：3 帧静音才判停
    for i in range(2):
        st = m.push(noise[:1600])
    ok &= (not st.stopped and m.speaking)
    print(f"  静音 2 帧 -> stopped={st.stopped}（应仍算有声，抗换气）  "
          f"{'OK' if not st.stopped else 'FAIL'}")
    st = m.push(noise[:1600])
    ok &= (st.stopped and not m.speaking)
    print(f"  静音 3 帧 -> stopped={st.stopped} speaking={m.speaking}  "
          f"{'OK' if st.stopped else 'FAIL'}")

    print("db 计算")
    ok &= (dbfs(np.zeros(1600, dtype=np.int16)) == -96.0)
    ok &= (-6.5 < dbfs((np.ones(1600) * 16384).astype(np.int16)) < -5.5)
    print(f"  全零 -> {dbfs(np.zeros(1600, dtype=np.int16)):.1f}dB（应为 -96）；"
          f"半幅正弦 -> {dbfs((np.ones(1600) * 16384).astype(np.int16)):.1f}dB（应约 -6）")

    print("\n自检:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

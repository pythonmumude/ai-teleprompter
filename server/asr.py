#!/usr/bin/env python3
"""流式 ASR 引擎：把阻塞的 FunASR 推理关进独立线程。

为什么必须独立线程：
    model.generate() 是纯阻塞的 torch 调用。放在 asyncio 事件循环里，
    会把 HTTP 和 WebSocket 一起冻住 —— 每 600ms 冻住 54ms，
    手机端就会看到一卡一卡的心跳。

为什么用 MPS（这台机器上实测出来的）：
    M1 CPU   RTF ≈ 1.0   ← 根本跟不上，队列只进不出，延迟无限增长
    M1 MPS   RTF ≈ 0.09  ← 快 10 倍
    所以这台机器**必须**走 MPS。CPU 只在 MPS 不可用时兜底，且要明确告警。

关于静音：
    MPS 有 10 倍余量，所以默认**不跳过静音块** —— 让模型自己处理静音
    （它会返回空文本），内部状态最一致。跳过静音只在 CPU 兜底时才需要。
"""
from __future__ import annotations

import queue
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

# 回调签名：on_text(文本, 元信息)  —— **从工作线程调用**
TextCallback = Callable[[str, dict], None]
StatsCallback = Callable[[dict], None]


@dataclass
class AsrConfig:
    model: str = "paraformer-zh-streaming"
    device: str = "mps"
    encoder_chunk_look_back: int = 4
    decoder_chunk_look_back: int = 1
    warmup_chunks: int = 3
    skip_silence: bool = False
    allow_cpu_fallback: bool = True
    chunk_ms: int = 600
    sample_rate: int = 16000
    queue_max: int = 40
    rtf_window: int = 40

    @classmethod
    def from_dict(cls, d: dict) -> "AsrConfig":
        known = set(cls.__dataclass_fields__)          # noqa: SIM118
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class AsrStats:
    """给调试页和日志用的运行状态。"""
    loaded: bool = False
    load_s: float = 0.0
    device: str = ""
    n_chunks: int = 0
    n_silent_skipped: int = 0
    n_dropped: int = 0          # 队列满被迫丢掉的块（>0 就说明跟不上）
    last_ms: float = 0.0
    mean_ms: float = 0.0
    p95_ms: float = 0.0
    rtf: float = 0.0
    backlog: int = 0
    text_chars: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "loaded": self.loaded, "loadS": round(self.load_s, 1),
            "device": self.device, "chunks": self.n_chunks,
            "skipped": self.n_silent_skipped, "dropped": self.n_dropped,
            "lastMs": round(self.last_ms), "meanMs": round(self.mean_ms),
            "p95Ms": round(self.p95_ms), "rtf": round(self.rtf, 3),
            "backlog": self.backlog, "chars": self.text_chars,
            "error": self.error,
        }


class StreamingAsr:
    """带输入队列的流式识别引擎。

    用法：
        asr = StreamingAsr(cfg, on_text=..., on_stats=...)
        asr.start()                     # 非阻塞，模型在工作线程里加载
        asr.feed(chunk_int16, speech=True)
        ...
        asr.flush()                     # 收工，把最后一块补零解码掉
        asr.stop()
    """

    def __init__(self, cfg: AsrConfig, on_text: TextCallback,
                 on_stats: StatsCallback | None = None) -> None:
        self.cfg = cfg
        self.on_text = on_text
        self.on_stats = on_stats
        self._q: queue.Queue = queue.Queue(maxsize=cfg.queue_max)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._model = None
        self._cache: dict = {}
        self._times: deque[float] = deque(maxlen=cfg.rtf_window)
        self._stats = AsrStats(device=cfg.device)
        self._lock = threading.Lock()
        self._consumed = 0          # 已消化多少采样点 —— 换成"音频时间"给延迟测量用

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="asr-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(("stop", None, False))
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def reset(self) -> None:
        """新的一场：清空队列与模型 cache。"""
        with self._lock:
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._cache = {}
            self._times.clear()
            self._stats = AsrStats(loaded=self._stats.loaded,
                                   load_s=self._stats.load_s, device=self._stats.device)

    # ---------------- 投喂 ----------------

    def feed(self, chunk: np.ndarray, speech: bool = True, is_final: bool = False,
             blocking: bool = False) -> bool:
        """塞一块音频。

        blocking=False（实时链路默认）：队列满就**丢最旧**的一块。
            丢新的会让音频断裂；丢旧的只是晚一点出字。实时场景下
            「宁可丢旧数据也别让延迟涨上去」是对的。
        blocking=True（离线回放用）：队列满就等，一块都不丢，能跑多快跑多快。

        返回 False 表示这块被丢掉了（说明实时链路跟不上）。
        """
        item = ("audio", np.asarray(chunk, dtype=np.int16), is_final)
        if blocking:
            self._q.put(item)
            return True
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass
            with self._lock:
                self._stats.n_dropped += 1
            return False

    def flush(self, tail: np.ndarray | None = None, blocking: bool = False) -> None:
        """收工：把残余补齐成最后一块，并让模型吐出最终结果。"""
        if tail is None or not tail.size:
            tail = np.zeros(self._chunk_samples, dtype=np.int16)
        self.feed(tail, speech=True, is_final=True, blocking=blocking)

    def drain(self, timeout: float = 20.0) -> bool:
        """等队列处理完（回放/测试用）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.empty():
                # 队列空了但最后一块可能还在算，多等一小会儿
                time.sleep(0.05)
                if self._q.empty():
                    return True
            time.sleep(0.02)
        return False

    # ---------------- 状态 ----------------

    def stats(self) -> AsrStats:
        with self._lock:
            s = self._stats
            s.backlog = self._q.qsize()
            if self._times:
                s.mean_ms = statistics.mean(self._times)
                s.p95_ms = sorted(self._times)[int(len(self._times) * 0.95)]
                # RTF = 推理耗时 / 音频时长。
                # 注意单位：chunk_samples 是采样点数，要除采样率才是秒。
                chunk_s = self._chunk_samples / self.cfg.sample_rate
                s.rtf = (s.mean_ms / 1000.0) / chunk_s if chunk_s else 0.0
            return s

    @property
    def _chunk_samples(self) -> int:
        return self.cfg.chunk_ms * 16

    # ---------------- 工作线程 ----------------

    def _load(self) -> bool:
        from funasr import AutoModel

        t0 = time.perf_counter()
        for device in (self.cfg.device, "cpu"):
            try:
                self._model = AutoModel(model=self.cfg.model, device=device,
                                        disable_update=True)
                if device != self.cfg.device:
                    msg = (f"MPS 加载失败，已回退 CPU（RTF≈1.0，会跟不上实时，"
                           f"强烈建议排查 MPS）")
                    print(f"⚠️  {msg}")
                    with self._lock:
                        self._stats.error = msg
                with self._lock:
                    self._stats.device = device
                break
            except Exception as exc:  # noqa: BLE001
                if device == "cpu" or not self.cfg.allow_cpu_fallback:
                    with self._lock:
                        self._stats.error = f"{type(exc).__name__}: {exc}"
                    print(f"✗ 模型加载失败：{type(exc).__name__}: {exc}")
                    return False
                print(f"⚠️  device={device} 加载失败，试下一个：{type(exc).__name__}")

        if self._model is None:
            return False

        load_s = time.perf_counter() - t0
        with self._lock:
            self._stats.loaded = True
            self._stats.load_s = load_s

        # 预热：首块带着 lazy init 会慢好几倍（实测 199ms vs 稳态 54ms）。
        # 在加载阶段空跑几次，第一块真实音频就不用背这个锅。
        silent = np.zeros(self._chunk_samples, dtype=np.int16)
        for _ in range(max(0, self.cfg.warmup_chunks)):
            self._generate(silent, False)
        self._cache = {}
        print(f"ASR 就绪：{self.cfg.model} @ {self._stats.device}，"
              f"加载+预热 {load_s:.1f}s")
        return True

    def _generate(self, chunk: np.ndarray, is_final: bool) -> str:
        audio = chunk.astype(np.float32) / 32768.0
        res = self._model.generate(
            input=audio,
            cache=self._cache,
            is_final=is_final,
            chunk_size=self._chunk_list,
            encoder_chunk_look_back=self.cfg.encoder_chunk_look_back,
            decoder_chunk_look_back=self.cfg.decoder_chunk_look_back,
        )
        if not res:
            return ""
        return res[0].get("text", "") or ""

    @property
    def _chunk_list(self) -> list[int]:
        """[0, center, right]，center 以 60ms 为单位。"""
        center = max(1, round(self.cfg.chunk_ms / 60))
        return [0, center, max(1, center // 2)]

    def _run(self) -> None:
        if not self._load():
            return
        while not self._stop.is_set():
            try:
                kind, chunk, is_final = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "stop":
                break

            t0 = time.perf_counter()
            try:
                text = self._generate(chunk, is_final)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._stats.error = f"推理失败：{type(exc).__name__}: {exc}"
                print(f"✗ ASR 推理失败：{type(exc).__name__}: {exc}")
                if is_final:
                    self._cache = {}
                continue
            dt = (time.perf_counter() - t0) * 1000

            with self._lock:
                self._times.append(dt)
                self._stats.last_ms = dt
                self._stats.n_chunks += 1
                if text:
                    self._stats.text_chars += len(text)
                self._consumed += int(np.size(chunk))
                # audio_ms = 这一块覆盖到音频的第几毫秒。
                # 用它和「墙上时间」相减，就是**实测端到端延迟** ——
                # 这是唯一诚实的延迟口径，别拿理论值糊弄。
                audio_ms = self._consumed * 1000 // self.cfg.sample_rate
            if is_final:
                # 一场结束，cache 必须清掉，否则下一场会带着上一场的上下文
                self._cache = {}

            if text:
                try:
                    self.on_text(text, {"ms": dt, "final": is_final,
                                        "audio_ms": audio_ms})
                except Exception as exc:  # noqa: BLE001
                    print(f"⚠️  on_text 回调异常：{type(exc).__name__}: {exc}")

            if self.on_stats is not None and self._stats.n_chunks % 10 == 0:
                try:
                    self.on_stats(self.stats().as_dict())
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# 自检：不开麦克风、不用手机，直接喂合成的"语音"验证引擎能跑通
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from server.audio import AudioConfig, AudioBuffer

    wav = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "tests", "fixtures", "say_sample.wav")
    try:
        import soundfile as sf
        audio, sr = sf.read(wav, dtype="float32")
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：读不到素材 {wav}（{exc}）")
        return 0
    assert sr == 16000
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)

    acfg = AudioConfig()
    results: list[tuple[str, float]] = []

    def on_text(t: str, meta: dict) -> None:
        results.append((t, meta["ms"]))

    eng = StreamingAsr(AsrConfig(chunk_ms=acfg.chunk_ms), on_text=on_text)
    eng.start()
    time.sleep(0.1)

    buf = AudioBuffer(acfg.chunk_samples)
    fsz = acfg.frame_samples
    t0 = time.perf_counter()
    for i in range(0, len(pcm) - fsz, fsz):          # 按 100ms 帧喂，模拟前端
        for ch in buf.push(pcm[i:i + fsz]):
            # blocking=True：离线回放不要丢数据，跑多快算多快
            eng.feed(ch, blocking=True)
    eng.flush(buf.flush(), blocking=True)
    eng.drain()
    wall = time.perf_counter() - t0

    text = "".join(t for t, _ in results)
    s = eng.stats()
    print(f"喂了 {len(pcm) / 16000:.1f}s 音频，墙上耗时 {wall:.1f}s"
          f"（比实时快 {len(pcm) / 16000 / wall:.1f}x）")
    print(f"块数 {s.n_chunks}　均值 {s.mean_ms:.0f}ms　RTF {s.rtf:.3f}　"
          f"device={s.device}　丢弃 {s.n_dropped}")
    print(f"识别：{text[:60]}…")
    ok = (bool(text) and s.rtf < 0.5 and s.n_dropped == 0
          and len(text) > 100 and text.startswith("很多人以为"))
    print("自检:", "全部通过" if ok else "有失败")
    eng.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

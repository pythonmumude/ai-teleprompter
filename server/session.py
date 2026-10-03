#!/usr/bin/env python3
"""会话：一台手机、一份稿件、一个对齐指针。

**单例设计**。单人自用场景下，多连接（手机提词页 + Mac 上的调试页）看到的是
同一场会话，不需要按连接隔离状态 —— 这跟之前歌词副屏那套「一个全局状态 + 多订阅者」
是同一个思路，也省掉一大堆连接间同步的麻烦。

线程模型（三条线程 + 一个事件循环，边界必须清楚）：
    asyncio 事件循环  —— WebSocket 收发、HTTP 请求
    ASR 工作线程      —— 阻塞推理，出字后回调
    音频解析          —— 在事件循环里做（每帧 3.2KB，开销可忽略）
    HTTP 线程         —— 视频分片落盘（ThreadingHTTPServer 自带线程池）

从 ASR 线程往事件循环推消息必须走 `call_soon_threadsafe`，
否则会在错误的线程里碰 asyncio 的对象。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import numpy as np

from . import protocol as P
from .align import AlignConfig, Aligner, ScriptIndex
from .asr import AsrConfig, StreamingAsr
from .audio import AudioBuffer, AudioConfig, LevelMeter
from .macmic import MacMic
from .recorder import Recorder


@dataclass
class Snapshot:
    """给 /api/state 与调试页的状态快照。"""
    script_chars: int = 0
    norm_chars: int = 0
    clauses: int = 0
    p: int = 0
    clause: int = 0
    progress: float = 0.0
    conf: float = 0.0
    dbfs: float = -96.0
    gate_db: float = -45.0
    speaking: bool = False
    frames: int = 0
    chunks_fed: int = 0
    audio_ms: int = 0
    last_asr: str = ""
    asr: dict = field(default_factory=dict)
    rec: dict = field(default_factory=dict)
    clients: int = 0
    started_at: float = 0.0
    audio_source: str = "phone"
    mac_frames: int = 0
    mac_error: str = ""

    def as_dict(self) -> dict:
        return {
            "scriptChars": self.script_chars, "normChars": self.norm_chars,
            "clauses": self.clauses, "p": self.p, "clause": self.clause,
            "progress": round(self.progress, 4), "conf": self.conf,
            "dbfs": self.dbfs, "gateDb": self.gate_db, "speaking": self.speaking,
            "frames": self.frames, "chunksFed": self.chunks_fed,
            "audioMs": self.audio_ms, "lastAsr": self.last_asr,
            "asr": self.asr, "rec": self.rec, "clients": self.clients,
            "audioSource": self.audio_source, "macFrames": self.mac_frames,
            "macError": self.mac_error,
        }


class Session:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.acfg = AudioConfig.from_dict(cfg["audio"])
        self.paths = {
            "root": os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")),
        }

        # 稿件（可以先空着，等手机端推过来）
        self.script_text = ""
        self.script_locked = False        # 由文件指定稿件时置位，不被手机端覆盖
        self.index: ScriptIndex | None = None
        self.aligner: Aligner | None = None

        acl = dict(cfg["asr"])
        acl["chunk_ms"] = self.acfg.chunk_ms
        acl["queue_max"] = self.acfg.queue_max
        self.asr = StreamingAsr(AsrConfig.from_dict(acl), on_text=self._on_text,
                                on_stats=self._on_stats)

        self.buf = AudioBuffer(self.acfg.chunk_samples)
        self.meter = LevelMeter(self.acfg)
        self.rec = Recorder(os.path.join(self.paths["root"], cfg["record"]["dir"]),
                            keep_parts=bool(cfg["record"].get("keep_parts", True)),
                            loudnorm=bool(cfg["record"].get("loudnorm", True)))

        # 音源：phone = 手机上传（默认）／mac = 本机麦克风。
        # Mac 采集的 PCM 走同一个 feed_pcm 入口，两条来源共用后面整条链路
        # （电平 → 停顿检测 → ASR → 对齐），所以切源不影响其它任何逻辑。
        acfg = cfg.get("audio", {}) or {}
        self.audio_source = str(acfg.get("source", "phone"))
        if self.audio_source not in ("phone", "mac"):
            self.audio_source = "phone"
        self.mac_device = str(acfg.get("mac_device", "") or "")
        self.mac = MacMic(on_pcm=lambda pcm: self.feed_pcm(pcm, source="mac"),
                          on_stats=self._on_mac_stats, log=print)

        self.snap = Snapshot(gate_db=self.acfg.gate_db)
        self.snap.audio_source = self.audio_source
        self.frozen = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[asyncio.Queue] = []
        self._last_state_pub = 0.0
        self._pending = np.zeros(0, dtype=np.int16)

    # ---------------- 生命周期 ----------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在事件循环起来之后调用一次。ASR 线程靠它回推消息。"""
        self._loop = loop

    def start(self) -> None:
        self.asr.start()
        self.snap.started_at = time.time()

    def stop(self) -> None:
        self.asr.stop()

    # ---------------- 稿件 ----------------

    def set_script(self, text: str, locked: bool = False) -> dict:
        self.script_text = text or ""
        self.script_locked = locked or self.script_locked
        self.index = ScriptIndex.build(self.script_text) if self.script_text else None
        self.aligner = (Aligner(self.index, AlignConfig.from_dict(self.cfg["align"]))
                        if self.index else None)
        self.snap.script_chars = len(self.script_text)
        self.snap.norm_chars = len(self.index.norm) if self.index else 0
        self.snap.clauses = len(self.index.clauses) if self.index else 0
        self.snap.p = self.snap.clause = 0
        self.snap.progress = 0.0
        return {
            "text": self.script_text,
            "chars": self.snap.script_chars,
            # 每句同时给出「原文偏移」和「归一化偏移」：
            #   原文偏移 → 前端渲染/调试页显示用
            #   归一化偏移 → 把后端推来的 p 换算成句内进度，用在句内扫光上
            # 这样前端一行归一化逻辑都不用写，两边永远不会算不一致。
            "clauses": [
                {"i": i, "start": c.start, "end": c.end, "text": c.text,
                 "normStart": self.index.clause_start_norm(i),
                 "normEnd": (self.index.clause_start_norm(i + 1)
                             if i + 1 < len(self.index.clauses)
                             else len(self.index.norm))}
                for i, c in enumerate(self.index.clauses)
            ] if self.index else [],
        }

    def reset_position(self, clause: int = 0) -> dict | None:
        """回到某一句开头（前端「重置」按钮）。"""
        if not self.aligner:
            return None
        r = self.aligner.jump_to_clause(clause)
        self.buf.reset()
        self.meter.reset()
        self.asr.reset()
        self.asr.start()
        self.snap.p, self.snap.clause = r.p, r.clause
        self.snap.progress = self.aligner.progress
        return self._cursor_msg(r)

    # ---------------- 音频 ----------------

    def feed_pcm(self, data: bytes, source: str = "phone") -> int:
        """收一帧 PCM（int16 LE）。返回吃掉的采样点数。

        手机上传（WS 二进制帧）和本机麦克风采集都走这个入口 —— 区别只在上游
        是谁在喂。音源切到 mac 之后手机那一路直接丢掉，免得两路音频混在一起
        把识别和对齐搅乱（同理切回 phone 时丢掉本机采集的残留）。
        """
        if not data or source != self.audio_source:
            return 0
        pcm = np.frombuffer(data, dtype="<i2")
        if not pcm.size:
            return 0
        pcm = pcm.astype(np.int16)

        # 逐 100ms 帧算电平：停顿检测要用最快的方式，不能等 VAD 更不等 ASR
        fsz = self.acfg.frame_samples
        self._pending = np.concatenate([self._pending, pcm]) if self._pending.size else pcm
        while self._pending.size >= fsz:
            frame = self._pending[:fsz]
            self._pending = self._pending[fsz:]
            st = self.meter.push(frame)
            self.snap.frames += 1
            self.snap.dbfs = st.dbfs
            self.snap.gate_db = st.threshold_db
            was = self.frozen
            self.frozen = not self.meter.speaking
            if self.frozen != was:
                # 冻结状态一变就立刻推一次，前端据此停/起游标
                self._publish_cursor_now()

        for chunk in self.buf.push(pcm):
            self.asr.feed(chunk, speech=self.meter.speaking)
            self.snap.chunks_fed += 1
            self.snap.audio_ms += self.acfg.chunk_ms

        self._maybe_pub_state()
        return int(pcm.size)

    # ---------------- 音源切换（手机麦 / 电脑麦） ----------------

    def audio_devices(self) -> dict:
        """列出本机可用输入设备（avfoundation），供前端下拉选择。"""
        return {"devices": MacMic.list_devices(), "current": self.mac_device,
                "source": self.audio_source}

    def set_audio_source(self, src: str, device: str = "") -> dict:
        if src not in ("phone", "mac"):
            return {"ok": False, "error": "音源只能是 phone 或 mac"}
        was = self.audio_source
        self.audio_source = src
        if device:
            self.mac_device = device
        self.snap.audio_source = src
        if was == "mac" and src != "mac" and self.mac.running:
            # 切回手机麦时把本机采集停掉，别让它白占着麦克风
            self.mac.stop()
        self.broadcast({"t": "audio-source", "source": src,
                        "device": self.mac_device})
        return {"ok": True, "source": src, "device": self.mac_device}

    def _on_mac_stats(self, frames: int) -> None:
        """MacMic 每收一块回调一次 —— 让 /api/state 也能看到实时帧数。

        只在 state_msg() 里刷新是不够的：HTTP 的 /api/state 直接读 snap、
        不经过 WS 广播，那样查出来永远是启动时的值（排查时会被误导）。
        """
        self.snap.mac_frames = frames
        if self.mac.error:
            self.snap.mac_error = self.mac.error

    def mac_start(self, wav_path: str = "") -> dict:
        """开始本机麦克风采集：一份喂识别，同时写 wav 当录像音轨。"""
        if self.audio_source != "mac":
            return {"ok": False, "error": "当前音源不是电脑麦克风"}
        ok, err = self.mac.start(self.mac_device, wav_path)
        self.snap.mac_error = err or self.mac.error
        return {"ok": ok, "error": err, "device": self.mac_device,
                "wav": wav_path}

    def mac_stop(self) -> dict:
        info = self.mac.stop()
        self.snap.mac_frames = info.get("frames", 0)
        self.snap.mac_error = info.get("error", "")
        return {"ok": True, **info}

    def end_utterance(self) -> None:
        """收工：把残余补零解码掉，逼模型吐出最后一块。"""
        tail = self.buf.flush()
        if tail is not None:
            self.asr.flush(tail)

    # ---------------- 订阅 / 广播 ----------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.append(q)
        self.snap.clients = len(self._subs)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)
        self.snap.clients = len(self._subs)

    def broadcast(self, msg: dict) -> None:
        """只在事件循环线程里调用。"""
        data = P.dumps(msg)
        for q in list(self._subs):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                # 调试页卡住不该拖累提词页：丢掉这一条
                pass

    def broadcast_threadsafe(self, msg: dict) -> None:
        """从 ASR 工作线程调用。"""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.broadcast, msg)

    def state_msg(self) -> dict:
        self.snap.asr = self.asr.stats().as_dict()
        self.snap.rec = self.rec.status_dict()
        # 本机麦克风的实时状态：mac 音源下前端 HUD 靠它判断采集是否正常
        self.snap.audio_source = self.audio_source
        self.snap.mac_frames = self.mac.frames
        if self.mac.error:
            self.snap.mac_error = self.mac.error
        return P.state_msg(
            audio={"dbfs": self.snap.dbfs, "gateDb": self.snap.gate_db,
                   "speaking": self.snap.speaking, "frozen": self.frozen,
                   "frames": self.snap.frames, "audioMs": self.snap.audio_ms},
            asr=self.snap.asr,
            align={"p": self.snap.p, "clause": self.snap.clause,
                   "progress": round(self.snap.progress, 4),
                   "conf": self.snap.conf, "lastAsr": self.snap.last_asr},
            rec=self.snap.rec,
        )

    def _maybe_pub_state(self, interval: float = 0.5) -> None:
        now = time.monotonic()
        if now - self._last_state_pub < interval:
            return
        self._last_state_pub = now
        self.snap.speaking = self.meter.speaking
        self.broadcast(self.state_msg())

    def _publish_cursor_now(self) -> None:
        if not self.aligner:
            return
        self.broadcast(self._cursor_msg(None))

    # ---------------- ASR 回调（在 ASR 线程里） ----------------

    def _on_text(self, text: str, meta: dict) -> None:
        if self.aligner is None:
            return
        r = self.aligner.feed(text)
        self.snap.p = r.p
        self.snap.clause = r.clause
        self.snap.progress = self.aligner.progress
        self.snap.conf = r.conf
        self.snap.last_asr = text
        self.broadcast_threadsafe(self._cursor_msg(r))
        self.broadcast_threadsafe(P.chunk_msg(text, meta.get("ms", 0)))

    def _on_stats(self, stats: dict) -> None:
        self.snap.asr = stats
        self.broadcast_threadsafe(P.state_msg(
            audio={"dbfs": self.snap.dbfs, "gateDb": self.snap.gate_db,
                   "speaking": self.meter.speaking, "frozen": self.frozen,
                   "frames": self.snap.frames, "audioMs": self.snap.audio_ms},
            asr=stats, align={"p": self.snap.p, "clause": self.snap.clause,
                              "progress": round(self.snap.progress, 4),
                              "conf": self.snap.conf, "lastAsr": self.snap.last_asr},
            rec=self.snap.rec,
        ))

    def _cursor_msg(self, r) -> dict:
        if r is None:
            # 冻结状态变化时也要推一次游标（前端据此停/起游标），
            # 这时没有新的对齐结果，就用当前指针现组一个。
            assert self.aligner is not None
            from .align import AlignResult
            idx = self.aligner.index
            p = self.aligner.p
            ci = idx.clause_at(p)
            c = idx.clauses[ci] if idx.clauses else None
            r = AlignResult(p=p, orig=idx.orig_of(p), clause=ci,
                            clause_from=c.start if c else 0,
                            clause_to=c.end if c else 0)
        return self.aligner.cursor_json(r, frozen=self.frozen)

    # ---------------- 录像 ----------------

    def rec_begin(self, sid: str, mime: str = "") -> dict:
        st = self.rec.begin(sid, mime)
        msg = {"t": P.DOWN_REC, "sid": sid, "seq": 0, "bytes": 0, "parts": 0,
               "final": "", "durationS": 0.0, "error": ""}
        self.broadcast(msg)
        return st.as_dict()

    def rec_append(self, sid: str, seq: int, data: bytes) -> dict:
        st, err = self.rec.append(sid, seq, data)
        d = st.as_dict()
        out = {"t": P.DOWN_REC, "sid": sid, "seq": seq, "bytes": st.total_bytes,
               "parts": len(st.parts), "final": d["final"],
               "durationS": d["durationS"], "error": err or d["error"]}
        self.broadcast(out)
        return out

    def rec_finalize(self, sid: str) -> dict:
        st = self.rec.finalize(sid)
        self.snap.rec = st.as_dict()
        out = {"t": P.DOWN_REC, "sid": sid, "seq": -1, "bytes": st.total_bytes,
               "parts": len(st.parts), "final": os.path.basename(st.final_path),
               "durationS": round(st.duration_s, 1), "error": st.error}
        self.broadcast(out)
        return out

    def rec_discard(self, sid: str) -> dict:
        """丢弃这一场录像。seq=-2 作为「已丢弃」标记，与 -1（拼接完成）区分。"""
        ok, err = self.rec.discard(sid)
        self.snap.rec = self.rec.status_dict()
        out = {"t": P.DOWN_REC, "sid": sid, "seq": -2, "bytes": 0, "parts": 0,
               "final": "", "durationS": 0.0,
               "ok": ok, "discarded": ok, "error": err}
        self.broadcast(out)
        return out


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

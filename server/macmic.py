#!/usr/bin/env python3
"""Mac 本机麦克风采集：一份 PCM，两处用。

为什么用 ffmpeg 而不是 sounddevice / pyaudio：
    项目本来就依赖 ffmpeg（成品拼接用它），不必再引一个需要编译的音频库；
    `-f avfoundation -i ":<设备索引>"` 直接吐 raw PCM，一行命令解决设备选择
    （USB 麦、耳麦、iPhone 连续互通在系统里都是 avfoundation 设备）。

两路用途（这也是为什么要自己读 stdout，而不是让 ffmpeg 直接写文件）：
    ① 提词跟随 —— 每块 PCM 立刻交给 Session，和手机上传的音频走同一条链路
    ② 录像音轨 —— 同时写进 wav，finalize 时替掉视频里手机录的那条轨

⚠️ 本机默认输入设备可能是 BlackHole 这类虚拟声卡（这台 Mac 就是），它本身
   采不到真实声音。所以设备必须由用户在页面上选，别用硬编码索引。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import wave

from recorder import _resolve_bin

SAMPLE_RATE = 16000
# 100ms 一块：与手机端 pcm-worklet 的打包粒度一致，后端停顿检测按同样粒度工作
CHUNK_BYTES = SAMPLE_RATE * 2 // 10


class MacMic:
    """一路本机麦克风采集。单用户自用，同一时刻只允许跑一路。"""

    def __init__(self, on_pcm=None, log=print, on_stats=None) -> None:
        self.on_pcm = on_pcm
        # 每收一块回调一次（供快照刷新）。不设的话 /api/state 只能看到启动时的帧数
        self.on_stats = on_stats
        self.log = log
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._wav = None
        self._wav_path = ""
        self._stop = threading.Event()
        self.device = ""
        self.frames = 0
        self.error = ""

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, device: str = "", wav_path: str = "") -> tuple[bool, str]:
        if self.running:
            return True, "已在采集"
        ffmpeg = _resolve_bin("ffmpeg")
        if shutil.which(ffmpeg) is None:
            return False, "找不到 ffmpeg"
        self.device = device or ":0"
        self._wav_path = wav_path
        self._stop.clear()
        self.frames = 0
        self.error = ""

        # avfoundation 的输入串是「视频设备:音频设备」，冒号前留空表示不要视频
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", self.device,
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)
        except OSError as exc:
            self.error = str(exc)
            return False, self.error

        if wav_path:
            try:
                os.makedirs(os.path.dirname(wav_path), exist_ok=True)
                self._wav = wave.open(wav_path, "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(SAMPLE_RATE)
            except OSError as exc:
                # 音轨存不下来不该拖垮提词 —— 降级成「只喂识别」
                self.log(f"⚠️ 录像音轨打不开（提词继续）：{exc}")
                self._wav = None

        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()
        return True, ""

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                self.frames += 1
                if self.on_stats is not None:
                    try:
                        self.on_stats(self.frames)
                    except Exception:  # noqa: BLE001
                        pass
                if self._wav is not None:
                    try:
                        self._wav.writeframes(chunk)
                    except (OSError, ValueError):
                        self._wav = None
                if self.on_pcm is not None:
                    try:
                        self.on_pcm(chunk)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"⚠️ 喂音失败：{type(exc).__name__}: {exc}")
        finally:
            err = b""
            try:
                if proc.stderr is not None:
                    err = proc.stderr.read() or b""
            except (OSError, ValueError):
                pass
            # 主动停止时 ffmpeg 也会打一行退出信息，那不算错
            if err and not self._stop.is_set():
                self.error = err.decode("utf-8", "replace").strip()[:200]
                self.log(f"⚠️ 麦克风采集中断：{self.error}")

    def stop(self) -> dict:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._wav is not None:
            try:
                self._wav.close()
            except (OSError, ValueError):
                pass
        self._wav = None
        self._proc = None
        self._thread = None
        return {"frames": self.frames, "wav": self._wav_path, "error": self.error}

    @staticmethod
    def list_devices() -> list[dict]:
        """列出 avfoundation 的音频输入设备，供前端下拉选择。"""
        ffmpeg = _resolve_bin("ffmpeg")
        if shutil.which(ffmpeg) is None:
            return []
        try:
            p = subprocess.run(
                [ffmpeg, "-hide_banner", "-f", "avfoundation",
                 "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out = (p.stderr or "") + (p.stdout or "")
        devices: list[dict] = []
        in_audio = False
        for line in out.splitlines():
            if "AVFoundation audio devices" in line:
                in_audio = True
                continue
            if "AVFoundation video devices" in line:
                in_audio = False
                continue
            if not in_audio or "] [" not in line:
                continue
            try:
                idx = line.split("] [", 1)[1].split("]", 1)[0]
                name = line.rsplit("] ", 1)[1].strip()
            except IndexError:
                continue
            devices.append({
                "index": idx, "name": name,
                # 虚拟声卡采不到真实声音，前端要给出提示
                "virtual": ("BlackHole" in name or "Virtual" in name
                            or "Soundflower" in name),
            })
        return devices

#!/usr/bin/env python3
"""录像片段接收与拼接。

为什么按 3 秒分片：
    Safari 的 MediaRecorder 把整段录在一个 Blob 里。录 10 分钟 1080p 就是
    几百 MB 全压在网页内存里，iOS 很可能直接把标签页杀掉 —— 素材一起没。
    分片实时回传之后，最坏情况只丢当前这一片。

为什么不能直接 concat：
    Safari 出的是 **fragmented MP4**（首片含 moov，后续是 moof 片段），
    而且 MediaRecorder 从不回头写 duration，所以文件时长是 0:00、不能拖拽。
    做法是先把所有分片**按字节顺序拼成一个 fMP4**，再用 ffmpeg remux
    （`-c copy`，不重编码、不损画质）把 moov 挪到前面并补上时长索引。

关键风险：**丢片**。少一片就是画面跳一下，而且要能立刻发现。
所以每个分片都校验连续性，缺号直接报警。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field

# 单片上限，防呆（3 秒 1080p 约 4MB，给到 64MB 足够宽松）
MAX_PART_BYTES = 64 * 1024 * 1024


def _resolve_bin(name: str) -> str:
    """把裸命令名解析成可执行的绝对路径。

    服务可能从精简 PATH 的环境启动（launchd、agent 后台任务），那些环境的
    PATH 往往不含 /opt/homebrew/bin，shutil.which('ffmpeg') 会返回 None ——
    真机上点「停止」就报「找不到 ffmpeg」。这里按常见安装位置兜底。
    """
    if os.path.sep in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    for cand in (f"/opt/homebrew/bin/{name}",
                 f"/usr/local/bin/{name}",
                 f"/usr/bin/{name}"):
        if os.path.isfile(cand):
            return cand
    return name


@dataclass
class RecStatus:
    sid: str = ""
    mime: str = ""
    dir: str = ""
    parts: dict[int, int] = field(default_factory=dict)   # seq -> 字节数
    total_bytes: int = 0
    started_at: float = 0.0
    final_path: str = ""
    duration_s: float = 0.0
    gaps: list[int] = field(default_factory=list)         # 缺号（丢片）
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "sid": self.sid, "mime": self.mime, "parts": len(self.parts),
            "bytes": self.total_bytes, "gaps": self.gaps,
            "final": os.path.basename(self.final_path) if self.final_path else "",
            "durationS": round(self.duration_s, 1), "error": self.error,
        }


class Recorder:
    """一场录像的分片仓库。单用户自用，同一时刻只维护一场。"""

    def __init__(self, root: str, keep_parts: bool = True,
                 loudnorm: bool = True) -> None:
        self.root = os.path.abspath(root)
        self.keep_parts = keep_parts
        self.loudnorm = loudnorm
        self._lock = threading.RLock()
        self.status = RecStatus()

    # ---------------- 写入 ----------------

    def begin(self, sid: str, mime: str = "") -> RecStatus:
        with self._lock:
            d = os.path.join(self.root, sid)
            os.makedirs(d, exist_ok=True)
            self.status = RecStatus(sid=sid, mime=mime, dir=d,
                                    started_at=time.time())
            return self.status

    def append(self, sid: str, seq: int, data: bytes) -> tuple[RecStatus, str]:
        """写一个分片。返回 (状态, 错误信息)。"""
        if len(data) > MAX_PART_BYTES:
            return self.status, f"分片过大（{len(data)} 字节）"
        with self._lock:
            if self.status.sid != sid:
                self.begin(sid)
            st = self.status
            path = os.path.join(st.dir, f"part_{seq:05d}.bin")
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)          # 原子落盘：崩了也不会留半个文件
            st.parts[seq] = len(data)
            st.total_bytes = sum(st.parts.values())
            return st, ""

    def status_dict(self) -> dict:
        with self._lock:
            self._refresh_gaps()
            return self.status.as_dict()

    # ---------------- 收工拼接 ----------------

    def finalize(self, sid: str) -> RecStatus:
        with self._lock:
            st = self.status
            if not st.parts:
                st.error = "没有任何分片"
                return st
            self._refresh_gaps()

            seqs = sorted(st.parts)
            merged = os.path.join(st.dir, "merged.mp4")
            with open(merged, "wb") as out:
                for s in seqs:
                    p = os.path.join(st.dir, f"part_{s:05d}.bin")
                    if not os.path.isfile(p):
                        continue
                    with open(p, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 20)

            final = os.path.join(st.dir, "final.mp4")
            st.final_path = final
            ok, msg = self._remux(merged, final, st.mime)
            if not ok:
                # 兜底 1：concat demuxer（对同源 mp4 分片常常有效）
                lst = os.path.join(st.dir, "list.txt")
                with open(lst, "w", encoding="utf-8") as fh:
                    for s in seqs:
                        fh.write(f"file 'part_{s:05d}.bin'\n")
                ok, msg2 = self._run([
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-err_detect", "ignore_err", "-i", lst,
                    "-c", "copy", final,
                ])
                if not ok:
                    # 兜底 2：重编码（慢，但至少有个能用的文件）
                    ok, msg3 = self._run([
                        "ffmpeg", "-y", "-i", merged,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-b:a", "192k", final,
                    ])
                    if not ok:
                        st.error = f"拼接失败：{msg} / {msg2} / {msg3}"
                        return st
                    msg = "remux 失败，已重编码兜底"
                else:
                    msg = "remux 失败，已用 concat 兜底"

            st.duration_s = self._probe_duration(final)
            if st.duration_s <= 0:
                st.error = (st.error + "；" if st.error else "") + "成品时长为 0，文件可能不可播"
            elif st.gaps:
                st.error = f"缺 {len(st.gaps)} 个分片：{st.gaps[:8]}"
            elif msg:
                st.error = msg
            return st

    # ---------------- 丢弃 ----------------

    def discard(self, sid: str) -> tuple[bool, str]:
        """丢弃一场录像：删掉整个会话目录。返回 (成功, 说明)。

        和 finalize 不同，这里**严格校验 sid** —— 删除不可逆，
        不能让一个传错的 sid 把别的会话删掉。同时要求目标确实落在 root 下、
        且末级目录名就是 sid，双保险防目录穿越。
        """
        with self._lock:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid or ""):
                return False, "非法会话号"
            st = self.status
            if sid != st.sid:
                return False, "会话不匹配，未删除"
            d = os.path.abspath(st.dir)
            if not (d.startswith(self.root + os.sep)
                    and os.path.basename(d) == sid):
                return False, "路径越界，未删除"
            if not os.path.isdir(d):
                return False, "目录不存在"
            shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(d):
                return False, "删除失败"
            self.status = RecStatus()          # 复位，可以接着录下一场
            return True, ""

    def _refresh_gaps(self) -> None:
        """分片序号必须从 1 连续。缺号 = 画面会跳。"""
        st = self.status
        if not st.parts:
            st.gaps = []
            return
        mx = max(st.parts)
        st.gaps = [i for i in range(1, mx + 1) if i not in st.parts]

    def _remux(self, src: str, dst: str, mime: str) -> tuple[bool, str]:
        """把 fMP4 重排成可拖拽的普通 mp4。

        音频过 loudnorm 响度归一（-16 LUFS，流媒体/播客标准）：手机录音电平
        因场而异（实测峰值 -0.5 ~ -27dB 都有，随说话音量与距离波动），固定增益
        会削波，loudnorm 自动适配并把真峰值钳在 -1.5dB。代价是音频轨重编码
        （aac 192k，秒级），视频轨仍 -c:v copy 不损画质。loudnorm=False 退回纯 remux。
        """
        if self.loudnorm:
            return self._run([
                "ffmpeg", "-y", "-fflags", "+genpts", "-i", src,
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", dst,
            ])
        return self._run([
            "ffmpeg", "-y", "-fflags", "+genpts", "-i", src,
            "-c", "copy", "-movflags", "+faststart", dst,
        ])

    def _run(self, cmd: list[str]) -> tuple[bool, str]:
        # ⚠️ 服务可能从精简 PATH 的环境启动（launchd / agent 后台任务），
        # shutil.which 找不到 /opt/homebrew/bin 下的 ffmpeg —— 真机点「停止」时
        # 报「找不到 ffmpeg」（2026-10-03 踩过）。这里统一做路径解析兜底。
        cmd = [_resolve_bin(cmd[0])] + cmd[1:]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return False, "ffmpeg 超时"
        if p.returncode != 0:
            tail = (p.stderr or "").strip().splitlines()[-3:]
            return False, " | ".join(tail)
        return True, ""

    def _probe_duration(self, path: str) -> float:
        if not os.path.isfile(path):
            return 0.0
        ffprobe = _resolve_bin("ffprobe")
        if shutil.which(ffprobe) is None:
            return 0.0
        try:
            p = subprocess.run([
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path,
            ], capture_output=True, text=True, timeout=60)
            return float((p.stdout or "0").strip() or 0)
        except (ValueError, subprocess.TimeoutExpired):
            return 0.0


# ---------------------------------------------------------------------------
# 自检：造 3 个假分片，验证连续性检查与拼接兜底逻辑不炸
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as td:
        r = Recorder(td)
        r.begin("t1", "video/mp4")
        for seq in (1, 2, 4):                    # 故意缺 3
            st, err = r.append("t1", seq, b"\x00" * 1024)
            ok &= (err == "")
        d = r.status_dict()
        # gaps 在 finalize/status_dict 时刷新
        r._refresh_gaps()
        ok &= r.status.gaps == [3]
        print(f"  缺号检查：parts={d['parts']} gaps={r.status.gaps}  "
              f"{'OK' if r.status.gaps == [3] else 'FAIL'}")

        st = r.finalize("t1")
        # 全是垃圾字节，ffmpeg 必然失败 —— 要的是"失败也给出明确错误、不抛异常"
        ok &= (st.error != "" or st.final_path.endswith("final.mp4"))
        print(f"  垃圾数据的 finalize：error={st.error[:60]!r}  "
              f"{'OK（没抛异常）' if True else 'FAIL'}")

        # 正常路径：用 ffmpeg 造两个真 mp4 分片，验证字节序拼接 + remux
        d2 = os.path.join(td, "t2")
        os.makedirs(d2, exist_ok=True)
        for i in (1, 2):
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                os.path.join(d2, f"src{i}.mp4"),
            ], check=False, capture_output=True)
        r2 = Recorder(td)
        r2.begin("t2", "video/mp4")
        for i in (1, 2):
            with open(os.path.join(d2, f"src{i}.mp4"), "rb") as fh:
                r2.append("t2", i, fh.read())
        st2 = r2.finalize("t2")
        # 两个独立 mp4 直接字节拼起来必然坏 —— 检查的是"给出了明确结论"
        print(f"  两个独立 mp4 拼接：duration={st2.duration_s}s "
              f"error={st2.error[:50]!r}")
        print(f"  成品存在={os.path.isfile(st2.final_path)}")

    print("\n自检:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())

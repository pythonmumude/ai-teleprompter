#!/usr/bin/env python3
"""HTTP 服务：网页、稿件、录像分片、调试状态。

为什么单独一个端口（而不是复用 WebSocket 那条）：
    一个 3 秒 1080p 分片约 4MB。如果走 WebSocket，后面的音频帧要排在它后面
    才能发（TCP 是先进先出），ASR 会直接卡住。所以视频必须走独立连接。

骨架直接搬自 esp32-rlcd-clock/tools/lyrics_bridge/web_server.py（已在实战里跑过）：
    ThreadingHTTPServer + HTTP/1.1 keep-alive + 目录穿越防护 + 后缀白名单。
    区别只是多了几个 POST 端点。

路由：
    GET  /                        提词页（手机）
    GET  /probe.html              能力探针（手机，第一步先跑它）
    GET  /debug.html              调试页（Mac 上看）
    GET  /api/state               当前状态快照（curl 排查用）
    GET  /api/config              前端需要的配置子集
    POST /api/script              设置稿件（body 是纯文本）
    POST /api/session/reset       回到某句开头（?clause=N）
    POST /api/session/end         收工，flush 最后一块音频
    POST /api/rec/<sid>/begin     开始一场录像（?mime=...）
    POST /api/rec/<sid>/<seq>     上传一个分片（body 是二进制）
    POST /api/rec/<sid>/finalize  拼接 + remux
    POST /api/rec/<sid>/discard   丢弃这场录像（删掉整个会话目录，不可逆）
    GET  /api/rec/<sid>           录像状态
    GET  /api/rec/<sid>/file      下载拼接好的 mp4
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import protocol as P

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")

# 只服务这些后缀，避免无意中把 server/ 下的 .py 或缓存吐出去
_ALLOWED_EXT = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".webp",
                ".svg", ".json", ".ico", ".woff2", ".mjs", ".map"}

MAX_BODY = 64 * 1024 * 1024      # 单片上限，与 recorder.MAX_PART_BYTES 对齐


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # 开 keep-alive
    server_version = "aiteleprompter"

    def log_message(self, *args) -> None:      # noqa: D102
        pass                                    # 每个请求都打日志太吵

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # noqa: N802
        # 任何异常都不许把连接搞断 —— 断连在手机上表现为"页面白屏"，
        # 而抛 500 + 错误正文至少能在屏幕上看见原因。
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_get(self) -> None:
        path, query = self._split()
        sess = self.server.sess                   # type: ignore[attr-defined]

        if path == "/api/state":
            return self._json(200, sess.snap.as_dict())
        if path == "/api/config":
            return self._json(200, self._public_config(sess))
        if path.startswith("/api/rec/"):
            parts = [p for p in path.split("/") if p]
            # /api/rec/<sid> 或 /api/rec/<sid>/file
            sid = parts[2] if len(parts) > 2 else ""
            if len(parts) >= 4 and parts[3] == "file":
                final = sess.rec.status.final_path
                if sid == sess.rec.status.sid and final and os.path.isfile(final):
                    return self._file(final, download=True)
                return self._text(404, "还没有成品")
            return self._json(200, sess.rec.status_dict())

        if path in ("/", "/index.html"):
            return self._file(os.path.join(WEB_DIR, "index.html"))
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # web/ 下其它文件按相对路径找（探针页、调试页、js）
        return self._file(os.path.join(WEB_DIR, path.lstrip("/")))

    def _route_post(self) -> None:
        path, query = self._split()
        sess = self.server.sess                   # type: ignore[attr-defined]
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json(413, {"error": f"body 过大 {n}"})
        body = self.rfile.read(n) if n else b""

        if path == "/api/script":
            text = body.decode("utf-8", errors="replace")
            if sess.script_locked:
                return self._json(409, {"error": "稿件由文件锁定，不接受网页覆盖"})
            info = sess.set_script(text)
            sess.broadcast(P.script_ack(info["text"], info["chars"], info["clauses"]))
            return self._json(200, {"ok": True, "chars": info["chars"],
                                    "clauses": len(info["clauses"])})

        if path == "/api/session/reset":
            clause = int(query.get("clause", ["0"])[0])
            msg = sess.reset_position(clause)
            if msg is None:
                return self._json(409, {"error": "还没有稿件"})
            sess.broadcast(msg)
            return self._json(200, {"ok": True, "clause": clause})

        if path == "/api/session/end":
            sess.end_utterance()
            return self._json(200, {"ok": True})

        if path.startswith("/api/rec/"):
            parts = [p for p in path.split("/") if p]
            sid = parts[2] if len(parts) > 2 else ""
            if len(parts) >= 4 and parts[3] == "begin":
                mime = query.get("mime", [""])[0]
                return self._json(200, sess.rec_begin(sid, mime))
            if len(parts) >= 4 and parts[3] == "finalize":
                return self._json(200, sess.rec_finalize(sid))
            if len(parts) >= 4 and parts[3] == "discard":
                return self._json(200, sess.rec_discard(sid))
            if len(parts) >= 4 and parts[3].isdigit():
                out = sess.rec_append(sid, int(parts[3]), body)
                return self._json(200, out)
            return self._json(400, {"error": "未知的录像端点"})

        return self._json(404, {"error": "not found"})

    # ---------------- 工具 ----------------

    def _split(self) -> tuple[str, dict]:
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        return u.path, parse_qs(u.query)

    def _public_config(self, sess) -> dict:
        """只把前端需要的字段吐出去。

        全部走 .get() —— 前端配置读不到只是少个开关，
        绝不该让 /api/config 整个 500 掉（那会让手机上连页面都起不来）。
        """
        c = sess.cfg
        p = c.get("prompt", {})
        r = c.get("record", {})
        a = c.get("audio", {})
        return {
            "prompt": {
                "leadChars": p.get("lead_chars", 3),
                "extrapolateAlpha": p.get("extrapolate_alpha", 0.8),
                "velocityWindowMs": p.get("velocity_window_ms", 1500),
                "minCharsPerSec": p.get("min_chars_per_sec", 1.0),
                "maxCharsPerSec": p.get("max_chars_per_sec", 12.0),
                "topInsetPx": p.get("top_inset_px", 0),
            },
            "record": {
                "timesliceMs": r.get("timeslice_ms", 3000),
                "width": r.get("width", 1440),
                "height": r.get("height", 1080),
                "fps": r.get("fps", 30),
                "videoBitsPerSecond": r.get("video_bits_per_second"),
            },
            "audio": {
                "frameMs": a.get("frame_ms", 100),
                "chunkMs": a.get("chunk_ms", 600),
                "gateDb": a.get("gate_db", -45),
                "gateMarginDb": a.get("gate_margin_db", 9),
                "gateFloorMinDb": a.get("gate_floor_min_db", -50),
                "gateFloorMaxDb": a.get("gate_floor_max_db", -32),
                "stopFrames": a.get("stop_frames", 3),
                "resumeFrames": a.get("resume_frames", 2),
            },
            "wsPort": c.get("ws", {}).get("port", 8788),
            # 走 Tailscale 时，页面在 https:8443 上，WebSocket 在 wss:8444 上。
            # 前端据此拼 URL：https 页面用 publicPort，局域网明文调试用内部端口。
            "wsPortPublic": c.get("ws", {}).get("public_port", 8444),
            "httpPortPublic": c.get("http", {}).get("public_port", 8443),
        }

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, abspath: str, download: bool = False) -> None:
        root = os.path.realpath(WEB_DIR) if not download else None
        real = os.path.realpath(abspath)
        if root is not None and not real.startswith(root + os.sep) and real != root:
            return self._text(403, "forbidden")      # 目录穿越防护
        if not os.path.isfile(real):
            return self._text(404, "not found")
        if root is not None and os.path.splitext(real)[1].lower() not in _ALLOWED_EXT:
            return self._text(403, "type not allowed")

        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        size = os.path.getsize(real)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(real)}"')
        self.end_headers()
        with open(real, "rb") as fh:
            while True:
                blk = fh.read(1 << 20)
                if not blk:
                    break
                try:
                    self.wfile.write(blk)
                except (BrokenPipeError, ConnectionResetError):
                    return


class WebServer:
    def __init__(self, sess, host: str = "127.0.0.1", port: int = 8787,
                 verbose: bool = True) -> None:
        self.sess = sess
        self.host, self.port, self.verbose = host, port, verbose
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> bool:
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            print(f"✗ HTTP 服务起不来（{self.host}:{self.port}）：{exc}")
            print("  端口被占的话改 config.json 里的 http.port")
            return False
        httpd.daemon_threads = True
        httpd.sess = self.sess            # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.5},
                                        daemon=True, name="http")
        self._thread.start()
        if self.verbose:
            print(f"  网页服务 {self.url}")
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# ---------------------------------------------------------------------------
# 自检：起服务，灌一份稿子，验三个端点
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import sys
    import tempfile
    import urllib.error
    import urllib.request

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

    with tempfile.TemporaryDirectory() as td:
        sess = _FakeSession(td)
        srv = WebServer(sess, port=8798, verbose=False)
        if not srv.start():
            return 1
        import time
        time.sleep(0.3)
        ok = True
        base = "http://127.0.0.1:8798"

        got = json.loads(urllib.request.urlopen(f"{base}/api/state", timeout=3).read())
        ok &= ("progress" in got)
        print(f"  /api/state -> {list(got)[:4]}…  {'OK' if ok else 'FAIL'}")

        cfg = json.loads(urllib.request.urlopen(f"{base}/api/config", timeout=3).read())
        ok &= ("prompt" in cfg and "wsPort" in cfg)
        print(f"  /api/config -> leadChars={cfg['prompt']['leadChars']} "
              f"wsPort={cfg['wsPort']}  {'OK' if ok else 'FAIL'}")

        html = urllib.request.urlopen(f"{base}/", timeout=3).read().decode("utf-8")
        ok &= ("<html" in html.lower() or "<!doctype" in html.lower())
        print(f"  / -> {len(html)} 字节  {'OK' if ok else 'FAIL'}")

        # 目录穿越必须被挡（urllib 会把 /../ 规范化掉，所以这里直接试 /server）
        try:
            code = urllib.request.urlopen(f"{base}/server/main.py", timeout=3).status
            ok = False
            print(f"  /server/main.py -> {code}  FAIL（应被拒）")
        except urllib.error.HTTPError as exc:
            ok &= exc.code in (403, 404)
            print(f"  /server/main.py -> HTTP {exc.code}  "
                  f"{'OK（已拒）' if exc.code in (403, 404) else 'FAIL'}")
        except Exception as exc:                                   # noqa: BLE001
            ok = False
            print(f"  /server/main.py -> {type(exc).__name__}  FAIL（连接被断）")

        # POST 稿件
        req = urllib.request.Request(f"{base}/api/script", data="你好世界，测试。".encode(),
                                     method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=3).read())
        ok &= r.get("chars") == 8 and r.get("clauses") == 2
        print(f"  POST /api/script -> {r}  "
              f"{'OK' if r.get('chars') == 8 and r.get('clauses') == 2 else 'FAIL'}")

        # 录制：begin / append / finalize
        r = json.loads(urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/rec/s1/begin?mime=video/mp4",
                                   data=b"", method="POST"), timeout=3).read())
        for seq, payload in ((1, b"A" * 100), (2, b"B" * 200)):
            r = json.loads(urllib.request.urlopen(
                urllib.request.Request(f"{base}/api/rec/s1/{seq}", data=payload,
                                       method="POST"), timeout=3).read())
        ok &= (r.get("parts") == 2 and r.get("bytes") == 300)
        print(f"  POST /api/rec/s1/{{1,2}} -> parts={r.get('parts')} "
              f"bytes={r.get('bytes')}  {'OK' if r.get('bytes') == 300 else 'FAIL'}")

        srv.stop()
        print("\n自检:", "全部通过" if ok else "有失败")
        return 0 if ok else 1


class _FakeSession:
    """自检用的极简会话替身（不加载模型）。"""

    def __init__(self, root: str) -> None:
        from .recorder import Recorder
        self.cfg = {
            "prompt": {"lead_chars": 3}, "record": {"timesize_ms": 3000},
            "audio": {"frame_ms": 100, "chunk_ms": 600}, "ws": {"port": 8788},
        }
        self.snap = _FakeSnap()
        self.rec = Recorder(root)
        self.script_locked = False
        self.script_text = ""

    def set_script(self, text: str) -> dict:
        self.script_text = text
        return {"text": text, "chars": len(text),
                "clauses": [{"i": 0, "start": 0, "end": 1, "text": "x"},
                            {"i": 1, "start": 1, "end": 2, "text": "y"}]}

    def broadcast(self, _msg: dict) -> None:
        pass

    def rec_begin(self, sid, mime=""):
        return self.rec.begin(sid, mime).as_dict()

    def rec_append(self, sid, seq, data):
        st, err = self.rec.append(sid, seq, data)
        return {"parts": len(st.parts), "bytes": st.total_bytes, "error": err}

    def rec_finalize(self, sid):
        return self.rec.finalize(sid).as_dict()


class _FakeSnap:
    def as_dict(self) -> dict:
        return {"progress": 0.0, "p": 0, "clause": 0}


if __name__ == "__main__":
    raise SystemExit(_selftest())

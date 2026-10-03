#!/usr/bin/env python3
"""入口：把 HTTP、WebSocket、ASR 线程、对齐、录像拼起来。

端口布局（为什么要两个端口）：
    8787  HTTP  —— 网页、稿件、录像分片上传、调试状态
    8788  WS    —— 上行音频、下行游标
    分成两个是因为录像分片有 4MB，跟音频共用一条 TCP 会把音频堵在后面。

Tailscale 把它们映射成：
    https://<机器>.<tailnet>.ts.net:8443/   → 127.0.0.1:8787
    wss://<机器>.<tailnet>.ts.net:8444/     → 127.0.0.1:8788

在本机上调试不用装 Tailscale —— Safari 认为 localhost 是安全上下文，
直接开 http://127.0.0.1:8787/ 就能拿摄像头和麦克风，整条链路都能验。

跑法：
    ~/.venvs/funasr/bin/python server/main.py
    ~/.venvs/funasr/bin/python server/main.py --script 稿子.txt   # 锁定稿件
    ~/.venvs/funasr/bin/python server/main.py --dry               # 不加载模型，只调界面
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import websockets  # noqa: E402
from websockets.asyncio.server import serve  # noqa: E402

from server import protocol as P  # noqa: E402
from server.session import Session, load_config  # noqa: E402
from server.web import WebServer  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TAILSCALE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def find_tailscale() -> str:
    if os.path.exists(TAILSCALE_CLI):
        return TAILSCALE_CLI
    return shutil.which("tailscale") or ""


def banner(sess: Session, http: WebServer, ws_port: int) -> None:
    h, w = sess.cfg["http"], sess.cfg["ws"]
    print()
    print("=" * 68)
    print("  AI 实时跟读提词器（本地 FunASR，局域网内，不上公网）")
    print("=" * 68)
    print(f"  本机自测（Mac 上直接开，localhost 算安全上下文）:")
    print(f"    提词页   http://127.0.0.1:{h['port']}/")
    print(f"    探针页   http://127.0.0.1:{h['port']}/probe.html")
    print(f"    调试页   http://127.0.0.1:{h['port']}/debug.html")
    print(f"  WebSocket  ws://127.0.0.1:{ws_port}/")
    print()

    ts = find_tailscale()
    if ts:
        print("  手机要用 https，先把两条映射挂上（只需一次，--bg 会持久化）:")
        print(f"    {ts} serve --bg --https={h.get('public_port', 8443)} "
              f"http://127.0.0.1:{h['port']}")
        print(f"    {ts} serve --bg --https={w.get('public_port', 8444)} "
              f"http://127.0.0.1:{ws_port}")
        print(f"    查看地址：{ts} serve status")
        print(f"    手机访问：https://<机器名>.<tailnet>.ts.net:"
              f"{h.get('public_port', 8443)}/")
    else:
        print("  ⚠️  没找到 Tailscale。手机端必须 HTTPS，两条路：")
        print("     1) 装 Tailscale（Mac + iPhone 同一账号，后台开 MagicDNS")
        print("        与 HTTPS Certificates），再用 tailscale serve 映射")
        print("     2) 退回 mkcert 自签证书 + iPhone 装根证书")
    cfg = sess.cfg
    print()
    print(f"  ASR    {cfg['asr']['model']} @ {cfg['asr']['device']}"
          f"（chunk {cfg['audio']['chunk_ms']}ms）")
    print(f"  录像   {cfg['record']['width']}×{cfg['record']['height']}"
          f" @{cfg['record']['fps']}fps，每 {cfg['record']['timeslice_ms']}ms 一片 → "
          f"{os.path.relpath(sess.rec.root, ROOT)}/")
    print(f"  稿件   {'已锁定 ' + str(len(sess.script_text)) + ' 字' if sess.script_text else '等手机端推送'}")
    print("=" * 68)
    print("  Ctrl+C 停止")
    print()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--script", default="", help="预置稿件文件（会锁定，不被网页覆盖）")
    ap.add_argument("--dry", action="store_true", help="不加载 ASR 模型，只调界面")
    ap.add_argument("--device", default="", help="覆盖 config 里的 asr.device")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.device:
        cfg["asr"]["device"] = args.device

    sess = Session(cfg)
    if args.script:
        with open(args.script, encoding="utf-8") as fh:
            info = sess.set_script(fh.read(), locked=True)
        print(f"已预置稿件：{info['chars']} 字 / {len(info['clauses'])} 句")

    loop = asyncio.get_running_loop()
    sess.bind_loop(loop)

    http = WebServer(sess, cfg["http"]["host"], cfg["http"]["port"])
    if not http.start():
        return 1

    if not args.dry:
        sess.start()
    else:
        print("（--dry：跳过 ASR 模型加载，音频会被收下但没有识别结果）")

    async def handler(ws) -> None:
        q = sess.subscribe()
        peer = getattr(ws, "remote_address", None)

        async def pump() -> None:
            try:
                while True:
                    await ws.send(await q.get())
            except (websockets.ConnectionClosed, asyncio.CancelledError):
                pass

        task = asyncio.create_task(pump())
        try:
            # 新连接先把现状推过去：稿件 + 就绪信息 + 状态快照
            await ws.send(P.dumps(P.hello_ack(cfg["asr"]["model"],
                                              sess.asr.stats().device or cfg["asr"]["device"],
                                              cfg["audio"]["chunk_ms"])))
            if sess.script_text and sess.index:
                await ws.send(P.dumps(P.script_ack(
                    sess.script_text, len(sess.script_text),
                    [{"i": i, "start": c.start, "end": c.end, "text": c.text,
                      "normStart": sess.index.clause_start_norm(i),
                      "normEnd": (sess.index.clause_start_norm(i + 1)
                                  if i + 1 < len(sess.index.clauses)
                                  else len(sess.index.norm))}
                     for i, c in enumerate(sess.index.clauses)])))
            await ws.send(P.dumps(sess.state_msg()))

            async for msg in ws:
                if isinstance(msg, (bytes, bytearray, memoryview)):
                    sess.feed_pcm(bytes(msg))
                    continue
                kind, payload = P.parse_up(msg)
                if kind == P.UP_HELLO:
                    mime = str(payload.get("mime", ""))
                    v = payload.get("video") or {}
                    print(f"手机接入 {peer}　{mime or '无录像'}　"
                          f"{v.get('width')}×{v.get('height')}@{v.get('frameRate')}")
                elif kind == P.UP_SCRIPT:
                    if sess.script_locked:
                        await ws.send(P.dumps(P.error_msg("稿件已锁定，忽略网页推送")))
                        continue
                    info = sess.set_script(str(payload.get("text", "")))
                    sess.broadcast(P.script_ack(info["text"], info["chars"],
                                                info["clauses"]))
                    print(f"收到稿件 {info['chars']} 字 / {len(info['clauses'])} 句")
                elif kind == P.UP_RESET:
                    m = sess.reset_position(int(payload.get("clause", 0) or 0))
                    if m:
                        sess.broadcast(m)
                elif kind == P.UP_END:
                    sess.end_utterance()
                elif kind == P.UP_PING:
                    await ws.send(P.dumps({"t": P.DOWN_PONG,
                                           "ts": payload.get("ts", 0)}))
        except websockets.ConnectionClosed:
            pass
        finally:
            task.cancel()
            sess.unsubscribe(q)

    sock = cfg["ws"]
    async with serve(handler, sock["host"], sock["port"],
                     ping_interval=20, ping_timeout=20,
                     max_size=4 * 1024 * 1024) as _server:
        banner(sess, http, sock["port"])
        stop = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
            except NotImplementedError:
                pass
        try:
            await stop
        except asyncio.CancelledError:
            pass

    print("\n正在收尾……")
    http.stop()
    sess.stop()
    print("已停止")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)

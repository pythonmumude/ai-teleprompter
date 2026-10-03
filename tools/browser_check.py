#!/usr/bin/env python3
"""用无头 Chrome 把网页版前端整条链路跑一遍 —— 不用碰手机。

为什么能做到：
    Chrome 有一对开关能造出**假摄像头和假麦克风**：
        --use-fake-ui-for-media-stream     权限自动通过
        --use-fake-device-for-media-stream 生成合成音视频
    于是 getUserMedia / AudioWorklet / WebSocket / MediaRecorder 这些
    "只有手机上才能测"的环节，在电脑上就能全跑一遍。
    localhost 又算安全上下文，连 HTTPS 都不用配。

    （假麦克风放的是合成音，识别不出字 —— 这里验的是**管道通不通**，
      识别准不准在 replay.py 里用已知稿件的真音频验。）

实现上用 Chrome DevTools Protocol，不引 puppeteer —— 已有 websockets 够用。

踩过的四个坑（都写在这儿，省得下次再撞）：

  1) **必须 `--no-sandbox` + 软件渲染**。本环境会拦住 Chrome 的子进程沙箱：
        sandbox initialization failed: Operation not permitted
        GPU process exited unexpectedly: exit_code=5   （连挂 6 次）
        FATAL: GPU process isn't usable. Goodbye.
     Chrome 直接自杀，表现为 DevTools 的 WebSocket "无关闭帧被掐断"，
     看着像协议问题，其实是进程没了。

  2) **Chrome 的 stderr 千万别丢进 DEVNULL**。上面那三行就是答案，
     丢掉了只能瞎猜。

  3) **连浏览器端点 + Target.attachToTarget(flatten)**，别直接连页面端点。
     并且要等 `Target.attachedToTarget` 事件到齐，再发带 sessionId 的命令。

  4) 连上之后 `Runtime.evaluate` 才是可靠的探针；
     `Runtime.enable` 在没有文档的目标上可能挂住。

跑法（先起服务）：
    ~/.venvs/funasr/bin/python tools/browser_check.py
    ~/.venvs/funasr/bin/python tools/browser_check.py --page probe.html --wait 30
    ~/.venvs/funasr/bin/python tools/browser_check.py --chrome "/path/to/chrome"
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import websockets  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SHOT_DIR = os.path.join(ROOT, "tests", "shots")

# 顺序即优先级：真 Chrome 在前（媒体能力最全），headless-shell 兜底
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    *glob.glob(os.path.expanduser(
        "~/.cache/puppeteer/chrome/*/chrome-mac-arm64/"
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")),
    *glob.glob(os.path.expanduser(
        "~/.cache/puppeteer/chrome-headless-shell/*/*/chrome-headless-shell")),
]

# 第 1 条坑写在这里：没有这几个开关，Chrome 会在 1~2 秒内自杀
BASE_FLAGS = [
    "--no-first-run", "--no-default-browser-check",
    "--no-sandbox", "--disable-gpu-sandbox", "--disable-gpu",
    "--enable-unsafe-swiftshader", "--disable-dev-shm-usage",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
]


class CDP:
    """够用就行的 DevTools Protocol 客户端（flat 模式）。"""

    def __init__(self) -> None:
        self.ws = None
        self._id = 0
        self._waiters: dict[int, asyncio.Future] = {}
        self.console: list[str] = []
        self.errors: list[str] = []
        self._reader: asyncio.Task | None = None
        self._attached = asyncio.Event()

    async def connect(self, url: str) -> None:
        self.ws = await websockets.connect(url, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._waiters.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                m = msg.get("method", "")
                p = msg.get("params", {})
                if m == "Target.attachedToTarget":
                    self._attached.set()
                elif m == "Runtime.consoleAPICalled":
                    args = " ".join(
                        str(a.get("value", a.get("description", "")))
                        for a in p.get("args", []))
                    line = f"[{p.get('type')}] {args}"
                    self.console.append(line)
                    if p.get("type") in ("error", "warning"):
                        self.errors.append(line)
                elif m == "Runtime.exceptionThrown":
                    d = p.get("exceptionDetails", {})
                    txt = d.get("exception", {}).get("description") or d.get("text", "")
                    self.errors.append("[异常] " + str(txt).split("\n")[0])
                elif m == "Log.entryAdded":
                    e = p.get("entry", {})
                    line = f"[{e.get('level')}] {e.get('text')}"
                    self.console.append(line)
                    if e.get("level") == "error":
                        self.errors.append(line)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def call(self, method: str, params: dict | None = None,
                   sess: str = "", timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[mid] = fut
        payload: dict = {"id": mid, "method": method, "params": params or {}}
        if sess:
            payload["sessionId"] = sess
        await self.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"{method} 超时（{timeout}s）") from exc

    async def js(self, expr: str, sess: str, *, await_promise: bool = False,
                 gesture: bool = False, timeout: float = 30.0):
        r = await self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": gesture,
        }, sess, timeout)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            raise RuntimeError((d.get("text", "") + " " + str(
                d.get("exception", {}).get("description", "")))[:300])
        return res.get("result", {}).get("value")

    async def shot(self, path: str, sess: str) -> None:
        r = await self.call("Page.captureScreenshot",
                            {"format": "png", "captureBeyondViewport": False}, sess)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(r["result"]["data"]))

    async def attach(self, url: str) -> tuple[str, str]:
        """建目标并挂上去，返回 (targetId, sessionId)。"""
        r = await self.call("Target.createTarget", {"url": url})
        tid = r["result"]["targetId"]
        self._attached.clear()
        r = await self.call("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = r["result"]["sessionId"]
        # 等 attachedToTarget 事件到齐再发带 sessionId 的命令，否则会被拒/断连
        try:
            await asyncio.wait_for(self._attached.wait(), 3.0)
        except asyncio.TimeoutError:
            await asyncio.sleep(0.4)
        return tid, sid

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass


def wait_devtools(port: int, timeout: float = 20.0) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                        timeout=1) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return None


def launch(binary: str, port: int, profile: str) -> tuple[subprocess.Popen, str]:
    """起 Chrome，stderr 落到文件（坑 2：别丢，出事全靠它）。"""
    log = os.path.join(profile, "chrome.log")
    fh = open(log, "w")  # noqa: SIM115
    args = [binary, "--headless=new" if "for Testing" in binary or
            "Google Chrome.app" in binary else "--headless",
            f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            *BASE_FLAGS, "about:blank"]
    p = subprocess.Popen(args, stdout=fh, stderr=fh)
    fh.close()
    return p, log


def chrome_crash_tail(log: str, lines: int = 6) -> str:
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            tail = fh.read().strip().splitlines()[-lines:]
        return "\n     ".join(tail)
    except OSError:
        return "(没有日志)"


async def run_with(binary: str, args, page_url: str, debug_port: int) -> tuple[bool, str]:
    """用指定浏览器跑一遍。返回 (是否可用, 说明)。"""
    profile = tempfile.mkdtemp(prefix="tp-chrome-")
    proc, log = launch(binary, debug_port, profile)
    cdp = CDP()
    try:
        ver = wait_devtools(debug_port)
        if not ver:
            return False, f"调试端口没起来；Chrome 日志尾：\n     {chrome_crash_tail(log)}"
        print(f"  浏览器 {ver.get('Browser')}"
              f"{'（headless-shell）' if 'HeadlessChrome' in str(ver.get('Browser')) else ''}")

        await cdp.connect(ver["webSocketDebuggerUrl"])
        tid, sid = await cdp.attach(page_url)
        await cdp.call("Runtime.enable", {}, sid)
        await cdp.call("Log.enable", {}, sid)
        await cdp.call("Page.enable", {}, sid)
        # 手机视口，env(safe-area-inset-*) 才有值
        await cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": args.width, "height": args.height, "deviceScaleFactor": 2,
            "mobile": True,
            "screenOrientation": {"type": "portraitPrimary", "angle": 0},
        }, sid)
        await cdp.call("Emulation.setUserAgentOverride", {
            "userAgent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
                          "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                          "Version/18.0 Mobile/15E148 Safari/604.1"),
        }, sid)
        await cdp.call("Emulation.setTouchEmulationEnabled", {"enabled": True}, sid)
        await asyncio.sleep(2.0)

        basics = await cdp.js("""(() => ({
            ready: document.readyState,
            prompter: typeof window.Prompter,
            capture: typeof window.Capture,
            wipeOk: !window.PrompterUnsupported,
            secure: window.isSecureContext,
            md: !!navigator.mediaDevices,
            mr: typeof MediaRecorder !== 'undefined',
            mime: (() => { try { const l = ['video/mp4;codecs=avc1.42E01E,mp4a.40.2',
              'video/mp4','video/webm;codecs=vp9,opus']; for (const x of l)
              { if (MediaRecorder.isTypeSupported(x)) return x; } } catch(e) {}
              return ''; })(),
        }))()""", sid)
        print(f"  基础能力 {json.dumps(basics, ensure_ascii=False)}")
        # 探针页故意不引 prompt.js / capture.js（它要自己量能力），所以只在提词页要求
        if args.page.startswith("index"):
            if basics.get("prompter") != "function" or basics.get("capture") != "function":
                return False, "前端脚本没加载成功（prompt.js / capture.js）"
        if not basics.get("secure") or not basics.get("md"):
            return False, "不是安全上下文或没有 mediaDevices（HTTPS 没配好）"

        if args.page.startswith("index"):
            await cdp.js("""(() => {
                const t = document.getElementById('script');
                t.value = %s;
                t.dispatchEvent(new Event('input'));
                return t.value.length;
            })()""" % json.dumps(args.script), sid)
            print(f"  稿件已填，点「开始提词」，等 {args.wait}s")
            # userGesture=true 才是"真的点了一下"，权限才给
            await cdp.js("document.getElementById('btnStart').click(); true",
                         sid, gesture=True)
            await asyncio.sleep(args.wait)

            st = await cdp.js("""(() => {
                const g = (id) => (document.getElementById(id) || {}).textContent || '';
                const v = document.getElementById('cam');
                return {
                  chips: { ws: g('chipWs'), asr: g('chipAsr'),
                           lvl: g('chipLvl'), rec: g('chipRec') },
                  kv: g('kv').slice(0, 240),
                  setupHidden: !document.getElementById('setup').classList.contains('show'),
                  clauseEls: document.querySelectorAll('#lines .cl').length,
                  curEls: document.querySelectorAll('#lines .cl.cur').length,
                  bandH: document.getElementById('band').clientHeight,
                  bandTop: getComputedStyle(document.getElementById('band')).top,
                  video: { w: v.videoWidth, h: v.videoHeight, paused: v.paused },
                };
            })()""", sid)
            print("  页面状态：")
            print("   " + json.dumps(st, ensure_ascii=False, indent=2).replace("\n", "\n   "))

        if args.page.startswith("probe"):
            # 探针页也要点一下才开始（getUserMedia 需要在用户手势里）
            print(f"  点「开始探测」，最多等 {args.probe_wait}s")
            await cdp.js("document.getElementById('go').click(); true", sid, gesture=True)
            end = time.time() + args.probe_wait
            last = ""
            while time.time() < end:
                await asyncio.sleep(2)
                txt = await cdp.js(
                    "(document.getElementById('out').innerText || '').slice(0, 4000)", sid)
                if txt == last and "结论" in txt:
                    break
                last = txt
            st = await cdp.js("""(() => ({
                disabled: document.getElementById('go').disabled,
                summary: document.getElementById('sum').innerText,
                rows: document.querySelectorAll('#out tr').length,
                bad: Array.from(document.querySelectorAll('#out .bad')).map(e =>
                        e.parentNode.innerText.replace(/\\s+/g,' ')).slice(0, 8),
                warn: Array.from(document.querySelectorAll('#out .warn')).map(e =>
                        e.parentNode.innerText.replace(/\\s+/g,' ')).slice(0, 8),
            }))()""", sid)
            print(f"  探测完成={not st['disabled']}　表格 {st['rows']} 行")
            print("  汇总：")
            for line in (st["summary"] or "").splitlines():
                print("    " + line)
            if st["bad"]:
                print(f"  ⚠️ 不利项 {len(st['bad'])} 条：")
                for x in st["bad"]:
                    print("     " + x[:150])
            if st["warn"]:
                print(f"  注意项 {len(st['warn'])} 条：")
                for x in st["warn"]:
                    print("     " + x[:150])

        # 服务端侧的真实证据（HTTP 直查，不依赖浏览器）
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{args.http_port}/api/state", timeout=5) as r:
                sv = json.loads(r.read())
            a = sv.get("asr") or {}
            print(f"  服务端快照：音频帧 {sv.get('frames')}　攒块 {sv.get('chunksFed')}　"
                  f"电平 {sv.get('dbfs')}dB　对齐 p={sv.get('p')}　"
                  f"RTF {a.get('rtf')}　录像分片 {(sv.get('rec') or {}).get('parts')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  取服务端快照失败：{exc}")

        await cdp.shot(os.path.join(SHOT_DIR,
                                    args.page.replace(".html", "") + ".png"), sid)
        print(f"  截图 tests/shots/{args.page.replace('.html', '')}.png")

        if cdp.errors:
            print(f"  ⚠️ 控制台报错 {len(cdp.errors)} 条：")
            for e in cdp.errors[:8]:
                print("     " + e[:180])
            return True, "有控制台报错"
        print("  控制台干净")
        return True, ""
    finally:
        await cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


async def main_async(args) -> int:
    url = f"http://127.0.0.1:{args.http_port}/{args.page}"
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{args.http_port}/api/config", timeout=3):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 服务没起（{args.http_port}）：{exc}")
        print("  先跑 ./run.sh")
        return 1

    bins = [args.chrome] if args.chrome else CHROME_CANDIDATES
    bins = [b for b in bins if b and os.path.exists(b)]
    if not bins:
        print("✗ 找不到任何 Chrome")
        return 1
    print(f"打开 {url}")

    last = ""
    for i, b in enumerate(bins):
        print(f"[{i + 1}/{len(bins)}] 试 {os.path.basename(b)}")
        try:
            ok, note = await run_with(b, args, url, args.debug_port)
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {exc}"
        if ok:
            print("\n判定:", "✓ 前端整条链路通过" if not note else f"△ 通过但有警告：{note}")
            return 0 if not note else 2
        last = note
        print(f"  不可用：{note}\n")

    print("✗ 所有浏览器都不可用。最后一条原因：")
    print("  " + last)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html")
    ap.add_argument("--http-port", type=int, default=8899)
    ap.add_argument("--debug-port", type=int, default=9333)
    ap.add_argument("--width", type=int, default=390)
    ap.add_argument("--height", type=int, default=844)
    ap.add_argument("--wait", type=float, default=14.0)
    ap.add_argument("--probe-wait", type=float, default=40.0)
    ap.add_argument("--chrome", default="", help="指定浏览器可执行文件")
    ap.add_argument("--script", default=(
        "很多人以为自律是靠意志力硬扛，其实不是。意志力是一种会消耗的资源。"
        "你早上用它拒绝了一次甜点，中午就更难拒绝第二次。"))
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

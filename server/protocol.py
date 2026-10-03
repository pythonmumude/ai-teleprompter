#!/usr/bin/env python3
"""WebSocket 消息协议。

一条连接两用：**上行二进制帧是音频，上行文本帧是 JSON 控制消息**。
这样音频不用额外开销打包成 JSON/base64（base64 会让 3.2KB 的帧变成 4.3KB）。

为什么视频分片不走这条连接：
    一个 3 秒的 1080p 分片约 4MB，走同一条 TCP 会把后面的音频帧排在它后面
    （head-of-line blocking），ASR 直接卡住。所以视频走独立 HTTP POST。

命名约定：上行（手机→电脑）用动词，下行（电脑→手机）用名词。
"""
from __future__ import annotations

import json
from typing import Any

# ---------------- 上行：iPhone → Mac ----------------

UP_HELLO = "hello"      # {"t":"hello","sid","mime","video":{...},"audio":{...}}
UP_SCRIPT = "script"    # {"t":"script","text":"全稿"}
UP_RESET = "reset"      # {"t":"reset","clause":0}     可选，跳到某句
UP_END = "end"          # {"t":"end"}                  收工，flush 最后一块
UP_PING = "ping"        # {"t":"ping","ts":123}

UP_KINDS = {UP_HELLO, UP_SCRIPT, UP_RESET, UP_END, UP_PING}

# ---------------- 下行：Mac → iPhone ----------------

DOWN_READY = "ready"    # {"t":"ready","model","device","chunkMs"}
DOWN_CURSOR = "cursor"  # 见 Aligner.cursor_json
DOWN_CHUNK = "chunk"    # {"t":"chunk","asr":"本块识别文本"}   只给调试页
DOWN_STATE = "state"    # {"t":"state","audio":{...},"asr":{...},"rec":{...}}
DOWN_REC = "rec"        # {"t":"rec","sid","seq","bytes","parts"}
DOWN_SCRIPT = "script"  # {"t":"script","text","chars","clauses"}
DOWN_ERROR = "error"    # {"t":"error","msg"}
DOWN_PONG = "pong"      # {"t":"pong","ts"}


def dumps(obj: dict) -> str:
    """下行消息一律走这个，保证中文不转义（调试页直接看得懂）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def parse_up(raw: str) -> tuple[str, dict[str, Any]]:
    """解析上行文本帧。返回 (kind, payload)；kind 为空串表示无法识别。"""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return "", {}
    if not isinstance(msg, dict):
        return "", {}
    kind = str(msg.get("t", ""))
    if kind not in UP_KINDS:
        return "", {}
    return kind, msg


def hello_ack(model: str, device: str, chunk_ms: int) -> dict:
    return {"t": DOWN_READY, "model": model, "device": device, "chunkMs": chunk_ms}


def script_ack(text: str, chars: int, clauses: list[dict]) -> dict:
    return {"t": DOWN_SCRIPT, "text": text, "chars": chars, "clauses": clauses}


def chunk_msg(text: str, infer_ms: float) -> dict:
    return {"t": DOWN_CHUNK, "asr": text, "ms": round(infer_ms)}


def error_msg(msg: str) -> dict:
    return {"t": DOWN_ERROR, "msg": msg}


def state_msg(*, audio: dict, asr: dict, align: dict, rec: dict) -> dict:
    return {"t": DOWN_STATE, "audio": audio, "asr": asr, "align": align, "rec": rec}

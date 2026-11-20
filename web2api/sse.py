# -*- coding: utf-8 -*-
"""各家网页版的流式响应 → OpenAI chunk 的映射。

网页版的 SSE 帧格式各不相同，但骨架相似：每个帧是一个 JSON，可能带
增量文本、结束标志或推理内容。映射层只做两件事：

  1. 从一帧里取出增量文本与推理文本
  2. 产出 OpenAI 的 chunk

各家的具体字段名由 adapter 提供，本模块不认识任何一家。
"""
from __future__ import annotations

import json
import time
import uuid


def chunk(model, delta, finish_reason=None, usage=None, created=None):
    """One OpenAI chat.completion.chunk."""
    body = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
        "object": "chat.completion.chunk",
        "created": int(created or time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage:
        body["usage"] = usage
    return "data: " + json.dumps(body, ensure_ascii=False) + "\n\n"


def done():
    return "data: [DONE]\n\n"


def parse_sse(raw):
    """Split an SSE stream into decoded JSON payloads.

    Lines that are not "data: ..." are ignored; "[DONE]" ends the stream.
    """
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            out.append(json.loads(payload))
        except ValueError:
            continue
    return out


def first_text(payload):
    """Best-effort: pull the first non-empty text field out of a frame."""
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return ""
    for key in ("content", "text", "delta", "message", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

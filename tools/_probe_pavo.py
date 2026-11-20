# -*- coding: utf-8 -*-
"""Probe Pavo AI's internal API from its Next.js bundles.

Pavo (app.pavo-ai.cn) is an AI video / short-drama platform. Its web app
talks a clean internal API on https://api.pavo-ai.cn/api — which makes it a
much better muse2api-style target than a chat service: the models it fronts
(Seedance 2.5, Kling 4.0, Seedream 5.5, GPT Image 2.6) are expensive via
the official APIs.

Two things this script establishes:
  1. the API base path (there is an "/api" prefix that is easy to miss)
  2. which endpoints are public vs need a session

Usage:
    python tools/_probe_pavo.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PROXY = os.environ.get("CAPTURE_PROXY", "http://127.0.0.1:7890")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/160.0.0.0 Safari/537.36")
SITE = "https://app.pavo-ai.cn/"


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(8_000_000).decode("utf-8", "replace")


def api(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Origin": "https://app.pavo-ai.cn",
               "Referer": "https://app.pavo-ai.cn/"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def main():
    print("=== 抓 chunk 里的接口路径 ===")
    html = fetch(SITE)
    chunks = list(re.findall(r'/_next/static/chunks/([\w.-]+\.js)', html))
    paths = set()
    bases = set()
    for c in chunks[:22]:
        try:
            js = fetch("https://app.pavo-ai.cn/_next/static/chunks/" + c)
        except Exception:  # noqa: BLE001
            continue
        paths.update(re.findall(r'["\'](/v1/[a-zA-Z0-9/_.{}$-]{2,80})["\']', js))
        for m in re.finditer(r'NEXT_PUBLIC_API_URL\s*:\s*"([^"]+)"', js):
            bases.add(m.group(1))
        for m in re.finditer(r'"(https?://[a-z0-9.-]*pavo[a-z0-9.-]*/api)"', js, re.I):
            bases.add(m.group(1))
    print("发现 %d 条 /v1 路径" % len(paths))
    for b in sorted(bases):
        print("  API base:", b)
    for p in sorted(paths)[:40]:
        print("   ", p)

    base = next((b for b in bases if b.endswith("/api")), "https://api.pavo-ai.cn/api")
    print("\n=== 公开端点（无需登录）===")
    for path in ("/v1/pixa/models",
                 "/v1/aigc/model-capability-spec?model_code=seedance-2-5",
                 "/v1/user/auth_methods",
                 "/v1/aigc/credits/estimate"):
        method = "POST" if "estimate" in path else "GET"
        r = api(method, base + path, {} if method == "POST" else None)
        code = r.get("code")
        msg = r.get("message")
        print("  %-52s %s %s" % (path, code, msg))
        if path.endswith("/models") and code == "000000":
            models = ((r.get("data") or {}).get("models") or [])
            print("      模型 %d 个：" % len(models))
            for m in models:
                print("        %-24s %-26s %s" % (m.get("model_code"),
                                                  m.get("model_alias"),
                                                  m.get("model_type")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

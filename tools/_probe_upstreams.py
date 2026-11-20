# -*- coding: utf-8 -*-
"""Survey free web AI chat services: can their internal API be called?

For each site this fetches the landing page, follows its main JS bundle and
greps for internal API paths. The question that decides whether a service is
worth wrapping: does the web app talk a clean JSON/SSE API, or is the
capability locked inside the UI?

Usage:
    python tools/_probe_upstreams.py
"""
from __future__ import annotations

import re
import sys
import urllib.request

PROXY = "http://127.0.0.1:7890"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/160.0.0.0 Safari/537.36")

TARGETS = [
    ("deepseek", "https://chat.deepseek.com/"),
    ("mistral", "https://chat.mistral.ai/chat"),
    ("qwen", "https://chat.qwen.ai/"),
    ("kimi", "https://www.kimi.com/"),
    ("doubao", "https://www.doubao.com/chat/"),
    ("perplexity", "https://www.perplexity.ai/"),
    ("grok", "https://grok.com/"),
]


def fetch(url, limit=3_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    with opener.open(req, timeout=45) as resp:
        return resp.read(limit).decode("utf-8", "replace")


def main():
    for name, url in TARGETS:
        print("\n" + "=" * 66)
        print("### %s  (%s)" % (name, url))
        try:
            html = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print("  页面取不到:", type(exc).__name__, exc)
            continue
        print("  页面 %d 字节" % len(html))

        bundles = re.findall(r'src="(https?://[^"]+\.js)"', html)
        if not bundles:
            bundles = re.findall(r'src="(/[^"]+\.js)"', html)
            bundles = [url.rstrip("/") + b if b.startswith("/") else b
                       for b in bundles]
        picked = 0
        api_paths = set()
        for bundle in bundles[:3]:
            if not bundle.startswith("http"):
                continue
            try:
                js = fetch(bundle)
            except Exception:  # noqa: BLE001
                continue
            picked += 1
            api_paths.update(re.findall(r'["\'](/api/[a-zA-Z0-9/_.{}$-]{3,60})["\']',
                                        js))
            api_paths.update(re.findall(
                r'["\'](https?://[a-z0-9.-]*(?:api|chat|conversation)[a-z0-9.-]*'
                r'/[a-zA-Z0-9/_.{}$-]{3,60})["\']', js))
        print("  取了 %d 个 JS 包" % picked)
        interesting = [p for p in sorted(api_paths)
                       if not re.search(r"trpc|graphql|\.map$|\.png|\.svg|webp|"
                                        r"analytics|telemetry|log|config|cdn", p, re.I)]
        for p in interesting[:22]:
            print("    ", p)
        if not interesting:
            print("    （没抓到内部 API 路径——可能走了 GraphQL/trpc，或包没取到）")


if __name__ == "__main__":
    sys.exit(main())

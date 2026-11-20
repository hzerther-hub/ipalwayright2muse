# -*- coding: utf-8 -*-
"""内部 API 抓取——让新服务的接入从"逆向 JS 包"变成"看一次真实流量"。

做法：用 CDP 打开已登录的浏览器会话，监听网络请求，在页面里触发一次
真实交互，然后把那条内部请求的形状原样录下来：

  请求   URL / 方法 / 关键请求头 / body
  响应   状态 / content-type / 前若干字节（流式接口看 SSE 帧）

对 DeepSeek 这类"接口藏在懒加载 chunk 里"的服务尤其有用——静态扒包
扒不到，但浏览器跑一次就有了。

用法：
    python -m web2api.capture --site https://chat.deepseek.com/ \
        --pattern completion --out capture_deepseek.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tools"))

CAPTURE_JS = r"""
(() => {
    window.__m2a_captured = [];
    if (window.__m2a_hooked) return 'ALREADY';
    window.__m2a_hooked = true;
    const origFetch = window.fetch;
    window.fetch = function(input, init) {
        try {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            const method = (init && init.method) || (input && input.method) || 'GET';
            const headers = {};
            try {
                const h = (init && init.headers) || (input && input.headers);
                if (h) {
                    if (typeof h.forEach === 'function' && !Array.isArray(h)) {
                        h.forEach((v, k) => { headers[k] = v; });
                    } else if (h instanceof Headers) {
                        h.forEach((v, k) => { headers[k] = v; });
                    } else { Object.assign(headers, h); }
                }
            } catch (e) {}
            let body = '';
            try { if (init && init.body) body = String(init.body).slice(0, 4000); } catch (e) {}
            const rec = {url: url, method: method, headers: headers, body: body,
                         status: null, contentType: '', head: '', t: Date.now()};
            window.__m2a_captured.push(rec);
            const finish = (status, ct, head) => {
                rec.status = status; rec.contentType = ct || ''; rec.head = head || '';
            };
            return origFetch.apply(this, arguments).then(resp => {
                finish(resp.status, resp.headers.get('content-type'), '');
                try {
                    const clone = resp.clone();
                    const reader = clone.body.getReader();
                    const dec = new TextDecoder();
                    let got = '';
                    (function pump() {
                        return reader.read().then(r => {
                            if (r.done || got.length > 6000) { rec.head = got.slice(0, 6000); return; }
                            got += dec.decode(r.value, {stream: true});
                            return pump();
                        });
                    })();
                } catch (e) {}
                return resp;
            }).catch(e => { finish('ERR:' + e.message, '', ''); throw e; });
        } catch (e) {
            return origFetch.apply(this, arguments);
        }
    };
    return 'OK';
})();
"""

READ_CAPTURE = "JSON.stringify(window.__m2a_captured || [])"

# Headers that carry the session - worth showing, worth redaction when printed.
SECRET_HEADERS = ("authorization", "cookie", "x-token", "x-auth", "token")


def _redact(headers):
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in SECRET_HEADERS:
            text = str(v)
            out[k] = text[:18] + "…(%d)" % len(text)
        else:
            out[k] = str(v)[:200]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help="要打开的页面")
    ap.add_argument("--pattern", default="", help="只保留 URL 匹配该正则的请求")
    ap.add_argument("--out", default="capture.json")
    ap.add_argument("--wait", type=int, default=90, help="采集窗口秒数")
    ap.add_argument("--cookie", default="", help="Cookie 字符串（已登录会话）")
    ap.add_argument("--chrome", default="")
    args = ap.parse_args()

    from cdp import CDP, http_json
    from browser import detect_chromium
    import subprocess
    import tempfile
    import urllib.request

    chrome = args.chrome or detect_chromium()
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="m2a_capture_")
    proxy = os.environ.get("CAPTURE_PROXY", "")
    args_cmd = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--remote-debugging-port=%d" % port,
                "--user-data-dir=%s" % profile, "--window-size=1400,1000"]
    if proxy:
        args_cmd.append("--proxy-server=" + proxy)
    proc = subprocess.Popen(args_cmd + [args.site],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        ws = None
        for _ in range(60):
            try:
                pages = [t for t in http_json("http://127.0.0.1:%d/json/list" % port)
                         if t.get("type") == "page"]
                if pages:
                    ws = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ws:
            print("拿不到 CDP 页面")
            return 1
        page = CDP(ws, timeout=120)
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.enable")
        if args.cookie:
            _inject_cookies(page, args.cookie)
        page.send("Page.navigate", {"url": args.site})
        time.sleep(8)
        page.js(CAPTURE_JS)

        print("\n已打开 %s" % args.site)
        print("请在浏览器里做一次真实操作（发一条消息），%d 秒后自动采集…\n" % args.wait)
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(3)
            raw = page.js(READ_CAPTURE) or "[]"
            try:
                records = json.loads(raw)
            except ValueError:
                records = []
            if records:
                break

        raw = page.js(READ_CAPTURE) or "[]"
        try:
            records = json.loads(raw)
        except ValueError:
            records = []
        if args.pattern:
            try:
                rx = re.compile(args.pattern)
                records = [r for r in records if rx.search(r.get("url") or "")]
            except re.error:
                pass
        for r in records:
            r["headers"] = _redact(r.get("headers"))
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"site": args.site, "records": records}, f,
                      ensure_ascii=False, indent=1)
        print("采到 %d 条请求 -> %s" % (len(records), args.out))
        for r in records[:12]:
            print("  %-6s %s" % (r.get("method"), (r.get("url") or "")[:110]))
            if r.get("body"):
                print("         body: %s" % r["body"][:200])
            if r.get("head"):
                print("         resp: %s" % r["head"][:300].replace("\n", "\\n"))
    finally:
        try:
            if page:
                page.close()
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    return 0


def _free_port():
    import socket
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _inject_cookies(page, cookie_string):
    """Set cookies from a "a=1; b=2" string for the target domain."""
    domain = os.environ.get("CAPTURE_DOMAIN", "")
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        for dom in filter(None, [domain, ".%s" % domain, domain]):
            try:
                page.send("Network.setCookie", {
                    "name": name.strip(), "value": value.strip(), "domain": dom,
                    "path": "/", "secure": True})
                break
            except Exception:  # noqa: BLE001
                continue


if __name__ == "__main__":
    sys.exit(main())

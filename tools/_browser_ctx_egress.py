"""Verify browser egress the way muse2api actually does it.

engine.py:499-529 connects to the BROWSER endpoint and creates each account's
browser context with:
    Target.createBrowserContext {
        "proxyServer": relay.url,
        "proxyBypassList": "<-loopback>",
    }
so the account browser does NOT rely on the system proxy at all. This script
reproduces that exact mechanism against the chain relay, and also reports the
default (no-proxy) context for comparison.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdp import CDP, http_json  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9335
RELAY = os.environ.get("TEST_RELAY", "http://127.0.0.1:17891")
IP_URL = "https://ipinfo.io/json"


def _launch():
    profile = os.path.join(os.environ["TEMP"], "m2a_ctx_probe")
    return subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--remote-debugging-port=%d" % PORT,
            "--user-data-dir=%s" % profile,
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _browser_ws():
    for _ in range(60):
        try:
            v = http_json("http://127.0.0.1:%d/json/version" % PORT)
            if v and "webSocketDebuggerUrl" in v:
                return v["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _probe(browser: CDP, ctx_id, label):
    t_res = browser.send(
        "Target.createTarget",
        {"url": "about:blank", "browserContextId": ctx_id},
    )
    target_id = t_res.get("result", {}).get("targetId")
    ws_url = "ws://127.0.0.1:%d/devtools/page/%s" % (PORT, target_id)
    page = CDP(ws_url, timeout=90)
    try:
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Page.navigate", {"url": IP_URL})
        time.sleep(12)
        body = page.js("document.body ? document.body.innerText : 'NO BODY'")
        print("[%s] egress:" % label)
        print((body or "").strip()[:500])
    finally:
        page.close()
    return body


def main():
    proc = _launch()
    try:
        ws = _browser_ws()
        if not ws:
            print("FAIL: no browser websocket endpoint")
            return 1
        print("browser endpoint:", ws)
        browser = CDP(ws, timeout=120)
        try:
            # 1) default context (no proxyServer) -> what the system proxy yields
            try:
                res = browser.send("Target.createBrowserContext")
                ctx = res.get("result", {}).get("browserContextId")
                print("default browserContextId:", ctx)
                if ctx:
                    _probe(browser, ctx, "default-context")
            except Exception as exc:  # noqa: BLE001
                print("[default-context] skipped:", exc)

            # 2) proxied context -> exactly what engine.py does
            print("relay under test:", RELAY)
            res = browser.send(
                "Target.createBrowserContext",
                {"proxyServer": RELAY, "proxyBypassList": "<-loopback>"},
            )
            ctx_id = res.get("result", {}).get("browserContextId")
            print("proxied browserContextId:", ctx_id)
            if not ctx_id:
                print("FAIL: no browser context id")
                return 1
            _probe(browser, ctx_id, "proxyServer=" + RELAY)
        finally:
            browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

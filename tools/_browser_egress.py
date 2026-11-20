"""Browser-side egress check using the project's own cdp.py CDP client.

This is the same code path muse2api uses to drive Chrome, so the egress it
observes is the egress a real session would have.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdp import CDP, http_json  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9334
IP_URL = "http://ip-api.com/json/?fields=query,countryCode,city,as,isp"
# ip-api returns an empty body to proxied callers, so use a page that always
# renders the visitor IP for the browser-side check.
IP_URL_HTML = "https://ipinfo.io/json"


def main():
    profile = os.path.join(os.environ["TEMP"], "m2a_egress_probe2")
    proc = subprocess.Popen(
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
    try:
        target = None
        for _ in range(60):
            try:
                targets = http_json("http://127.0.0.1:%d/json/list" % PORT)
                pages = [t for t in targets if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if not target:
            print("   FAIL: no CDP page target")
            return 1

        print("   target:", target.get("url"))
        cdp = CDP(target["webSocketDebuggerUrl"], timeout=60)
        try:
            cdp.send("Page.enable")
            cdp.send("Network.enable")
            cdp.send("Page.navigate", {"url": IP_URL_HTML})
            time.sleep(10)
            print("   page url:", cdp.js("location.href"))
            print("   page state:", cdp.js("document.readyState"))
            body = cdp.js("document.body ? document.body.innerText : 'NO BODY'")
            print("   browser egress:", (body or "").strip()[:300])
        finally:
            cdp.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

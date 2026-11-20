# -*- coding: utf-8 -*-
"""Find the full invite code and the referral link in the DOM.

The code card renders masked ("XRP9TJ" as displayed) and the referral link
is published to the clipboard by a button rather than exposed as an href.
This greps the rendered HTML and every attribute for the real values.
"""
from __future__ import annotations

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

import core  # noqa: E402
import invite  # noqa: E402

CARD_CONTEXT = r"""(() => {
    const overlay = [...document.querySelectorAll('div')]
        .find(d => (d.innerText || '').includes('在卡片上划一划')) || document.body;
    const code = [...overlay.querySelectorAll('*')].find(el => {
        const t = (el.innerText || '').trim();
        const r = el.getBoundingClientRect();
        return /^[A-Z0-9]{4,16}$/.test(t) && r.width > 20 && el.children.length === 0;
    });
    if (!code) return '';
    let out = [];
    let el = code;
    for (let i = 0; i < 5 && el; i++, el = el.parentElement) {
        out.push({
            depth: i,
            tag: el.tagName.toLowerCase(),
            cls: (el.className || '').toString().slice(0, 100),
            attrs: Object.fromEntries([...el.attributes]
                .map(a => [a.name, a.value.slice(0, 160)])),
            html: el.outerHTML.slice(0, 1200),
        });
    }
    return JSON.stringify(out);
})()"""

HTML_GREP = r"""(() => {
    const html = document.documentElement.outerHTML;
    const hits = {};
    const patterns = {
        inviteLink: /(https?:\/\/[^"'\s<>]*(?:invite|referral|ref=)[^"'\s<>]*)/gi,
        refParam: /([?&](?:ref|invite|invite_code|code)=[A-Za-z0-9._-]+)/gi,
        museLink: /(https?:\/\/(?:www\.)?muse\.ai[^"'\s<>]{0,80})/gi,
        longToken: /(?<![A-Za-z0-9])([A-Z0-9]{8,24})(?![A-Za-z0-9])/g,
    };
    for (const k of Object.keys(patterns)) {
        const m = html.match(patterns[k]) || [];
        hits[k] = [...new Set(m)].slice(0, 12);
    }
    return JSON.stringify(hits);
})()"""

BUTTON_INFO = r"""(() => {
    const overlay = [...document.querySelectorAll('div')]
        .find(d => (d.innerText || '').includes('在卡片上划一划')) || document.body;
    const out = [];
    overlay.querySelectorAll('button,[role=button],a').forEach(el => {
        const r = el.getBoundingClientRect();
        out.push({
            tag: el.tagName.toLowerCase(),
            text: (el.innerText || '').trim().slice(0, 40),
            attrs: Object.fromEntries([...el.attributes]
                .map(a => [a.name, a.value.slice(0, 200)])),
            rect: {x: Math.round(r.left), y: Math.round(r.top),
                   w: Math.round(r.width), h: Math.round(r.height)},
            inViewport: r.top >= 0 && r.left >= 0 &&
                        r.bottom <= (window.innerHeight || 0) &&
                        r.right <= (window.innerWidth || 0),
        });
    });
    return JSON.stringify(out);
})()"""


def _click(page, expr):
    raw = page.js(expr)
    if not raw:
        return False
    pt = json.loads(raw)
    for kind in ("mousePressed", "mouseReleased"):
        page.send("Input.dispatchMouseEvent", {
            "type": kind, "x": pt["x"], "y": pt["y"],
            "button": "left", "clickCount": 1})
    return True


def main():
    from cdp import CDP, http_json
    from proxy import HTTPProxyRelay
    from browser import muse_cdp_port

    accounts = core._account_store()
    account = next(a for a in accounts if a.get("cookies"))
    relay = HTTPProxyRelay(account.get("proxy") or {})
    port = muse_cdp_port()
    info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=8)
    browser = CDP(info["webSocketDebuggerUrl"], timeout=120)
    ctx_id = browser.send("Target.createBrowserContext", {
        "proxyServer": relay.url, "proxyBypassList": "<-loopback>",
    }).get("result", {}).get("browserContextId")
    target = browser.send("Target.createTarget", {
        "url": "about:blank", "browserContextId": ctx_id,
    }).get("result", {}).get("targetId")
    page = CDP("ws://127.0.0.1:%d/devtools/page/%s" % (port, target), timeout=120)
    try:
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.enable")
        now = time.time()
        expires = account.get("cookies_exp") or {}
        for name, value in (account.get("cookies") or {}).items():
            if not value:
                continue
            try:
                exp = float(expires.get(name) or 0)
            except (TypeError, ValueError):
                exp = 0
            exp_val = exp if exp > now + 3600 else now + 7 * 86400
            for dom in (".muse.ai", "muse.ai"):
                try:
                    page.send("Network.setCookie", {
                        "name": name, "value": value, "domain": dom,
                        "path": "/", "secure": True, "expires": exp_val})
                    break
                except Exception:  # noqa: BLE001
                    continue
        page.send("Page.navigate", {"url": "https://muse.ai/"})
        time.sleep(12)
        _click(page, invite.INVITE_BUTTON)
        time.sleep(5)

        print("\n=== 邀请浮层里的按钮 ===")
        raw = page.js(BUTTON_INFO) or "[]"
        try:
            for item in json.loads(raw):
                print(json.dumps(item, ensure_ascii=False))
        except ValueError:
            print(raw[:1200])

        print("\n=== 代码卡片的 DOM 上下文 ===")
        raw = page.js(CARD_CONTEXT) or "[]"
        try:
            for item in json.loads(raw):
                print("\n--- depth %d <%s> ---" % (item["depth"], item["tag"]))
                print("  cls  :", item["cls"])
                print("  attrs:", json.dumps(item["attrs"], ensure_ascii=False)[:600])
                print("  html :", item["html"][:700].replace("\n", " "))
        except ValueError:
            print(raw[:1500])

        print("\n=== 整页 HTML 里的邀请相关链接/令牌 ===")
        raw = page.js(HTML_GREP) or "{}"
        try:
            print(json.dumps(json.loads(raw), ensure_ascii=False, indent=1)[:3000])
        except ValueError:
            print(raw[:1500])

        print("\n=== 点击「复制推荐链接」并观察 ===")
        try:
            browser.send("Browser.grantPermissions", {
                "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
                "origin": "https://muse.ai"})
        except Exception as exc:  # noqa: BLE001
            print("  授权失败:", exc)

        # The code is painted on a canvas; the copy button stays disabled
        # until the card is revealed. Reveal it first (the canvas takes
        # Enter/Space, or a swipe), then the copy button becomes usable.
        print("\n=== 揭示代码（canvas 聚焦 + Enter/Space）===")
        canvas = page.js(r"""(() => {
            const c = document.querySelector('canvas[aria-label="显示邀请码"]')
                || document.querySelector('canvas[role=button]');
            if (!c) return '';
            c.scrollIntoView({block: 'center'});
            c.focus();
            const r = c.getBoundingClientRect();
            return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2,
                                   label: c.getAttribute('aria-label') || ''});
        })()""") or ""
        print("  canvas:", canvas)

        def copy_state():
            return page.js(r"""(() => {
                for (const el of document.querySelectorAll('button,[role=button]')) {
                    const t = (el.innerText || '').trim();
                    if (/复制推荐链接/.test(t)) {
                        return JSON.stringify({disabled: !!el.disabled,
                                              aria: el.getAttribute('aria-disabled')});
                    }
                }
                return '';
            })()""") or ""

        def clip():
            return page.js(
                "(async()=>{try{return await navigator.clipboard.readText()}"
                "catch(e){return 'ERR:'+e}})()") or ""

        print("  揭示前 复制按钮:", copy_state())

        for key, vk, code_name in (("Enter", 13, "Enter"), (" ", 32, "Space")):
            for kind in ("rawKeyDown", "keyUp"):
                params = {"type": kind, "key": key, "code": code_name,
                          "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
                if kind == "rawKeyDown" and key == "Enter":
                    params["text"] = "\r"
                try:
                    page.send("Input.dispatchKeyEvent", params)
                except Exception as exc:  # noqa: BLE001
                    print("   键盘失败:", exc)
            time.sleep(1.5)
            print("  %s 之后 复制按钮: %s" % (key.strip(), copy_state()))
            if json.loads(copy_state() or "{}").get("disabled") is False:
                break

        # A swipe across the card is the other documented gesture.
        if json.loads(copy_state() or "{}").get("disabled") is not False:
            try:
                pt = json.loads(canvas)
                page.send("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": pt["x"] - 80, "y": pt["y"],
                    "button": "left", "clickCount": 1})
                for i in range(16):
                    page.send("Input.dispatchMouseEvent", {
                        "type": "mouseMoved",
                        "x": pt["x"] - 80 + i * 10, "y": pt["y"], "button": "left"})
                    time.sleep(0.05)
                page.send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": pt["x"] + 80, "y": pt["y"],
                    "button": "left", "clickCount": 1})
                time.sleep(1.5)
                print("  划动之后 复制按钮:", copy_state())
            except Exception as exc:  # noqa: BLE001
                print("  划动失败:", exc)

        ok = _click(page, invite.COPY_BUTTON)
        print("  点击复制:", ok)
        time.sleep(2)
        print("  剪贴板:", clip() or "(空)")
        print("  页面提示:", page.js(r"""(() => {
            const out = [];
            document.querySelectorAll('[role=status],[role=alert],[class*="toast"],[class*="Toast"]')
                .forEach(el => { const t=(el.innerText||'').trim(); if(t) out.push(t); });
            return out.join(' | ');
        })()""") or "(无)")
    finally:
        try:
            page.close()
            browser.send("Target.disposeBrowserContext", {"browserContextId": ctx_id})
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

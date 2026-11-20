# -*- coding: utf-8 -*-
"""Probe the Muse settings panel for the invite/referral (邀请码) mechanism.

The usage panel shows two buttons - "升级" (upgrade) and "兑现邀请码"
(redeem invite code). This script drives muse2api's own running browser over
CDP (same session, same account proxy) and dumps the whole settings dialog
plus every invite-related control it can find.

Usage:
    python tools/_invite_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tools"))

import core  # noqa: E402

DIALOG_TEXT = r"""(() => {
    const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
        .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
    return d ? (d.innerText || '') : '';
})()"""

INVITE_CONTROLS = r"""(() => {
    const out = [];
    document.querySelectorAll('button,a,[role=button],[role=link]').forEach(el => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0) return;
        const t = ((el.innerText||'') + '|' + (el.getAttribute('aria-label')||'')).trim();
        if (/邀请|兑现|推荐|invite|referral|redeem/i.test(t)) {
            out.push({tag: el.tagName.toLowerCase(), text: t.slice(0,80),
                      href: el.getAttribute('href')||'',
                      testid: el.getAttribute('data-testid')||''});
        }
    });
    return JSON.stringify(out);
})()"""

SETTINGS_ENTRY = r"""(() => {
    const els = [...document.querySelectorAll('button,[role=button],a')];
    for (const el of els) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const t = ((el.innerText || '') + '|' + (el.getAttribute('aria-label') || '')).trim();
        if (/^(设置|設定|Settings)$/.test(t)) {
            return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2, text: t});
        }
    }
    return '';
})()"""

USAGE_ENTRY = r"""(() => {
    const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
        .find(e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden');
    const scope = d || document;
    for (const el of scope.querySelectorAll('button,[role=button],a')) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const t = ((el.innerText || '') + '|' + (el.getAttribute('aria-label') || '')).trim();
        if (/使用情况|使用情況|用量|Usage/i.test(t)) {
            return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2, text: t});
        }
    }
    return '';
})()"""


def _click(page, expr):
    raw = page.js(expr)
    if not raw:
        return False
    pt = json.loads(raw)
    page.send("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": pt["x"], "y": pt["y"],
        "button": "left", "clickCount": 1})
    page.send("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": pt["x"], "y": pt["y"],
        "button": "left", "clickCount": 1})
    return True


def main():
    accounts = core._account_store()
    if not accounts:
        print("账号池为空")
        return 1
    account = accounts[0]
    aid = account["id"]
    print("账号:", account.get("label"), "| id:", aid)

    sys.path.insert(0, core.MUSE_DIR)
    from cdp import CDP, http_json  # noqa: E402
    from proxy import HTTPProxyRelay  # noqa: E402
    from browser import muse_cdp_port  # noqa: E402

    proxy = account.get("proxy") or {}
    relay = HTTPProxyRelay(proxy) if proxy.get("server") else None
    port = muse_cdp_port()

    info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=8)
    browser = CDP(info["webSocketDebuggerUrl"], timeout=120)
    ctx_id = None
    page = None
    try:
        res = browser.send("Target.createBrowserContext", {
            "proxyServer": relay.url if relay else "",
            "proxyBypassList": "<-loopback>",
        })
        ctx_id = res.get("result", {}).get("browserContextId")
        t = browser.send("Target.createTarget",
                         {"url": "about:blank", "browserContextId": ctx_id})
        target = t.get("result", {}).get("targetId")
        page = CDP("ws://127.0.0.1:%d/devtools/page/%s" % (port, target), timeout=120)
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.enable")

        # Inject the account's session cookies one by one - the browser
        # rejects "__Host-" prefixed cookies that carry a domain attribute.
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
                        "path": "/", "secure": True, "expires": exp_val,
                    })
                    break
                except Exception:  # noqa: BLE001
                    continue

        page.send("Page.navigate", {"url": "https://muse.ai/"})
        time.sleep(12)

        print("\n=== 主页面上的邀请入口 ===")
        raw = page.js(INVITE_CONTROLS) or "[]"
        try:
            found = json.loads(raw)
        except ValueError:
            found = []
        print(json.dumps(found, ensure_ascii=False, indent=1) if found
              else "  （页面上没有可见的邀请码入口）")

        # Open the invite panel itself - that is where the codes come from.
        invite_click = r"""(() => {
            const el = document.querySelector('[data-testid="hatch-invite-friends-button"]')
                || [...document.querySelectorAll('button,[role=button]')].find(e => {
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && /邀请/.test(e.innerText || '');
                });
            if (!el) return '';
            const r = el.getBoundingClientRect();
            return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
        })()"""
        print("\n=== 点击「邀请」按钮 ===")
        print("  clicked:", _click(page, invite_click))
        time.sleep(6)

        panel = page.js(r"""(() => {
            const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
                .find(e => e.getClientRects().length > 0 &&
                           getComputedStyle(e).visibility !== 'hidden');
            if (d) return (d.innerText || '');
            // The invite panel may render inline rather than as a dialog.
            const el = document.querySelector('[data-testid*="invite"], [class*="invite"]');
            return el ? (el.innerText || '') : '';
        })()""") or ""
        print("\n=== 邀请面板内容 ===")
        print(panel.strip()[:3000] or "(未取到面板内容)")

        code = page.js(r"""(() => {
            const out = [];
            document.querySelectorAll('[data-testid*="invite"], [class*="invite"], input, code, pre')
                .forEach(el => {
                    const v = (el.value || el.innerText || '').trim();
                    if (v && v.length > 3 && v.length < 120 &&
                        !/邀请|Invite|好友|Friends/i.test(v)) {
                        out.push({tag: el.tagName.toLowerCase(),
                                  testid: el.getAttribute('data-testid') || '', v: v});
                    }
                });
            return JSON.stringify(out);
        })()""") or "[]"
        try:
            codes = json.loads(code)
        except ValueError:
            codes = []
        print("\n=== 邀请码/奖励相关字段 ===")
        print(json.dumps(codes, ensure_ascii=False, indent=1)[:2500] if codes
              else "  （没有取到邀请码字段）")

        link = page.js(r"""(() => {
            const out = [];
            document.querySelectorAll('a[href], button, [data-clipboard-text], [data-testid]')
                .forEach(el => {
                    const href = el.getAttribute('href') || '';
                    const clip = el.getAttribute('data-clipboard-text') || '';
                    const tid = el.getAttribute('data-testid') || '';
                    const t = (el.innerText || '').trim().slice(0, 40);
                    if (/muse\.ai|invite|referral|copy|clipboard/i.test(href + clip + tid) ||
                        /复制|邀请|copy/i.test(t)) {
                        out.push({tag: el.tagName.toLowerCase(), text: t,
                                  href: href.slice(0, 160), clip: clip.slice(0, 160),
                                  testid: tid});
                    }
                });
            return JSON.stringify(out);
        })()""") or "[]"
        try:
            links = json.loads(link)
        except ValueError:
            links = []
        print("\n=== 推荐链接 / 复制入口 ===")
        print(json.dumps(links, ensure_ascii=False, indent=1)[:2500] if links
              else "  （没有取到推荐链接）")
    finally:
        try:
            if page:
                page.close()
            if ctx_id:
                browser.send("Target.disposeBrowserContext",
                             {"browserContextId": ctx_id})
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        if relay:
            relay.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

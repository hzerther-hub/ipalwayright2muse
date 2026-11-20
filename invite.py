# -*- coding: utf-8 -*-
"""Muse 额度的完整视图：周限额、额外额度、邀请次数与邀请码。

目的：**额度的事不用登 Muse 网页**。面板直接读。

额度有两个互不相干的池子（Muse 设置 → 使用情况面板原文）：

  周限额        每周一自动重置，面上写明「周限额重置时间」
  额外的使用额度  「从不过期」，额度面板里另一个独立的百分比

额外额度来自邀请机制，Muse 的原文是：

  「只要 TA 在加入后 48 小时内通过『设置』兑现邀请码，你们就能分别获得
    10 亿个 Muse 词元。还可使用 30 次。」

也就是说：双向各得 10 亿，每账号 30 次上限，48 小时内必须兑现。

面板要把「还可使用 N 次」、完整邀请码、推荐链接都读出来——邀请码默认打码，
要划一下卡片或按 Enter 才显示；推荐链接走剪贴板，没有 href，所以两者都得
在页面里主动取。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tools"))

import core  # noqa: E402

# 邀请面板是一次真实页面交互，比查一次额度慢，所以缓存一段时间
CACHE_TTL = 600

INVITE_BUTTON = r"""(() => {
    const el = document.querySelector('[data-testid="hatch-invite-friends-button"]')
        || [...document.querySelectorAll('button,[role=button]')].find(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && /邀请/.test(e.innerText || '');
        });
    if (!el) return '';
    const r = el.getBoundingClientRect();
    return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
})()"""

PANEL_TEXT = r"""(() => {
    const d = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')]
        .find(e => e.getClientRects().length > 0 &&
                   getComputedStyle(e).visibility !== 'hidden');
    if (d) return (d.innerText || '');
    const el = document.querySelector('[data-testid*="invite"], [class*="invite"]');
    return el ? (el.innerText || '') : '';
})()"""

COPY_BUTTON = r"""(() => {
    for (const el of document.querySelectorAll('button,[role=button]')) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0) continue;
        if (/复制推荐链接|复制邀请|Copy/i.test(el.innerText || '')) {
            return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
        }
    }
    return '';
})()"""

# Patch the clipboard BEFORE the page's own scripts run. Patching later is
# too late: the copy button captures navigator.clipboard.writeText at module
# load, so a late patch never sees the call. The copied payload is an
# "invite message" (邀请消息) that carries the code.
PATCH_CLIPBOARD = r"""
(() => {
    try {
        window.__m2a_clip = '';
        const orig = navigator.clipboard;
        if (orig && orig.writeText) {
            const w = orig.writeText.bind(orig);
            orig.writeText = (text) => {
                window.__m2a_clip = String(text || '');
                return Promise.resolve();
            };
        }
        if (orig && orig.write) {
            const w = orig.write.bind(orig);
            orig.write = (items) => {
                try {
                    const parts = [];
                    for (const item of items || []) {
                        if (item && item.text) parts.push(item.text);
                    }
                    window.__m2a_clip = parts.join('');
                } catch (e) {}
                return w(items);
            };
        }
    } catch (e) {}
})();
"""

READ_CLIPBOARD = "window.__m2a_clip || ''"

# The code is painted on a canvas; Enter/Space on the focused canvas reveals
# it, and the copy button only becomes usable afterwards.
REVEAL_CODE = r"""(() => {
    const c = document.querySelector('canvas[aria-label="显示邀请码"]')
        || document.querySelector('canvas[role=button]');
    if (!c) return '';
    c.scrollIntoView({block: 'center'});
    c.focus();
    const r = c.getBoundingClientRect();
    return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2});
})()"""

CODE_TEXT = r"""(() => {
    const els = [...document.querySelectorAll('*')].filter(el => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0) return false;
        const t = (el.innerText || '').trim();
        return /^[A-Z0-9]{4,16}$/.test(t) && el.children.length === 0;
    });
    return els.map(e => (e.innerText || '').trim()).join(',');
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


def _account():
    """The first enabled account that has a session - the one to read from."""
    for account in core._account_store():
        if account.get("cookies") and account.get("enabled", True):
            return account
    return None


def _parse_panel(text):
    """Pull the rules out of the invite panel's own wording."""
    out = {}
    remaining = re.search(r"还可使用\s*(\d+)\s*次", text or "")
    if remaining:
        out["remaining_uses"] = int(remaining.group(1))
    each = re.search(r"分别获得\s*([\d.]+)\s*亿", text or "")
    if each:
        out["credits_per_use_billion"] = float(each.group(1))
    window = re.search(r"加入后\s*(\d+)\s*小时内", text or "")
    if window:
        out["redeem_window_hours"] = int(window.group(1))
    if "兑现邀请码" in (text or ""):
        out["mentions_redeem"] = True
    if re.search(r"分别获得", text or ""):
        out["both_sides_rewarded"] = True
    return out


class InviteReader:
    """Reads the invite panel through muse2api's own browser."""

    def __init__(self, ttl=CACHE_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0

    def cached(self):
        with self._lock:
            if not self._cached:
                return None
            return dict(self._cached, age=int(time.time() - self._cached_at),
                        stale=(time.time() - self._cached_at) > self.ttl)

    def read(self, force=False):
        """Read the invite panel. Returns the parsed result plus any error."""
        with self._lock:
            if (not force and self._cached
                    and time.time() - self._cached_at < self.ttl):
                return dict(self._cached, cached=True)
            self._cached = None
        result = self._read_now()
        with self._lock:
            self._cached = result
            self._cached_at = time.time()
        return result

    def _read_now(self):
        account = _account()
        if not account:
            return {"ok": False, "error": "账号池里没有可用账号"}
        aid = account["id"]
        try:
            from cdp import CDP, http_json
            from proxy import HTTPProxyRelay
            from browser import muse_cdp_port
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "缺少 CDP/代理依赖：%s" % exc}

        proxy = account.get("proxy") or {}
        relay = None
        browser = None
        ctx_id = None
        page = None
        try:
            if proxy.get("server"):
                relay = HTTPProxyRelay(proxy)
            port = muse_cdp_port()
            info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=8)
            if not info or not info.get("webSocketDebuggerUrl"):
                return {"ok": False,
                        "error": "muse2api 的浏览器没运行（CDP %d 不通）" % port}
            browser = CDP(info["webSocketDebuggerUrl"], timeout=120)
            res = browser.send("Target.createBrowserContext", {
                "proxyServer": relay.url if relay else "",
                "proxyBypassList": "<-loopback>",
            })
            ctx_id = res.get("result", {}).get("browserContextId")
            target = browser.send("Target.createTarget", {
                "url": "about:blank", "browserContextId": ctx_id,
            }).get("result", {}).get("targetId")
            page = CDP("ws://127.0.0.1:%d/devtools/page/%s" % (port, target),
                       timeout=120)
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Network.enable")

            # Session cookies, one by one: the browser rejects "__Host-"
            # prefixed cookies that carry a domain attribute.
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

            # Patch the clipboard before the page scripts capture it.
            try:
                page.send("Page.addScriptToEvaluateOnNewDocument",
                          {"source": PATCH_CLIPBOARD})
            except Exception:  # noqa: BLE001
                page.js(PATCH_CLIPBOARD)

            page.send("Page.navigate", {"url": "https://muse.ai/"})
            time.sleep(12)

            if not _click(page, INVITE_BUTTON):
                return {"ok": False, "error": "页面上找不到「邀请」入口"}
            time.sleep(5)

            # Reveal the code first: the copy button is only usable once the
            # card has been revealed.
            reveal = page.js(REVEAL_CODE) or ""
            if reveal:
                for key, vk, code_name in (("Enter", 13, "Enter"), (" ", 32, "Space")):
                    for kind in ("rawKeyDown", "keyUp"):
                        params = {"type": kind, "key": key, "code": code_name,
                                  "windowsVirtualKeyCode": vk,
                                  "nativeVirtualKeyCode": vk}
                        if kind == "rawKeyDown" and key == "Enter":
                            params["text"] = "\r"
                        try:
                            page.send("Input.dispatchKeyEvent", params)
                        except Exception:  # noqa: BLE001
                            pass
                    time.sleep(1.2)
                    if page.js(CODE_TEXT):
                        break

            codes_raw = page.js(CODE_TEXT) or ""
            codes = [c for c in codes_raw.split(",") if c.strip()]

            # The copied payload is an invite message carrying the code.
            referral = ""
            if _click(page, COPY_BUTTON):
                time.sleep(2)
                referral = page.js(READ_CLIPBOARD) or ""

            text = page.js(PANEL_TEXT) or ""

            parsed = _parse_panel(text)
            parsed.update({
                "ok": True,
                "label": account.get("label") or aid,
                "account_id": aid,
                "invite_code": codes[0] if codes else "",
                "referral_link": referral,
                "panel_text": text.strip()[:1200],
                "checked_at": int(time.time()),
            })
            if parsed.get("remaining_uses") and parsed.get("credits_per_use_billion"):
                parsed["max_total_billion"] = (
                    parsed["remaining_uses"] * parsed["credits_per_use_billion"])
            return parsed
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                if page:
                    page.close()
                if ctx_id and browser:
                    browser.send("Target.disposeBrowserContext",
                                 {"browserContextId": ctx_id})
                if browser:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
            if relay:
                try:
                    relay.close()
                except Exception:  # noqa: BLE001
                    pass


READER = InviteReader()


def quota_view():
    """Everything about the account's quota, read from the panel."""
    accounts = core._account_store()
    rows = []
    for account in accounts:
        quota = account.get("quota") or {}
        rows.append({
            "id": account.get("id"),
            "label": account.get("label") or account.get("id"),
            "enabled": bool(account.get("enabled", True)),
            "plan": quota.get("plan") or "",
            "weekly_used_pct": quota.get("weekly_used_pct"),
            "weekly_reset": quota.get("weekly_reset") or "",
            "extra_used_pct": quota.get("extra_used_pct"),
            "extra_left": quota.get("extra_left") or "",
            "extra_expires": quota.get("extra_expires") or "",
            "quota_checked_at": quota.get("checked_at"),
        })
    return {"accounts": rows}

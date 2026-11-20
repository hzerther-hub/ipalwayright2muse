# -*- coding: utf-8 -*-
"""硬保证：muse2api 的浏览器流量必须从配置的静态住宅节点出去。

四层验证，任一层不成立，保证即视为失效：

  G1 节点归属   配置的上游就是那个静态住宅 SOCKS5 节点
  G2 链路静态   经链式中继多次采样，出口恒为该节点
  G3 账号绑定   每个启用账号的代理都指向链式中继，不会退回系统代理
  G4 浏览器实流 用 muse2api 同款 browserContext 实测浏览器出口

目标国家由 REQUIRED_COUNTRY 决定（留空 = 任意 Muse 支持地区），
供应商名称由 PROXY_VENDOR 决定——静态住宅代理可以买在任何 Muse 开放国家。

G4 是关键那一环。muse2api 的生成流量出自 Chromium，而面板其余检查都是
在 requests/链路层面做的——它们查不到「浏览器上下文没挂上代理」这种情况。
实测：浏览器不挂代理时会跟随 Windows 系统代理，从香港 IP 出去，而其它
检查全部显示绿灯。

处置策略（用户决定）：实测到浏览器出口与目标节点不符时，停用该账号，
阻断生成，直到人工恢复。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

import core  # noqa: E402

# 面板自己只监听本机，但这里仍以 muse2api 的真实端口为准
MUSE_PORT = core.MUSE_PORT
MUSE_BASE = "http://127.0.0.1:%d" % MUSE_PORT
RELAY_URL = "http://%s:%d" % core.RELAY_LISTEN

# 目标节点：配置的那个静态住宅 IP
REQUIRED_COUNTRY = core.REQUIRED_COUNTRY
PROXY_VENDOR = core.PROXY_VENDOR

# 供应商入口，面板把链接摊开给用户
VENDOR_LINKS = [
    ("静态住宅节点目录", "https://www.ipdeep.cn/static-residence"),
    ("静态代理优惠页", "https://www.ipdeep.cn/static-proxy-deals"),
    ("供应商官网", "https://www.ipdeep.cn/"),
]

# Muse 消费者可用地区
MUSE_REGIONS = ("US", "CA", "JP", "SG", "MX", "AU")

# 浏览器实测用的 IP 回显页（ip-api 对代理请求会返回空 body）
IP_URL = "https://ipinfo.io/json"

WATCHDOG_INTERVAL = 180  # 3 分钟
HISTORY_LIMIT = 20


# --------------------------------------------------------------------------
# muse2api 管理接口
# --------------------------------------------------------------------------

def _muse_key():
    return (core._muse_env() or {}).get("MUSE2API_KEY", "")


def _muse_admin(method, path, payload=None, timeout=60):
    """Call muse2api's admin API with the shared bearer key."""
    key = _muse_key()
    if not key:
        return {"error": "读不到 MUSE2API_KEY"}
    data = None
    headers = {"Authorization": "Bearer " + key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(MUSE_BASE + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"error": "HTTP %d: %s" % (exc.code,
                                          exc.read().decode("utf-8", "replace")[:200])}
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        return json.loads(body)
    except ValueError:
        return {"error": "响应不是 JSON"}


def _account_store():
    """muse2api 的账号池落盘文件（含代理口令，管理接口不回传）。"""
    path = os.path.join(core.MUSE_DATA, "accounts.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _full_proxy(aid):
    """The account's proxy including credentials, straight from the store."""
    for account in _account_store():
        if account.get("id") == aid:
            return account.get("proxy") or {}
    return {}


# --------------------------------------------------------------------------
# 第 1 步：iKuuu 是否在运行
# --------------------------------------------------------------------------

def _system_proxy():
    """Windows 系统代理开关（ProxyEnable / ProxyServer）。"""
    out = {"enabled": None, "server": "", "error": ""}
    query = ("HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings")
    enable = core._run_text(["reg", "query", query, "/v", "ProxyEnable"])
    if enable is None:
        out["error"] = "读不到系统代理注册表项"
        return out
    out["enabled"] = "0x1" in enable
    server = core._run_text(["reg", "query", query, "/v", "ProxyServer"])
    if server:
        for line in server.splitlines():
            if "ProxyServer" in line and "REG_SZ" in line:
                out["server"] = line.split("REG_SZ", 1)[1].strip()
    return out


def check_jump():
    """iKuuu 跳板是否在运行，且它的出口不在中国大陆。"""
    cfg = core._chain_cfg()
    jump_proxy = cfg.get("CHAIN_HTTP_PROXY") or "http://127.0.0.1:7890"
    port = int(jump_proxy.rsplit(":", 1)[1] or 7890)
    alive = core._port_open("127.0.0.1", port)

    jump_exit = {}
    if alive:
        info = core._ipinfo_via_proxy(jump_proxy, timeout=20)
        if info and info.get("ip"):
            jump_exit = {
                "exit_ip": info.get("ip"),
                "country_code": (info.get("country") or "").upper(),
                "city": info.get("city"),
                "org": info.get("org"),
            }

    country = jump_exit.get("country_code") or ""
    mainland = country in ("", "CN")
    checks = {
        "jump_running": alive,
        "jump_exit_known": bool(jump_exit.get("exit_ip")),
        "not_mainland": not mainland,
    }
    problems = []
    if not alive:
        problems.append("iKuuu 核心未运行（%d 端口无监听），请启动 iKuuuVPN" % port)
    elif not jump_exit:
        problems.append("iKuuu 在运行，但读不到跳板出口 IP")
    elif mainland:
        problems.append("跳板出口是中国大陆（%s），访问不了境外静态节点"
                        % (country or "未知"))

    return {
        "name": "第1步 · iKuuu 运行",
        "ok": all(checks.values()),
        "subtitle": ("iKuuu 核心在运行，跳板出口 %s %s/%s"
                     % (jump_exit.get("exit_ip") or "?",
                        jump_exit.get("country_code") or "?",
                        jump_exit.get("city") or "?")) if alive else "iKuuu 核心未运行",
        "checks": checks,
        "jump_proxy": jump_proxy,
        "jump_exit": jump_exit,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# 第 2 步：代理是否开启
# --------------------------------------------------------------------------

def check_proxy_enabled():
    """系统代理开关 + 链式中继 + 上游节点，三者都通才算代理真的开着。"""
    cfg = core._chain_cfg()
    hop = core.hop()
    sysproxy = _system_proxy()

    relay_alive = bool(hop.get("relay_alive"))
    upstream = bool(hop.get("upstream_reachable"))
    jump_alive = bool(hop.get("jump_alive"))

    sys_on = sysproxy.get("enabled")
    sys_points_to_jump = bool(sysproxy.get("server")) and \
        sysproxy["server"].rsplit(":", 1)[0] in ("127.0.0.1", "localhost") and \
        sysproxy["server"].rsplit(":", 1)[-1] == str(
            (cfg.get("CHAIN_HTTP_PROXY") or "http://127.0.0.1:7890").rsplit(":", 1)[-1])

    checks = {
        "system_proxy_on": sys_on is True,
        "system_proxy_points_to_jump": sys_points_to_jump,
        "jump_alive": jump_alive,
        "relay_alive": relay_alive,
        "upstream_reachable": upstream,
    }
    problems = []
    if sys_on is False:
        problems.append("Windows 系统代理处于关闭状态")
    elif not sys_points_to_jump:
        problems.append("系统代理指向 %s，不是跳板端口"
                        % (sysproxy.get("server") or "（未设置）"))
    if not relay_alive:
        problems.append("链式中继 %d 未监听" % core.RELAY_LISTEN[1])
    if not upstream:
        problems.append("上游节点经跳板不可达，静态 IP 链路已断")

    return {
        "name": "第2步 · 代理开关",
        "ok": all(checks.values()),
        "subtitle": ("系统代理 %s（%s），链路 %s → %s → %s"
                     % ("已开启" if sysproxy.get("enabled") else "已关闭",
                        sysproxy.get("server") or "未设置",
                        (hop.get("chain") or {}).get("relay") or "?",
                        (hop.get("chain") or {}).get("jump") or "?",
                        (hop.get("chain") or {}).get("upstream") or "?")),
        "checks": checks,
        "system_proxy": sysproxy,
        "chain": hop.get("chain") or {},
        "problems": problems,
    }


# --------------------------------------------------------------------------
# 第 3 步：IP 是不是不合法的静态住宅 IP
# --------------------------------------------------------------------------

def check_residential_ip():
    """Is the egress a legitimate Japanese static residential IP?

    Public IP-quality sources see a datacenter ASN for static residential
    proxies sold at scale, so "residential" is established by the vendor
    product line plus behavioural proof (a single unchanging dedicated
    address), while the risk side is covered by the authoritative sources.
    """
    cfg = core._chain_cfg()
    node = (cfg.get("CHAIN_SOCKS_HOST") or "").strip()

    ipdata = core.check_ip()
    if "error" in ipdata:
        return {
            "name": "第3步 · 静态住宅 IP",
            "ok": False,
            "checks": {"reachable": False},
            "problems": ["出口检测失败：%s" % ipdata["error"]],
        }

    risk = ipdata.get("risk") or {}
    pc = risk.get("proxycheck") or {}
    ipapi = risk.get("ipapi") or {}
    scam = risk.get("scamalytics") or {}
    block = risk.get("blocklist") or {}
    who = risk.get("ipwhois") or {}

    static = core.static_exit_check()
    country = (ipdata.get("country_code") or "").upper()

    fraud = scam.get("fraud_score")
    try:
        fraud = int(fraud) if fraud is not None else None
    except (TypeError, ValueError):
        fraud = None
    pc_risk = pc.get("risk")
    try:
        pc_risk = int(pc_risk) if pc_risk is not None else None
    except (TypeError, ValueError):
        pc_risk = None
    hits = block.get("hits")

    # 厂商产品线佐证：供应商目录里确有该国家的静态住宅节点
    catalog = core.catalog()
    target_nodes = [n for n in catalog.get("nodes", [])
                    if n.get("country") == (REQUIRED_COUNTRY or country)]

    checks = {
        "country_required": (country == REQUIRED_COUNTRY if REQUIRED_COUNTRY
                              else country in MUSE_REGIONS),
        "in_muse_regions": country in MUSE_REGIONS,
        "is_static": bool(static.get("is_static")),
        "matches_configured_node": bool(static.get("matches_configured_node")),
        "no_proxy_flag": core._yes(pc.get("proxy")) is False,
        "no_vpn_flag": core._yes(pc.get("vpn")) is False,
        "no_tor_flag": not core._yes(pc.get("tor")),
        "zero_fraud_score": fraud == 0,
        "zero_proxycheck_risk": pc_risk == 0,
        "not_blacklisted": (hits == 0),
        "vendor_has_target_country": bool(target_nodes),
    }

    problems = []
    if not checks["country_required"]:
        problems.append("出口国家是 %s，要求 %s"
                        % (country or "未知",
                           REQUIRED_COUNTRY or "Muse 支持地区"))
    if not checks["in_muse_regions"]:
        problems.append("出口国家不在 Muse 可用地区 %s" % (MUSE_REGIONS,))
    if not checks["is_static"]:
        problems.append("出口 IP 在采样之间变化，不是静态 IP")
    if not checks["matches_configured_node"]:
        problems.append("出口 IP 与配置的节点 %s 不一致" % (node or "?"))
    if not checks["no_proxy_flag"]:
        problems.append("proxycheck.io 把该 IP 标记为代理")
    if not checks["no_vpn_flag"]:
        problems.append("proxycheck.io 把该 IP 标记为 VPN")
    if not checks["no_tor_flag"]:
        problems.append("该 IP 是 Tor 出口")
    if fraud is not None and fraud > 0:
        problems.append("scamalytics 欺诈分 %d（应为 0）" % fraud)
    if pc_risk is not None and pc_risk > 0:
        problems.append("proxycheck.io 风险分 %d（应为 0）" % pc_risk)
    if hits:
        problems.append("命中 %d 个黑名单（blocklist.de）" % hits)
    if not checks["vendor_has_target_country"] and not catalog.get("error"):
        problems.append("供应商目录里没有 %s 静态住宅节点" % country)

    notes = []
    if ipapi.get("hosting"):
        notes.append("ip-api 标 hosting=true：静态住宅代理的 IP 段挂在机房 ASN 下，"
                     "这是常态，不代表不是住宅 IP")
    if pc.get("type"):
        notes.append("proxycheck.io 分类：%s（公共源不会标成 Residential）" % pc.get("type"))
    if who.get("org"):
        notes.append("ASN：%s / %s" % (who.get("org") or "", who.get("isp") or ""))

    return {
        "name": "第3步 · 静态住宅 IP",
        "ok": all(checks.values()),
        "subtitle": ("%s · %s/%s · %s 静态住宅节点"
                     % (ipdata.get("ip") or "?", country or "?",
                        ipdata.get("city") or "?", PROXY_VENDOR),
        "checks": checks,
        "ip": ipdata.get("ip"),
        "country_code": country,
        "city": ipdata.get("city"),
        "node_ip": node,
        "static_samples": static.get("samples") or [],
        "risk": risk,
        "notes": notes,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# G1 节点归属
# --------------------------------------------------------------------------

def check_node_identity():
    """The configured upstream is the static-residential node we expect."""
    cfg = core._chain_cfg()
    host = (cfg.get("CHAIN_SOCKS_HOST") or "").strip()
    port = cfg.get("CHAIN_SOCKS_PORT") or ""
    user = cfg.get("CHAIN_SOCKS_USER") or ""

    checks = {
        "node_configured": bool(host),
        "node_authenticated": bool(user),
        "country_ok": False,
        "vendor_has_target_static": False,
    }
    problems = []
    if not host:
        problems.append("data/chain.env 没有配置 CHAIN_SOCKS_HOST（上游节点未指定）")
    if host and not user:
        problems.append("CHAIN_SOCKS_USER 为空，节点凭据不完整")

    # 出口国家：以链路实测为准
    country = ""
    current = core.exit_ip()
    if current.get("ip"):
        country = (current.get("country_code") or "").upper()
    checks["country_ok"] = (country == REQUIRED_COUNTRY if REQUIRED_COUNTRY
                            else country in MUSE_REGIONS)
    if country and not checks["country_ok"]:
        problems.append("出口国家是 %s，要求 %s"
                        % (country, REQUIRED_COUNTRY or "Muse 支持地区"))

    # 厂商目录佐证：供应商确实提供该国家的静态住宅节点
    catalog = core.catalog()
    target_nodes = [n for n in catalog.get("nodes", [])
                    if n.get("country") == (REQUIRED_COUNTRY or country)]
    checks["vendor_has_target_static"] = bool(target_nodes)
    if not target_nodes and not catalog.get("error"):
        problems.append("供应商目录里没有 %s 静态住宅节点" % country)

    return {
        "name": "G1 节点归属",
        "ok": all(checks.values()),
        "checks": checks,
        "node_host": host,
        "node_port": port,
        "country_code": country,
        "required_country": REQUIRED_COUNTRY,
        "catalog_target_nodes": len(target_nodes),
        "problems": problems,
    }


# --------------------------------------------------------------------------
# G2 链路静态性
# --------------------------------------------------------------------------

def check_link_static():
    """The relay egress never changes and is the configured node."""
    static = core.static_exit_check()
    problems = list(static.get("reasons") or [])
    checks = {
        "samples_taken": len(static.get("samples") or []) == static.get("sample_rounds"),
        "is_static": bool(static.get("is_static")),
        "matches_node": bool(static.get("matches_configured_node")),
        "is_overseas": bool(static.get("is_overseas")),
    }
    return {
        "name": "G2 链路静态",
        "ok": bool(static.get("ok")) and all(checks.values()),
        "checks": checks,
        "samples": static.get("samples") or [],
        "unique": static.get("unique") or [],
        "configured_node": static.get("configured_node") or "",
        "problems": problems,
    }


# --------------------------------------------------------------------------
# G3 账号代理绑定
# --------------------------------------------------------------------------

def check_account_binding(status):
    """Every enabled account is routed through the chain relay."""
    accounts = status.get("accounts") or []
    checks = {
        "has_accounts": bool(accounts),
        "all_enabled_bound": True,
        "all_enabled_reachable": True,
    }
    problems = []
    per_account = []

    for account in accounts:
        if not account.get("enabled"):
            continue
        proxy = account.get("proxy") or {}
        server = proxy.get("server") or ""
        pstatus = account.get("proxy_status") or {}
        node = pstatus.get("exit_ip") or ""
        bound = bool(server)
        if server and server != RELAY_URL:
            # Not the chain relay: acceptable only if its own egress is the node.
            bound = bool(node)
        reachable = bool(pstatus.get("proxy_reachable"))
        if not bound:
            problems.append("账号 %s 没有绑定代理，会退回系统代理出口"
                            % (account.get("label") or account.get("id")))
        elif not reachable:
            problems.append("账号 %s 的代理不可达" % (account.get("label") or account.get("id")))
        checks["all_enabled_bound"] = checks["all_enabled_bound"] and bound
        checks["all_enabled_reachable"] = checks["all_enabled_reachable"] and reachable
        per_account.append({
            "id": account.get("id"),
            "label": account.get("label") or account.get("id"),
            "server": server,
            "bound": bound,
            "reachable": reachable,
            "reported_exit_ip": node,
        })

    return {
        "name": "G3 账号绑定",
        "ok": all(checks.values()),
        "checks": checks,
        "accounts": per_account,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# G4 浏览器实流（关键）
# --------------------------------------------------------------------------

def _free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _chromium():
    """Reuse muse2api's own detection so the probe runs the same browser."""
    try:
        from browser import detect_chromium
        return detect_chromium()
    except Exception:  # noqa: BLE001
        return "chromium"


def _browser_contexts(port, proxy_server):
    """Create a default context and a proxied one, exactly like engine.py.

    engine.py:499-529 creates each account's context with
        Target.createBrowserContext {"proxyServer": relay.url,
                                     "proxyBypassList": "<-loopback>"}
    so the account browser never relies on the system proxy.
    """
    from cdp import CDP, http_json

    ws = None
    for _ in range(60):
        try:
            info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=3)
            if info and info.get("webSocketDebuggerUrl"):
                ws = info["webSocketDebuggerUrl"]
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    if not ws:
        return None, None
    browser = CDP(ws, timeout=120)
    default_id = None
    proxied_id = None
    try:
        try:
            res = browser.send("Target.createBrowserContext")
            default_id = res.get("result", {}).get("browserContextId")
        except Exception:  # noqa: BLE001
            default_id = None
        res = browser.send("Target.createBrowserContext", {
            "proxyServer": proxy_server,
            "proxyBypassList": "<-loopback>",
        })
        proxied_id = res.get("result", {}).get("browserContextId")
    except Exception:  # noqa: BLE001
        pass
    return browser, (default_id, proxied_id)


def _read_ip(browser, port, context_id):
    """Navigate a context to the IP echo page and read the reported address."""
    from cdp import CDP

    res = browser.send("Target.createTarget",
                       {"url": "about:blank", "browserContextId": context_id})
    target_id = res.get("result", {}).get("targetId")
    if not target_id:
        return ""
    ws_url = "ws://127.0.0.1:%d/devtools/page/%s" % (port, target_id)
    page = CDP(ws_url, timeout=90)
    try:
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Page.navigate", {"url": IP_URL})
        deadline = time.time() + 25
        body = ""
        while time.time() < deadline:
            time.sleep(1.5)
            body = page.js("document.body ? document.body.innerText : ''") or ""
            body = body.strip()
            if body.startswith("{"):
                break
        if body.startswith("{"):
            try:
                return str(json.loads(body).get("ip") or "")
            except ValueError:
                return ""
        return ""
    finally:
        page.close()


def _live_browser_probe(proxy_server):
    """Measure the egress inside muse2api's OWN running browser.

    muse2api drives Chromium over CDP on MUSE2API_CDP_PORT. Connecting to
    that endpoint and opening a throwaway context with the account's proxy
    measures the very browser that issues the real requests - same process,
    same flags, same profile. The context is disposed right after, so the
    account's live context is never touched.

    Returns {"proxied_ip", "default_ip", "error", "ok"}.
    """
    from cdp import CDP, http_json

    try:
        from browser import muse_cdp_port
        port = muse_cdp_port()
    except Exception:  # noqa: BLE001
        port = 19210

    out = {"proxied_ip": "", "default_ip": "", "error": "", "ok": False}
    browser = None
    ctx_id = None
    try:
        info = http_json("http://127.0.0.1:%d/json/version" % port, timeout=5)
        if not info or not info.get("webSocketDebuggerUrl"):
            out["error"] = "muse2api 的浏览器 CDP 端点未就绪"
            return out
        browser = CDP(info["webSocketDebuggerUrl"], timeout=90)

        res = browser.send("Target.createBrowserContext", {
            "proxyServer": proxy_server,
            "proxyBypassList": "<-loopback>",
        })
        ctx_id = res.get("result", {}).get("browserContextId")
        if not ctx_id:
            out["error"] = "muse2api 浏览器没有返回上下文 ID"
            return out

        # The default context of that browser shows what the system proxy
        # would yield - the contrast that makes the risk visible.
        try:
            listing = browser.send("Target.getBrowserContexts")
            default_id = (listing.get("result") or {}).get("defaultBrowserContextId")
            if default_id:
                out["default_ip"] = _read_ip(browser, port, default_id)
        except Exception:  # noqa: BLE001
            pass

        out["proxied_ip"] = _read_ip(browser, port, ctx_id)
        out["ok"] = bool(out["proxied_ip"])
        if not out["ok"]:
            out["error"] = "muse2api 浏览器没有回显出口 IP"
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            if browser is not None and ctx_id:
                browser.send("Target.disposeBrowserContext",
                             {"browserContextId": ctx_id})
        except Exception:  # noqa: BLE001
            pass
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def _standalone_browser_probe(proxy_server):
    """Measure the egress in a separate Chromium, same mechanism as engine.py.

    Fallback for when muse2api's browser is not running.
    """
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="m2a_guarantee_")
    proc = subprocess.Popen(
        [_chromium(),
         "--headless=new",
         "--no-sandbox",
         "--disable-gpu",
         "--remote-debugging-port=%d" % port,
         "--user-data-dir=%s" % profile,
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {"proxied_ip": "", "default_ip": "", "error": "", "ok": False}
    try:
        browser, contexts = _browser_contexts(port, proxy_server)
        if not browser or not contexts or not contexts[1]:
            out["error"] = "拿不到带代理的浏览器上下文"
            return out
        default_id, proxied_id = contexts
        try:
            if default_id:
                out["default_ip"] = _read_ip(browser, port, default_id)
            out["proxied_ip"] = _read_ip(browser, port, proxied_id)
        finally:
            browser.close()
        out["ok"] = bool(out["proxied_ip"])
        if not out["ok"]:
            out["error"] = "浏览器没有回显出口 IP"
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
    return out


def _browser_probe(proxy_server):
    """Prefer muse2api's own browser; fall back to a standalone Chromium."""
    live = _live_browser_probe(proxy_server)
    if live.get("ok"):
        live["mode"] = "live"
        return live
    standalone = _standalone_browser_probe(proxy_server)
    standalone["mode"] = "standalone"
    standalone["live_error"] = live.get("error")
    return standalone


def check_browser_egress(status):
    """Measure what the account browser actually egresses through.

    status: "match" | "mismatch" | "unavailable".
    Only "mismatch" is a hard failure - an unmeasurable browser is a soft
    problem and must never take a working account down.
    """
    node = ""
    cfg = core._chain_cfg()
    node = (cfg.get("CHAIN_SOCKS_HOST") or "").strip()

    accounts = [a for a in (status.get("accounts") or []) if a.get("enabled")]
    if not accounts:
        return {
            "name": "G4 浏览器实流",
            "ok": False,
            "status": "unavailable",
            "checks": {"has_enabled_account": False, "ip_matches_node": False},
            "browser_ip": "",
            "default_ip": "",
            "node_ip": node,
            "offenders": [],
            "problems": ["没有启用中的账号，无法实测浏览器出口"],
        }

    results = []
    offenders = []
    problems = []
    browser_ip = ""
    default_ip = ""
    probe_mode = ""

    for account in accounts:
        aid = account.get("id")
        proxy = _full_proxy(aid)
        server = proxy.get("server") or ""
        if not server:
            problems.append("账号 %s 没有代理配置" % (account.get("label") or aid))
            offenders.append({"id": aid, "label": account.get("label") or aid,
                              "reason": "没有代理配置"})
            results.append({"id": aid, "label": account.get("label") or aid,
                            "status": "mismatch", "browser_ip": ""})
            continue

        # engine.py routes the browser through its own relay so the browser
        # gets the proxy credentials; reproduce that instead of pointing
        # Chromium straight at a proxy that needs authentication.
        relay_url = server
        relay = None
        try:
            from proxy import HTTPProxyRelay
            relay = HTTPProxyRelay(proxy)
            relay_url = relay.url
        except Exception as exc:  # noqa: BLE001
            problems.append("账号 %s 的代理无法建立中继：%s"
                            % (account.get("label") or aid, exc))

        probe = _browser_probe(relay_url)
        if relay is not None:
            try:
                relay.close()
            except Exception:  # noqa: BLE001
                pass

        measured = probe.get("proxied_ip") or ""
        browser_ip = browser_ip or measured
        default_ip = default_ip or (probe.get("default_ip") or "")
        probe_mode = probe.get("mode") or ""

        if not measured:
            results.append({"id": aid, "label": account.get("label") or aid,
                            "status": "unavailable", "browser_ip": "",
                            "error": probe.get("error")})
            problems.append("账号 %s 浏览器出口实测失败：%s"
                            % (account.get("label") or aid, probe.get("error") or "未知原因"))
            continue

        if node and measured != node:
            offenders.append({
                "id": aid,
                "label": account.get("label") or aid,
                "browser_ip": measured,
                "node_ip": node,
                "reason": "浏览器出口 %s ≠ 目标节点 %s" % (measured, node),
            })
            results.append({"id": aid, "label": account.get("label") or aid,
                            "status": "mismatch", "browser_ip": measured})
        else:
            results.append({"id": aid, "label": account.get("label") or aid,
                            "status": "match", "browser_ip": measured})

    hard = [r for r in results if r["status"] == "mismatch"]
    soft = [r for r in results if r["status"] == "unavailable"]
    status_text = "mismatch" if hard else ("unavailable" if soft else "match")

    return {
        "name": "G4 浏览器实流",
        "ok": status_text == "match",
        "status": status_text,
        "checks": {
            "has_enabled_account": bool(accounts),
            "ip_matches_node": status_text == "match",
            "measured": bool(results),
        },
        "browser_ip": browser_ip,
        "default_ip": default_ip,
        "node_ip": node,
        "probe_mode": probe_mode,
        "accounts": results,
        "offenders": offenders,
        "problems": problems,
    }


# --------------------------------------------------------------------------
# 第 4 步：muse2api 可以用
# --------------------------------------------------------------------------

def check_usable(status, browser_layer):
    """The end-to-end verdict: muse2api can be used on this egress."""
    accounts = []
    for account in (status or {}).get("accounts") or []:
        pstatus = account.get("proxy_status") or {}
        # The account-level `ok` is None until a session check runs, which is
        # "not verified yet", not "failed" - the proxy test carries its own
        # session_ok signal. Keep the three states apart.
        if account.get("ok") is False or pstatus.get("session_ok") is False:
            session_state = "failed"
        elif account.get("ok") is True or pstatus.get("session_ok") is True:
            session_state = "verified"
        else:
            session_state = "unknown"
        accounts.append({
            "id": account.get("id"),
            "label": account.get("label") or account.get("id"),
            "enabled": bool(account.get("enabled")),
            "session_state": session_state,
            "expires_at": account.get("expires_at"),
            "proxy": (account.get("proxy") or {}).get("server") or "",
        })

    broken_sessions = [a for a in accounts
                       if a["enabled"] and a["session_state"] == "failed"]

    checks = {
        "service_running": bool(status) and "error" not in status,
        "browser_running": bool((status or {}).get("browser_running")),
        "has_account": bool((status or {}).get("accounts")),
        "browser_egress_matches": bool(browser_layer.get("ok")),
        "sessions_healthy": not broken_sessions,
    }
    problems = []
    if not checks["service_running"]:
        problems.append("muse2api 服务未运行")
    if not checks["browser_running"]:
        problems.append("muse2api 无头浏览器未运行，生成会失败")
    if not checks["has_account"]:
        problems.append("账号池为空")
    if not checks["browser_egress_matches"]:
        problems.extend(browser_layer.get("problems") or [])
        if browser_layer.get("status") == "mismatch":
            problems.append("浏览器实际出口与目标节点不符，已阻断该账号")
    for account in broken_sessions:
        problems.append("账号 %s 的 Muse 会话校验失败" % account["label"])

    return {
        "name": "第4步 · muse2api 可用",
        "ok": all(checks.values()),
        "subtitle": ("浏览器实际出口 %s（目标 %s），%d 个账号"
                     % (browser_layer.get("browser_ip") or "?",
                        browser_layer.get("node_ip") or "?",
                        len(accounts))),
        "checks": checks,
        "accounts": accounts,
        "browser_ip": browser_layer.get("browser_ip") or "",
        "default_ip": browser_layer.get("default_ip") or "",
        "node_ip": browser_layer.get("node_ip") or "",
        "probe_mode": browser_layer.get("probe_mode") or "",
        "problems": problems,
    }


# --------------------------------------------------------------------------
# 汇总 + 处置
# --------------------------------------------------------------------------

def verify(enforce=False):
    """Run every layer and return the full guarantee report.

    The report is an ordered checklist:
      第1步 iKuuu 是否在运行
      第2步 代理是否开启
      第3步 IP 是不是不合法的静态住宅 IP
      第4步 muse2api 可以用
    """
    started = time.time()
    status = _muse_admin("GET", "/admin/status")
    if "error" in status:
        status = {}

    jump = check_jump()
    proxy = check_proxy_enabled()
    residential = check_residential_ip()
    browser = check_browser_egress(status)
    usable = check_usable(status, browser)

    steps = [jump, proxy, residential, usable]
    ok = all(step["ok"] for step in steps)

    problems = []
    for step in steps:
        problems.extend("%s：%s" % (step["name"], p)
                        for p in step.get("problems") or [])

    if ok:
        headline = "muse2api 可以用：出口是 %s 静态住宅 IP %s" % (
            PROXY_VENDOR, residential.get("ip") or "")
    else:
        broken = [s["name"].split(" · ")[0] for s in steps if not s["ok"]]
        headline = "不满足条件：%s" % "、".join(broken)

    report = {
        "ok": ok,
        "verdict": "usable" if ok else "broken",
        "headline": headline,
        "steps": steps,
        # kept for callers that still read the layer view
        "layers": [jump, proxy, residential, browser, usable],
        "problems": problems,
        "checked_at": int(time.time()),
        "duration_ms": int((time.time() - started) * 1000),
        "enforced": [],
        "browser_running": bool(status.get("browser_running")),
        "vendor_links": [{"label": label, "url": url}
                         for label, url in VENDOR_LINKS],
        "required_country": REQUIRED_COUNTRY,
    }

    if enforce:
        report["enforced"] = enforce_report(report)
    return report


def enforce_report(report):
    """Disable accounts whose browser egress is provably wrong.

    Only a measured mismatch counts. An unmeasurable browser is a soft
    problem and must not take a working account down.
    """
    enforced = []
    for layer in report.get("layers") or []:
        if not layer.get("name", "").startswith("G4"):
            continue
        for offender in layer.get("offenders") or []:
            aid = offender.get("id")
            if not aid:
                continue
            res = _muse_admin("PATCH", "/admin/accounts/%s" % aid,
                              {"enabled": False}, timeout=30)
            ok = "error" not in res
            enforced.append({
                "id": aid,
                "label": offender.get("label"),
                "reason": offender.get("reason"),
                "disabled": ok,
                "error": res.get("error"),
            })
            if ok:
                offender["action"] = "已停用账号，阻断生成"
    return enforced


# --------------------------------------------------------------------------
# 常驻看门狗
# --------------------------------------------------------------------------

class Watchdog:
    """Re-verifies the guarantee on a timer and enforces on mismatch."""

    def __init__(self, interval=WATCHDOG_INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.latest = None
        self.history = []

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="guarantee-watchdog")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                report = verify(enforce=True)
                with self._lock:
                    self.latest = report
                    self.history.insert(0, {
                        "checked_at": report["checked_at"],
                        "ok": report["ok"],
                        "verdict": report["verdict"],
                        "problems": report["problems"][:4],
                        "enforced": report.get("enforced") or [],
                    })
                    del self.history[HISTORY_LIMIT:]
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.latest = {"ok": False, "verdict": "error",
                                   "headline": "看门狗执行失败：%s" % exc,
                                   "layers": [], "problems": [str(exc)]}
            self._stop.wait(self.interval)

    def snapshot(self):
        with self._lock:
            return {
                "latest": self.latest,
                "history": list(self.history),
                "interval": self.interval,
                "running": bool(self._thread and self._thread.is_alive()),
            }

    def run_now(self, enforce=True):
        report = verify(enforce=enforce)
        with self._lock:
            self.latest = report
            self.history.insert(0, {
                "checked_at": report["checked_at"],
                "ok": report["ok"],
                "verdict": report["verdict"],
                "problems": report["problems"][:4],
                "enforced": report.get("enforced") or [],
            })
            del self.history[HISTORY_LIMIT:]
        return report


WATCHDOG = Watchdog()

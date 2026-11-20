# -*- coding: utf-8 -*-
"""Core logic for the local static-IP compliance panel.

Everything here is stdlib-only and safe to import from the HTTP server:

  * exit_ip()      - discover the current egress IP through the chain relay
  * check_ip()     - Muse region eligibility + multi-source risk scoring
  * catalog()      - static-residential node catalogue from the vendor
  * hop()          - iKuuu jump-hop state and three-layer chain health
  * service_*()    - start/stop/restart muse2api and the chain relay

Credentials (the upstream SOCKS user/password) are read from data/chain.env
into memory only and are never returned to the caller.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(REPO, "tools")
LIB = os.path.join(REPO, "lib")
DATA = os.path.join(REPO, "data")
CHAIN_ENV = os.path.join(DATA, "chain.env")

RELAY_LISTEN = ("127.0.0.1", 17891)
MUSE_PORT = 18610
PANEL_PORT = 18611

# Muse consumer regions that the account policy accepts.
MUSE_REGIONS = ("US", "CA", "JP", "SG", "MX", "AU")

# Warn when the Muse session has less than this many seconds left.
SESSION_WARN_SECONDS = 6 * 3600

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/160.0.0.0 Safari/537.36"
)


def _panel_config():
    """Panel settings: env vars win, then panel.env, then defaults.

    Nothing here is specific to one operator: the target country, the
    vendor name and the catalogue to consult are all configuration, because
    a static-residential proxy can be bought in any Muse-supported country
    from any vendor.
    """
    values = {}
    env_file = os.path.join(REPO, "panel.env")
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip().upper()] = val.strip().strip("'\"")
    except OSError:
        pass
    for key in ("MUSE2API_DIR", "REQUIRED_COUNTRY", "PROXY_VENDOR"):
        env_value = os.environ.get(key, "").strip()
        if env_value:
            values[key] = env_value
    return values


_CONFIG = _panel_config()


def _muse_dir():
    """Resolve the muse2api deployment this panel manages.

    Layout guarantee: the panel and muse2api live as SIBLINGS, each directory
    named after its own repository - so the panel sits next to a directory
    called "muse2api". That is the default lookup and it is checked, not
    assumed (see muse_dir_state).

    Override with MUSE2API_DIR (environment variable or panel.env) when the
    two are not siblings.
    """
    value = _CONFIG.get("MUSE2API_DIR", "")
    if not value:
        value = os.path.join(os.path.dirname(REPO), "muse2api")
    return os.path.abspath(value)


# The country the egress must be in. Empty means "any country Muse serves",
# which is the generic default - pin it to a specific country only when the
# deployment calls for one.
REQUIRED_COUNTRY = _CONFIG.get("REQUIRED_COUNTRY", "").strip().upper()

# How the upstream static-residential provider is referred to in messages.
PROXY_VENDOR = _CONFIG.get("PROXY_VENDOR", "").strip() or "静态住宅代理"

MUSE_DIR = _muse_dir()
MUSE_DATA = os.path.join(MUSE_DIR, "data")
MUSE_ENV = os.path.join(MUSE_DIR, ".env")


def muse_dir_state():
    """Whether the resolved muse2api directory really is a muse2api install.

    The sibling layout is a guarantee, so it should be verified rather than
    assumed - a wrong path shows up as an unreadable account pool or a
    missing API key, which is hard to trace back to a layout problem.
    """
    parent = os.path.dirname(MUSE_DIR)
    return {
        "path": MUSE_DIR,
        "exists": os.path.isdir(MUSE_DIR),
        "has_app": os.path.isfile(os.path.join(MUSE_DIR, "app.py")),
        "has_env": os.path.isfile(MUSE_ENV),
        "has_accounts": os.path.isfile(os.path.join(MUSE_DATA, "accounts.json")),
        "sibling_of_panel": os.path.abspath(parent) == os.path.abspath(
            os.path.dirname(REPO)),
        "panel_dir": REPO,
        "parent": parent,
        "ok": (os.path.isfile(os.path.join(MUSE_DIR, "app.py"))
               and os.path.isfile(MUSE_ENV)),
    }

sys.path.insert(0, TOOLS)
sys.path.insert(0, LIB)
sys.path.insert(0, REPO)
import upstreams  # noqa: E402
try:
    import ipdeep_catalog  # noqa: E402
except Exception:  # noqa: BLE001  - catalogue is optional
    ipdeep_catalog = None


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _chain_cfg():
    """Parse data/chain.env. Credentials stay in memory and never leave here."""
    cfg = {}
    for line in _read_text(CHAIN_ENV).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        cfg[key.strip()] = val.strip()
    return cfg


def _jump_proxy():
    """iKuuu jump HTTP proxy used for every outbound probe.

    Python's urllib does not read the Windows system proxy, so a direct
    https lookup resolves DNS locally and fails for domains the local
    resolver cannot answer. Routing through the jump keeps DNS and egress
    identical to the browser's.
    """
    return _chain_cfg().get("CHAIN_HTTP_PROXY") or "http://127.0.0.1:7890"


def _http_get(url, timeout=20, via=None, headers=None):
    if via:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": via, "https": via}))
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    opener.addheaders = [("User-Agent", UA), ("Accept", "*/*")]
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "ERR:%s:%s" % (type(exc).__name__, str(exc)[:140])


def _json_get(url, timeout=20, via=None, headers=None):
    body = _http_get(url, timeout=timeout, via=via, headers=headers)
    if body.startswith("ERR:"):
        return None
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def _muse_post(path, payload=None, timeout=60):
    """POST to muse2api's admin API with the shared bearer key."""
    key = (_muse_env() or {}).get("MUSE2API_KEY", "")
    if not key:
        return {"error": "读不到 MUSE2API_KEY"}
    data = None
    headers = {"Authorization": "Bearer " + key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (MUSE_PORT, path),
                                 data=data, headers=headers, method="POST")
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


# --------------------------------------------------------------------------
# upstream-agnostic API client
# --------------------------------------------------------------------------

def upstream_request(upstream_id, method, path, payload=None, timeout=60):
    """Call any registered upstream's admin API.

    Every upstream speaks the same contract (see upstreams.py), so the panel
    needs one client rather than one per service.
    """
    spec = upstreams.get(upstream_id)
    if not spec:
        return {"error": "未知的上游：%s" % upstream_id}
    key = upstreams.api_key_of(spec)
    if not key:
        return {"error": "读不到 %s 的访问密钥" % (spec.get("key_env") or "API Key")}
    url = upstreams.admin_base(spec) + path
    data = None
    headers = {"Authorization": "Bearer " + key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def upstream_accounts(upstream_id):
    """The upstream's account pool, straight from its own store on disk."""
    spec = upstreams.get(upstream_id)
    if not spec:
        return []
    data_dir = spec.get("data_dir")
    if not data_dir:
        data_dir = os.path.join(spec.get("cwd") or "", "data")
    path = os.path.join(data_dir, "accounts.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def upstream_id():
    """The upstream the panel is currently pointed at."""
    return _UPSTREAM


_UPSTREAM = os.environ.get("PANEL_UPSTREAM", "").strip() or "muse2api"


def set_upstream(upstream_id_value):
    """Point the panel at a different upstream (process-wide)."""
    global _UPSTREAM
    if not upstreams.get(upstream_id_value):
        return False
    _UPSTREAM = upstream_id_value
    os.environ["PANEL_UPSTREAM"] = upstream_id_value
    return True


def _account_store():
    """muse2api 的账号池落盘文件（含 cookie 与代理口令，管理接口不回传）。"""
    path = os.path.join(MUSE_DATA, "accounts.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _muse_get(path, timeout=30):
    """GET from muse2api's admin API with the shared bearer key."""
    key = (_muse_env() or {}).get("MUSE2API_KEY", "")
    if not key:
        return {"error": "读不到 MUSE2API_KEY"}
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (MUSE_PORT, path),
                                 headers={"Authorization": "Bearer " + key})
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


def api_key_info():
    """The service's access key and the URLs clients connect to."""
    upstream = upstream_id()
    data = upstream_request(upstream, "GET", "/apikey")
    if "error" in data:
        return data
    base_url = data.get("base_url") or ""
    if not base_url:
        # The public base is unset -> the admin page derives the host from
        # the browser. The panel cannot, so give the local address.
        spec = upstreams.get(upstream) or {}
        base_url = (spec.get("base") or "").rstrip("/") + "/v1"
    models_url = data.get("models_url") or (base_url + "/models")
    media_url = data.get("media_url") or "/v1/media/{name}"
    return {
        "upstream": upstream,
        "api_key": data.get("api_key") or "",
        "base_url": base_url,
        "models_url": models_url,
        "media_url": media_url,
    }


def api_key_rotate():
    """Generate a fresh access key. The old one stops working immediately."""
    upstream = upstream_id()
    data = upstream_request(upstream, "POST", "/apikey/rotate", {}, timeout=30)
    if "error" in data:
        return data
    return {
        "ok": bool(data.get("ok")),
        "upstream": upstream,
        "api_key": data.get("api_key") or "",
        "previous": data.get("previous") or "",
        "message": data.get("message") or "",
    }


def _muse_env():
    """Parse muse2api's own .env (MUSE2API_KEY lives there)."""
    cfg = {}
    for line in _read_text(MUSE_ENV).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        cfg[key.strip()] = val.strip()
    return cfg


# --------------------------------------------------------------------------
# exit IP discovery
# --------------------------------------------------------------------------

def _ipinfo_via_proxy(proxy_url, timeout=25):
    """Read the egress IP through an HTTP CONNECT proxy.

    Reads exactly Content-Length bytes: waiting for EOF would hang on
    keep-alive and report a false negative.
    """
    host_port = proxy_url.split("://", 1)[1].rstrip("/")
    host, _, port = host_port.partition(":")
    port = int(port or 80)
    target = "ipinfo.io"
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(
            ("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (target, target)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 200 " not in head:
            return {"error": "proxy refused CONNECT: %s" % head.strip()}
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=target)
        tls.settimeout(timeout)
        tls.sendall(
            ("GET /json HTTP/1.1\r\nHost: %s\r\nUser-Agent: panel\r\n"
             "Accept: application/json\r\nConnection: close\r\n\r\n" % target).encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
        header, _, rest = data.partition(b"\r\n\r\n")
        clen = None
        for line in header.decode("latin1", "replace").split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                clen = int(line.split(":", 1)[1].strip())
        body = rest
        if clen is not None:
            while len(body) < clen:
                chunk = tls.recv(65536)
                if not chunk:
                    break
                body += chunk
            body = body[:clen]
        return json.loads(body.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def exit_ip(proxy_url=None):
    """Current egress IP and geo, through the chain relay by default."""
    proxy = proxy_url or ("http://%s:%d" % RELAY_LISTEN)
    info = _ipinfo_via_proxy(proxy)
    if info and "error" not in info and info.get("ip"):
        return {
            "ip": info.get("ip"),
            "country_code": info.get("country"),
            "country": info.get("country"),
            "city": info.get("city"),
            "region": info.get("region"),
            "org": info.get("org"),
            "timezone": info.get("timezone"),
            "source": "ipinfo.io",
        }
    # Fallback: ip-api over the same proxy.
    data = _json_get(
        "http://ip-api.com/json/?fields=query,country,countryCode,city,regionName,org,as,isp,proxy,hosting",
        timeout=25, via=proxy)
    if data and data.get("query"):
        return {
            "ip": data.get("query"),
            "country_code": data.get("countryCode"),
            "country": data.get("country"),
            "city": data.get("city"),
            "region": data.get("regionName"),
            "org": data.get("org"),
            "timezone": None,
            "source": "ip-api.com",
        }
    return {"error": info.get("error") if isinstance(info, dict) else "egress probe failed"}


# --------------------------------------------------------------------------
# risk scoring
# --------------------------------------------------------------------------

def _yes(value):
    """Interpret proxycheck.io style string flags ("yes"/"no").

    Python truthiness would read the string "no" as True, so every flag coming
    from proxycheck.io has to be normalised explicitly.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "true", "1")
    return bool(value)


def _probe_proxycheck(ip):
    # The api. subdomain was decommissioned and no longer has a DNS record;
    # the API lives on the apex domain now.
    data = _json_get(
        "https://proxycheck.io/v2/%s?vpn=1&asn=1&risk=1" % ip,
        timeout=25, via=_jump_proxy())
    if not data or data.get("status") != "ok":
        return None
    # The current API returns the record under the IP as a top-level key; the
    # retired api. host used to wrap it in a "data" object. Accept both.
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    node = container.get(ip) or {}
    if not node:
        return None
    node_type = str(node.get("type") or "").strip()
    # The keyless tier omits "vpn" for clean IPs and only returns "type",
    # so the type is the field that actually carries the VPN verdict.
    vpn_flag = _yes(node.get("vpn")) or node_type.lower() == "vpn"
    fraud = node.get("fraud_score")
    try:
        fraud = int(fraud) if fraud is not None else None
    except (TypeError, ValueError):
        fraud = None
    return {
        "proxy": node.get("proxy"),
        "vpn": "yes" if vpn_flag else "no",
        "tor": node.get("tor"),
        "type": node_type,
        "risk": node.get("risk"),
        "fraud_score": fraud,
        "asn": node.get("asn"),
        "org": node.get("organisation") or node.get("org"),
    }


def _probe_ipapi(ip):
    data = _json_get(
        "http://ip-api.com/json/%s?fields=status,message,country,countryCode,city,regionName,"
        "org,as,isp,proxy,hosting,mobile" % ip, timeout=25, via=_jump_proxy())
    if not data or data.get("status") != "success":
        return None
    return {
        "country_code": data.get("countryCode"),
        "country": data.get("country"),
        "city": data.get("city"),
        "region": data.get("regionName"),
        "org": data.get("org"),
        "asn": data.get("as"),
        "isp": data.get("isp"),
        "proxy": data.get("proxy"),
        "hosting": data.get("hosting"),
        "mobile": data.get("mobile"),
    }


def _probe_ipwhois(ip):
    """ipwho.is - country / city / ASN / ISP / timezone.

    Its free tier carries no `security` block, so it complements rather than
    replaces proxycheck.io; it is the more accurate geo source though.
    """
    data = _json_get("https://ipwho.is/%s" % ip, timeout=25, via=_jump_proxy())
    if not data or not data.get("success"):
        return None
    conn = data.get("connection") or {}
    tz = data.get("timezone") or {}
    return {
        "country_code": data.get("country_code"),
        "country": data.get("country"),
        "city": data.get("city"),
        "region": data.get("region"),
        "asn": conn.get("asn"),
        "org": conn.get("org"),
        "isp": conn.get("isp"),
        "domain": conn.get("domain"),
        "timezone": tz.get("id"),
    }


def _probe_scamalytics(ip):
    body = _http_get("https://scamalytics.com/ip/%s" % ip, timeout=25, via=_jump_proxy())
    if not body or body.startswith("ERR:"):
        return None
    m = re.search(r"Fraud Score:\s*(\d+)", body)
    if not m:
        return None
    return {"fraud_score": int(m.group(1))}


def _probe_blocklist(ip):
    body = _http_get("https://blocklist.de/api.php?ip=%s" % ip, timeout=25, via=_jump_proxy())
    if not body or body.startswith("ERR:"):
        return None
    m = re.search(r"([\d,]+)\s+entries found", body)
    if not m:
        return {"hits": 0}
    return {"hits": int(m.group(1).replace(",", ""))}


def check_ip(ip=None):
    """Muse eligibility + risk verdict for one IP (defaults to current exit)."""
    current = exit_ip() if not ip else None
    if not ip:
        if "error" in current:
            return {"error": current["error"]}
        ip = current["ip"]

    # The five risk sources are independent and each may take up to its
    # timeout; run them concurrently so one slow source cannot dominate.
    probes = {
        "ipapi": _probe_ipapi,
        "ipwhois": _probe_ipwhois,
        "proxycheck": _probe_proxycheck,
        "scamalytics": _probe_scamalytics,
        "blocklist": _probe_blocklist,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=len(probes)) as pool:
        futures = {name: pool.submit(fn, ip) for name, fn in probes.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result(timeout=40)
            except Exception:  # noqa: BLE001
                results[name] = None
    ipapi = results.get("ipapi")
    ipwhois = results.get("ipwhois")
    proxycheck = results.get("proxycheck")
    scamalytics = results.get("scamalytics")
    blocklist = results.get("blocklist")

    country_code = ""
    if ipwhois:
        country_code = (ipwhois.get("country_code") or "").upper()
    if not country_code and ipapi:
        country_code = (ipapi.get("country_code") or "").upper()
    # Cross-check: two independent geo sources disagreeing is worth surfacing.
    geo_conflict = bool(
        ipapi and ipwhois
        and (ipapi.get("country_code") or "").upper() not in ("", country_code)
        and (ipapi.get("country_code") or "").upper() != country_code)

    hosting = bool(ipapi.get("hosting")) if ipapi else False
    is_proxy = bool(ipapi.get("proxy")) if ipapi else False
    vpn = _yes(proxycheck.get("vpn")) if proxycheck else False
    fraud = (scamalytics or {}).get("fraud_score") or (proxycheck or {}).get("fraud_score") or 0
    hits = (blocklist or {}).get("hits", 0)

    # Hard risks decide the verdict. "hosting" is only advisory: every static
    # residential proxy segment carries a datacenter flag in the IP databases,
    # so vetoing on it would flag a healthy residential exit as unsafe.
    hard = []
    soft = []
    penalty = 0
    if hosting:
        penalty += 15
        soft.append("IP 段被标记为托管段（静态住宅代理常见；Muse 主要看地区与账号行为）")
    if geo_conflict:
        soft.append("两个地理数据源的国家码不一致")
    if is_proxy:
        penalty += 30
        hard.append("被标记为代理")
    if vpn:
        penalty += 30
        hard.append("被标记为 VPN")
    if fraud:
        penalty += min(40, int(fraud))
        hard.append("欺诈分 %s" % fraud)
    if hits:
        penalty += min(30, int(hits) * 10)
        hard.append("拉黑记录 %s 条" % hits)

    score = max(0, 100 - penalty)
    eligible = country_code in MUSE_REGIONS

    if not eligible:
        verdict = "not_eligible"
        headline = "%s 不在 Muse 可用地区（%s）" % (
            country_code or "未知", "/".join(MUSE_REGIONS))
    elif hard:
        verdict = "risky"
        headline = "%s 地区合规，但存在风险：%s" % (country_code, "、".join(hard))
    else:
        verdict = "compliant"
        headline = "%s 地区合规，代理 / VPN / 欺诈分 / 拉黑项均为 0" % country_code

    return {
        "ip": ip,
        "country_code": country_code,
        "country": (ipwhois or {}).get("country") or (ipapi or {}).get("country"),
        "city": (ipwhois or {}).get("city") or (ipapi or {}).get("city"),
        "region": (ipwhois or {}).get("region") or (ipapi or {}).get("region"),
        "org": (ipwhois or {}).get("org") or (ipapi or {}).get("org") or (proxycheck or {}).get("org"),
        "asn": (ipwhois or {}).get("asn") or (ipapi or {}).get("asn") or (proxycheck or {}).get("asn"),
        "isp": (ipwhois or {}).get("isp") or (ipapi or {}).get("isp"),
        "timezone": (ipwhois or {}).get("timezone") or (current or {}).get("timezone"),
        "hosting": hosting,
        "proxy": is_proxy,
        "muse_eligible": eligible,
        "muse_regions": list(MUSE_REGIONS),
        "risk": {
            "ipwhois": ipwhois,
            "proxycheck": proxycheck,
            "ipapi": ipapi,
            "scamalytics": scamalytics,
            "blocklist": blocklist,
        },
        "score": score,
        "verdict": verdict,
        "headline": headline,
        "reasons": hard,
        "notes": soft,
    }


# --------------------------------------------------------------------------
# static-overseas exit + muse2api safety
# --------------------------------------------------------------------------

def static_exit_check(rounds=3, gap=1.2):
    """Verify the egress really is a static overseas (Japan) IP.

    "Static" cannot be read off a single lookup - it is proven by sampling the
    egress several times: a rotating or session-bound exit changes between
    samples, a static one never does.
    """
    cfg = _chain_cfg()
    configured = cfg.get("CHAIN_SOCKS_HOST", "")
    relay = "http://%s:%d" % RELAY_LISTEN

    samples = []
    for index in range(rounds):
        info = _ipinfo_via_proxy(relay, timeout=25)
        ip = (info or {}).get("ip") or ""
        if ip:
            samples.append(ip)
        if index + 1 < rounds:
            time.sleep(gap)

    current = exit_ip()
    country = (current.get("country_code") or "").upper()
    unique = sorted(set(samples))
    is_static = len(samples) == rounds and len(unique) == 1
    matches_configured = bool(configured) and bool(unique) and unique[0] == configured
    is_overseas = country not in ("", "CN")
    # No pinned country means "any region Muse serves"; a pinned country
    # narrows it further for deployments that need one specific region.
    country_ok = (country == REQUIRED_COUNTRY if REQUIRED_COUNTRY
                  else country in MUSE_REGIONS)

    reasons = []
    if not samples:
        reasons.append("读不到出口 IP（链式中继不可达）")
    elif not is_static:
        reasons.append("出口 IP 在 %d 次采样中变化（%s），不是静态 IP"
                       % (rounds, " / ".join(unique)))
    if not is_overseas:
        reasons.append("出口不在海外（国家码 %s）" % (country or "未知"))
    if not country_ok:
        reasons.append("出口国家是 %s，要求 %s"
                       % (country or "未知",
                          REQUIRED_COUNTRY or "Muse 支持地区 %s" % ("/".join(MUSE_REGIONS),)))

    return {
        "required_country": REQUIRED_COUNTRY,
        "sample_rounds": rounds,
        "samples": samples,
        "unique": unique,
        "is_static": is_static,
        "is_overseas": is_overseas,
        "country_code": country,
        "country_ok": country_ok,
        "configured_node": configured,
        "matches_configured_node": matches_configured,
        "ok": bool(samples) and is_static and is_overseas and country_ok,
        "reasons": reasons,
    }


def muse2api_safety(exit_data=None, static_data=None):
    """Judge whether the current upstream is safe to run on this egress."""
    upstream = upstream_id()
    spec = upstreams.get(upstream) or {}
    port = _port_of(spec)
    label = spec.get("label") or upstream
    result = {
        "verdict": "unknown",
        "headline": "",
        "service_running": False,
        "browser_running": False,
        "upstream": upstream,
        "exit_ip": (exit_data or {}).get("ip") or "",
        "accounts": [],
        "issues": [],
        "notes": [],
        "confirmations": [],
    }

    if not port or not _port_open("127.0.0.1", port):
        result["verdict"] = "bad"
        result["headline"] = "%s 服务未运行（%s 端口无监听）" % (label, port)
        result["issues"].append("服务未启动，请在「服务控制」页启动")
        return result
    result["service_running"] = True

    if not upstreams.api_key_of(spec):
        result["verdict"] = "bad"
        result["headline"] = "读不到 %s 的访问密钥，无法校验" % label
        result["issues"].append("%s 的密钥未配置" % label)
        return result

    data = upstream_request(upstream, "GET", "/status", timeout=40)
    if not data or "error" in data:
        result["verdict"] = "bad"
        result["headline"] = "无法读取 %s 的管理接口" % label
        result["issues"].append("管理接口无响应")
        return result

    result["browser_running"] = bool(data.get("browser_running"))
    exit_now = result["exit_ip"]
    if not exit_now:
        # Standalone call (GET /api/muse2api) arrives without exit_data.
        # Measure the egress ourselves, otherwise the "exit matches" check
        # below would pass vacuously and the UI would claim a verification
        # that never happened.
        probe = exit_ip()
        exit_now = probe.get("ip") or ""
        result["exit_ip"] = exit_now
    now = time.time()

    for account in data.get("accounts") or []:
        pstatus = account.get("proxy_status") or {}
        quota = account.get("quota") or {}
        expires = float(account.get("expires_at") or 0)
        session_left = expires - now if expires else 0
        issues = []
        notes = []
        checks = {
            "enabled": bool(account.get("enabled")),
            "session_ok": bool(account.get("ok")),
            "essential_cookies": bool(account.get("essential_ok")),
            "proxy_configured": bool(pstatus.get("proxy_configured")),
            "proxy_reachable": bool(pstatus.get("proxy_reachable")),
            "exit_ip_match": (not exit_now) or (pstatus.get("exit_ip") == exit_now),
            "session_valid": expires > now,
        }
        if not checks["enabled"]:
            issues.append("账号已停用")
        if not checks["session_ok"]:
            issues.append("Muse 会话校验未通过")
        if not checks["essential_cookies"]:
            issues.append("关键 Cookie 不完整")
        if not checks["proxy_configured"]:
            issues.append("未配置代理")
        elif not checks["proxy_reachable"]:
            issues.append("代理不可达")
        if exit_now and not checks["exit_ip_match"]:
            issues.append("账号出口 %s 与当前出口 %s 不一致"
                          % (pstatus.get("exit_ip") or "?", exit_now))
        if expires and session_left < SESSION_WARN_SECONDS:
            issues.append("会话将在 %.1f 小时后过期" % (session_left / 3600))
        used = quota.get("weekly_used_pct")
        extra_used = quota.get("extra_used_pct")
        extra_left = quota.get("extra_left") or ""
        extra_never = quota.get("extra_expires") == "never"
        # The weekly allowance and the additional credits are two separate
        # pools. Running the weekly one out does not mean the account is
        # spent - the additional credits never expire, so they are what
        # actually decides whether generation still works.
        extra_available = bool(extra_left) and (
            not isinstance(extra_used, int) or extra_used < 100)
        if isinstance(used, int) and used >= 100:
            if extra_available:
                notes.append("周限额已用满（%d%%），但额外额度仍有余量（%s）"
                             % (used, extra_left))
            else:
                issues.append("周限额已用满（%d%%），且额外额度也已耗尽" % used)
        elif isinstance(used, int) and used >= 80:
            notes.append("周限额已用 %d%%" % used)

        result["accounts"].append({
            "id": account.get("id"),
            "label": account.get("label") or account.get("id"),
            "checks": checks,
            "session_left_seconds": int(session_left) if expires else 0,
            "weekly_used_pct": used,
            "weekly_reset": quota.get("weekly_reset") or "",
            "quota_plan": quota.get("plan") or "",
            "extra_used_pct": extra_used,
            "extra_left": extra_left,
            "extra_expires": "never" if extra_never else "",
            "account_exit_ip": pstatus.get("exit_ip") or "",
            "issues": issues,
            "notes": notes,
            "ok": not issues,
        })

    hard = []
    soft = []
    for account in result["accounts"]:
        hard.extend(account["issues"])
        soft.extend(account["notes"])

    if static_data:
        if static_data.get("ok"):
            result["confirmations"].append(
                "出口为静态 %s IP（%s）" % (static_data.get("required_country"),
                                          " / ".join(static_data.get("unique") or [])))
        else:
            hard.extend(static_data.get("reasons") or [])
    if exit_data:
        if exit_data.get("verdict") == "not_eligible":
            hard.append("出口地区不在 Muse 白名单")
        elif exit_data.get("verdict") == "risky":
            hard.extend(exit_data.get("reasons") or [])
    if not result["browser_running"]:
        soft.append("无头浏览器未运行，生成任务会失败")
    if not result["accounts"]:
        soft.append("muse2api 账号池为空")
    for account in result["accounts"]:
        if account["ok"]:
            result["confirmations"].append(
                "%s 会话、Cookie、代理出口均正常（剩余 %.1f 小时）"
                % (account["label"], account["session_left_seconds"] / 3600))

    result["issues"] = hard
    result["notes"] = soft
    if hard:
        result["verdict"] = "bad"
        result["headline"] = "%s 当前不安全：%s" % (label, "、".join(hard[:3]))
    elif soft:
        result["verdict"] = "warn"
        result["headline"] = "%s 可用，但有 %d 项提示" % (label, len(soft))
    else:
        result["verdict"] = "safe"
        result["headline"] = "%s 安全：出口、会话、代理、额度全部正常" % label
    return result


def exit_overview():
    """Current egress: compliance verdict + static check + muse2api safety."""
    ipdata = check_ip()
    if "error" in ipdata:
        return {"error": ipdata["error"]}
    static_data = static_exit_check()
    ipdata["static_exit"] = static_data
    ipdata["muse2api"] = muse2api_safety(exit_data=ipdata, static_data=static_data)
    return ipdata


# --------------------------------------------------------------------------
# vendor catalogue
# --------------------------------------------------------------------------

def catalog():
    """Static-residential nodes from the vendor, annotated with Muse eligibility."""
    if ipdeep_catalog is None:
        return {"error": "ipdeep_catalog module unavailable"}
    try:
        rows = ipdeep_catalog.collect()
    except Exception as exc:  # noqa: BLE001
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    if not rows:
        return {"error": "catalogue fetch returned no nodes"}

    for row in rows:
        row["muse_ok"] = row.get("country") in MUSE_REGIONS

    eligible = sorted(
        [r for r in rows if r["muse_ok"]],
        key=lambda r: (r["price"], r["country"], r["city"]))
    ineligible = sorted(
        [r for r in rows if not r["muse_ok"]],
        key=lambda r: (r["price"], r["country"], r["city"]))

    current = exit_ip()
    current_ip = current.get("ip")

    return {
        "fetched_at": int(time.time()),
        "total": len(rows),
        "eligible_count": len(eligible),
        "nodes": eligible + ineligible,
        "current": {
            "ip": current_ip,
            "country_code": current.get("country_code"),
            "city": current.get("city"),
        } if current_ip else None,
    }


# --------------------------------------------------------------------------
# jump hop + chain health
# --------------------------------------------------------------------------

def _port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def hop():
    """iKuuu jump state plus the three-layer chain health."""
    cfg = _chain_cfg()
    jump_proxy = cfg.get("CHAIN_HTTP_PROXY") or "http://127.0.0.1:7890"

    jump_exit = {}
    try:
        info = _ipinfo_via_proxy(jump_proxy, timeout=20)
        if info and info.get("ip"):
            jump_exit = {
                "exit_ip": info.get("ip"),
                "country_code": info.get("country"),
                "city": info.get("city"),
                "org": info.get("org"),
            }
    except Exception:  # noqa: BLE001
        jump_exit = {}

    relay_alive = _port_open(*RELAY_LISTEN)
    jump_alive = _port_open("127.0.0.1", int(jump_proxy.rsplit(":", 1)[1] or 7890))

    upstream_reachable = False
    if jump_alive:
        upstream_reachable = _upstream_reachable(
            cfg.get("CHAIN_SOCKS_HOST", ""), int(cfg.get("CHAIN_SOCKS_PORT", "1080") or 1080),
            cfg.get("CHAIN_SOCKS_USER", ""), cfg.get("CHAIN_SOCKS_PASS", ""), jump_proxy)

    country = (jump_exit.get("country_code") or "").upper()
    mainland = country in ("CN", "")
    recommendation = ""
    if not jump_exit:
        recommendation = "无法读取跳板出口 IP：跳板可能未启动或 7890 端口不通。"
    elif mainland:
        recommendation = ("跳板出口为中国大陆（%s），无法访问境外静态 IP 节点，"
                          "请在 iKuuuGUI 中切换到非中国大陆节点。" % (country or "未知"))
    elif not relay_alive:
        recommendation = "跳板出口 %s 可用，但链式中继 %d 未监听，请先启动链式中继。" % (
            country, RELAY_LISTEN[1])
    elif not upstream_reachable:
        recommendation = ("跳板出口 %s 正常，但上游节点经跳板不可达，"
                          "静态 IP 链路已断，请检查上游节点或重启链式中继。" % country)
    else:
        recommendation = ("跳板出口 %s/%s 非中国大陆，链路三层通畅，无需切换。"
                          % (country, jump_exit.get("city") or "-"))

    return {
        "jump_alive": jump_alive,
        "relay_alive": relay_alive,
        "upstream_reachable": upstream_reachable,
        "jump_exit": jump_exit,
        "mainland": mainland,
        "chain": {
            "relay": "%s:%d" % RELAY_LISTEN,
            "jump": jump_proxy,
            "upstream": "%s:%s" % (cfg.get("CHAIN_SOCKS_HOST", "?"),
                                   cfg.get("CHAIN_SOCKS_PORT", "?")),
        },
        "recommendation": recommendation,
    }


def _upstream_reachable(host, port, user, password, jump_proxy, timeout=20):
    """Can we reach the upstream SOCKS5 through the jump proxy?"""
    if not host:
        return False
    try:
        jhost, _, jport = jump_proxy.split("://", 1)[1].rstrip("/").partition(":")
        sock = socket.create_connection((jhost, int(jport or 80)), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                      % (host, port, host, port)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 200 " not in head:
            return False
        sock.sendall(b"\x05\x02\x00\x02")
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 0x05:
            return False
        if resp[1] == 0x02:
            sock.sendall(b"\x01" + bytes([len(user)]) + user.encode()
                         + bytes([len(password)]) + password.encode())
            auth = sock.recv(2)
            if len(auth) < 2 or auth[1] != 0x00:
                return False
        elif resp[1] != 0x00:
            return False
        req = (b"\x05\x01\x00\x03" + bytes([len(b"ipinfo.io")]) + b"ipinfo.io"
               + (443).to_bytes(2, "big"))
        sock.sendall(req)
        reply = sock.recv(4)
        return len(reply) >= 4 and reply[1] == 0x00
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# service control
# --------------------------------------------------------------------------

def _run_text(cmd, timeout=20):
    """Run a command and return its stdout as text, never raising on decode.

    netstat and PowerShell emit the console codepage (GBK on zh-CN Windows),
    not UTF-8. Decoding with the default codec raises inside the reader
    thread, which makes subprocess.run return stdout=None and turns a
    cosmetic encoding problem into a 500 on /api/status.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if proc.stdout is None:
        return None
    return proc.stdout.decode("utf-8", "replace")


def _listening_pid(port):
    """PID owning a listening TCP port, via netstat (no third-party deps)."""
    out = _run_text(["netstat", "-ano"])
    if not out:
        return None
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        local = parts[1]
        try:
            if int(local.rsplit(":", 1)[1]) != int(port):
                continue
        except (ValueError, IndexError):
            continue
        try:
            return int(parts[-1])
        except ValueError:
            return None
    return None


def _process_start_time(pid):
    out = _run_text(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process -Id %d).StartTime.ToString('s')" % pid])
    return out.strip() if out else None


def service_status(target):
    """Status of a managed service. "chain" is the local relay; anything
    else is a registered upstream."""
    if target == "chain":
        pid = _listening_pid(RELAY_LISTEN[1])
        return {"target": target, "port": RELAY_LISTEN[1], "running": bool(pid),
                "pid": pid, "started_at": _process_start_time(pid) if pid else None}
    spec = upstreams.get(target)
    if not spec:
        return {"error": "unknown target"}
    port = _port_of(spec)
    pid = _listening_pid(port)
    return {"target": target, "label": spec.get("label") or target, "port": port,
            "running": bool(pid), "pid": pid,
            "started_at": _process_start_time(pid) if pid else None}


def _port_of(spec):
    base = spec.get("base") or ""
    try:
        return int(base.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0


def service_start(target):
    status = service_status(target)
    if status.get("running"):
        return {"ok": True, "message": "already running", "pid": status.get("pid")}

    if target == "chain":
        proc = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "start_chain.py")],
            cwd=REPO, capture_output=True, timeout=90)
        time.sleep(1.5)
        status = service_status(target)
        output = (proc.stdout or b"").decode("utf-8", "replace")
        errors = (proc.stderr or b"").decode("utf-8", "replace")
        return {"ok": bool(status.get("running")),
                "message": (output or errors or "").strip()[-300:],
                "pid": status.get("pid")}

    spec = upstreams.get(target)
    if not spec:
        return {"ok": False, "message": "unknown target"}
    command = spec.get("start")
    if not command:
        return {"ok": False, "message": "上游 %s 没有定义启动命令" % target}
    cwd = spec.get("cwd") or REPO
    data_dir = spec.get("data_dir") or os.path.join(cwd, "data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        log = open(os.path.join(data_dir, "server.out.log"), "a", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
        getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=log,
                                stderr=subprocess.STDOUT,
                                creationflags=creation, close_fds=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "启动失败：%s" % exc}
    time.sleep(3.0)
    return {"ok": True, "message": "%s started" % target, "pid": proc.pid}


def service_stop(target):
    status = service_status(target)
    if not status.get("running"):
        return {"ok": True, "message": "not running"}
    pid = status["pid"]
    # Bytes, not text: taskkill writes the console codepage, and decoding it
    # as UTF-8 raises inside the reader thread and kills the call.
    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                   capture_output=True, timeout=30)
    time.sleep(1.5)
    after = service_status(target)
    return {"ok": not after.get("running"),
            "message": "stopped pid %s" % pid,
            "pid": None if not after.get("running") else after.get("pid")}


def service_restart(target):
    service_stop(target)
    time.sleep(1.0)
    return service_start(target)


def status():
    """Overall panel status - every registered upstream plus the relay."""
    services = {}
    for uid in upstreams.ids():
        services[uid] = service_status(uid)
    services["chain"] = service_status("chain")
    return {
        "panel": {"port": PANEL_PORT, "repo": REPO},
        "services": services,
        "exit": exit_ip(),
        "muse_regions": list(MUSE_REGIONS),
        "now": int(time.time()),
    }

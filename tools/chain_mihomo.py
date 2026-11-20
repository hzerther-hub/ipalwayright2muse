"""Chained mihomo instance: client -> iKuuuVPN(7890) -> IPDEEP -> target.

This is the production topology, built entirely from the stdio JSON-RPC API of
the iKuuuVPN core (no GUI, no encrypted config):

    mihomo (our instance, mixed-port 17899)
        proxy "ipdeep-jp"  (socks5)
            dialer-proxy -> "ikuuu-hop" (http -> 127.0.0.1:7890)
        proxy-group "PROXY" -> ipdeep-jp

mihomo's dialer-proxy makes the SOCKS5 dial itself travel through the iKuuuVPN
HTTP proxy, which is what lets a China-hosted machine reach IPDEEP at all.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CORE = r"C:\Program Files\ikuuu_vpn\app\iKuuuVPNCore.exe"
CFG_DIR = os.path.join(os.path.expanduser("~"), ".config", "mihomo")
CFG_PATH = os.path.join(CFG_DIR, "config.json")
CTRL_PORT = 19090
MIXED_PORT = 17899


def build_config(socks_host, socks_port, socks_user, socks_pass):
    hop = {
        "name": "ikuuu-hop",
        "type": "http",
        "server": "127.0.0.1",
        "port": 7890,
    }
    target = {
        "name": "ipdeep-jp",
        "type": "socks5",
        "server": socks_host,
        "port": int(socks_port),
    }
    if socks_user:
        target["username"] = socks_user
        target["password"] = socks_pass
    target["dialer-proxy"] = "ikuuu-hop"
    return {
        "port": MIXED_PORT,
        "socks-port": MIXED_PORT + 1,
        "http-port": MIXED_PORT + 2,
        "mixed-port": MIXED_PORT,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:%d" % CTRL_PORT,
        "dns": {"enable": True, "listen": "127.0.0.1:1054", "enhanced-mode": "fake-ip"},
        "proxies": [hop, target],
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["ipdeep-jp", "ikuuu-hop", "DIRECT"]}
        ],
        "rules": ["MATCH,PROXY"],
    }


class Core:
    def __init__(self):
        self.proc = subprocess.Popen(
            [CORE], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=1, universal_newlines=True,
            encoding="utf-8", errors="replace",
        )
        self.lines = []
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for raw in self.proc.stdout:
            raw = raw.rstrip("\r\n")
            if raw:
                with self.lock:
                    self.lines.append(raw)

    def send(self, method, data):
        self.proc.stdin.write(
            json.dumps({"id": method, "method": method, "data": data}, ensure_ascii=False) + "\n"
        )
        self.proc.stdin.flush()

    def drain(self, wait=1.0):
        time.sleep(wait)
        with self.lock:
            out = list(self.lines)
            self.lines.clear()
        return out

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def api(path, timeout=10):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (CTRL_PORT, path))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def via_proxy(url, proxy):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    opener.addheaders = [("User-Agent", "curl/8.0")]
    with opener.open(url, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0..0.1"
    port = sys.argv[2] if len(sys.argv) > 2 else "11085"
    user = sys.argv[3] if len(sys.argv) > 3 else "ipdeepuser"
    pwd = sys.argv[4] if len(sys.argv) > 4 else "ipdeeppass"

    os.makedirs(CFG_DIR, exist_ok=True)
    cfg = build_config(host, port, user, pwd)
    with open(CFG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print("config written:", CFG_PATH)

    core = Core()
    time.sleep(1.2)
    core.drain(0.4)
    core.send("setupConfig", json.dumps({"yaml": json.dumps(cfg)}))
    time.sleep(3.0)

    print("\n--- core log after setupConfig ---")
    for ln in core.drain(1.0):
        if "stdio mode" in ln:
            continue
        print("   ", ln[:240])

    print("\n--- listeners opened by this core ---")
    out = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, timeout=20
    ).stdout
    found = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        local = parts[1]
        try:
            p = int(local.rsplit(":", 1)[1])
        except ValueError:
            continue
        if p in (CTRL_PORT, MIXED_PORT, MIXED_PORT + 1, MIXED_PORT + 2, 1054):
            found.add(p)
            print("   ", line.strip())
    print("    open:", sorted(found))

    print("\n--- proxies known to the core ---")
    try:
        data = json.loads(api("/proxies"))
        for name, info in (data.get("proxies") or {}).items():
            print("   %-14s type=%-8s now=%s" % (name, info.get("type"), info.get("now")))
    except Exception as exc:
        print("   api failed:", type(exc).__name__, exc)
        for ln in core.drain(1.0):
            if "level=error" in ln:
                print("   err:", ln[:200])
        core.close()
        return 1

    print("\n--- select ipdeep-jp in group PROXY ---")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/proxies/PROXY" % CTRL_PORT,
            data=json.dumps({"name": "ipdeep-jp"}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print("   selected")
    except Exception as exc:
        print("   select failed:", type(exc).__name__, exc)

    time.sleep(1.5)
    try:
        data = json.loads(api("/proxies/PROXY"))
        print("   PROXY now =", data.get("now"))
    except Exception:
        pass

    print("\n--- egress through chained mihomo (mixed-port %d) ---" % MIXED_PORT)
    proxy = "http://127.0.0.1:%d" % MIXED_PORT
    for url in (
        "http://ip-api.com/json/?fields=query,countryCode,city,as,isp",
        "https://muse.ai/api/session",
    ):
        try:
            print("  ", url)
            print("    ->", via_proxy(url, proxy).strip()[:200])
        except Exception as exc:
            print("    -> FAIL", type(exc).__name__, exc)

    print("\n--- recent core log ---")
    for ln in core.drain(1.0)[-10:]:
        print("   ", ln[:220])

    print("\n(core left running for inspection; pid %d)" % core.proc.pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())

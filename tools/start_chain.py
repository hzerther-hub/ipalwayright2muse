r"""One-shot launcher for the iKuuuVPN -> IPDEEP chain relay.

Writes the chain relay configuration into data/chain.env (or uses the values you
pass in) and starts the relay as a background process.

Usage (PowerShell):
  python tools\start_chain.py --socks-host 1.2.3.4 --socks-port 1080 --socks-user USER --socks-pass PASS

Then point muse2api at the relay:
  - account proxy field : http://127.0.0.1:17891
  - Chrome --proxy-server: http://127.0.0.1:17891
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, "data", "chain.env")
LOG_FILE = os.path.join(ROOT, "data", "chain.log")
RELAY_PID = os.path.join(ROOT, "data", "chain.pid")
DEFAULT_LISTEN = "127.0.0.1:17891"
DEFAULT_UPSTREAM = "http://127.0.0.1:7890"


def load_env_file(path):
    values = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def save_env_file(path, values):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Chain relay config for muse2api\n")
        fh.write("# client -> 127.0.0.1:17891 -> iKuuuVPN(7890) -> IPDEEP SOCKS5 -> target\n")
        for key in sorted(values):
            fh.write("%s=%s\n" % (key, values[key]))


def detect_egress(proxy):
    req = urllib.request.Request(
        "http://ip-api.com/json/?fields=query,countryCode,city,as,isp",
        headers={"User-Agent": "muse2api-chain/1.0"},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socks-host", default=None, help="IPDEEP SOCKS5 host")
    ap.add_argument("--socks-port", default=None, type=int)
    ap.add_argument("--socks-user", default=None)
    ap.add_argument("--socks-pass", default=None)
    ap.add_argument("--listen", default=None)
    ap.add_argument("--upstream", default=None, help="iKuuuVPN HTTP proxy")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()

    stored = load_env_file(ENV_FILE)

    if args.stop:
        if os.path.exists(RELAY_PID):
            with open(RELAY_PID, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip())
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False)
                print("stopped chain relay pid", pid)
            except OSError as exc:
                print("stop failed:", exc)
            if os.path.exists(RELAY_PID):
                os.remove(RELAY_PID)
        else:
            print("no chain relay pid file")
        return 0

    cfg = {
        "CHAIN_LISTEN": args.listen or stored.get("CHAIN_LISTEN", DEFAULT_LISTEN),
        "CHAIN_HTTP_PROXY": args.upstream or stored.get("CHAIN_HTTP_PROXY", DEFAULT_UPSTREAM),
        "CHAIN_SOCKS_HOST": args.socks_host or stored.get("CHAIN_SOCKS_HOST", ""),
        "CHAIN_SOCKS_PORT": str(args.socks_port or stored.get("CHAIN_SOCKS_PORT", "1080")),
        "CHAIN_SOCKS_USER": args.socks_user or stored.get("CHAIN_SOCKS_USER", ""),
        "CHAIN_SOCKS_PASS": args.socks_pass or stored.get("CHAIN_SOCKS_PASS", ""),
        "CHAIN_LOG": LOG_FILE,
    }
    save_env_file(ENV_FILE, cfg)
    print("config written to", ENV_FILE)

    env = dict(os.environ)
    env.update(cfg)

    logfh = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "tools", "chain_proxy.py")],
        env=env,
        stdout=logfh,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with open(RELAY_PID, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    print("chain relay started, pid", proc.pid)
    time.sleep(2)

    listen = cfg["CHAIN_LISTEN"]
    proxy = "http://" + listen
    print("\n--- egress through relay ---")
    try:
        print(json.dumps(detect_egress(proxy), ensure_ascii=False))
    except Exception as exc:
        print("probe failed:", type(exc).__name__, exc)
        print("check", LOG_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Try several targets through the upstream SOCKS5 to tell an IPDEEP outage
apart from a single blocked destination."""
import os
import socket
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _chain_via_jump import fetch_ipinfo, http_connect, socks5_over  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TARGETS = [
    ("ipinfo.io", 443),
    ("muse.ai", 443),
    ("www.google.com", 443),
    ("api.github.com", 443),
]


def load_env():
    cfg = {}
    with open(os.path.join(ROOT, "data", "chain.env"), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def probe(cfg, host, port, timeout=20):
    jump = cfg["CHAIN_HTTP_PROXY"].split("://", 1)[1].rstrip("/")
    jh, _, jp = jump.partition(":")
    jport = int(jp or 80)
    try:
        s = http_connect(jh, jport, cfg["CHAIN_SOCKS_HOST"], int(cfg["CHAIN_SOCKS_PORT"]))
    except Exception as exc:  # noqa: BLE001
        return "jump-connect-fail: %s" % exc
    try:
        s = socks5_over(s, cfg["CHAIN_SOCKS_USER"], cfg["CHAIN_SOCKS_PASS"], host, port,
                         timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return "socks5-fail: %s" % exc
    try:
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(s, server_hostname=host)
        tls.settimeout(timeout)
        tls.sendall(("HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % host).encode())
        head = tls.recv(256).decode("latin1", "replace")
        first = head.split("\r\n", 1)[0]
        return "OK  %s" % first
    except Exception as exc:  # noqa: BLE001
        return "tls-fail: %s: %s" % (type(exc).__name__, exc)


def main():
    cfg = load_env()
    for host, port in TARGETS:
        print("%-20s -> %s" % (host, probe(cfg, host, port)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Reliable exit-IP check through the local chain relay.

Reads exactly Content-Length bytes instead of waiting for EOF (keep-alive
would hang the previous probe), so the result is not a false negative.
"""
import json
import os
import socket
import ssl
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def http_connect(proxy_host, proxy_port, target_host, target_port, timeout=25):
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    sock.settimeout(timeout)
    sock.sendall((
        "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
        % (target_host, target_port, target_host, target_port)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
    if " 200 " not in head:
        sock.close()
        raise RuntimeError("proxy refused CONNECT: %s" % head.strip())
    return sock


def read_http_body(sock, host, path="/json", timeout=25):
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.settimeout(timeout)
    tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: exit-ip-check\r\n"
                 "Accept: application/json\r\nConnection: close\r\n\r\n"
                 % (path, host)).encode())
    data = b""
    while b"\r\n\r\n" not in data:
        c = tls.recv(65536)
        if not c:
            break
        data += c
    head, _, rest = data.partition(b"\r\n\r\n")
    head_txt = head.decode("latin1", "replace")
    clen = None
    for line in head_txt.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            clen = int(line.split(":", 1)[1].strip())
    body = rest
    if clen is not None:
        while len(body) < clen:
            c = tls.recv(65536)
            if not c:
                break
            body += c
        body = body[:clen]
    return body.decode("utf-8", "replace")


def main():
    cfg = load_env()
    host, _, port = cfg["CHAIN_LISTEN"].partition(":")
    port = int(port)
    print("chain relay: %s:%d" % (host, port))
    try:
        sock = http_connect(host, port, "ipinfo.io", 443)
        body = read_http_body(sock, "ipinfo.io")
        d = json.loads(body)
        print("  exit IP   : %s" % d.get("ip"))
        print("  country   : %s" % d.get("country"))
        print("  city      : %s" % d.get("city"))
        print("  org / ASN : %s" % d.get("org"))
        print("  timezone  : %s" % d.get("timezone"))
    except Exception as exc:  # noqa: BLE001
        print("  ERROR: %s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

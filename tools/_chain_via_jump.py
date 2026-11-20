# -*- coding: utf-8 -*-
"""Test the upstream SOCKS5 through the jump HTTP proxy (the real chain path).

Chain:  local relay -> jump(7890 HTTP CONNECT) -> upstream SOCKS5 -> target

Also reports the jump's own exit IP so a node rotation can be spotted.
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


def http_connect(jump_host, jump_port, target_host, target_port, timeout=25):
    """Open a tunnel to target through the jump HTTP proxy. Returns the socket."""
    sock = socket.create_connection((jump_host, jump_port), timeout=timeout)
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
        raise RuntimeError("jump refused CONNECT: %s" % head.strip())
    return sock


def socks5_over(sock, user, password, host, port, timeout=25):
    sock.settimeout(timeout)
    sock.sendall(b"\x05\x02\x00\x02")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 0x05:
        raise RuntimeError("socks5 greeting bad: %r" % (resp,))
    if resp[1] == 0x02:
        sock.sendall(b"\x01" + bytes([len(user)]) + user.encode()
                     + bytes([len(password)]) + password.encode())
        auth = sock.recv(2)
        if len(auth) < 2 or auth[1] != 0x00:
            raise RuntimeError("socks5 auth rejected: %r" % (auth,))
    elif resp[1] != 0x00:
        raise RuntimeError("socks5 no acceptable auth method: %r" % (resp,))
    req = (b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
           + port.to_bytes(2, "big"))
    sock.sendall(req)
    head = sock.recv(4)
    if len(head) < 4:
        raise RuntimeError("socks5 reply too short: %r" % (head,))
    if head[1] != 0x00:
        raise RuntimeError("socks5 CONNECT refused, code=0x%02x" % head[1])
    atyp = head[3]
    if atyp == 0x01:
        sock.recv(4)
    elif atyp == 0x03:
        ln = sock.recv(1)[0]
        sock.recv(ln)
    elif atyp == 0x04:
        sock.recv(16)
    sock.recv(2)
    return sock


def fetch_ipinfo(sock, host="ipinfo.io"):
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.sendall(("GET /json HTTP/1.1\r\nHost: %s\r\nUser-Agent: chain-probe\r\n"
                 "Connection: close\r\n\r\n" % host).encode())
    data = b""
    while True:
        c = tls.recv(65536)
        if not c:
            break
        data += c
    text = data.decode("utf-8", "replace")
    body = text.split("\r\n\r\n", 1)[-1]
    try:
        d = json.loads(body)
        return "%s | %s %s | %s" % (d.get("ip"), d.get("country"), d.get("city"), d.get("org"))
    except Exception:  # noqa: BLE001
        return "RAW: %s" % text[:200].replace("\r\n", " ")


def main():
    cfg = load_env()
    jump = cfg["CHAIN_HTTP_PROXY"].split("://", 1)[1].rstrip("/")
    jh, _, jp = jump.partition(":")
    jport = int(jp or 80)
    sh = cfg["CHAIN_SOCKS_HOST"]
    sp = int(cfg["CHAIN_SOCKS_PORT"])

    print("=== jump exit IP (iKuuuVPN node) ===")
    try:
        s = http_connect(jh, jport, "ipinfo.io", 443)
        print("   ", fetch_ipinfo(s))
    except Exception as exc:  # noqa: BLE001
        print("    ERROR:", exc)

    print("\n=== upstream SOCKS5 through jump (%s:%s) ===" % (sh, sp))
    try:
        s = http_connect(jh, jport, sh, sp)
        print("    jump CONNECT to upstream: OK")
    except Exception as exc:  # noqa: BLE001
        print("    jump CONNECT to upstream FAILED:", exc)
        return 1
    try:
        s = socks5_over(s, cfg["CHAIN_SOCKS_USER"], cfg["CHAIN_SOCKS_PASS"], "ipinfo.io", 443)
        print("    socks5 handshake + CONNECT: OK")
    except Exception as exc:  # noqa: BLE001
        print("    socks5 FAILED:", exc)
        return 1
    try:
        print("    exit:", fetch_ipinfo(s))
    except Exception as exc:  # noqa: BLE001
        print("    TLS/IP read failed:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Layered probe of the static-exit chain.

  L0  local relay      127.0.0.1:17891
  L1  jump HTTP proxy  127.0.0.1:7890      (iKuuuVPN)
  L2  upstream SOCKS5  <ip>:<port>         (IPDEEP, via L1 as dialer)

Prints the observed exit IP at each layer so a break can be localised.
"""
import json
import os
import socket
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IP_URL = "https://ipinfo.io/json"


def load_env():
    cfg = {}
    path = os.path.join(ROOT, "data", "chain.env")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def via_http_proxy(proxy_url, timeout=25):
    """Use the proxy as an HTTP CONNECT proxy."""
    host_port = proxy_url.split("://", 1)[1].rstrip("/")
    host, _, port = host_port.partition(":")
    port = int(port or 80)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall((
                "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n"
                % ("ipinfo.io", "ipinfo.io")).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
            if " 200 " not in head:
                return "CONNECT-FAIL: %s" % head.strip()
            tls = _wrap_tls(sock)
            req = ("GET /json HTTP/1.1\r\nHost: ipinfo.io\r\n"
                   "User-Agent: chain-probe\r\nConnection: close\r\n\r\n")
            tls.sendall(req.encode())
            data = b""
            while True:
                c = tls.recv(65536)
                if not c:
                    break
                data += c
            return _ip_from_http(data.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return "ERROR: %s: %s" % (type(exc).__name__, exc)


def _wrap_tls(sock):
    import ssl
    ctx = ssl.create_default_context()
    return ctx.wrap_socket(sock, server_hostname="ipinfo.io")


def _ip_from_http(text):
    body = text.split("\r\n\r\n", 1)[-1]
    try:
        d = json.loads(body)
        return "%s | %s %s" % (d.get("ip"), d.get("country"), d.get("city"))
    except Exception:  # noqa: BLE001
        return "RAW: %s" % text[:160].replace("\r\n", " ")


def via_socks5(host, port, user, password, timeout=25):
    """SOCKS5 CONNECT through the upstream, optionally dialing via jump proxy."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"\x05\x01\x02")  # version, 1 method, user/pass
            resp = sock.recv(2)
            if len(resp) < 2 or resp[0] != 0x05:
                return "SOCKS5 handshake bad: %r" % (resp,)
            if resp[1] == 0x02:
                sock.sendall(b"\x01" + bytes([len(user)]) + user.encode()
                             + bytes([len(password)]) + password.encode())
                auth = sock.recv(2)
                if len(auth) < 2 or auth[1] != 0x00:
                    return "SOCKS5 auth rejected: %r" % (auth,)
            req = b"\x05\x01\x00\x03" + bytes([len(b"ipinfo.io")]) + b"ipinfo.io" + \
                (443).to_bytes(2, "big")
            sock.sendall(req)
            head = sock.recv(4)
            if len(head) < 4 or head[1] != 0x00:
                return "SOCKS5 connect refused: code=%r" % (head[1] if len(head) > 1 else None,)
            atyp = head[3]
            if atyp == 0x01:
                sock.recv(4)
            elif atyp == 0x03:
                ln = sock.recv(1)[0]
                sock.recv(ln)
            elif atyp == 0x04:
                sock.recv(16)
            sock.recv(2)
            tls = _wrap_tls(sock)
            tls.sendall(b"GET /json HTTP/1.1\r\nHost: ipinfo.io\r\n"
                        b"User-Agent: chain-probe\r\nConnection: close\r\n\r\n")
            data = b""
            while True:
                c = tls.recv(65536)
                if not c:
                    break
                data += c
            return _ip_from_http(data.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return "ERROR: %s: %s" % (type(exc).__name__, exc)


def main():
    cfg = load_env()
    jump = cfg["CHAIN_HTTP_PROXY"]
    relay = "http://" + cfg["CHAIN_LISTEN"]

    print("L1 jump HTTP proxy  %s" % jump)
    print("   -> %s" % via_http_proxy(jump))

    print("\nL0 local relay      %s" % relay)
    print("   -> %s" % via_http_proxy(relay))

    print("\nL2 upstream SOCKS5  %s:%s (direct)"
          % (cfg["CHAIN_SOCKS_HOST"], cfg["CHAIN_SOCKS_PORT"]))
    print("   -> %s" % via_socks5(cfg["CHAIN_SOCKS_HOST"], int(cfg["CHAIN_SOCKS_PORT"]),
                                  cfg["CHAIN_SOCKS_USER"], cfg["CHAIN_SOCKS_PASS"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Chained local proxy: client -> local HTTP proxy -> upstream HTTP proxy -> upstream SOCKS5 -> target.

Why this exists
---------------
IPDEEP (and similar static-residential vendors) block direct connections from
mainland-China source IPs. The only reliable way in from this machine is to
hop out through a non-China exit first. iKuuuVPN (mihomo) already provides a
local HTTP proxy on 127.0.0.1:7890, so the chain becomes:

    muse2api / Chrome  ->  127.0.0.1:17891 (this relay)
                       ->  127.0.0.1:7890   (iKuuuVPN HTTP proxy, non-China exit)
                       ->  IPDEEP SOCKS5   (Japan static residential exit)
                       ->  target

The relay speaks plain HTTP proxy semantics on the client side, so it works
for both `requests` (proxies={"http": ...}) and Chromium (proxyServer).

Environment variables
---------------------
  CHAIN_LISTEN      host:port to listen on          (default 127.0.0.1:17891)
  CHAIN_HTTP_PROXY  upstream HTTP proxy              (default http://127.0.0.1:7890)
  CHAIN_SOCKS_HOST  upstream SOCKS5 host             (empty = skip socks5 hop)
  CHAIN_SOCKS_PORT  upstream SOCKS5 port             (default 1080)
  CHAIN_SOCKS_USER  upstream SOCKS5 username         (optional)
  CHAIN_SOCKS_PASS  upstream SOCKS5 password         (optional)
  CHAIN_LOG         path of the log file             (optional)
"""

import os
import re
import select
import socket
import struct
import sys
import threading
import time

LISTEN = os.environ.get("CHAIN_LISTEN", "127.0.0.1:17891")
HTTP_PROXY = os.environ.get("CHAIN_HTTP_PROXY", "http://127.0.0.1:7890")
SOCKS_HOST = os.environ.get("CHAIN_SOCKS_HOST", "")
SOCKS_PORT = int(os.environ.get("CHAIN_SOCKS_PORT", "1080"))
SOCKS_USER = os.environ.get("CHAIN_SOCKS_USER", "")
SOCKS_PASS = os.environ.get("CHAIN_SOCKS_PASS", "")
LOG_PATH = os.environ.get("CHAIN_LOG", "")

_log_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    if LOG_PATH:
        with _log_lock:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def split_hostport(text, default_port=80):
    text = text.strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port or default_port)
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host, int(port or default_port)
    return text, default_port


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("closed")
        buf += chunk
    return buf


def recv_until(sock, marker, limit=65536):
    buf = b""
    while marker not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def http_connect_via_upstream(host, port):
    """Open a CONNECT tunnel to host:port through the upstream HTTP proxy."""
    proxy_host, proxy_port = split_hostport(HTTP_PROXY.replace("http://", ""), 7890)
    sock = socket.create_connection((proxy_host, proxy_port), timeout=20)
    sock.settimeout(20)
    req = (
        "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n"
        "Proxy-Connection: keep-alive\r\n\r\n" % (host, port, host, port)
    )
    sock.sendall(req.encode())
    head = recv_until(sock, b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n")[0]:
        sock.close()
        raise OSError("upstream CONNECT failed: %r" % head[:120])
    return sock


def socks5_connect(tunnel, target_host, target_port):
    """Perform a SOCKS5 handshake over an existing tunnel and connect to the target."""
    methods = b"\x00\x02" if SOCKS_USER else b"\x00"
    tunnel.sendall(b"\x05" + bytes([len(methods)]) + methods)
    resp = recv_exact(tunnel, 2)
    if resp[0] != 5:
        raise OSError("bad socks5 greeting: %r" % resp)
    method = resp[1]
    if method == 2:
        user = SOCKS_USER.encode()
        pwd = SOCKS_PASS.encode()
        tunnel.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        auth = recv_exact(tunnel, 2)
        if auth[1] != 0:
            raise OSError("socks5 auth rejected: %r" % auth)
    elif method != 0:
        raise OSError("socks5 auth method unsupported: %d" % method)

    host = target_host.encode("idna") if re.match(r"^[A-Za-z0-9.\-]+$", target_host) else b""
    if host:
        req = b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", target_port)
    else:
        try:
            packed = socket.inet_aton(target_host)
        except OSError:
            packed = socket.inet_pton(socket.AF_INET6, target_host)
        req = b"\x05\x01\x00\x01" + packed + struct.pack(">H", target_port)
    tunnel.sendall(req)

    head = recv_exact(tunnel, 4)
    if head[0] != 5:
        raise OSError("bad socks5 reply: %r" % head)
    if head[1] != 0:
        raise OSError("socks5 connect failed, code=%d" % head[1])
    atyp = head[3]
    if atyp == 1:
        recv_exact(tunnel, 4 + 2)
    elif atyp == 3:
        ln = recv_exact(tunnel, 1)[0]
        recv_exact(tunnel, ln + 2)
    elif atyp == 4:
        recv_exact(tunnel, 16 + 2)
    else:
        raise OSError("socks5 bad atyp=%d" % atyp)
    return tunnel


def open_chain(target_host, target_port):
    """Return a socket connected to target_host:target_port through the full chain."""
    if SOCKS_HOST:
        tunnel = http_connect_via_upstream(SOCKS_HOST, SOCKS_PORT)
        return socks5_connect(tunnel, target_host, target_port)
    return http_connect_via_upstream(target_host, target_port)


def pump(a, b):
    socks = [a, b]
    try:
        while True:
            ready, _, err = select.select(socks, [], socks, 120)
            if err:
                break
            if not ready:
                break
            for s in ready:
                other = b if s is a else a
                data = s.recv(65536)
                if not data:
                    return
                other.sendall(data)
    except OSError:
        return
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def handle(client):
    client.settimeout(30)
    try:
        head = recv_until(client, b"\r\n\r\n")
    except OSError:
        client.close()
        return
    if not head:
        client.close()
        return

    lines = head.split(b"\r\n")
    try:
        method, target, _ = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        client.close()
        return

    if method.upper() == "CONNECT":
        host, port = split_hostport(target, 443)
        try:
            upstream = open_chain(host, port)
        except Exception as exc:  # noqa: BLE001
            log("CONNECT %s:%d FAILED %s" % (host, port, exc))
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
            client.close()
            return
        log("CONNECT %s:%d OK" % (host, port))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        client.settimeout(None)
        upstream.settimeout(None)
        pump(client, upstream)
        return

    # Plain HTTP request (absolute-form URI).
    m = re.match(r"^https?://([^/]+)(/.*)?$", target)
    if not m:
        client.close()
        return
    host, port = split_hostport(m.group(1), 80)
    path = m.group(2) or "/"
    try:
        upstream = open_chain(host, port)
    except Exception as exc:  # noqa: BLE001
        log("HTTP %s:%d FAILED %s" % (host, port, exc))
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        except OSError:
            pass
        client.close()
        return
    log("HTTP %s:%d OK" % (host, port))
    head_text = "\r\n".join(
        "%s %s%s" % (method, path, " HTTP/1.1") if i == 0 else ln.decode("latin-1")
        for i, ln in enumerate(lines)
    )
    upstream.sendall(head_text.encode("latin-1") + b"\r\n\r\n")
    client.settimeout(None)
    upstream.settimeout(None)
    pump(client, upstream)


def main():
    host, port = split_hostport(LISTEN, 17891)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    log(
        "chain relay up on %s:%d  upstream_http=%s  socks=%s:%s"
        % (host, port, HTTP_PROXY, SOCKS_HOST or "-", SOCKS_PORT if SOCKS_HOST else "-")
    )
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

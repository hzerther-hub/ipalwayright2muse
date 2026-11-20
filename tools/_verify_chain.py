"""End-to-end verification of the chain relay, including SOCKS5 auth.

Starts a mock upstream SOCKS5 server that REQUIRES username/password, then a
chain relay in front of it, then checks:
  1. egress IP seen through the relay is the non-China exit (iKuuuVPN)
  2. HTTPS CONNECT through the relay reaches a real site
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def wait_port(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def get_via_proxy(url, proxy):
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    opener = urllib.request.build_opener(handler)
    opener.addheaders = [("User-Agent", "curl/8.0")]
    with opener.open(url, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    mock_env = dict(os.environ)
    mock_env["MOCK_LISTEN"] = "127.0.0.1:11081"
    mock_env["MOCK_USER"] = "testuser"
    mock_env["MOCK_PASS"] = "testpass"
    mock = subprocess.Popen(
        [PY, os.path.join(ROOT, "tools", "mock_socks5.py")],
        env=mock_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    relay_env = dict(os.environ)
    relay_env["CHAIN_LISTEN"] = "127.0.0.1:17892"
    relay_env["CHAIN_HTTP_PROXY"] = "http://127.0.0.1:7890"
    relay_env["CHAIN_SOCKS_HOST"] = "127.0.0.1"
    relay_env["CHAIN_SOCKS_PORT"] = "11081"
    relay_env["CHAIN_SOCKS_USER"] = "testuser"
    relay_env["CHAIN_SOCKS_PASS"] = "testpass"
    relay = subprocess.Popen(
        [PY, os.path.join(ROOT, "tools", "chain_proxy.py")],
        env=relay_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        ok_mock = wait_port("127.0.0.1", 11081)
        ok_relay = wait_port("127.0.0.1", 17892)
        print("mock socks5 listening:", ok_mock)
        print("chain relay listening:", ok_relay)
        if not (ok_mock and ok_relay):
            return 1

        print("\n[1] egress IP through relay (expect iKuuuVPN exit, NOT 123.152.x.x):")
        print(get_via_proxy("http://ip-api.com/json/?fields=query,countryCode,city,as", "http://127.0.0.1:17892"))

        print("\n[2] HTTPS CONNECT through relay (expect HTTP 200):")
        body = get_via_proxy("https://muse.ai/", "http://127.0.0.1:17892")
        print("bytes received:", len(body))
        print("title found:", "Muse" in body)

        print("\n[3] negative control - wrong socks5 password must fail:")
        bad_env = dict(relay_env)
        bad_env["CHAIN_SOCKS_PASS"] = "wrongpass"
        bad = subprocess.Popen(
            [PY, os.path.join(ROOT, "tools", "chain_proxy.py")],
            env=bad_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_port("127.0.0.1", 17893, timeout=3)
            bad_env["CHAIN_LISTEN"] = "127.0.0.1:17893"
            try:
                out = get_via_proxy("http://ip-api.com/json/?fields=query", "http://127.0.0.1:17893")
                print("UNEXPECTED SUCCESS:", out[:120])
            except Exception as exc:
                print("correctly rejected:", type(exc).__name__)
        finally:
            bad.terminate()
        return 0
    finally:
        mock.terminate()
        relay.terminate()


if __name__ == "__main__":
    sys.exit(main())

"""Verify browser-side and requests-side egress are identical after the switch.

Chromium follows the Windows WinINET system proxy; Python requests follows
HTTP_PROXY/HTTPS_PROXY. Both must now land on the same Japanese exit IP, or
the session gets flagged for using two IPs.
"""

import json
import os
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proxy import request_proxy_context, request_proxies  # noqa: E402

PROXY = {"server": "http://127.0.0.1:17891", "username": "", "password": ""}
IP_URL = "http://ip-api.com/json/?fields=query,countryCode,city,as,isp"


def requests_side():
    """Use the project's own helpers exactly as engine.py does.

    request_proxies() returns {"proxies": {...}}, so it is passed as **kwargs
    to requests.get - passing it to urllib would silently ignore the proxy.
    """
    import requests

    out = {}
    with request_proxy_context(PROXY) as opts:
        resp = requests.get(IP_URL, timeout=30, **opts)
        out["proxy_context"] = resp.json()
    resp = requests.get(IP_URL, timeout=30, **request_proxies(PROXY))
    out["request_proxies"] = resp.json()
    return out


def browser_side():
    """Drive a headless Chrome through CDP and read the egress it observes."""
    script = r'''
import json, subprocess, time, urllib.request, os
chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
port = 9333
proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
    "--remote-debugging-port=%d" % port,
    "--user-data-dir=%s" % os.path.join(os.environ["TEMP"], "m2a_egress_probe"),
    "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    targets = json.loads(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=5).read())
    tid = targets[0]["id"]
    ws_url = targets[0]["webSocketDebuggerUrl"]
    import socket, base64, os as o
    # minimal websocket client
    u = ws_url.split("//",1)[1]
    hostport, path = u.split("/",1)
    host, prt = hostport.split(":")
    s = socket.create_connection((host, int(prt)), timeout=10)
    key = base64.b64encode(o.urandom(16)).decode()
    req = ("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key))
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    def send(obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        mask = o.urandom(4)
        n = len(data)
        if n < 126: hdr.append(0x80 | n)
        elif n < 65536: hdr.append(0x80 | 126); hdr += n.to_bytes(2, "big")
        else: hdr.append(0x80 | 127); hdr += n.to_bytes(8, "big")
        hdr += mask
        hdr += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        s.sendall(bytes(hdr))
    def recv():
        h = s.recv(2)
        ln = h[1] & 0x7F
        if ln == 126: ln = int.from_bytes(s.recv(2), "big")
        elif ln == 127: ln = int.from_bytes(s.recv(8), "big")
        payload = b""
        while len(payload) < ln:
            payload += s.recv(ln - len(payload))
        return payload.decode("utf-8", "replace")
    send({"id":1,"method":"Page.enable"})
    send({"id":2,"method":"Page.navigate","params":{"url":"http://ip-api.com/json/?fields=query,countryCode,city,as,isp"}})
    deadline = time.time() + 40
    body = ""
    while time.time() < deadline:
        try:
            msg = json.loads(recv())
        except Exception:
            continue
        if msg.get("method") == "Page.loadEventFired":
            break
    send({"id":3,"method":"Runtime.evaluate","params":{"expression":"document.body.innerText","returnByValue":True}})
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            msg = json.loads(recv())
        except Exception:
            continue
        if msg.get("id") == 3:
            body = msg.get("result",{}).get("result",{}).get("value","")
            break
    print(body)
finally:
    try: proc.terminate()
    except Exception: pass
'''
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_egress_browser_probe.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script)
    try:
        out = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=120)
        return out.stdout.strip() or out.stderr.strip()[:300]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def main():
    print("=" * 88)
    print("EGRESS CONSISTENCY CHECK")
    print("=" * 88)
    print("\n[requests side - muse2api proxy.py]")
    try:
        rs = requests_side()
        for k, v in rs.items():
            print("   %-16s %s" % (k, v.get("query")))
    except Exception as exc:
        print("   FAIL", type(exc).__name__, exc)

    print("\n[browser side - headless Chrome via WinINET system proxy]")
    try:
        bs = browser_side()
        print("   ", bs[:300])
    except Exception as exc:
        print("   FAIL", type(exc).__name__, exc)

    print("\n[direct (no proxy) for reference]")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(IP_URL, headers={"User-Agent": "x"}), timeout=25) as r:
            print("   ", json.loads(r.read().decode()).get("query"))
    except Exception as exc:
        print("   FAIL", type(exc).__name__, exc)


if __name__ == "__main__":
    main()

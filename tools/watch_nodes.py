r"""Watch the active iKuuuVPN node's egress IP and score every node you visit.

Usage:
  python tools\watch_nodes.py            # watch forever, print a report per new IP
  python tools\watch_nodes.py --seconds 600

Switch nodes in the iKuuuVPN GUI; each new egress IP is detected, probed and
summarised (ASN / ISP / proxy+hosting flags / fraud score / blacklist count).
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ip_quality import (  # noqa: E402
    detect_ip,
    probe_ipapi,
    probe_proxycheck,
    probe_scamalytics,
    probe_blocklist,
)

PROXY = os.environ.get("WATCH_PROXY", "http://127.0.0.1:7890")
OUT = os.environ.get("WATCH_OUT", "")


def write(text):
    print(text, flush=True)
    if OUT:
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main():
    seconds = 900
    for i, a in enumerate(sys.argv):
        if a == "--seconds" and i + 1 < len(sys.argv):
            seconds = int(sys.argv[i + 1])

    seen = {}
    write("watching egress via %s (Ctrl+C to stop)" % PROXY)
    deadline = time.time() + seconds
    while time.time() < deadline:
        ip = detect_ip(PROXY)
        if ip and ip not in seen:
            seen[ip] = time.strftime("%H:%M:%S")
            write("")
            write("=" * 76)
            write("NEW EGRESS %s   detected %s" % (ip, seen[ip]))
            write("=" * 76)
            for fn in (probe_ipapi, probe_proxycheck, probe_scamalytics, probe_blocklist):
                try:
                    write("  " + fn(ip))
                except Exception as exc:  # noqa: BLE001
                    write("  %s failed: %s" % (fn.__name__, exc))
        time.sleep(4)

    write("\nwatch finished, %d distinct egress IPs seen" % len(seen))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

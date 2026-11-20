# -*- coding: utf-8 -*-
"""Local static-IP compliance panel - HTTP server.

Stdlib only. Serves the JSON API and the single-file front end on
127.0.0.1:18611. It runs as its own process so it stays reachable even
when muse2api is down (the panel can restart it).

    python local-panel/server.py
"""
from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core  # noqa: E402
import guarantee  # noqa: E402
import login as muse_login  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
HOST = "127.0.0.1"
PORT = core.PANEL_PORT

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "muse2api-local-panel/1.0"

    # keep the console readable
    def log_message(self, fmt, *args):
        sys.stderr.write("[panel] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- helpers
    def _read_json(self):
        """Parse the request body as JSON; an empty body is an empty dict."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, relpath):
        safe = os.path.normpath(relpath).lstrip("\\/").replace("\\", "/")
        if safe in ("", "."):
            safe = "index.html"
        full = os.path.join(STATIC_DIR, safe.replace("/", os.sep))
        if not os.path.isfile(full) or not os.path.abspath(full).startswith(
                os.path.abspath(STATIC_DIR)):
            self._send_json({"error": "not found"}, status=404)
            return
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- routing
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/status":
                self._send_json(core.status())
                return
            if path == "/api/layout":
                self._send_json(core.muse_dir_state())
                return
            if path == "/api/exit":
                ip = (query.get("ip") or [""])[0].strip()
                self._send_json(core.exit_overview() if not ip else core.check_ip(ip))
                return
            if path == "/api/muse2api":
                self._send_json(core.muse2api_safety())
                return
            if path == "/api/guarantee":
                self._send_json(guarantee.WATCHDOG.snapshot())
                return
            if path == "/api/login/status":
                reveal = (query.get("reveal") or ["0"])[0] == "1"
                self._send_json(muse_login.MANAGER.status(reveal_cookies=reveal))
                return
            if path == "/api/login/pool":
                reveal = (query.get("reveal") or ["0"])[0] == "1"
                self._send_json({"accounts": muse_login.MANAGER.pool_overview(reveal)})
                return
            if path == "/api/apikey":
                self._send_json(core.api_key_info())
                return
            if path == "/api/catalog":
                self._send_json(core.catalog())
                return
            if path == "/api/hop":
                self._send_json(core.hop())
                return
            if path.startswith("/api/"):
                self._send_json({"error": "unknown endpoint"}, status=404)
                return
            self._send_file(path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": "%s: %s" % (type(exc).__name__, exc)}, status=500)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            # ---- API Key ----
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "apikey" \
                    and parts[2] == "rotate":
                self._send_json(core.api_key_rotate())
                return
            # ---- muse.ai 登录 ----
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "login":
                action = parts[2]
                if action == "start":
                    payload = self._read_json()
                    result = muse_login.MANAGER.start(
                        payload.get("email", ""), payload.get("password", ""),
                        payload.get("label", ""))
                elif action == "otp":
                    payload = self._read_json()
                    result = muse_login.MANAGER.submit_otp(
                        payload.get("code", ""), payload.get("session_id", ""))
                elif action == "import":
                    payload = self._read_json()
                    result = muse_login.MANAGER.import_to_pool(
                        payload.get("session_id", ""), payload.get("label", ""))
                else:
                    self._send_json({"ok": False, "message": "unknown action"},
                                    status=400)
                    return
                self._send_json(result)
                return
            # /api/guarantee/check - re-run the verification right now
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "guarantee" \
                    and parts[2] == "check":
                report = guarantee.WATCHDOG.run_now(enforce=True)
                self._send_json(report)
                return
            # /api/service/{target}/{action}
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "service":
                target, action = parts[2], parts[3]
                if target not in ("muse2api", "chain"):
                    self._send_json({"ok": False, "message": "unknown target"}, status=400)
                    return
                if action == "start":
                    result = core.service_start(target)
                elif action == "stop":
                    result = core.service_stop(target)
                elif action == "restart":
                    result = core.service_restart(target)
                else:
                    self._send_json({"ok": False, "message": "unknown action"}, status=400)
                    return
                result["status"] = core.service_status(target)
                self._send_json(result)
                return
            self._send_json({"error": "unknown endpoint"}, status=404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)},
                             status=500)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("local-panel listening on http://%s:%d" % (HOST, PORT), flush=True)
    print("repo: %s" % core.REPO, flush=True)
    # The watchdog re-verifies the egress guarantee on a timer, so the panel
    # keeps the promise even when nobody has the page open.
    guarantee.WATCHDOG.start()
    print("guarantee watchdog started (every %ds)" % guarantee.WATCHDOG.interval,
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        guarantee.WATCHDOG.stop()
        server.server_close()


if __name__ == "__main__":
    main()

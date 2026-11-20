# -*- coding: utf-8 -*-
"""muse.ai 登录（面板端）。

从面板直接完成「登录 muse.ai → 取 cookie → 导入账号池」，并且保证
**登录 IP 与后续使用 IP 一致**：整个登录流程跑在一个
`--proxy-server=<链式中继>` 的 Chromium 里，登录请求从日本出口发出，
取到的 cookie 因此绑定在同一个 IP 上。

这一点是硬要求。cookie 的登录 IP 与 muse2api 出图时的出口 IP 不一致，
会直接触发 Muse 的地区/风控策略。

密码不落盘、不进日志；cookie 只留在内存里，面板按需提供脱敏或完整视图。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))
sys.path.insert(0, os.path.join(HERE, "tools"))

import core  # noqa: E402

# 复用 tools/_muse_login.py 里经过实战打磨的登录动作：
# React 受控输入的填法、提交按钮的识别、页面状态分类。
import muse_login as ml  # noqa: E402

LOGIN_URL = "https://muse.ai/"
ESSENTIAL = ("hatch_sess",)

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_NEED_OTP = "need_otp"
STATE_DONE = "done"
STATE_FAILED = "failed"


def _relay_url():
    """The chain relay - the only egress that yields the Japanese IP."""
    return "http://%s:%d" % core.RELAY_LISTEN


def _free_port():
    import socket
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _chromium():
    try:
        from browser import detect_chromium
        return detect_chromium()
    except Exception:  # noqa: BLE001
        return ml.CHROME


class LoginSession:
    """One login attempt, driven by a background thread so the UI can poll."""

    def __init__(self, email, password, label=""):
        self.id = "login_%d" % int(time.time() * 1000)
        self.email = email
        self.password = password
        self.label = label or email
        self.state = STATE_IDLE
        self.stage = ""
        self.error = ""
        self.cookies = {}
        self.expires = {}
        self.exit_ip = ""
        self.steps = []
        self.otp_needed = False
        self.imported = False
        self.imported_id = ""
        self._otp = ""
        self._otp_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._proc = None
        self._page = None

    # ---------- progress reporting ----------

    def _log(self, message):
        with self._lock:
            self.steps.append({"at": int(time.time()), "message": message})
            del self.steps[:-40]

    def _set_state(self, state, stage="", error=""):
        with self._lock:
            self.state = state
            if stage:
                self.stage = stage
            if error:
                self.error = error

    def snapshot(self, reveal_cookies=False):
        with self._lock:
            data = {
                "id": self.id,
                "state": self.state,
                "stage": self.stage,
                "error": self.error,
                "label": self.label,
                "email": self.email,
                "otp_needed": self.otp_needed,
                "imported": self.imported,
                "imported_id": self.imported_id,
                "exit_ip": self.exit_ip,
                "steps": list(self.steps),
                "cookie_names": sorted(self.cookies.keys()),
                "has_essential": all(n in self.cookies for n in ESSENTIAL),
                "expires": dict(self.expires),
            }
            cookies = dict(self.cookies)
        if reveal_cookies and cookies:
            data["cookies"] = cookies
        return data

    def cookie_string(self):
        """The 'hatch_sess=...; hatch_vml=...' format the admin page expects."""
        with self._lock:
            items = sorted(self.cookies.items())
        return "; ".join("%s=%s" % (name, value) for name, value in items)

    # ---------- OTP ----------

    def submit_otp(self, code):
        code = (code or "").strip()
        if not code:
            return {"ok": False, "message": "验证码为空"}
        with self._lock:
            self._otp = code
        self._otp_event.set()
        self._log("收到验证码")
        return {"ok": True}

    # ---------- worker ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return {"ok": False, "message": "已经在运行了"}
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="muse-login")
        self._thread.start()
        return {"ok": True}

    def _run(self):
        self._set_state(STATE_RUNNING, "启动浏览器")
        relay = _relay_url()
        if not core._port_open("127.0.0.1", core.RELAY_LISTEN[1]):
            self._set_state(STATE_FAILED, "",
                            "链式中继 %d 没有监听，无法保证登录 IP 与使用 IP 一致"
                            % core.RELAY_LISTEN[1])
            self._log("链式中继不可用")
            return

        port = _free_port()
        profile = tempfile.mkdtemp(prefix="m2a_panel_login_")
        try:
            self._proc = subprocess.Popen(
                [_chromium(),
                 "--headless=new",
                 "--no-sandbox",
                 "--disable-gpu",
                 "--proxy-server=" + relay,
                 "--remote-debugging-port=%d" % port,
                 "--user-data-dir=%s" % profile,
                 "--window-size=1280,900",
                 LOGIN_URL],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            self._set_state(STATE_FAILED, "", "启动 Chrome 失败：%s" % exc)
            self._log("启动 Chrome 失败")
            return

        try:
            self._run_flow(port)
        except Exception as exc:  # noqa: BLE001
            self._set_state(STATE_FAILED, "", "%s: %s" % (type(exc).__name__, exc))
            self._log("流程异常：%s" % type(exc).__name__)
        finally:
            try:
                if self._proc:
                    self._proc.terminate()
                    self._proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(profile, ignore_errors=True)

    def _run_flow(self, port):
        from cdp import CDP, http_json

        page_ws = None
        for _ in range(60):
            try:
                pages = [t for t in http_json("http://127.0.0.1:%d/json/list" % port)
                         if t.get("type") == "page"]
                if pages:
                    pages.sort(key=lambda t: 0 if "muse.ai" in (t.get("url") or "") else 1)
                    page_ws = pages[0]["webSocketDebuggerUrl"]
                    if "muse.ai" in (pages[0].get("url") or ""):
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        if not page_ws:
            self._set_state(STATE_FAILED, "", "拿不到浏览器的调试端口")
            return

        page = CDP(page_ws, timeout=120)
        self._page = page
        try:
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Page.navigate", {"url": LOGIN_URL})
            time.sleep(10)

            for step in range(40):
                st = ml._state(page)
                state = ml.classify(st)

                if state == "need_email":
                    self._set_state(STATE_RUNNING, "填写邮箱")
                    sel = "input[type=email], input[type=text], input:not([type])"
                    ml._set(page, sel, self.email)
                    time.sleep(1)
                    ml._submit(page, ["继续", "Continue", "下一步"], sel)
                    self._log("已提交邮箱")
                elif state == "need_password":
                    self._set_state(STATE_RUNNING, "填写密码")
                    sel = "input[type=password]"
                    ml._set(page, sel, self.password)
                    time.sleep(1)
                    ml._submit(page, ["继续", "Continue", "登录", "Log in", "Sign in"], sel)
                    self._log("已提交密码")
                elif state == "need_login_click":
                    ml._click(page, ["登录", "Log in", "Sign in"])
                    self._log("点击登录")
                elif state == "need_code":
                    self._set_state(STATE_NEED_OTP, "等待邮箱验证码")
                    self._log("需要邮箱验证码")
                    with self._lock:
                        self.otp_needed = True
                    self._otp_event.wait(timeout=600)
                    self._otp_event.clear()
                    with self._lock:
                        code = self._otp
                        self._otp = ""
                        self.otp_needed = False
                    if not code:
                        continue
                    sel = "input[autocomplete=one-time-code]"
                    ml._set(page, sel, code)
                    time.sleep(1)
                    res = ml._submit(page, ["下一步", "Next", "继续", "Continue"], sel)
                    if res == "AUTO_SUBMITTED":
                        for _ in range(10):
                            time.sleep(4)
                            if ml.classify(ml._state(page)) != "need_code":
                                break
                            if not any("正在确认" in (b.get("t") or "")
                                       for b in ml._buttons_state(page)):
                                break
                    self._log("已提交验证码")
                else:
                    break
                time.sleep(6)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

        self._set_state(STATE_RUNNING, "读取 cookie")
        cookies, expires = self._harvest(port)
        with self._lock:
            self.cookies = cookies
            self.expires = expires
        self._log("取到 cookie：%s" % ", ".join(sorted(cookies)) or "无")

        missing = [n for n in ESSENTIAL if n not in cookies]
        if missing:
            self._set_state(STATE_FAILED, "",
                            "没有取到关键 cookie：%s。登录可能没成功，或页面还在验证码步骤。"
                            % ", ".join(missing))
            return
        self._set_state(STATE_DONE, "完成")

    def _harvest(self, port):
        """Read the cookies at browser level - httpOnly ones are invisible to JS."""
        from cdp import CDP, http_json

        cookies = {}
        expires = {}
        try:
            ver = http_json("http://127.0.0.1:%d/json/version" % port, timeout=10)
            browser = CDP(ver["webSocketDebuggerUrl"], timeout=60)
            try:
                res = browser.send("Storage.getCookies")
            finally:
                browser.close()
        except Exception as exc:  # noqa: BLE001
            self._log("读取 cookie 失败：%s" % type(exc).__name__)
            return cookies, expires

        for c in res.get("result", {}).get("cookies", []):
            dom = (c.get("domain") or "").lstrip(".")
            if "muse.ai" not in dom:
                continue
            cookies[c["name"]] = c.get("value", "")
            try:
                e = int(float(c.get("expires", -1)))
            except (TypeError, ValueError):
                e = -1
            if e > 0:
                expires[c["name"]] = e
        return cookies, expires


class LoginManager:
    """Keeps every login session - one account, one cookie set."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}      # id -> LoginSession
        self._order = []         # ids, newest first

    def _next_label(self):
        """Suggest muse-jp-02, muse-jp-03 ... for accounts that were not named."""
        with self._lock:
            used = {s.label for s in self._sessions.values()}
        index = 1
        while True:
            index += 1
            candidate = "muse-jp-%02d" % index
            if candidate not in used:
                return candidate

    def current(self):
        with self._lock:
            if not self._order:
                return None
            return self._sessions.get(self._order[0])

    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def start(self, email, password, label=""):
        email = (email or "").strip()
        if not email:
            return {"ok": False, "message": "邮箱不能为空"}
        if not password:
            return {"ok": False, "message": "密码不能为空"}
        with self._lock:
            for session in self._sessions.values():
                if session.state in (STATE_RUNNING, STATE_NEED_OTP):
                    return {"ok": False,
                            "message": "账号 %s 的登录流程还在跑，先等它结束" % session.label}
            session = LoginSession(email, password, label or self._next_label())
            self._sessions[session.id] = session
            self._order.insert(0, session.id)
        session.start()
        return {"ok": True, "id": session.id, "label": session.label}

    def status_all(self, reveal_cookies=False):
        with self._lock:
            sessions = [self._sessions[i] for i in self._order
                        if i in self._sessions]
        out = []
        for session in sessions:
            data = session.snapshot(reveal_cookies=reveal_cookies)
            data["cookie_string"] = session.cookie_string() if reveal_cookies else ""
            out.append(data)
        return out

    def status(self, reveal_cookies=False):
        session = self.current()
        if not session:
            return {"state": STATE_IDLE, "stage": "", "steps": [],
                    "cookie_names": [], "has_essential": False, "sessions": []}
        data = session.snapshot(reveal_cookies=reveal_cookies)
        data["cookie_string"] = session.cookie_string() if reveal_cookies else ""
        data["sessions"] = self.status_all(reveal_cookies=reveal_cookies)
        return data

    def submit_otp(self, code, session_id=""):
        session = self.get(session_id) if session_id else self.current()
        if not session:
            return {"ok": False, "message": "没有登录流程"}
        return session.submit_otp(code)

    def import_to_pool(self, session_id="", label=""):
        """Push one session's cookies into muse2api's account pool."""
        session = self.get(session_id) if session_id else self.current()
        if not session or not session.cookies:
            return {"ok": False, "message": "还没有可导入的 cookie"}
        missing = [n for n in ESSENTIAL if n not in session.cookies]
        if missing:
            return {"ok": False, "message": "缺少关键 cookie：%s" % ", ".join(missing)}
        payload = {
            "label": label or session.label,
            "cookies": dict(session.cookies),
            "expires": dict(session.expires),
        }
        res = core.upstream_request(core.upstream_id(), "POST",
                                   "/accounts", payload, timeout=60)
        if "error" in res:
            return {"ok": False, "message": res["error"]}
        account = res.get("account") or res
        session.imported = True
        session.imported_id = account.get("id")
        return {"ok": True, "message": "已导入账号池",
                "account_id": account.get("id"), "label": account.get("label")}

    def pool_overview(self, reveal_cookies=False):
        """Every account already in muse2api's pool, with its cookie summary."""
        accounts = core._account_store()
        out = []
        for account in accounts:
            cookies = account.get("cookies") or {}
            expires = account.get("cookies_exp") or {}
            item = {
                "id": account.get("id"),
                "label": account.get("label") or account.get("id"),
                "enabled": bool(account.get("enabled", True)),
                "ok": account.get("ok"),
                "expires_at": account.get("expires_at"),
                "proxy": (account.get("proxy") or {}).get("server") or "",
                "cookie_names": sorted(cookies.keys()),
                "cookie_count": len(cookies),
                "has_essential": all(n in cookies for n in ESSENTIAL),
                "expires": expires,
            }
            if reveal_cookies:
                item["cookies"] = cookies
            out.append(item)
        return out



MANAGER = LoginManager()

"""Automated muse.ai login through the chain relay (JP egress).

The whole flow runs in a Chrome instance whose --proxy-server points at the
chain relay, so the login request leaves from the same Japanese IP that the
account will use later. Sessions are read at the browser level (CDP sees
httpOnly cookies) and uploaded to the muse2api account pool.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdp import CDP, http_json  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9337
RELAY = os.environ.get("TEST_RELAY", "http://127.0.0.1:17891")
LOGIN_URL = "https://muse.ai/"

STATE_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis).map(el => ({
    type: el.type || 'text',
    name: el.name || '',
    id: el.id || '',
    placeholder: el.getAttribute('placeholder') || '',
    aria: el.getAttribute('aria-label') || '',
    inputmode: el.getAttribute('inputmode') || '',
    maxlength: el.getAttribute('maxlength') || '',
    autocomplete: el.getAttribute('autocomplete') || '',
    value: el.value || '',
  }));
  const buttons = Array.from(document.querySelectorAll('button,[role=button]'))
    .filter(vis).map(el => ({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      text: (el.innerText || '').trim().slice(0, 40),
      aria: el.getAttribute('aria-label') || '',
      disabled: !!el.disabled,
    }));
  return JSON.stringify({
    url: location.href,
    title: document.title,
    inputs,
    buttons,
    body: (document.body ? document.body.innerText : '').slice(0, 900),
  });
})()
"""

FILL_JS = r"""
((sel, text) => {
  const el = document.querySelector(sel);
  if (!el) return 'NOT_FOUND:' + sel;
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, text);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.focus();
  return 'OK';
})
"""

# Click the submit-looking control whose visible text matches one of the hints.
CLICK_JS = r"""
((hints) => {
  const btns = Array.from(document.querySelectorAll('button,[role=button],input[type=submit]'))
    .filter(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && !el.disabled;
    });
  for (const h of hints) {
    for (const b of btns) {
      const t = ((b.innerText || b.value || '') + '|' + (b.getAttribute('aria-label') || '')).trim();
      if (t.includes(h)) { b.click(); return 'CLICKED:' + t; }
    }
  }
  // Muse renders "resend code" as a plain span/div - match exact text too
  for (const h of hints) {
    const els = Array.from(document.querySelectorAll('span,div,p,label,em,strong'))
      .filter(el => {
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return false;
        const t = (el.textContent || '').trim();
        return t === h && !Array.from(el.children).some(c => (c.textContent || '').trim() === h);
      });
    if (els.length) { els[0].click(); return 'CLICKED_TEXT:' + h; }
  }
  return 'NO_BUTTON';
})
"""


def _set(page, sel, text):
    """Fill an input the way a human does.

    Setting .value from JS does NOT update React controlled inputs, so the
    submit button stays disabled. CDP Input.insertText fires real
    beforeinput/input events that React actually listens to. We verify the
    value landed and fall back to the JS setter only if it did not.
    """
    focus = ("(()=>{const el=document.querySelector(%s);"
             "if(!el) return 'NOT_FOUND'; el.focus(); el.select&&el.select();"
             "return 'OK';})()") % json.dumps(sel)
    page.js(focus)
    time.sleep(0.3)
    try:
        page.send("Input.insertText", {"text": text})
    except Exception as exc:                        # noqa: BLE001
        print("  insertText failed:", exc)
    time.sleep(0.5)
    read = ("(()=>{const el=document.querySelector(%s);"
            "return el ? String(el.value) : 'NO_EL';})()") % json.dumps(sel)
    val = page.js(read)
    if val != text:
        print("  insertText did not stick (%r) -> JS setter" % val)
        page.js("(%s)(%s, %s)" % (FILL_JS, json.dumps(sel), json.dumps(text)))
        time.sleep(0.4)
        val = page.js(read)
    return "value=%r" % val


def _click(page, hints):
    return page.js("(%s)(%s)" % (CLICK_JS, json.dumps(hints)))


def _submit(page, hints, sel=None):
    """Click the submit control; fall back to Enter when it stays disabled."""
    r = _click(page, hints)
    if r.startswith("NO_BUTTON"):
        bs = _buttons_state(page)
        print("  buttons:", json.dumps(bs, ensure_ascii=False))
        if any("正在确认" in (b.get("t") or "") or "onfirming" in (b.get("t") or "")
               or "处理中" in (b.get("t") or "") for b in bs):
            return "AUTO_SUBMITTED"
        if sel:
            r = _press_enter(page, sel)
            print("  submit via:", r)
        else:
            r = "NO_BUTTON"
    return r


def _press_enter(page, sel):
    page.js("(()=>{const el=document.querySelector(%s); if(el) el.focus();})()"
            % json.dumps(sel))
    for kind in ("keyDown", "keyUp"):
        params = {"type": kind, "key": "Enter", "code": "Enter",
                  "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
        if kind == "keyDown":
            params["text"] = "\r"
        page.send("Input.dispatchKeyEvent", params)
    return "ENTER"


def _buttons_state(page):
    raw = page.js("JSON.stringify(Array.from(document.querySelectorAll('button'))"
                  ".filter(b=>b.getBoundingClientRect().width>0)"
                  ".map(b=>({t:(b.innerText||'').trim(),d:!!b.disabled,"
                  "ty:b.getAttribute('type')||''})))")
    try:
        return json.loads(raw)
    except Exception:                                # noqa: BLE001
        return []


def _state(page):
    raw = page.js(STATE_JS)
    return json.loads(raw)


def _dump(st, label):
    print("\n===== state: %s =====" % label)
    print("url:", st.get("url"))
    for i in st.get("inputs", []):
        print("  input:", json.dumps(i, ensure_ascii=False))
    for b in st.get("buttons", []):
        print("  button:", json.dumps(b, ensure_ascii=False))
    print("  body:", (st.get("body", "") or "").replace("\n", " | ")[:600])


def classify(st):
    """Return one of: need_email / need_password / need_code / done / unknown."""
    inputs = st.get("inputs", [])
    has_pw = any(i.get("type") == "password" for i in inputs)
    has_code = any(
        (i.get("maxlength") in ("6", "8") and i.get("inputmode") in ("numeric", "tel"))
        or "code" in (i.get("autocomplete") or "").lower()
        or "验证码" in (i.get("placeholder") or "")
        or "验证码" in (i.get("aria") or "")
        for i in inputs
    )
    has_emailish = any(
        (i.get("type") in ("email", "tel", "text"))
        and not i.get("value")
        and (i.get("placeholder") or i.get("aria"))
        for i in inputs
    )
    if has_pw:
        return "need_password"
    if has_code:
        return "need_code"
    if has_emailish:
        return "need_email"
    # Muse restores a pending login session from the profile, so the first
    # screen may be the account picker ("log in as <account>") instead of the
    # email form. Click through it.
    if not inputs:
        for b in st.get("buttons", []):
            t = (b.get("text") or "").strip()
            if t in ("登录", "Log in", "Sign in"):
                return "need_login_click"
    body = st.get("body", "") or ""
    low = body.lower()
    if "验证码" in body or "verification code" in low or "one-time" in low:
        return "need_code"
    if "密码" in body or "password" in low:
        return "need_password"
    if "继续" in body or "continue" in low or "log in" in low or "登录" in body:
        return "need_email"
    return "done"


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file(path):
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _creds():
    """Credentials come from data/muse.env (gitignored) or CLI flags."""
    env = _load_env_file(os.path.join(ROOT, "data", "muse.env"))
    dot = _load_env_file(os.path.join(ROOT, ".env"))
    email = env.get("MUSE_EMAIL") or dot.get("MUSE_EMAIL") or ""
    password = env.get("MUSE_PASSWORD") or dot.get("MUSE_PASSWORD") or ""
    key = os.environ.get("MUSE2API_KEY") or dot.get("MUSE2API_KEY") or ""
    base = (os.environ.get("MUSE2API_BASE")
            or dot.get("MUSE2API_BASE") or "http://127.0.0.1:18610")
    return email, password, key, base


def _wait_otp(page, path, timeout, resends=5):
    """Poll a file the operator drops the emailed code into.

    When the wait runs out we click "resend code" so the code stays fresh and
    keep waiting - the operator can take as long as they need.
    """
    if not path:
        return ""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline and attempt <= resends:
        attempt += 1
        window = min(300, max(30, int(deadline - time.time())))
        print("  waiting for OTP in %s (try %d, up to %ds) ..." % (path, attempt, window),
              flush=True)
        stop = time.time() + window
        while time.time() < stop:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    txt = fh.read().strip()
                if txt:
                    print("  OTP received: %s" % ("*" * len(txt)), flush=True)
                    return txt
            except OSError:
                pass
            time.sleep(2)
        print("  no OTP yet -> clicking 'resend code'", flush=True)
        print("  resend:", _click(page, ["重发安全码", "Resend code", "重新发送"]), flush=True)
        time.sleep(5)
    return ""


def main():
    ap = argparse.ArgumentParser()
    _email, _password, _key, _base = _creds()
    ap.add_argument("--email", default=_email)
    ap.add_argument("--password", default=_password)
    ap.add_argument("--label", default="muse-jp-01")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--alt", action="store_true",
                    help="at the code step, try the 'other way' options first")
    ap.add_argument("--otp-file", default=os.path.join(ROOT, "data", "muse_otp.txt"),
                    help="file to poll for the emailed one-time code")
    ap.add_argument("--otp-timeout", type=int, default=3000)
    ap.add_argument("--otp", default="",
                    help="inline one-time code (skips waiting for the file)")
    ap.add_argument("--base", default=_base)
    ap.add_argument("--key", default=_key)
    args = ap.parse_args()
    if not args.email:
        print("FAIL: no email (put it in data/muse.env)")
        return 2

    profile = os.path.join(os.environ["TEMP"], "m2a_login_flow")
    proc = subprocess.Popen(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--proxy-server=" + RELAY,
            "--remote-debugging-port=%d" % PORT,
            "--user-data-dir=%s" % profile,
            "--window-size=1280,900",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        page_ws = None
        for _ in range(60):
            try:
                pages = [t for t in http_json("http://127.0.0.1:%d/json/list" % PORT)
                         if t.get("type") == "page"]
                if pages:
                    # prefer the tab that already points at muse.ai
                    pages.sort(key=lambda t: 0 if "muse.ai" in (t.get("url") or "") else 1)
                    page_ws = pages[0]["webSocketDebuggerUrl"]
                    if "muse.ai" in (pages[0].get("url") or ""):
                        break
            except Exception:
                pass
            time.sleep(0.5)
        if not page_ws:
            print("FAIL: no CDP page target")
            return 1

        page = CDP(page_ws, timeout=120)
        try:
          try:
            page.send("Page.enable")
            page.send("Runtime.enable")
            # make sure we are really on the login page
            page.send("Page.navigate", {"url": LOGIN_URL})
            time.sleep(10)

            alt_used = False
            otp_used = False
            for step in range(40):
                st = _state(page)
                state = classify(st)
                print("[step %d] state=%s" % (step, state), flush=True)
                if state == "need_email":
                    sel = "input[type=email], input[type=text], input:not([type])"
                    r = _set(page, sel, args.email)
                    print("  fill email:", r)
                    time.sleep(1)
                    print("  click:", _submit(page, ["继续", "Continue", "下一步"], sel))
                elif state == "need_password":
                    sel = "input[type=password]"
                    r = _set(page, sel, args.password)
                    print("  fill password:", r)
                    time.sleep(1)
                    print("  click:", _submit(page, ["继续", "Continue", "登录", "Log in", "Sign in"], sel))
                elif state == "need_login_click":
                    print("  click:", _click(page, ["登录", "Log in", "Sign in"]))
                elif state == "need_code":
                    if args.alt and not alt_used:
                        alt_used = True
                        print("  click alt:", _click(page, ["试试其他方式"]))
                        time.sleep(4)
                        st2 = _state(page)
                        _dump(st2, "ALT OPTIONS")
                        time.sleep(2)
                        continue
                    if not otp_used:
                        otp_used = True
                        code = args.otp or _wait_otp(page, args.otp_file, args.otp_timeout)
                        args.otp = ""               # a retry must use a fresh code
                        if not code:
                            _dump(st, "NEEDS OTP")
                            print("\n!! OTP timeout")
                            return 10
                        sel = "input[autocomplete=one-time-code]"
                        print("  fill otp:", _set(page, sel, code))
                        time.sleep(1)
                        res = _submit(page, ["下一步", "Next", "继续", "Continue"], sel)
                        print("  click:", res)
                        if res == "AUTO_SUBMITTED":
                            # Muse auto-submits as soon as the 6th digit lands;
                            # give the verification round-trip room to finish.
                            for _ in range(10):
                                time.sleep(4)
                                st3 = _state(page)
                                s3 = classify(st3)
                                if s3 != "need_code":
                                    print("  verified -> state=%s" % s3)
                                    break
                                bs = _buttons_state(page)
                                if not any("正在确认" in (b.get("t") or "") for b in bs):
                                    break   # still on the code page but not confirming
                        try:
                            os.remove(args.otp_file)   # consume it; wrong code -> wait for a new one
                        except OSError:
                            pass
                        otp_used = False           # wrong code -> let the loop wait for a fresh one
                        time.sleep(6)
                        continue
                    _dump(st, "NEEDS OTP")
                    print("\n!! 需要邮箱验证码，流程停在这里。请把验证码给我，我继续。")
                    return 10
                else:
                    _dump(st, "FINAL/" + state)
                    break
                time.sleep(6)

            time.sleep(4)
            st = _state(page)
            _dump(st, "post-login")
          except Exception as exc:  # noqa: BLE001
            # never lose the session because of a CDP hiccup - fall through
            # to the cookie harvest below
            print("!! login loop error:", exc)
            try:
                st = _state(page)
                _dump(st, "AFTER ERROR")
            except Exception:  # noqa: BLE001
                pass
        finally:
            page.close()

        # read cookies at browser level
        ver = http_json("http://127.0.0.1:%d/json/version" % PORT)
        b = CDP(ver["webSocketDebuggerUrl"], timeout=60)
        try:
            res = b.send("Storage.getCookies")
        finally:
            b.close()
        cookies = {}
        expires = {}
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
        print("\n=== muse.ai cookies:", sorted(cookies.keys()))
        print("=== has hatch_sess:", "hatch_sess" in cookies)

        if args.upload:
            if "hatch_sess" not in cookies:
                print("!! 没有 hatch_sess，不上传")
                return 11
            import urllib.request
            payload = {"label": args.label, "cookies": cookies, "expires": expires}
            req = urllib.request.Request(
                args.base.rstrip("/") + "/admin/accounts",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": "Bearer " + args.key,
                         "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                print("=== upload:", r.read().decode("utf-8", "replace")[:400])
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())

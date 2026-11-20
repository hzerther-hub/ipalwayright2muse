# -*- coding: utf-8 -*-
"""Smoke test for the local compliance panel.

Starts nothing: it expects the panel to already be listening on
127.0.0.1:18611 and exercises every API endpoint.

    python tools/_panel_smoke.py
"""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:18611"

FAILURES = []


def get(path, timeout=90):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "panel-smoke"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def post(path, timeout=90):
    req = urllib.request.Request(BASE + path, data=b"",
                                 headers={"User-Agent": "panel-smoke"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print("[%s] %-34s %s" % (mark, name, detail))
    if not cond:
        FAILURES.append(name)


def main():
    print("panel smoke test -> %s\n" % BASE)

    # ---- /api/exit -------------------------------------------------------
    try:
        d = get("/api/exit")
    except Exception as exc:  # noqa: BLE001
        print("FATAL: /api/exit unreachable: %s" % exc)
        return 1
    check("/api/exit returns a verdict", d.get("verdict") in
          ("compliant", "risky", "not_eligible"), "verdict=%s" % d.get("verdict"))
    check("/api/exit has an IP", bool(d.get("ip")), "ip=%s" % d.get("ip"))
    check("/api/exit country code", bool(d.get("country_code")),
          "cc=%s" % d.get("country_code"))
    check("/api/exit has a score", isinstance(d.get("score"), int),
          "score=%s" % d.get("score"))
    check("/api/exit risk block present", isinstance(d.get("risk"), dict))
    if d.get("country_code"):
        eligible = d["country_code"] in ("US", "CA", "JP", "SG", "MX", "AU")
        check("verdict matches eligibility", eligible == (d["verdict"] != "not_eligible"),
              "cc=%s verdict=%s" % (d["country_code"], d["verdict"]))

    # ---- /api/exit -> static_exit block ----------------------------------
    se = d.get("static_exit")
    check("/api/exit carries static_exit", isinstance(se, dict))
    if isinstance(se, dict):
        rounds = se.get("sample_rounds") or 0
        samples = se.get("samples") or []
        unique = se.get("unique") or []
        check("static_exit sampled the egress", len(samples) == rounds and rounds > 0,
              "rounds=%d samples=%d" % (rounds, len(samples)))
        # "static" means every sample returned the same address.
        check("static flag matches the samples",
              bool(se.get("is_static")) == (len(samples) == rounds and len(unique) == 1),
              "is_static=%s unique=%s" % (se.get("is_static"), unique))
        check("static_exit country requirement",
              bool(se.get("country_ok")) ==
              (se.get("country_code") == se.get("required_country")),
              "cc=%s need=%s" % (se.get("country_code"), se.get("required_country")))
        if se.get("ok"):
            check("static_exit ok implies all checks pass",
                  bool(se.get("is_static")) and bool(se.get("is_overseas"))
                  and bool(se.get("country_ok")), "reasons=%s" % (se.get("reasons") or []))
        else:
            check("static_exit not ok explains why", bool(se.get("reasons")),
                  "reasons=%s" % (se.get("reasons") or []))

    # ---- /api/exit -> muse2api block -------------------------------------
    ms = d.get("muse2api")
    check("/api/exit carries muse2api verdict",
          isinstance(ms, dict) and ms.get("verdict") in ("safe", "warn", "bad"),
          "verdict=%s" % (ms.get("verdict") if isinstance(ms, dict) else None))
    if isinstance(ms, dict):
        check("muse2api block reports the egress IP", bool(ms.get("exit_ip")),
              "ip=%s" % ms.get("exit_ip"))
        accounts = ms.get("accounts") or []
        check("muse2api block lists accounts", isinstance(accounts, list))
        for account in accounts:
            checks = account.get("checks") or {}
            if account.get("account_exit_ip") and ms.get("exit_ip"):
                check("account %s exit matches" % account.get("label"),
                      checks.get("exit_ip_match") is True,
                      "account=%s panel=%s" % (account.get("account_exit_ip"),
                                               ms.get("exit_ip")))
            break

    # ---- /api/muse2api ----------------------------------------------------
    try:
        m = get("/api/muse2api", timeout=90)
    except Exception as exc:  # noqa: BLE001
        check("/api/muse2api reachable", False, str(exc)[:80])
        m = {}
    if m:
        check("/api/muse2api returns a verdict",
              m.get("verdict") in ("safe", "warn", "bad"),
              "verdict=%s" % m.get("verdict"))
        # A standalone call must still measure the egress - otherwise the
        # "exit matches" check passes without comparing anything.
        check("/api/muse2api measured the egress", bool(m.get("exit_ip")),
              "ip=%s" % m.get("exit_ip"))
        check("/api/muse2api accounts is a list",
              isinstance(m.get("accounts"), list),
              "count=%s" % len(m.get("accounts") or []))
        for account in m.get("accounts") or []:
            checks = account.get("checks") or {}
            check("account %s has all checks" % account.get("label"),
                  set(checks) >= {"enabled", "session_ok", "essential_cookies",
                                  "proxy_configured", "proxy_reachable",
                                  "exit_ip_match", "session_valid"},
                  "checks=%s" % sorted(checks))
            if account.get("account_exit_ip") and m.get("exit_ip"):
                check("account %s exit matches" % account.get("label"),
                      checks.get("exit_ip_match") is True,
                      "account=%s panel=%s" % (account.get("account_exit_ip"),
                                               m.get("exit_ip")))

    # ---- /api/guarantee ---------------------------------------------------
    try:
        g = post("/api/guarantee/check", timeout=240)
    except Exception as exc:  # noqa: BLE001
        check("/api/guarantee reachable", False, str(exc)[:80])
        g = {}
    if g:
        check("/api/guarantee returns a verdict",
              g.get("verdict") in ("usable", "broken"),
              "verdict=%s" % g.get("verdict"))
        steps = g.get("steps") or []
        check("/api/guarantee has the four steps", len(steps) == 4,
              "steps=%s" % [s.get("name") for s in steps])
        for step in steps:
            check("step %s" % step.get("name"), isinstance(step.get("ok"), bool),
                  "ok=%s" % step.get("ok"))
        # The browser egress is the whole point: it must be measured, not assumed.
        last = steps[-1] if steps else {}
        check("/api/guarantee measured the browser egress",
              bool(last.get("browser_ip")), "ip=%s" % last.get("browser_ip"))
        check("/api/guarantee names the target node",
              bool(last.get("node_ip")), "node=%s" % last.get("node_ip"))
        links = g.get("vendor_links") or []
        check("/api/guarantee exposes the vendor links",
              bool(links) and all((l.get("url") or "").startswith("http") for l in links),
              "links=%d" % len(links))

    # ---- /api/login ------------------------------------------------------
    try:
        lg = get("/api/login/status")
    except Exception as exc:  # noqa: BLE001
        check("/api/login/status reachable", False, str(exc)[:80])
        lg = {}
    if lg:
        check("/api/login/status has a state", lg.get("state") in
              ("idle", "running", "need_otp", "done", "failed"),
              "state=%s" % lg.get("state"))
        check("/api/login/status lists sessions",
              isinstance(lg.get("sessions"), list),
              "count=%s" % len(lg.get("sessions") or []))
    try:
        pool = get("/api/login/pool")
    except Exception as exc:  # noqa: BLE001
        check("/api/login/pool reachable", False, str(exc)[:80])
        pool = {}
    if pool:
        accounts = pool.get("accounts") or []
        check("/api/login/pool lists accounts", isinstance(accounts, list),
              "count=%s" % len(accounts))
        for account in accounts:
            check("pool account %s has cookies" % account.get("label"),
                  account.get("cookie_count", 0) > 0,
                  "cookies=%s essential=%s" % (account.get("cookie_count"),
                                               account.get("has_essential")))
            check("pool account %s has hatch_sess" % account.get("label"),
                  "hatch_sess" in (account.get("cookie_names") or []))
            break

    # ---- /api/upstreams ---------------------------------------------------
    try:
        ups = get("/api/upstreams")
    except Exception as exc:  # noqa: BLE001
        check("/api/upstreams reachable", False, str(exc)[:80])
        ups = {}
    if ups:
        check("/api/upstreams lists the registry",
              "muse2api" in (ups.get("upstreams") or []),
              "upstreams=%s" % (ups.get("upstreams") or []))
        check("/api/upstreams names the current one",
              bool(ups.get("current")), "current=%s" % ups.get("current"))

    # ---- /api/invite ------------------------------------------------------
    try:
        inv = get("/api/invite")
    except Exception as exc:  # noqa: BLE001
        check("/api/invite reachable", False, str(exc)[:80])
        inv = {}
    if inv and inv.get("ok"):
        check("/api/invite reports the remaining uses",
              isinstance(inv.get("remaining_uses"), int),
              "uses=%s" % inv.get("remaining_uses"))
        check("/api/invite reports the per-use credit",
              isinstance(inv.get("credits_per_use_billion"), (int, float)),
              "credit=%s 亿" % inv.get("credits_per_use_billion"))
        check("/api/invite reports the redeem window",
              isinstance(inv.get("redeem_window_hours"), int),
              "window=%sh" % inv.get("redeem_window_hours"))
        check("/api/invite has the invite code", bool(inv.get("invite_code")),
              "code=%s" % inv.get("invite_code"))

    # ---- /api/catalog ----------------------------------------------------
    try:
        c = get("/api/catalog", timeout=120)
    except Exception as exc:  # noqa: BLE001
        check("/api/catalog reachable", False, str(exc)[:80])
        c = {}
    if c:
        check("/api/catalog has nodes", c.get("total", 0) > 0,
              "total=%s eligible=%s" % (c.get("total"), c.get("eligible_count")))
        check("/api/catalog eligible > 0", (c.get("eligible_count") or 0) > 0,
              "eligible=%s" % c.get("eligible_count"))
        codes = {n.get("country") for n in c.get("nodes", []) if n.get("muse_ok")}
        check("/api/catalog includes JP and US", {"JP", "US"} <= codes,
              "countries=%s" % sorted(codes))
        prices = [n.get("price") for n in c.get("nodes", []) if n.get("muse_ok")]
        check("/api/catalog eligible prices sane",
              bool(prices) and min(prices) > 0, "min=%s" % (min(prices) if prices else None))

    # ---- /api/hop --------------------------------------------------------
    try:
        h = get("/api/hop", timeout=90)
    except Exception as exc:  # noqa: BLE001
        check("/api/hop reachable", False, str(exc)[:80])
        h = {}
    if h:
        check("/api/hop relay alive", bool(h.get("relay_alive")),
              "relay=%s" % h.get("relay_alive"))
        check("/api/hop jump alive", bool(h.get("jump_alive")),
              "jump=%s" % h.get("jump_alive"))
        check("/api/hop upstream reachable", bool(h.get("upstream_reachable")),
              "upstream=%s" % h.get("upstream_reachable"))
        check("/api/hop has a recommendation", bool(h.get("recommendation")))

    # ---- /api/status -----------------------------------------------------
    try:
        s = get("/api/status")
    except Exception as exc:  # noqa: BLE001
        check("/api/status reachable", False, str(exc)[:80])
        s = {}
    if s:
        svc = s.get("services") or {}
        check("/api/status lists muse2api", "muse2api" in svc)
        check("/api/status lists chain", "chain" in svc)
        check("/api/status reports muse2api running",
              bool(svc.get("muse2api", {}).get("running")),
              "running=%s pid=%s" % (svc.get("muse2api", {}).get("running"),
                                     svc.get("muse2api", {}).get("pid")))

    print("\n%d checks failed" % len(FAILURES))
    if FAILURES:
        for name in FAILURES:
            print("   - %s" % name)
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

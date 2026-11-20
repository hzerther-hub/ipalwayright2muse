# -*- coding: utf-8 -*-
"""Upstream services this panel manages.

The panel is not tied to one upstream. Anything that speaks the **upstream
contract** below can be registered here and gets the full panel treatment:
exit guarantee, account pool, login, API key, service control.

The contract (all paths relative to the service's base URL):

  GET   {admin}/status
        -> {accounts: [...], browser_running: bool}
        An account needs: id, label, enabled, ok, expires_at, cookies_exp,
        proxy {server, username, password}, quota {weekly_used_pct, ...},
        essential_ok, proxy_status {proxy_configured, proxy_reachable,
        exit_ip}
  POST  {admin}/accounts          {label, cookies, expires, proxy}
  PATCH {admin}/accounts/{id}     {enabled, proxy}
  POST  {admin}/accounts/{id}/proxy/test   {proxy}
  POST  {admin}/accounts/{id}/test
  GET   {admin}/apikey             -> {api_key, base_url, models_url, ...}
  POST  {admin}/apikey/rotate
  GET   {health}                   -> liveness

Configuration lives in ``upstreams.json`` next to the panel (gitignored) so
new upstreams can be added without touching code; the built-in defaults
below cover a stock install.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = HERE

DEFAULT_ADMIN = "/admin"


def _muse_dir():
    value = os.environ.get("MUSE2API_DIR", "").strip()
    if not value:
        try:
            with open(os.path.join(REPO, "panel.env"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MUSE2API_DIR="):
                        value = line.split("=", 1)[1].strip().strip("'\"")
                        break
        except OSError:
            pass
    if not value:
        value = os.path.join(os.path.dirname(REPO), "muse2api")
    return os.path.abspath(value)


def _defaults():
    """The built-in upstreams."""
    muse_dir = _muse_dir()
    return {
        "muse2api": {
            "label": "muse2api",
            "base": "http://127.0.0.1:18610",
            "admin": DEFAULT_ADMIN,
            "health": "/healthz",
            "key_env": "MUSE2API_KEY",
            "key_file": os.path.join(muse_dir, ".env"),
            "cwd": muse_dir,
            "start": [sys_executable(), "-m", "uvicorn", "app:app",
                      "--host", "0.0.0.0", "--port", "18610"],
            "browser": True,
            "note": "上游 muse2api：浏览器驱动的生图/生视频服务",
        },
    }


def sys_executable():
    import sys
    return sys.executable


def _resolve(path, fallback=None):
    """Resolve a configured path.

    A path may be absolute, relative to the panel repo, or relative to the
    panel's parent directory (where sibling deployments live). "<...>"
    placeholders are ignored so an example file can be copied verbatim.
    """
    if not path or str(path).startswith("<"):
        return fallback
    path = os.path.expanduser(str(path))
    if os.path.isabs(path):
        return os.path.normpath(path)
    sibling = os.path.join(os.path.dirname(REPO), path)
    if os.path.exists(sibling):
        return os.path.normpath(sibling)
    return os.path.normpath(os.path.join(REPO, path))


def load():
    """Merged upstream registry: defaults plus upstreams.json overrides."""
    registry = _defaults()
    path = os.path.join(REPO, "upstreams.json")
    try:
        with open(path, encoding="utf-8") as f:
            extra = json.load(f)
    except (OSError, ValueError):
        return registry
    if not isinstance(extra, dict):
        return registry
    for key, spec in extra.items():
        if not isinstance(spec, dict):
            continue
        merged = dict(registry.get(key) or {})
        merged.update(spec)
        merged.setdefault("label", key)
        merged.setdefault("admin", DEFAULT_ADMIN)
        merged.setdefault("health", "/healthz")
        merged.setdefault("key_env", "MUSE2API_KEY")
        merged.setdefault("browser", False)
        merged["cwd"] = _resolve(spec.get("cwd"), REPO)
        merged["key_file"] = _resolve(spec.get("key_file"))
        merged["data_dir"] = _resolve(spec.get("data_dir"))
        merged.setdefault("base",
                          "http://127.0.0.1:%s" % (merged.get("port") or 0))
        if merged.get("port"):
            merged["base"] = merged["base"].rsplit(":", 1)[0] + ":%s" % merged["port"]
        command = merged.get("start")
        if isinstance(command, list) and command and command[0] == "python":
            command = [sys.executable] + command[1:]
        merged["start"] = command
        registry[key] = merged
    return registry


def get(upstream_id):
    """One upstream record, or None."""
    return load().get(upstream_id)


def ids():
    return list(load().keys())


def api_key_of(spec):
    """The upstream's access key - read from its own env file / environment."""
    env_name = spec.get("key_env") or "MUSE2API_KEY"
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    path = spec.get("key_file")
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(env_name + "="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    return ""


def admin_base(spec):
    """Base URL for an upstream's admin API."""
    return (spec.get("base") or "").rstrip("/") + (spec.get("admin") or DEFAULT_ADMIN)

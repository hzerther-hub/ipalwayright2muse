# -*- coding: utf-8 -*-
"""上游契约——每个 web2api 服务都实现这一组接口，面板据此接管它。

面板（local-panel）只认这组接口，不关心上游是谁：

  GET   {admin}/status
  POST  {admin}/accounts
  PATCH {admin}/accounts/{id}
  DELETE {admin}/accounts/{id}
  POST  {admin}/accounts/{id}/proxy/test
  POST  {admin}/accounts/{id}/test
  GET   {admin}/apikey
  POST  {admin}/apikey/rotate
  GET   {health}

只要实现这组接口，一个服务就能被面板接管：出口保证、账号池、登录、
API Key、服务启停。两个已接入的例子：muse2api（浏览器驱动生图/生视频）
与 deepseek2api（网页版内部 API，纯对话）。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid

from fastapi import HTTPException

_LOCK = threading.Lock()


class ContractMixin:
    """FastAPI mixin implementing the upstream contract.

    Subclasses provide:
        service_name   str   - shown in messages
        models         list  - OpenAI-shaped model list
        data_dir       str   - where accounts.json lives
        key_env        str   - env var holding the access key
        probe_account(account) -> dict   - test one account's session
        proxy_test(account) -> dict     - test one account's proxy
    """

    service_name = "web2api"
    models = [{"id": "web2api", "object": "model", "owned_by": "web2api"}]
    data_dir = "data"
    key_env = "WEB2API_KEY"

    # ---- subclass hooks -------------------------------------------------

    def probe_account(self, account):
        return {"ok": None, "message": "该服务没有实现会话检测"}

    def proxy_test(self, account):
        return {"ok": False, "proxy_configured": False, "proxy_reachable": False,
                "exit_ip": None, "session_ok": None,
                "message": "该服务没有实现代理检测"}

    # ---- shared machinery -----------------------------------------------

    @property
    def accounts_file(self):
        return os.path.join(self.data_dir, "accounts.json")

    def load_accounts(self):
        try:
            with open(self.accounts_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def save_accounts(self, accounts):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.accounts_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.accounts_file)

    def api_key(self):
        value = os.environ.get(self.key_env, "").strip()
        if not value:
            env_path = os.path.join(os.path.dirname(self.data_dir), ".env")
            try:
                with open(env_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(self.key_env + "="):
                            value = line.split("=", 1)[1].strip().strip("'\"")
                            break
            except OSError:
                pass
        if not value:
            value = "m2a_" + secrets.token_hex(24)
            try:
                os.makedirs(self.data_dir, exist_ok=True)
                with open(os.path.join(self.data_dir, ".generated_key"), "w",
                          encoding="utf-8") as f:
                    f.write(value)
            except OSError:
                pass
        return value

    def rotate_key(self):
        old = self.api_key()
        new = "m2a_" + secrets.token_hex(24)
        env_path = os.path.join(os.path.dirname(self.data_dir), ".env")
        try:
            os.makedirs(os.path.dirname(env_path), exist_ok=True)
            with open(env_path, "a", encoding="utf-8") as f:
                f.write("%s=%s\n" % (self.key_env, new))
        except OSError:
            pass
        return {"ok": True, "api_key": new, "previous": old,
                "message": "已生成新 Key 并立即生效；旧 Key 已失效"}

    # ---- contract endpoints ---------------------------------------------

    def contract_status(self):
        accounts = self.load_accounts()
        return {
            "accounts": [{
                "id": a.get("id"),
                "label": a.get("label") or a.get("id"),
                "enabled": a.get("enabled", True),
                "ok": a.get("ok"),
                "expires_at": a.get("expires_at"),
                "cookies_exp": a.get("cookies_exp") or {},
                "proxy": a.get("proxy"),
                "quota": a.get("quota") or {},
                "essential_ok": bool(a.get("cookies") or a.get("token")),
                "cookie_count": len(a.get("cookies") or {}),
                "proxy_status": a.get("proxy_status") or {},
                "created_at": a.get("created_at"),
                "last_used": a.get("last_used"),
                "use_count": a.get("use_count", 0),
                "note": a.get("note", ""),
            } for a in accounts],
            "browser_running": False,
            "base_url": "",
            "service": self.service_name,
        }

    def contract_add_account(self, label="", cookies=None, expires=None,
                             proxy=None, token=None, extra=None):
        accounts = self.load_accounts()
        aid = uuid.uuid4().hex[:12]
        now = int(time.time())
        account = {
            "id": aid,
            "label": label or aid,
            "cookies": cookies or {},
            "token": token or "",
            "cookies_exp": expires or {},
            "proxy": proxy or {},
            "enabled": True,
            "ok": None,
            "created_at": now,
            "last_used": None,
            "use_count": 0,
            "note": "",
        }
        if extra:
            account.update(extra)
        accounts.append(account)
        self.save_accounts(accounts)
        return {"ok": True, "account": account}

    def contract_patch_account(self, aid, label=None, enabled=None, proxy=None):
        accounts = self.load_accounts()
        for a in accounts:
            if a.get("id") != aid:
                continue
            if label is not None:
                a["label"] = label
            if enabled is not None:
                a["enabled"] = enabled
            if proxy is not None:
                a["proxy"] = proxy
                a["proxy_status"] = {}
                a["ok"] = None
            self.save_accounts(accounts)
            return {"ok": True, "account": a}
        raise HTTPException(404, "账号不存在")

    def contract_delete_account(self, aid):
        accounts = self.load_accounts()
        kept = [a for a in accounts if a.get("id") != aid]
        if len(kept) == len(accounts):
            raise HTTPException(404, "账号不存在")
        self.save_accounts(kept)
        return {"ok": True, "id": aid}

    def contract_probe_account(self, aid):
        accounts = self.load_accounts()
        account = next((a for a in accounts if a.get("id") == aid), None)
        if not account:
            raise HTTPException(404, "账号不存在")
        result = self.probe_account(account)
        ok = result.get("ok")
        for a in accounts:
            if a.get("id") == aid:
                if ok is not None:
                    a["ok"] = ok
                a["note"] = (result.get("message") or "")[:200]
                a["checked_at"] = int(time.time())
                self.save_accounts(accounts)
        return result

    def contract_proxy_test(self, aid):
        accounts = self.load_accounts()
        account = next((a for a in accounts if a.get("id") == aid), None)
        if not account:
            raise HTTPException(404, "账号不存在")
        result = self.proxy_test(account)
        recorded = {**result, "checked_at": int(time.time())}
        for a in accounts:
            if a.get("id") == aid:
                a["proxy_status"] = recorded
                self.save_accounts(accounts)
        return result

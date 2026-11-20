# -*- coding: utf-8 -*-
"""deepseek2api — DeepSeek 网页版聊天，转成 OpenAI 兼容 API。

和 muse2api 同一个形态，但技术路线不同：

  muse2api      能力只在 UI 里（生图/生视频），必须驱动 Chromium 点按钮
  deepseek2api  网页版内部就有标准 chat 接口，直接调，不驱动 UI

内部接口是从网页版的 JS 包里扒出来的（tools/_probe_upstreams.py 可复现）：

  POST /api/v0/chat/completion             对话（SSE 流）
  POST /api/v0/chat/continue               续写
  POST /api/v0/chat/resume_stream          恢复流
  POST /api/v0/chat/create_pow_challenge   proof-of-work 挑战
  POST /api/v0/auth/ticket                 取 auth ticket

每个对话请求前要先过一道 PoW，答案由官方 wasm 求解（web2api/pow.py）。

**状态**：服务与上游契约已通；对话接口需要账号会话（cookie + token），
未接入时明确返回 501 而不是假装成功。
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "web2api"))
sys.path.insert(0, os.path.join(REPO, "lib"))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from web2api.contract import ContractMixin  # noqa: E402
from web2api import pow as ds_pow  # noqa: E402

SITE = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
CHALLENGE_PATH = "/api/v0/chat/create_pow_challenge"


class DeepSeek2api(ContractMixin):
    service_name = "deepseek2api"
    models = [
        {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek-web"},
        {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek-web"},
    ]
    data_dir = os.path.join(HERE, "data")
    key_env = "DEEPSEEK2API_KEY"

    def probe_account(self, account):
        # A real probe needs the auth token; without it the session is unusable.
        if not account.get("token") and not account.get("cookies"):
            return {"ok": False, "message": "账号没有会话凭据（token 或 cookie）"}
        return {"ok": None, "message": "会话探测还没接上"}

    def proxy_test(self, account):
        proxy = account.get("proxy") or {}
        return {
            "ok": False,
            "proxy_configured": bool(proxy.get("server")),
            "proxy_reachable": False,
            "exit_ip": None,
            "session_ok": None,
            "message": "代理检测还没接上",
        }


svc = DeepSeek2api()
app = FastAPI(title="deepseek2api", version="0.2.0")


def auth(authorization: str = Header(default=None)):
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token != svc.api_key():
        raise HTTPException(401, "API Key 无效或缺失")


# --------------------------------------------------------------------------
# 上游契约
# --------------------------------------------------------------------------

@app.get("/admin/status")
def status(_=Depends(auth)):
    return svc.contract_status()


class AccountIn(BaseModel):
    label: str = ""
    cookies: dict[str, str] = Field(default_factory=dict)
    expires: dict[str, int] = Field(default_factory=dict)
    proxy: dict | None = None
    token: str = ""


@app.post("/admin/accounts")
def add_account(req: AccountIn, _=Depends(auth)):
    return svc.contract_add_account(req.label, req.cookies, req.expires,
                                    req.proxy, req.token)


class AccountPatch(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    proxy: dict | None = None


@app.patch("/admin/accounts/{aid}")
def patch_account(aid: str, req: AccountPatch, _=Depends(auth)):
    return svc.contract_patch_account(aid, req.label, req.enabled, req.proxy)


@app.delete("/admin/accounts/{aid}")
def del_account(aid: str, _=Depends(auth)):
    return svc.contract_delete_account(aid)


@app.post("/admin/accounts/{aid}/proxy/test")
def test_account_proxy(aid: str, _=Depends(auth)):
    return svc.contract_proxy_test(aid)


@app.post("/admin/accounts/{aid}/test")
def test_account(aid: str, _=Depends(auth)):
    return svc.contract_probe_account(aid)


@app.get("/admin/apikey")
def get_apikey(_=Depends(auth)):
    return {"api_key": svc.api_key(), "base_url": "", "models_url": "/v1/models",
            "media_url": "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    return svc.rotate_key()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    accounts = svc.load_accounts()
    usable = [a for a in accounts if a.get("enabled", True) and a.get("token")]
    return {"ok": True, "accounts": len(accounts), "usable": len(usable)}


# --------------------------------------------------------------------------
# OpenAI 兼容接口
# --------------------------------------------------------------------------

@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": svc.models}


class Message(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str = "deepseek-chat"
    messages: list[Message] = Field(default_factory=list)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest, _=Depends(auth)):
    """还没接上 DeepSeek 的会话与 token——先明确报错，不要假装成功。

    接上需要三步：
      1. 登录拿到会话 cookie 与 auth token（网页版登录要手机号/邮箱+验证码）
      2. 每个请求前换 PoW 挑战并求解（web2api/pow.py 已实现）
      3. 按抓到的请求形状调 /api/v0/chat/completion，把 SSE 映射成 OpenAI chunk
    用 web2api/capture.py 抓一次真实请求就能确定第 3 步的形状。
    """
    accounts = [a for a in svc.load_accounts()
                if a.get("enabled", True) and (a.get("token") or a.get("cookies"))]
    if not accounts:
        raise HTTPException(
            503,
            "账号池里没有可用会话。先在面板登录/导入 DeepSeek 账号"
            "（需要网页版登录拿 token 与 cookie）。")
    raise HTTPException(
        501,
        "DeepSeek 对话链路还没接上：PoW 求解已实现（web2api/pow.py），"
        "但还缺真实请求的形状。用 web2api/capture.py 抓一次即可补齐。")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "18612")))

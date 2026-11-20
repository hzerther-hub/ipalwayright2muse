# -*- coding: utf-8 -*-
"""通义千问 —— https://chat.qwen.ai/ 的网页版聊天，转成 OpenAI 兼容 API。

状态：**脚手架**。服务与上游契约已通（面板可以接管它），对话接口还没接上。

接入步骤（每次约 10 分钟）：
  1. 登录 https://chat.qwen.ai/ 拿到会话凭据，导入面板的账号池
  2. python -m web2api.capture --site https://chat.qwen.ai/ --pattern "chat|completion" --out qwen2api.json
     在弹出的浏览器里发一条消息，脚本会录下内部请求的形状
  3. 按录到的形状写 adapter：构造请求、解 SSE、映射成 OpenAI chunk

已知情况：页面能开但接口在懒加载的 chunk 里，需浏览器级抓包
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "web2api"))
sys.path.insert(0, os.path.join(REPO, "lib"))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from web2api.contract import ContractMixin  # noqa: E402


class Qwen2api(ContractMixin):
    service_name = "qwen2api"
    models = [{"id": "qwen2api", "object": "model", "owned_by": "qwen2api"}]
    data_dir = os.path.join(HERE, "data")
    key_env = "QWEN2API_KEY"


svc = Qwen2api()
app = FastAPI(title="qwen2api", version="0.1.0")


def auth(authorization: str = Header(default=None)):
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token != svc.api_key():
        raise HTTPException(401, "API Key 无效或缺失")


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
    return {"api_key": svc.api_key(), "base_url": "",
            "models_url": "/v1/models", "media_url": "/v1/media/{name}"}


@app.post("/admin/apikey/rotate")
def rotate_apikey(_=Depends(auth)):
    return svc.rotate_key()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    return {"ok": True, "accounts": len(svc.load_accounts())}


@app.get("/v1/models")
def models(_=Depends(auth)):
    return {"object": "list", "data": svc.models}


class Message(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    model: str = "qwen2api"
    messages: list[Message] = Field(default_factory=list)
    stream: bool = False


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest, _=Depends(auth)):
    accounts = [a for a in svc.load_accounts()
                if a.get("enabled", True) and (a.get("token") or a.get("cookies"))]
    if not accounts:
        raise HTTPException(503, "账号池里没有可用会话——先在面板导入 通义千问 的账号")
    raise HTTPException(
        501,
        "通义千问 的对话链路还没接上。用 web2api/capture.py 抓一次真实请求，"
        "按录到的形状写 adapter 即可。")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "18616")))

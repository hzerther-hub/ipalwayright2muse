# services —— 各家网页版 AI 的 *2api 服务

一个仓库，多个服务。每个服务把某个 AI 网站的**网页版免费额度**转成
OpenAI 兼容 API，用来替代按量付费的官方 API。

```
services/
  deepseek2api/     DeepSeek 网页版聊天      —— 接口已确认，对话链路待接
  mistral2api/      Mistral Le Chat          —— 待抓包
  perplexity2api/   Perplexity              —— 待抓包
  grok2api/         Grok                    —— 待抓包
  qwen2api/         通义千问                —— 待抓包
  kimi2api/         Kimi                    —— 待抓包
  doubao2api/       豆包                     —— 待抓包
web2api/            共用框架（契约 / 抓包 / PoW / SSE 映射）
```

## 为什么走网页版

省 API 钱。各家的官方 API 都有价格，而网页版聊天本身免费或有免费额度。
代价是要处理会话、限流与风控——这正是 `web2api/` 要一次性解决的部分。

**注意**：这属于对目标服务的非官方调用，使用前请自行确认与目标服务条款
的相容性。

## 共用框架

| 模块 | 作用 |
|---|---|
| `web2api/contract.py` | 上游契约的 FastAPI mixin——实现它，面板就能接管这个服务（账号池、API Key、服务启停、出口保证） |
| `web2api/capture.py` | 内部 API 抓取——**接入新服务的关键工具** |
| `web2api/pow.py` | DeepSeek 的 proof-of-work 求解（跑官方的 wasm） |
| `web2api/sse.py` | 各家 SSE → OpenAI chunk 的映射 |

## 接入一个新服务（实测流程）

静态扒 JS 包往往不够——接口藏在懒加载的 chunk 里，或者被 Cloudflare 挡着。
所以统一走**真实流量捕获**：

```bash
# 1. 装一个已登录的浏览器会话的 cookie
export CAPTURE_PROXY=http://127.0.0.1:7890
export CAPTURE_DOMAIN=chat.deepseek.com

# 2. 抓一次真实交互
python -m web2api.capture --site https://chat.deepseek.com/ \
    --pattern "completion" --out capture_deepseek.json
#    然后在弹出的浏览器里发一条消息，脚本自动录下请求形状
```

捕获结果里会有：URL、方法、关键请求头（会话 token 会脱敏）、请求体、
以及流式响应的前几帧。有了这个形状，写适配器就是机械劳动。

### 各家的已知情况

| 服务 | 页面直取 | 已知内部接口 | 备注 |
|---|---|---|---|
| **Pavo** | ✅ | **73 条** `/v1/*`：`/v1/pixa/short-drama/*`（短剧/分镜/关键帧/角色/场景/道具）、`/v1/aigc/credits/*`（积分）、`/v1/chat/*`（对话）、`/v1/user/*`（登录/邀请码） | **API base 是 `https://api.pavo-ai.cn/api`**（有一层 `/api` 前缀，漏了会 000201）。模型目录与能力规格公开，无需登录；其余要 token |
| **DeepSeek** | ✅ | `/api/v0/chat/completion`、`/api/v0/chat/continue`、`/api/v0/chat/create_pow_challenge`、`/api/v0/auth/ticket` | 需 auth token；每请求前有 PoW |
| Mistral | ❌ 403 | 未知 | Cloudflare 级防护，必须真浏览器会话 |
| Perplexity | ❌ 403 | 未知 | 同上 |
| Grok | ❌ 403 | 未知 | 同上 |
| Qwen | ⚠️ | 未知 | JS 分包懒加载，需浏览器级抓包 |
| Kimi | ⚠️ | 未知 | 同上 |
| 豆包 | ⚠️ | 未知 | 同上 |

（403 与「未知」是 `tools/_probe_upstreams.py` 的实测结果，可复现。）

## 面板接管

每个服务实现了上游契约后，在面板的 `upstreams.json` 里登记即可：

```json
{
  "deepseek2api": {
    "base": "http://127.0.0.1:18612",
    "admin": "/admin",
    "key_env": "DEEPSEEK2API_KEY",
    "cwd": "../ipalwayright2muse/services/deepseek2api",
    "start": ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18612"]
  }
}
```

然后面板侧边栏的「上游」选择器就能切过去，出口保证、账号池、API Key、
服务启停全部可用。

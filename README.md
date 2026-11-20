# 静态出口 IP 面板

muse2api 的本地运维面板。一个**独立进程**的侧边栏页面，专盯一件事：

> **muse2api 到底是不是从你配置的那个静态住宅 IP 出去的？**

外加它依赖的配套能力：出口合规评分、节点目录、跳板状态、服务启停、
账号登录取 cookie、API Key 管理。

面板只监听 `127.0.0.1:18611`。

---

## 它和 muse2api 的关系

**面板是独立项目，不是 muse2api 的一部分。**

muse2api 是上游的开源项目（[czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api)），
负责把 muse.ai 网页端反代成 OpenAI 兼容 API。面板是它旁边的一套运维工具，
两者通过 HTTP + 共享的部署目录协作：

```
┌─────────────────────────────────────────────────────────┐
│  静态出口 IP 面板（本项目, :18611）                      │
│    独立进程 · 只监听 127.0.0.1 · 与 muse2api 互不依赖     │
└───────────┬──────────────────────────┬──────────────────┘
            │ HTTP /admin/*             │ 读写文件
            ▼                          ▼
┌────────────────────────┐   ┌──────────────────────────────┐
│  muse2api（上游, :18610）│   │  MUSE2API_DIR 指向的部署目录   │
│  app.py / engine.py     │   │   .env        (MUSE2API_KEY)  │
│  admin.html（管理页）    │   │   data/accounts.json（账号池）│
└───────────┬────────────┘   └──────────────────────────────┘
            │ 浏览器流量
            ▼
   链式中继 :17891 ──► 跳板代理 ──► 静态住宅 SOCKS5 ──► muse.ai
                                    ▲ 你配置的节点，出口国家自选
```

面板**必须在 muse2api 挂掉时仍然可用**——否则「重启 muse2api」这个功能
会把自己一起关掉。这也是它独立成进程、独立成项目的原因。

### 为什么面板里不能改 muse2api 的管理页

管理页 `admin.html` 会被「一键在线升级」里的 `git checkout -f <sha> -- .`
强制覆盖，任何改动都会在升级时丢失。面板因此完全不碰上游文件。

---

## 安装与运行

只需要 **Python 3.9+**，面板本身零第三方依赖（`lib/` 里 vendored 的
muse2api 基础件用到 `requests` / `websocket-client`，缺失时相关功能会明确报错）。

```powershell
git clone https://github.com/hzerther-hub/ipalwayright2muse.git
cd ipalwayright2muse

# 写一个 panel.env（见下节配置）
python server.py           # 前台
tools\m2a-panel.cmd start  # 后台 + 健康检查
tools\m2a-panel.cmd stop
tools\m2a-panel.cmd status
tools\m2a-panel.cmd smoke  # 冒烟测试
```

打开 <http://127.0.0.1:18611>。

---

## 配置

全部通过 `panel.env`（已被 .gitignore 排除）或同名环境变量：

| 键 | 默认 | 说明 |
|---|---|---|
| `MUSE2API_DIR` | 同级目录 `../muse2api` | 面板要管理的那套 muse2api 部署 |
| `REQUIRED_COUNTRY` | 空 | 出口必须落在哪个国家。**留空 = 任意 Muse 开放地区**；填了才强制那一个 |
| `PROXY_VENDOR` | `静态住宅代理` | 上游供应商名称，只用于文案与目录佐证 |

```ini
MUSE2API_DIR=E:\muse\muse2api
REQUIRED_COUNTRY=JP
PROXY_VENDOR=你的供应商名
```

### 目录布局的保证

面板与 muse2api **平级放置，各自的目录名等于各自的仓库名**：

```
E:\muse\
  ipalwayright2muse\   ← 面板（本仓库）
  muse2api\            ← muse2api 部署
```

这是默认的查找规则，而且**会被校验而不是被假设**——「服务控制」页的
「部署布局」卡片会逐项显示：是否平级、目标目录是否真的是 muse2api 部署、
`.env` 与账号池是否就位。布局不对时立刻能看出来，不会变成
「读不到账号池」这种难查的间接症状。

不在同一父目录时，用 `MUSE2API_DIR` 指过去即可。

---

## 链路工具

面板自带的链式中继（`tools/chain_proxy.py`）把「本地 HTTP 代理」转成
「跳板 → 静态住宅 SOCKS5」的完整链路。很多静态住宅供应商屏蔽境内直连，
必须经跳板接入：

```powershell
python tools\start_chain.py --socks-host <HOST> --socks-port <PORT> `
  --socks-user <USER> --socks-pass <PASS>
python tools\start_chain.py --stop
```

凭据写在 `data/chain.env`（已被 .gitignore 排除，绝不入库）。

---

## 六个面板

| 面板 | 内容 |
|---|---|
| **出口合规** | 实测当前出口 IP / 国家 / 城市 / ASN / 时区；按 Muse 开放地区判定；ipwho.is + proxycheck.io + ip-api + scamalytics + blocklist.de 五源风险评分；静态性校验；muse2api 安全性判定；支持输入任意 IP 手测 |
| **出口保证** | **硬保证**：muse2api 的浏览器流量必须从你配置的静态住宅节点出去。四步核验 + 常驻看门狗 + 不符即停用账号 |
| **账号登录** | 在面板里登录 muse.ai 并取回 cookie；多账号各自一套；直接导入账号池 |
| **选点目录** | 供应商静态住宅节点全表，标注 Muse 合规、价格、CIDR，按价格排序 |
| **跳板状态** | 跳板出口与链路三层健康（中继 → 跳板 → 上游节点），给出是否需要切换的建议 |
| **服务控制** | muse2api 与链式中继的启停、PID、端口、启动时间；部署布局校验；接入信息与 API Key 的读取/生成 |

---

## 出口保证：四步核验

按顺序核验，任何一步不过，结论就是「不满足条件」，而不是「大概可以」。

| 步骤 | 检查 |
|---|---|
| **第1步 · 跳板运行** | 跳板端口在监听、读得到跳板出口 IP、跳板出口非中国大陆 |
| **第2步 · 代理开关** | 系统代理已开启且指向跳板端口、链式中继在监听、上游节点经跳板可达 |
| **第3步 · 静态住宅 IP** | 国家符合要求（`REQUIRED_COUNTRY`，留空则任意 Muse 开放地区）、出口静态不轮换、出口等于配置的节点、未被标记代理/VPN/Tor、欺诈分 0、proxycheck 风险分 0、未命中黑名单、供应商目录确有该国静态住宅节点 |
| **第4步 · muse2api 可用** | 服务在跑、无头浏览器在跑、账号池非空、**浏览器实际出口等于目标节点**、账号会话校验通过 |

### 第4步为什么必须实测浏览器

muse2api 的生成流量出自 Chromium，而前面所有检查都在 requests/链路层面做。
实测证据：**浏览器不挂代理时会跟随系统代理，从另一个 IP 出去**——
此时 requests 层的检查全绿，但账号已经在用错误的 IP 对接 Muse。

面板因此直接连到 **muse2api 正在运行的那个浏览器**（CDP 端点
`MUSE2API_CDP_PORT`，默认 19210），在它里面开一个用完即弃的隔离上下文：

```
Target.createBrowserContext {
    "proxyServer": <账号代理中继>,
    "proxyBypassList": "<-loopback>",
}
```

这就是 muse2api 自己给每个账号建上下文的方式——**同一个浏览器进程、
同一套启动参数、同一个账号代理配置**。测完立刻
`Target.disposeBrowserContext`，账号正在使用的上下文完全不受影响
（引擎是通过 `self.page.js()` 在自己的页面里读附件，不枚举标签页）。

面板同时测那个浏览器的**默认上下文**作为对照，把「如果不挂代理会用哪个 IP」
直接摆出来。

若 muse2api 的浏览器没起来（CDP 端点不可达），自动回退到另起一个
同配置 Chromium 测量，页面上的「实测对象」会如实标出用的是哪种。

### 处置：停用账号，阻断生成

实测到浏览器出口与目标节点不符时，**立刻把该账号 `enabled=false`**，
muse2api 不会用它出图/对话，直到人工在面板或管理页恢复。

只有**实测出不同的 IP** 才会触发停用。浏览器起不来、页面读不到这类
「测不出来」的情况只是提示，不会把一个正常账号误杀。

### 常驻看门狗

面板进程内每 **3 分钟**自动复检一遍并记录历史，不打开页面也持续生效。
页面「看门狗」卡片显示最近 20 次结论与处置动作。

---

## 账号登录与 cookie

muse.ai 的关键会话 cookie 是 `hatch_sess` / `hatch_vml`，都带 **httpOnly**——
网页里的 `document.cookie` 读不到，必须走浏览器底层（CDP）。

面板的登录流程跑在一个 `--proxy-server=<链式中继>` 的 Chromium 里，
**登录请求从同一个出口发出**，因此 cookie 的登录 IP 与出图时的出口 IP 一致，
不会触发 Muse 的地区/风控策略。这是硬要求，不能省。

- 动作逻辑复用 `tools/muse_login.py`（React 受控输入的填法、提交按钮识别、
  页面状态分类）
- 登录流程由后台线程驱动，UI 轮询状态；验证码直接在面板里输入
- 一个账号一套 cookie，多个账号并行管理；值默认脱敏，可显示完整值/复制
- 「导入账号池」直接推送到 muse2api，不需要复制粘贴
- 密码只在本机内存中使用，不落盘、不写日志

---

## 「住宅 IP」是怎么判定的

公共 IP 质量源**不会**把静态住宅代理标成 `Residential`——它的 IP 段挂在
机房 ASN 下，ip-api 照样标 `hosting=true`，proxycheck.io 标 `Business`。
这是静态住宅代理的常态，面板如实展示这些标签而不假装公共源认了它。

「住宅」由两样东西认定：**供应商产品线**（静态住宅节点目录佐证）
加上**行为证据**（多次采样恒定、专属凭据、单一 IP）。
**风险**那侧则由权威源兜底（欺诈分、风险分、黑名单、代理/VPN/Tor 标记）。

---

## API

```
GET  /api/status                          服务总览
GET  /api/layout                          部署布局校验（平级 / 目录 / .env / 账号池）
GET  /api/exit?ip=<可选>                   单 IP 合规判定与风险评分
GET  /api/muse2api                        muse2api 安全性判定
GET  /api/guarantee                       看门狗最近一次结论 + 历史
POST /api/guarantee/check                 立刻复检四步（会实测浏览器出口，约 20 秒）
GET  /api/login/status                    登录会话状态（含各账号的 cookie）
GET  /api/login/pool                      muse2api 账号池总览
POST /api/login/start                     {email, password, label} 开始登录
POST /api/login/otp                       {code, session_id} 提交验证码
POST /api/login/import                    {session_id, label} 导入账号池
GET  /api/apikey                          读取当前 API Key 与接入地址
POST /api/apikey/rotate                   重新生成 API Key（旧 Key 立即失效）
GET  /api/catalog                         供应商节点目录
GET  /api/hop                             跳板与链路健康
POST /api/service/{muse2api|chain}/{start|stop|restart}
```

---

## 安全边界

- 面板只监听 `127.0.0.1`，不对外暴露
- 所有 `/api/` 无鉴权——但仅本机可达。**若将来要开放，必须先加 token**
- 上游代理凭据（`data/chain.env` 的 SOCKS 用户名/密码）永不回传前端，也不出现在日志里
- 登录密码只在内存中使用，不落盘、不写日志
- cookie 只在内存中，面板默认脱敏显示
- 服务控制是**有副作用**的操作（会启停进程）。这是明确要求的功能，
  按钮即用户动作，不再叠加确认弹窗

### 提交前凭据闸门

```powershell
python tools\_precommit_gate.py
```

扫描**暂存的 git blob**（不是工作区）里的凭据明文，命中即拒绝提交。
范围包括本仓库的 `data/`、`panel.env`，以及 `MUSE2API_DIR` 下
muse2api 那边的 `.env` / `chain.env` / 账号备注。

---

## 仓库结构

```
server.py            面板 HTTP 服务（stdlib http.server）
core.py              基础能力 + 配置 + 出口检测 + 服务控制
guarantee.py         出口保证四步核验 + 浏览器实流 + 看门狗
login.py             muse.ai 登录（多账号、各自一套 cookie）
static/index.html    前端（单文件，内联 CSS/JS，无构建步骤）
lib/
  cdp.py             Chrome DevTools Protocol 客户端（vendored 自 muse2api）
  proxy.py           账号代理中继（vendored 自 muse2api）
  browser.py         Chromium 探测与 CDP 端口
tools/
  muse_login.py      muse.ai 登录自动化
  chain_proxy.py     链式中继（本地 → 跳板 → 上游 SOCKS5）
  start_chain.py     链路启动器
  ipdeep_catalog.py  供应商节点目录抓取
  _panel_smoke.py    冒烟测试
  _precommit_gate.py 凭据闸门
  ...
data/                面板自身数据（chain.env 等，含明文凭据，已 gitignore）
panel.env            面板配置（已 gitignore）
```

---

## 支持这个项目

这个面板是**独立维护**的：它要持续跟进 muse.ai 的页面变化、代理供应商的
节点目录、IP 质量查询源的接口（这几个源都改过接口或换过域名）。

如果你用得上它，欢迎赞助 **$10** —— 一次性或按月都行：

- **GitHub Sponsors**：<https://github.com/sponsors/hzerther-hub>

赞助用于：

- 跟进 muse.ai / 供应商 / 各类 IP 查询接口的变动，保持功能可用
- 维护节点目录抓取与多源风险评分
- 响应 issue 与新地区的适配需求

不想赞助也完全没关系——项目会继续维护，MIT 协议，欢迎 fork 和提 PR。

---

## 已知限制

**跳板节点无法程序化切换。** 常见的 GUI 客户端（mihomo 内核）在运行的核心
实例上没有开 `external-controller`，节点配置存在加密存储里，GUI 的 IPC 端口
也不是 HTTP API。面板因此只做只读检测与建议，实际切换需在客户端手动操作。

**节点目录的 CIDR 覆盖不全。** 通常只有促销节点带 `IpSection`（CIDR），
常规价节点只有国家/城市码与价格。面板按此如实展示，不臆造 CIDR。

**公共源不认「住宅」。** 见上文判定口径。

---

## 相关链接

- muse2api（上游）：<https://github.com/czg86389-hub/muse2api>
- 静态住宅节点目录（IPDEEP）：<https://www.ipdeep.cn/static-residence>
- 静态代理优惠页（IPDEEP）：<https://www.ipdeep.cn/static-proxy-deals>
- muse.ai：<https://muse.ai/>

## License

MIT

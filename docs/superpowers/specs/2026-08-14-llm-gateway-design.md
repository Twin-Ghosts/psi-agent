# C 端免费模型转发器设计

| 项 | 值 |
|---|---|
| 状态 | 待评审 |
| 结论 | litellm 独立容器持 key 做 provider 路由；psi-cloud 加无状态 `modules/llm` 做鉴权与错误收口 |
| 影响面 | 云端 +1 容器 +1 模块，`core/` 扩一次契约；客户端只改 `base_url` |
| 事实来源 | 服务器实地 + 线上实测，见「实测基线」。**与本地既有文档冲突时以本节为准** |

## 结论先行

客户端默认免费模型改走 `https://account.genuineknowledge.cn/llm/v1`，由 psi-cloud
的 `modules/llm` 校验登录态后转发给同机 litellm 容器（容器网络内，不出网、不暴露
端口），litellm 只负责 provider 路由与持有上游 key。

`modules/llm` **无状态**：不建表、无 `llm.db`、无后台任务。本轮不做配额。

## W —— 问题与验收标准

### 现状

C 端默认免费模型**当前完全不可用**。线上实测：

```
POST https://misakamikoto.genuineknowledge.cn/chat/completions
→ 401 {"error":{"message":"Authentication Fails, Your api key: ****5c2d is invalid"}}
```

`****5c2d` 即 2026-08-10 废弃的那把 key。这台机器是个人云服务器，换 key 的人与用
key 的人不是同一个人 —— 这是根因，不是「忘了换」。

同一响应还暴露两个独立问题：上游错误信封原样透传，泄漏了 key 后四位；无任何可自助
验证的接口，本次换 key 靠群里来回确认三轮仍未确认成功。

### 验收标准

| # | 标准 | 判定方式 |
|---|---|---|
| A1 | 客户端开箱可用默认免费模型，用户不填 key | SPA v1/v2 新建会话直接对话成功 |
| A2 | 上游 key 只存在于公司服务器 litellm 容器 | 安装包与 psi-cloud 容器内均检索不到上游 key |
| A3 | 换 key 不发版 | 改 litellm 容器环境变量 + 重启，客户端零改动即生效 |
| A4 | 换 key 后可自助验证 | `GET /llm/v1/health/upstream` 一条 curl 返回上游是否接受当前 key |
| A5 | 未登录不可调用 | 无 Bearer / 无效 token → 401 |
| A6 | 上游错误不泄漏细节 | 响应体不含上游原文、不含 key 任何片段；上游 key 无效对外是 502 不是 401 |
| A7 | reasoning 与 usage 字段不丢 | 带工具调用的多轮对话在 reasoning model 下不报错 |
| A8 | 客户端断连时上游请求终止 | 手工验证：断连后上游连接关闭 |

### 不做

- **不做配额。** 本轮登录态即门槛，见「登录态的边界」。加配额是后续，届时加表不影响本轮任何对外接口。
- **不做 litellm 的 virtual key 与 UI。** 实测需要 Postgres，见「实测基线」。
- **不做 `deepseek-v4-pro` 转发。** 保持现状：pro 是用户自填 key 直连 DeepSeek 的高级配置，不走转发器。
- **不给 auth / analytics 补测试。** 已知缺口（见「测试」），本轮只在 `modules/llm` 立样板，不做无关范围扩张。
- **不动 psi-agent 的 AI 层与 Session 层。**
- **不迁移个人服务器上的其它服务。** 仅停用其 LLM 转发。

## H —— 方案与取舍

### 候选方案

| 方案 | 鉴权 | 配额主键 | 组件数 | 判定 |
|---|---|---|---|---|
| litellm 直接对外 | master_key（进安装包=公开）或 virtual key（需 Postgres） | litellm 自己的体系 | 最少 | 否决 |
| 先直接对外，后加闸门 | 同上，P1 再换 | 同上 | 少 | 否决 |
| **psi-cloud 闸门 + litellm 后端** | 复用 `auth.resolve_session` | `user_id`（在 auth.db） | +1 模块 | **采用** |

否决前两个的理由是三条独立事实叠加：

1. **对外鉴权无可用选项。** 裸 master_key 一旦进安装包就等于公开 —— 现有占位
   Bearer `haitun-default` 已经是这个状态。virtual key 实测需要 Postgres，为一个
   key 引一套数据库，而 psi-cloud 已在用 SQLite。
2. **配额的天然主键是 `user_id`，它在 auth.db 里。** 即便本轮不做配额，把闸门放在
   psi-cloud 才使后续加配额不需要动拓扑。litellm 不认识我们的用户。
3. **错误信封无处收口。** A6 要求不透传上游细节，litellm 直接对外就没有收口点 ——
   个人服务器现在就是原样透传泄漏了 key 后四位。

### 为什么 litellm 是独立容器而不是 psi-cloud 的依赖

`requirements.lock` 现在 7 个包。litellm 进来是几十个传递依赖，且自带配置体系、
自带 DB 迁移、自带鉴权模型，与 psi-cloud 的「manifest 自报 + 框架分库 + `PSI_`/
`AUTH_` 前缀分层」正面冲突 —— 等于一个框架里塞第二个框架。

独立容器则两个框架各自完整，边界是一次 HTTP 调用，升级 litellm 不动 psi-cloud 的
锁文件。

### 仓内既有惯例的对标

无直接友商对标（这是内部基建）。仓内对标是 psi-agent 的 AI 层 —— 它本身就是一个
provider 无关的转发器（`src/psi_agent/ai/__init__.py:47` 挂 `/chat/completions`，
`ai/server.py:28` 只用自己配置的 key、无视请求头 Authorization）。

**为什么不直接复用它当云端转发器：** 它只出 SSE（`ai/server.py:53` 固定
`text/event-stream`，无非流式分支），且强制 `stream_options.include_usage=True`、
在超阈值时注入 compaction 帧 —— 这些是给 Session 层用的专有语义，不是通用 OpenAI
兼容语义。当内部组件够用，当对外网关不够。

### 登录态的边界

登录态挡住匿名白刷，但挡不住单个登录用户无限调用。真正的节流是注册那道门 ——
注册要过短信/邮件验证码，那已经限频且花钱（`send_quota` 表）。风险从「任何人白刷」
降到「注册用户可无限用」，当前 4 个用户，可接受。

### 实测基线

均在 `root@account.genuineknowledge.cn` 实地确认。

| 项 | 实况 |
|---|---|
| 节点 | 新加坡 `8.222.255.23`，Ubuntu 24.04，Docker Compose v5.4.0 |
| 资源 | 内存 7.2G（用 0.8G），磁盘 40G（用 5G） |
| 现有容器 | `psi-cloud`，`127.0.0.1:8081->8000`，healthy，网络 `psi-cloud_default` |
| Caddy | v2.11.4，宿主机原生，只反代整个 vhost，`text/event-stream` 自动关缓冲 |
| 依赖 | `requirements.lock` 7 个包，httpx 已在内；dev 依赖含 pytest / respx |
| 容器出网 | 通 |
| auth 复用点 | `modules/auth/service.py:125` `resolve_session(ctx, token)`；`deps.py` 有 `require_bearer` / `client_ip` |
| 错误基类 | `core/errors.py` 已有 `ProviderFailure`(502) / `RateLimited`(429,带 `Retry-After`) / `Unauthorized`(401)，本模块**不需新建异常** |
| 测试现状 | **零测试**：无 `tests/`，git 全历史无测试文件，`AGENTS.md` 未提测试 |
| git | **无 remote，服务器上是唯一副本** |
| litellm virtual key | 官方文档明确「Database connection required」，需 Postgres |
| litellm master_key | 静态 `config.yaml` + `master_key` 即可，不需要 DB |
| DeepSeek 在册模型 | 实测 `/v1/models` = `['deepseek-v4-flash', 'deepseek-v4-pro']` |

### 开工前诊断核对（触发式要求）

W/H 的部分依据来自 `docs/onboarding/psi-agent C端注册登录方案.md`（数周前）。
逐条核对结果：

| 诊断文档 | 服务器实况 | 影响 |
|---|---|---|
| 阿里云内地节点，域名未备案，80 端口被按 Host 拦截，ACME 只能 tls-alpn-01 | **新加坡 8.222.255.23**，80 无拦截，Caddyfile 注释确认双通道均可用、已不再禁用 http-01 | 无影响，但说明该文档「已知限制」一节已过时 |
| 「新增业务模块不改 `docker-compose.yml`」 | 契约确实如此 | 本轮加**独立容器**必须改编排 —— 属另一类变更，不是规则失效。提交说明须写明 |
| 未提测试 | 零测试，dev 依赖已备但从未用过 | 本轮建 `tests/` 与首个样板 |
| 未提 model 名有效性 | 实测 `deepseek-v4-flash` 在册有效 | **推翻本设计初稿的怀疑**：litellm 官方 deepseek 文档在册名单已过时，不可作为 DeepSeek 现役型号依据 |

## 架构

```text
客户端 SPA（v1 + v2）
  base_url: https://account.genuineknowledge.cn/llm/v1
  api_key:  登录态 token
         │  HTTPS
         ▼
Caddy（宿主机，v2.11.4，本次不改 Caddyfile）
         │  reverse_proxy 127.0.0.1:8081
         ▼
容器 psi-cloud
  ├── modules/auth       /auth/*        auth.db
  ├── modules/analytics  /api/events    analytics.db
  └── modules/llm        /llm/v1/*      无库、无后台任务    ← 新增
        resolve_session → 流式转发 → 错误收口
         │  http://litellm:4000（容器网络内，不暴露端口）
         ▼
容器 litellm（新增）
  model_list: deepseek-v4-flash → deepseek/deepseek-v4-flash
  持 DEEPSEEK_API_KEY，无 DATABASE_URL
         ▼
    api.deepseek.com
```

`modules/llm` 是三个模块里唯一不建表的：`Module.schema` 留空，`build_context` 只装
httpx client 与配置。框架仍按模块名分配 `llm.db` 路径（`core/app.py` 的 `_module_db`
无条件建目录），该文件不会被创建，因为没有 SQL 碰它。

### 扩 `Module` 契约（唯一需要动 `core/` 的地方）

`modules/llm` 要调 auth 的 `resolve_session`，而当前 `Module`（`core/module.py`）没有
「依赖另一模块」这一项。直接 `from ..auth.service import ...` 是业务 import 业务。

服务器 `AGENTS.md` 硬规则给了做法：「如果你发现必须改，说明契约缺了一项，该扩契约
（`core/module.py` 的 `Module` 字段），而不是在框架里写业务名」。

**加 `Module.requires: Sequence[str] = ()`**，框架在 `build_context` 时注入依赖模块的
ctx。附带必须把 `registry.py` 的排序从 name 字典序改为拓扑序 —— 现在 `llm` 排在
`auth` 之后纯属字典序巧合，`build_context` 顺序不能靠巧合。环要有明确报错，参照
`_check_prefix_conflicts` 的既有风格（启动即拦，不留到运行期）。

## 接口

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| POST | `/llm/v1/chat/completions` | Bearer | 流式 + 非流式转发 |
| GET | `/llm/v1/models` | Bearer | 可用模型列表 |
| GET | `/llm/v1/health/upstream` | Bearer | 换 key 后自助验证（A4） |

`/models` 只反映 litellm 的 `model_list`，**不证明上游 key 有效** —— 这正是本次踩的
坑。故单独给 `/health/upstream`，用最小 token 发一次真实上游请求。

**该路由不进 `/healthz`。** `core/app.py` 的 `/healthz` 注释明确「只探各模块的库，
不碰任何供应商 —— 探针不该花钱」，而 compose healthcheck 是 30 秒一次。

`base_url` 末尾的 `/v1` 是必须的：AI 层往后拼 `/chat/completions`（无 `/v1` 前缀，
`ai/__init__.py:47`），拼出 `/llm/v1/chat/completions`，同时对任何 OpenAI 兼容客户端
成立。

不开 CORS（`cors_origins=()`）—— 调用方是本机 Gateway 进程，不是浏览器跨域。

## 数据流

两处必须原样透传，都是 psi-agent 特有的：

- **`reasoning_content`** —— Session 层要求 tool call 轮次把 `reasoning` 完整回传给
  API（`src/psi_agent/session/AGENTS.md:261`）。丢字段会让带工具调用的多轮对话在
  上游报错。对应 A7。
- **`stream_options.include_usage`** —— AI 层会强制设置（`ai/server.py`），转发器不能
  吃掉。对应 A7。

其余未知参数一律透传，与主仓「参数透传」约定一致（主仓 `AGENTS.md` 设计理念第 10 条）。

**model 字段的处置**（避免实现时二义）：

- 请求未带 `model` → 用 `LLM_DEFAULT_MODEL` 填充
- 请求的 `model` 在允许清单内（本轮只有 `deepseek-v4-flash`）→ 透传
- 请求的 `model` 不在清单内 → **422 `invalid_input`，不转发**

第三条不能靠「转给 litellm 让它报错」来实现：litellm 对未知模型返回的错误会被错误
映射规则翻成 502 `provider_failure`，而这其实是客户端错误。502 会让排查方向指向上游，
是本次故障里最消耗时间的那类误导。允许清单从配置读，不硬编码在代码里。

## 错误处理

上游错误一律映射为 psi-cloud 统一信封，上游原文只进服务端日志。全部复用
`core/errors.py` 现有异常，**不新建**：

| 上游情形 | 对外 | 异常类 |
|---|---|---|
| key 无效 / 余额不足 | 502 `provider_failure` | `ProviderFailure` |
| 上游限频 | 429 `rate_limited` + `Retry-After` | `RateLimited(retry_after=…)` |
| 上游超时 / 连不上 | 502 `provider_failure` | `ProviderFailure` |
| 登录态无效 | 401 `unauthorized` | `Unauthorized` |

**上游 key 无效对外必须是 502 而不是 401。** 401 会让 SPA 以为登录态过期去跳登录，
而真因是服务端配置坏了。区分这两者正是本次故障的核心：用户看到「模型不可用」，
真因是服务端 key 没换。对应 A6。

**流式的状态码边界。** SSE 发出首个 chunk 后 HTTP 状态码已定，之后无法改成 502。
分两种处置，实现时都要覆盖：

- 首 chunk 之前失败 → 正常 HTTP 错误响应
- 首 chunk 之后失败 → 流内发错误 chunk 后终止

**取消传导。** 客户端断连时上游 httpx 请求必须终止，否则上游继续生成并计费而无人
接收。与主仓「所有 run() 可被 cancel」理念一致（主仓 `AGENTS.md` 第 13 条）。对应 A8。

按硬规则 2（`service.py` 不 import fastapi），`service.py` 返回
`AsyncIterator[bytes]`，`StreamingResponse` 只出现在 `router.py`。不得在中间攒完整包
—— 攒了首字延迟就等于整轮时长。

## 配置

litellm 容器（`config.yaml`）：

```yaml
model_list:
  - model_name: deepseek-v4-flash        # 对外名，与客户端现值一致
    litellm_params:
      model: deepseek/deepseek-v4-flash  # 上游名，实测在册
      api_key: os.environ/DEEPSEEK_API_KEY
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

psi-cloud 侧新增环境变量，按现有分层（业务前缀归模块，与 `AUTH_*` 对等）：

| 变量 | 用途 |
|---|---|
| `LLM_UPSTREAM_BASE_URL` | litellm 地址（容器网络内） |
| `LLM_UPSTREAM_MASTER_KEY` | 调 litellm 用 |
| `LLM_DEFAULT_MODEL` | 请求未带 model 时的填充值 |
| `LLM_ALLOWED_MODELS` | 允许清单，逗号分隔。清单外返 422 |
| `LLM_REQUEST_TIMEOUT` | 上游超时 |

`DEEPSEEK_API_KEY` **只给 litellm 容器**，psi-cloud 不持有上游 key —— 即使
`modules/llm` 有漏洞也拿不到上游凭证。对应 A2。

## 客户端改动

只改 `base_url`，model 名不动（实测在册有效）：

| 文件 | 现值 | 改为 |
|---|---|---|
| `src/psi_agent/gateway/spa-v2/src/services/bootstrapAi.ts:12` | `https://misakamikoto.genuineknowledge.cn` | `https://account.genuineknowledge.cn/llm/v1` |
| `src/psi_agent/gateway/spa/src/bootstrapAi.js:8` | 同上 | 同上 |

v1 SPA 那份别漏，两份都在使用中。`bootstrapAi.test.ts` 断言了配置值，需同步。

### ⚠️ 「`api_key` 改为传登录态 token」这条没照做（实施时推翻）

本节原先写「`api_key` 字段改为传登录态 token」，并判断这会牵动 `isPlaceholderAi()`
（`bootstrapAi.ts:56`）、是客户端唯一有逻辑风险的改动点。**两条既有硬约束挡着，不能
照做：**

1. `spa-v2/src/services/api.ts:271` —— token 全程由 Gateway 持有并加密落盘，**前端
   拿不到也不该存**；`authFlow.ts:290` 更要求登录组件源码不出现 token 字面量（XSS）。
   SPA 里根本取不到 token 可填。
2. `gateway/_auth_store.py:10` 与 `gateway/__init__.py` —— `api_key` 是**明文写进快照**
   的，注释明确写着「登录凭证不再踩这个坑」。把 token 当 api_key 存就是重新踩。

**实际做法：** SPA 继续填哨兵 `haitun-default`，由 **Gateway 在拉起 AI 子进程时替换成
真 token**（`gateway/_free_model.py`）。替换条件是**哨兵 + 与认证服务同源**两条同时成立
—— token 只能发给签发它的那台主机，否则改一份快照就能把凭证送去任意域名。

于是 `isPlaceholderAi()` **语义不变、无需改动**（`api_key` 在前端看来始终是哨兵），
spec 预判的那个风险点不存在。token 只活在 `Ai` 实例里：不进 `state/latest.json`、不经
`/ais` 下发、不进 `AuthManager.status()`。

轮换靠 `AIManager.refresh_where()` 在登录/登出时**原地重建** socket（`AiInfo` 一个字段
都不变）。**不能指望 `_config_key` 自然生效** —— 进去重键的是哨兵，它看不见 token 变化。

详见 `gateway/AGENTS.md` 的「免费模型的 key 替换」一节。

未登录时的行为要定：`ensureDefaultAi()` 在无登录态时不应创建默认 AI，而应引导登录。

## 测试

psi-cloud 当前零测试（见实测基线）。本轮在 `modules/llm` 立样板并建 `tests/`，
不回填 auth / analytics —— 那是独立决定，不在本轮范围。

`modules/llm` 是三个模块里最好测的：无状态、无库、唯一外部依赖是一次 HTTP 调用，
`respx`（已在 dev 依赖）可完整 mock litellm，全部用例不花钱。

| 用例组 | 覆盖 | 对应标准 |
|---|---|---|
| 鉴权 | 无 Bearer → 401；无效 token → 401；有效 token → 放行 | A5 |
| 转发 | 请求体透传（含未知参数）；`reasoning_content` 不丢；`stream_options` 不被吃掉 | A7 |
| 错误映射 | 上游 401/429/超时 → 502/429/502；响应体不含上游原文 | A6 |
| 流式 | 首 chunk 前失败 → HTTP 错误；首 chunk 后失败 → 流内错误帧 | A6 |

**A8（取消传导）用手工验证兑现**，在交付文档 T 段贴证据 —— `respx` 模拟不了「上游
还在生成时客户端跑了」，硬凑自动化测试会得到一个不测真实行为的假用例。

不测 litellm 本身（它有自己的测试）；不做真实上游的自动化测试（花钱且不稳定，
`/health/upstream` 是手工用的）。

## 任务分解

| # | 任务 | 完成判定 |
|---|---|---|
| 1 | 扩 `Module.requires` + 拓扑排序 + 环检测 | 依赖 ctx 正确注入；构造顺序不依赖字典序；环有启动期报错 |
| 2 | litellm 容器进 compose，只监听容器网络 | 宿主机 curl 不到 litellm 端口；psi-cloud 容器内能调通 |
| 3 | `modules/llm` 骨架 + 鉴权 + 非流式转发 | A5；上游错误被重写为统一信封（A6） |
| 4 | 流式转发 + 取消传导 + reasoning 透传 | A7、A8；首字延迟接近上游裸调 |
| 5 | `/models` + `/health/upstream` | A4；`/healthz` 不含花钱探测 |
| 6 | `tests/` + `modules/llm` 用例 | 上表四组用例通过 |
| 7 | 客户端：两处 `base_url` + token 传递 + `isPlaceholderAi` 语义 | A1；SPA v1/v2 均可用 |
| 8 | 换 key 演练 | A3：改环境变量 + 重启，客户端零改动生效 |
| 9 | 停用个人服务器 LLM 转发 | 旧域名不再转发；其它用途已确认不受影响 |

## 风险

**截图里那把新 key 已泄露。** `sk-d56c...` 在飞书群以明文截图流转，本次调查也用它做过
验证。上线应另生成一把，不用这把。

**服务器上的 git 无 remote，是唯一副本。** 与本设计无关但优先级更高：一次误操作就丢
整个云端仓库。动手前先建远端备份。

**个人服务器域名的其它用途。** `scripts/dev-feishu.ps1:19` 也在用
`misakamikoto.genuineknowledge.cn` 作 BaseUrl。任务 9 停用前需确认该用途是否受影响
—— 那是开发脚本，可能指向的是完全不同的服务。

**litellm 版本未锁。** 独立容器要钉具体 tag，不用 `latest` —— 否则某次重建拉到不兼容
版本，故障点会落在一个没人改过的组件上。

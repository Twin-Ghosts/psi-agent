# C 端免费模型转发器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 C 端默认免费模型的上游 key 从个人云服务器迁到公司服务器，由 litellm 独立容器持有，psi-cloud 加无状态 `modules/llm` 做登录态鉴权与错误收口。

**Architecture:** 客户端 → Caddy → psi-cloud `modules/llm`（鉴权 + 错误收口）→ litellm 容器（持 key + provider 路由）→ api.deepseek.com。litellm 只监听容器网络，不对外。`modules/llm` 无状态：不建表、无后台任务。

**Tech Stack:** FastAPI + httpx + anyio（psi-cloud）；litellm proxy 容器；pytest + respx（测试）；TypeScript/Vue（客户端两版 SPA）。

**Spec:** `docs/superpowers/specs/2026-08-14-llm-gateway-design.md`

## Global Constraints

以下是 psi-cloud 仓库（服务器 `/srv/psi-cloud`）的既有硬规则，逐字来自其 `AGENTS.md`，每个任务都隐含适用：

- **`core/` 里不出现业务名字。** 出现了就是隔离破了。
- **`service.py` 不 import fastapi。** 状态码只在 `core/errors.py`。
- **SQL 只在 `repository.py`。** 别处出现 `execute(` 就是越层。（本模块无库，故不应出现任何 SQL）
- **`anyio`，不用 `asyncio` 原生 API；不用 `pathlib`。**
- **不留抑制。** 没有 `noqa`、没有 `type: ignore` 兜底。
- **失败响应不回报剩余次数，也不回报标识归谁。**
- **日志不打完整手机号、邮箱、验证码。** 本模块追加：**日志不打上游 key 任何片段、不打完整对话内容。**
- **自家限频跑在调供应商之前。** 顺序反了要花冤枉钱。
- Python >= 3.14；ruff line-length 88，lint select = `["E","F","I","UP","B","SIM"]`。
- psi-cloud 依赖只增不换：**不引入 litellm 作为 Python 依赖**（它是独立容器）。

psi-agent 主仓约束（客户端任务适用）：

- 异步用 `anyio`，禁止 `asyncio` 原生 API 与 `pathlib`。
- 零抑制：不堆 `noqa`，不设 `per-file-ignores`。

## 环境事实（实施时依赖，已实地核实）

- 服务器 `root@account.genuineknowledge.cn`，已配置免密，新加坡节点，Ubuntu 24.04。
- 仓库在 `/srv/psi-cloud`，**无 git remote，服务器上是唯一副本** —— 改动前先在服务器本地打 tag 备份。
- 现有容器 `psi-cloud`，`127.0.0.1:8081->8000`，compose 网络 `psi-cloud_default`。
- Caddy v2.11.4 宿主机原生，`/etc/caddy/Caddyfile` 是唯一权威副本，**本次不改它**。
- 生产编排 `docker-compose.yml`，开发编排 `docker-compose.dev.yml`（需显式 `-f`）。
- `.env` 整体注入容器（不逐条列举），`PSI_DATA_DIR` 与 `AUTH_CODE_HASH_SALT` 由编排固定。
- DeepSeek 在册模型实测：`['deepseek-v4-flash', 'deepseek-v4-pro']`。
- psi-cloud 当前**零测试**，无 `tests/` 目录，dev 依赖已声明 `pytest` / `pytest-asyncio` / `respx` / `ruff`。

## File Structure

服务器 `/srv/psi-cloud`（新增/修改）：

```
src/psi_cloud/core/module.py        改：Module.requires + ModuleRuntime.deps
src/psi_cloud/core/registry.py      改：字典序 → 拓扑序 + 环/缺失检测
src/psi_cloud/core/app.py           改：按拓扑序构造 ctx 并注入 deps
src/psi_cloud/modules/llm/
  __init__.py
  config.py                         LlmSettings（LLM_* 环境变量）
  deps.py                           LlmContext / get_llm_ctx
  service.py                        转发逻辑，不 import fastapi
  router.py                         3 个路由 + StreamingResponse
  manifest.py                       MODULE = Module(requires=("auth",), schema=())
tests/
  conftest.py
  test_registry.py                  拓扑序 / 环 / 缺失依赖
  modules/llm/test_auth.py
  modules/llm/test_forward.py
  modules/llm/test_errors.py
  modules/llm/test_stream.py
docker-compose.yml                  改：+ litellm service
litellm/config.yaml                 新建
.env                                改：+ LLM_* / DEEPSEEK_API_KEY / LITELLM_MASTER_KEY
```

本仓 `psi-agent`（新增/修改）：

```
src/psi_agent/gateway/spa-v2/src/services/bootstrapAi.ts       改：base_url + token
src/psi_agent/gateway/spa-v2/src/services/bootstrapAi.test.ts  改：同步断言
src/psi_agent/gateway/spa/src/bootstrapAi.js                   改：base_url + token
```

---

## Task 0：服务器改动前备份

**这一步不可跳过。** 服务器上的 git 无 remote，是唯一副本（spec「风险」节）。

- [ ] SSH 上服务器，确认工作区干净：`cd /srv/psi-cloud && git status --short`
- [ ] 打备份 tag：`git tag backup/before-llm-gateway-2026-08-14`
- [ ] 备份现有 `.env` 到 `/root/psi-cloud-env-backup-2026-08-14`（600 权限），确认可读回
- [ ] 记录当前容器状态与镜像 digest：`docker compose ps` + `docker compose images`，贴进交付文档

---

## Task 1：扩 `Module.requires` + 拓扑序 + 环检测

对应 spec 任务 1。这是唯一动 `core/` 的任务，先做因为 Task 3 依赖它。

**为什么必须改排序：** `create_app()` 顺着 `discover()` 返回的顺序调
`build_context`，而 `discover()` 返回 `sorted(key=name)`。`llm` 排在 `auth` 之后
纯属字典序巧合 —— 依赖注入不能靠巧合。

### Interfaces

```python
# core/module.py —— ModuleRuntime 加一个字段
@dataclass(frozen=True, slots=True)
class ModuleRuntime:
    name: str
    db: Database
    data_dir: str
    deps: Mapping[str, Any] = field(default_factory=dict)
    """本模块 requires 声明的模块 ctx，键是模块名。框架填，模块只读。"""

# core/module.py —— Module 加一个字段
    requires: Sequence[str] = ()
    """依赖的其它模块名。框架保证它们的 ctx 先构造好并从
    ModuleRuntime.deps 传入 —— 模块之间不直接 import。"""

# core/registry.py
def discover(disabled: frozenset[str] = frozenset()) -> list[Module]:
    """返回拓扑序模块列表：被依赖者在前，同层按 name 稳定排序。"""

def _topo_sort(modules: list[Module]) -> list[Module]:
    """按 requires 拓扑排序。缺失依赖 → ValueError；成环 → ValueError。
    同层用 name 排序，保证启动日志与路由顺序可复现。"""
```

### Steps

- [ ] 写 `tests/` 骨架：`tests/__init__.py`、`tests/conftest.py`（暂空）；`pyproject.toml` 的 `[tool.pytest.ini_options]` 加 `asyncio_mode = "auto"`、`testpaths = ["tests"]`
- [ ] 先写测试 `tests/test_registry.py`：直接测 `_topo_sort`，构造裸 `Module` 实例，不碰真实 modules 包 —— 四个用例：(a) `B.requires=("A",)` 且 name 逆序时 A 仍在前；(b) 无依赖时按 name 排序；(c) `requires` 指向不存在的模块 → `ValueError`，消息含缺失名与声明方；(d) A↔B 互相 requires → `ValueError`，消息含「环」与环上模块名
- [ ] 跑测试，确认失败（`_topo_sort` 不存在）
- [ ] 改 `core/module.py`：`ModuleRuntime` 加 `deps`，`Module` 加 `requires`；`Mapping` 从 `collections.abc` 导入
- [ ] 改 `core/registry.py`：加 `_topo_sort`，把 `return sorted(...)` 换成 `return _topo_sort(found)`；更新 `discover` docstring 与模块头注释（现在写的是「按 name 排序」）。禁用某模块导致依赖缺失时，报错要指明是 `disabled` 造成 —— 否则排查方向会跑到拼写错误上
- [ ] 跑测试，全绿
- [ ] 改 `core/app.py`：构造 ctx 的循环里，按 `module.requires` 从已构造的 `contexts` 取出依赖，作为 `deps=` 传给 `ModuleRuntime`；因为已是拓扑序，缺失即框架 bug，用 `raise RuntimeError` 而非 `.get()` 兜底
- [ ] `ruff check` + `ruff format --check`，跑全部测试
- [ ] 手工验证契约未破：`docker compose up -d --build` 后 `curl -s localhost:8081/healthz`，auth 与 analytics 仍 ok

---

## Task 2：litellm 独立容器进编排

对应 spec 任务 2。**必须钉具体 tag，不用 `latest`**（spec「风险」节）。

**注意这违反了「新增业务模块不改 `docker-compose.yml`」的契约字面** —— 但那条契约
说的是业务模块，本任务加的是独立容器，属另一类变更。提交说明须写明这点。

### Interfaces

```yaml
# docker-compose.yml 新增 service
  litellm:
    image: ghcr.io/berriai/litellm:v1.77.3-stable   # 实施时确认该 tag 存在，不用 latest
    restart: unless-stopped
    env_file: [.env]
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    command: ["--config", "/app/config.yaml", "--port", "4000"]
    # ** 不写 ports ** —— 只在 compose 网络内可达，宿主机与外网都碰不到
```

```yaml
# litellm/config.yaml
model_list:
  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-v4-flash
      api_key: os.environ/DEEPSEEK_API_KEY
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

### Steps

- [ ] 确认要用的镜像 tag 真实存在（`docker pull` 试拉），记下 digest
- [ ] `.env` 追加 `DEEPSEEK_API_KEY`（**新生成的 key，不用截图里那把泄露的**）与 `LITELLM_MASTER_KEY`（`openssl rand -hex 32` 生成）。文件权限确认 600
- [ ] 新建 `litellm/config.yaml`（内容同上）
- [ ] `docker-compose.yml` 加 litellm service；psi-cloud service 加 `depends_on: [litellm]`
- [ ] `docker compose up -d`，`docker compose logs litellm` 确认启动无 DB 报错
- [ ] **验证不对外**：宿主机 `curl -sS -m 3 localhost:4000/health` 必须失败（连接被拒）
- [ ] **验证容器内通**：`docker compose exec psi-cloud python -c` 用 httpx 调 `http://litellm:4000/v1/models`，带 `Authorization: Bearer $LITELLM_MASTER_KEY`，应返回含 `deepseek-v4-flash` 的列表
- [ ] **验证上游真的通**：容器内对 litellm 发一次 `max_tokens: 1` 的 `/v1/chat/completions`，确认 200。失败则先解决，不要带着坏 key 往下做
- [ ] 确认 `.env` 与 `litellm/config.yaml` 不含真实 key 之外的敏感值；`git status` 检查 `.env` 已被 ignore

---

## Task 3：`modules/llm` 骨架 + 鉴权 + 非流式转发

对应 spec 任务 3。验收 A5、A6（非流式部分）。

### Interfaces

```python
# modules/llm/config.py
@dataclass(frozen=True, slots=True)
class LlmSettings:
    upstream_base_url: str      # LLM_UPSTREAM_BASE_URL
    upstream_master_key: str    # LLM_UPSTREAM_MASTER_KEY
    default_model: str          # LLM_DEFAULT_MODEL
    allowed_models: tuple[str, ...]  # LLM_ALLOWED_MODELS，逗号分隔
    request_timeout: float      # LLM_REQUEST_TIMEOUT，默认 300

def llm_settings() -> LlmSettings: ...

# modules/llm/deps.py
class LlmContext:
    def __init__(self, *, settings: LlmSettings, client: httpx.AsyncClient,
                 auth: Any) -> None: ...
    # auth 是 auth 模块的 ctx，由 manifest 从 runtime.deps["auth"] 传入

def get_llm_ctx(request: Request) -> LlmContext: ...
Ctx = Annotated[LlmContext, Depends(get_llm_ctx)]

# modules/llm/service.py —— 不 import fastapi
async def authenticate(ctx: LlmContext, token: str) -> str:
    """token → user_id。委托 auth 模块的 resolve_session。"""

def resolve_model(ctx: LlmContext, payload: dict[str, Any]) -> dict[str, Any]:
    """填默认 model；清单外 raise InvalidInput（不转发）。返回新 payload。"""

async def complete(ctx: LlmContext, payload: dict[str, Any]) -> dict[str, Any]:
    """非流式转发。上游异常映射为 core.errors 里的既有异常。"""

def map_upstream_error(status: int, body: str, retry_after: str | None) -> DomainError:
    """上游状态码 → 统一信封。body 只进日志，不进返回值的 message。"""
```

### Steps

- [ ] 新建 `modules/llm/__init__.py`、`config.py`，`LlmSettings` 照 `modules/auth/config.py` 的读环境变量风格写（不引新配置库）
- [ ] 写测试 `tests/modules/llm/test_errors.py`：`map_upstream_error` 的四条映射（401→502 `provider_failure`、429→429 带 `retry_after`、超时→502、5xx→502），并断言返回的 `message` 不含传入 body 的任何子串
- [ ] 实现 `service.py` 的 `map_upstream_error`，跑测试转绿
- [ ] 写测试 `tests/modules/llm/test_forward.py`：用 `respx` mock `http://litellm:4000/v1/chat/completions`，断言 (a) 未知参数原样出现在上游请求体；(b) 响应里 `reasoning_content` 不丢；(c) 请求带 `stream_options` 时不被改写；(d) 无 `model` 时填 `LLM_DEFAULT_MODEL`；(e) 清单外 model → `InvalidInput`（422）且 **respx 未收到任何请求**
- [ ] 实现 `resolve_model` 与 `complete`，跑测试转绿
- [ ] 写测试 `tests/modules/llm/test_auth.py`：`authenticate` 在 token 无效时（mock 的 auth ctx 抛 `Unauthorized`）向外仍是 `Unauthorized`；有效时返回 user_id
- [ ] 实现 `authenticate`，跑测试转绿
- [ ] 写 `router.py`：`POST /chat/completions`，用 `BearerToken` 依赖（从 `...auth.deps` 复用不行 —— 那是业务 import 业务；把 `require_bearer` 的等价实现放在 `modules/llm/deps.py`，8 行，重复优于跨业务耦合）
- [ ] 写 `manifest.py`：`MODULE = Module(name="llm", prefix="/llm/v1", router=router, schema=(), requires=("auth",), build_context=build_context, cors_origins=())`；`build_context` 从 `runtime.deps["auth"]` 取 auth ctx，建 `httpx.AsyncClient(timeout=...)`
- [ ] 日志检查：确认 service 与 router 里没有任何一行会打出 token、master key、或完整对话内容
- [ ] `ruff check`，全部测试通过
- [ ] 部署验证：`docker compose up -d --build`，无 Bearer 调 `/llm/v1/chat/completions` → 401；用真实登录 token 调（非流式 `"stream": false`）→ 200 有内容

---

## Task 4：流式转发 + 取消传导

对应 spec 任务 4。验收 A7、A8，以及 A6 的流式分支。

**状态码边界是本任务的核心难点：** SSE 首个 chunk 发出后 HTTP 状态码已定。所以
上游连接必须在**产出第一个 chunk 之前**就建立并读到响应头 —— 那时还能返回 502。

### Interfaces

```python
# modules/llm/service.py
async def stream_completion(
    ctx: LlmContext, payload: dict[str, Any]
) -> AsyncIterator[bytes]:
    """流式转发。第一个 chunk 之前的失败以异常抛出（router 转 HTTP 错误）；
    之后的失败以 SSE 错误帧发出并终止。

    实现要点：httpx 的 stream 上下文必须在生成器体内打开并持有 ——
    客户端断连时生成器被 GC/aclose，上下文退出即关闭上游连接（A8）。
    不得在中间攒完整包：攒了首字延迟等于整轮时长。
    """

def error_frame(exc: DomainError) -> bytes:
    """流中失败时发的 SSE 帧：data: {"error": code, "message": msg}\\n\\n
    随后跟 data: [DONE]\\n\\n —— 客户端的 SSE 解析器不会因缺 DONE 而挂住。"""
```

### Steps

- [ ] 写测试 `tests/modules/llm/test_stream.py`：respx mock 流式响应，断言 (a) 上游 chunk 原样透传、顺序不变；(b) 上游返回 401 响应头（未出 chunk）时 `stream_completion` 抛 `ProviderFailure` —— 即错误发生在首个 `yield` 之前；(c) 上游发两个 chunk 后断开时，产出的最后一帧是错误帧且不含上游原文；(d) 首字不被攒：mock 慢速上游，第一个 chunk 到达即可从生成器取到，不等流结束
- [ ] 实现 `stream_completion` 与 `error_frame`，跑测试转绿
- [ ] `router.py`：`stream` 为真时返回 `StreamingResponse(media_type="text/event-stream")`；先 `await anext()` 拿到首个 chunk 再构造响应，这样首 chunk 前的异常还能走 `_domain_error_handler`（**这是 (b) 能兑现的前提，实现时别省**）
- [ ] `ruff check`，全部测试通过
- [ ] 部署验证 A7：用真实 token 发一次带 `stream: true` + `stream_options.include_usage` 的请求，确认末帧含 usage
- [ ] 部署验证 A8：`curl` 流式请求，中途 Ctrl-C；`docker compose logs litellm` 应显示该请求被取消/断开，而非继续跑完。把日志片段贴进交付文档 T 段
- [ ] 手工测首字延迟：对比容器内直调 litellm 与经 psi-cloud，首字延迟差应在几十毫秒量级

---

## Task 5：`/models` + `/health/upstream`

对应 spec 任务 5。验收 A4。

**`/health/upstream` 是本次故障的直接对策** —— 换 key 后能一条 curl 自证，不必靠
群里来回确认。**它不进 `/healthz`**：compose healthcheck 30 秒一次，探针不该花钱。

### Interfaces

```python
# modules/llm/service.py
async def list_models(ctx: LlmContext) -> dict[str, Any]:
    """转发 litellm 的 /v1/models，过滤到 allowed_models。
    ** 不证明上游 key 有效 ** —— 这正是本次踩的坑，故另有 check_upstream。"""

async def check_upstream(ctx: LlmContext) -> dict[str, Any]:
    """对上游发一次 max_tokens=1 的真实请求。返回
    {"ok": bool, "model": str, "detail": str} —— detail 是给运维看的原因
    摘要（如 "upstream rejected credentials"），不含 key 任何片段、不含上游原文。"""
```

### Steps

- [ ] 写测试：`list_models` 过滤掉清单外模型；`check_upstream` 在上游 401 时返回 `ok=False` 且 `detail` 不含上游 body 与 key 片段（**注意它返回结果而不抛异常** —— 诊断接口要能报告失败，不是自己 500）
- [ ] 实现两个函数，跑测试转绿
- [ ] `router.py` 加 `GET /models` 与 `GET /health/upstream`，都要 Bearer
- [ ] 确认 `core/app.py` 的 `/healthz` 未被改动（不含任何供应商探测）
- [ ] 部署验证 A4：`curl -H "Authorization: Bearer <token>" https://account.genuineknowledge.cn/llm/v1/health/upstream` → `{"ok": true, ...}`
- [ ] 反向验证：临时把 litellm 的 `DEEPSEEK_API_KEY` 改成无效值重启，同一条 curl 应返回 `ok: false`；恢复正确 key 并确认恢复 `ok: true`

---

## Task 6：测试门禁与文档同步

对应 spec 任务 6。用例本身已在 Task 3–5 里随功能写完（TDD），本任务只做收口。

- [ ] 核对 spec「测试」节四组用例全部存在且通过：鉴权(A5)、转发(A7)、错误映射(A6)、流式(A6)
- [ ] 服务器上跑全量：`python -m pytest tests/ -q` 与 `ruff check .`，两者都必须干净
- [ ] 更新 `/srv/psi-cloud/AGENTS.md`：补「测试」一节（怎么跑、mock 上游用 respx、不做真实上游自动化测试的原因）；`modules/llm` 加进模块清单；`Module.requires` 写进契约说明
- [ ] 确认 `AGENTS.md` 里没有把 spec 内容复制进来（三向同步：信息只归属一层）

---

## Task 7：客户端接入

对应 spec 任务 7。验收 A1。

### ⚠️ spec 修正（实施时发现，须同步回 spec）

spec「客户端改动」写「`api_key` 字段改为传登录态 token」。**这一条不能照做**，两条
既有硬约束挡着：

1. `spa-v2/src/services/api.ts:271` —— token 全程由 Gateway 持有并加密落盘，
   **前端拿不到也不该存**；`authFlow.ts:290` 更进一步要求登录组件源码不出现 token
   字面量，理由是 XSS。SPA 里根本取不到 token 可填。
2. `gateway/_auth_store.py:10` 与 `gateway/__init__.py:222` —— `api_key` 是**明文写进
   快照**的，注释明确写着「登录凭证不再踩这个坑」。把 token 当 api_key 存就是重新踩。

**改为：** SPA 继续填哨兵值，由 **Gateway 在拉起 AI 子进程时把哨兵替换成真实 token**。
好处是 token 不进快照、不进前端、AI 层与 Session 层零改动（符合 spec「不做」第 5 条）。
`_ai_manager._config_key` 把 api_key 计入去重键（`_ai_manager.py:131`），所以重新登录
后 token 变化会自然拉起新 socket、旧的退役 —— 轮换不需要额外机制。

### Interfaces

```ts
// spa-v2/src/services/bootstrapAi.ts
export const DEFAULT_REMOTE_AI = {
  provider: 'openai',
  model: 'deepseek-v4-flash',                              // 不变，实测在册
  base_url: 'https://account.genuineknowledge.cn/llm/v1',  // 改
  api_key: 'haitun-default',                               // 不变：哨兵，Gateway 替换
}
```

```python
# gateway/__init__.py —— 构造 AI socket 配置处（现 167 行附近）
# api_key 为哨兵且 base_url 指向云端时，用 AuthManager 持有的 token 替换。
# 未登录则不拉起该 socket ——  免费模型需要登录态。
```

### Steps

- [ ] 读 `gateway/__init__.py:150-270` 与 `_ai_manager.py:50-115`，确认替换点与未登录时的现有行为
- [ ] 写 Python 测试（`tests/psi_agent/gateway/` 下，跟随既有命名）：哨兵 + 云端 base_url + 已登录 → 传给 AI 的 api_key 是真实 token；未登录 → 不拉起/明确失败；用户自填 key → 原样透传不被替换
- [ ] 实现替换逻辑，跑测试转绿。**替换后的 token 不得写回快照** —— 加断言或测试守住
- [ ] 改 `spa-v2/src/services/bootstrapAi.ts:12` 的 `base_url`
- [ ] 同步 `spa-v2/src/services/bootstrapAi.test.ts` 断言
- [ ] 改 `spa/src/bootstrapAi.js:8` 的 `base_url`（v1 别漏，两份都在用）
- [ ] 复核 `isPlaceholderAi()`（`bootstrapAi.ts:56`）：api_key 仍是哨兵，语义**不变** —— 确认 `pickPreferredAi` 行为与改动前一致，不需要改。若跑测试发现不一致再处理
- [ ] 前端测试与 lint：按仓库既有命令跑 `spa-v2` 的单测
- [ ] 端到端验证 A1：已登录状态下 SPA v2 新建会话直接对话成功；SPA v1 同样验一次

---

## Task 8：换 key 演练

对应 spec 任务 8。验收 A3 —— 这是整个方案要解决的原始问题，必须实测一遍。

- [ ] `docker compose exec` 进 psi-cloud 容器，检索环境变量与文件系统，确认**拿不到上游 key**（A2）
- [ ] 检索客户端构建产物，确认不含上游 key（A2）
- [ ] 演练：把 litellm 的 `DEEPSEEK_API_KEY` 换成另一把有效 key（或同一把改一位再改回），`docker compose up -d litellm`
- [ ] 客户端不做任何改动，对话仍然成功 → A3 成立
- [ ] 用 `/llm/v1/health/upstream` 自证换后可用 → A4 与 A3 联动成立
- [ ] 把演练的命令序列写成 5 行以内的 runbook，放进交付文档 A 段（下次换 key 的人照抄即可）

---

## Task 9：停用个人服务器 LLM 转发

对应 spec 任务 9。**这一步涉及他人机器，且有未确认依赖，须先确认再动手。**

- [ ] 确认 `scripts/dev-feishu.ps1:19` 用 `misakamikoto.genuineknowledge.cn` 做 BaseUrl 时指向的是什么服务 —— 若与 LLM 转发无关则本任务不受其阻塞；若相关，先给它换地址
- [ ] 与个人服务器持有者确认该机器上是否还有其它在用服务，只停 LLM 转发，不动其它
- [ ] 停用后验证旧地址不再转发；确认线上客户端已全部指向新地址（旧版安装包本就已坏，不构成回退）
- [ ] 若确认阻塞或需他人操作，**不要强行推进** —— 在交付文档 A 段记下卡点与责任人，其余任务照常收尾

---

## 收尾

- [ ] 提交：本仓只提交 spec 与本 plan 及客户端代码改动，**不提交其它文档**
- [ ] 提交说明须写明：加独立容器改了 `docker-compose.yml`，属「独立容器」类变更，不违反「新增业务模块不改编排」的契约
- [ ] 服务器改动无 remote 可推，在服务器本地 commit 并打 tag `llm-gateway-2026-08-14`
- [ ] 把 spec 的「客户端改动」节按 Task 7 的修正更新（三向同步：spec ←→ 代码不能对不上）
- [ ] 交付文档补 A/T 两段：A 段只放路径与 commit，T 段贴 A1–A8 的验证证据（A8 与 A3 是手工证据）

## 已知开放项（归属他人，不阻塞本 plan 前 8 个任务）

1. 上线用哪把 key —— 截图里那把 `sk-d56c...` 在群里明文流转过且本次调查用它验证过，视为已泄露，应另生成
2. `scripts/dev-feishu.ps1:19` 是否受影响（Task 9 的前置）
3. `/srv/psi-cloud` 无 git remote、服务器上唯一副本 —— 优先级高于本方案，建议单独排

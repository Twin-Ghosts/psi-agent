# Desktop Fusion Memory 内嵌接入设计

> 状态：设计已确认，等待书面复核
>
> psi-agent 基线：`origin/main@f82ee9a32816e1bb140409fe15ea8396d6d6f421`
>
> Fusion Memory 参考实现：`codex/jsonl-sqlite-layer-20260903@69383bb4442f29f01380f100da6b5c5a2b55163c`

## 目标

只为 `agents/desktop` 增加本地长期记忆，使同一 workspace 的不同 session 能共享并召回历史信息，同时保证不同 workspace 互相隔离。

接入必须满足以下核心约束：

1. 不修改 psi-agent 原始 `history.jsonl` 的写入格式、内容或生命周期。
2. Fusion Memory 自己的 JSONL 只保存可追溯的原始 user 文本和最终 assistant 文本，以及 scope clear tombstone。
3. trigger、schedule、heartbeat、compacted、system、tool、reasoning、turn context、传输标记和未完成回合不得进入 Fusion Memory 原始记录。
4. Fusion Memory 内嵌在现有 desktop Session 进程，不启动 MCP server、sidecar、watcher、模型服务或其他额外进程。
5. 生产实现只落在 `agents/desktop`。不修改 `agents/feishu`，不修改 psi-agent 微内核。
6. 只移植 Fusion Memory 的生产必需代码，不引入其 tests、eval、benchmark、CLI、installer、MCP server、Postgres 或实验实现。
7. 不新增 Python 依赖。异步边界遵循 psi-agent 的 `anyio`/`aiohttp` 约定。

当前产品假设用户第一版不会切换 workspace，但实现仍以规范化 workspace 路径作为隔离边界，不能把 `session_id` 当成长期记忆边界。

## 非目标

- 不替换、压缩、清理或重新定义 psi-agent 的原始会话历史。
- 不做账号级、组织级、飞书身份级或跨设备同步。
- 不提供 Fusion Memory 安装、启动、doctor、health、token 分配或服务运维流程。
- 不接入时序图、事件边、事实关系、实体图、profiles、views、后台任务或调试审计系统。
- 不把 summary card、LLM 抽取结果或模型生成文本当作原始证据。
- 不在本次改动中增加 memory UI、workspace 迁移 UI 或数据管理页面。

## 方案选择

评估过三种接入方式：

| 方案 | 优点 | 代价 | 结论 |
|---|---|---|---|
| 远端 MCP 或本地 sidecar | 最大程度复用原产品边界 | 需要额外进程、认证、health/setup 和运维，违背本次约束 | 不采用 |
| 整包引入 `fusion-memory` | 上游功能最完整 | 带入三十余张表、CLI、服务层、实验索引和不需要的依赖面 | 不采用 |
| desktop 内置裁剪 runtime | 无额外进程，能按 psi-agent 生命周期精确过滤 history，范围可控 | 需要维护一个明确的生产子集 | 采用 |

采用第三种方案。参考提交中的 JSONL/SQLite 持久化语义会被移植并适配 psi-agent 约定，但不会整体 cherry-pick Fusion Memory 包。

## 总体架构

```text
AppData histories/{session_id}.jsonl
              |
              | 仅扫描与当前 workspace 对应的 session
              v
agents/desktop system_after_turn
              |
              | 过滤并配对原始 user + 最终 assistant
              v
<workspace>/.fusion-memory/evidence.jsonl   <- 权威、追加式、先 fsync
              |
              v
<workspace>/.fusion-memory/memory.sqlite3  <- 可重建索引、FTS5、WAL
              |
              +--> DashScope embedding/rerank（同一进程内 HTTP）
              +--> 可选 LLM 抽取（同一进程内 HTTP）
              |
              v
新 session 首轮自动召回 + 后续 memory skill/tool 召回
```

数据默认保存在用户 workspace 的 `.fusion-memory/` 下，而不是 agent 能力包目录：

- `evidence.jsonl`：原始证据权威层。
- `memory.sqlite3`：可丢弃、可重建索引层。
- `.gitignore`：忽略该目录的运行时数据，避免进入用户仓库。
- 迁移备份、损坏隔离文件与主文件相邻，文件名带时间或随机后缀。

workspace ID 由规范化后的绝对 workspace 路径生成 SHA-256。Windows 路径按平台语义规范大小写和分隔符。workspace 移动或重命名在第一版中视为新 scope，不自动合并旧记忆。

`FUSION_MEMORY_JOURNAL_PATH` 可以覆盖当前 workspace 的 journal 路径，主要用于测试和受控部署。无论物理路径是否覆盖，所有读写仍必须校验记录中的 workspace ID。

## 组件边界

生产模块放在 `agents/desktop/tools/_fusion_memory/`：

| 模块 | 职责 |
|---|---|
| `journal.py` | JSONL 追加、fsync、`span_id` 幂等与冲突检测、半行恢复、tombstone 重放、复制备份 |
| `store.py` | 5 个存储对象的 schema、WAL/FTS5、事务、迁移备份、损坏隔离、`rebuild_index()` |
| `embedding.py` | DashScope embedding/rerank 适配器和可选语言模型适配器；只处理模型调用，不持久化凭据 |
| `ingest.py` | history 定位、workspace/session 映射、原始消息过滤、回合配对、稳定 ID、checkpoint 和渐进补齐 |
| `retrieval.py` | FTS/dense 候选融合、rerank、证据预算、summary card 导航及 evidence pack 构造 |
| `runtime.py` | 按 workspace 延迟创建并缓存 runtime，串联 ingestion/retrieval，统一降级和日志边界 |

另有三个顶层 tool wrapper 供 psi-agent 自动发现：

- `memory_add.py`
- `memory_search.py`
- `memory_answer_context.py`

wrapper 只解析参数、取得 runtime context 并调用内部模块，不承载存储或检索算法。

SQLite、文件 fsync 等同步标准库操作通过 `anyio.to_thread.run_sync` 包住完整临界区；HTTP 使用仓库已有的 `aiohttp`。不使用 `asyncio` 原生 API，不创建 subprocess。

## 写入语义

### 原始 history 保持不变

psi-agent 继续按照原有逻辑写入 AppData `histories/{session_id}.jsonl`。Fusion Memory 不拦截 `Conversation.add()`/`commit()`，也不删除或修改任何 history 行。

`system_after_turn()` 只在普通 chat 的最终 assistant 已 commit 后触发。它根据当前 `session_id` 重新读取会话 history，并通过 checkpoint 增量处理新增内容。直接使用 hook 参数只能作为定位提示，不能绕过 history 中的最终提交事实。

### 合法证据

一次可写入的完整回合必须同时满足：

1. user 行的 wire role 为 `user`，message kind 为普通 `chat`，content 为非空字符串。
2. 后续存在同一回合的最终、可展示 assistant 行，wire role 为 `assistant`，message kind 为普通 `chat`，content 为非空字符串。
3. assistant 行不是仅 reasoning、仅 tool call、错误流、最大轮数占位或不完整流的结果。

每个完整回合产生两个 evidence span，分别保留 user 和 assistant 的可见原文。只移除 `[SEND:]`/`[RECV:]` 等传输层标记；不摘要、不改写、不截断、不做语义清洗。仅含传输标记而没有可见文本的 assistant 不能完成一个可写回合。

以下内容一律不写入 evidence journal：

- `system`、`tool`、`compacted` role 或 kind。
- trigger、schedule、heartbeat、channel event 和其他非普通 chat 回合。
- reasoning/thinking、tool call 参数、tool result。
- supervisor advice、turn context 和其他临时 hook context。
- context compaction 生成的 summary。
- summary card、memory item、实体/主题标签和任何模型输出。
- 只有 user、没有最终 assistant 的回合。

### 稳定 ID 与幂等

`span_id` 由 `workspace_id + session_id + history line number + role + content hash` 确定性生成。相同 `span_id` 和相同规范记录重复写入时为 no-op；相同 ID 对应不同内容时抛出冲突并停止该批索引更新，绝不能静默覆盖。

`turn_id` 由配对后的 user/assistant 行号和内容哈希确定性生成。它只表达来源配对，不引入新的合成文本。

写入顺序固定为：

1. 追加 JSONL 并按配置 fsync。
2. 在 SQLite 事务内写 `evidence_spans` 和 `fts_memory`。
3. 可选 LLM 抽取 `memory_items`。
4. 从 evidence/source IDs 确定性更新 `summary_cards` 导航字段。
5. 为 evidence 和 memory item 渐进补齐 embedding。

JSONL 失败时不得更新 SQLite。SQLite、LLM 或向量失败时，已经 fsync 的 JSONL 保留，聊天正常结束，索引可在下次调用重放恢复。

### `memory_add` 的边界

`memory_add` 不是任意文本写入通道。它只能把当前 workspace 中已经存在的 evidence span 提升为一个 durable `memory_item`，并要求有效的 `source_span_ids`。没有来源、来源跨 workspace 或来源不存在时拒绝。

因此 `memory_add` 不向 JSONL 追加模型改写文本，也不能制造无证据事实。用户当前回合本身会在最终回答完成后由自动 ingestion 写入；skill 不需要在同一回合重复保存原文。

promotion 本身是 SQLite 中的派生优化，不是新的权威事实。索引重建后，原始 source spans 和 FTS 命中必须仍然存在；promotion 的 kind/salience 可以由后续派生流程重新计算，不承诺逐字段还原。

## 最小存储设计

SQLite 只保留 4 张普通业务表和 1 张统一 FTS5 虚表：

| 对象 | 用途 | 关键内容 |
|---|---|---|
| `evidence_spans` | JSONL 的检索投影 | scope、session/turn/line provenance、speaker、原文、hash、时间、可选 embedding |
| `memory_items` | 合并原 facts/events 的轻量派生索引 | kind、文本、置信度、salience、source span IDs、可选 embedding、模型/schema 版本 |
| `summary_cards` | 检索导航 | 确定性检索键、原文短摘、source span IDs、更新时间；不能单独成为答案证据 |
| `ingest_checkpoints` | history 增量扫描与模型补齐进度 | history 路径、已确认行、prefix/hash 校验、embedding/extraction 水位 |
| `fts_memory` | 统一全文检索 | doc type、doc ID、workspace ID、可搜索文本 |

SQLite 为 FTS5 自动创建的 shadow 表不计入业务表数量。schema 测试使用业务表白名单，并显式允许这些 SQLite 内部表。

向量以 JSON 数组或紧凑 BLOB 存在对应 evidence/memory row 中，不增加独立向量表。SQLite 启用 WAL；数据库连接和迁移受 workspace runtime 锁保护。

明确删除或不移植：

- `fact_relations`、`event_edges`。
- 独立 `entities`。
- `current_views`、`entity_profiles`。
- `encoding_decisions`、`retrieval_utility_examples`。
- `debug_traces`、`audit_events`、`background_tasks`。
- 全部 `chronology_*` 表和时序图代码。
- 分立的 evidence/fact/event/profile FTS 表。

Fusion Memory 的 profiles/views 行为默认关闭，且本次根本不创建对应表或生产代码。desktop 现有的自适应学习画像是另一套功能，不在本次范围内。

JSONL 只包含 `evidence_span` 和 `scope_clear` 两类记录。SQLite 从 JSONL 重建时必须无损恢复原始 evidence 和 FTS；memory items、summary cards 和 embedding 属派生数据，可随后渐进重算。

### 旧完整 schema 的处理

本次不会沿用 Fusion Memory 原 SQLite 中的三十余张表，也不会在原库上逐张 `DROP TABLE`。若目标位置检测到旧完整 schema、未知业务表或不兼容 `user_version`：

1. 使用 SQLite backup API 保存完整旧库，并复制其相邻 JSONL。
2. 将旧库和 WAL/SHM 移出活动文件名，保留可恢复副本。
3. 创建只有本节 5 个存储对象的新数据库。
4. 若存在有效 JSONL，从 JSONL 重建 evidence/FTS，再渐进生成派生索引。
5. 若旧库没有权威 JSONL，不把旧 derived tables 反推成“原始文本”；保留备份并从空的新 journal/index 开始。

因此最小 schema 的取舍不会静默销毁旧库，同时也不会让旧实验表继续进入 desktop 生产路径。

## 既有 history 回填

第一次启用时，runtime 会进行本地回填：

1. 从 AppData `state/latest.json` 读取 session 到 workspace 的持久映射。
2. 只选择规范化 workspace 与当前 workspace 相同的 session histories。
3. 始终包含当前 session；同时扫描当前 workspace 下的 legacy `histories/`。
4. 无法证明 workspace 归属的 AppData history 不做猜测、不导入。
5. 先完成原始过滤、JSONL 和 SQLite/FTS 回填，再按每次调用预算渐进补向量和 LLM 派生项。

当前用户不切换 workspace 时，Gateway state 中的既有普通 sessions 都会落入同一 scope，因此能完成预期的一次性回填。若 checkpoint 指向的文件缩短、prefix hash 不匹配或原子重写导致签名变化，回退到幂等重扫，而不是相信旧 offset。

## 召回设计

### 新 session 首轮自动召回

`system_prompt_builder()` 是首轮自动召回入口，但不能假设它在一个 Session 内只调用一次：
desktop 的自适应画像会通过 `system_prompt_rebuild_checker()` 在后续回合触发 prompt 重建。
Fusion Memory runtime 因此按规范化后的 `(workspace_id, session_id)` 保存进程内的首轮召回消费状态；
只有第一次带非空普通 user 原文的 builder 调用会执行自动检索并返回注入块，后续重建只更新原有动态画像，
不得再次检索或注入记忆。该消费状态不写入 JSONL 或 SQLite，Session 进程重启后允许重新执行一次首轮召回。

首次符合条件的 builder 调用执行：

1. 用 `workspace_raw`/runtime context 确定 workspace scope。
2. 初始化或恢复该 workspace runtime。
3. 执行必要的 history 回填和索引重放。
4. 以首条 user 原文查询 FTS；有向量时融合 dense 候选；有 DashScope key 时可 rerank。
5. 把有明确来源的少量原始 evidence 注入首轮 prompt 的动态区。

注入块必须把召回内容标为“不可信历史数据”，禁止把其中的指令当系统指令执行。每条证据带 `span_id`、原 session 和时间信息；没有命中或 runtime 降级时不注入空说明。

### 后续按需召回

后续回合不自动重复注入。新增 `skills/fusion-memory/SKILL.md`，在用户询问过去的对话、偏好、长期事实、既有决定、历史计划或明确要求记住时加载。

- `memory_search`：返回当前 workspace 的原始 evidence 结果和 provenance。
- `memory_answer_context`：执行混合召回、导航扩展、rerank 和预算裁剪，输出可直接用于回答的 evidence pack。
- `memory_add`：只提升已有 evidence，不能创建无来源文本。

`memory_items` 和 `summary_cards` 可以扩展查询、聚类或导航，但 `memory_answer_context` 的最终证据包只能包含 `evidence_spans`。若导航项无法解析到仍存在的 source span，就丢弃该项。

summary card 仅保存确定性导航字段和 source span IDs，不保存回答，不写入 JSONL，不独立参与答案引用。

## Prompt 与 skill

现有 `FUSION_MEMORY_SECTION` 会从“远端 MCP/飞书身份/运维说明”改为简短的 desktop 本地记忆说明，并以实际存在的本地工具为启用条件，不再检查 `FUSION_MEMORY_MCP_URL`。

desktop tool 索引与顺序只保留三个本地工具。删除 desktop 中残留的 `organization_memory_add` 描述；组织级记忆仍不属于 desktop。

Fusion Memory skill 只描述：

- 何时搜索记忆。
- 何时使用 raw search 或 answer context。
- 如何依赖 provenance 回答。
- 如何处理“记住”请求和来源约束。
- 召回失败时如何回到当前对话，不假装记得。

skill 和 system prompt 均不得包含 setup、start、doctor、health、token 申请、服务地址配置或修改 `.env` 的指令。README/AGENTS 只记录开发者可审的运行时契约，不能要求最终用户自行部署 Fusion Memory。

## 模型与凭据契约

embedding 和 rerank 都直接调用 DashScope，并且只读取同一个 key：

```text
DASHSCOPE_API_KEY
```

不读取、不兼容以下旧变量或错误用途：

- `FUSION_MEMORY_EMBEDDING_API_KEY`
- `FUSION_MEMORY_RERANKER_API_KEY`
- 将 `FUSION_MEMORY_MODEL_API_KEY` 当作向量 key

默认模型：

- embedding：`text-embedding-v4`
- rerank：`qwen3-rerank`

模型名、endpoint、timeout、批大小可以通过不含秘密的 `FUSION_MEMORY_EMBEDDING_*` / `FUSION_MEMORY_RERANKER_*` 配置覆盖，但 key 来源不能覆盖。

`FUSION_MEMORY_MODEL_API_KEY` 只用于语言模型抽取/归纳。LLM 配置按以下优先级解析：

1. 使用显式 `FUSION_MEMORY_MODEL_*`；若只提供专用 key，可复用 agent 的 provider/model/base URL 元数据补齐非秘密字段。
2. 未提供专用 key 时，尝试整体复用进程环境中的 `PSI_AI_PROVIDER`、`PSI_AI_MODEL`、`PSI_AI_API_KEY`、`PSI_AI_BASE_URL`。
3. 两组都不完整或 provider 不受当前适配器支持时，关闭 LLM 派生，保留原始 evidence、SQLite 和 FTS。

“复用 agent 模型”只发生在配置层，不修改微内核 hook 签名，不借用私有 `AiClient` 实例，也不读取 Gateway `state/latest.json` 中保存的 key snapshot。

`.env.example` 只列变量名和用途，不放真实 key。凭据不得写入 JSONL、SQLite、checkpoint、备份、日志、异常正文或 tool 返回。

保留参考实现的 journal 开关：

- `FUSION_MEMORY_ENABLE_JOURNAL`：默认开启；关闭时整个 durable memory 写入/召回路径关闭，不能退化成 SQLite-only 写入。
- `FUSION_MEMORY_JOURNAL_PATH`：覆盖当前 workspace journal 路径。
- `FUSION_MEMORY_JOURNAL_FSYNC`：默认开启。

## 错误处理与恢复

所有 Fusion Memory 错误都不能让已成功的聊天回合失败。普通异常记录不含内容和凭据的 warning，并按以下规则降级：

| 故障 | 行为 |
|---|---|
| DashScope key 缺失/embedding 失败 | 保留 raw + FTS，记录待补向量水位 |
| rerank 失败 | 使用融合前分数和确定性排序 |
| LLM key 缺失/抽取失败 | 跳过 LLM memory item 抽取；确定性 summary card 仍可更新，raw + FTS 可用 |
| SQLite 写失败 | JSONL 已保留；下次启动重放 |
| SQLite 损坏 | 将原文件和 WAL/SHM 隔离为带后缀的损坏副本，从 JSONL 重建 |
| JSONL 尾部半行 | 保存半行副本，截到最后完整换行，再继续追加 |
| 完整但缺换行的尾记录 | 校验有效后补换行 |
| `span_id` 重复且内容相同 | 幂等 no-op |
| `span_id` 重复但内容不同 | 冲突告警，停止该批索引，绝不覆盖 |

schema 迁移前使用 SQLite backup API 备份数据库，并同时复制 JSONL。生产备份方法始终成对保存两者；不得只备份可重建 SQLite 而遗漏权威 journal。

`rebuild_index()` 创建新 SQLite、重放 evidence/tombstone，再原子替换可用索引。重建期间如果模型不可用，先恢复 raw/FTS，派生数据以后补齐。

## 测试与验收

### 定向测试

1. **写入过滤**：混合 history fixture 覆盖普通 chat、trigger、schedule、heartbeat、compacted、system、tool、reasoning、turn context、传输标记及不完整回合；断言 journal 只出现配对完成的原始可见 user/assistant 文本。
2. **原 history 不变**：ingestion 前后 history 文件逐字节一致。
3. **跨 session**：workspace A 的 session 1 写入后，session 2 首轮可召回；workspace B 同查询无结果。
4. **首次回填**：Gateway state 只选择当前 workspace histories，checkpoint 幂等；未知 workspace 归属的历史不导入。
5. **恢复**：半行、缺换行完整尾记录、重复 ID、内容冲突、SQLite 损坏、tombstone、迁移前双文件备份和 `rebuild_index()`。
6. **schema 白名单**：只存在 4 张业务表、`fts_memory` 及其 SQLite shadow 表。
7. **证据边界**：summary card 和无来源 memory item 永远不能进入 answer context。
8. **模型降级**：缺 key、HTTP timeout、429/5xx、非法响应时 raw/FTS 仍可读，聊天 hook 不向外抛普通异常。
9. **凭据解析**：embedding/rerank 只使用 `DASHSCOPE_API_KEY`；LLM 专用配置优先，`PSI_AI_*` 仅作完整 fallback；日志和持久化无 key。
10. **无额外进程**：对 runtime 路径封锁 `subprocess`，断言不调用 systemd、MCP server、watcher 或本地模型服务。
11. **prompt/skill 契约**：无 setup/start/doctor/health/token/`.env` 修改文案，无组织级或飞书身份说明，工具描述与真实 wrapper 一致。

模型测试全部使用进程内 mock HTTP server，不调用真实 DashScope，也不需要真实 key。

### 回归门槛

- 新增 desktop Fusion Memory 定向测试全部通过。
- desktop system/tool/skill、session hook 和 history 投影相关测试通过。
- `ruff check`、`ruff format --check`、`ty check` 通过本次涉及范围。
- desktop 打包导入 smoke 证明新增生产模块被包含，且没有新增依赖或外部进程入口。
- 完整 pytest 与相同机器上的 `origin/main` 对照，不接受新增失败。

本设计 worktree 的初始全量基线在共享文件系统上暴露了既有时序问题：`import psi_agent.cli` 冷启动约 27.5 秒，而部分 subprocess 集成测试只等待 socket 10 秒；中断时结果为 `69 passed, 13 failed`，失败均表现为嵌套 `uv run psi-agent` 未在 10 秒内创建 socket。该现象发生在任何功能改动前。实施验收必须使用同环境的 `origin/main` 对照，不能把这些基线时序失败误报为 Fusion Memory 回归，也不能因此放宽新增同进程定向测试。

## 预期改动范围

生产文件限定在：

- `agents/desktop/tools/_fusion_memory/`
- `agents/desktop/tools/memory_add.py`
- `agents/desktop/tools/memory_search.py`
- `agents/desktop/tools/memory_answer_context.py`
- `agents/desktop/skills/fusion-memory/SKILL.md`
- `agents/desktop/systems/system.py`
- `agents/desktop/systems/prompt_sections.py`
- `agents/desktop/.env.example`
- `agents/desktop/README.md`
- `agents/desktop/AGENTS.md`

仓库级 `tests/`、本设计文档和后续实施计划可以新增或修改。`src/psi_agent/` 与 `agents/feishu/` 不得有生产改动。

## 完成定义

当且仅当以下条件同时成立，接入才算完成：

1. 同一 workspace 的新 session 能自动召回旧 session 的原始证据。
2. 不同 workspace 之间没有写入、搜索或首轮注入泄漏。
3. Fusion Memory journal 中没有任何非原始会话文本或模型生成文本。
4. SQLite 删除后能从 JSONL 恢复 raw/FTS；模型恢复后能渐进补齐派生索引。
5. 模型服务不可用时聊天仍正常，FTS 仍可召回。
6. desktop 不需要 setup，不启动额外进程，不依赖完整 Fusion Memory 包。
7. 所有新增定向测试和静态检查通过，完整测试相对 `origin/main` 无新增失败。

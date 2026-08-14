# psi-agent 登录延迟优化

**描述：** 登录模块调用境外云端认证服务，每次点击等待 600–1000ms。通过连接复用把冷连接的两个 RTT 省掉，压到单 RTT。

**版本号：** 1.0

**状态：** 开发中

**适用范围：** psi-agent Gateway 认证链路

**关键词：** 登录、延迟、连接复用、keepalive、AuthManager

**创建人：** @待补

**审核人：** @待补

**关联SOP：**

- 《真知开发执行 SOP》v1.0 —— 本文档的四段结构依据
- 《真知开发规范 SOP》v1.0 —— 三向同步与信息归属

> **起草说明：** W / H 两段由 AI 依据 2026-08-14 与负责人的对话记录起草，选型判断（只做方案 A）由负责人口头确认，署名待补。

***

## W —— 是什么

### 1. 解决谁的什么痛点

**谁：** 所有使用手机号 / 邮箱登录 psi-agent 客户端的终端用户。

**什么损失：** 登录面板上每一次点击（获取验证码、提交验证码）都要等 600–1000ms。这不是服务端慢，服务端处理时间实测接近 0——延迟全部花在跟境外服务器重建连接上。

**为什么会这样：** `_auth_manager.py:143-146` 的 `_ensure_session()` 创建 `ClientSession` 时**没有传 connector**，因此走 aiohttp 默认的 `keepalive_timeout=15s`。而登录流程每一步的天然间隔都远超 15 秒：

| 用户动作 | 到下一步的间隔 |
|---|---|
| 打开登录面板 → 输完手机号点发码 | 5–20s |
| 点发码 → 短信到达并输完验证码 | 30–90s |

于是 `self._token` 那份 `ClientSession` 虽然被持有和复用，**连接池里的连接每次都已经被回收**——「复用」在代码层面成立，在网络层面一次都没成立过。

### 2. 实测数据（2026-08-14，本机 → `account.genuineknowledge.cn`）

域名解析到 `8.222.255.23`（阿里云海外节点），服务端 HTTP/2 + TLS 1.3。

| 阶段 | 耗时 | 说明 |
|---|---|---|
| RTT（ping ×4） | **226ms**（224/227 min/max） | 物理下限，改不动 |
| DNS 首次解析 | 200ms | 之后走系统缓存 ~5ms |
| TCP 握手 | 1 RTT ≈ 195ms | |
| TLS 握手 | 1 RTT ≈ 200ms | TLS 1.3，非 2 RTT |
| 请求本身 | 1 RTT ≈ 200ms | 服务端处理 ≈ 0 |
| **冷连接合计** | **600 / 764 / 998ms**（三次） | = 3 个 RTT |
| **热连接（复用）** | **205ms** | = 1 个 RTT |

结论：可省的是 TCP + TLS 两个 RTT，约 **420ms**，占总延迟三分之二。这也是这条路上全部能省的量。

服务端已通告 `alt-svc: h3=":443"`，HTTP/3 能进一步省握手 RTT，但 aiohttp 不支持 h3，这条路本期用不上。

### 3. 做完什么样算完（验收标准）

按 SOP 规则二，标准写在此处，T 段只做逐条核验。

| # | 标准 | 判定方式 |
|---|---|---|
| A1 | `_ensure_session()` 显式传入 `TCPConnector`，`keepalive_timeout` / `ttl_dns_cache` 为具名常量而非字面量 | 读码 + 单测断言 connector 配置 |
| A2 | 「发码 → 等待 ≥60s → 校验」全程复用同一条连接，不重新握手 | `aiohttp.TraceConfig` 的 `on_connection_reuseconn` 触发、`on_connection_create_start` 不触发 |
| A3 | `keepalive_timeout` 的取值有**实测依据**，不是估值 | 实测服务端空闲超时（方法见 spec），文档记录实测值与结论 |
| A4 | 登录面板挂载后、用户点「获取验证码」时走的是热连接 | 预热生效，该次请求耗时 ≈ 1 RTT |
| A5 | `send-code` / `verify` / `complete` / `bind` 四个 POST **任何情况下不自动重试** | 单测断言：注入连接错误后调用次数恒为 1 |
| A6 | 预热失败不影响登录可用性 | 单测：预热抛异常时 `send_code` 仍正常返回 |
| A7 | 现有认证测试全绿 | `uv run pytest tests/psi_agent/gateway/` |

### 4. 明确不做什么

- **不做国内边缘接入（原方案 B）**：阿里云 GA / DCDN 做 TLS 卸载可把 RTT 从 226ms 降到 ~40ms，但方案 A 落地后延迟已到 210ms，在「点下去感觉是即时的」约 300ms 阈值之下。再省的 160ms 用户基本感知不到，却要每月付费、多一个故障点、还要确认 `.cn` 域名备案要求。**花钱买感知不到的提升，不划算。**
- **不做服务迁国内（原方案 C）**：数周工作量 + 短信通道与合规，为登录延迟不值得。
- **不改 SPA**：预热挂在已有的 `/auth/status` 探测上（`api.ts:376`），前端一行不动。
- **不改认证语义**：不动错误码、不动两段式注册、不动 tempToken 不下发页面的约束。
- **不给 POST 加重试**：理由见 H 段。

***

## H —— 怎么做

### 4. 有哪几种做法，为什么选这个

三个候选，判断标准的优先级是：**效果可感知 > 工作量 > 不引入新故障点**。

| | 做法 | 工作量 | 每次点击延迟 | 取舍 |
|---|---|---|---|---|
| **A** | 客户端连接复用：显式 connector 调长 keepalive + DNS 缓存，`/auth/status` 被探测时预热云端连接 | ~40 行 + 测试，半天 | 600–1000ms → **~210ms** | 不碰云端、不碰 DNS、零部署风险。**选定** |
| B | 国内边缘接入（阿里云 GA / DCDN 做 TLS 卸载） | 配置在仓库外，~¥1000+/月，备案待确认 | → ~50ms | 效果最好，但 A 之后的增量感知不到 |
| C | 认证服务迁国内区部署 | 数周，含短信通道与合规 | → ~50ms | 为登录延迟不值得 |

**选 A 的理由：** A 不跟物理距离较劲，只是不再重复付握手的钱——这部分开销本来就是纯浪费。它把延迟压到 210ms 就触及了单 RTT 的地板，B 和 C 花的钱与风险买的是用户感知不到的 160ms。三条路里只有 A 的收益与成本比是明确划算的。

**B 不是被否决，是被推后。** 若将来出现「用户在更远地区（如南美、非洲）反馈登录慢」或「RTT 中位数涨到 400ms 以上」，B 重新进入考虑，那时需要先查清 GA 真实报价与 `.cn` 域名备案要求。

### 5. 别人怎么做的，我这样是否更好

**仓内既有惯例——A 其实是回归惯例，不是创新。** 仓内另两处 HTTP 客户端都显式传 connector：

- `src/psi_agent/channel/_core.py:45` —— `ClientSession(connector=connector, ...)`
- `src/psi_agent/session/ai_client.py:53` —— 同样显式传入
- `src/psi_agent/_sockets.py:55` —— 统一的 connector 构造入口

只有 `_auth_manager.py:145` 是全仓唯一不传 connector 的那处。**这是遗漏，不是有意的设计选择**——认证是仓内唯一走公网长距离的客户端，恰恰最需要调参，却唯独没调。

**业界惯例：** 连接池 + keepalive + DNS 缓存是 HTTP 客户端标准配置，requests 的 `HTTPAdapter`、Go 的 `http.Transport.IdleConnTimeout`、curl 的连接复用都是同一件事。aiohttp 默认 15 秒偏短是它面向短请求场景的取舍，长交互场景需要自己调。

**「POST 不重试」的依据：** 这是 HTTP 语义的常识（POST 非幂等），但本仓有一条更硬的具体理由——`authFlow.ts:238-249` 记录了一个已踩过的坑：D1 是兜底屏、文案一律是「验证码不正确」，任何后端异常都会被显示成用户抄错了码。若 verify 因连接陈旧失败后自动重试，可能导致验证码被消耗两次，用户看到的是「验证码不正确」而码完全正确。**加重试会把一个性能优化变成一个正确性缺陷。**

### 开工前的代码核对

SOP 的触发式要求针对「诊断写于数周前」的情况。本次诊断（代码阅读 + 链路实测）与文档撰写同为 2026-08-14 同一会话内完成，**该触发条件不适用**，无需核对。

但有一项**失败的测量必须留档，防止后人误用**：会话中曾用探针脚本 `scripts/_probe_keepalive.py` 试图测服务端空闲超时，结果自相矛盾（空闲 2s 判为已断开、10s 判为复用成功、30s 判为已断开、120s 又判为复用成功）。原因是该脚本用「耗时是否 < 400ms」反推连接状态，而 RTT 抖动（实测冷连接 600–998ms 波动）足以污染这个判断。**该数据无效，脚本已删除。** 正确方法是用 `TraceConfig` 直接观测连接事件，见 spec。因此 A3 被列为独立验收项。

另有一项**开工前已核实的选型**：connector 的 `enable_cleanup_closed` 经实测确认不能加（本仓 aiohttp 3.14.1 + Python 3.14.7 下它已废弃为 no-op 并触发 `DeprecationWarning`，而仓规禁止 `noqa` 压制）。结论与实测输出记于 spec 第一节。

***

## A —— 执行过程

按 SOP 载体分工，本段只记录实际路径、分支、commit 与中途变更的决策理由，代码细节归 spec，不复制。

- 技术 spec：`docs/superpowers/specs/2026-08-14-auth-connection-reuse-design.md`
- 实施计划：`docs/superpowers/plans/2026-08-14-auth-connection-reuse.md`
- 分支：`ci/oss-publish-via-pyinstaller`（沿用当前分支，未另开）

关键 commit：

| commit | 内容 |
| --- | --- |
| `e1b915b1` | 诊断与设计两份文档 |
| `3239894f` | 实施计划 |
| `16d720e5` | Task 1 连接池配置 |
| `7d04f1e4` | Task 2 重试边界 |
| `1304173b` | Task 3 预热能力 |
| `55e8bb0a` | Task 4 接线 |

### 中途变更的决策理由

**1. keepalive 保持 120s，未按「取下方一档」的原定规则调整。** 原计划是「第一次出现 create 的梯度即服务端空闲超时上界，取其下方一档」。实测整条梯度（10/30/60/90/120/180s）**全部复用，没出现过 create** —— 服务端超时比 180s 还长，梯度没测到上界。此时 120s 已稳在安全侧（客户端先于服务端回收），不需要改。也没有往上加，因为登录全程最大间隔约 90s，120s 已完整覆盖，再加只是让空闲连接多占资源。

**2. 重试只捕 `ServerDisconnectedError`，比 spec 初稿收窄。** 初稿写的是捕 `(ServerDisconnectedError, ClientOSError)`。写计划时核对 aiohttp 继承树发现前者本就是后者的子类，这个元组等价于只捕 `ClientOSError` —— 而 `ClientConnectorError`（DNS 失败、连接被拒）也是它的子类，于是真正连不通的情况也会被重试，白等一个超时周期。收窄后加了一条专门的测试守住这个边界。

**3. 顺带发现一个仓库级的测试陷阱（不属本任务范围，未修）。** `pyproject.toml` 的 `addopts` 里有个裸 `--cov`，它会把紧跟其后的第一个路径参数当成自己的值吞掉 —— `uv run pytest tests/xxx.py` 于是静默变成跑全量 1299 个测试。本任务早期报出的「基线 7 passed」就是这么来的（实际只跑了 `test_auth_store.py`）。真实基线按文件是 manager 6 / store 7。绕法是 `-o addopts="--strict-markers -ra"`，已写入实施计划。改 `pyproject.toml` 会影响所有人的本地跑法与 CI，超出本任务范围，未动。

***

## T —— 测试与验收

照 W 段第 3 节的 A1–A7 逐条核验，不自定新标准。全部通过。

| 项 | 结论 | 证据 |
| --- | --- | --- |
| A1 连接跨短信等待存活 | 通过 | `test_session_connector_keeps_connection_across_sms_wait` 断言 connector 的 `_keepalive_timeout` 为 120s，覆盖登录最大间隔（等短信 30-90s）；真机梯度探针 10/30/60/90/120/180s 全部 `reuse` |
| A2 幂等 GET 遇陈旧连接重试一次 | 通过 | `test_idempotent_get_retries_once_on_stale_connection` |
| A3 连不通的服务不重试 | 通过 | `test_unreachable_server_is_not_retried`（造 `ClientConnectorError`，断言只发一次） |
| A4 四个业务 POST 一次不重试 | 通过 | `test_business_post_never_retries`，参数化覆盖 send_code / verify / complete / bind |
| A5 预热不误伤凭证 | 通过 | `test_warm_401_does_not_clear_credentials`；预热走 `/me` 且不带 token |
| A6 预热节流 | 通过 | `test_nudge_warm_throttles_consecutive_calls`；真机 4 次连发 `/auth/status` 只触发 1 次预热 |
| A7 预热失败不拖垮 Gateway | 通过 | 真机断网启动：退出码 124（活到被 kill），日志 1 条 warning，`Traceback` / `ExceptionGroup` 计数均为 0 |

### 效果实测（2026-08-14，真机对生产端点）

| | 三次耗时 | 均值 |
| --- | --- | --- |
| 冷连接 | 740 / 992 / 711 ms | 814 ms |
| 热连接 | 208 / 232 / 213 ms | 218 ms |

单次请求省 **597ms**，比设计估的 420ms 更多 —— 冷连接除 TCP+TLS 两个 RTT 外还要付 TLS 证书链校验。热连接 218ms 与实测 RTT 226ms 基本相等，说明已经贴到物理下限（一个 RTT），客户端侧没有进一步可压的空间。

`/auth/status` 首次 218ms、之后约 2ms，预热本身不让这个端点变慢。

### 回归

- `test_auth_connection.py` + `test_auth_manager.py` + `test_auth_store.py`：25 passed
- `tests/psi_agent/gateway/`：184 passed, 2 skipped
- ruff：clean
- `ty`：维持仓库既有的 2 个错误（`examples/haitun-workspace/tools/run_flow.py` 的 `os.killpg`），本任务零新增

其中 A3（keepalive 取值须有实测依据）是本任务唯一带前置测量的验收项——若实测发现服务端空闲超时低于预设值，需回头修 spec 的常量取值并在 A 段记录该偏差。



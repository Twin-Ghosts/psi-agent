# 出向跨容器发文件：独立容器的文件让飞书侧可点击下载

**描述：** 修复独立容器（`psi-agent-luolin` / `psi-agent-chengxx`）里的 agent 生成文件后，飞书侧收不到可点击下载附件的问题。出向链路改为「session 供字节 → channel 上传」，不依赖共享文件系统。

**版本号：** 1.0

**状态：** 开发中（代码与本机验收 V4-V6 完成；V1-V3 需改生产，待用户确认后发布验收）

**适用范围：** psi-agent 出向文件链路（`[SEND:]` → 飞书附件），生产为新加坡节点 account.genuineknowledge.cn

**关键词：** 出向、跨容器、FileChunk、MediaSource buffer、external-sessions

**创建人：** @zsd

**审核人：** @待补

**关联文档：**

- `docs/superpowers/specs/2026-08-22-external-container-recovery-plan.md` —— 上一轮把出向列为观察项 V11 并「先不管」，本任务承接
- `docs/onboarding/真知开发执行SOP-v1.0.md` —— 本文档结构依据

***

## W —— 是什么

### 1. 解决谁的什么痛点

罗霖与成 xx 的飞书私聊被路由到各自的独立容器（`PSI_FEISHU_EXTERNAL_SESSIONS`）。这两人的
agent 生成文件后，**飞书侧收不到可点击下载的附件**。

2026-08-22 成 xx 实测：要一份可下载的 md，agent 回「文件已存在于工作区，直接发送即可」，
然后没有附件。生产日志同刻实证（`docker logs psi-agent-gateway`，本次复量仍在）：

```
[Lark] [2026-08-22 18:37:29,892] [WARNING] outbound: materialize blocked:
could not read local file '/workspace/真知问题解决与求助SOP（优化版）.md':
[Errno 2] No such file or directory
```

机制：飞书 WS 长连接同一 App 只允许一条，所以 channel 只能跑在 gateway 容器里；而出向
上传是**拿路径读本地文件**。三个容器各挂自己的宿主目录到 `/workspace`（生产
`docker inspect` 本次复核）：

| 容器 | 宿主目录 | 容器内 |
|---|---|---|
| `psi-agent-gateway` | `/srv/haitun/psi-agent/workspace` | `/workspace` |
| `psi-agent-luolin` | `/srv/haitun/psi-agent/workspace-luolin` | `/workspace` |
| `psi-agent-chengxx` | `/srv/haitun/psi-agent/workspace-chengxx` | `/workspace` |

独立容器的 agent 输出 `[SEND:/workspace/x.md]`，gateway 拿这个路径去读**自己的**
`/workspace` —— 那是另一个卷。同名不同物、多数情况直接不存在。

**受损面不止「少一个附件」。** 上传失败后 marker 不被消费，`[SEND:/workspace/...]`
原样当文本发给用户（8-22 11:12 成 xx 收到的就是这行裸标记）；agent 那边以为发成功了，
于是对用户说「已发送」。用户看到的是自相矛盾的两条消息。

这条链路对**本地** session 一直是好的，只在跨容器时坏 —— 上一轮修好了入向（用户发文件
给 agent），出向是同一堵墙的另一面，当时负责人定「先不管」，本任务承接。

### 2. 做完什么样算完（验收标准，可判定）

| 编号 | 验收标准 | 判定方式 |
|---|---|---|
| **V1** | 独立容器的 agent 生成一个**新名字**的文件并发送，飞书侧收到可点击下载的附件 | 真实飞书消息驱动，人眼确认附件可点击下载。**文件名必须是本次新造的** —— 生产上 `/workspace/真知问题解决与求助SOP（优化版）.md` 有硬链接（inode 701477，本次复核 link count = 2），用那个名字验会假绿 |
| **V2** | `psi-agent-luolin` 容器验过 | 同 V1 判法，独立记一次 |
| **V3** | `psi-agent-chengxx` 容器验过 | 同 V1 判法，独立记一次 |
| **V4** | 有覆盖出向跨容器路径的测试 | 新增测试在本机通过；**必须能在修复前失败** —— 提交前先 stash 掉产品代码跑一次，确认红 |
| **V5** | 本地 session 的出向发文件未被破坏 | 既有 `test_feishu.py` / `test__core.py` 相关用例仍通过，且本地路径仍走「直接交路径给 SDK」不产生额外 HTTP 请求 |
| **V6** | 私密区守卫仍然有效 | `_private_space.blocks_send` 的既有用例仍通过；新增路径不绕过它 |

V1-V3 靠真实飞书消息驱动（gateway 的飞书 WS 收到消息才会走到出向链路），无法用脚本代替。

### 3. 明确不做什么

- **不撤生产上两处临时改动。** 硬链接（inode 701477）与 build 机到生产的多余 ssh 公钥
  `haitun1-build-to-prod`，等本任务验收通过后另行撤除。硬链接尤其不能现在撤 —— 它是
  V1 假绿的来源，但撤它属另一件事，本任务只保证「不用那个文件名验证」。
- **不给容器挂共享卷。** 见 H 段候选 B 的取舍。
- **不改入向链路。** `_attachment_handoff`（`client.py:300`）已解决入向，本次不动。
- **不开 PR。** 用户统一提。
- **不做出向的通用「文件服务」抽象。** 只解决 `[SEND:]` 这一条出向路径；不引入
  下载链接、不做文件缓存、不做断点续传。

***

## H —— 怎么做

### 4. 有哪几种做法，为什么选这个

先定位到真正的那一层。出向链路（行号本次逐个核过，见下方核对结论）：

1. `channel/_core.py:88` 实例化 `SendMarkerScanner`，`:126` 喂流式 content
2. `channel/_markers.py:41` 的 scanner 扫出 `[SEND:/path]` → `_types.py:9` 的
   `FileChunk(path)`，只带一个 `path` 字段
3. `channel/feishu/client.py:490` 收到 `FileChunk`，过私密区守卫后调 `_send_file`
4. `client.py:193-200` 的 `_send_file`：`channel.send(chat_id, {"image": {"source": path}})`，
   失败再 `{"file": {"source": path}}`

**关键事实：读本地文件的不是我们的代码，是 SDK。** `{"source": <str>}` 经
`lark_channel/channel/_coerce.py:165-170` 判成 `MediaSource(kind="file", path=…)`，再由
`channel/outbound/media/uploader.py:148-165` 在 gateway 进程里 `Path(path).read_bytes()`。
失败即 `sender.py:430` 打出 `materialize blocked`。

而同一个 `_coerce.py:156-164`：**`bytes` 会被判成 `MediaSource(kind="buffer")`**，
`uploader.py:146-147` 直接用这段字节上传，不碰文件系统。生产镜像内的 SDK（1.2.0）
本次已逐条核过这三处都在。**这决定了修复的落点：让 channel 手里有字节，而不是让
gateway 能看见对方的文件系统。**

候选方案：

**A（选定）· session 开 `GET /files`，channel 取字节再上传。**
独立容器的 session 已经在 `http://0.0.0.0:8081` 上服务 `POST /chat/completions` 与
`POST /events`（`session/server.py:30-31`，容器内 `config.yml:19`）。加一个
`GET /files?path=…` 返回字节流；`FileChunk` 带上来源地址；`_send_file` 在有地址时
先取字节、把 `bytes` 交给 SDK。

- 优点：不动部署拓扑、不停机、不改 compose。复用已经打通且正在承载全部消息流量的
  同一条 HTTP 通道 —— 那条通道通不通，本身就是消息能不能到的前提，不新增故障模式。
  同名不同物的问题被彻底消除：字节来自哪个容器是显式的，不再靠路径碰运气。
- 优点：本地 session 完全不受影响 —— 没有来源地址时走原路径（直接交路径给 SDK），
  连一次多余的 HTTP 请求都不产生。
- 代价：session 多一个端点，多一份路径包含判定要写对。字节要过一次内存
  （飞书文件上限 30MB，可接受；设上限拦住误传大文件）。
- 代价：`FileChunk` 多一个字段。这是**信息补全**而非兜路 —— 一个「要传输的文件」
  在跨容器世界里，光有 path 本就不足以定位，缺的就是「在谁那儿」。

**B · 给三个容器挂一个共享卷。**

- 优点：零代码改动。
- 代价：改 compose + 重启全部三个容器（含连带重建 oauth-proxy），有停机。
- 致命代价：**没有真正解决问题。** 三个容器的 `/workspace` 各是各的根，
  `/workspace/x.md` 在两个容器里指不同文件；共享卷得挂在**另一个**路径下，于是 agent
  必须学会「要发的文件得先拷到共享目录」。等于把机制问题转嫁成 prompt 约定 —— agent
  忘了拷就静默失败，而这正是当前故障的形态。且它反向打穿了独立容器的文件系统隔离
  （那是这套部署存在的理由）。
- 结论：绕路，且换来的隔离损失比省下的代码多。否决。

**C · 独立容器自己上传到飞书，回传 `file_key`。**
两个独立容器确实有 `PSI_FEISHU_APP_ID` / `PSI_FEISHU_APP_SECRET`（本次核过），
技术上可行；`_coerce.py:168-169` 也认 `file_`/`img_` 前缀的 key，channel 侧几乎零改动。

- 优点：字节不过 gateway，省一跳。
- 代价：把「飞书」这个具体渠道塞进 session 层。session 现在对渠道一无所知 ——
  `FileChunk` 是渠道中立的，telegram 也在用（`channel/telegram/client.py:127`）。
  按 C 做，session 得知道自己的输出要发去飞书、得持有飞书凭据、得处理 image/file
  两种上传 —— 未来接第三个渠道要再来一遍。
- 代价：`file_key` 有效期与幂等语义要另行确认，多一个待验的未知。
- 结论：省的那一跳换来一层错位的耦合，不值。否决。

**判断标准的优先级**（按负责人既有取向）：① 不动生产拓扑、无停机优先；
② 结构上消除问题，而不是加约定绕开；③ 抽象要名副其实 —— `FileChunk` 加的是它
本就该有的信息，不是给它挂一个新职责。A 在三条上都占优。

### 5. 别人怎么做的，我这样是否更好

**仓内既有惯例（最强对标，且是同一堵墙的另一面）：** 入向已经这么解决过。
`client.py:300` 的 `_attachment_handoff` 的做法是「不在本容器下载，把**协议事实**
（`message_id`/`file_key`）交给对端容器，由它自取」。方向恰好互为镜像：入向是
「谁要用谁去取」，出向是「谁有谁来供」。两边共同的原则是**不假装两个容器共享文件系统**。
本方案与之同构，不新造第二套世界观。

**仓内既有惯例（端点形状）：** gateway 早有 `GET /workspace/file`
（`gateway/server.py:290` → `_workspace_manager.py:132-152`）：给路径、可选给 root，
`resolve()` 后判包含、越界抛 `PermissionError`。新端点照抄这套判定，不自创一套路径校验。

**业界：** 这是容器化 IM 机器人的常见形态 —— 收发进程与工作进程分离时，附件靠
对象存储或内部 HTTP 传字节，而非共享挂载（共享挂载会把隔离打穿，正是候选 B 的问题）。
我们没有对象存储，内部 HTTP 是同类做法里最轻的一档。

**友商：无直接对标及理由。** 「一个飞书 App 的 WS 单连接 + 每人一个独立容器」这个
拓扑是本项目为绕备案与换隔离而临时形成的（见记忆：部署拓扑是过渡态），公开产品里
找不到同形状的实现可比。故只对标业界通用手法与仓内惯例。

### 开工前核对诊断（触发式要求）

上游诊断由别的会话写于 8-22。本次开工前逐条核到代码与生产，**4 处不符**：

1. **「`source: path` 似乎是让 gateway 自己去打开该路径」—— 方向对，落点错。**
   不是我们的代码去 open，是 SDK 的 `kind="file"` coercion
   （`_coerce.py:165-170` → `uploader.py:148-165`）。差别是实质的：修复不该去改写路径，
   而该在调用点换 source 形态。**正因为看清这一层，才发现 SDK 本来就收 `bytes`
   （`_coerce.py:156-164`），修复代价从「挂卷/改拓扑」降到「几十行代码」。**

2. **「grep 出向日志零命中，可能说明压根没走到 `_send_file`」—— 判据无效，结论也不对。**
   `_send_file` 里那三行（`as image` / `image rejected` / `trying file`）全是
   `logger.debug`，而生产 72h 日志里 **DEBUG 行数为 0**（同期我们自己的 INFO 行 164 条，
   说明日志在正常输出，只是级别到不了 DEBUG）。用 DEBUG 关键词去证「没走到」，
   在生产日志级别下永远零命中。有效判据是 SDK 的 WARNING：`materialize blocked`
   —— 它 72h 内 **13 次命中**。**结论相反：确实走到了 `_send_file`，且失败在上传。**

3. **上游文档「`materialize`/`outbound` 这两个字符串在生产 `/app/src` 全树都不存在，
   那行日志来自 workspace 层的工具，不是 gateway」—— 错。**
   来源是 `lark_channel/channel/outbound/sender.py:430`，即 gateway 进程内的 SDK。
   「不在 `/app/src`」这个观察本身没错，但推论错了：SDK 不在 `/app/src` 底下。

4. **13 次 `materialize blocked` 里只有 4 次是本 bug。** 另外 9 次是
   `code=234011 Can't recognize image format` —— `_send_file` 先试 image 的探测性失败，
   之后 fallback 到 file 会成功，属正常噪声。**排查时不能把 13 当作故障次数**，
   否则会误判影响面。

核对**相符**的部分（不再复核）：上游给的 6 处 file:line 全部成立；三个容器的挂载与
`PSI_FEISHU_EXTERNAL_SESSIONS` 配置与上游描述一致；`_send_markers.py:29-41` 确实已有
空路径过滤，裸 `[SEND:]` 不会进到 `_send_file`（上游提示「先读」的那处注记已读，
guard 已在，无需补）。

生产侧本次新量到、上游没有的两条：三个容器同在 `psi-agent_default` 网络
（`172.19.0.2/3/4`），gateway 到两个独立容器的 `8081` **实测可达**（打空 body 得 HTTP 400，
即服务在、只是拒绝空请求）—— 方案 A 的前提成立。宿主的 `127.0.0.1:8081` 是 `psi-cloud`
占用，与独立容器的 8081 不冲突（后者未映射到宿主，仅 docker 网络内可达）。

***

## A —— 执行过程

技术方案即本文档 W/H 两段，落地步骤见 `docs/superpowers/plans/2026-08-24-outbound-cross-container-file.md`。本段只列落点与路径，不复述设计。

### 代码落点

| 层 | 位置 | 做了什么 |
|---|---|---|
| Session（新） | `src/psi_agent/session/file_serving.py:48` `resolve_within_root()` | 路径判定纯逻辑：限 workspace 根内、`resolve()` 后比前缀（挡 `..` 与符号链接）、体积上限 `MAX_FILE_BYTES`（`:28`，30MB）。**存在性检查排在包含性之后**，根外文件一律 403 不泄漏存在性 |
| Session | `src/psi_agent/session/server.py:18` `_make_files_handler()`、`:66` 注册 `GET /files` | HTTP 壳，`web.FileResponse` + `Content-Disposition: attachment`。与同端口 `POST /chat/completions` 同级无鉴权（端口只在 docker 网络内、未发布到宿主），理由写在函数 docstring |
| Session | `src/psi_agent/session/agent.py:240` `workspace_path` 只读 property | handler 需要根路径；原先只有私有 `_workspace_path` |
| Channel | `src/psi_agent/channel/_types.py:25` `FileChunk.source: str = ""` | 字节可从哪取；默认空值使入向侧所有构造点无需改动 |
| Channel | `src/psi_agent/channel/_core.py:37` `_byte_source`、`:141` 扫描循环里盖章 | `session_socket` 为 `http(s)://` 时填规范化前缀，否则留空。盖在 `post()` 而非 `SendMarkerScanner` 内：scanner 是纯解码 |
| Feishu | `src/psi_agent/channel/feishu/client.py:201` `_fetch_bytes()` | `GET {source}/files?path=...` 取字节；非 200 / 异常 / 空体 / 超限一律记日志返回 `None`，**不抛** |
| Feishu | 同上 `:234` `_send_file(channel, chat_id, path, source="")` | `source` 非空则改传 `bytes`（SDK 走 `kind="buffer"` 不碰文件系统）；走 file 分支时补 `file_name`；取字节失败回落原路径 |
| Feishu | 同上 `:552` 调用点 | 传 `chunk.source`。私有空间守卫（`_private_space.blocks_send`）位置不变，仍在其之前 |

### 三向同步

| 文件 | 补了什么 |
|---|---|
| `src/psi_agent/channel/AGENTS.md` | ChannelCore 段加 `_byte_source` 盖章条目（含「为什么不放 scanner」）；Feishu 约定段加两条——图片先试再降级会留常量级 `materialize blocked` WARNING（**勿把条数当故障数**）、`_fetch_bytes` 必须交 bytes 而非路径的根因 |
| `src/psi_agent/session/AGENTS.md` | 新增「GET /files——出向文件的字节来源」小节：为什么需要、五个关注点的落点表（纯逻辑分离、限根内、不泄漏存在性、体积上限、无鉴权是刻意的） |
| `src/psi_agent/gateway/AGENTS.md` | **未改**。核对后确认该文件从未记载 `PSI_FEISHU_EXTERNAL_SESSIONS`，无过期表述需要对齐；本次事实归属 session / channel 两层 |

### 本机质量门

- `ruff check src tests` → `All checks passed!`；`ruff format src tests` 已格式化。过程中修掉的都是自己新代码的问题：3 处 SIM117（嵌套 `async with`）、5 处 PLC0415（函数内 import，已提到文件顶部，`web` 一并补上）。**教训**：先前只对改动文件跑 lint 漏掉了这 8 条，全量 `src tests` 才暴露
- 测试见 T 段

***

## T —— 测试与验收

照 W 段 V1-V6 逐条核验，不新立标准。

| 项 | 结论 | 依据 |
|---|---|---|
| **V1** | **未验**（待发布） | 需要改生产（构建镜像 + 重启 gateway），按约定先问用户再动。发布后用一个本次新造的文件名验证 |
| **V2** | **未验**（待发布） | 同 V1，`psi-agent-luolin` 需独立记一次 |
| **V3** | **未验**（待发布） | 同 V1，`psi-agent-chengxx` 需独立记一次 |
| **V4** | **通过** | 见下「V4 明细」 |
| **V5** | **通过** | 见下「V5 明细」 |
| **V6** | **通过** | 见下「V6 明细」 |

### V4 明细：测试覆盖 + 修复前能失败

新增 23 条：`tests/psi_agent/session/test_file_serving.py` 12 条（根内放行；`..` 逃逸 403；符号链接逃逸 403（Windows 无权限时 skip）；root 为 None 403；空路径 400；不存在 404；目录 400；超限 413；常量等于 30MB；端点逐字节一致且 `Content-Disposition` 带中文名；端点逃逸 403 且不泄漏内容；端点缺文件 404）、`tests/psi_agent/channel/feishu/test_feishu.py` 7 条（无 source 时传路径且 `_fetch_bytes` 零调用；有 source 时上传 bytes；bytes 走 file 分支带 `file_name`；取字节失败回落路径；`_stream_reply` 把 `chunk.source` 作第 4 位置参传给 `_send_file`；对**真起的** session server 端到端取字节逐字节一致 + 根外返回 `None`；主机不可达返回 `None`）、`tests/psi_agent/channel/test__core.py` 4 条（TCP 填 `_byte_source` 含尾斜杠规范化；unix socket 与命名管道留空；`post()` 对 TCP 盖章、对本地留空）。

**修复前能失败已实测**：`git stash` 掉 5 个产品文件改动 + 临时移走新模块 `file_serving.py` 后跑同一批用例——

- feishu 7 条全红（7 failed, 79 passed）
- `_core` 4 条全红，且失败原因是 `AttributeError: 'FileChunk' object has no attribute 'source'` / `'ChannelCore' object has no attribute '_byte_source'`，即**冲着缺失的产品代码红**，不是撞 Windows 基线
- `test_file_serving.py` 12 条因 `ModuleNotFoundError: No module named 'psi_agent.session.file_serving'` 整个模块无法收集（比逐条断言更强的红）

恢复改动后同一批用例转绿。

**（实测坑）** 一次 pytest 调用里若有模块收集失败，整个 run 被 `Interrupted` 打断、其余文件不执行，需分文件跑才能看到各自的红。

### V5 明细：本地出向未被破坏

`test_file_serving.py` + `test_feishu.py` + `test__core.py` 三文件合跑：**17 failed, 103 passed, 1 skipped**。17 条全部在 `test__core.py`，已在未改动的 main 检出上复核为同样的 17 条红（`17 failed, 2 passed`），是 Windows 无 Unix socket 的既有基线，非本次回归（对齐根 AGENTS.md 与既往记录）。1 skipped 是符号链接用例在 Windows 无 `SeCreateSymbolicLinkPrivilege`。

「不产生额外 HTTP 请求」由 `test_send_file_without_source_passes_path_and_makes_no_request` 显式断言 `_fetch_bytes` 调用次数为 0，而非仅看结果相同。

**（实测坑）** 在 worktree 里跑必须给 `PYTHONPATH` 指向本 worktree 的 `src`，否则 `psi_agent` 解析到主检出 `F:\code\psi-agent\src\psi_agent`，新模块表现为「明明存在却 ModuleNotFoundError」。命令见 plan。

### V6 明细：私密区守卫

`_private_space` 相关既有用例随上述合跑通过。结构上守卫未被绕过：`client.py:552` 调用点的守卫判断在 `_send_file` **之前**、位置与判据均未改动；`_fetch_bytes` 只在 `_send_file` 内部、守卫放行之后才可能执行。`_private_space.owner_of` 用 realpath + parts 判定，对跨容器路径字符串同样成立（已读代码确认，未改）。

### 发布与生产验收（V1-V3）待办

按 plan「发布与验收」节执行，每一步改生产前先问用户：镜像构建 + 三层核验（第三层必须验镜像内产物，8-18 事故缺的就是这层）→ 先停旧再起新（飞书出向 WS 单连接，不做滚动更新）→ 连带重建 oauth-proxy（`network_mode: "service:gateway"`，不重建则显示 Up 但 8090 已死）→ 临时把自己的 open_id 加进 `PSI_FEISHU_EXTERNAL_SESSIONS` 自测两个容器（**新造文件名**）→ 撤回该临时 env。回退点：`psi-agent-gateway:backup-20260822` / `backup-20260822-174853`（同指 `896467e05f72`），当前生产 `527deff72043`。**不得** `docker compose down -v`（pgvector 数据在命名卷 `deploy_fusion_memory_pgdata`）。

***

## 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0 | 2026-08-24 | 初版，W/H 开工前落定；开工前核对出 4 处与 8-22 诊断不符 |
| 1.1 | 2026-08-24 | 补 A（代码落点 / 三向同步 / 本机质量门）与 T（V4-V6 通过并附实测明细，V1-V3 记未验待发布） |

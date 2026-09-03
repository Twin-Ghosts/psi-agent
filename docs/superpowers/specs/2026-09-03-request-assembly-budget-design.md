# 请求装配点预算化与前缀缓存稳定化 · 本批设计

> 姊妹文档：`docs/superpowers/specs/2026-09-03-tool-progressive-disclosure-design.md`（下批，工具按需暴露）。
> 分工：本份让「这个请求会有多大」有归属、并把前缀稳定下来；那一份削减工具数量本身。两批**不可合并**，理由见姊妹文档第一节。

## 结论

1. **这个仓库缺的不是十个功能，是「这个请求会有多大」这件事没有归属。** 现有四道防护（写入端截断、线上截断、压缩、`max_tool_rounds`）每一道只守局部量，没有一道知道总量。再加单点封顶还是同一个形状。
2. **压缩现在承担正确性，而不是优化。** 上下文撑爆的唯一出路是压缩，而压缩要靠一次 LLM 调用成功。该调用失败则历史不变、下一回合原样再爆，**死锁且重启无效**（超大历史在磁盘上）。2026-09-02 只能人工改历史文件救回。
3. **修法是把预算下沉到唯一的装配点**（`session/agent.py:688-708`），分两级：一级省略（确定性、必然成功、零成本）保正确性；二级压缩（可失败）只提质量。HTTP 400 死锁被**结构性消除** —— 不是补 `except` 分支，是超限的请求再也装配不出来。
4. **前缀缓存实测有效且自动**（deepseek-v4-flash，命中 99.7%）。真正的杠杆一直是**「别让前缀变」，不是「别发那么多」** —— 靠缓存把 285566 字符的工具 schema 变便宜，而不是变没有，零能力损失风险。
5. **两处对前期结论的修正**：M1 复活了 `AGENTS.md` 坑 19 已论证并删除的 boundary 方案（第三节）；`Conversation.save()` 每次 commit 全量重写整个历史文件，实测 **133 MB/min** 持续写入（第四节）。
6. **不做历史轮转。** 实测证伪了「整文件进内存」的担心（66MB 解析仅 0.48 秒）。真问题是写放大，轮转治不了它。

本批 6 件：件一A 预算、件一B 前缀稳定、件二A 写入准入、件二B append-only 落盘、件三 回合收敛、件四 压缩移出会话锁、件六 运维（无代码）。

---

## W —— 是什么

### 1. 生产现状（2026-09-03 实测）

生产机 `root@account.genuineknowledge.cn`，容器 `psi-agent-gateway`，镜像 `psi-agent-gateway:feishuweb-7c1b91c7`。挂载只有一条：`/srv/haitun/psi-agent/workspace` → `/workspace`。**内核代码在镜像内 `/app/src`，不是挂载。**

2026-09-02 为验证根因在生产做了 6 项临时改动，至今未回滚，备份在 `/root/tmpfix-20260902/`。逐项 md5 核实**全部仍在**：

| 项 | 落点 | 落在哪一层 |
|---|---|---|
| T0 | `workspace/.psi/appdata/histories/feishu-ou_d9545….jsonl` 尾部人工追加压缩行（10303 → 10502 行） | 挂载卷 |
| T2 | `/app/src/psi_agent/ai/server.py` 三处 `logger.debug` → `info` | 镜像层 |
| M1 | `workspace/systems/system.py` 画像块挪到缓存边界之后 + 打点日志 | 挂载卷 |
| M2 | `/app/src/psi_agent/session/agent.py` 硬编码白名单，工具 210 → 53 | 镜像层 |
| M4 | `workspace/.env:46` `PSI_MAX_CONTEXT_TOKENS=250000` | 挂载卷 |
| M5 | `/app/src/psi_agent/session/protocol.py:55` `DEFAULT_MAX_TOOL_ROUNDS` 20 → 50 | 镜像层 |

**三处纠正早期交接单：**

1. **M1 落在挂载卷**，不在镜像层，所以它和 T0/M4 一样扛得住容器重建；只有 T2/M2/M5 在重建时会丢。半状态比原估计轻。
2. **判据是「是否重建」而非「是否重启」。** 实测 `restarts=2`（02:52:55Z 与 07:44:34Z 各一次），而 `created` 仍是 2026-09-01T13:04:42Z —— 两次都是 restart 而非 recreate，6 项改动**一个没丢**。`docker restart` 不清写入层，只有 `docker compose up -d` 或换镜像才会。
3. **备份里 `system.py.pre-m1log` 与线上 md5 完全相同**（它是加完日志后的快照），真基线是 `system.py.orig`。拿 `pre-m1log` 回滚等于没回滚。

> **改生产容器文件只能 `docker cp` + `docker restart`，严禁 `docker compose up -d`。** `/app/src` 在镜像里不是挂载，`up -d` 会重建容器并静默丢掉所有改动。

其他现状：重启后日志内压缩 0 次、撞轮次上限 0 次、真 HTTP 400 0 次（早前 `grep -c "400"` 报的 78 次是假阳性，`400098` 这类数字被匹配了；用 `grep -iE "status_code: 400|Error code: 400|HTTP/1.1\" 400|400 Bad"` 复核为 0）。公共区有 **3** 个高博定时任务（`remind-checkin-gaobo-0950` / `remind-checkout-gaobo-1930` / `remind-lunch-gaobo-1155`），不是 2 个。

### 2. 前缀缓存实测（deepseek-v4-flash，2026-09-03）

方法：从生产机 curl 打 `https://api.deepseek.com/v1/chat/completions`，稳定系统提示 30379 字符 + 8 个工具定义，`max_tokens=8`，每次间隔 3 秒。只有出网 API 调用，未触碰任何生产文件。

| 探测 | prompt_tokens | cache_hit | cache_miss |
|---|---|---|---|
| R1 冷启（nonce1, 8 工具, 尾部A） | 19519 | 0 | 19519 |
| R2 原样重发 | 19519 | 19456 | 63 |
| R3 只改尾部 | 19521 | 19456 | 65 |
| R4 删掉 1 个工具（剩 7） | 19444 | 13568 | 5876 |
| R5 工具恢复 8 个 | 19519 | 19456 | 63 |
| R6 对照：换头部 nonce | 19519 | 0 | 19519 |

**结论：**

1. **前缀缓存真实存在、自动生效、无需 opt-in**，命中率可达 99.7%。`usage` 报 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`，以及 OpenAI 兼容的 `prompt_tokens_details.cached_tokens`，两者一致。
2. **改请求尾部几乎零代价**（R3 只多 miss 2 token）。
3. **改 `tools` 数组会打掉一大片缓存**：工具区仅占 body 的 0.7%，却让命中从 19456 掉到 13568，损失 5888 token。`tools` 参与缓存键已坐实。**但 `tools` 在序列化中的确切位置未测定** —— 13568 恰为 212×64（块对齐），单个数据点不足以定位。本文档不声称「`tools` 一定排在 `messages` 之前」。
4. **R6 是变异对照**，归零证明这把尺子吃劲。
5. **交叉验证**：30379 字符 → 19519 token，约 **1.56 字符/token**（中文密集）。生产系统提示 181218 字符按此折算约 117k token，与交接单的 117k 吻合。而 JSON 类工具结果（ASCII 键名）约 3.5-4 字符/token —— 同一仓库内比值跨度约 **2.6 倍**。
6. **区分两家上游**：deepseek 自动缓存（实测 R2 命中 99.7%，请求里没有任何 `cache_control`）。Anthropic 的 prompt caching 是 **opt-in** 的，而 `src/` 里 `cache_control` / `ephemeral` 出现 **0 次**（grep 可复核，仅 `session/system_prompt.py` 与 `session/AGENTS.md` 的注释各命中 1 次）。**本仓当前并未对 Anthropic 开启缓存**，所以对它不存在「击穿」，本批的收益是让前缀真正稳定、把开启缓存变成一个可行选项。

### 3. 验收标准

| 件 | 验收标准 |
|---|---|
| 件一A | 预算内载荷字节不变；超预算时按「最老最大优先」省略且**一次降到预算比例以下**（滞回）；省略后仍是合法 OpenAI wire 形状（`tool_calls` 与对应 `tool` 结果必须成对）；被省略内容可经句柄取回原文；估算偏差不超设定阈值 |
| 件一B | `profile/advice/policy` 不再出现在 system prompt；`HAITUN_CACHE_BOUNDARY` 从代码中消失；连续多回合 `prompt_cache_hit_tokens / prompt_tokens` 不低于阈值；`tools` 数组在会话内字节不变 |
| 件二A | 触发器无变更时历史行数不变；**且**有变更时该行照常写入（反向用例必须有） |
| 件二B | 一次 commit 的写入字节数与新增内容成正比，而非与文件总大小成正比 |
| 件三 | 同一工具搜不到时第 N 次后不再发出该调用，且模型收到**显式说明**而非静默截断 |
| 件四 | 压缩期间会话锁不被持有；并发安全性独立评估 |
| 件六 | 3 个定时任务移入个人区后仍能按时触发（人工确认） |

**每条判据都必须做变异复核**：故意改坏实现，断言用例真的失败。绿而不吃劲的用例在这个仓库出过事 —— 一个分支撞四次兜底分支，提前吃掉了结论。

---

## H —— 怎么做

### 1. 核心：装配点拥有预算（件一A）

`session/agent.py:688-708` 是**唯一**的请求装配点：`messages_for_ai()` 摊平历史（`history_display.py:186`），再与 `tool_defs` 装进 `request_body`。所有成本从这两行流过。

`messages_for_ai(history)` 升级为 `build_request(history, tools, budget)`，返回载荷与估算大小，并**保证载荷不超预算**。

这**不是新造抽象**，是把已存在的 `max_context_tokens` 从错误的层搬到正确的层：今天它由 AI 层观测（`ai/server.py:277-281`）、Session 层事后反应（`agent.py:981` `_maybe_compact`），改后由装配点当场执行。且 `messages_for_ai` 现已名不副实 —— 自称投影，实际在做 `truncate_tool_result` 与丢弃 system 到 compacted 之间的全部行，改名后名实相符。

**两级成本控制：**

- **一级 省略**：丢最老最大的行、留句柄。确定性、必然成功、纯本地零成本。
- **二级 压缩**：LLM 总结保语义。尽力而为、可失败、18 秒级且占会话锁。

一级保正确性，二级只提质量。**压缩失败不再意味死锁，只意味这次省略糙一点。**

**估算方式**：用字符数做预算，比值由上游自己的 `prompt_tokens` 逐回合校准（每次成功回合同时拿到发出字符数与 `usage.prompt_tokens`，比值存下给下一回合用），首回合用保守默认。不引分词器、不引依赖。上面第 2 节第 5 条实测的 2.6 倍跨度正是**不能写死单一系数**的理由。

### 2. 省略必须滞回（件一A 的关键约束）

历史是**追加写**的，天然缓存友好：每回合只在尾部长，前缀原样，所以 R3 那样近乎全命中。但**任何收缩都改动前缀** —— 压缩要把摘要并进 system（`history_display.py:186` 之后的合并逻辑），省略要丢老行，两者都动前面 → 全量 miss。

若为了「刚好卡进预算」而每回合丢一行，就等于**每回合全 miss，比不省更贵**。

所以收缩必须**一次降到预算的某个比例以下**（例如 50%），让后续多个回合都能在同一份缓存前缀上生长。说人话：**别每天扔一件行李，要扔就一次腾出半个箱子。**

既有的 `COMPACTION_COOLDOWN_FRACTION = 0.1`（`agent.py:62`，配合 `_tokens_at_last_compaction`（281）与 `_compaction_cooldown_elapsed`（1038））是同一个直觉，但它作用在压缩上、且以上游 token 为尺，应统一到装配点。

### 3. 易变内容归位（件一B）—— 修正 M1

**这一节修正前期方案。** 早前判断是「M1 做对了一半，应改为经 `turn_context` 移到尾部」。核实后实情不同：

`AGENTS.md` 坑 19 已经把这套设计连同完整理由写下了，**并且正确机制已经在跑**：

- `turn_context_builder` 在 `agents/feishu/systems/system.py:1586`，docstring 明确写着 delivered at the **tail**，「so the prompt and every earlier turn stay byte-identical」。
- 内核侧：`history_display.py:51` `TURN_CONTEXT_KEY`，`:53` 归入 `_DISPLAY_ONLY_KEYS`（**不写回 history 行**，所以历史行逐字节稳定），只在 `_project_for_ai`（`:259`）投影时折进 `content`。
- 坑 19 还明确记着：`stable_prefix + 边界 + dynamic_suffix` 那套边界常量**已随此设计一并删除**，因为它「并不解决这件事：省下的只是重扫开销，前缀照样每回合变」。

而 `agents/feishu/systems/system.py:1552-1563` 里，`profile_text + advice_text + policy_text` 被**拼进 system prompt**，插在 `HAITUN_CACHE_BOUNDARY` 之后：

```python
injected = "\n".join(part for part in (profile_text, advice_text, policy_text) if part)
if injected:
    boundary = "<!-- HAITUN_CACHE_BOUNDARY -->"
    if boundary in prompt:
        index = prompt.find(boundary) + len(boundary)
        final = prompt[:index] + "\n" + injected + "\n" + prompt[index:]
```

**M1 复活了一个仓库已经论证过并删掉的方案。** 而且它在结构上无效：system 是 `message[0]`，易变块在它内部再靠后，仍然排在**全部历史之前** → 后面所有历史的缓存都被打掉。实测数字：稳定前缀 181218 字符，边界后变动 880 字符。

**件一B 的改法**（已确认范围）：把 `profile/advice/policy` 从 `system_prompt_builder` 的拼接搬到 `build_turn_context()`（`system.py:1298`），并**删掉 M1 引入的 boundary**。范围比原计划小得多，且判据现成（缓存命中率）。

注意 `system.py:1567-1581` 那套 `budget.add` / `budget.log` 记账是为拼接路径设计的（两条 splice 路径的换行数不同，`spliced_at_boundary` 分支就是为了不留 1 字符残差）。搬迁后这段记账要跟着走，否则 `budget.log(actual=final)` 的残差校验会失去意义。

### 4. tools 数组会话内冻结（件一B）

M2 现有实现是 `_tmpfix_allow = _TMPFIX_CORE | _tmpfix_seen`，其中 `_tmpfix_seen` 扫历史里用过的工具名 —— **每遇一个新工具就变一次，每变一次打掉一次缓存**。线上日志 `tools_exposed=53 of 210` 而白名单只有 49 个，说明 `seen` 已涨了 4 个，即已发生至少 4 次缓存作废。

**它省的是发送字符数，赔的是 99.7% 的命中率。** 本批工具数量**保持 210 不动**，只把数组冻住；减工具单独成批。

### 5. 写入端（件二A / 件二B）

**件二A 写入准入**：触发器无变更不写历史 + trigger 空 filter 语义收口。生产 `assignment-delivery-refresh` 的 `filter: {}` 是空对象，配合 raw 路会绕过过滤（见记忆中「trigger 的 filter 被 raw 路空 filter 绕过」：一句「你好」跑两个回合，1056 次注入已烤进压缩摘要删不掉）。

**件二B append-only 落盘**（本次新增，实测驱动）。`session/conversation.py:186-202` 的 `save()`：

```python
content = "\n".join(json.dumps(msg, ensure_ascii=False) for msg in self.messages) + "\n"
tmp_path = self._path.with_suffix(".jsonl.tmp")
await anyio.Path(str(tmp_path)).write_text(content, encoding="utf-8")
await anyio.Path(str(tmp_path)).replace(str(self._path))
```

**名字叫 `.jsonl`（追加格式），行为是每次 commit 全量覆写。** `agent.py` 有 8 个 commit 点（450 / 467 / 680 / 892 / 909 / 938 / 970 / 1030），一个回合会踩好几次；`schedule_registry.py:472,516` 与 `trigger_registry.py:273,307` 还各有一处。66MB 的会话每 commit 一次就写 66MB。

实测坐实：

- gateway cgroup 写入速率 **133 MB/min**（120 秒采样 `io.stat` 的 `wbytes` 增量 280408064 字节）
- 容器启动 75 分钟，blkio 累计写 **5.81GB**
- 401 个历史文件共 373MB，其中 **40 个在这 75 分钟内被 touch，合计 325MB**

改法：`save()` 改为真追加（只写新增行）。件二A 减少「写多少次」，件二B 减少「每次写多大」，是同一笔账的两面。

### 6. 件三 / 件四 / 件六

- **件三 回合收敛**：`feishu_docs_search` 曾被换词调 305 次直到上游 402。判据必须验「模型收到显式说明」而非静默截断 —— 静默截断会让模型以为真没结果、继续换词，这是 `truncate_tool_result`（`history_display.py:86`）docstring 已写明的坑。
- **件四 压缩移出会话锁**（原 M3）：件一做完后压缩变罕见，本件优先级可能下降；但压缩占锁会延迟**下一条**消息（排队 p50 曾达 169 秒，8-31 恶化到 774 秒），仍需处理。涉并发，需独立评估。
- **件六 运维**：3 个公共区高博定时任务移入个人区（缺 `<open_id>/` 那层目录），不改代码。风险：需确认 scheduler 能重新认到，现在动可能让提醒静默失效。

### 7. 临时改动处置

**本批一次部署里先回滚 6 项到 `.orig` 基线再上新码，不逐条上生产。**

理由：M2 是硬编码白名单、M5 是常量硬改、M1 复活了已删除的方案且其打点日志用 `%`-格式混在 f-string 项目里 —— 三者都不是正规实现的形状，留着会让正规实现的判据分不清是自己生效还是临时改动生效。

**回滚必须用 `.orig` 而非 `.pre-m1log`**（见 W 段第 1 节纠正 3）。

---

## A —— 执行过程

本文档为设计，实现按 6 件拆卡执行，**件一A 排第一**（它改共享装配路径，后续各件都在其之上）。建议顺序：

```
件一A（预算/滞回/句柄）
  └→ 件一B（易变块归位 + 删 boundary + tools 冻结）
       └→ 件二A（写入准入）
            └→ 件二B（append-only 落盘）
                 └→ 件三（回合收敛）
                      └→ 件四（压缩移出锁）
件六（运维，无代码，可并行）
```

每张卡的提示词须点名「**落代码并提交**」—— 本仓一天出过三例 0 commit 直接进 review。

**已核实的执行环境坑（必须写进每张卡）：**

1. **worktree 里跑 pytest 必须带 `PYTHONPATH=src`**，否则 `.pth` 写死主检出 src，测的是另一份代码 —— 会出现 13 个 collection error、passed 少一百多，且能伪造出回归。
2. **pytest 跑子树必须 `-o testpaths=` 且写在路径参数之前**，两种写错法都不报错、只是数字变大。
3. **Windows 上 5 条 session 测试恒失败**（asyncio 子进程 `NotImplementedError`），全量 57 failed 是**基线不是回归**；实际在 57-62 间浮动（硬编码管道名被残留进程占着）。判回归前先用 `git stash` 做控制实验。
4. **Kanban worktree 的 HEAD 可能领先 task 分支 ref**，只 merge 分支名会静默丢 commit。
5. **改名/搬迁自查的正则会漏掉分段拼接路径** —— 判据报 0 处而代码里还剩 10 处。件一B 搬迁 `profile/advice/policy` 时尤其要防这条。

---

## T —— 测试与验收

### 1. 判据设计原则

- **每条判据都要能变红。** 写完做变异复核：故意改坏实现，断言用例真的失败。
- **件一B 用缓存命中率而非延迟 p50 做回归判据。** p50 会被样本分布污染 —— 2026-09-02 报出的「一句你好 135 秒 → 24 秒，5.6 倍」被次日 14 小时观测推翻：真人会话 p50 只从 8361 降到 7813 ms，p90 反而从 25542 恶化到 38239 ms，因为改后样本被单个会话占了 71%。命中率不受这个影响。
- **反向用例必须有。** 件二A 只验「不写」会把「永远不写」也判绿。

### 2. 明确的非目标与边界

- **件一治不了固定开销本身。** 若系统提示 + 工具 schema 就超预算，省略掉全部历史也没用。现默认预算 `100000`（`src/psi_agent/ai/__init__.py:149,151`）**小于**固定开销约 117k token，压缩在数学上**永远达不标** —— 这才是「一个考勤任务压缩 50 次、单调递增到 400093」的真因，不是压缩坏了。**故预算默认值必须提到固定开销之上，且可配置。**
- **不做历史轮转。** 「整个文件进内存」经实测证伪：66MB 解析仅 **0.48 秒**（130 MB/s），会话不常驻内存。轮转把 66MB 切成 6 个 11MB，活跃那份仍然每 commit 全量重写 —— **轮转减少历史总量（磁盘还有 14G，不紧张），不减少写放大**。真修法是件二B。
- **gateway 内存需单独查，成因不是历史文件。** 实测 `2.595GiB / 3GiB = 86.51%`，撞的是 `HostConfig.Memory=3221225472` 的**自身硬限**，不是宿主内存不足（宿主 7.1G、available 1.6G）。离 OOMKill 不远，但与本批无因果，不在此处并案。
- **磁盘大小 ≠ 内容字符数**：历史文件磁盘大小约为解码后字符数的 2 倍（中文转义成 `\uXXXX`）。9.58 MB 文件 = 4.45 MB 实际内容，两数不可混比。

### 3. 已被推翻的说法 —— 不得引用

| 说法 | 状态 |
|---|---|
| 「一句你好 135 秒 → 24 秒，5.6 倍」 | 已推翻，见本段第 1 节 |
| 「定时任务抢上游连接池导致尾部恶化」 | 已证伪：73 个真人回合与其活跃分钟重叠 **0** 次 |
| 「公共区那两个 TASK.md 有权改触发器和定时任务」 | 它们是 `fire: tool` 调 `feishu_message_send`，不进模型 |
| 「调高阈值让压缩变罕见」 | 对真人成立（8.9% → 2.7%），对定时任务**完全不成立** —— 一个考勤任务常驻 38-40 万 token，每回合必然压缩，约 2000 万 token 无效消耗 |
| 「压缩是整趟作废重发」 | **读码已推翻**，见下 |
| 「78 次 HTTP 400」 | 假阳性，`grep -c "400"` 匹配了 `400098` 这类数字；精确复核为 **0** |
| 「M1 做对了一半，应改为经 turn_context 移到尾部」 | 方向对但陈述不准：`turn_context` 早已在跑，M1 是复活了已删除的 boundary 方案。见 H 段第 3 节 |

**关于「整趟作废重发」的读码结论**：`agent.py` 里 `_maybe_compact`（`:981`）发生在回合**正常完成之后** —— `accumulated_content` 已保存、`assistant_msg` 已写历史、`commit()` 已落盘，紧接着 `_finish(COMPLETED)` 直接 return。那一趟**有效、用户拿到了回复**。所谓 112.3 秒不是白跑，是 4 个有效回合各自在回复之后额外付了一次压缩调用；压缩信号是**下一趟**才起作用（历史里多了 `compacted` 行，`messages_for_ai` 据此裁剪）。

因此「发送前预判以避免白跑」这个立项理由**不成立**，该机制只作为预算执行的副产物存在。这也是原 P0-1 被降级、M3（件四）被提前的原因。

### 4. 部署与回滚

- **一次部署**：先回滚 6 项临时改动到 `.orig`，再上新码。不逐条上生产。
- **镜像必须做三层核验**（build 机 src 会漂移，第三层验镜像内产物是 8-18 事故缺的那层）。
- **改生产容器文件只能 `docker cp` + `docker restart`**，严禁 `docker compose up -d`。
- **改完必须核 md5** 确认改的是真正被加载的那份 —— 生产有多份同名文件（`workspace/` vs 容器内 vs build 机）。已知：`psi-agent` 在 `lark.oauth` 不在 `account`；要改 `workspace/` 那份；`up -d` 不重载内容。

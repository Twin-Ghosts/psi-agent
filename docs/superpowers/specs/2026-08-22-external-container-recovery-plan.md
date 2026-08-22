# 独立容器降级修复与代码基线收敛

**描述：** 修复 8-18 构建导致的罗霖/成 xx 独立容器路由降级、21 名用户记忆服务失效，并把线上未入库代码收敛回 git，随后走一遍正式发布流程验证发布文档。

**版本号：** 1.0

**状态：** 待评审

**适用范围：** psi-agent 生产环境（新加坡节点 account.genuineknowledge.cn）

**关键词：** external-sessions、代码基线、Fusion Memory、发布流程、W/H/A/T

**创建人：** @zsd

**审核人：** @待补

**关联文档：**

- `docs/haitun-delivery/psi-agent-release-to-cloud.md` —— 发布流程，本任务阶段 2 照它执行并回头修正它
- `docs/haitun-delivery/migration-log.md` —— 8-20 迁移实录，已证实迁移未改代码
- 《真知开发执行 SOP》v1.0 —— 本文档结构依据

***

## W —— 是什么

### 1. 解决谁的什么痛点

**痛点一 · 罗霖的对话上下文一分为二。**

罗霖（`ou_c77e484d4cf5699947408c9448a8e777`）的飞书私聊本应转发到独立容器
`psi-agent-luolin`，实际落在主 gateway 进程内的本地 Session。同一个 open_id 因此有两份互不相干的历史：

| 位置 | 行数 | 最后写入 | 由什么驱动 |
|---|---|---|---|
| `workspace-luolin`（独立容器） | 9875 | 持续写入中 | 容器自己的定时 trigger（作业投递） |
| `workspace`（主容器） | 4523 | **8-21 20:01 冻结** | 飞书消息 |

他在飞书里说的话，他容器里的 agent 看不到；容器里做的作业投递，飞书侧看不到。

**痛点二 · 成 xx 同样降级。** `ou_716d18b92e20c74726821c79f02826d7` 情况相同：独立容器侧 3931 行（写入中），主容器侧 3844 行（8-21 19:57 冻结）。

**痛点三 · 21 名用户的长期记忆存不进去。**
`memory_user_not_configured` 累计报错 358 次。token 表 `/workspace/.psi/memory_tokens.json`
只有 24 人且 mtime 停在 8-07 15:43，而有历史的 session 有 45 人，**21 人不在表里**。
根因见 `workspace/tools/_fusion_memory_config.py:180-186`：查不到 entry 时，
`auto_register_feishu` 为假就抛错，而 `FUSION_MEMORY_AUTO_REGISTER_FEISHU` 在三个 `.env` 中均未配置，
默认值 `False`（同文件 `:48`）。

**痛点四 · 跨容器文件交接坏了，用户看到裸 `[SEND:]` 标记。**

2026-08-22 11:12 成 xx 向海豚要一份 md，收到的不是可点击下载的附件，而是一行原文
`[SEND:/workspace/真知问题解决与求助SOP（优化版）.md]`。生产日志同刻实证：

```
[Lark] 11:14:35 [WARNING] outbound: materialize blocked: could not read local file
'/workspace/真知问题解决与求助SOP（优化版）.md': [Errno 2] No such file or directory
```

文件确实存在，但**在另一个容器里**：`psi-agent-chengxx` 容器内 7346 字节，
`psi-agent-gateway` 容器内不存在。

机制：飞书 WS 长连接只能在主容器（同一 App 只允许一条），所以 channel 跑在主容器；
而 `_send_file()` 是拿路径读**本地**文件上传（`channel/feishu/client.py:167`：
`channel.send(chat_id, {"image": {"source": path}})`）。消息转发到独立容器后，
agent 输出的 `[SEND:]` 路径指向独立容器的卷，主容器的 `/workspace` 是另一个卷，
读不到 → `materialize blocked` → 标记未被消费，原样当文本发出。

**这不是昨天引入的新缺陷，是昨天只补了转发的一半。** 消息转发通了，文件交接没通。
原设计发现并修过这个问题——`origin/fix/external-session-attachments` 分支的两个 commit
标题即为证：`7d5c9225`「独立容器的会话不再『文件明明收到了却说没收到』」、
`1aeb6c34`「让跨容器附件交接块自带取件说明」。这两个 commit 从未进入部署线，
所以修好路由后若不一并 cherry-pick，文件交接仍然是坏的。**故这两个 commit 由「顺带捞」
提为阶段 1 必做项。**

**痛点五 · 线上跑的代码不在任何提交里，且缺两个已发布的修复。** 详见 H 段核对结论。

### 2. 做完什么样算完（验收标准，可判定）

| 编号 | 验收标准 | 判定方式 |
|---|---|---|
| **V1** | 生产 gateway 运行的代码等于某个明确 commit，无 `docker cp` 痕迹 | 容器内 99 个 `.py` 按字节 md5 与该 commit 逐一比对，全同 |
| **V2** | 罗霖飞书私聊进入 `psi-agent-luolin` 容器 | 发一条测试消息，`workspace-luolin` 侧历史行数增加，主容器侧不增 |
| **V3** | 成 xx 飞书私聊进入 `psi-agent-chengxx` 容器 | 同 V2 判法 |
| **V4** | 压缩劫持修复与工具结果截断修复在线上生效 | 容器内 `grep MIN_SUMMARY_CHARS session/agent.py`、`grep MAX_TOOL_RESULT_CHARS session/history_display.py` 均命中 |
| **V5** | 新飞书用户不再报 `memory_user_not_configured` | 重启后新用户首次对话，token 表出现其条目；日志无新增该错误 |
| **V6** | 21 名存量用户记忆可用 | 逐个确认已在 token 表中 |
| **V7** | 两个独立容器的历史/记忆/文件按 D4 决策处置完毕，无数据丢失 | 处置前后文件数与行数对账 |
| **V8** | 发布流程文档 §38 的错误结论已修正 | 文档中不再出现「与任何提交都不一致（最佳 89/97）」 |
| **V9** | `psi-agent-release-to-cloud.md` §5 八条判据全部通过 | 照文档逐条执行 |
| **V10** | pgvector 数据未丢失 | `deploy_fusion_memory_pgdata` 卷仍在，表数量与发布前一致 |
| **V11** | 跨容器文件交接可用 | 让独立容器的 agent 发一个文件，飞书侧收到**可点击下载的附件**而非裸 `[SEND:]` 文本；日志无 `materialize blocked` |

### 3. 明确不做什么

- **不做两份历史的自动合并。** 两侧内容来源不同（飞书对话 vs 定时 trigger），
  时间线交错，机器合并会产出语义错乱的上下文。处置方式见 D4，需负责人拍。
- **不在本任务内重构 `.private` 私密区机制。** 容器内当前**没有** `_private_space.py`，
  宿主 `src/` 里有一份（8-20 14:27）但不参与运行（gateway 只 bind mount `workspace`）。
  是否恢复该机制单独立项，本任务只保证不再退化。
- **不改 Dockerfile 的境内镜像源。** 发布文档 §568 已记录这是已知设计缺口，与本任务无关。
- **不动 ToC 栈**（`psi-cloud`、`psi-litellm`）。

***

## H —— 怎么做

### 4. 有哪几种做法，为什么选这个

**决策 D1 · 代码基线取哪儿**

| 候选 | 取舍 | 结论 |
|---|---|---|
| A. 还原成镜像里的原始代码 | 镜像版本恰恰**缺** external-sessions，还原即让罗霖永久降级 | ✗ |
| B. 以 `deploy-214-envelope-tombstone` 为基线 | 该分支有原实现，但相对当前生产 39 处不同 + 14 个文件缺失，过于陈旧，merge 会大面积回退 | ✗ 仅作参考 |
| C. **以 `origin/main` 为基线，补回 external-sessions** | 生产 92/99 文件已与 main 逐字节相同，差异面最小；main 还含生产缺失的两个修复 | ✓ **选定** |

**决策 D2 · external-sessions 用哪份实现**

| 候选 | 取舍 | 结论 |
|---|---|---|
| A. 沿用昨晚 `docker cp` 的临时实现 | 已在生产验证可路由，但无测试、未 review、且 `_private_space` 接线是断的 | ✗ |
| B. **参考 `deploy-214-envelope-tombstone` 原实现，在 main 上重做并补测试** | 有原始设计可依，能补齐测试与文档 | ✓ **选定** |

原实现位置：`origin/deploy-214-envelope-tombstone:src/psi_agent/gateway/_feishu_manager.py:42`
（`external_sessions()`）、`:39`（`_EXTERNAL_ENV_KEY`）、`:165-168`（路由分支）。
一并 cherry-pick `origin/fix/external-session-attachments` 的 `7d5c9225`、`1aeb6c34` 两个后续修复。

**决策 D3 · 记忆服务修复的时机**

记忆服务修复需改 `.env` + 重启 gateway，而重启会让 `docker cp` 的代码消失、罗霖立刻再次降级——
**两件事在「重启」这一点上冲突**。

| 候选 | 取舍 | 结论 |
|---|---|---|
| A. 今天单独抢修记忆服务 | 21 人早一天可用，但要么再 `docker cp` 一轮延续不受控代码，要么牺牲罗霖 | ✗ |
| B. **并入正式发布窗口一次做完** | 记忆服务是功能降级（存不进长期记忆）而非完全不可用，可等一天 | ✓ **选定（负责人已定）** |

**决策 D4′ · 两个独立容器的历史/记忆/文件如何处置**

现状事实：

| 维度 | 罗霖 | 成 xx |
|---|---|---|
| 独立容器 workspace | 214M | 54M |
| 独立容器侧历史 | 9875 行，写入中 | 3931 行，写入中 |
| 主容器侧历史 | 4523 行，8-21 20:01 冻结 | 3844 行，8-21 19:57 冻结 |
| 独立容器 token 表 | 有，仅 1 条自己的 | **无 `memory_tokens.json`** |
| 文件 | 私密文件在独立容器（含 67M inbox） | 在独立容器 |

**关键事实：主容器侧两份历史都已冻结**，冻结时刻正是 8-21 20:01 前后 `docker cp` 生效、
路由切到独立容器的时刻。所以主容器侧是一段**起止明确、有界、可校对**的区间
（8-18 降级起 → 8-21 20:01 路由恢复止，约 3 天），不是持续增长无法划界的数据。

三个候选：

- **D4-a 保留双份，不合并。** 主容器侧改名归档留证。代价：那 3 天飞书对话的上下文 agent 读不到。
- **D4-b 归档后把飞书对话部分人工挑拣追加到独立容器侧。** 上下文最完整，但两侧时间线交错。
- **D4-c 以主容器侧覆盖独立容器侧。** ✗ 直接否决——会丢掉独立容器侧近 6000 行作业投递记录。

**选定 D4-a′（拆成两步，归档必做、迁移待定）：**

1. **归档（阶段 2 无条件执行）：** 主容器侧两个文件改名保存，不删。无论后续选什么，
   这一步都是回退底线。
2. **迁移与否降级为阶段 2 之后的独立判断，不阻塞发布。** 需先看那 3 天的内容是否有实质工作
   上下文（方案讨论、决策）还是零碎问答——这要读内容才能定，本次未读（涉他人对话，
   且量大：罗霖 21.9M / 成 xx 3.4M）。

若决定迁移，两条技术约束必须先解决：

- 独立容器侧同期在写 trigger 记录，**时间线是交错的**。按时间穿插可能让 agent 读到
  「自己回答了没被问过的问题」。
- `.jsonl` 含 `compacted` 等结构行，盲目 append 可能破坏压缩状态。

**⚠️ 上下文溢出风险：** 罗霖那份历史 21.9M / 9875 行，已存在一次
`bak-ctxoverflow-20260819` 备份（25.6M）——**它撑爆过上下文**。再追加 4523 行有触发同样
问题的风险。故即使决定迁移，也只挑拣关键内容，**不整段追加**。

记忆侧另需处置：成 xx 容器**没有 token 表**，`FUSION_MEMORY_TOKEN_MAP_FILE` 却已配置——
修好路由后他的记忆会立刻走上与主容器同一个报错路径。罗霖容器的表里只有自己 1 条，需确认够用。

### 5. 别人怎么做的，我这样是否更好

**无直接外部对标**——「线上工作树快照与 git 失联」是本仓特有的运维状态，不是通用工程问题。

仓内既有惯例可循，且本方案严格沿用：

- 发布流程照 `psi-agent-release-to-cloud.md` 执行，不自创路径。其四条硬约束（不用 `down -v`、
  oauth-proxy 必须跟着重建、不滚动更新、先打备份 tag）全部保留。
- 「以代码为准，回头修计划表述」是 SOP 附录 A 的 A 段要求，本任务阶段 3 反过来修发布文档，
  正是这条的应用。

比现状强在哪：这是第一次让生产代码等于一个可指名的 commit。在此之前任何一次发布都是
「拿一个未知基线覆盖另一个未知基线」（发布文档 §38 原话）。

### 开工前核对结论（SOP 触发式要求）

W/H 依据的诊断部分写于 8-20（发布文档 §38），开工前已核对代码，**发现三处与诊断不符**：

**不符一 · 偏差规模。** §38 称「线上 src 与本仓任何一次提交都不一致（最佳 89/97）」。
实测：**容器内 99 个 `.py` 与 `origin/main` 相同 92、不同 7、缺失 0。**

根因是 §38 的核验方法有缺陷——经 ssh 文本通道取文件会被转成 CRLF，导致每个文件 md5 全变，
产生「全不一致」假象。同一文件 `session/ai_client.py`：ssh 取回 5010 字节 / md5 `7320b26b`，
容器内按字节读 4894 字节 / md5 `d81a7f3f`，**CR-stripped 后与 `origin/main` 完全相同**。
我第一次量也踩了同一个坑。**核验必须在容器内按字节做。**

**不符二 · 差异文件清单不对。** §38 列的 7 个文件与实测的 7 个只有 1 个重合
（`gateway/_feishu_manager.py`）。实测差异分两组，边界很干净：

A 组 · mtime `Aug 18 16:25`（8-18 构建的工作树，共 4 个）

| 文件 | 差异 | 方向 |
|---|---|---|
| `session/agent.py` | 缺 `MIN_SUMMARY_CHARS` / `MIN_SOURCE_CHARS` / `HIJACK_ECHO_PREFIXES` | 线上**落后** main |
| `session/history_display.py` | 缺 `MAX_TOOL_RESULT_CHARS` 与 `_TRUNCATION_MARKER` | 线上**落后** main |
| `cli.py` | 缺 `SelfUpdate` 命令（-44 行） | 线上**落后** main |
| `ai/server.py` | 多 2 行 deepseek 配置（见下） | 线上**独有，未入库** |

B 组 · mtime `Aug 21 20:00`（昨晚 `docker cp`，共 3 个）：
`gateway/_feishu_manager.py`、`gateway/server.py`、`gateway/__init__.py`。

**结论：真正需要人判断的只有 1 个文件**（`ai/server.py` 那 2 行），
其余 6 个靠部署 main + 重做 external-sessions 自然收敛。工作量比 §38 描述的小一个量级。

**不符三 · `_private_space.py` 的位置。** §38 称它「线上在跑，任何提交里都不存在」。
实测：**运行中的容器 `/app/src` 里没有这个文件**。宿主 `/srv/haitun/psi-agent/src/` 里有一份
（8-20 14:27），但 gateway 只 bind mount 了 `workspace`，该目录不参与运行。§38 量的是宿主目录，
量错了对象。且容器内 `_feishu_manager.py` 已无 `_private_space` 的 import（grep 零命中），
私密区隔离当前是断开状态。

### deepseek 那 2 行具体是什么

位置：生产容器 `/app/src/psi_agent/ai/server.py`，在剥离请求体私有字段之后、
设置 `stream_options.include_usage` 之前：

```python
    body.pop("routing", None)
+   if provider == "deepseek":
+       body["reasoning_effort"] = "high"
    stream_opts = body.get("stream_options", {})
```

作用：当上游 provider 是 deepseek 时，强制在请求体注入 `reasoning_effort="high"`，
把模型的推理强度顶到最高档。对其他 provider 无影响。

**它不属于任何人的分支——git 里根本没有这段代码。** 实测搜索范围与结果：

```
git log --all -S'reasoning_effort' -- src/     → 零命中
遍历全部远程分支 tip 的 ai/server.py           → 零命中
遍历全部本地分支                                → 零命中
git log --all -G'reasoning_effort'             → 8 个 commit，但逐个核查其
                                                 ai/server.py 全为 0 命中
```

那 8 个 `-G` 命中是噪音：匹配的是 `build/` 下 PyInstaller 打包产物
（`Analysis-00.toc`、`PYZ-00.pyz` 等）里碰巧出现的字符串，以及 kanban 自身的
checkpoint commit，与 `ai/server.py` 无关。

**结论：这 2 行是直接在生产工作树上改的，从未进入版本控制。** 这比「某位同事的分支没合」
更严重——8-18 以 main 为基构建时丢掉的不只是别人分支里的功能，还有这种只存在于服务器
文件系统上的改动。也说明 mtime `Aug 18 16:25` 不可作为作者线索：那次构建把整个工作树的
时间戳都刷新了，真正该问的是**谁动过生产 `src/`**，范围比一个人大。

**处置（负责人已定）：本次修复直接丢弃这 2 行。** 已向同事确认过。丢弃后 7 个差异文件
全部可机械收敛到 main，阶段 1 不再有需要人判断的项。

***

## A —— 执行过程

> 开工后追写。按 SOP 规则一，本段在 W/H 落定后才开始填。

### 阶段 0 · 固化现状（只读 + 打 tag，不重启）

- [x] 容器内 99 个 `.py` 按字节取回本地存证（唯一权威的生产代码）
- [x] 宿主 `/srv/haitun/psi-agent/src` 一并取回（判断 `_private_space.py` 去留时要用）
- [x] `docker tag psi-agent-gateway:local psi-agent-gateway:backup-20260822`
      （发布文档 §6.2：现在只有一个 tag，没有退路）
- [ ] 通知运维与同事：**正式部署前不要碰 gateway 容器**（需人工发出，未完成）

**存证路径：** `F:\code\psi-agent-evidence\20260822-prod-gateway\`（仓外，不入 git）。
服务器侧副本 `/tmp/psi-evidence-20260822`，可清理。

| 目录 | 来源 | 文件总数 | 其中 `.py` | 校验结果 |
|---|---|---|---|---|
| `container-app-src/src` | 容器 `psi-agent-gateway:/app/src` | 283 | **99** | 283/283 字节一致 |
| `host-srv-src/src` | 宿主 `/srv/haitun/psi-agent/src` | 279 | 97 | 279/279 字节一致 |

取回方式全程二进制：`docker cp <容器>:/app/src -` 出 tar 流、md5 清单由**容器内 python3 按
`open(p,"rb")` 计算**、tar 走 `scp`、本地解包后逐文件重算 md5 与容器内清单比对，
`mismatch=0 missing=0 extra=0`。刻意不走 `ssh 'cat'` 文本通道，即 §38 假阳性的来源。
`__pycache__` 全部排除。清单文件 `container-app-src.md5`、`host-srv-src.md5` 随存证留档。

镜像 tag 已就位，`backup-20260822` 与 `local` 同指 image `896467e05f72`（构建于 8-18 16:39）。
本阶段对生产只有 `docker tag` 一次写操作，打完四个容器 uptime 未变
（gateway 15h、chengxx 18h、luolin 40h、oauth-proxy 15h），无重启。

顺带实证三点：① `_private_space.py` 在宿主清单命中、容器清单零命中，**「不符三」成立**；
② A 组 mtime 全为 `Aug 18 16:25`、B 组全为 `Aug 21 20:00`，边界与「不符二」一致；
③ V4 当前确为不通过——容器内 `MIN_SUMMARY_CHARS`、`MAX_TOOL_RESULT_CHARS` 均零命中，
而 `PSI_FEISHU_EXTERNAL_SESSIONS` 命中 1 次（`docker cp` 的实现在位）。

> ⚠️ 阶段 0 完成前的风险窗口：gateway 当前代码是 `docker cp` 进去的，
> `docker compose up -d` 或 restart 重建容器即消失，罗霖立刻再次降级。
> **存证已完成，该窗口对「代码丢失」已关闭**（副本可回灌）；
> 但「容器被重建 → 罗霖降级」的风险仍在，至正式发布窗口为止。

### 阶段 1 · 本地代码收敛（基线 `origin/main`）

- [ ] A 组三个落后文件直接取 main（`agent.py`、`history_display.py`、`cli.py`）
- [ ] `ai/server.py` 的 deepseek 2 行：**丢弃**（负责人已定，不入库）
- [ ] 参考 `deploy-214-envelope-tombstone` 重做 external-sessions，补测试
- [ ] cherry-pick `7d5c9225`、`1aeb6c34`（**必做**，跨容器文件交接 = V11）
- [ ] 新增配置项 `FUSION_MEMORY_AUTO_REGISTER_FEISHU`
- [ ] 弃掉本地 commit `46264245`（昨晚的重复实现）
- [ ] 跑测试（注意：Windows 上 5 条 session 测试 + 全量 57 failed 是既有基线，不是回归）

### 阶段 2 · 走正式发布流程

- [ ] 照 `psi-agent-release-to-cloud.md` 执行，同一窗口内一并落地记忆服务 `.env` 改动
- [ ] 按 D4 决策处置两个容器的历史/记忆/文件
- [ ] 补成 xx 容器缺失的 `memory_tokens.json`
- [ ] 按 §5 八条判据 + 本文 V1-V11 验收

### 阶段 3 · 反过来修文档

- [ ] 修正 §38：换成实测的 92/99，改掉「任何提交都不存在」的结论
- [ ] 附录 A 增补：**核验必须在容器内按字节比对**，经 ssh 文本通道会因 CRLF 产生假阳性
- [ ] §38 标注「未实测」的收敛方法，这次实测了，补结果

### 中途改了哪些决定

> 待追写。

***

## T —— 测试与验收

> 收尾时补齐。按 SOP 规则二，本段只对 W 的 V1-V11 逐条核验，不自定新标准。

| 编号 | 结果 | 证据 |
|---|---|---|
| V1 | 待验 | |
| V2 | 待验 | |
| V3 | 待验 | |
| V4 | 待验 | |
| V5 | 待验 | |
| V6 | 待验 | |
| V7 | 待验 | |
| V8 | 待验 | |
| V9 | 待验 | |
| V10 | 待验 | |
| V11 | 待验 | |

### 怎么测的

> 待追写。

### 还剩什么问题

- `_private_space` 私密区机制是否恢复，未决（W 段已列为排除项，需单独立项）
- Dockerfile 硬编码境内镜像源，已知缺口，本任务不动
- **流程问题（比代码修复更值得定规矩）：** 生产部署依赖了未提 PR 的本地代码，
  这是本次事故的真正来源。8-18 与之前是两位不同同事部署，前者以 main 为基构建，
  丢失了后者未提交的功能。需要一条规矩：**只能从可指名的 commit 构建镜像。**

***

## 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| 1.0 | 2026-08-22 | 初版，W/H 落定，开工前核对出 3 处与 8-20 诊断不符 |

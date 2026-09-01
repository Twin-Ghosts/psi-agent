# 延迟排查探针

从 stdin 读 psi-agent 日志, 交代一轮对话的时间去哪了。

```bash
docker logs psi-agent-gateway 2>&1 | python3 tail_union.py
docker logs psi-agent-gateway 2>&1 | python3 model_repair.py
docker logs psi-agent-gateway 2>&1 | python3 gap_probe.py --min-ms 5000
```

| 脚本 | 回答什么 |
|---|---|
| `parse.py` | 公共解析层 (日志行 → 记录 → 回合)。不单独运行, 但有 `--self-check` |
| `tail_union.py` | 一张表: 每轮的墙钟 / 模型 / 工具 / 其余, 加最慢 N 轮 |
| `model_repair.py` | 模型耗时占比 + **两端标记的配平核对** |
| `gap_probe.py` | 日志里的静默空档夹在哪两条之间 |

## 三件必读的事

**1. 它们从 stdin 读, 自己不调 docker。** 不喂 stdin 会**报错退出 (exit 2)**, 不会给一张
全零表。这个硬闸是特意加的 —— 原先在云上的版本静默输出 `n=0` 全零、退出码 0, 于是「量到
了 0」和「根本没量」长得一模一样, 已经骗过人一次。

**2. 模型耗时以 `ai/server.py` 两端为准, 不要拿 agent 侧配对。** 理由写在
`src/psi_agent/ai/server.py` 的 `_TURN_MARKER_OPEN` 上方: agent 侧标记只覆盖部分调用路
径, 2331 轮里 241 轮 (10%) 没有, 配不上对的整轮被丢弃, 而丢掉的恰是走特殊分支的慢轮次
—— 上一轮排查因此把 63.4% 报成了 39.2%。`model_repair.py` 会并排给出两种口径, 就是为了
让这个差别可见。

**3. 判据会腐化, 但不写死行号。** 匹配按 `logger 名 + 消息文本`, 集中在 `parse.py` 的
`_PATTERNS`, 一条一处注释指向产地。改那些日志文本后跑:

```bash
python3 parse.py --self-check
```

它拿内置样例行验证每条模式仍能命中, 并验证 `elapsed_ms` / `outcome` / 工具名抽得出来
(「匹配上了但字段读不出来」会静默变成 0, 所以这一步单独验)。

## 这是重写, 不是搬运

云端 `/root/latency-probe/` 下那份原件**没有拿到**（那台机器同时在跑部署，本轮是纯本地任
务）。这四个文件是按「它们要回答什么问题」重新实现的。已知可能的差异:

- **口径不同**: 云上那份靠 `函数名:行号` 匹配, 并用时间戳配对反推耗时。本版改成读日志行
  里自带的 `elapsed_ms` 字段 (本轮同时给 AI 两端和工具结果都加上了这个字段)。所以本版
  **不能**直接跑 2026-08 之前的存档来算耗时 —— 那些日志里没有这个字段。旧存档仍能解析
  (session 列与 elapsed_ms 都做成可选), 只是耗时列会是 0。
- **少 9 个脚本**: 云上还有 `phases.py` / `concurrency.py` / `queue.py` / `stalls.py` /
  `tail_analysis.py` / `refresh_cost.py` / `ks_durations.py` / `verify_patch.py` /
  `model_breakdown.py` / `profile_chain.py`。本轮只固化了卡片点名的三个 + 它们依赖的
  `parse.py`。其中排队/停顿类的问题, `gap_probe.py` 的边界统计能顶一部分。
- **输出格式几乎肯定不一样**: 列名、排序、分位数取法都是本版自己定的。拿两版的数字逐位
  对比没有意义, 该比的是结论。

如果原件后来找回来了, 值得做一次交叉验算 (同一份日志两边跑, 比结论而非比格式)。

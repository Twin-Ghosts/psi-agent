# 动态一层评测落盘 + 前后对比摘要 — 实施工作树

> PR：`feat/todo-dynamic-layer1-eval`（基线 `origin/main a13cdc4a`）
> 依据：桌面《CEO需求满足度评估与总体开发方案_动静态分层》第六 / 七章；CEO 纪要「MVP 起即记录 check 结果数据，先搭评测体系（更紧急）再搭数据体系」。

## 目标

动态一层（前后连续性、事情搞没搞定）的**判定主体已在 main**：`company-todo-audit`（闭环五要素 + 逾期回流）与 `todo-completion-standard`（四档 + E1-E3 + 消失裁决）。本 PR 只补「余下」两件事，让判定结果**可积累、可对比、可评估**：

1. **判定结果落盘** —— 每轮 audit 的逐行判定写 `.todo-eval/YYYY-MM-DD.json` + 飞书「评测记录」表，形成个人跨周期连续性序列。
2. **逐人前后对比摘要** —— mentor / boss 报表加 新开 / 承接 / 消失 / 回流 / 顺延 六项，替代「凭印象看趋势」。

## 改动清单

| 文件 | 改动 |
|---|---|
| `skills/company-todo-audit/SKILL.md` | 新增「动态一层 · 评测落盘」「前后对比摘要」两节；description 与 boss 统计句同步 |
| `tests/test_todo_dynamic_layer1.py` | 新增测试：落盘 schema / 幂等 / 六项摘要 / 消失裁决 / 工具引用 / 索引 |
| `AGENTS.md` | `company-todo-audit` 索引行补「评测落盘 + 前后对比摘要」 |
| `docs/todo-dynamic-layer1-eval-worktree.md` | 本工作树 |

## 判定口径（不变，只加落盘与对比）

- **闭环**：台账五要素齐（验收人 / 截止到期 / 勾选提交 completed_at / mentor 打分评语 / 评价回写 wiki）才置「已闭环」。
- **回流**：未闭环按原截止日算逾期，「[逾期 N 天]」回流进本周期；请假顺延不计逾期但仍回流。
- **消失**：上期有、本期无 → 归 `todo-completion-standard` 的「推断已完成／待确认」，**不判未闭环、更不判失实**。

## 落盘 schema（.todo-eval/YYYY-MM-DD.json，与静态二层共用目录约定）

`date / cycle / person / item / item_type / verdict / missing_elements / evidence_level / evidence_refs / rules_hit`；同 (cycle, person, item) 幂等覆盖；同步飞书「评测记录」表。

## 测试与验收

- 运行：`python -m pytest agents/feishu/tests/test_todo_dynamic_layer1.py`（隔离收集配置）；`python -m ruff check agents/feishu/tests/test_todo_dynamic_layer1.py`。
- 验收：测试全绿；`git diff` 只含本表声明的 4 个文件。
- 定时任务：`todo-cycle-audit`（cron `30 14 * * 1,3,5`）**由用户在与 agent 对话中用 `schedule_manage` 自行设置**，本 PR 不携带仓库内 schedule 实体。

## 后续（不属本 PR）

- 动态二层成长评价：消费本 PR 落盘的连续性序列，另立计划。
- 静态三层对齐 / `sync_org_tree` 缺口：另立计划。
- 准确率评估集：本 PR 的落盘是它的数据前提，评估运行本身另立计划。

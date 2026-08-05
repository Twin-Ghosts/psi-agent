---
name: feishu-rookie-onboarding
description: "Use when a new hire joins (feishu.hr.user_created), when sending or re-sending the onboarding SOP cards, or when handling a <feishu_card_action> whose handler is rookie_sop_tick / rookie_sop_role_set. Covers per-module tickable cards, the 研发/非研发 role choice, daily 9:30 reminders, and the 18:30 HR digest with its overview-table link."
category: productivity
agent_editable: true
---

# 新人入职 SOP 卡片闭环

新人入职后：按 SOP 模块发**逐行可勾选**的卡（multi_use，勾一行只结那一行）→ 新人自己勾 →
写明细表并重算总览行 → 每日 9:30 按截止日催办 → 每日 18:30 给 HR 发汇总卡 + 总览表链接。

## When to use

- 通讯录新建员工（`feishu.hr.user_created`），或用户要求「给某人发入职 SOP 卡」。
- 收到 `<feishu_card_action>`，且 `dispatch.handler` 为 `rookie_sop_tick` 或 `rookie_sop_role_set`。

## When not to use

- 管理制度确认卡（那是 `feishu-handbook-onboarding`，两者互不替代）。
- 普通待办清单 → `feishu-todo-card`；一张卡只要一个答案 → `feishu_message_send_card`。
- 签字确认、背调材料收集等线下环节。

## Instructions

### 发卡

1. 调 `rookie_sop_card_send`。触发器场景参数留空，靠 Session 注入的 `event_payload_json`。
2. 手工联调传 `open_id`（必填）、可选 `name` 与 `onboard_date`（`YYYY-MM-DD`，默认今天）。
3. 幂等：同一人重复调用复用已有明细行，不会写出两套，也不会重复建定时任务。
4. 工具成功后卡片已可见：本轮**零 assistant 文本**（不要说「卡片已发送」）。

### 处理勾选

1. 解析 `<feishu_card_action>` 整段 JSON，调 `rookie_sop_tick(card_action_json=<整段 JSON>)`。
2. 不要先复述「你点击了…」—— 卡片已由框架原地重绘。
3. 成功 → 零文本结束；只有工具返回 `ok=false` 才回报必要错误，不得谎称成功。
4. **批量**：payload 若包在 `<feishu_card_action_batch>` 里，**每条各调一次**
   （漏一条就丢一项完成），然后最多回一条汇总，或直接零文本。

### 处理角色选择

1. `dispatch.handler` 为 `rookie_sop_role_set` 时调 `rookie_sop_role_set(card_action_json=…)`。
2. 选「非研发」→ 开发环境模块全标 `不适用`（不计进度分母、不催办、不进 HR 日报）。
3. 选「研发」→ 工具会发一张**新卡**列出 5 个研发项。这是刻意的：原卡按钮点完即被消费，
   `feishu_message_edit_card` 不重新注册回调，编辑出来的按钮全是死的。

### 定时任务

- 催办：每人一份 `rookie-remind-<open_id 后 8 位>`，`cron="30 9 * * *"`、`fire=tool`、
  `tool="rookie_sop_remind"`、`tool_args={"open_id": "ou_…"}`。由 `rookie_sop_card_send` 自动建。
  新人出新手村后工具会删掉自己这一份。
- HR 日报：全局一份 `rookie-digest-daily`，落在 HR 自己的 Session，`cron="30 18 * * *"`、
  **`fire=prompt`**（内容要现算聚合，`fire=tool` 到点不经 LLM 只能传固定参数），
  TASK 正文写「调用 rookie_sop_digest」。
  **这一份不会自动建**——它需要真实的 HR open_id，必须上线时手工建一次：
  ```text
  schedule_manage(action="create", schedule_name="rookie-digest-daily",
    cron="30 18 * * *", fire="prompt",
    content="调用 rookie_sop_digest 给 HR 发今天的新人入职进度日报。",
    visibility="silent", description="新人入职进度 HR 日报")
  ```
  没建之前，`rookie_sop_digest` 工具本身可用（可手工调），但到点不会自己发。

## 边界

- 禁止用 `feishu_message_edit_card` 改这些卡（不重新注册回调，按钮会全死）。
- 禁止手写 `schedules/*/TASK.md` 或 `triggers/*/TRIGGER.md`，一律走
  `schedule_manage` / `trigger_manage`。
- 总览表是**投影**：只由工具从明细整体重算，不要手工改它、也不要写增量更新逻辑。
- 单卡最多 40 行。

## 配置

`config/rookie_sop.yaml`：SOP 清单（模块 / 项 / 验收标准 / `window_days` / `dev_only`）、
`sop_doc_url`、`hr_notify_id`。改 SOP 只改这里，不动代码。
运行时的 `app_token` 与两个 `table_id` 存在 workspace 的 `.psi/rookie_sop/base.json`。

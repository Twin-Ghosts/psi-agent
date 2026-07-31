---
name: feishu-handbook-onboarding
description: "Use when HR/user NL-configures new-hire handbook SOP links or notify-me, when a new hire joins (feishu.hr.user_created), when sending the confirm card, or when handling <feishu_card_action> handbook_onboarding_process_submit / handbook_submit. Prefer handbook_onboarding_configure over asking anyone to edit YAML."
category: productivity
agent_editable: true
---

# 入职管理制度确认（卡片闭环）

新员工入职后：发欢迎 + 管理制度链接 + **确认表单卡** → 对方提交 → **校验** → 通过则通知本人与 HR；失败则说明原因并 **再发一张新卡**（旧卡点击后已失效，不能改原卡重填）。

## When to use

- 用户说「以后新人入职发这份 SOP/手册」并贴出飞书文档链接；或「确认通过后通知我」。
- 通讯录新建员工（`feishu.hr.user_created`）或「给某人发入职手册确认卡」。
- 收到 `<feishu_card_action>`，且 `dispatch.handler` 为 `handbook_onboarding_process_submit`。

## When not to use

- 普通群提醒、与手册确认无关的审批卡。
- 试图轮询文档勾选框代替确认卡（不要做）。
- **不要**让用户/HR 自己改 `config/handbook_onboarding.yaml`——一律用工具写入。

## Instructions

### 配置（NL + 链接，HR 零改文件）

1. 用户给出 SOP/手册 **URL**（可多条）→ 立刻调用  
   `handbook_onboarding_configure(links_json=<JSON 数组或单个 URL 字符串>)`。  
   - 有标题时用 `[{"title":"员工手册","url":"https://..."}]`；只有链接时直接传 URL 字符串也行。  
   - 默认 `replace_links=true`（整表替换）；用户说「再加一份」时用 `replace_links=false`。
2. 用户说「通过后通知我 / 通知 HR」→  
   `handbook_onboarding_configure(hr_notify_id=<对方 ou_...>)`。  
   「通知我」时用 `<feishu_context>` 的 `sender_open_id`。
3. 可用 `handbook_onboarding_show_config` 核对当前链接与通知对象。
4. 配置成功后简短确认已保存的链接/通知对象即可；**不要**教用户去改 YAML。

### 发卡（欢迎 / 失败重发）

1. 优先调用 `handbook_onboarding_send_welcome`（读已保存配置里的链接）。
2. 触发器场景：参数可留空，靠 Session 注入的 `event_payload_json`。
3. 手工联调：传入 `open_id`（必填）与可选 `name`。
4. 工具成功后卡片已对用户可见：本轮 **零 assistant 文本**（不要说「卡片已发送」）。

### 处理确认提交

1. 解析 `<feishu_card_action>` 整段 JSON。
2. 立即调用 `handbook_onboarding_process_submit(card_action_json=<整段 JSON 字符串>)`，不要先复述「你点击了…」。
3. 工具内已完成校验与通知 / 重发卡：
   - `passed=true` → 零文本结束。
   - `passed=false` 且 `resent_card=true` → 零文本结束。
   - `ok=false` 且带 `error` → 仅回复必要错误。
4. **不要**手写第二张卡，除非本工具明确失败且用户要求兜底。

### 环境

- 飞书后台需订阅 `contact.user.created_v3`；Channel/Gateway 带同一组 `PSI_FEISHU_APP_ID` / `PSI_FEISHU_APP_SECRET`。
- 触发器 `handbook-onboarding-welcome` 已挂 `feishu.hr.user_created`；一般无需再 `trigger_manage`，除非用户要改提醒文案/另挂群通知。

## 相关事件

| 信号 | 用途 |
|------|------|
| `feishu.hr.user_created` | 入职入口 → 触发器调 `handbook_onboarding_send_welcome` |
| `haitun.hr.handbook_confirmed` | 合成接口预留；本 MVP 以双侧消息通知为准 |

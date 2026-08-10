---
name: rookie-sop-welcome
description: "（次要路径，默认对真实新人不生效）通讯录新建员工时，给新人发入职卡"
event: feishu.hr.user_created
source: feishu
filter: {"open_id": "ou_a6875df821ff538b9db67c2a5cd5f428"}
visibility: silent
run_once: false
fire: tool
raw_event: contact.user.created_v3
tool: rookie_sop_card_send
tool_args: {}
---

向 payload.open_id 发送入职卡（multi_use 勾选卡入口 + 明细文档），并为该新人建立每日
9:30 催办定时任务。open_id / name 由 Session 注入 event_payload_json，不要写死 tool_args。

**这不是主路径 —— 主路径是 HR 在飞书里对 agent 说「给<某人>发入职卡」**
（见 `skills/feishu-rookie-onboarding/SKILL.md`），由 agent 解析出对方 open_id 后
直接调 `rookie_sop_card_send`。通讯录事件触发这条路只是**备用/次要**：
入职流程里「通讯录建号」和「HR 决定发卡」并非同一时刻——HR 可能想晚几天发、
先核实完材料再发，或者这个人根本不该走这套 SOP。让 HR 显式说一句，比
「一进通讯录就自动收卡」更贴近真实用法，也更不容易在还没准备好时误发。

因此 `filter` 被**刻意且长期**收窄到测试账号（王炜博）的 open_id，不是临时的联调
措施、也不打算在上线时放宽到 `{}`。`Trigger` 本身没有 enabled/disabled 字段
（见 `src/psi_agent/session/trigger_registry.py`），filter 收窄到没人会撞上就是
这里唯一能用的「默认关闭」手段；本文件保留是为了不丢工具引用与说明，若未来确实
需要「通讯录建号即自动发卡」，把 filter 改回 `{}` 即可重新生效——但那是一次
需要新决策的产品选择，不是这次改动要做的事。

该测试 open_id 由 `GET /open-apis/contact/v3/users/:open_id` 实际查证归属为「王炜博」，
不是从 workspace 目录名推断的 —— 目录名里还有别人的 open_id（曾据此填错成「张启华」）。
换测试账号时请同样用通讯录接口核实姓名，别靠目录名猜。

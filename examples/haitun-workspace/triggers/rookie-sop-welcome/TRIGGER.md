---
name: rookie-sop-welcome
description: 通讯录新建员工时，给新人发入职 SOP 的逐项可勾选卡片
event: feishu.hr.user_created
source: feishu
filter: {"open_id": "ou_055352a21a405ea00044bca1ec046d41"}
visibility: silent
run_once: false
fire: tool
raw_event: contact.user.created_v3
tool: rookie_sop_card_send
tool_args: {}
---

向 payload.open_id 发送按 SOP 模块拆分的 multi_use 勾选卡，并为该新人建立每日 9:30 催办定时任务。
open_id / name 由 Session 注入 event_payload_json，不要写死 tool_args。

**当前处于联调阶段：`filter` 被刻意收窄到测试账号（王炜博）的 open_id。**
空 `filter: {}` 会让任何一个新入职的真人都自动收到 7 张卡，联调期间这是误发风险。
上线时把 `filter` 改回 `{}` 即可对全体新人生效。

入职触发方式待核对：若真实流程是 HR 先在表里登记、通讯录后建，把本触发器停用，
改为手动或表驱动调用 `rookie_sop_card_send`（入口是独立工具，换触发方式不影响其余部分）。

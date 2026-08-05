---
name: rookie-sop-welcome
description: 通讯录新建员工时，给新人发入职 SOP 的逐项可勾选卡片
event: feishu.hr.user_created
source: feishu
filter: {}
visibility: silent
run_once: false
fire: tool
raw_event: contact.user.created_v3
tool: rookie_sop_card_send
tool_args: {}
---

向 payload.open_id 发送按 SOP 模块拆分的 multi_use 勾选卡，并为该新人建立每日 9:30 催办定时任务。
open_id / name 由 Session 注入 event_payload_json，不要写死 tool_args。

入职触发方式待核对：若真实流程是 HR 先在表里登记、通讯录后建，把本触发器停用，
改为手动或表驱动调用 `rookie_sop_card_send`（入口是独立工具，换触发方式不影响其余部分）。

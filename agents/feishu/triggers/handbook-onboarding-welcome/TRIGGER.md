---
name: handbook-onboarding-welcome
description: 通讯录新建员工时，向新员工私聊发送管理制度确认卡
event: feishu.hr.user_created
source: feishu
# 刻意为之: 通讯录新建员工没有可收窄的字段(每个新人都要发), 所以确实要放行一切。
# 空 filter 不再等于放行一切, 必须显式写; raw 路同样不能靠留空放行。
filter: {match: all}
raw_filter: {match: all}
visibility: silent
run_once: false
fire: tool
raw_event: contact.user.created_v3
tool: handbook_onboarding_send_welcome
tool_args: {}
---

向 payload.open_id 发送欢迎 + 管理制度链接 + 确认表单卡。
open_id / name 由 Session 注入 event_payload_json，无需写死 tool_args。

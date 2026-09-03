---
name: assignment-delivery-refresh
description: 定期刷新当前安排者名下任务的已读和接收进度卡
event: haitun.assignment.delivery_check
source: haitun
# 刻意为之: 本事件按 routing.open_id 分发到各人 Session, payload 只有 tick,
# 没有可收窄的字段, 所以确实要放行一切。空 filter 不再等于放行一切, 必须显式写。
filter: {match: all}
visibility: silent
run_once: false
fire: tool
tool: assignment_delivery_refresh
tool_args: {}
---

由合成事件按飞书用户路由，不经过 LLM，工具只处理 Memory 中尚未结束且未超过七天的投递记录。

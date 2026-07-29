---
name: work-assignment-delegation
description: "Use when the user wants to assign work to another person, help the recipient understand the task, or record a reviewable work assignment in Fusion Memory. Covers generic project sync, developer tasks, handoff, and follow-up without limiting the scenario to engineering work."
category: productivity
---

# 工作安排委派

当用户要把一项工作交给另一个人、希望对方理解后推进，并且要把这次安排记成可追溯记录时，使用这个 skill。

核心原则：

- 先识别缺失信息，再问清楚。
- 不能把推测写成确定事实。
- 不把场景限制在开发任务；不只限于开发任务，项目同步、交接、客户沟通、跨部门协作都适用。
- 只在事实确认后写入 Memory。
- 需要记录时，调用 `assignment_upsert` 创建或更新安排，调用 `assignment_transition` 记录确认接收、方案提交和结束状态。
- 需要查回时，调用 `assignment_get` 或 `assignment_list`。

推荐流程：

1. 识别安排者、接收者、任务目标、背景、期望结果、截止时间、原始资料链接。
2. 找出缺口，向用户确认。
3. 在用户确认后，调用 `assignment_upsert` 记录安排。
4. 如果接收者确认收到，调用 `assignment_transition`。
5. 如果接收者需要形成可评审方案，先帮助整理方案，再记录 transition。

常用工具：

- `assignment_upsert`
- `assignment_get`
- `assignment_list`
- `assignment_transition`
- `feishu_message_send`
- `feishu_message_send_card` / 现有卡片发送工具（如果当前 workspace 已提供）

输出要求：

- 简洁、可执行。
- 不暴露内部推理过程。
- 不写多余的过程性说明。
- 只在必要时追问。

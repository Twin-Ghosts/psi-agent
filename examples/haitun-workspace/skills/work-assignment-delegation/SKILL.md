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
- 需要记录时，调用 `assignment_upsert` 创建或更新安排；接收者确认时调用 `assignment_accept`，方案提交和结束状态调用 `assignment_transition`。
- 需要查回时，调用 `assignment_get` 或 `assignment_list`。

推荐流程：

1. 识别安排者、接收者、任务目标、背景、期望结果、截止时间、原始资料链接。
2. 找出缺口，向用户确认。
3. 在用户确认后，调用 `assignment_upsert` 记录安排并显式写入 `state: "assigned"`；如果已有记录仍是 `draft`，先调用 `assignment_transition` 执行 `transition_type: "assign"`。
4. 需要通过飞书交给接收者时，调用 `assignment_send_card`。该工具从 Memory 拉取权威详情，卡片把“安排者原始内容”“Agent 分析整理 (非安排者原话)”和“参考资料”分区展示，只保留“确认接收并创建飞书任务”动作。
5. 如果接收者确认收到，调用 `assignment_accept`。它校验当前飞书操作者是接收者，确认 Memory 状态，并通过 Memory 的原子发布 claim 创建、记录至多一个对应的飞书任务。
6. 如果接收者需要形成可评审方案，先帮助整理方案，再记录 transition。

接收者流程：

1. 工作安排卡片已经分区包含安排者原始内容、Agent 整理的背景/目标/缺口/风险/行动项和参考资料；只有需要刷新状态或继续讨论时才调用 `assignment_get`。
2. 不要重复输出卡片已经完整展示的详情，也不要让接收者等待模型重新组织同一份内容。
3. 明确区分事实、假设和待确认事项；缺失信息只标成缺口，不补写成事实。
4. 接收者确认收到时，调用 `assignment_accept`。成功后不再调用 `feishu_task_create` 或 `assignment_transition`，避免重复任务和重复状态迁移。
5. 需要方案时，协助接收者形成可评审方案，至少包括目标理解、影响范围、关键步骤、风险、验证方式和需要评审的问题。
6. 接收者确认方案后，调用 `assignment_transition`，其中 `transition_type: "submit_plan"`，并把方案写入 `plan`。
7. 如果接收者明确不形成方案或任务不需要方案，调用 `assignment_transition`，其中 `transition_type: "close"`，并写入 `closure_reason`。不要调用 `closed_without_plan`，Memory 没有这个 transition。

确认与发布语义：

- Memory 的“已接收”和飞书原生任务的“已发布”是两个结果。
- `assignment_accept` 先确认接收，再向 Memory 原子申请一次发布权；只有获得 claim 的 Gateway 可以创建飞书任务。不要直接调用底层发布协议。
- 飞书任务发布成功或失败后，Memory 在同一事务中终结 claim、追加 `delivery_records` 和审计事件，但不借此改变工作安排的业务状态。
- 飞书任务发布失败不撤销接收。若发布已被 claim、已经失败，或任务已创建但 Memory 未能完成记录，只能人工核对后补记，不要再次创建任务。
- 不要自动重试飞书任务创建。飞书任务接口没有客户端幂等键，未经确认的重试可能创建重复任务。
- 如果 `assignment_accept` 返回 `already_published=true`，直接视为成功，不再创建新任务。

可评审方案要求：

- 说明接收者对任务的理解，而不是替安排者新增事实。
- 列出准备采用的步骤、交付物和验收方式。
- 标出仍需安排者或评审人决策的问题。
- 不开始实施，除非用户明确要求进入实施。
- 如果方案基于假设，必须把假设放在单独小节。

场景模板：

1. 通用工作安排：优先简洁，围绕背景、结论、行动项、负责人、截止时间和来源组织。
2. 开发任务：补充影响范围、代码模块、技术约束、验证方式和评审关注点。
3. 交接或同步：强调上下文、未决事项、依赖关系和下一步接力人。

模板规则：

- 模板只改变表达和重点，不改变已确认事实。
- 模板不得改变已确认事实。
- 模板不能凭空补齐事实缺口。
- 若模板与当前事实不一致，以事实为准。

常用工具：

- `assignment_upsert`
- `assignment_get`
- `assignment_list`
- `assignment_transition`
- `assignment_send_card`
- `assignment_accept`
- `feishu_message_send`
- `feishu_message_send_card` / 现有卡片发送工具（只有 `assignment_send_card` 不满足当前卡片需求时再直接使用）

输出要求：

- 简洁、可执行。
- 不暴露内部推理过程。
- 不写多余的过程性说明。
- 只在必要时追问。

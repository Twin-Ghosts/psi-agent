---
name: company-todo-review
description: "公司 TODO 管理体系的评价回收：执行人勾选 TODO 卡交付后，给 mentor 发一张 1-5 分的评价卡（另有「打回重做」），mentor 提交后把打分与评语写进该 mentor 的 Bitable 台账，并把「评分 + 评语」追加到该人 wiki 快照页对应 todo 之后（wiki_write 只能整页覆盖，所以先 wiki_read 取回全文再回写）。agent 可给建议分但以 mentor 分为准。Use when 某人交付了 todo 要请 mentor 评价 / mentor 给了打分评语要记下来 / 要把评价回写进 wiki, 或 TODO 卡被勾选后的评价环节。采集派发在 company-todo-sync，闭环判定在 company-todo-audit。"
category: productivity
---

# 公司 TODO 评价回收（交付 → mentor 打分 → 回写 wiki）

周期流水线的第二段:把「交付了」变成「评价过且记下来了」。采集与派发在
[`company-todo-sync`],闭环判定与回流在 [`company-todo-audit`]。

一条原则贯穿本技能:**mentor 分是权威分**。agent 可以给建议分,但两者并列存放,
冲突时以 mentor 为准 —— 不许用建议分覆盖或替代。

## 1. 交付事件

执行人勾选 TODO 卡的某一行 → 框架把该行改成已完成态并原地更新卡片 →
派发到 `feishu_todo_card_tick` → 在那里把对应的飞书任务标完成。

连点会被合并成一个批(`<feishu_card_action_batch>`):**每条都要逐个处理,但只回一条
消息** —— 这是框架已有约定,不要每条都回一句。

交付状态的权威源是**飞书任务**(`completed_at` 非空),不是台账里的状态字段。查回用
`feishu_api` `GET /open-apis/task/v2/tasks/:task_guid`,task_guid 从台账「任务 GUID」列取。

## 2. 给 mentor 发评价卡

交付后给该 todo 的验收人(台账 mentor 字段)发一张评价卡,`feishu_message_send_card`:

- 按钮 1-5 分,另有「打回重做」。
- 评语走卡片表单**一次提交** —— 不要分两次问。
- 卡片里带上:负责人、todo 标题、原定截止日、实际完成时间、以及 agent 建议分及其理由。
- **私聊发给 mentor 本人**,不进群。

卡片动作回调未必接通,`value` 里要带齐 `task_guid` / 台账 `record_id` / 负责人 open_id
做兜底,别只依赖回调上下文。卡片墓碑先于业务处理器落盘,所以处理器里**先做能失败的
写操作、再回消息**:处理器失败就等于永久烧掉这次点击(表现为「点击不了」)。

## 3. mentor 提交后做两件事(两件都要做完)

### 3.1 写台账

`feishu_bitable_update_record` 写这四个字段:

| 字段 | 写什么 |
|---|---|
| mentor 打分 | mentor 给的 1-5,权威分 |
| agent 建议分 | 按「是否按期、是否一次通过、成果物是否齐备」给 1-5,仅参考 |
| mentor 评语 | **原文**,不要润色概括 —— 这段要原样回写 wiki |
| 状态 | 「已交付」;打回重做则回「进行中」 |

**列名对不上会被静默丢弃**,写前先确认台账列名与 `fields_json` 的键一致。
状态置为「已闭环」是 [`company-todo-audit`] 的事,本技能不置 —— 闭环要五要素齐备。

### 3.2 回写 wiki(顺序:先台账后 wiki)

评价要追加到该人 wiki **快照页**对应 todo 之后,也就是《张三 todo 2026-08-05》那一页,
不是汇总页。

`wiki_write` **只能整页覆盖、没有 append**,所以:

1. `wiki_read` 取回快照页全文。
2. 在对应 todo 那一条**之后**插入一行,形如
   `> mentor 评价(李四,2026-08-08):4 分。<评语原文>`。
3. **其余字节原样写回**,整页 `wiki_write`。

三条不许放宽的:
- 只插入,不改动那条 todo 本身的文字 —— 快照页是那次填报的事实。
- 同一人的同一快照页**不要并发改写**(后写整页覆盖先写)。按人串行。
- 重复提交同一条评价时,先检查该条评语是否已在页面里;已有就不重复插入(幂等)。

「评价已回写进 wiki」是闭环五要素的第 5 项,靠 `wiki_read` 命中评语文本来验证 ——
所以这一步没做完,那条 todo 永远闭不了环。

## 4. 打回重做

- 台账状态回「进行中」,mentor 打分与评语照常记(记的是这次打回的理由)。
- 评语同样回写 wiki:打回也是评价过程的一部分,不写就丢了。
- 该 todo 的飞书任务**重开**(`PATCH /open-apis/task/v2/tasks/:task_guid`,
  `task.completed_at` 置 `"0"` 并在 `update_fields` 里点名该字段 —— 空 `update_fields`
  会被硬拦,飞书返回成功但一个字段都不改)。
- 下一周期它会作为未闭环项被 [`company-todo-audit`] 带逾期天数回流。

## 5. mentor 迟迟不打分

评价卡超时未回,是闭环第 4 项永远缺、整条挂在未闭环的直接原因。处理:
提醒该 mentor 一次;仍未回则**升级提醒其上级**(上级从 wiki 人员页的「上级」区块读)。
不要替 mentor 打分,也不要用 agent 建议分顶替。

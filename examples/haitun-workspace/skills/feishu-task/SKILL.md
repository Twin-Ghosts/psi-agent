---
name: feishu-task
description: 飞书任务（task v2）接口表 —— 建任务派给人、列任务、读任务详情（含每个执行人完成情况）、改任务、标完成/重开。用 feishu_api 按表调用。含在线学习（eLearning）学习记录读取。
---

# 飞书任务接口

用 `feishu_api` 按下表调用。表里每一行都对应一个真实接口；`rules` 块是同一份知识的可执行副本，
参数不合规会在**发请求之前**被拦下来。

飞书原生任务：给人派活、带截止时间、列出来、标完成。机器人自己的 tenant token 就能用
（`task:task:write`）。

## 五个接口

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 建任务（可同时派人） | POST | `/open-apis/task/v2/tasks` | query `user_id_type`；body `summary`、`description`、`due`、`members` |
| 列任务 | GET | `/open-apis/task/v2/tasks` | query `type`、`page_size`、`completed`、`page_token` |
| 读任务详情 | GET | `/open-apis/task/v2/tasks/:task_guid` | query `user_id_type` |
| 改任务 | PATCH | `/open-apis/task/v2/tasks/:task_guid` | body `task` + `update_fields` |
| 标完成 / 重开 | PATCH | `/open-apis/task/v2/tasks/:task_guid` | body `task.completed_at` + `update_fields` |

## 时间是毫秒字符串，而且要自己算

`due` 和 `completed_at` 都是**毫秒**epoch，而且是**字符串**不是数字：

```
"due": {"timestamp": "1786323600000", "is_all_day": false}
```

秒和毫秒差三个零，传秒进去任务会落在 1970 年。当前时间在每轮对话的上下文里给了，据此换算，
不要凭印象编一个时间戳。全天任务把 `is_all_day` 设 true。

**标完成就是把 `completed_at` 写成「现在」**，重开是把它写成字符串 `"0"`：

```
标完成：body = {"task": {"completed_at": "<现在的毫秒时间戳>"}, "update_fields": ["completed_at"]}
重开：  body = {"task": {"completed_at": "0"},                 "update_fields": ["completed_at"]}
```

## PATCH 必须带 update_fields，否则什么都不会变

改任务是「字段级」的：`task` 里放新值，`update_fields` 里列出**哪些字段这次要改**。
只写 `task` 不写 `update_fields`，飞书会当成「没有要改的字段」—— **返回成功，但一个字都没改**。
这是本域唯一的静默失败，两个数组必须一一对应：

```
body = {"task": {"summary": "新标题", "due": {...}}, "update_fields": ["summary", "due"]}
```

反过来，`update_fields` 里列了但 `task` 里没给值的字段，会被**清空**——想删掉截止时间就是这么删的，
但别误伤：改标题时顺手把 `due` 写进 `update_fields`，那条截止时间就没了。

## 派人：member 对象的三个键别对调

```
{"id": "ou_xxx", "type": "user", "id_type": "open_id", "role": "assignee"}
```

- `type` 是**成员类别**（`user` / `app`），
- `id_type` 是**id 形态**（`open_id` / `user_id`），
- `role` 是 `assignee`（执行人）或 `follower`（关注人）。

把 `type` 写成 `"open_id"` 会被拒成 **1470400** —— 那是把 id 形态填进了成员类别。
执行人和关注人放在**同一个 `members` 数组**里，靠 `role` 区分，不是两个字段。

## 列任务只能列「调用身份自己的」

`type=my_tasks` 里的 "my" 指的是**发请求的那个身份**。用机器人 token 调，列出来的是机器人
负责的任务 —— **不是某个员工的任务清单**。想看某人的任务得用那个人的授权（`prefer=user` +
`user_key`）。`completed` 只接受字符串 `"true"` / `"false"`，不传表示全都要。

想知道「我派给张三的活他做完了没」，不要去列张三的任务，读任务详情即可：
`assignee_related[]` 里每个执行人各带自己的 `completed_at`，多人任务里谁做完了一目了然。
详情里的 `status` 是整个任务的状态，和单个执行人的完成情况不是一回事。

## 在线学习（eLearning）学习记录

同一份技能里顺带这一个只读接口，因为它也只是一个平铺的 GET：

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 读课程报名/学习记录 | GET | `/open-apis/elearning/v2/course_registrations` | `user_ids`（可重复）、`user_id_type`、`page_size`、`page_token` |

`user_ids` 是**可重复的 query 参数**，不是逗号分隔的一个值 —— 传数组，`feishu_api` 会把
一个列表值展开成重复的键：`{"user_ids": ["ou_a", "ou_b"]}`。不传 `user_ids` 就是全部人。

只读**报名和学习记录**（谁报了名、完成状态、进度、分数）。**建课程、发布课程、指派给全员
是在 eLearning 管理后台做的**，开放平台没有对应写接口，别去找。

```rules
- endpoint: POST /open-apis/task/v2/tasks
  token: tenant_then_user
  required: [summary]
  fields:
    user_id_type: {default: open_id, choices: [open_id, user_id, union_id]}
  pitfalls:
    - due.timestamp 是毫秒 epoch 的字符串, 传秒会落在 1970 年。
    - member 对象:type 是成员类别(user/app), id_type 是 id 形态(open_id/user_id), role 是 assignee/follower。
    - type 写成 "open_id" 会被拒成 1470400。
    - 执行人和关注人在同一个 members 数组里靠 role 区分。

- endpoint: GET /open-apis/task/v2/tasks
  token: tenant_then_user
  fields:
    type: {default: my_tasks}
    user_id_type: {default: open_id, choices: [open_id, user_id, union_id]}
    completed: {choices: ["true", "false"]}
    page_size: {default: 50, max: 100, min: 1, on_fail: "page_size 取值 1-100"}
  paginate: {items: items, page_size: 50}
  pitfalls:
    - my_tasks 是"发请求的身份"自己的任务;机器人 token 列不出某个员工的任务清单。
    - 要看某人的任务须用那个人的授权(prefer=user + user_key)。

- endpoint: GET /open-apis/task/v2/tasks/:task_guid
  token: tenant_then_user
  fields:
    user_id_type: {default: open_id, choices: [open_id, user_id, union_id]}
  pitfalls:
    - assignee_related[] 里每个执行人各带自己的 completed_at, 这才是"某人做完没"的依据。
    - task.status 是整个任务的状态, 和单个执行人的完成情况不是一回事。

- endpoint: PATCH /open-apis/task/v2/tasks/:task_guid
  token: tenant_then_user
  required: [task, update_fields]
  fields:
    user_id_type: {default: open_id, choices: [open_id, user_id, union_id]}
    update_fields: {min_items: 1, on_fail: "update_fields 不能为空, 否则飞书返回成功但一个字段都不改"}
  pitfalls:
    - 只写 task 不写 update_fields 会静默不改:返回成功, 数据没动。
    - update_fields 里列了而 task 里没给值的字段会被清空(删截止时间就是这么删的)。
    - 标完成 = completed_at 写成现在的毫秒时间戳;重开 = 写字符串 "0"。

- endpoint: GET /open-apis/elearning/v2/course_registrations
  token: tenant
  fields:
    user_id_type: {default: open_id, choices: [open_id, user_id, union_id]}
    page_size: {default: 100, max: 100, min: 1, on_fail: "page_size 取值 1-100"}
  paginate: {items: items, page_size: 100}
  pitfalls:
    - user_ids 是可重复的 query 参数, 传数组而不是逗号分隔的字符串。
    - 只读报名和学习记录;建课程/发布/指派全员在 eLearning 管理后台, 开放平台没有写接口。
```

授权与权限：任务需要 `task:task`（写要 `task:task:write`）；eLearning 那条只认机器人的
tenant token（`elearning:course_registration:read`）。任务的建/改/完成想以员工本人身份出现在
他的任务列表里，就传 `user_key` 并用 `prefer=user`；否则任务的创建者是机器人。

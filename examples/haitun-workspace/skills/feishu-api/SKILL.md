---
name: feishu-api
description: "Calling any Feishu/Lark Open Platform endpoint through the generic feishu_api tool — 通讯录/组织架构查人、考勤组与班次配置、培训报名记录、云文档全局搜索、审批实例与任务查询、日历日程查询、任务(Task)增删改查、群信息与成员、知识库空间与节点。Use when a Feishu capability has no dedicated feishu_* tool, or when the user asks 查某人信息/查部门成员/查考勤配置/搜文档/查审批状态/查日程/管任务/查群成员/查知识库. Carries the endpoint tables, the token strategy, and the rule for when a dedicated tool must be used instead."
category: integration
---

# 飞书通用 API 调用

用 `feishu_api` 打任意飞书开放平台端点。专用工具只覆盖「请求形状容易搞错」的那些
（二进制上传、表格坐标、回应 id 解析），其余端点走这里 —— 端点知识放在本文档，
不占常驻上下文。

回复用中文，除非用户明显在用其他语言。

## 先检查有没有专用工具

`feishu_api` 能打任意端点，包括写操作。**给错 URI 就是一次真实写入**，所以下面这些
必须用专用工具，不要手搓请求：

| 场景 | 用这个 | 为什么不能手搓 |
|---|---|---|
| 发图片/文件/语音/视频 | `feishu_message_send_image` / `_send_file` / `_send_audio` / `_send_video` | body 必须是真文件句柄，JSON 表达不了；`feishu_api` 会直接拒绝并指路 |
| 上传到云盘 | `feishu_drive_upload` | 同上 |
| 表格写入 | `feishu_sheet_write` / `_append` | 裸 `!A1` 区间会**静默丢数据** |
| 多维表格写入 | `feishu_bitable_*` | 列名对不上会被**静默丢弃** |
| 移除表情回应 | `feishu_message_unreact` | 要先按 emoji 解析出 reaction_id，多个命中必须拒绝 |
| OAuth 授权 | `feishu_auth_*` | 管着 UAT 存储与回调接收 |
| 发/编辑消息、卡片 | `feishu_message_send` / `_edit` / `_edit_card` | `<at>` 升级 post、卡片 update_multi 等组包细节 |
| 读/写群公告 | `feishu_chat_announcement` / `_set` / `_clear` | 公告是 **docx 文档**（不是 im/v1），根 block_id 就是 chat_id，每次写都要按 `revision_id` 乐观锁重读 |
| 改群设置 / 禁言 | `feishu_chat_update` / `feishu_chat_mute` | 加人权限与群名片权限**必须成对**；禁言根本不在群设置那个 body 里（写了会被静默忽略） |
| 解散群 / 转让群主 | `feishu_chat_dismiss` / `feishu_chat_transfer_owner` | 解散**不可逆且不保留群记录**，工具要求显式 `confirm="解散群"` |
| 群菜单 / 群标签页 | `feishu_chat_menu_*` / `feishu_chat_tab*` | 菜单是三层嵌套包装对象、带子菜单的一级菜单不能有链接；标签页 11 种类型只有 2 种能建 |
| 搜索消息 | `feishu_message_search` | 只吃 user token，且**只返回 message_id**，必须回查才有正文 |

判断方法：先用 `tool_search` 找一下有没有 `feishu_` 开头的对应工具；有就用它。

## 参数怎么填

```
feishu_api(
  method="GET",
  uri="/open-apis/contact/v3/users/:user_id",
  paths_json='{"user_id":"ou_abc"}',
  query_json='{"user_id_type":"open_id"}',
  user_key="<sender_open_id>",
)
```

- `uri` **保留 `:name` 占位符**，值放 `paths_json` —— 别自己拼进去，交给 SDK 转义。
  占位符没填会直接报 `missing_path_params`，不会打出一个 404。
- `query_json` 的值会被字符串化；列表值会重复同一个 key。
- `body_json` 只在 POST/PUT/PATCH 用。

## token 策略

- `prefer="tenant"`（默认）：先用机器人身份，只在确实被拒时回落到调用者的 user token。
  绝大多数查询用这个。
- `prefer="user"`：直接要求调用者授权。用于**读某人自己的数据**（本人日程、本人待办）
  和**应归属于本人**的写入。
- `user_key` 一律传 `<feishu_context>` 里的 `sender_open_id` —— 不传就没有可回落的 token。
- `identity="user"` / `"bot"` 只在创建有归属的内容时才需要显式选。

## 端点表

### 通讯录 / 组织架构

| 要什么 | method + uri | 说明 |
|---|---|---|
| 查一个人 | `GET /open-apis/contact/v3/users/:user_id` | `query_json='{"user_id_type":"open_id"}'`；拿手机/邮箱/部门 |
| 查部门成员 | `GET /open-apis/contact/v3/users/find_by_department` | `query: department_id, page_size(≤50), page_token` |
| 按名字全局搜人 | `GET /open-apis/search/v1/user` | `query: query, page_size`；**只支持 user token**，必须 `prefer="user"` + `user_key` |
| 部门列表 | `GET /open-apis/contact/v3/departments/:department_id/children` | `query: page_size` |

根部门 id 是 `0`。`user_id_type` 不传默认可能不是 open_id，查人时显式写上。

### 考勤

| 要什么 | method + uri |
|---|---|
| 打卡记录 | `POST /open-apis/attendance/v1/user_tasks/query` — body: `{"user_ids":[...],"check_date_from":20260801,"check_date_to":20260807}`，query: `{"employee_type":"employee_id"}` |
| 考勤组列表 | `POST /open-apis/attendance/v1/groups/list` |
| 考勤组配置 | `GET /open-apis/attendance/v1/groups/:group_id` |
| 班次列表 | `POST /open-apis/attendance/v1/shifts/list` |
| 班次配置 | `GET /open-apis/attendance/v1/shifts/:shift_id` |

日期是 **整数** `YYYYMMDD`，不是字符串。`user_ids` 要的是 employee_id 体系，跟 open_id 不同。

### 云文档搜索

| 要什么 | method + uri |
|---|---|
| 全局搜文档 | `POST /open-apis/suite/docs-api/search/object` — body: `{"search_key":"关键词","count":20}` |

**只支持 user token**：`prefer="user"` + `user_key`，搜到的是那个人有权限看的东西。

### 审批（查询部分）

| 要什么 | method + uri |
|---|---|
| 我的待办 | `POST /open-apis/approval/v4/tasks/query` — body: `{"user_id":"ou_...","page_size":20}` |
| 实例列表 | `POST /open-apis/approval/v4/instances/query` — body 带 `approval_code` / 时间区间 |
| 实例详情 | `GET /open-apis/approval/v4/instances/:instance_id` |
| 审批定义 | `GET /open-apis/approval/v4/approvals/:approval_code` | 拿表单字段结构，代人提交前必读 |

发起、同意/拒绝、订阅仍用 `feishu_approval_create` / `_decide` / `_subscribe`。

### 日历

| 要什么 | method + uri |
|---|---|
| 日程列表 | `GET /open-apis/calendar/v4/calendars/:calendar_id/events` — query: `start_time`/`end_time`（**秒级时间戳字符串**）、`page_size` |
| 主日历 id | `POST /open-apis/calendar/v4/calendars/primary` | `prefer="user"` 拿本人主日历 |

建日程仍用 `feishu_calendar_create_event` / `_create_per_person`。

### 任务 (Task v2)

| 要什么 | method + uri |
|---|---|
| 建任务 | `POST /open-apis/task/v2/tasks` — body: `{"summary":"...","due":{"timestamp":"..."},"members":[{"id":"ou_...","role":"assignee"}]}` |
| 查任务 | `GET /open-apis/task/v2/tasks/:task_guid` |
| 列任务 | `GET /open-apis/task/v2/tasks` — query: `page_size`, `completed` |
| 改任务 | `PATCH /open-apis/task/v2/tasks/:task_guid` — body: `{"task":{...},"update_fields":["summary"]}` |
| 完成任务 | `PATCH` 同上，`update_fields:["completed_at"]`，`completed_at` 为毫秒字符串 |

改任务**必须**带 `update_fields`，不带则什么都不会变。

### 群 / 知识库

| 要什么 | method + uri |
|---|---|
| 搜我在的群 | `GET /open-apis/im/v1/chats/search` — query: `query`, `page_size` |
| 群成员 | `GET /open-apis/im/v1/chats/:chat_id/members` — query: `page_size`(≤100), `page_token` |
| 知识空间列表 | `GET /open-apis/wiki/v2/spaces` — query: `page_size` |
| 空间节点 | `GET /open-apis/wiki/v2/spaces/:space_id/nodes` — query: `parent_node_token`, `page_size` |
| 节点详情 | `GET /open-apis/wiki/v2/spaces/get_node` — query: `token`（wiki node_token） |

wiki 节点的 `obj_token` 才是文档 id，读内容要用它而不是 `node_token`。
建群拉人用 `feishu_chat_create`；建 wiki 文档用 `feishu_wiki_create_doc*`。

**群的运营几乎都有专用工具了，别手搓**：群列表 `feishu_chat_list`、群公告
`feishu_chat_announcement`/`_set`/`_clear`、群设置 `feishu_chat_update`、禁言
`feishu_chat_mute`、转让群主 `feishu_chat_transfer_owner`、解散群
`feishu_chat_dismiss`、群菜单 `feishu_chat_menu_*`、群标签页 `feishu_chat_tab*`。
这些端点各自都有一个「照着文档写也会错」的地方（公告是 docx 文档且按 revision 乐观锁、
禁言不在群设置那个 body 里、加人权限和群名片权限必须成对、解散不可逆），
所以护栏在工具里，不在这张表里。

### 培训

| 要什么 | method + uri |
|---|---|
| 课程报名记录 | `GET /open-apis/elearning/v2/course_registrations` — query: `page_size`, `user_id_type` |

## 分页

返回里有 `has_more: true` 就带上 `page_token` 再问一次。`page_size` 各端点上限不同
（多数 50，群成员 100），超了会报错而不是截断。

## 报错怎么读

`feishu_api` 会把已知错误码翻成 `hint` 字段 —— 先读它。常见的：

- `99991663` / `99991661`：token 无效或缺失 → 传 `user_key`，或该端点只吃 user token 时加 `prefer="user"`
- `1254302` / `1254303`：没权限 → 需要在应用后台加 scope，或让本人授权
- `230002`：没有该资源权限 → 机器人不在群里/不是文档协作者
- `code="use_dedicated_tool"`：打到了上传端点，按返回的 `tool` 字段换工具
- `code="missing_path_params"`：`uri` 里的 `:name` 没在 `paths_json` 填

权限不足时不要反复重试同一个调用 —— 先用 `feishu_auth_*` 确认授权状态。

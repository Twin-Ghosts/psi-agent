---
name: feishu-bitable
description: 飞书多维表格（bitable）接口表 —— 建 base、建/删数据表、建/删字段、读写记录、复制 base、高级权限自定义角色。用 feishu_api 按表调用。批量写记录、改单元格、条件搜索、建带列的表、改字段定义仍然是专用工具。
---

# 飞书多维表格接口

用 `feishu_api` 按下表调用。表里每一行都对应一个真实接口；`rules` 块是同一份知识的可执行副本，
参数不合规会在**发请求之前**被拦下来。

`app_token` 是 `feishu.cn/base/<app_token>` 里的那一段。wiki 链接（`feishu.cn/wiki/...`）要先用
`feishu_api` 打 `wiki/v2/spaces/get_node` 换：`obj_type` 是 `bitable` 时它的 `obj_token` 才是 `app_token`。

**写操作要带 `prefer=user` 和本人 `user_key`。** 这不是可选项：base 归谁所有由它决定，
`identity` 空着且这个人从没被问过时，请求**不会发出**，返回 `need_identity_choice` —— 那时候去问用户
「这个表放你名下还是机器人名下」，再调 `feishu_identity_set`。归属不能替人猜。

**这个域最大的坑：列名对不上会被静默丢弃。** 往一张列叫「导师」的表里写 `{"Mentor": "李四"}`，
飞书返回 `code: 0`、告诉你写成功了，格子是空的。所以凡是**按列名写值**的接口都留了专用工具，
它们会先把列名跟表里的真实字段核一遍（见下面「这些不走通用接口」）。

## base 本身

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 新建一个 base | POST | `/open-apis/bitable/v1/apps` | `name`、`folder_token`、`time_zone` |
| 读 base 元信息 | GET | `/open-apis/bitable/v1/apps/:app_token` | 无 |
| 改名 / 开关高级权限 | PUT | `/open-apis/bitable/v1/apps/:app_token` | `name`、`is_advanced` |
| 复制整个 base | POST | `/open-apis/bitable/v1/apps/:app_token/copy` | `name`、`folder_token`、`without_content` |

新建返回的 `default_table_id` 是自动建出来的第一张表，只有一个占位索引列。返回里的 `app_token`
拼成 `https://feishu.cn/base/<app_token>` 就是分享链接，发给用户。

读元信息有两个实际用途：确认 `app_token` 真的指向一个能访问的 base，以及看 `is_advanced` ——
自定义角色要求先开高级权限。

`copy` 是**模板**用法：维护一份建好的台账，按项目/按月复制，而不是每次重新建表建列。
`without_content=true` 只复制结构（空表、同样的列），这通常才是模板想要的。限流 20 次/分钟，
正在被复制的 base 返回 1254036，等一下重试。

高级权限**开不了**的两种情况：base 在 wiki 里，或者被嵌在文档/表格里（1254301）。

## 数据表

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 列出 base 里的数据表 | GET | `/open-apis/bitable/v1/apps/:app_token/tables` | `page_size` |
| 批量建空表（只有名字） | POST | `/open-apis/bitable/v1/apps/:app_token/tables/batch_create` | `tables`（`[{"name":"合同"}]`） |
| 批量删表（连数据一起） | POST | `/open-apis/bitable/v1/apps/:app_token/tables/batch_delete` | `table_ids` |

批量建出来的表只有一个占位索引列，列要另外加。要**一次把列定义好**，用
`feishu_bitable_create_table` —— 那通常才是你想要的。一次最多 50 张，一个 base 最多 100 张表。
表名 1-100 字符，不能含 `/ \ ? * : [ ]`。

删表是**不可逆**的，连行带列一起消失。只想清空数据保留结构用 `feishu_bitable_clear_table`，
只想删列用下面的删字段。一个 base 必须留至少一张表，删最后一张飞书拒绝（1254034）。
删掉有数据的表之前先跟用户确认：传 `user_key`，本人会私聊收到 6 位确认码，
带 `confirm=<那6位数字>` 才会真的删。

## 字段（列）

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 列出表的字段 | GET | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields` | `page_size` |
| 加一个字段 | POST | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields` | `field_name`、`type`、`property`、`ui_type` |
| 删一个字段 | DELETE | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields/:field_id` | 无 |

**写值之前先列一遍字段**，把列名的准确写法拿到手 —— 这是防住「静默丢弃」最省事的一步。
返回里的 `type` 是整数，对照下面的字段类型表读。

删字段会**丢掉这一列的所有数据**。索引（主）列删不掉，飞书返回 1254046。
只是列建错了想改，用 `feishu_bitable_update_field` 改而不是删了重建 —— 删重建会把数据丢光。

`property` 是类型专属设置：单选/多选 `{"options":[{"name":"高","color":0}]}`（color 0-54）、
数字 `{"formatter":"0.00"}`、日期 `{"date_formatter":"yyyy-MM-dd"}`、人员多选 `{"multiple":true}`。
`ui_type` 是显示变体，如 `Progress`、`Currency`、`Rating`、`Email`、`Barcode`。

## 记录（行）

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 翻页读全表 | GET | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records` | `page_size`、`filter`、`sort`、`field_names` |
| 读一行 | GET | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id` | `automatic_fields` |
| 建一行 | POST | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records` | `fields` |
| 批量删行 | POST | `/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_delete` | `records`（record_id 数组） |

读一行加 `with_shared_url=true` 能拿到这一行的直达链接。`automatic_fields=true` 额外返回
创建人/创建时间/最后修改人/修改时间。

要**按内容找行**（"张三那几行"、"状态是进行中且金额大于一万的"）用 `feishu_bitable_search_records`，
它是飞书文档指定的拿 `record_id` 的办法。上面这个 GET 的 `filter` 只覆盖简单情况。

一次最多删 500 行；超了就分几次调。一张表最多 20000 行。

建一行的 `fields` 是 `{列名: 值}`。**列名对不上会被静默丢弃且返回成功** —— 一行以上、
或者拿不准列名写法，改用 `feishu_bitable_create_records`，它会先核对列名。

值的写法跟着列类型走：文本给字符串，数字给数字，单选给选项名，多选给名字数组，
日期给**毫秒**时间戳，复选框给 true/false，人员给 `[{"id":"ou_..."}]`，
超链接给 `{"text":"...","link":"https://..."}`，附件给 `[{"file_token":"..."}]`，
关联给 record_id 数组，地理位置给 `"lat,lng"`。
公式、查找引用、创建时间、自动编号这些**算出来的列写不进去**。

## 高级权限与自定义角色

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 建自定义角色 | POST | `/open-apis/bitable/v1/apps/:app_token/roles` | `role_name`、`table_roles` |
| 列出自定义角色 | GET | `/open-apis/bitable/v1/apps/:app_token/roles` | `page_size` |
| 把人加进角色 | POST | `/open-apis/bitable/v1/apps/:app_token/roles/:role_id/members` | `member_id`、`member_id_type` |

这是「一个 base，不同角色看到不同内容」的做法：全员都能打开同一个 base，各自只看见自己那一片。
**要求先开高级权限**（`PUT /apps/:app_token` 带 `is_advanced=true`，或先 GET 看 `is_advanced`）。

`table_roles` 一张表一个对象：`[{"table_id":"tblXXX","table_perm":1}]`，
`table_perm` 0=无权限、1=可看、2=可编辑自己加的记录、4=可编辑全部。
按行控制加 `"rec_rule":{"conditions":[...],"perm":1}`，按列控制加 `"field_perm":{"fld1":1,"fld2":2}`。

## 字段类型表

`type` 是整数，建字段和读字段都用它。**19（查找引用）建不出来**，API 不支持。

| type | 含义 | type | 含义 |
|---|---|---|---|
| 1 | 文本 | 20 | 公式 |
| 2 | 数字 | 21 | 双向关联 |
| 3 | 单选 | 22 | 地理位置 |
| 4 | 多选 | 23 | 群组 |
| 5 | 日期 | 1001 | 创建时间 |
| 7 | 复选框 | 1002 | 最后更新时间 |
| 11 | 人员 | 1003 | 创建人 |
| 13 | 电话 | 1004 | 修改人 |
| 15 | 超链接 | 1005 | 自动编号 |
| 17 | 附件 | 18 | 单向关联 |

一张表的**第一个字段是索引（主）列**，只接受 **1、2、5、13、15、20、22**，其余类型飞书返回 1254012。
建表时把文本键列放第一个。索引列也不能删。

## 这些不走通用接口

| 工具 | 为什么必须是工具 |
|---|---|
| `feishu_bitable_create_records` | 批量写行。**写之前**把所有列名跟表里的真实字段核一遍，写完再拿飞书回显的字段跟请求比一次 —— 两头都查才挡得住「写了 22 行、每行只有键列有值」。按 500 一批发。 |
| `feishu_bitable_update_record` | 改单元格。同样是写前核列名 + 写后比回显。飞书对不认识的列名既不写也不报错。 |
| `feishu_bitable_update_records` | 批量改行，同上，按 1000 一批发。 |
| `feishu_bitable_search_records` | 条件搜索。`conditions` 是**嵌套数组**，rules 只能校验顶层字段，钻不进去；而且 `view_id` 和 `filter`/`sort` 同时给时飞书**悄悄忽略 view_id** 去搜全表，工具直接拒绝这个组合。 |
| `feishu_bitable_create_table` | 建带列的表。`fields` 是嵌套数组，要逐个检查 type、挡掉 19、并确认第一个字段能当索引列 —— 都在数组里面，rules 校验不到。 |
| `feishu_bitable_update_field` | 改字段定义。飞书这个接口是**整体替换**，没传的东西会被清掉，所以工具先把当前定义读回来补齐（同类型时连 `property` 一起带上），否则改个名字就把单选选项全清了。 |
| `feishu_bitable_clear_table` | 清空整表。要先翻页把所有 `record_id` 收齐再分批删 —— 两个不同接口串起来，一行表格表达不了。 |

```rules
- endpoint: POST /open-apis/bitable/v1/apps
  token: user
  required: [name]
  fields:
    # pattern 而不是 max: max 用 float() 转换值, 对字符串一律放过, 长度根本没被拦。
    name: {pattern: '^[\s\S]{1,255}$', on_fail: 'base 名字最长 255 字符'}

- endpoint: GET /open-apis/bitable/v1/apps/:app_token

- endpoint: PUT /open-apis/bitable/v1/apps/:app_token
  token: user
  fields:
    name:
      # 长度用 pattern 而不是 max(max 对字符串一律放过);两条共用一个 on_fail,
      # 所以文案把长度和非法字符都写上。
      pattern: '^[\s\S]{1,100}$'
      forbid: '[?/\\*:\[\]]'
      on_fail: 'base 名字最长 100 字符, 且不能含 ? / \ * : [ ] (飞书返回 1254031)'
  pitfalls:
    - 'base 在 wiki 里或被嵌进文档时开不了高级权限 (1254301)。'
    - '飞书先改名再切权限, 可能只成功一半; 看返回里的 changed。'

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/copy
  token: user
  pitfalls:
    - 'without_content=true 只复制结构, 模板用法通常要这个。'
    - '限流 20 次/分钟; 正在复制中返回 1254036, 等一下重试。'

- endpoint: GET /open-apis/bitable/v1/apps/:app_token/tables
  paginate: {items: items, page_size: 100}

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables
  token: user
  prefer_tool: feishu_bitable_create_table
  hard: true
  why: 建表要逐个检查 fields 里的 type、挡掉建不出来的 19、并确认第一个字段能当索引列; 这些都在嵌套数组里面, 端点表校验钻不进去。

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/batch_create
  token: user
  required: [tables]
  fields:
    tables: {max_items: 50}
  pitfalls:
    - '建出来的表只有一个占位索引列; 要一次把列定义好请用 feishu_bitable_create_table。'
    - '表名 1-100 字符, 不能含 / \ ? * : [ ]。'
    - '一个 base 最多 100 张表。'

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/batch_delete
  token: user
  required: [table_ids]
  fields:
    table_ids: {max_items: 50}
  confirm: DELETE_BITABLE_TABLES
  pitfalls:
    - '删表连行带列一起消失, 不可逆。只清数据用 feishu_bitable_clear_table, 只删列用 DELETE .../fields/:field_id。'
    - '一个 base 必须留至少一张表, 删最后一张返回 1254034。'

- endpoint: GET /open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields
  paginate: {items: items, page_size: 100}

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields
  token: user
  required: [field_name, type]
  fields:
    type:
      choices: [1, 2, 3, 4, 5, 7, 11, 13, 15, 17, 18, 20, 21, 22, 23, 1001, 1002, 1003, 1004, 1005]
      on_fail: 'type 不是可建的字段类型; 19 (查找引用) API 建不出来, 其余见技能里的字段类型表。'
  pitfalls:
    - 'property 是类型专属设置: 单选 {"options":[{"name":"高","color":0}]}, 数字 {"formatter":"0.00"}, 日期 {"date_formatter":"yyyy-MM-dd"}, 人员多选 {"multiple":true}。'

- endpoint: DELETE /open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields/:field_id
  token: user
  pitfalls:
    - '删字段会丢掉这一列的所有数据; 列建错了想改用 feishu_bitable_update_field, 别删了重建。'
    - '索引 (主) 列删不掉, 返回 1254046。'

- endpoint: PUT /open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields/:field_id
  token: user
  prefer_tool: feishu_bitable_update_field
  hard: true
  why: 这个接口是整体替换, 没传的字段会被清掉; 工具会先把当前定义读回来补齐 (同类型时连 property 一起带上), 直接调会把单选选项/数字格式清空。

- endpoint: GET /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records
  paginate: {items: items, page_size: 100}
  fields:
    page_size: {max: 500}
  pitfalls:
    - '要按内容找行请用 feishu_bitable_search_records —— 飞书文档把它定为拿 record_id 的办法, 这里的 filter 只覆盖简单情况。'

- endpoint: GET /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id
  fields:
    with_shared_url: {default: true, in: query}
  pitfalls:
    - '带 with_shared_url=true 才有这一行的直达链接(默认已带上); automatic_fields=true 才返回创建人/修改时间。'

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records
  token: user
  required: [fields]
  pitfalls:
    - '列名对不上会被飞书静默丢弃且返回成功。一行以上、或拿不准列名写法, 用 feishu_bitable_create_records (它先核对列名)。'
    - '先 GET .../fields 把列名的准确写法拿到手。'
    - '公式/查找引用/创建时间/自动编号这些算出来的列写不进去。'

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_create
  token: user
  prefer_tool: feishu_bitable_create_records
  hard: true
  why: 飞书对不认识的列名既不写也不报错, 直接返回 code 0; 工具写前核对列名、写后拿回显跟请求比, 才挡得住「写了 22 行、每行只有键列有值」。

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_update
  token: user
  prefer_tool: feishu_bitable_update_records
  hard: true
  why: 同 batch_create —— 列名静默丢弃只能靠写前核对 + 写后比回显两头查。

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_delete
  token: user
  required: [records]
  fields:
    records: {max_items: 500}
  pitfalls:
    - 'records 是 record_id 的字符串数组; 一次最多 500 行, 超了分几次调。'

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/search
  token: user
  prefer_tool: feishu_bitable_search_records
  hard: true
  why: conditions 是嵌套数组, 端点表只能校验顶层字段; 而且 view_id 和 filter/sort 同时给时飞书悄悄忽略 view_id 去搜全表, 工具会拒绝这个组合而不是让你以为搜的是那个视图。

- endpoint: PUT /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id
  token: user
  prefer_tool: feishu_bitable_update_record
  hard: true
  why: 改单元格靠写前核列名 + 写后比回显; 飞书对不认识的列名既不写也不报错, 直接调会得到「改成功了、格子没变」。

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/roles
  token: user
  required: [role_name, table_roles]
  pitfalls:
    - '要求先开高级权限: PUT /apps/:app_token 带 is_advanced=true。'
    - 'table_perm 0=无权限 1=可看 2=可编辑自己加的记录 4=可编辑全部。'

- endpoint: GET /open-apis/bitable/v1/apps/:app_token/roles
  paginate: {items: items, page_size: 100}

- endpoint: POST /open-apis/bitable/v1/apps/:app_token/roles/:role_id/members
  token: user
  required: [member_id]
  fields:
    member_id_type: {choices: [open_id, union_id, user_id], default: open_id, in: query}
```

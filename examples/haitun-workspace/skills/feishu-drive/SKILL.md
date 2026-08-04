---
name: feishu-drive
description: 飞书云盘（drive）接口表 —— 列文档评论、列评论回复、删除云文档/文件夹。用 feishu_api 按表调用。发评论、回复评论、下载、上传仍然是专用工具。
---

# 飞书云盘接口

用 `feishu_api` 按下表调用。表里每一行都对应一个真实接口；`rules` 块是同一份知识的可执行副本，
参数不合规会在**发请求之前**被拦下来。

本域**读**的部分进了表格，**写**的部分留在专用工具里，分界线只有一条：
写评论的 body 是嵌套的 `elements` 数组，而校验只能按顶层键查、钻不进数组 ——
正文拼错了飞书照收，评论发出去是空的却返回成功。这属于静默失败那一类，所以不表格化。

三个概念先分清：

- `file_token` 是**云文档本身**的 token，从它的网址里取（`feishu.cn/docx/<token>`）。
- `file_type` 必须跟 token 的真实类型一致，写错飞书会报"找不到"而不是纠正你。
- `comment_id` 是**一条评论主楼**的 id，从评论列表里取；主楼底下的每条回复另有 `reply_id`。

## 评论

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 列整篇文档的评论（主楼） | GET | `/open-apis/drive/v1/files/:file_token/comments` | `file_type`、`is_whole`、`is_solved` |
| 列某条评论下的回复 | GET | `/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies` | `file_type` |
| 发一条整篇评论 | POST | `/open-apis/drive/v1/files/:file_token/comments` | **用 `feishu_drive_add_comment`** |
| 在评论下回复（可@人） | POST | `/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies` | **用 `feishu_drive_reply_comment`** |

`is_whole=true` 只列"整篇文档"评论，也就是没有挂在具体段落上的那些。
划词评论（挂在某段文字上的）不带这个参数才看得见，两者在飞书里是同一个接口的两种视图。

`is_solved` 不传是"全都要"，传 `false` 只看未解决的，传 `true` 只看已解决的 ——
不传和传 `false` 是不同的意思，别用它当默认值。

## 文件

| 要做的事 | method | endpoint | 关键参数 |
|---|---|---|---|
| 删除云文档 / 文件夹（进回收站） | DELETE | `/open-apis/drive/v1/files/:file_token` | `type`（必填） |
| 下载文件到本地 | GET | `/open-apis/drive/v1/medias/:file_token/download` | **用 `feishu_file_download`** |
| 上传本地文件到云盘 | POST | `/open-apis/drive/v1/medias/upload_all` | **用 `feishu_drive_upload`** |
| 上传本地文件（files 端点） | POST | `/open-apis/drive/v1/files/upload_all` | **用 `feishu_drive_upload`** |

删除是**可恢复**的，进回收站不是物理删除。但调用方必须是文件所有者，或者对父文件夹有编辑/管理权限
—— 所以删用户自己的文件要带上他的 `user_key` 以他的身份删。

删**文件夹**跟删文档不一样：飞书是异步做的，响应里回一个 `task_id`，删除并没有当场完成。
要确认结果得拿这个 task_id 去查任务状态。删文档没有这个字段。

要删的东西在**知识库（wiki）里**的话，`file_token` 不能直接用 wiki 节点的 token：
先用 `feishu_api` 打 `wiki/v2/spaces/get_node` 换出 `obj_token` / `obj_type`，再删那个。

## 为什么下载和上传不在表格里

这两个是本域唯一两处通用工具**表达不了**的地方，方向正好相反：

- **下载**的产物是**磁盘上的一个文件**，不是 JSON 响应。`feishu_file_download` 从二进制响应里取字节写到
  本地路径，还管着 tenant→用户授权的两段降级（机器人看不见的文件才回退到用户身份）。它另有一条
  `is_url=True` 的分支专门收审批表单里那种**直链**（约 12 小时失效，过期要重读审批实例换新链接）。
- **上传**的 body 里要放**真的文件句柄**，JSON 字符串表达不了。`medias/upload_all` 和 `files/upload_all`
  两个端点已经被通用工具硬拒，绕不过去。超过 20MB 要走分片上传，`feishu_drive_upload` 会直接告诉你大小。

```rules
- endpoint: GET /open-apis/drive/v1/files/:file_token/comments
  token: tenant_then_user
  required: [file_type]
  fields:
    file_type: {choices: [docx, doc, sheet, bitable, file], on_fail: "file_type 必须跟 file_token 的真实类型一致"}
    is_whole: {choices: ['true', 'false']}
    is_solved: {choices: ['true', 'false']}
    page_size: {default: 50, max: 100}
  paginate: {items: items, page_size: 50}
  pitfalls:
    - is_whole=true 只列整篇评论; 划词评论(挂在某段文字上)不带这个参数才看得见
    - is_solved 不传是"全都要", 传 false 只看未解决 —— 不传和传 false 不是一回事

- endpoint: POST /open-apis/drive/v1/files/:file_token/comments
  prefer_tool: feishu_drive_add_comment
  hard: true
  why: >
    body 是 reply_list.replies[].content.elements[] 三层嵌套, 校验按顶层键查、钻不进数组,
    正文拼错飞书照收 —— 评论发出去是空的却返回成功。工具替你拼这层结构。

- endpoint: GET /open-apis/drive/v1/files/:file_token/comments/:comment_id/replies
  token: tenant_then_user
  required: [file_type]
  fields:
    file_type: {choices: [docx, doc, sheet, bitable, file]}
    page_size: {default: 50, max: 100}
  paginate: {items: items, page_size: 50}
  pitfalls:
    - comment_id 是主楼 id(从评论列表取); 主楼下每条回复另有 reply_id, 两者不能互换

- endpoint: POST /open-apis/drive/v1/files/:file_token/comments/:comment_id/replies
  prefer_tool: feishu_drive_reply_comment
  hard: true
  why: >
    同上的 elements 嵌套, 而且@人要在 elements 最前面插一个 person 节点;
    顺序错了@不生效但依然返回成功。

- endpoint: DELETE /open-apis/drive/v1/files/:file_token
  token: tenant_then_user
  required: [type]
  fields:
    type:
      choices: [file, docx, doc, sheet, bitable, mindnote, slides, folder, shortcut]
      on_fail: "type 必填且必须是这 9 种之一"
  pitfalls:
    - 进回收站, 可恢复; 但调用方必须是所有者或对父文件夹有编辑权限, 删用户的文件要带他的 user_key
    - 删文件夹是异步的, 响应回一个 task_id, 删除没有当场完成; 删文档没有这个字段
    - wiki 里的文档不能直接用节点 token, 先 `feishu_api` 打 `wiki/v2/spaces/get_node` 换出 obj_token 再删

- endpoint: POST /open-apis/drive/v1/medias/upload_all
  prefer_tool: feishu_drive_upload
  hard: true
  why: body 里要真的文件句柄, JSON 字符串表达不了; 硬发出去会拿到 400 boundary not found。

- endpoint: POST /open-apis/drive/v1/files/upload_all
  prefer_tool: feishu_drive_upload
  hard: true
  why: 同 medias/upload_all, body 要真文件句柄。

- endpoint: GET /open-apis/drive/v1/medias/:file_token/download
  prefer_tool: feishu_file_download
  hard: true
  why: >
    产物是磁盘上的文件而不是 JSON 响应, 通用工具表达不了落盘;
    工具还管着 tenant→用户授权的两段降级, 以及审批直链(约 12 小时失效)那条 is_url 分支。
```

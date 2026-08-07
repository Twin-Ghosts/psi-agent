---
name: rookie-doc-sync
description: 新人入职清单文档被编辑时，把勾选状态同步回明细表
event: haitun.rookie.doc_edited
source: feishu
filter: {}
visibility: silent
run_once: false
fire: tool
raw_event: drive.file.edit_v1
tool: rookie_sop_sync_doc
tool_args: {}
---

新人在自己的清单文档里勾了项 → 读回 todo 块的 done 状态 → 写进明细表 → 重算总览。
document_id 由 Session 注入 event_payload_json，不要写死 tool_args。

`filter: {}` 在这里是**刻意留空**的，与 rookie-sop-welcome 不同：文档变更事件的
document_id 只有落在 state 的 docs 索引里才会被处理，映射不到就直接报错返回，
所以不存在误同步别人文档的风险，不需要按 open_id 收窄。

`fire=tool`：到点不经过 LLM。同步是纯数据搬运，让模型参与只会增加延迟和不确定性。

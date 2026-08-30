# 国数周报 Agent — Demo Workspace

可跑通的周报问答 demo：agent 包经 MCP 连接周报取数服务，用自建 mock 库打通全链路。
入口组的真实服务就绪后，改 `GUOSHU_WEEKLY_MCP_URL` 即可切换，agent 侧不改代码。

对应方案文档《国家数据集团周报 Agent 开发方案》第 1 期「骨架打通」。

## 快速开始

前置：MySQL 8.4 已起，`weekly_mock` 库已导入（见「数据层准备」）。

```bash
# 1. 起 mock 取数服务
cd mock-mcp
python server.py --port 18900

# 2. 另开一个终端，跑契约测试
cd ..
export GUOSHU_WEEKLY_MCP_URL=http://127.0.0.1:18900/mcp
export GUOSHU_WEEKLY_MCP_TOKEN=demo-token
python tests/smoke_test.py
```

预期 `265/265 passed`。

## 数据层准备

用原生 MySQL 8.4 而非翻译层：396 道参考 SQL 全部原样可跑（100%），
执行计划来自与生产同一个优化器。

```bash
# 免安装 ZIP，整个装在一个目录里，卸载 = 停服务 + 删目录
# https://cdn.mysql.com//Downloads/MySQL-8.4/mysql-8.4.11-winx64.zip

mysqld --defaults-file=my.ini --initialize-insecure --console
mysqld --defaults-file=my.ini --console

mysql -u root -e "CREATE DATABASE weekly_mock CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;"
mysql -u root --default-character-set=utf8mb4 weekly_mock < weekly_mock-full-20260817.sql

# 只读用户：让「只读」由数据库强制，而不只是注释里的声明
mysql -u root -e "
CREATE USER 'weekly_ro'@'127.0.0.1' IDENTIFIED BY 'weekly-ro-2026';
GRANT SELECT ON weekly_mock.* TO 'weekly_ro'@'127.0.0.1';"
```

`my.ini` 要点：`character-set-server = utf8mb4`、`collation-server =
utf8mb4_0900_ai_ci`（与 dump 一致，中文不乱码）、`bind-address = 127.0.0.1`
（本机演示，零远程暴露面）。

**不要写 `default_authentication_plugin`** —— 该变量在 MySQL 8.4 已移除
（属 8.0 时代），写了会让 `--initialize` 直接失败。8.4 默认就是
`caching_sha2_password`，无需覆盖。

导入后校验行数请用 `SELECT COUNT(*)`，**不要信
`information_schema.TABLE_ROWS`** —— 那是 InnoDB 估算值，实测 158 行的表读出 14。

预期 12 张表：task 158、task_attachment 543、task_board 2、task_category 47、
task_group_detail 55、task_group_progress_history 404、task_milestone 602、
task_progress 1068、task_progress_import 20、task_workflow_action 1613、
task_workflow_submission 470、task_year_goal 387。

### 接进 psi-agent 对话

```bash
export GUOSHU_WEEKLY_MCP_URL=http://127.0.0.1:18900/mcp
export GUOSHU_WEEKLY_MCP_TOKEN=demo-token

psi-agent gateway --default-agent examples/guoshu-weekly-workspace
```

Windows 上 `--listen` 必须带 `http://` 前缀；从 Git Bash 启动网关会让子进程挂
`0xC0000142`，改用 PowerShell。

## 结构

```
guoshu-weekly-workspace/
├── systems/system.py          # system prompt：取数纪律 + 口径硬约束 + 角色化输出
├── tools/
│   ├── _weekly_config.py      # 环境变量解析、URL 校验（/mcp + 非 loopback 强制 HTTPS）
│   ├── _weekly_mcp.py         # MCP 客户端（惰性建连、supervisor 线程、只读重试）
│   ├── weekly_schema.py       # 结构与字段字典
│   ├── weekly_query.py        # 任务查询（列表 / 详情）
│   ├── weekly_aggregate.py    # 聚合、快照时间、导入对账
│   ├── weekly_progress.py     # 进展、里程碑、审批、附件、健康自检
│   ├── weekly_group.py        # 集团组专表（明细 / 负责人 / 历史进展 / 统计）
│   └── weekly_goal.py         # 年度目标与里程碑（覆盖率 / 缺口 / 完成率 / 错配）
├── mock-mcp/                  # 仅 demo 用，不属于交付物
│   ├── _db.py                 # MySQL 连接（只读用户，密码不进日志）
│   ├── _store.py              # 只读查询层 + 口径规则 + 字段管控
│   └── server.py              # 31 个语义化取数工具（Streamable HTTP MCP）
└── tests/
    ├── smoke_test.py          # 265 条契约断言，不花模型 token
    └── baseline.py            # 396 题准确率基线（LLM 判定）
```

## 取数契约

31 个语义化工具，SQL 与口径规则固化在服务端，agent 侧不产 SQL：

| 工具 | 用途 |
|------|------|
| `weekly_schema` | 看板、分类树、**表字段清单**、口径说明 |
| `weekly_task_query` | 按看板/分类/状态/负责人/关键词查正式任务 |
| `weekly_task_detail` | 单任务详情（明细 + 近期进展 + 年度目标） |
| `weekly_progress_history` | 进展版本回溯，`version_no` 倒序 |
| `weekly_progress_coverage` | 进展历史覆盖度（行数/任务数/起止/最大版本）、未发布进展分档、期号缺号、**每任务最新一期的下一步安排**、最新一期缺下一步计数 |
| `weekly_aggregate` | 按 board/category/**primary_category**/status/project_group/owner/**workflow_status** 聚合，`top` 硬切前 N 组 |
| `weekly_scale` | 多子表一次成表：规模（里程碑/附件/年度目标，**全部 `COUNT(DISTINCT)` 防 JOIN 放大**）/ 完备度（有该项的任务数）/ 进展密度（分母含零期任务） |
| `weekly_field_completeness` | 字段填报完整度（R-07/R-19），字段走白名单 |
| `weekly_task_ranking` | 按子表计数排名（附件/进展/里程碑/提交单） |
| `weekly_rank` | 排名并列口径三选一：硬切 N 条 / 保留并列 / 每组各自第一，可限定看板并回显 `total_count` |
| `weekly_milestone_query` | 里程碑清单（已复核正式任务口径），单任务按 `sort_order` 编排 |
| `weekly_workflow_query` | 审批动作流水（谁在哪个环节做了什么），可按 action/看板过滤、按任务聚合次数；**按环节+动作两维分档**、人均动作数（分子分母同回） |
| `weekly_submission_query` | 审批提交单（`round_no` / `status` / 填报人），可查任务状态与最新单状态不一致；**按类型分档**（initial / progress）、O2OA 外部标识填充率、在途单（按成员枚举而非取反）、**在途总数/按看板分档/一任务多单**、**会签需求与按人分档/会签耗时对比**、人均轮次、已发布进展单 vs 已发布进展行 |
| `weekly_owner_roles` | 按角色分别计数（as_owner / as_lead / any_role） |
| `weekly_person_stats` | 人员统计（任务量/人均/独苗/跨组/双角色/标识写法/填报人/审核人/自审） |
| `weekly_attachment_stats` | 附件统计（容量/类型/最大/上传人/挂载去向/**零附件任务清单**/在途提交单/逐月/软删/孤儿） |
| `weekly_attachment_query` | 附件清单（不含 `storage_path`），可按任务或**看板**筛 |
| `weekly_import_audit` | 导入批次对账，`reconcile_rows` 反查实际落库行核对声明值，`orphans` 查批次引用完整性 |
| `weekly_freshness` | 各看板最新进展时间 |
| `weekly_health` | 连通性自检与各表行数 |
| `weekly_progress_range` | 时间窗内的进展（全表跨任务，可按月/季/任务分组计数），`peak` 直接给峰值组 |
| `weekly_task_lifecycle` | 任务创建/发布的时间分布与建到发的时长 |
| `weekly_freshness_distribution` | 新鲜度分桶（30/90/180 天）、自定义天窗、时间漂移检出、滞后清单（含从未上报）、近期上报清单 |
| `weekly_approval_turnaround` | 审批时效（汇总/按看板/最慢/待审积压） |
| `weekly_group_detail_query` | 集团组明细（目标成果/实施举措/进度成效/完成时间文本/多值负责人），可按 `status` 与 `non_empty` 交叉筛矛盾数据 |
| `weekly_group_owner_query` | 集团组按牵头人或项目负责人查任务（多值精确匹配） |
| `weekly_group_history` | 集团组历史进展（专表，可按年/月/季/任务/填报人/**滞报天数**/**提交单挂接率**分组，天窗可用 `last_days` 或**日历月** `last_months`） |
| `weekly_group_stats` | 集团组统计（负责人构成/分隔符写法/一栏几人/完成时间**写法分档**与去重取值/字数/附件/期数/成效一致性） |
| `weekly_year_goal_query` | 年度目标条目（按任务/年份，带里程碑摘要） |
| `weekly_year_goal_stats` | 年度目标统计（分年/覆盖率/缺口，可限在办/缺口分组/跨年跨度/连续设标） |
| `weekly_milestone_stats` | 里程碑统计（完成率/多维分解/软删审计/每任务分布/任务与里程碑错配） |

`weekly_workflow_query` 与 `weekly_submission_query` 是两张表，不可互相替代：
动作流水**聚合不出**提交单状态。混用会答出「5 个提交单全部通过」而真值是
「2 个全部驳回」。

集团组的进展**不在** `task_progress` 里，而在自己的 `task_group_progress_history`
（362 条已发布）。所以 `weekly_progress_history` / `weekly_progress_range` 查集团
任务一律返回空，必须走 `weekly_group_history`。同理，目标成果、实施举措、完成时间
文本和多值负责人只在 `task_group_detail`，`weekly_task_query` 没有这些列。

每个返回都自带 `caliber`（本次生效口径）与 `snapshot_note`（演示数据声明），
agent 据此给出依据、也据此判断不可答。

### 服务端固化的口径

| 规则 | 落点 |
|------|------|
| R-01 正式任务口径 | `_store.formal_task_clause()`，被所有任务类查询强制附加 |
| R-02 / R-08 空分组保留 | 聚合走 LEFT JOIN，口径条件写在 ON 上 |
| R-04 / R-14 敏感字段 | `opinion` / `review_comment` 凭 bearer token 分级，见下节 |
| R-07 / R-19 填报完整度 | `weekly_field_completeness`，空串按未填计入 missing |
| R-09 / R-10 导入对账 | 批次数 vs 去重快照日期数 vs 去重导入时间数 |
| R-11 / R-13 多值负责人 | 去空格后匹配；分管领导按填法枚举计数 |
| R-12 完成时间是文本 | 任务详情的顶层 `caliber` 无条件声明 |
| R-17 里程碑复核 | JOIN 回 task 表复核正式任务口径 |
| 附件路径不外泄 | `storage_path` 在 `BLOCKED_FIELDS`，不进任何返回 |
| 相对时间窗锚定快照日 | `_store.AS_OF = 2026-08-15`，非 `CURDATE()`，见下节 |
| 集团历史双闸门 | `_store.group_history_gate()`，任务侧 R-01 + 行级 `is_published = 1` |
| 集团组多值负责人 | `FIND_IN_SET` 逐元素匹配，不用 `LIKE` 以免跨人误命中 |
| 没设目标算 0 不算没有 | 覆盖率/缺口走 `NOT EXISTS` 全表口径，`JOIN` 会把 11 个缺口任务整行丢掉 |
| 里程碑完成状态是两值码 | `status` 只有 1（未完成）/ 2（已完成），无「进行中」档，别按三态解读 |
| 里程碑软删审计看全表 | `deleted` 口径故意不加任务闸门：问的是表本身，按任务过滤会少算 |
| 提交单状态另有一套码值 | 已发布叫 `published` 不叫 `approved`；给值域外的词过滤会静默失效，工具随结果回 `status_domain` 并点名该条件未生效 |
| 附件大小是字节不换算 | `file_size` 原样报出，换成「约 3.8MB」即与精确值不一致 |
| 组内人数由服务端去重 | `group_by=project_group` 直接给 `lead_owner_count` / `project_owner_count`，让模型数人名会数错 |
| 姓名列填满≠ID 列填满 | `project_owner_name` 128 条全满而 `project_owner_id` 只有 119 条；只看姓名列会如实答「无缺失」，与真值 9 条相反 |
| 填报闸门与审核闸门不同 | 填报统计加 `p.is_published = 1`，审核统计**不加**——审过但未发布的进展同样算审过 |
| 多值负责人栏「单人」是一档 | 分隔符统计里 `单人无分隔符` 与逗号、顿号并列成档，不是缺失 |
| 附件挂载一条只进一档 | 优先级 进展 > 提交单 > 任务本体，各档相加等于总数；「在途」按提交单自己的 `status <> 'published'` 判 |
| 孤儿行必须走 NOT EXISTS | 附件 `task_id` 对不上任务表的有 3 条，用 JOIN 查会恒等于 0 |
| 软删审计不加任务闸门 | 问的是表本身（543 行中 33 条已删），按任务过滤会少算 |
| 多子表同查必须逐项去重 | `weekly_scale` 三张子表一次 JOIN，每个计数都是 `COUNT(DISTINCT ...)`：不去重时技术组 294 个里程碑会被附件行数乘成 1363；口径同时给出自检法——各组里程碑相加应等于全库总数 474 |
| 子表条数≠有该项的任务数 | `mode=totals` 答「多少个里程碑」（294），`mode=completeness` 答「多少任务有里程碑」（80），拿一个答另一个必错 |
| 「在途」按成员枚举不用取反 | `status <> 'published'` 会把 `cancelled` 那 1 张算进来（60 vs 59）：它既未发布也不在途 |
| 外部标识三列填充率不同 | `o2_process_id` / `o2_work_id` 各 460 而 `o2_task_id` 只有 60，拿一列代答另一列会把缺失率答反 |
| 期数按 `version_no` 去重 | 一期可能有多行，`COUNT(*)` 会把「几期」答成「几行」 |
| 声明值必须反查才算对账 | `changed_tasks` 是批次自己声明的数字，`reconcile_rows=True` 才反查实际落库；`LEFT JOIN` 不可换 `JOIN`——声明 43 实落 0 那批正是最极端的对不上 |
| 「最长的标识」问标识不问任务 | 同一个标识挂 3 个任务只算一个标识，不去重会返回 128 行、并列几个也数不出来 |
| 逐任务清单看 `total_count` | 问「每个任务各多少」时 `top` 只是页大小，`total_count` 与 `row_count` 相等才说明列全了 |
| 审批流转状态是唯一不加发布闸门的分组 | `group_by=workflow_status` 若照例先筛 `published`，七档只剩一档 128，未发布的 22 条全部消失；它与 `group_by=status`（未开始/进行中/已完成/已停用）是两套词汇、两个总体（150 vs 128） |
| 「未发布」用取反不用相加 | `workflow_status <> 'published'` 得 22；把在途各档相加会漏掉 `cancelled` 那 1 条——它既未发布也不在途 |
| 「最近三个月」是日历月不是 90 天 | `last_months=3` 从 2026-08-15 回到 05-15，`last_days=90` 落在 05-17，中间三行让 5 月由 16 变 13；两个参数互斥，同时给会得出第三个窗口，服务端直接报错 |
| 「最新一期」按 `version_no` 定序 | `weekly_progress_coverage` 的 `latest_round` 用 `ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY version_no DESC, id DESC)`；按 `progress_date` 取最新会错——补报的老期号可能日期更晚，而不收敛则 16 期任务出 16 行、最老那期的下一步被当成现在的安排 |
| 完成时间「写法分档」≠去重取值数 | 分档得 6 类（46 条各进一档，相加等于 `total_count`），去重取值是 28 个，两者差一个量级；判别顺序即优先级，`2026年6月底` 固定进含「底」档 |
| 滞报天数取最后一次上报 | `grouping=lag` 用 `MAX(report_time)` 与快照日之差，用 `MIN` 会把老任务全排到榜首；同时回 `total_tasks`，因为从未上报的任务不在这张表里，拿行数当集团组任务数会少算 |
| 孤儿引用与「未走导入」是两件事 | `orphans=True` 按 `NOT EXISTS` 判 `import_id` 有值却查不到批次，结果 0 即引用完整；`import_id IS NULL` 的 120 条是手工填报，单列为 `rows_without_import`，混进孤儿数会把它们全报成异常 |
| 0 是结论不是空结果 | 孤儿数 0、最新一期缺下一步 0，口径里直接写明「这是结论本身，不要换口径重算」，否则模型会反复改条件去凑非零 |
| 提交单不加任务发布闸门 | `by_kind` 得 312 progress + 150 initial = 462；加上发布闸门会缩成 310/128，把未发布任务的提交单一起吞掉。在途任务的提交单同样是提交单 |
| 「一个都没有」用 `NOT EXISTS` 一次列全 | `zero_attachment` 直接给 22 条零附件任务，并另给分母 128；缺这一档时模型只能对 128 个任务逐个调 `weekly_attachment_query` 看谁返回空 |
| 看板在 `task` 上不在附件行上 | 按看板筛附件必须 JOIN 回 task（`weekly_attachment_query` 的 `board` 参数），并顺带带出 `task_name`；否则「集团组有哪些附件」只能按 46 个任务逐个调，还算不出看板总数 52 |
| 计数题一律服务端聚合，不许翻清单手数 | 清单封顶 200 行，手数只看得到第一页——基线里模型自己写过「无法精确求出全库总数」。在途 61、动作 1578、集团历史 404 都远超 200，所以各自都有一次成型的聚合档 |
| 「需会签」与「正在会签」是两个问题 | `sign_summary` 的 `need_sign = 1` 有 155 张（另 307 张不需，合计 462），而在途 `status = 'signing'` 只有 9 张；拿后者答前者会少一个量级 |
| `rejected` 也是在途的一档 | `inflight_by_board` 九档相加等于 61；漏掉 `rejected` 则集团组少 4、技术组少 9 |
| 耗时均值只算已完结的单 | `sign_turnaround` 要求 `completed_at` 与 `submitted_at` 均非空，两档 274 + 128 = 402 小于总数 462；未完结的单没有耗时，硬塞进分母会把均值拉低 |
| 会签人空值是「没有会签人」 | `by_signer` 排除 `signer_name IS NULL`：空值不代表某人签了 0 单，混进来会多出一个不存在的「人」 |
| 人均类指标分子分母同回 | `rounds_per_task` 3.08 = 462 / 150、`actions_per_task` 10.52 = 1578 / 150，分母都是「有记录的任务数」而非已发布的 128；只回均值时模型会自己拿别处的任务数去除 |
| 同一动作在不同环节各自计数 | `by_node_action` 按 `node_type` + `action` 两维分 6 档；只按 `action` 分会把 955 条 `approved` 揉成一档，答不了「哪个环节驳回得多」（`audit/rejected` 13） |
| 已发布进展「单」与「行」不同表 | 提交单侧 272（只数 `submission_kind = 'progress'`，含 initial 会变 400），`task_progress` 侧 943；再并入集团组专表会得到 1305，三个数答的是三个问题 |
| 挂接率的分母不加行级发布闸门 | `by=linkage` 走全部 404 行而非过闸的 362 行；`linked_rows = 0` 是结论——集团成效历史与审批提交单没有外键落库，不是查不到 |

### 相对时间窗以快照日为基准

「最近 30 天」「今年以来」这类问法，基准是数据快照日 `2026-08-15`，
不是机器墙钟。数据止于 `progress_date` 2026-08-01，而墙钟已经走过去了：
按当前时间算窗口会静默滑出数据区间，答出一个比真值小的数。

锚点固定在服务端，模型因此既不需要知道今天几号、也无法用自己的日期替换它。
每个返回的 `caliber` 会写明本次生效的窗口与基准日。

### 敏感字段权限分级

R-04/R-14 要的是「按权限返回」。一律遮蔽同样不满足需求——那等于这条 P0 能力
没实现，而且不可测。判定依据是传输层的 `Authorization` 头，**不是模型说的话**：

| 凭证 | `opinion` / `review_comment` |
|------|------|
| `GUOSHU_WEEKLY_MOCK_TOKEN`（默认 `demo-token`） | 遮蔽为「[按权限不展示]」 |
| `GUOSHU_WEEKLY_MOCK_ADMIN_TOKEN`（默认 `demo-admin-token`） | 返回原文 |

`caliber` 字段会如实说明本次凭证拿到的是哪一档。agent 侧的 token 来自启动方的
环境变量，**模型无法自选凭证提权**——这是刻意的，用户或提示词都不能放宽它。
生产环境把这个 header 换成 OA 身份 + 行级策略即可，判定位置不变。

## Demo 与生产的差距

以下都是**有意未做**，不是遗漏。按方案文档的分期推进：

| 项 | demo 现状 | 生产需要 |
|----|-----------|----------|
| 数据源 | 本机 MySQL 8.4 + weekly_mock | 入口组 MCP + oa_biz 真实库 |
| 鉴权 | 单个进程级 token | per-user token map + BFF 身份映射 |
| 数据权限 | 敏感字段按 token 两档分级 | 按 OA 真实身份做行级权限 |
| 前端 | 无（经 psi-agent 既有接口） | 专建对话应用 + BFF（方案第六章） |
| 材料生成 | 无 | 报告下载与图表（P1，第 5 期） |
| 评测 | 265 条契约断言 + 396 题基线 | 再加 200 题真实库集 + 多轮追问集 |

### mock 数据层的两处不可外推

- **性能**：引擎虽与生产同为 MySQL 8.4，但数据量（1.1 MB）、网络与并发都不同，
  `≤10s / ≤30s` 的验收仍须在真实库重测。
- **脏值口径**：R-11（分管领导多种填法）在干净的 mock 数据上测不出真实价值，
  要等真实库适配。

`gold_sql` 在本机 MySQL 上的可跑率是 **396/396（100%）**，含两道查
`information_schema` 的权限边界题——它们能跑，但 Agent 侧仍应判为不可答。

## 切到真实服务

```bash
export GUOSHU_WEEKLY_MCP_URL=https://weekly.example.internal/mcp
export GUOSHU_WEEKLY_MCP_TOKEN=<入口组签发>
```

`mock-mcp/` 整个目录不参与交付。agent 侧**不含任何 mock 专属分支逻辑**——
这是刻意的，否则切真实库时会带出隐藏路径。

## 配置

| 变量 | 必需 | 说明 |
|------|------|------|
| `GUOSHU_WEEKLY_MCP_URL` | 是 | 路径必须是 `/mcp`；非 loopback 强制 HTTPS |
| `GUOSHU_WEEKLY_MCP_TOKEN` | 是 | bearer token，由启动方提供 |
| `GUOSHU_WEEKLY_MCP_TIMEOUT_SECONDS` | 否 | 默认 30，限 0.1~120 |
| `GUOSHU_WEEKLY_MCP_MAX_RETRIES` | 否 | 默认 2，限 0~5，仅读操作重试 |
| `GUOSHU_WEEKLY_MYSQL_HOST` | 否 | mock 库地址，默认 `127.0.0.1` |
| `GUOSHU_WEEKLY_MYSQL_PORT` | 否 | 默认 `3306` |
| `GUOSHU_WEEKLY_MYSQL_USER` | 否 | 默认 `weekly_ro`（只读） |
| `GUOSHU_WEEKLY_MYSQL_PASSWORD` | 否 | 只读用户口令 |
| `GUOSHU_WEEKLY_MYSQL_DB` | 否 | 默认 `weekly_mock` |

Agent 不读、不写、不打印 token，不改 `.env`，不向用户索要凭证。
连不上就如实报错——**没有本地兜底**，也不得启动本地周报服务。

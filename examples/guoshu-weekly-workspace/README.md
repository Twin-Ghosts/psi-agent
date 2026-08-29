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

预期 `75/75 passed`。

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
│   └── weekly_group.py        # 集团组专表（明细 / 负责人 / 历史进展 / 统计）
├── mock-mcp/                  # 仅 demo 用，不属于交付物
│   ├── _db.py                 # MySQL 连接（只读用户，密码不进日志）
│   ├── _store.py              # 只读查询层 + 口径规则 + 字段管控
│   └── server.py              # 24 个语义化取数工具（Streamable HTTP MCP）
└── tests/
    ├── smoke_test.py          # 75 条契约断言，不花模型 token
    └── baseline.py            # 396 题准确率基线（LLM 判定）
```

## 取数契约

24 个语义化工具，SQL 与口径规则固化在服务端，agent 侧不产 SQL：

| 工具 | 用途 |
|------|------|
| `weekly_schema` | 看板、分类树、**表字段清单**、口径说明 |
| `weekly_task_query` | 按看板/分类/状态/负责人/关键词查正式任务 |
| `weekly_task_detail` | 单任务详情（明细 + 近期进展 + 年度目标） |
| `weekly_progress_history` | 进展版本回溯，`version_no` 倒序 |
| `weekly_progress_coverage` | 进展历史覆盖度（行数/任务数/起止/最大版本） |
| `weekly_aggregate` | 按 board/category/status/project_group/owner 聚合 |
| `weekly_field_completeness` | 字段填报完整度（R-07/R-19），字段走白名单 |
| `weekly_task_ranking` | 按子表计数排名（附件/进展/里程碑/提交单） |
| `weekly_milestone_query` | 里程碑清单（已复核正式任务口径） |
| `weekly_workflow_query` | 审批动作流水（谁在哪个环节做了什么） |
| `weekly_submission_query` | 审批提交单（`round_no` / `status` / 填报人） |
| `weekly_owner_roles` | 按角色分别计数（as_owner / as_lead / any_role） |
| `weekly_attachment_query` | 附件清单（不含 `storage_path`） |
| `weekly_import_audit` | 导入批次对账 |
| `weekly_freshness` | 各看板最新进展时间 |
| `weekly_health` | 连通性自检与各表行数 |
| `weekly_progress_range` | 时间窗内的进展（全表跨任务，可按月/季/任务分组计数） |
| `weekly_task_lifecycle` | 任务创建/发布的时间分布与建到发的时长 |
| `weekly_freshness_distribution` | 新鲜度分桶（30/90/180 天）、自定义天窗、时间漂移检出 |
| `weekly_approval_turnaround` | 审批时效（汇总/按看板/最慢/待审积压） |
| `weekly_group_detail_query` | 集团组明细（目标成果/实施举措/进度成效/完成时间文本） |
| `weekly_group_owner_query` | 集团组按牵头人或项目负责人查任务（多值精确匹配） |
| `weekly_group_history` | 集团组历史进展（专表，可按年/月/季/任务/填报人分组） |
| `weekly_group_stats` | 集团组统计（负责人构成/完成时间格式/字数/附件/期数） |

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
| 评测 | 44 条契约断言 + 396 题基线 | 再加 200 题真实库集 + 多轮追问集 |

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

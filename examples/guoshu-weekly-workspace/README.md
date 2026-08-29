# 国数周报 Agent — Demo Workspace

可跑通的周报问答 demo：agent 包经 MCP 连接周报取数服务，用自建 mock 库打通全链路。
入口组的真实服务就绪后，改 `GUOSHU_WEEKLY_MCP_URL` 即可切换，agent 侧不改代码。

对应方案文档《国家数据集团周报 Agent 开发方案》第 1 期「骨架打通」。

## 快速开始

```bash
# 1. 构建 mock 数据层（把 MySQL dump 转成 SQLite，一次性）
cd mock-mcp
python build_sqlite.py ~/Downloads/weekly_mock-full-20260817.sql

# 2. 起 mock 取数服务
python server.py --port 18900

# 3. 另开一个终端，跑契约测试
cd ..
export GUOSHU_WEEKLY_MCP_URL=http://127.0.0.1:18900/mcp
export GUOSHU_WEEKLY_MCP_TOKEN=demo-token
python tests/smoke_test.py
```

预期 `26/26 passed`。

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
│   └── weekly_progress.py     # 进展、里程碑、审批、附件、健康自检
├── mock-mcp/                  # 仅 demo 用，不属于交付物
│   ├── build_sqlite.py        # MySQL dump → SQLite
│   ├── _sqlite_compat.py      # MySQL 函数与 INTERVAL 语法兼容层
│   ├── _store.py              # 只读查询层 + 口径规则 + 字段管控
│   └── server.py              # 11 个语义化取数工具（Streamable HTTP MCP）
└── tests/smoke_test.py        # 26 条契约断言，不花模型 token
```

## 取数契约

11 个语义化工具，SQL 与口径规则固化在服务端，agent 侧不产 SQL：

| 工具 | 用途 |
|------|------|
| `weekly_schema` | 看板、两级分类树、字段字典与口径说明 |
| `weekly_task_query` | 按看板/分类/状态/负责人/关键词查正式任务 |
| `weekly_task_detail` | 单任务详情（明细 + 近期进展 + 年度目标） |
| `weekly_progress_history` | 进展版本回溯，`version_no` 倒序 |
| `weekly_aggregate` | 按 board/category/status/project_group/owner 聚合 |
| `weekly_milestone_query` | 里程碑清单（已复核正式任务口径） |
| `weekly_workflow_query` | 审批提交与动作流水 |
| `weekly_attachment_query` | 附件清单（不含 `storage_path`） |
| `weekly_import_audit` | 导入批次对账 |
| `weekly_freshness` | 各看板最新进展时间 |
| `weekly_health` | 连通性自检与各表行数 |

每个返回都自带 `caliber`（本次生效口径）与 `snapshot_note`（演示数据声明），
agent 据此给出依据、也据此判断不可答。

### 服务端固化的口径

| 规则 | 落点 |
|------|------|
| R-01 正式任务口径 | `_store.formal_task_clause()`，被所有任务类查询强制附加 |
| R-02 / R-08 空分组保留 | 聚合走 LEFT JOIN，口径条件写在 ON 上 |
| R-04 / R-14 敏感字段 | `opinion` / `review_comment` 按权限返回「[按权限不展示]」 |
| R-09 / R-10 导入对账 | 批次数 vs 去重快照日期数 vs 去重导入时间数 |
| R-11 / R-13 多值负责人 | 去空格后匹配；分管领导按填法枚举计数 |
| R-12 完成时间是文本 | 任务详情的顶层 `caliber` 无条件声明 |
| R-17 里程碑复核 | JOIN 回 task 表复核正式任务口径 |
| 附件路径不外泄 | `storage_path` 在 `BLOCKED_FIELDS`，不进任何返回 |

## Demo 与生产的差距

以下都是**有意未做**，不是遗漏。按方案文档的分期推进：

| 项 | demo 现状 | 生产需要 |
|----|-----------|----------|
| 数据源 | SQLite（本机无 MySQL/Docker） | 入口组 MCP + oa_biz 真实库 |
| 鉴权 | 单个进程级 token | per-user token map + BFF 身份映射 |
| 数据权限 | 敏感字段一律遮蔽 | 按 OA 真实身份做行级权限 |
| 前端 | 无（经 psi-agent 既有接口） | 专建对话应用 + BFF（方案第六章） |
| 材料生成 | 无 | 报告下载与图表（P1，第 5 期） |
| 评测 | 26 条契约断言 | 396 题 + 200 题 LLM 跑分基线 |

### mock 数据层的两处不可外推

- **性能**：SQLite 单机查询远快于真实库，`≤10s / ≤30s` 的验收必须在真实库重测。
- **脏值口径**：R-11（分管领导多种填法）在干净的 mock 数据上测不出真实价值，
  要等真实库适配。

`gold_sql` 在 SQLite 上的可跑率是 394/396；余下 2 题查 `information_schema`，
属权限边界题，本就该判不可答。

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
| `GUOSHU_WEEKLY_MOCK_DB` | 否 | mock 服务的 SQLite 路径 |

Agent 不读、不写、不打印 token，不改 `.env`，不向用户索要凭证。
连不上就如实报错——**没有本地兜底**，也不得启动本地周报服务。

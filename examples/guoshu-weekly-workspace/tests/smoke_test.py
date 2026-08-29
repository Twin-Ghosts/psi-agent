"""Deterministic contract tests for the guoshu-weekly demo.

These run without an LLM: they exercise the MCP client and the service's caliber
rules directly, so a regression in the取数 contract is caught without spending
model tokens.  The LLM-dependent accuracy baseline (396/200 questions) is a
separate harness -- see README.

Run (with the mock service already up):
    GUOSHU_WEEKLY_MCP_URL=http://127.0.0.1:18900/mcp \
    GUOSHU_WEEKLY_MCP_TOKEN=demo-token \
    python tests/smoke_test.py
"""

# ruff: noqa: RUF001, RUF003  中文口径文案与注释里的全角标点是给人看的正文, 不能换成半角。
# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

WORKSPACE = Path(__file__).resolve().parent.parent
REPO_SRC = WORKSPACE.parent.parent / "src"
sys.path.insert(0, str(REPO_SRC))

from psi_agent.session.tool_registry import ToolRegistry  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((PASS if condition else FAIL, name, detail))


def _first(result: dict[str, Any], field: str) -> Any:
    """First row's ``field``, or ``None`` when the result came back empty."""
    rows = result.get("rows") or []
    return rows[0].get(field) if rows else None


async def _call(registry: ToolRegistry, tool: str, **kwargs: Any) -> dict[str, Any]:
    func = registry.get(tool)
    if func is None:
        raise AssertionError(f"tool not registered: {tool}")
    return json.loads(await func(**kwargs))


async def _probe_with_token(token: str) -> dict[str, Any] | None:
    """Call the service directly with a different bearer token.

    The workspace client takes its token from the environment on purpose -- the
    agent must not be able to pick its own credential -- so exercising the
    elevated branch means bypassing the client, not adding a parameter to it.
    """
    url = os.environ.get("GUOSHU_WEEKLY_MCP_URL", "")
    if not url:
        return None
    try:
        async with (
            httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as http_client,
            streamable_http_client(url, http_client=http_client) as (read, write, *_),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("weekly_workflow_query", {"task": "2", "limit": 3})
            # Content blocks are a union; only TextContent carries .text, so read
            # it defensively rather than assuming the first block is text.
            text = next((getattr(b, "text", "") for b in result.content or [] if getattr(b, "text", "")), "")
            return json.loads(text) if text else None
    except Exception:
        return None


async def run() -> int:
    registry = await ToolRegistry.load(WORKSPACE / "tools", "smoke")

    expected_tools = {
        "weekly_schema",
        "weekly_task_query",
        "weekly_task_detail",
        "weekly_progress_history",
        "weekly_progress_range",
        "weekly_task_lifecycle",
        "weekly_freshness_distribution",
        "weekly_approval_turnaround",
        "weekly_aggregate",
        "weekly_milestone_query",
        "weekly_workflow_query",
        "weekly_submission_query",
        "weekly_owner_roles",
        "weekly_attachment_query",
        "weekly_field_completeness",
        "weekly_progress_coverage",
        "weekly_task_ranking",
        "weekly_import_audit",
        "weekly_group_detail_query",
        "weekly_group_owner_query",
        "weekly_group_history",
        "weekly_group_stats",
        "weekly_freshness",
        "weekly_health",
    }
    loaded = set(registry.tools)
    check(
        "所有 24 个工具注册，且无 helper 泄漏为工具",
        loaded == expected_tools,
        f"缺 {sorted(expected_tools - loaded)}；多 {sorted(loaded - expected_tools)}",
    )

    health = await _call(registry, "weekly_health")
    check("weekly_health 连通", health.get("ok") is True, str(health.get("error", ""))[:120])
    if health.get("ok") is not True:
        report()
        return 1
    check(
        "mock 库 12 张表齐全",
        health.get("table_count") == 12,
        f"table_count={health.get('table_count')}",
    )

    # --- envelope shape -----------------------------------------------------
    agg = await _call(registry, "weekly_aggregate", group_by="status")
    check(
        "返回是解包后的信封（不是 JSON 字符串套 JSON）",
        isinstance(agg.get("rows"), list) and "result" not in agg,
        f"keys={list(agg)[:8]}",
    )
    check(
        "每个结果自带 caliber 口径元信息",
        bool(agg.get("caliber")),
        str(agg.get("caliber"))[:100],
    )
    check(
        "每个结果自带演示数据声明",
        "演示数据" in str(agg.get("snapshot_note", "")),
        str(agg.get("snapshot_note"))[:80],
    )

    # --- R-01 formal-task caliber ------------------------------------------
    check(
        "R-01 正式任务口径已固化并回传",
        "workflow_status = 'published'" in str(agg.get("caliber")) and "is_deleted = 0" in str(agg.get("caliber")),
        str(agg.get("caliber"))[:120],
    )
    statuses = {r["group_name"]: r["cnt"] for r in agg["rows"]}
    total_by_status = sum(statuses.values())
    boards = await _call(registry, "weekly_aggregate", group_by="board")
    total_by_board = sum(r["cnt"] for r in boards["rows"])
    check(
        "两种分组维度的正式任务总数一致",
        total_by_status == total_by_board,
        f"status 合计={total_by_status} board 合计={total_by_board}",
    )

    # --- R-02 / R-08 empty groups survive ----------------------------------
    categories = await _call(registry, "weekly_aggregate", group_by="category")
    check(
        "R-02/R-08 LEFT JOIN 保留空分组（存在 cnt=0 的分类）",
        any(r["cnt"] == 0 for r in categories["rows"]),
        f"分类数={len(categories['rows'])} 零任务分类数={sum(1 for r in categories['rows'] if r['cnt'] == 0)}",
    )

    # --- permission gating (R-04 / R-14) -----------------------------------
    workflow = await _call(registry, "weekly_workflow_query", limit=5)
    opinions = [r.get("opinion") for r in workflow.get("rows", [])]
    check(
        "R-04/R-14 审批意见按权限遮蔽",
        bool(opinions) and all(o == "[按权限不展示]" for o in opinions),
        f"samples={opinions[:2]}",
    )

    # --- blocked fields never cross the boundary ---------------------------
    attachments = await _call(registry, "weekly_attachment_query", limit=5)
    rows = attachments.get("rows", [])
    check(
        "storage_path 绝不外泄",
        bool(rows) and all("storage_path" not in r for r in rows),
        f"字段={list(rows[0]) if rows else []}",
    )
    # Only the data rows matter here: the caliber text legitimately *names*
    # storage_path to state that it is withheld.
    rows_only = json.dumps(rows, ensure_ascii=False)
    check(
        "附件数据行中不含 storage_path（口径说明里提及是正常的）",
        "storage_path" not in rows_only,
    )

    detail = await _call(registry, "weekly_task_detail", task="行业可信数据空间建设")
    check(
        "任务详情不含 storage_path / payload",
        "storage_path" not in json.dumps(detail, ensure_ascii=False)
        and '"payload"' not in json.dumps(detail, ensure_ascii=False),
    )
    check(
        "R-12 completion_time 标注为文本不可运算",
        "不可做日期运算" in json.dumps(detail, ensure_ascii=False),
        "缺少 R-12 口径提示",
    )

    # --- permission tier is decided by the bearer token --------------------
    # R-04/R-14 say "by permission", so BOTH branches must be exercisable:
    # blanket redaction fails the requirement as surely as blanket exposure, and
    # makes the capability untestable. The deciding input is the transport's
    # Authorization header -- nothing the model says can widen it.
    workflow = await _call(registry, "weekly_workflow_query", task="2", limit=3)
    caliber_text = str(workflow.get("caliber", ""))
    check(
        "敏感字段权限状态在 caliber 中如实声明",
        "按权限展示" in caliber_text and ("无敏感字段权限" in caliber_text or "有敏感字段权限" in caliber_text),
        caliber_text[:120],
    )
    elevated = await _probe_with_token(os.environ.get("SMOKE_ADMIN_TOKEN", "demo-admin-token"))
    if elevated is None:
        check("提权凭证可读到 opinion 原文", False, "提权探测失败（服务未起或 token 不符）")
    else:
        opinions = [r.get("opinion") for r in elevated.get("rows", [])]
        revealed = [o for o in opinions if o and o != "[按权限不展示]"]
        check(
            "提权凭证可读到 opinion 原文（权限分级真的分级了）",
            bool(revealed),
            f"opinions={json.dumps(opinions, ensure_ascii=False)[:110]}",
        )
        check(
            "普通凭证与提权凭证结果确有差异",
            [r.get("opinion") for r in workflow.get("rows", [])] != opinions,
        )

    # --- truncation is reported, not silent --------------------------------
    small = await _call(registry, "weekly_task_query", board="tech", limit=2)
    check(
        "截断显式上报 has_more + total_count",
        small.get("has_more") is True
        and isinstance(small.get("total_count"), int)
        and small["total_count"] > small["row_count"],
        f"row_count={small.get('row_count')} total={small.get('total_count')} has_more={small.get('has_more')}",
    )

    # --- progress ordering and draft exclusion -----------------------------
    history = await _call(registry, "weekly_progress_history", task="行业可信数据空间建设", limit=10)
    versions = [r["version_no"] for r in history.get("rows", [])]
    check(
        "进展版本按 version_no 倒序（第一条即当期）",
        versions == sorted(versions, reverse=True) and bool(versions),
        f"versions={versions[:5]}",
    )
    check(
        "默认只返回正式发布的进展（is_published=1）",
        all(r.get("is_published") == 1 for r in history.get("rows", [])),
    )

    # --- milestone re-checks the formal caliber (R-17) ----------------------
    milestones = await _call(registry, "weekly_milestone_query", year="2026", limit=5)
    check(
        "R-17 里程碑关联任务表复核正式任务口径",
        milestones.get("ok") is True and "is_deleted = 0" in str(milestones.get("caliber")),
        str(milestones.get("caliber"))[:110],
    )

    # --- import reconciliation (R-09 / R-10) -------------------------------
    audit = await _call(registry, "weekly_import_audit", limit=5)
    recon = audit.get("reconciliation", {})
    check(
        "R-09/R-10 导入批次对账字段齐全",
        {"batch_count", "distinct_dates", "distinct_import_times"} <= set(recon),
        f"recon={recon}",
    )

    # --- submission forms: a separate table from the action log -------------
    # Regression guard for a real defect: resolve_task() gated task_id on R-01
    # and fell through to fuzzy name matching on a miss, so task="2" returned a
    # DIFFERENT task's submissions (5 rows, all published) in place of task 2's
    # own (2 rows, both rejected).
    subs = await _call(registry, "weekly_submission_query", task="2")
    task_ids = {r.get("task_id") for r in subs.get("rows", [])}
    check(
        "提交单按 task_id 外键取数，不被正式任务口径截断",
        subs.get("ok") is True and task_ids == {2},
        f"task_ids={sorted(task_ids)} row_count={subs.get('row_count')}",
    )
    check(
        "提交单返回 round_no 与 status（动作流水无法聚合出这两项）",
        bool(subs.get("rows"))
        and {"round_no", "status"} <= set(subs["rows"][0])
        and isinstance(subs.get("status_breakdown"), list),
    )
    check(
        "提交单不返回 payload 草稿快照",
        '"payload"' not in json.dumps(subs.get("rows", []), ensure_ascii=False),
    )
    pending = await _call(registry, "weekly_submission_query", reporter="u3208", exclude_status="approved")
    check(
        "提交单支持按填报人过滤 + 排除状态",
        pending.get("ok") is True and pending.get("row_count", 0) > 0,
        f"row_count={pending.get('row_count')}",
    )

    # --- role-split counting (weekly_task_query ORs the owner columns) ------
    roles = await _call(registry, "weekly_owner_roles", person="u3208")
    role_row = (roles.get("rows") or [{}])[0]
    check(
        "按角色分别计数，四个维度齐全",
        {"as_owner", "as_project_owner", "as_lead_owner", "any_role"} <= set(role_row),
        f"keys={sorted(role_row)}",
    )
    check(
        "any_role 是三角色去重并集，不小于任一单角色",
        bool(role_row)
        and int(role_row["any_role"])
        >= max(int(role_row["as_owner"]), int(role_row["as_project_owner"]), int(role_row["as_lead_owner"])),
        f"row={role_row}",
    )

    # --- schema exposes column lists, minus blocked fields -----------------
    schema = await _call(registry, "weekly_schema")
    table_columns = schema.get("table_columns") or {}
    attachment_columns = table_columns.get("task_attachment") or []
    check(
        "字段清单可查（此前只能从样例行反推，漏掉 is_deleted）",
        "is_deleted" in attachment_columns and len(attachment_columns) >= 9,
        f"task_attachment={attachment_columns}",
    )
    check(
        "字段清单本身不含禁止外泄字段",
        all("storage_path" not in cols and "payload" not in cols for cols in table_columns.values()),
    )

    # --- aggregate capabilities that replace hand-counting ------------------
    # Without these the agent walked every task one detail call at a time (43
    # calls on one question) and ran out of tool rounds before answering.
    completeness = await _call(registry, "weekly_field_completeness", field="overall_goal")
    comp_row = (completeness.get("rows") or [{}])[0]
    check(
        "R-07/R-19 字段填报完整度一次调用可得",
        {"total", "filled", "missing"} <= set(comp_row),
        f"row={comp_row}",
    )
    check(
        "完整度统计自洽：filled + missing = total",
        bool(comp_row) and int(comp_row["filled"]) + int(comp_row["missing"]) == int(comp_row["total"]),
        f"row={comp_row}",
    )
    listing = await _call(registry, "weekly_field_completeness")
    check(
        "空参数返回支持字段清单而非报错",
        listing.get("ok") is True and bool(listing.get("supported_fields")),
    )
    bad_field = await _call(registry, "weekly_field_completeness", field="task_name; DROP TABLE task")
    check(
        "字段名走白名单，注入式入参被拒",
        bad_field.get("ok") is False and bad_field["error"]["code"] == "unsupported_field",
        str(bad_field.get("error"))[:90],
    )

    coverage = await _call(registry, "weekly_progress_coverage")
    cov_row = (coverage.get("rows") or [{}])[0]
    check(
        "进展覆盖度含行数/任务数/起止日期/最大版本",
        {"progress_rows", "tasks_covered", "earliest", "latest", "max_version"} <= set(cov_row),
        f"keys={sorted(cov_row)}",
    )

    ranking = await _call(registry, "weekly_task_ranking", metric="attachments", top=5)
    counts = [int(r["cnt"]) for r in ranking.get("rows", [])]
    check(
        "任务排名按计数降序且可复现",
        counts == sorted(counts, reverse=True) and len(counts) == 5,
        f"counts={counts}",
    )
    bad_metric = await _call(registry, "weekly_task_ranking", metric="不支持")
    check(
        "不支持的排名指标明确报错",
        bad_metric.get("ok") is False and bad_metric["error"]["code"] == "unsupported_metric",
    )

    # --- freshness anchors relative time -----------------------------------
    fresh = await _call(registry, "weekly_freshness")
    check(
        "weekly_freshness 给出快照时间用于锚定相对时间",
        bool(fresh.get("rows")) and all(r.get("latest_progress") for r in fresh["rows"]),
        f"rows={fresh.get('rows')}",
    )

    # --- error paths are explicit, never fabricated -------------------------
    missing = await _call(registry, "weekly_task_detail", task="根本不存在的任务zzz999")
    check(
        "查不到的任务如实报错而非编造",
        missing.get("ok") is False and missing["error"]["code"] == "task_not_found",
        str(missing.get("error"))[:100],
    )
    bad_group = await _call(registry, "weekly_aggregate", group_by="不支持的维度")
    check(
        "不支持的聚合维度明确报错",
        bad_group.get("ok") is False and bad_group["error"]["code"] == "unsupported_group_by",
        str(bad_group.get("error"))[:100],
    )
    bad_status = await _call(registry, "weekly_task_query", status="9")
    check(
        "非法 status 明确报错",
        bad_status.get("ok") is False,
        str(bad_status.get("error"))[:100],
    )
    empty_task = await _call(registry, "weekly_task_detail", task="  ")
    check(
        "空参数在客户端侧就被拒（不打远端）",
        empty_task.get("ok") is False and empty_task["error"]["code"] == "invalid_argument",
        str(empty_task.get("error"))[:100],
    )

    # --- R-13 multi-value owner matching -----------------------------------
    owners = await _call(registry, "weekly_aggregate", group_by="owner")
    check(
        "R-11/R-13 分管领导按填法枚举计数",
        owners.get("ok") is True and bool(owners.get("rows")),
        f"填法数={len(owners.get('rows', []))}",
    )
    named = await _call(registry, "weekly_task_query", owner="王振国", limit=5)
    check(
        "按负责人姓名可检出任务（去空格匹配）",
        named.get("ok") is True and named.get("total_count", 0) > 0,
        f"total={named.get('total_count')}",
    )

    # 时间维度：相对窗以数据快照日为锚，不是机器墙钟。两者相差十余天，
    # 用 CURDATE() 会把窗口滑出数据、算出偏小的数。
    w30 = await _call(registry, "weekly_progress_range", last_days=30)
    check(
        "最近 30 天进展期数与任务数（锚定快照日）",
        w30.get("total_count") == 17 and w30.get("total_tasks") == 17,
        f"total_count={w30.get('total_count')} total_tasks={w30.get('total_tasks')}",
    )
    check(
        "相对时间窗口径显式声明以快照日为基准",
        "2026-08-15" in str(w30.get("caliber", "")),
        str(w30.get("caliber"))[:120],
    )
    ytd = await _call(registry, "weekly_progress_range", date_from="2026-01-01", date_to="2026-08-15")
    check(
        "计数不受 200 行截断影响（今年以来 366 期）",
        ytd.get("total_count") == 366 and ytd.get("row_count") == 200 and ytd.get("has_more") is True,
        f"total={ytd.get('total_count')} rows={ytd.get('row_count')} has_more={ytd.get('has_more')}",
    )
    months = await _call(registry, "weekly_progress_range", date_from="2026-01-01", date_to="2026-08-15", by="month")
    check(
        "按月分组趋势可直答（7 个月桶）",
        months.get("ok") is True and months.get("row_count") == 7,
        f"rows={months.get('row_count')}",
    )
    # progress_date 与 report_time 不可互换：补报时两者相差数十天。
    late = await _call(
        registry,
        "weekly_progress_range",
        date_from="2026-07-01",
        date_to="2026-07-31",
        date_field="report_time",
    )
    check(
        "按上报时间可查出补报更早周期的进展（lag_days>0）",
        late.get("ok") is True and sum(1 for r in late.get("rows", []) if int(r.get("lag_days") or 0) > 0) == 3,
        f"rows={late.get('row_count')}",
    )
    check(
        "不支持的 date_field 明确报错，不静默改口径",
        (await _call(registry, "weekly_progress_range", date_field="created_at")).get("error", {}).get("code")
        == "unsupported_field",
        "",
    )
    life = await _call(registry, "weekly_task_lifecycle")
    check(
        "任务创建到发布的时长汇总（128 条 / 均 30.3 天 / 最长 60 天）",
        life.get("ok") is True
        # SUM() 是 Decimal，经 JSON 序列化成字符串，取数一律先转 int/str 再比。
        and int(life["rows"][0]["with_published_at"]) == 128
        and str(life["rows"][0]["avg_days_to_publish"]) == "30.3"
        and int(life["rows"][0]["max_days_to_publish"]) == 60,
        str(life.get("rows"))[:140],
    )
    fresh = await _call(registry, "weekly_freshness_distribution")
    check(
        "新鲜度分档带全局最新时间与落后天数",
        fresh.get("row_count") == 5 and fresh.get("days_behind") == 1,
        f"buckets={fresh.get('row_count')} days_behind={fresh.get('days_behind')}",
    )
    check(
        "自定义天窗可答分档表达不了的区间（7 天内 23 条）",
        (await _call(registry, "weekly_freshness_distribution", within_days=7))["rows"][0]["task_count"] == 23,
        "",
    )
    drift = await _call(registry, "weekly_freshness_distribution", drift=True, limit=8)
    check(
        "冗余列漂移可检出（latest_progress_time 与实际最新不一致）",
        drift.get("ok") is True and drift.get("row_count") == 8,
        f"rows={drift.get('row_count')}",
    )
    turn = await _call(registry, "weekly_approval_turnaround", scope="summary")
    check(
        "审批时效汇总（400 轮 / 均 14.7 天 / 最长 59 天）",
        turn["rows"][0]["completed_rounds"] == 400
        and str(turn["rows"][0]["avg_days"]) == "14.7"
        and turn["rows"][0]["max_days"] == 59,
        str(turn.get("rows"))[:140],
    )
    # 待审提交单本就尚未发布，加 R-01 闸门会把积压查成空。
    pending = await _call(registry, "weekly_approval_turnaround", scope="pending", top=8)
    check(
        "待审积压不套发布闸门，否则查成空",
        pending.get("row_count") == 8 and pending["rows"][0]["pending_days"] == 583,
        f"rows={pending.get('row_count')} 首行={_first(pending, 'pending_days')}",
    )

    # 集团组两张专表：现有工具一条都读不到，Q/R 两类 56 题全靠这四个入口。
    gd = await _call(registry, "weekly_group_detail_query", limit=8)
    check(
        "集团明细可读且带 task_name（Q1-01 前 8 条）",
        gd.get("row_count") == 8 and _first(gd, "task_id") == 97 and "数据资产入表" in str(_first(gd, "task_name")),
        f"rows={gd.get('row_count')} 首行={str(_first(gd, 'task_name'))[:30]}",
    )
    check(
        "集团明细默认口径声明完成时间不可做日期运算（R-12）",
        "R-12" in str(gd.get("caliber")),
        str(gd.get("caliber"))[:120],
    )
    due26 = await _call(registry, "weekly_group_detail_query", contains="2026", field="completion_time", limit=200)
    check(
        "完成时间按文本匹配 2026（Q4-03 = 31 条，非日期运算）",
        due26.get("row_count") == 31,
        f"rows={due26.get('row_count')}",
    )
    bad_field = await _call(registry, "weekly_group_detail_query", fields="storage_path")
    check(
        "集团明细字段走白名单，未收录字段拒绝",
        bad_field.get("ok") is False and bad_field.get("error", {}).get("code") == "unsupported_field",
        str(bad_field.get("error"))[:120],
    )

    lead = await _call(registry, "weekly_group_owner_query", person="唐立本", role="lead")
    proj = await _call(registry, "weekly_group_owner_query", person="唐立本", role="project")
    check(
        "牵头人与项目负责人是两个角色，不可混用（R1-01 = 5 / R1-02 = 3）",
        lead.get("row_count") == 5 and proj.get("row_count") == 3,
        f"lead={lead.get('row_count')} project={proj.get('row_count')}",
    )
    check(
        "多值负责人按元素精确匹配，不跨人误命中",
        "FIND_IN_SET" in str(lead.get("caliber")),
        str(lead.get("caliber"))[:140],
    )

    hist = await _call(registry, "weekly_group_history", limit=1)
    check(
        "集团历史进展双闸门后 362 行（漏 is_published 会多出 42 行草稿）",
        int(hist.get("total_count") or 0) == 362,
        f"total_count={hist.get('total_count')} total_tasks={hist.get('total_tasks')}",
    )
    check(
        "集团历史口径写明两道闸门缺一不可",
        "两道闸门" in str(hist.get("caliber")),
        str(hist.get("caliber"))[:140],
    )
    t99 = await _call(registry, "weekly_group_history", task="99")
    check(
        "单任务历史 7 期，版本号倒序（Q2-01）",
        t99.get("row_count") == 7 and _first(t99, "version_no") == 7,
        f"rows={t99.get('row_count')} 首版={_first(t99, 'version_no')}",
    )
    v3 = await _call(registry, "weekly_group_history", task="99", version_no=3)
    check(
        "可定位到指定期次（R2-03 第 3 期）",
        v3.get("row_count") == 1 and _first(v3, "version_no") == 3,
        f"rows={v3.get('row_count')} 版本={_first(v3, 'version_no')}",
    )
    by_year = await _call(registry, "weekly_group_history", by="year")
    years = {str(r.get("bucket")): r.get("progress_count") for r in by_year.get("rows") or []}
    check(
        "按年份分布 2025=137 / 2026=225（Q6-01）",
        years.get("2025") == 137 and years.get("2026") == 225,
        str(years),
    )
    latest = await _call(registry, "weekly_group_history", latest_only=True, limit=200)
    check(
        "各任务最新一期共 46 条，与看板任务数一致（R2-01）",
        latest.get("row_count") == 46,
        f"rows={latest.get('row_count')}",
    )
    bad_by = await _call(registry, "weekly_group_history", by="week")
    check(
        "集团历史不支持的分组明确报错",
        bad_by.get("ok") is False and bad_by.get("error", {}).get("code") == "unsupported_group_by",
        str(bad_by.get("error"))[:120],
    )

    owners = await _call(registry, "weekly_group_stats", scope="owners")
    orow = (owners.get("rows") or [{}])[0]
    check(
        "牵头人构成 46 / 多人 19 / 单人 27 / 去重 24 位（R1-03、R1-04）",
        int(orow.get("tasks") or 0) == 46
        and int(orow.get("multi_lead") or 0) == 19
        and int(orow.get("single_lead") or 0) == 27
        and int(owners.get("distinct_leads") or 0) == 24,
        f"{orow} distinct={owners.get('distinct_leads')}",
    )
    ct = await _call(registry, "weekly_group_stats", scope="completion_time")
    crow = (ct.get("rows") or [{}])[0]
    check(
        "完成时间格式分布 ISO 6 / 自由文本 40 / 空 0（Q4-02、Q4-04）",
        int(crow.get("iso_date") or 0) == 6
        and int(crow.get("free_text") or 0) == 40
        and int(crow.get("blank") or 0) == 0,
        str(crow),
    )
    lens = await _call(registry, "weekly_group_stats", scope="field_lengths")
    lrow = (lens.get("rows") or [{}])[0]
    check(
        "目标成果字数 均 45.6 / 最长 51（R5-03）",
        str(lrow.get("avg_chars")) == "45.6" and int(lrow.get("max_chars") or 0) == 51,
        str(lrow),
    )
    att = await _call(registry, "weekly_group_stats", scope="attachments", top=8)
    check(
        "零附件任务保留在清单里 18/46（inner_join_drops_zero）",
        int(att.get("no_attachment_summary", {}).get("no_attachment") or 0) == 18 and _first(att, "attachments") == 0,
        f"summary={att.get('no_attachment_summary')} 首行附件={_first(att, 'attachments')}",
    )
    rounds = await _call(registry, "weekly_group_stats", scope="history_rounds", top=8, min_rounds=5)
    check(
        "至少 5 期的任务 46 个，边界取等（Q6-03）",
        int(rounds.get("tasks_at_least", {}).get("tasks") or 0) == 46,
        str(rounds.get("tasks_at_least")),
    )
    bad_scope = await _call(registry, "weekly_group_stats", scope="whatever")
    check(
        "集团统计不支持的口径明确报错",
        bad_scope.get("ok") is False and bad_scope.get("error", {}).get("code") == "unsupported_scope",
        str(bad_scope.get("error"))[:120],
    )

    return report()


def report() -> int:
    width = max(len(name) for _, name, _ in _results) if _results else 10
    failed = 0
    lines = []
    for status, name, detail in _results:
        if status == FAIL:
            failed += 1
            lines.append(f"[{status}] {name:<{width}}  {detail}")
        else:
            lines.append(f"[{status}] {name}")
    print("\n".join(lines))
    print(f"\n{len(_results) - failed}/{len(_results)} passed")
    return 1 if failed else 0


def main() -> int:
    if not os.environ.get("GUOSHU_WEEKLY_MCP_URL"):
        print("GUOSHU_WEEKLY_MCP_URL is not set", file=sys.stderr)
        print("start the mock service first: python mock-mcp/server.py", file=sys.stderr)
        return 2
    return anyio.run(run)


if __name__ == "__main__":
    raise SystemExit(main())

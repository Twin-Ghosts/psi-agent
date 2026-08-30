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
        "weekly_year_goal_query",
        "weekly_year_goal_stats",
        "weekly_milestone_stats",
        "weekly_freshness",
        "weekly_health",
        "weekly_person_stats",
        "weekly_attachment_stats",
        "weekly_rank",
        "weekly_scale",
    }
    loaded = set(registry.tools)
    check(
        "所有 31 个工具注册，且无 helper 泄漏为工具",
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

    # --- A1: 闸门类问题的三处失分 ------------------------------------------
    # M3-03. approved 不在提交单状态值域内（已发布叫 published），所以这个过滤
    # 静默失效、结果等于未过滤。工具必须自己说出来，否则会被当成「已排除」。
    check(
        "M3-03 提交单状态值域随结果返回",
        pending.get("status_domain")
        == [
            "cancelled",
            "pending_audit",
            "pending_fill",
            "pending_leader",
            "published",
            "rejected",
            "signing",
        ],
        f"status_domain={pending.get('status_domain')}",
    )
    pending_caliber = str(pending.get("caliber", ""))
    check(
        "M3-03 值域外的过滤词被显式点名为未生效",
        "approved" in pending_caliber and "未筛掉任何行" in pending_caliber,
        f"caliber={pending_caliber[-120:]}",
    )
    check(
        "M3-03 u3208 共 29 条，total_count 与 row_count 一致且未截断",
        pending.get("total_count") == 29 and pending.get("row_count") == 29 and pending.get("has_more") is False,
        f"total={pending.get('total_count')} rows={pending.get('row_count')}",
    )
    pending_states = {r.get("status") for r in pending.get("rows", [])}
    check(
        "M3-03 结果含 published（说明过滤确实没生效，别按题面反推）",
        "published" in pending_states,
        f"states={sorted(pending_states)}",
    )
    check(
        "M3-03 口径要求清单类问题逐条列全",
        "逐条列全" in pending_caliber,
    )

    # M1-01. 附件大小是字节，模型此前换算成「约 3.8MB」而与精确值不一致。
    att19 = await _call(registry, "weekly_attachment_query", task="19")
    att_rows = att19.get("rows", [])
    check(
        "M1-01 任务 19 有 2 个未删除附件",
        att19.get("ok") is True and att19.get("row_count") == 2,
        f"row_count={att19.get('row_count')}",
    )
    check(
        "M1-01 file_size 为精确字节 3995969 / 1637494",
        [str(r.get("file_size")) for r in att_rows] == ["3995969", "1637494"],
        f"sizes={[r.get('file_size') for r in att_rows]}",
    )
    att_caliber = str(att19.get("caliber", ""))
    check(
        "M1-01 口径明说字节原样不换算",
        "字节" in att_caliber and "不要换算" in att_caliber,
        f"caliber={att_caliber}",
    )

    # N3-04. 各组牵头人数此前要模型自己数人名，参考 9 被答成 14。
    groups = await _call(registry, "weekly_aggregate", group_by="project_group")
    grows = groups.get("rows", [])
    check(
        "N3-04 专项组聚合 11 组，任务数合计 128",
        groups.get("ok") is True and len(grows) == 11 and sum(int(r["cnt"]) for r in grows) == 128,
        f"groups={len(grows)} sum={sum(int(r['cnt']) for r in grows) if grows else 0}",
    )
    check(
        "N3-04 服务端直接给出去重后的牵头人数与责任人数",
        bool(grows) and {"cnt", "lead_owner_count", "project_owner_count"} <= set(grows[0]),
        f"keys={sorted(grows[0]) if grows else []}",
    )
    check(
        "N3-04 各组牵头人数与 gold 一致",
        [int(r["lead_owner_count"]) for r in grows] == [9, 10, 11, 9, 9, 8, 7, 6, 6, 6, 5],
        f"leads={[r.get('lead_owner_count') for r in grows]}",
    )
    check(
        "N3-04 任务数序列与 gold 一致（同数按组名定序，避免并列漂移）",
        [int(r["cnt"]) for r in grows] == [19, 15, 15, 14, 12, 11, 10, 10, 8, 8, 6],
        f"cnt={[r.get('cnt') for r in grows]}",
    )
    check(
        "N3-04 牵头人数不超过任务数",
        all(int(r["lead_owner_count"]) <= int(r["cnt"]) for r in grows),
    )
    group_caliber = str(groups.get("caliber", ""))
    check(
        "N3-04 口径注明计数已由服务端去重，禁止自行数人名",
        "去重" in group_caliber and "不要自己数" in group_caliber,
        f"caliber={group_caliber[-100:]}",
    )
    other_agg = await _call(registry, "weekly_aggregate", group_by="owner")
    check(
        "其他聚合维度不受影响，仍只返回 group_name/cnt",
        other_agg.get("ok") is True and "lead_owner_count" not in (other_agg.get("rows") or [{}])[0],
    )

    # --- A2: F 类负责人与组织 ------------------------------------------------
    # F3-01/02/04. 姓名列 128 条全满、ID 列只有 119 条：只暴露姓名列时模型会
    # 如实报「无缺失」，与 gold 的 9 条直接矛盾。缺口只能从 ID 列看见。
    owner_id_comp = await _call(registry, "weekly_field_completeness", field="project_owner_id")
    comp_id_row = (owner_id_comp.get("rows") or [{}])[0]
    check(
        "F3-04 project_owner_id 完整度 128/119/9",
        [str(comp_id_row.get(k)) for k in ("total", "filled", "missing")] == ["128", "119", "9"],
        f"row={comp_id_row}",
    )
    missing_owners = await _call(registry, "weekly_field_completeness", field="project_owner_id", list_missing=True)
    check(
        "F3-01 缺责任人 ID 的 9 条任务可逐条列出，id 与 gold 一致",
        missing_owners.get("total_count") == 9
        and [r.get("id") for r in missing_owners.get("rows", [])] == [21, 27, 29, 33, 73, 99, 125, 149, 150],
        f"ids={[r.get('id') for r in missing_owners.get('rows', [])]}",
    )
    check(
        "F3-01 缺项清单带姓名列，便于说明「有名字但没 ID」",
        bool(missing_owners.get("rows"))
        and {"task_name", "project_owner_name", "lead_owner_name"} <= set(missing_owners["rows"][0]),
        f"keys={sorted(missing_owners.get('rows', [{}])[0])}",
    )

    # F2-02/03/04. 牵头人任务量此前靠模型翻明细自己数，首位被答成 6 条。
    workload = await _call(registry, "weekly_person_stats", scope="workload", top=3)
    check(
        "F2-02 牵头人任务量榜首 吴晓东 14 条（并列按姓名定序）",
        (workload.get("rows") or [{}])[0].get("person") == "吴晓东"
        and int((workload.get("rows") or [{}])[0].get("task_count", 0)) == 14,
        f"top={workload.get('rows')}",
    )
    wl_summary = await _call(registry, "weekly_person_stats", scope="workload_summary")
    wl_row = (wl_summary.get("rows") or [{}])[0]
    check(
        "F2-03 人均任务数 8.00 由服务端算（128 任务 / 16 人）",
        str(wl_row.get("avg_tasks_per_person")) == "8.00"
        and int(wl_row.get("tasks", 0)) == 128
        and int(wl_row.get("people", 0)) == 16,
        f"row={wl_row}",
    )
    single = await _call(registry, "weekly_person_stats", scope="single_task")
    check(
        "F2-04 只带一条任务的 4 位牵头人由 HAVING 判定",
        [r.get("person") for r in single.get("rows", [])]
        == ["project_lead_a", "project_lead_b", "project_lead_c", "余承志"],
        f"rows={[r.get('person') for r in single.get('rows', [])]}",
    )

    # F4-01/02/03/04. 标识写法异构：三档相加 128 但空标识不进任何档。
    id_fmt = await _call(registry, "weekly_person_stats", scope="id_format")
    check(
        "F4-01/02 标识格式分档 纯数字 69 / u 前缀 50 / NDG 域账号 9",
        [(r.get("id_format"), int(r.get("task_count", 0))) for r in id_fmt.get("rows", [])]
        == [("纯数字工号", 69), ("u 前缀账号", 50), ("NDG 域账号", 9)],
        f"rows={id_fmt.get('rows')}",
    )
    check(
        "F4-01 口径提醒各档相加不等于任务总数（空标识不进档）",
        "各档相加不等于任务总数" in str(id_fmt.get("caliber", "")),
    )
    variants = await _call(registry, "weekly_person_stats", scope="id_variants")
    check(
        "F4-03 同名多标识为 0 行，且口径说明 0 行即不存在",
        variants.get("row_count") == 0 and "不存在这种人" in str(variants.get("caliber", "")),
        f"rows={variants.get('row_count')}",
    )
    longest = await _call(registry, "weekly_person_stats", scope="id_longest", top=4)
    check(
        "F4-04 最长标识 11 字符且口径点明存在并列",
        int((longest.get("rows") or [{}])[0].get("id_length", 0)) == 11 and "并列" in str(longest.get("caliber", "")),
        f"top={longest.get('rows')}",
    )

    # F6-01/02/03/04. 填报在 task_progress 上，任务闸门之外还有行级 is_published。
    reporters = await _call(registry, "weekly_person_stats", scope="reporters", top=3)
    check(
        "F6-01 填报最多 10515 共 63 轮 / 4 个任务",
        [(r.get("reporter_id"), int(r.get("reported_rounds", 0))) for r in reporters.get("rows", [])]
        == [("10515", 63), ("10445", 57), ("10564", 50)],
        f"rows={reporters.get('rows')}",
    )
    check(
        "F6-01 口径写明任务闸门与进展行发布闸门是两道",
        "p.is_published = 1" in str(reporters.get("caliber", "")),
    )
    rep_count = await _call(registry, "weekly_person_stats", scope="reporter_count")
    check(
        "F6-02 去重填报人 43 位由服务端算",
        int((rep_count.get("rows") or [{}])[0].get("reporter_count", 0)) == 43,
        f"row={rep_count.get('rows')}",
    )
    reviewers = await _call(registry, "weekly_person_stats", scope="reviewers", top=3)
    check(
        "F6-03 审核口径不加 is_published，首位 10277/10291 各 119 条",
        [(r.get("reviewer_id"), int(r.get("reviewed", 0))) for r in reviewers.get("rows", [])]
        == [("10277", 119), ("10291", 119), ("10270", 116)],
        f"rows={reviewers.get('rows')}",
    )
    check(
        "F6-03 口径解释为何审核不加发布闸门",
        "审过但未发布的进展同样算审过" in str(reviewers.get("caliber", "")),
    )
    self_rev = await _call(registry, "weekly_person_stats", scope="self_review")
    check(
        "F6-04 自审 7 条，按 ID 相等判定而非姓名",
        self_rev.get("row_count") == 7 and "不按姓名" in str(self_rev.get("caliber", "")),
        f"row_count={self_rev.get('row_count')}",
    )

    # F7-02/04. 跨组与双角色此前被答成 4 组、人名也不对。
    cross = await _call(registry, "weekly_person_stats", scope="cross_group", top=3)
    check(
        "F7-02 跨组榜首 吴晓东 跨 8 组，group_count 已去重",
        (cross.get("rows") or [{}])[0].get("person") == "吴晓东"
        and int((cross.get("rows") or [{}])[0].get("group_count", 0)) == 8,
        f"top={cross.get('rows')}",
    )
    dual = await _call(registry, "weekly_person_stats", scope="dual_role")
    check(
        "F7-04 双角色 6 人，两列各自计数不可相加",
        [(r.get("person"), int(r.get("as_lead", 0)), int(r.get("as_project_owner", 0))) for r in dual.get("rows", [])]
        == [
            ("吴晓东", 14, 1),
            ("孙立群", 12, 2),
            ("马跃进", 11, 2),
            ("周文斌", 9, 4),
            ("胡建国", 8, 1),
            ("余承志", 1, 7),
        ],
        f"rows={dual.get('rows')}",
    )
    check(
        "F7-04 口径提醒不要把两个角色的计数相加",
        "别把两列相加" in str(dual.get("caliber", "")),
    )

    # F5-02/04. 多值负责人栏的分隔符是混填的，「单人」必须是独立一档。
    seps = await _call(registry, "weekly_group_stats", scope="separators")
    check(
        "F5-02 分隔符分档 半角逗号 26 / 单人 18 / 全角顿号 2",
        [(r.get("separator_kind"), int(r.get("n", 0))) for r in seps.get("rows", [])]
        == [("半角逗号", 26), ("单人无分隔符", 18), ("全角顿号", 2)],
        f"rows={seps.get('rows')}",
    )
    check(
        "F5-02 口径说明「单人无分隔符」是一档而非缺失",
        "是独立一档不是缺失" in str(seps.get("caliber", "")),
    )
    widths = await _call(registry, "weekly_group_stats", scope="owner_widths", top=3)
    top_width = (widths.get("rows") or [{}])[0]
    check(
        "F5-04 责任人最多的一条是 数据资产入表试点推进（3 人）",
        top_width.get("task_name") == "数据资产入表试点推进"
        and top_width.get("project_owner_names") == "胡建国,方永康,邓少华"
        and int(top_width.get("owner_count", 0)) == 3,
        f"top={top_width}",
    )

    # F5-01. 牵头人与责任人要同表并列取出，还得带专项组。
    # task_name 由服务端固定带上，不在 fields 白名单里，写进去会报 unsupported_field。
    group_owners = await _call(
        registry,
        "weekly_group_detail_query",
        fields="lead_owner_names,project_owner_names,project_group",
        limit=8,
    )
    check(
        "F5-01 三列可一次取全并带 project_owner_names 的原始多值形态",
        bool(group_owners.get("rows"))
        and {"task_name", "lead_owner_names", "project_owner_names", "project_group"} <= set(group_owners["rows"][0]),
        f"keys={sorted(group_owners.get('rows', [{}])[0])}",
    )
    check(
        "F5-01 前 8 条按任务 id 定序，首条与 gold 一致",
        [r.get("task_name") for r in group_owners.get("rows", [])][:2]
        == ["数据资产入表试点推进", "公共数据授权运营模式创新"]
        and group_owners["rows"][0].get("project_owner_names") == "胡建国,方永康,邓少华",
        f"first={group_owners.get('rows', [{}])[0]}",
    )

    # --- A3: J 类附件 -------------------------------------------------------
    # J2-01/02/03. 明细上限 200 条，靠翻行求和必然少算（真实 454 条）。
    att_sum = await _call(registry, "weekly_attachment_stats", scope="summary")
    sum_row = (att_sum.get("rows") or [{}])[0]
    check(
        "J2-01 附件 454 条 / 1863.8MB / 均值 4203.9KB，字节为权威值",
        int(sum_row.get("attachment_count", 0)) == 454
        and str(sum_row.get("total_bytes")) == "1954375767"
        and str(sum_row.get("total_mb")) == "1863.8"
        and str(sum_row.get("avg_kb")) == "4203.9",
        f"row={sum_row}",
    )
    by_ext = await _call(registry, "weekly_attachment_stats", scope="by_ext")
    check(
        "J2-02 类型分布 pptx 130 / xlsx 116 / pdf 107 / docx 101",
        [(r.get("ext"), int(r.get("n", 0))) for r in by_ext.get("rows", [])]
        == [("pptx", 130), ("xlsx", 116), ("pdf", 107), ("docx", 101)],
        f"rows={by_ext.get('rows')}",
    )
    largest = await _call(registry, "weekly_attachment_stats", scope="largest", top=2)
    big = (largest.get("rows") or [{}])[0]
    check(
        "J2-03 最大附件 行业数据标注基地能力建设-会议纪要.pdf（7.99MB / 8379724 字节）",
        big.get("file_name") == "行业数据标注基地能力建设-会议纪要.pdf"
        and str(big.get("file_size")) == "8379724"
        and str(big.get("size_mb")) == "7.99",
        f"top={big}",
    )

    # J3-01/02/03/04. 挂载归属四问，一条附件只进一档；孤儿行必须走 NOT EXISTS。
    by_link = await _call(registry, "weekly_attachment_stats", scope="by_link")
    check(
        "J3-01 挂载分布 进展 315 / 任务本体 81 / 提交单 58，合计等于 454",
        [(r.get("link_type"), int(r.get("n", 0))) for r in by_link.get("rows", [])]
        == [("挂在进展", 315), ("挂在任务本体", 81), ("挂在提交单", 58)]
        and sum(int(r.get("n", 0)) for r in by_link.get("rows", [])) == 454,
        f"rows={by_link.get('rows')}",
    )
    open_sub = await _call(registry, "weekly_attachment_stats", scope="on_open_submission")
    check(
        "J3-02 在途提交单附件 58 个（按提交单自己的码值判，不用 workflow_status）",
        int((open_sub.get("rows") or [{}])[0].get("attachment_count", 0)) == 58
        and "s.status <> 'published'" in str(open_sub.get("caliber", "")),
        f"row={open_sub.get('rows')}",
    )
    by_prog = await _call(registry, "weekly_attachment_stats", scope="by_progress", top=8)
    check(
        "J3-03 已发布进展带附件 Top8 首两条各 3 个，且按任务 id/期号定序",
        [
            (r.get("task_name"), int(r.get("version_no", 0)), int(r.get("attachment_count", 0)))
            for r in by_prog.get("rows", [])
        ][:3]
        == [
            ("数据要素标准国际对标", 15, 3),
            ("数据交易平台功能迭代（2期）", 2, 3),
            ("全国一体化算力网调度平台建设", 10, 2),
        ],
        f"rows={by_prog.get('rows')}",
    )
    orphan = await _call(registry, "weekly_attachment_stats", scope="orphan")
    check(
        "J3-04 孤儿附件 3 条，口径点明 JOIN 会恒等于 0",
        int((orphan.get("rows") or [{}])[0].get("orphan_count", 0)) == 3
        and "NOT EXISTS" in str(orphan.get("caliber", "")),
        f"row={orphan.get('rows')}",
    )

    # J4-02/03. 软删审计问的是表本身，加任务闸门会少算。
    deleted = await _call(registry, "weekly_attachment_stats", scope="deleted")
    del_row = (deleted.get("rows") or [{}])[0]
    check(
        "J4-03 已删附件 33 条 / 116.4MB，全表 543 行",
        int(del_row.get("deleted", 0)) == 33
        and str(del_row.get("deleted_mb")) == "116.4"
        and int(del_row.get("total_rows", 0)) == 543,
        f"row={del_row}",
    )
    check(
        "J4-03 口径说明这是全表口径不加任务闸门",
        "不加任务闸门" in str(deleted.get("caliber", "")),
    )
    del_link = await _call(registry, "weekly_attachment_stats", scope="deleted_by_link")
    check(
        "J4-02 已删附件挂载分布 进展 20 / 提交单 7 / 任务本体 6",
        [(r.get("link_type"), int(r.get("n", 0))) for r in del_link.get("rows", [])]
        == [("挂在进展", 20), ("挂在提交单", 7), ("挂在任务本体", 6)],
        f"rows={del_link.get('rows')}",
    )

    # J5-01/02/03. 上传人维度此前被当成任务维度答，月度也漏了起始月过滤。
    uploaders = await _call(registry, "weekly_attachment_stats", scope="by_uploader", top=3)
    check(
        "J5-01 上传最多 10354 共 26 个 / 98.2MB",
        [(r.get("uploader_id"), int(r.get("upload_count", 0))) for r in uploaders.get("rows", [])]
        == [("10354", 26), ("10515", 24), ("10438", 23)],
        f"rows={uploaders.get('rows')}",
    )
    up_count = await _call(registry, "weekly_attachment_stats", scope="uploader_count")
    check(
        "J5-02 上传过附件的 46 人由服务端去重",
        int((up_count.get("rows") or [{}])[0].get("uploader_count", 0)) == 46,
        f"row={up_count.get('rows')}",
    )
    by_month = await _call(registry, "weekly_attachment_stats", scope="by_month", date_from="2026-01-01")
    check(
        "J5-03 2026 年逐月 23/26/32/31/28/16/18/3 且未截断",
        [int(r.get("n", 0)) for r in by_month.get("rows", [])] == [23, 26, 32, 31, 28, 16, 18, 3]
        and by_month.get("has_more") is False,
        f"rows={[(r.get('ym'), r.get('n')) for r in by_month.get('rows', [])]}",
    )
    check(
        "J5-03 口径回显起始月，未限月时明说含全部历史",
        "仅 2026-01-01 起" in str(by_month.get("caliber", "")),
    )

    # 未知口径必须报错并列出支持值，而不是静默退回默认档。
    bad_scope = await _call(registry, "weekly_attachment_stats", scope="by_weekday")
    check(
        "附件统计不支持的口径显式报错并列出支持值",
        bad_scope.get("ok") is False
        and bad_scope.get("error", {}).get("code") == "unsupported_scope"
        and "by_progress" in str(bad_scope.get("error", {}).get("message", "")),
        f"err={bad_scope.get('error')}",
    )
    bad_person = await _call(registry, "weekly_person_stats", scope="salary")
    check(
        "人员统计不支持的口径显式报错",
        bad_person.get("ok") is False and bad_person.get("error", {}).get("code") == "unsupported_scope",
        f"err={bad_person.get('error')}",
    )

    # --- A4: 集合边界 --------------------------------------------------------
    # L 类。同一份数据、同一个度量，三种并列口径的行数各不相同。这三条断言绑在
    # 一起才有意义：任何一档退化成另一档，都会让「前 3 名」答出别人的行数。
    cut3 = await _call(registry, "weekly_rank", metric="progress_rounds", mode="cut", top=3)
    check(
        "L2-03 cut 硬切 3 条，口径写明边界外并列不补列",
        cut3.get("row_count") == 3
        and "硬切前 3 条" in str(cut3.get("caliber", ""))
        and "不要补列" in str(cut3.get("caliber", "")),
        f"row_count={cut3.get('row_count')} caliber={str(cut3.get('caliber'))[:160]}",
    )
    ties3 = await _call(registry, "weekly_rank", metric="progress_rounds", mode="keep_ties", top=3)
    check(
        "L3-01 keep_ties 前 3 名共 12 行（并列全列），且每行带 rk",
        ties3.get("row_count") == 12
        and all("rk" in r for r in ties3.get("rows", []))
        and ties3.get("has_more") is False,
        f"row_count={ties3.get('row_count')}",
    )
    per_group = await _call(
        registry, "weekly_rank", metric="progress_rounds", mode="per_group", group_by="project_group"
    )
    check(
        "L4-01 per_group 一组一行共 11 行，不受 top 影响且未截断",
        per_group.get("row_count") == 11
        and per_group.get("has_more") is False
        and len({r.get("bucket") for r in per_group.get("rows", [])}) == 11,
        f"row_count={per_group.get('row_count')} has_more={per_group.get('has_more')}",
    )
    # ascending 那一端必须留住零值行：期数为 0 的任务正是「最少」的答案，
    # INNER JOIN 会把它们整行丢掉（inner_join_drops_zero）。
    fewest = await _call(registry, "weekly_rank", metric="progress_rounds", mode="cut", top=5, ascending=True)
    check(
        "L 类 ascending 保留零期数任务（LEFT JOIN，非 INNER）",
        fewest.get("row_count") == 5 and all(int(r.get("metric_value", -1)) == 0 for r in fewest.get("rows", [])),
        f"rows={[(r.get('task_id'), r.get('metric_value')) for r in fewest.get('rows', [])]}",
    )
    bad_metric = await _call(registry, "weekly_rank", metric="salary")
    check(
        "weekly_rank 未知 metric 报错并列出值域",
        bad_metric.get("ok") is False
        and bad_metric.get("error", {}).get("code") == "unsupported_metric"
        and "progress_rounds" in str(bad_metric.get("error", {}).get("message", "")),
        f"err={bad_metric.get('error')}",
    )
    bad_mode = await _call(registry, "weekly_rank", mode="dense_rank")
    check(
        "weekly_rank 未知 mode 报错并列出三档",
        bad_mode.get("ok") is False and bad_mode.get("error", {}).get("code") == "unsupported_mode",
        f"err={bad_mode.get('error')}",
    )
    no_axis = await _call(registry, "weekly_rank", mode="per_group")
    check(
        "per_group 缺 group_by 报错而非静默退回全局排名",
        no_axis.get("ok") is False and no_axis.get("error", {}).get("code") == "unsupported_group_by",
        f"err={no_axis.get('error')}",
    )

    # B4-01. 任务只挂二级分类，一级分类要经 parent_id 上跳；按 category 分组
    # 会返回 47 档，那是另一个问题的答案。
    pcat = await _call(registry, "weekly_aggregate", group_by="primary_category", board="tech")
    check(
        "B4-01 技术组一级分类 6 档，首档 关键技术攻关 18",
        pcat.get("row_count") == 6
        and (pcat.get("rows") or [{}])[0].get("group_name") == "关键技术攻关"
        and int((pcat.get("rows") or [{}])[0].get("cnt", 0)) == 18,
        f"rows={[(r.get('group_name'), r.get('cnt')) for r in pcat.get('rows', [])]}",
    )
    check(
        "一级分类口径点明不是二级分类，且看板过滤走分类树",
        "不是二级分类" in str(pcat.get("caliber", "")),
    )
    # B4-03. 9 个二级分类并列 5 个任务，「前 5」必须硬切，并告知总组数。
    cat5 = await _call(registry, "weekly_aggregate", group_by="category", top=5)
    check(
        "B4-03 二级分类硬切 5 组并回显共 47 组",
        cat5.get("row_count") == 5 and cat5.get("total_groups") == 47 and "共 47 组" in str(cat5.get("caliber", "")),
        f"row_count={cat5.get('row_count')} total_groups={cat5.get('total_groups')}",
    )

    # G2-01. 「在办任务没定目标」问的是 status IN (0, 1) 那批；不加这道过滤会
    # 把已完成/已暂停的混进来（11 行 vs 10 行）。
    miss_all = await _call(registry, "weekly_year_goal_stats", scope="missing", year=2026, top=200)
    miss_open = await _call(
        registry, "weekly_year_goal_stats", scope="missing", year=2026, top=200, in_progress_only=True
    )
    check(
        "G2-01 2026 无目标共 11 个，其中在办 10 个（0 未开始同样在办）",
        miss_all.get("total_count") == 11
        and miss_open.get("total_count") == 10
        and "仅在办任务" in str(miss_open.get("caliber", "")),
        f"all={miss_all.get('total_count')} open={miss_open.get('total_count')}",
    )

    # O4-01. 单任务里程碑此前没有 task 参数，问「任务 19 有哪些里程碑」会拿回
    # 全board首页——一个完整、像样、但属于另一个问题的答案。
    ms19 = await _call(registry, "weekly_milestone_query", task="19")
    check(
        "O4-01 任务 19 恰好 2 条里程碑，按任务内 sort_order 编排",
        ms19.get("total_count") == 2
        and {int(r.get("task_id", 0)) for r in ms19.get("rows", [])} == {19}
        and [int(r.get("sort_order", 0)) for r in ms19.get("rows", [])] == [1, 2]
        and "sort_order" in str(ms19.get("caliber", "")),
        f"total={ms19.get('total_count')} "
        f"rows={[(r.get('task_id'), r.get('sort_order')) for r in ms19.get('rows', [])]}",
    )
    ms_all = await _call(registry, "weekly_milestone_query")
    check(
        "里程碑不带 task 时覆盖全部 474 条（明细截断但总数精确）",
        ms_all.get("total_count") == 474 and ms_all.get("has_more") is True,
        f"total={ms_all.get('total_count')}",
    )

    # C6-02. 未发布进展的 status 是进展行自己的审批码值，与任务的
    # workflow_status 是两套词汇，套错就答成 published/pending_audit。
    unpub = await _call(registry, "weekly_progress_coverage", scope="unpublished")
    check(
        "C6-02 未发布进展 草稿 26 / 待审核 58 / 驳回 39，合计 123",
        [(int(r.get("status", -1)), int(r.get("cnt", 0))) for r in unpub.get("rows", [])] == [(0, 26), (1, 58), (2, 39)]
        and unpub.get("total_count") == 123,
        f"rows={unpub.get('rows')} total={unpub.get('total_count')}",
    )
    check(
        "未发布进展口径显式排除拿 workflow_status 来套",
        "不要拿任务的 workflow_status" in str(unpub.get("caliber", "")),
    )
    gaps = await _call(registry, "weekly_progress_coverage", scope="version_gaps")
    check(
        "期号缺号 5 个任务，missing_count = 最大期号 - 实际期数",
        gaps.get("row_count") == 5
        and all(
            int(r.get("max_version", 0)) - int(r.get("rounds", 0)) == int(r.get("missing_count", -1))
            for r in gaps.get("rows", [])
        ),
        f"rows={gaps.get('rows')}",
    )

    # 库里真实存在的 completion_time 写法有 28 种，含「持续推进」这类非日期文本。
    ct_values = await _call(registry, "weekly_group_stats", scope="completion_time_values", top=200)
    check(
        "completion_time 去重后 28 种，含非日期文本且原样返回",
        ct_values.get("total_count") == 28
        and "持续推进" in {str(r.get("completion_time")) for r in ct_values.get("rows", [])}
        and "不要归纳成自己的类别名" in str(ct_values.get("caliber", "")),
        f"total={ct_values.get('total_count')}",
    )
    effect = await _call(registry, "weekly_group_stats", scope="effect_consistency", top=200)
    check(
        "成效一致性 46 行，不一致的排最前（same 升序）",
        effect.get("row_count") == 46
        and [int(r.get("same", -1)) for r in effect.get("rows", [])]
        == sorted(int(r.get("same", -1)) for r in effect.get("rows", [])),
        f"row_count={effect.get('row_count')}",
    )

    # 任务 workflow_status 与最新提交单 status 是两套码值，跨两次调用用眼比对
    # 会把行集混掉；这一档专门做这次比较，并且故意不加发布闸门。
    mismatch = await _call(registry, "weekly_submission_query", status_mismatch=True)
    check(
        "任务状态与最新提交单状态不一致 16 个，口径说明不加发布闸门",
        mismatch.get("row_count") == 16
        and "不加发布闸门" in str(mismatch.get("caliber", ""))
        and all(r.get("workflow_status") != r.get("latest_submission_status") for r in mismatch.get("rows", [])),
        f"row_count={mismatch.get('row_count')}",
    )

    # 动作日志比 200 行上限长，所以过滤必须落在服务端；值域外的 action 报错，
    # 不能静默不过滤——那会返回全量并看着像答案。
    bad_action = await _call(registry, "weekly_workflow_query", action="submit")
    check(
        "action 值域外报错并列出四个真实取值（不静默退回全量）",
        bad_action.get("ok") is False
        and bad_action.get("error", {}).get("code") == "unsupported_action"
        and "submitted" in str(bad_action.get("error", {}).get("message", ""))
        and "[" not in str(bad_action.get("error", {}).get("message", "")),
        f"err={bad_action.get('error')}",
    )
    by_task = await _call(registry, "weekly_workflow_query", by_task=True)
    check(
        "动作日志按任务聚合 150 行，口径点明 action_count 是次数",
        by_task.get("row_count") == 150 and "次数不是任务数" in str(by_task.get("caliber", "")),
        f"row_count={by_task.get('row_count')}",
    )

    # 「哪个月上报最多」的裁决落服务端：返回 19 行让模型自己挑，它会把
    # 2026-02(61) 和名次靠后的月份看成并列。
    peak = await _call(registry, "weekly_progress_range", by="month", peak=True)
    check(
        "峰值月 2026-02 共 61 条，单行返回且无假截断信号",
        peak.get("row_count") == 1
        and (peak.get("rows") or [{}])[0].get("bucket") == "2026-02"
        and int((peak.get("rows") or [{}])[0].get("progress_count", 0)) == 61
        and peak.get("has_more") is False,
        f"rows={peak.get('rows')} has_more={peak.get('has_more')}",
    )

    # 滞后清单：在办含 status 0，从未上报（NULL）算滞后且排最前。
    stale = await _call(registry, "weekly_freshness_distribution", stale_days=30)
    check(
        "滞后 30 天的在办任务 46 个，从未上报排最前且 days_since 为空",
        stale.get("total_count") == 46
        and (stale.get("rows") or [{}])[0].get("latest_progress_time") is None
        and (stale.get("rows") or [{}])[0].get("days_since") is None
        and "0 未开始同样在办" in str(stale.get("caliber", "")),
        f"total={stale.get('total_count')} first={(stale.get('rows') or [{}])[0]}",
    )
    stale_pg = await _call(registry, "weekly_freshness_distribution", stale_days=30, by="project_group")
    check(
        "滞后按专项组分组 11 组，各组相加等于 46",
        stale_pg.get("row_count") == 11 and sum(int(r.get("stale_count", 0)) for r in stale_pg.get("rows", [])) == 46,
        f"rows={[(r.get('project_group'), r.get('stale_count')) for r in stale_pg.get('rows', [])]}",
    )
    recent = await _call(registry, "weekly_freshness_distribution", recent_days=7)
    check(
        "近 7 天上报 23 个任务，不加 status 过滤（问的是有无上报）",
        recent.get("row_count") == 23 and "不加 status 过滤" in str(recent.get("caliber", "")),
        f"row_count={recent.get('row_count')}",
    )
    bad_by = await _call(registry, "weekly_freshness_distribution", stale_days=30, by="owner")
    check(
        "滞后清单不支持的分组轴显式报错",
        bad_by.get("ok") is False and bad_by.get("error", {}).get("code") == "unsupported_group_by",
        f"err={bad_by.get('error')}",
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

    # 年度目标：原先只在单任务详情里露一角，全盘覆盖率类问题无路可走。
    goals = await _call(registry, "weekly_year_goal_query", limit=1)
    check(
        "年度目标全盘 313 条，计数不受 200 行截断影响（G3-02）",
        int(goals.get("total_count") or 0) == 313,
        f"total_count={goals.get('total_count')} total_tasks={goals.get('total_tasks')}",
    )
    g2026 = await _call(registry, "weekly_year_goal_query", task="隐私计算平台自主可控攻关", year=2026)
    check(
        "单任务单年度目标可定位，附里程碑摘要（G1-01、G4-03）",
        g2026.get("row_count") == 1
        and _first(g2026, "year") == 2026
        and "3项标志性成果" in str(_first(g2026, "milestone_summary")),
        f"rows={g2026.get('row_count')} 摘要={str(_first(g2026, 'milestone_summary'))[:40]}",
    )
    gby = await _call(registry, "weekly_year_goal_stats", scope="by_year")
    gyears = {str(r.get("year")): r.get("goal_count") for r in gby.get("rows") or []}
    check(
        "各年度目标条数 2025=128 / 2026=117 / 2027=68（G3-01、E7-01）",
        gyears.get("2025") == 128 and gyears.get("2026") == 117 and gyears.get("2027") == 68,
        str(gyears),
    )
    cov = await _call(registry, "weekly_year_goal_stats", scope="coverage", year=2026)
    crow = (cov.get("rows") or [{}])[0]
    check(
        "2026 覆盖率 分母 128 / 有 117 / 缺 11 / 91.4%（G2-02、G2-03）",
        int(crow.get("total_tasks") or 0) == 128
        and int(crow.get("has_goal") or 0) == 117
        and int(crow.get("missing_goal") or 0) == 11
        and str(crow.get("coverage_pct")) == "91.4",
        str(crow),
    )
    miss = await _call(registry, "weekly_year_goal_stats", scope="missing", year=2026, top=200)
    check(
        "缺 2026 目标的任务列得出来（G2-01，用 JOIN 会把它们丢掉）",
        miss.get("row_count") == 11,
        f"rows={miss.get('row_count')}",
    )
    mg = await _call(registry, "weekly_year_goal_stats", scope="missing_by_group", year=2026, top=20)
    check(
        "缺口按专项组分布 6 组，首组 2 条（G2-04）",
        mg.get("row_count") == 6 and int(_first(mg, "missing_count") or 0) == 2,
        f"rows={mg.get('row_count')} 首组={_first(mg, 'missing_count')}",
    )
    span = await _call(registry, "weekly_year_goal_stats", scope="span", min_years=3, top=8)
    check(
        "平均每任务 2.45 个年度，三年及以上边界取等（G3-03、G3-04）",
        str(span.get("avg_years_per_task")) == "2.45"
        and span.get("row_count") == 8
        and int(_first(span, "year_count") or 0) == 3,
        f"avg={span.get('avg_years_per_task')} rows={span.get('row_count')}",
    )
    both = await _call(registry, "weekly_year_goal_stats", scope="multi_year", year=2025, year_to=2026, top=5)
    check(
        "连续两年都设目标的任务 117 条（G5-01、G5-02）",
        int(both.get("tasks_in_both_years") or 0) == 117 and both.get("row_count") == 5,
        f"both={both.get('tasks_in_both_years')} rows={both.get('row_count')}",
    )
    need_year = await _call(registry, "weekly_year_goal_stats", scope="coverage")
    check(
        "覆盖率口径缺 year 时明确报错，不静默按全年算",
        need_year.get("ok") is False and need_year.get("error", {}).get("code") == "invalid_argument",
        str(need_year.get("error"))[:120],
    )

    # 里程碑：原有 weekly_milestone_query 只能列行，完成率类问题只能手数。
    ms = await _call(registry, "weekly_milestone_stats", scope="summary")
    srow = (ms.get("rows") or [{}])[0]
    check(
        "里程碑总体 474 / 完成 242 / 完成率 51.1%（H2-01、H4-04）",
        int(srow.get("total") or 0) == 474
        and int(srow.get("finished") or 0) == 242
        and str(srow.get("finish_rate_pct")) == "51.1",
        str(srow),
    )
    check(
        "里程碑口径同时声明 R-17 复核与 status 两值码",
        "R-17" in str(ms.get("caliber")) and "两值码" in str(ms.get("caliber")),
        str(ms.get("caliber"))[:140],
    )
    ms26 = await _call(registry, "weekly_milestone_stats", scope="summary", year=2026)
    m26 = (ms26.get("rows") or [{}])[0]
    check(
        "2026 里程碑 273 / 完成 142 / 未完 131（H2-03）",
        int(m26.get("total") or 0) == 273 and int(m26.get("unfinished") or 0) == 131,
        str(m26),
    )
    by_yr = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="year")
    ybk = {str(r.get("bucket")): r.get("total") for r in by_yr.get("rows") or []}
    check(
        "里程碑按年份 2025=201 / 2026=273（E7-03、H2-04）",
        ybk.get("2025") == 201 and ybk.get("2026") == 273,
        str(ybk),
    )
    by_cat = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="category", top=8)
    check(
        "里程碑按类别 6 类，首类国家任务 90 / 完成 53（H3-01、H3-02）",
        by_cat.get("row_count") == 6
        and str(_first(by_cat, "bucket")) == "国家任务"
        and int(_first(by_cat, "total") or 0) == 90
        and int(_first(by_cat, "finished") or 0) == 53,
        f"rows={by_cat.get('row_count')} 首类={_first(by_cat, 'bucket')}",
    )
    by_grp = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="group_name", top=8)
    check(
        "里程碑按承担组 6 组，带完成率（H3-03）",
        by_grp.get("row_count") == 6 and _first(by_grp, "finish_rate_pct") is not None,
        f"rows={by_grp.get('row_count')} 首组={_first(by_grp, 'bucket')}",
    )
    floor20 = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="category", min_total=20, top=20)
    lowest = min(
        (r for r in floor20.get("rows") or []),
        key=lambda r: float(r.get("finish_rate_pct") or 0),
        default={},
    )
    check(
        "计数不少于 20 的类别里完成率最低是平台上线 45.2%（H3-04）",
        str(lowest.get("bucket")) == "平台上线" and str(lowest.get("finish_rate_pct")) == "45.2",
        str(lowest),
    )
    deleted = await _call(registry, "weekly_milestone_stats", scope="deleted")
    drow = (deleted.get("rows") or [{}])[0]
    check(
        "软删除审计 有效 566 / 已删 36 / 全表 602（H4-01、H4-02）",
        int(drow.get("active") or 0) == 566
        and int(drow.get("deleted") or 0) == 36
        and int(drow.get("total_rows") or 0) == 602,
        str(drow),
    )
    per_task = await _call(registry, "weekly_milestone_stats", scope="per_task", top=8)
    psum = per_task.get("summary") or {}
    check(
        "每任务均 3.70 个里程碑，3 个任务一个都没设（H5-02、H5-03）",
        str(psum.get("avg_per_task")) == "3.70" and int(psum.get("tasks_without_milestone") or 0) == 3,
        str(psum),
    )
    check(
        "里程碑最多的任务 6 个（H5-04）",
        int(_first(per_task, "milestones") or 0) == 6,
        f"首行={_first(per_task, 'task_name')} {_first(per_task, 'milestones')}",
    )
    mm1 = await _call(registry, "weekly_milestone_stats", scope="mismatch", top=20)
    check(
        "任务标完成但里程碑未全完成 6 条（H6-01）",
        mm1.get("row_count") == 6,
        f"rows={mm1.get('row_count')}",
    )
    mm2 = await _call(registry, "weekly_milestone_stats", scope="mismatch", kind="milestones_done_task_open", top=20)
    check(
        "里程碑全完成但任务仍在办 8 条（H6-02）",
        mm2.get("row_count") == 8,
        f"rows={mm2.get('row_count')}",
    )
    by_ts = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="task_status", top=8)
    ts = {str(r.get("bucket")): str(r.get("finish_rate_pct")) for r in by_ts.get("rows") or []}
    check(
        "按任务状态看里程碑完成率 已完成 93.8% 高于在办 44.6%（H6-03）",
        ts.get("2") == "93.8" and ts.get("1") == "44.6",
        str(ts),
    )
    bad_dim = await _call(registry, "weekly_milestone_stats", scope="by_dimension", by="owner")
    check(
        "里程碑不支持的维度明确报错",
        bad_dim.get("ok") is False and bad_dim.get("error", {}).get("code") == "unsupported_group_by",
        str(bad_dim.get("error"))[:120],
    )

    # --- A5: JOIN 放大与去重 -------------------------------------------------
    # K 类。三张子表一起 JOIN 时里程碑会被附件行数乘一遍：技术组真实 294，不去重
    # 会报 1363（这也是 K6-01 标准答案本身错的地方，它对任务/目标用了 DISTINCT
    # 却对里程碑用了裸 COUNT）。两个看板相加必须等于全库里程碑总数 474。
    sc_board = await _call(registry, "weekly_scale", by="board")
    sb = {str(r.get("bucket")): r for r in sc_board.get("rows") or []}
    tech = sb.get("技术组重点任务进展") or {}
    grp = sb.get("集团重点任务调度") or {}
    check(
        "K6-01 看板规模去重后 技术组 82/77/294/402、集团 46/40/180/52",
        int(tech.get("tasks") or 0) == 82
        and int(tech.get("with_year_goal") or 0) == 77
        and int(tech.get("milestones") or 0) == 294
        and int(tech.get("attachments") or 0) == 402
        and int(grp.get("tasks") or 0) == 46
        and int(grp.get("milestones") or 0) == 180
        and int(grp.get("attachments") or 0) == 52,
        f"tech={tech} group={grp}",
    )
    check(
        "K6-01 各看板里程碑相加 474 等于全库总数（没被 JOIN 放大）",
        sum(int(r.get("milestones") or 0) for r in sc_board.get("rows") or []) == 474,
        f"sum={sum(int(r.get('milestones') or 0) for r in sc_board.get('rows') or [])}",
    )
    check(
        "K6-01 口径给出「相加等于全库总数」这条自检法",
        "COUNT(DISTINCT" in str(sc_board.get("caliber", ""))
        and "相加等于全库里程碑总数" in str(sc_board.get("caliber", "")),
        str(sc_board.get("caliber"))[:160],
    )
    # 一次 JOIN 出四个维度，才能避免「分四次查再拼」这条放大路径。
    sc_grp = await _call(registry, "weekly_scale", by="project_group")
    check(
        "K6-02 按专项组 11 行，首组 标准安全组 19/79/50",
        sc_grp.get("row_count") == 11
        and str(_first(sc_grp, "bucket")) == "标准安全组"
        and int(_first(sc_grp, "tasks") or 0) == 19
        and int(_first(sc_grp, "milestones") or 0) == 79
        and int(_first(sc_grp, "attachments") or 0) == 50,
        f"rows={sc_grp.get('row_count')} 首组={_first(sc_grp, 'bucket')}",
    )
    # totals 与 completeness 是两个问题：「多少个里程碑」问子表行数（294），
    # 「多少任务有里程碑」问任务数（80）。拿一个答另一个必错。
    sc_full = await _call(registry, "weekly_scale", by="board", mode="completeness")
    cb = {str(r.get("bucket")): r for r in sc_full.get("rows") or []}
    ctech = cb.get("技术组重点任务进展") or {}
    check(
        "K6-03 技术组完备度 82 个任务中 77 有目标 / 80 有里程碑 / 73 有进展",
        int(ctech.get("tasks") or 0) == 82
        and int(ctech.get("has_goal") or 0) == 77
        and int(ctech.get("has_milestone") or 0) == 80
        and int(ctech.get("has_progress") or 0) == 73,
        str(ctech),
    )
    check(
        "K6-03 completeness 口径点明 has_* 是任务数不是子表条数",
        "不是子表条数" in str(sc_full.get("caliber", "")),
        str(sc_full.get("caliber"))[:160],
    )
    # 集团看板一条已发布进展都没有；intensity 的分母必须留住这批零期任务，
    # 换 INNER JOIN 会让这一行整行消失，均值也就被抬高了。
    sc_int = await _call(registry, "weekly_scale", by="project_group", mode="intensity")
    check(
        "K2-04 进展密度首位 关键技术攻关组 10 任务 / 99 行 / 9.90",
        str(_first(sc_int, "bucket")) == "关键技术攻关组"
        and int(_first(sc_int, "tasks") or 0) == 10
        and int(_first(sc_int, "progress_rows") or 0) == 99
        and str(_first(sc_int, "rows_per_task")) == "9.90",
        f"首行={_first(sc_int, 'bucket')} {_first(sc_int, 'rows_per_task')}",
    )
    sc_int_b = await _call(registry, "weekly_scale", by="board", mode="intensity")
    zero_row = next(
        (r for r in sc_int_b.get("rows") or [] if str(r.get("bucket")) == "集团重点任务调度"),
        {},
    )
    check(
        "K2-04 零进展看板仍在分母里（LEFT JOIN 保留，46 任务 / 0 行）",
        int(zero_row.get("tasks") or 0) == 46 and int(zero_row.get("progress_rows", -1)) == 0,
        str(zero_row),
    )
    bad_axis = await _call(registry, "weekly_scale", by="owner")
    check(
        "weekly_scale 未知分组轴报错并列出支持值",
        bad_axis.get("ok") is False
        and bad_axis.get("error", {}).get("code") == "unsupported_by"
        and "project_group" in str(bad_axis.get("error", {}).get("message", "")),
        f"err={bad_axis.get('error')}",
    )
    bad_smode = await _call(registry, "weekly_scale", by="board", mode="coverage")
    check(
        "weekly_scale 未知口径报错并列出三档",
        bad_smode.get("ok") is False and bad_smode.get("error", {}).get("code") == "unsupported_mode",
        f"err={bad_smode.get('error')}",
    )

    # I 类。O2OA 三个外部标识填充率互不相同：process_id/work_id 各 460，
    # task_id 只有 60。拿一列代答另一列会把缺失率答反。
    ext = await _call(registry, "weekly_submission_query", scope="external_ids")
    erow = (ext.get("rows") or [{}])[0]
    check(
        "I9-01 提交单外部标识 462 总 / 460 / 460 / 60，缺 task_id 402 占 87.0%",
        int(erow.get("total") or 0) == 462
        and int(erow.get("has_process_id") or 0) == 460
        and int(erow.get("has_work_id") or 0) == 460
        and int(erow.get("has_task_id") or 0) == 60
        and int(erow.get("missing_task_id") or 0) == 402
        and str(erow.get("missing_task_id_pct")) == "87.0",
        str(erow),
    )
    check(
        "I9-01 口径点明三列填充率不同，不可互相代答",
        "不要用其中一列代答另一列" in str(ext.get("caliber", "")),
        str(ext.get("caliber"))[:160],
    )
    # 「在途」必须枚举成员：status <> 'published' 会把 cancelled 那 1 张算进来，
    # 答成 60 而不是 59（negation_includes_cancelled）。
    infl = await _call(registry, "weekly_submission_query", scope="inflight_external")
    irow = (infl.get("rows") or [{}])[0]
    check(
        "I9-02 在途且有流程号 59 张（取反会算成 60）",
        int(irow.get("inflight_with_process_id") or 0) == 59,
        str(irow),
    )
    check(
        "I9-02 口径说明在途按成员枚举、cancelled 不算在途",
        "cancelled" in str(infl.get("caliber", ""))
        and "不用 status <> 'published' 取反" in str(infl.get("caliber", "")),
        str(infl.get("caliber"))[:180],
    )
    bad_sub = await _call(registry, "weekly_submission_query", scope="payload")
    check(
        "weekly_submission_query 未知口径报错而非退回默认列表",
        bad_sub.get("ok") is False and bad_sub.get("error", {}).get("code") == "unsupported_scope",
        f"err={bad_sub.get('error')}",
    )

    # I8-01. 提交单已发布、进展行仍未发布：两套码值各判一次。期数按 version_no
    # 去重，否则「几期」会答成「几行」。
    unpub = await _call(registry, "weekly_progress_coverage", scope="unpublished_by_task", limit=10)
    check(
        "I8-01 按任务列未发布期数 共 72 个任务，首两位 4 期",
        unpub.get("total_count") == 72
        and unpub.get("row_count") == 10
        and unpub.get("has_more") is True
        and int(_first(unpub, "unpublished_rounds") or 0) == 4,
        f"total={unpub.get('total_count')} rows={unpub.get('row_count')}",
    )
    check(
        "I8-01 口径说明按 version_no 去重、是期数不是行数",
        "是「期数」不是「行数」" in str(unpub.get("caliber", "")),
        str(unpub.get("caliber"))[:180],
    )

    # F4-04. 问的是「标识」不是「任务」：同一个标识挂 3 个任务只算一个标识。
    # 不去重会返回 128 行、同一个 id 重复三次，也就数不出有几个并列最长。
    longest = await _call(registry, "weekly_person_stats", scope="id_longest", top=6)
    lrows = longest.get("rows") or []
    top_ids = [str(r.get("owner_user_id")) for r in lrows if int(r.get("id_length") or 0) == 11]
    check(
        "F4-04 最长标识去重后 4 个 11 位 NDG 账号并列，且带 task_count",
        len(top_ids) == 4 and len(set(top_ids)) == 4 and all("task_count" in r for r in lrows),
        f"ids={top_ids}",
    )
    check(
        "F4-04 口径说明一行一个去重标识、并列要一起陈述",
        "一行一个去重后的标识" in str(longest.get("caliber", "")),
        str(longest.get("caliber"))[:160],
    )

    # R7-01. changed_tasks 是批次自己声明的数字。要判断「对不上」必须反查实际
    # 落库；LEFT JOIN 不可换 INNER，第 20 批声明 43、实落 0，正是最极端那条。
    rec = await _call(registry, "weekly_import_audit", reconcile_rows=True)
    b20 = next((r for r in rec.get("rows") or [] if int(r.get("id") or 0) == 20), {})
    check(
        "R7-01 第 20 批声明 43 实落 0（INNER JOIN 会丢掉这行）",
        int(b20.get("declared_tasks") or 0) == 43
        and int(b20.get("actual_tasks", -1)) == 0
        and int(b20.get("task_diff") or 0) == -43,
        str(b20),
    )
    check(
        "Q5-02 全部 20 个批次声明与实落都不等，由服务端给出 mismatched_batches",
        rec.get("mismatched_batches") == 20 and rec.get("row_count") == 20,
        f"mismatched={rec.get('mismatched_batches')} rows={rec.get('row_count')}",
    )
    plain = await _call(registry, "weekly_import_audit")
    check(
        "不核对时口径主动声明 changed_tasks 只是声明值",
        "未与实际落库行核对" in str(plain.get("caliber", "")),
        str(plain.get("caliber"))[:180],
    )

    # Q4-02. 「某看板哪些任务没设目标」要在服务端按看板过滤，否则拿全库行自己
    # 筛会把 total_count 一起丢掉。
    gmiss = await _call(registry, "weekly_year_goal_stats", scope="missing", year=2026, board="group", top=20)
    check(
        "Q4-02 集团看板 2026 无目标 6 个任务，口径回显看板过滤",
        gmiss.get("row_count") == 6 and gmiss.get("total_count") == 6 and "仅看板" in str(gmiss.get("caliber", "")),
        f"rows={gmiss.get('row_count')} total={gmiss.get('total_count')}",
    )
    bad_board = await _call(registry, "weekly_year_goal_stats", scope="missing", year=2026, board="nope")
    check(
        "年度目标未匹配看板显式报错",
        bad_board.get("ok") is False and bad_board.get("error", {}).get("code") == "board_not_found",
        f"err={bad_board.get('error')}",
    )

    # Q2-03. 「集团看板每个任务各有几个附件」问的是整整 46 行；不带 board 时
    # 全库任务在抢名次，集团的任务一个都排不上来。total_count 与 row_count
    # 相等才说明列全了。
    q23 = await _call(registry, "weekly_rank", metric="attachments", board="group", ascending=True, top=60)
    check(
        "Q2-03 集团看板逐任务附件 46 行，total_count 与 row_count 相等",
        q23.get("row_count") == 46 and q23.get("total_count") == 46 and q23.get("has_more") is False,
        f"rows={q23.get('row_count')} total={q23.get('total_count')}",
    )
    check(
        "Q2-03 口径回显看板过滤，并说明 total_count 应与 row_count 相等",
        "仅看板" in str(q23.get("caliber", "")) and "不等即未列全" in str(q23.get("caliber", "")),
        str(q23.get("caliber"))[:200],
    )
    short = await _call(registry, "weekly_rank", metric="attachments", board="group", ascending=True, top=5)
    check(
        "Q2-03 top 给小了时 total_count 仍报 46，据此可判断没列全",
        short.get("row_count") == 5 and short.get("total_count") == 46,
        f"rows={short.get('row_count')} total={short.get('total_count')}",
    )
    bad_rboard = await _call(registry, "weekly_rank", metric="attachments", board="nope")
    check(
        "weekly_rank 未匹配看板显式报错而非静默全库排名",
        bad_rboard.get("ok") is False and bad_rboard.get("error", {}).get("code") == "board_not_found",
        f"err={bad_rboard.get('error')}",
    )

    # ---- A6：审批流转状态 / 自然月窗 / 滞报榜 / 写法归档 / 最新一期 / 孤儿引用 ----

    # B6-01. 审批流转状态是全库口径，唯一不加发布闸门的分组。加了闸门只会剩
    # published 一档 128，问题问的那 22 条全部消失。
    wf = await _call(registry, "weekly_aggregate", group_by="workflow_status")
    wf_map = {str(r.get("group_name")): int(r.get("cnt", -1)) for r in wf.get("rows") or []}
    check(
        "B6-01 审批流转状态 7 档，published 128 / pending_audit 7 / cancelled 1",
        wf_map
        == {
            "published": 128,
            "pending_audit": 7,
            "pending_leader": 5,
            "pending_fill": 3,
            "rejected": 3,
            "signing": 3,
            "cancelled": 1,
        },
        str(wf_map),
    )
    wf_totals = wf.get("totals") or {}
    check(
        "B6-02/04 未发布 22 条、已发布占比 85.3 由服务端算好",
        int(wf_totals.get("total_tasks", -1)) == 150
        and int(wf_totals.get("unpublished_tasks", -1)) == 22
        and str(wf_totals.get("published_pct")) == "85.3",
        str(wf_totals),
    )
    check(
        "B6 口径点明与业务状态不是一套词汇，且未发布不能按在途各档相加",
        "不是一套词汇" in str(wf.get("caliber", "")) and "cancelled" in str(wf.get("caliber", "")),
        str(wf.get("caliber"))[:200],
    )
    # 对照组：业务状态分组仍带发布闸门，两个口径不能互相顶替。
    biz = await _call(registry, "weekly_aggregate", group_by="status")
    biz_map = {str(r.get("group_name")): int(r.get("cnt", -1)) for r in biz.get("rows") or []}
    check(
        "B6 对照 业务状态仍在发布闸门内（14/78/31/5，合计 128）",
        biz_map == {"未开始": 14, "进行中": 78, "已完成": 31, "已停用": 5},
        str(biz_map),
    )

    # R6-03. 自然月与 90 天是两个窗口：三个月前是 2026-05-15，90 天前是
    # 2026-05-17，中间夹着 3 行，5 月桶因此 16 vs 13。
    m3 = await _call(registry, "weekly_group_history", by="month", last_months=3)
    m3_map = {str(r.get("bucket")): int(r.get("progress_count", -1)) for r in m3.get("rows") or []}
    check(
        "R6-03 最近三个月按自然月回溯 16/30/12/45",
        m3_map == {"2026-05": 16, "2026-06": 30, "2026-07": 12, "2026-08": 45},
        str(m3_map),
    )
    d90 = await _call(registry, "weekly_group_history", by="month", last_days=90)
    check(
        "R6-03 反例 90 天窗 5 月只有 13，两个窗口不可互替",
        int({str(r.get("bucket")): r.get("progress_count") for r in d90.get("rows") or []}.get("2026-05", -1)) == 13,
        str({str(r.get("bucket")): r.get("progress_count") for r in d90.get("rows") or []}),
    )
    check(
        "R6-03 口径写明自然月回溯而非 90 天",
        "自然月回溯" in str(m3.get("caliber", "")) and "2026-05-15" in str(m3.get("caliber", "")),
        str(m3.get("caliber"))[:220],
    )
    both = await _call(registry, "weekly_group_history", by="month", last_days=90, last_months=3)
    check(
        "R6-03 两个窗口同时给显式报错，不静默合成第三个窗口",
        both.get("ok") is False and both.get("error", {}).get("code") == "invalid_argument",
        str(both.get("error")),
    )

    # R6-04. 滞报榜取 MAX(report_time)，用最早一期会把老任务全排到榜首。
    lag = await _call(registry, "weekly_group_history", by="lag", limit=5)
    check(
        "R6-04 滞报最久前 5 名 105/110/137/124/99，天数 16/14/14/13/12",
        [(int(r.get("task_id", -1)), int(r.get("lag_days", -1))) for r in lag.get("rows") or []]
        == [(105, 16), (110, 14), (137, 14), (124, 13), (99, 12)],
        str([(r.get("task_id"), r.get("lag_days")) for r in lag.get("rows") or []]),
    )
    check(
        "R6-04 上榜任务 46 个，口径声明从未报过的不在榜上",
        int(lag.get("total_tasks", -1)) == 46 and "从未报过的不在榜上" in str(lag.get("caliber", "")),
        f"total={lag.get('total_tasks')} caliber={str(lag.get('caliber'))[:120]}",
    )

    # E4-03. 「各种写法各有多少条」问的是 6 个格式档，不是 28 个去重取值；
    # 两者差一个量级，档位判别的优先级也必须固定（'2026年6月底' 归含「底」）。
    fmt = await _call(registry, "weekly_group_stats", scope="completion_time_formats")
    fmt_map = {str(r.get("fmt")): int(r.get("cnt", -1)) for r in fmt.get("rows") or []}
    check(
        "E4-03 完成时间 6 档 其他 12 / 含底 11 / 标准日期 6 / 季度 6 / 中文年月 6 / 中文年月日 5",
        fmt_map
        == {
            "其他": 12,
            "模糊表述（含“底”）": 11,
            "标准日期 YYYY-MM-DD": 6,
            "季度 YYYYQn": 6,
            "中文年月": 6,
            "中文年月日": 5,
        },
        str(fmt_map),
    )
    check(
        "E4-03 各档相加 46 等于 total_count（一条只进一档）",
        sum(fmt_map.values()) == 46 and int(fmt.get("total_count", -1)) == 46,
        f"sum={sum(fmt_map.values())} total={fmt.get('total_count')}",
    )
    # 去重取值是另一个口径，28 个，两者不能互答。
    vals = await _call(registry, "weekly_group_stats", scope="completion_time_values", top=60)
    check(
        "E4-03 对照 去重取值 28 个，与 6 档不是同一个问题",
        int(vals.get("total_count", -1)) == 28,
        f"total={vals.get('total_count')}",
    )

    # K4-04. 状态在 task 上、成效在集团明细表里，矛盾判定必须两侧同时给条件；
    # 缺 non_empty 会把「未开始且成效为空」也收进来，那并不矛盾。
    k44 = await _call(
        registry,
        "weekly_group_detail_query",
        status="0",
        non_empty="progress_effect",
        fields="progress_effect",
    )
    check(
        "K4-04 未开始却写了成效的 6 条（97/108/130/137/140/142）",
        [int(r.get("task_id", -1)) for r in k44.get("rows") or []] == [97, 108, 130, 137, 140, 142]
        and int(k44.get("total_count", -1)) == 6,
        str([r.get("task_id") for r in k44.get("rows") or []]),
    )
    check(
        "K4-04 口径区分业务状态与审批流转状态",
        "与审批流转状态" in str(k44.get("caliber", "")) and "progress_effect 非空" in str(k44.get("caliber", "")),
        str(k44.get("caliber"))[:200],
    )
    k44_open = await _call(registry, "weekly_group_detail_query", status="0", fields="progress_effect")
    check(
        "K4-04 不加 non_empty 时行数会变（矛盾判定必须带非空条件）",
        int(k44_open.get("total_count", -1)) >= 6,
        f"total={k44_open.get('total_count')}",
    )
    bad_status = await _call(registry, "weekly_group_detail_query", status="9")
    check(
        "weekly_group_detail_query 非法 status 报错而非静默忽略",
        bad_status.get("ok") is False and bad_status.get("error", {}).get("code") == "invalid_status",
        str(bad_status.get("error")),
    )
    bad_ne = await _call(registry, "weekly_group_detail_query", non_empty="nope")
    check(
        "weekly_group_detail_query 非法 non_empty 列名报错并给出值域",
        bad_ne.get("ok") is False and bad_ne.get("error", {}).get("code") == "unsupported_field",
        str(bad_ne.get("error"))[:160],
    )

    # F5-01. 牵头人/项目负责人两列都在集团明细表里，不在共享的 task 行上。
    f51 = await _call(
        registry,
        "weekly_group_detail_query",
        fields="lead_owner_names,project_owner_names,project_group",
        limit=8,
    )
    check(
        "F5-01 前 8 条带牵头人与项目负责人（首行 唐立本 / 胡建国,方永康,邓少华）",
        f51.get("row_count") == 8
        and _first(f51, "lead_owner_names") == "唐立本"
        and _first(f51, "project_owner_names") == "胡建国,方永康,邓少华",
        f"lead={_first(f51, 'lead_owner_names')} proj={_first(f51, 'project_owner_names')}",
    )
    check(
        "F5-01 total_count 46 说明只是截断到 8 条，不是全部",
        int(f51.get("total_count", -1)) == 46,
        f"total={f51.get('total_count')}",
    )

    # C4-01 / C4-02. 一任务一行是这个 scope 的全部意义：任务 13 有 16 期，
    # 不按 version_no 收敛就会出 16 行，且最老那期的下一步会被当成现在的安排。
    c41 = await _call(registry, "weekly_progress_coverage", scope="latest_round", project_group="算力网络组")
    check(
        "C4-01 算力网络组最新一期下一步 8 条，一任务一行",
        [int(r.get("task_id", -1)) for r in c41.get("rows") or []] == [8, 9, 13, 17, 31, 44, 45, 94]
        and int(c41.get("total_count", -1)) == 8,
        str([r.get("task_id") for r in c41.get("rows") or []]),
    )
    check(
        "C4-01 任务 13 取到 version_no 16 而非更早期号",
        {int(r.get("task_id", -1)): int(r.get("version_no", -1)) for r in c41.get("rows") or []}.get(13) == 16,
        str([(r.get("task_id"), r.get("version_no")) for r in c41.get("rows") or []]),
    )
    check(
        "C4-01 口径写明不按 progress_date 取最新",
        "不是按 progress_date 取最新" in str(c41.get("caliber", "")),
        str(c41.get("caliber"))[:200],
    )
    c42 = await _call(registry, "weekly_progress_coverage", scope="latest_round", project_group="标准安全组")
    check(
        "C4-02 标准安全组 10 条（首行任务 1，末行任务 86）",
        [int(r.get("task_id", -1)) for r in c42.get("rows") or []] == [1, 11, 12, 18, 24, 30, 36, 46, 52, 86]
        and int(c42.get("total_count", -1)) == 10,
        str([r.get("task_id") for r in c42.get("rows") or []]),
    )
    # C4-04. 0 是结论：最新一期都写了下一步。别把它当空结果去换口径重查。
    c44 = await _call(registry, "weekly_progress_coverage", scope="missing_next")
    check(
        # 用字符串比而不是 int(x or -1)：0 本身是假值，会被 or 兑成 -1 而误判。
        "C4-04 最新一期缺下一步的任务数 = 0",
        str(_first(c44, "tasks_missing_next")) == "0",
        str(c44.get("rows")),
    )
    check(
        "C4-04 口径写明中间某期空着不算",
        "中间某期空着不算" in str(c44.get("caliber", "")),
        str(c44.get("caliber"))[-80:],
    )

    # R7-04. 孤儿与「未走导入」是两回事：120 条手工填报不能算成引用不完整。
    orph = await _call(registry, "weekly_import_audit", orphans=True)
    check(
        # SUM() 回字符串、COUNT() 回整数，一律按字符串比，顺带避开 0 被 or 兑掉。
        "R7-04 孤儿进展 0 条 / 孤儿批次 0 个，未走导入的 120 条单列",
        str(_first(orph, "orphan_rows")) == "0"
        and str(_first(orph, "orphan_batch_ids")) == "0"
        and str(_first(orph, "rows_without_import")) == "120",
        str(orph.get("rows")),
    )
    check(
        "R7-04 附带 20 个批次的对账数，孤儿检查与对账同时给",
        int(orph.get("reconciliation", {}).get("batch_count") or 0) == 20,
        str(orph.get("reconciliation")),
    )
    check(
        "R7-04 口径声明 0 即引用完整、不要换口径重算",
        "不要换口径重算" in str(orph.get("caliber", "")),
        str(orph.get("caliber"))[-60:],
    )

    # A7 治理循环调用：以下三处是基线里重复调用最凶的题，缺的不是数据而是
    # 「一次调用答完」的路径。基线里 weekly_progress_history 被单题调 74 次、
    # weekly_attachment_query 51 次，6 轮题通过率只有 12.1%——慢与错同源。

    # I4-03. 提交单只加软删闸门，不加任务发布闸门。
    kind = await _call(registry, "weekly_submission_query", scope="by_kind")
    kind_map = {str(r.get("submission_kind")): int(r.get("submission_count", -1)) for r in kind.get("rows") or []}
    check(
        "I4-03 提交单类型 progress 312 / initial 150，相加 462",
        kind_map == {"progress": 312, "initial": 150} and sum(kind_map.values()) == 462,
        str(kind_map),
    )
    check(
        "I4-03 口径写明不加任务发布闸门",
        "不加任务发布闸门" in str(kind.get("caliber", "")),
        str(kind.get("caliber"))[:80],
    )

    # O3-04. NOT EXISTS 判存在性，分母另给，别拿 22 当分母。
    zero = await _call(registry, "weekly_attachment_stats", scope="zero_attachment")
    check(
        "O3-04 零附件正式任务 22 条，首行任务 9，分母 128",
        zero.get("row_count") == 22
        and int(_first(zero, "task_id") or -1) == 9
        and int(zero.get("total_formal_tasks") or -1) == 128,
        f"rows={zero.get('row_count')} 分母={zero.get('total_formal_tasks')}",
    )
    check(
        "O3-04 口径点明 NOT EXISTS 判定与分母区分",
        "NOT EXISTS" in str(zero.get("caliber", "")) and "total_formal_tasks 才是分母" in str(zero.get("caliber", "")),
        str(zero.get("caliber"))[-90:],
    )

    # R5-01. 看板在 task 上，附件表没有 board_id，按看板筛必须 JOIN 回任务。
    gatt = await _call(registry, "weekly_attachment_query", board="group", limit=10)
    check(
        "R5-01 集团组附件前 10 按 task_id 定序（97/97/97/101/102/103/104...）",
        [int(r.get("task_id", -1)) for r in gatt.get("rows") or []] == [97, 97, 97, 101, 102, 103, 104, 104, 104, 104],
        str([r.get("task_id") for r in gatt.get("rows") or []]),
    )
    check(
        "R5-01 带 board 时同时给出 task_name，否则答不了「哪些任务」",
        all(str(r.get("task_name") or "") for r in gatt.get("rows") or []),
        str(_first(gatt, "task_name")),
    )
    gall = await _call(registry, "weekly_attachment_query", board="group")
    check(
        "R5-01 集团组共 52 个有效附件，一次调用列全",
        gall.get("row_count") == 52 and gall.get("has_more") is False,
        f"rows={gall.get('row_count')} has_more={gall.get('has_more')}",
    )
    gname = await _call(registry, "weekly_attachment_query", board="集团重点任务调度", limit=3)
    check(
        "R5-01 看板 code 与看板名都能认",
        gname.get("ok") is not False and int(_first(gname, "task_id") or -1) == 97,
        str(_first(gname, "task_id")),
    )
    gbad = await _call(registry, "weekly_attachment_query", board="不存在的组")
    check(
        "R5-01 错看板名报 board_not_found 而非静默返回全库",
        gbad.get("ok") is False and gbad.get("error", {}).get("code") == "board_not_found",
        str(gbad.get("error")),
    )
    plain = await _call(registry, "weekly_attachment_query", limit=3)
    check(
        "R5-01 对照 不带 board 时仍不带 task_name（旧行为未变）",
        plain.get("row_count") == 3 and "task_name" not in (plain.get("rows") or [{}])[0],
        str(list((plain.get("rows") or [{}])[0])),
    )
    check(
        "storage_path 在按看板筛时同样不外泄",
        all("storage_path" not in r for r in gatt.get("rows") or []),
        str(list((gatt.get("rows") or [{}])[0])),
    )

    # J2-03. 金标就是取最大的那一条，largest + top=1 一次即答。
    big = await _call(registry, "weekly_attachment_stats", scope="largest", top=1)
    check(
        "J2-03 最大附件一次即答（行业数据标注基地能力建设-会议纪要.pdf / 8379724 字节）",
        big.get("row_count") == 1
        and int(_first(big, "file_size") or -1) == 8379724
        and str(_first(big, "file_name")) == "行业数据标注基地能力建设-会议纪要.pdf",
        f"{_first(big, 'file_name')} {_first(big, 'file_size')}",
    )

    # A8. 病灶是「从被截断的 200 行清单里手数」——模型自己都写过「无法精确求出
    # 全库总数」。以下各档一律服务端聚合完再回，断言盯的是数字本身与分母口径。
    ifc = await _call(registry, "weekly_submission_query", scope="inflight_count")
    check(
        "I3-01 在途提交单 61 张、分布在 55 个任务上",
        str(_first(ifc, "inflight_submissions")) == "61" and str(_first(ifc, "tasks")) == "55",
        str(ifc.get("rows")),
    )
    ifb = await _call(registry, "weekly_submission_query", scope="inflight_by_board")
    ifb_rows = {(r.get("board_code"), r.get("status")): r.get("submission_count") for r in ifb.get("rows") or []}
    check(
        "I3-02 在途按看板 + 状态两维分 9 档，rejected 同属在途（group 4 / tech 9）",
        ifb.get("row_count") == 9
        and str(ifb_rows.get(("group", "rejected"))) == "4"
        and str(ifb_rows.get(("tech", "rejected"))) == "9"
        and str(ifb_rows.get(("group", "pending_audit"))) == "14"
        and str(ifb_rows.get(("tech", "pending_leader"))) == "10",
        str(ifb.get("rows")),
    )
    check(
        "I3-02 各档相加等于在途总数 61（漏掉任一档即少算）",
        sum(int(v) for v in ifb_rows.values()) == 61,
        str(sorted(ifb_rows.items())),
    )
    ifm = await _call(registry, "weekly_submission_query", scope="inflight_multi")
    check(
        "I3-02 同时挂 2 张在途单的任务 6 个，服务端 HAVING 判定",
        ifm.get("row_count") == 6 and all(str(r.get("pending_submissions")) == "2" for r in ifm.get("rows") or []),
        str([r.get("task_id") for r in ifm.get("rows") or []]),
    )
    sgs = await _call(registry, "weekly_submission_query", scope="sign_summary")
    check(
        "I4-01 需会签 155 / 不需 307 / 合计 462",
        str(_first(sgs, "need_sign")) == "155"
        and str(_first(sgs, "no_sign")) == "307"
        and str(_first(sgs, "total")) == "462",
        str(sgs.get("rows")),
    )
    check(
        # need_sign 是标记、signing 是当前节点，两者答的不是一个问题。
        "I4-01 need_sign 不等于在途 signing 的 9 张（两套口径已在 caliber 里点明）",
        "signing" in str(sgs.get("caliber")),
        str(sgs.get("caliber")),
    )
    bys = await _call(registry, "weekly_submission_query", scope="by_signer")
    check(
        "I4-02 会签人 9 位，罗小川 29 单居首、郑亚楠 1 单垫底",
        bys.get("row_count") == 9
        and str(_first(bys, "signer_name")) == "罗小川"
        and str(_first(bys, "signed_count")) == "29"
        and all(str(r.get("signer_name") or "") for r in bys.get("rows") or []),
        str([(r.get("signer_name"), r.get("signed_count")) for r in bys.get("rows") or []]),
    )
    sgt = await _call(registry, "weekly_submission_query", scope="sign_turnaround")
    sgt_rows = {str(r.get("need_sign")): (r.get("n"), r.get("avg_days")) for r in sgt.get("rows") or []}
    check(
        "I4-03 会签耗时 128 单 14.7 天 vs 不会签 274 单 14.5 天，未完结的不进分母",
        sgt.get("row_count") == 2
        and str(sgt_rows.get("1")) == "(128, '14.7')"
        and str(sgt_rows.get("0")) == "(274, '14.5')",
        str(sgt.get("rows")),
    )
    check(
        "I4-03 两档相加 402 < 462，caliber 已写明未完结不计",
        sum(int(v[0]) for v in sgt_rows.values()) == 402 and "402" in str(sgt.get("caliber")),
        str(sgt.get("caliber")),
    )
    rpt = await _call(registry, "weekly_submission_query", scope="rounds_per_task")
    check(
        "I5-01 人均提交轮次 3.08 = 462 / 150，分子分母一并回",
        str(_first(rpt, "avg_rounds")) == "3.08"
        and str(_first(rpt, "total_submissions")) == "462"
        and str(_first(rpt, "tasks")) == "150",
        str(rpt.get("rows")),
    )
    pvp = await _call(registry, "weekly_submission_query", scope="published_vs_progress")
    check(
        "I5-02 已发布进展提交单 272 vs 已发布进展行 943（两表两闸门）",
        str(_first(pvp, "published_progress_submissions")) == "272"
        and str(_first(pvp, "published_progress_rows")) == "943",
        str(pvp.get("rows")),
    )
    bna = await _call(registry, "weekly_workflow_query", scope="by_node_action")
    bna_rows = {(r.get("node_type"), r.get("action")): r.get("action_count") for r in bna.get("rows") or []}
    check(
        "I3-03 动作按 node_type + action 分 6 档，approved 在三个节点各自计数",
        bna.get("row_count") == 6
        and str(bna_rows.get(("fill", "submitted"))) == "460"
        and str(bna_rows.get(("audit", "approved"))) == "400"
        and str(bna_rows.get(("leader", "approved"))) == "400"
        and str(bna_rows.get(("sign", "approved"))) == "155"
        and str(bna_rows.get(("admin", "created"))) == "150"
        and str(bna_rows.get(("audit", "rejected"))) == "13",
        str(bna.get("rows")),
    )
    check(
        "I3-03 各档相加等于动作总数 1578",
        sum(int(v) for v in bna_rows.values()) == 1578,
        str(sorted(bna_rows.items())),
    )
    apt = await _call(registry, "weekly_workflow_query", scope="actions_per_task")
    check(
        "I3-04 人均动作 10.52 = 1578 / 150，分母是有动作的任务数而非 128",
        str(_first(apt, "avg_actions")) == "10.52"
        and str(_first(apt, "total_actions")) == "1578"
        and str(_first(apt, "tasks")) == "150",
        str(apt.get("rows")),
    )
    lnk = await _call(registry, "weekly_group_history", by="linkage")
    check(
        "I8-03 集团成效历史 404 行全部未挂提交单（linked 0），分母不是过闸的 362",
        str(_first(lnk, "total_rows")) == "404"
        and str(_first(lnk, "linked_rows")) == "0"
        and str(_first(lnk, "unlinked_rows")) == "404"
        and str(_first(lnk, "published_rows")) == "362",
        str(lnk.get("rows")),
    )
    check(
        # 0 必须被读成「确实没有挂接」，不能被读成「查不到」。
        "I8-03 caliber 明说 0 即没有挂接，不是查不到",
        "不是查不到" in str(lnk.get("caliber")),
        str(lnk.get("caliber")),
    )
    badflow = await _call(registry, "weekly_workflow_query", scope="by_action")
    check(
        "A8 动作侧错 scope 报 unsupported_scope 并列出值域，而非静默返回全量日志",
        badflow.get("ok") is False and badflow.get("error", {}).get("code") == "unsupported_scope",
        str(badflow.get("error")),
    )

    # A9 F 类：完整率百分比、按组点名、集团看板负责人两列分歧。
    pct = await _call(registry, "weekly_field_completeness", field="project_owner_id")
    check(
        # 模型在基线里答「完整率 100%」，与它自己引用的 119/128 自相矛盾。
        "F3-04 项目负责人 ID 完整率由服务端给出 128/119/93.0",
        str(_first(pct, "total")) == "128"
        and str(_first(pct, "filled")) == "119"
        and str(_first(pct, "filled_pct")) == "93.0",
        str(pct.get("rows")),
    )
    check(
        "F3-04 caliber 点明 filled_pct 已算好、不要自己重算",
        "不要自己拿 filled / total 重算" in str(pct.get("caliber")),
        str(pct.get("caliber")),
    )
    full = await _call(registry, "weekly_field_completeness", field="project_owner_name")
    check(
        # 姓名列确实是 100%，两列口径不同这件事必须两边都能验出来。
        "F3-01 姓名列 128/128/100.0，与 ID 列的 93.0 不是同一个问题",
        str(_first(full, "filled")) == "128" and str(_first(full, "filled_pct")) == "100.0",
        str(full.get("rows")),
    )

    roster = await _call(registry, "weekly_person_stats", scope="group_roster", project_group="标准安全组")
    names = [r.get("person") for r in roster.get("rows") or []]
    check(
        "F7-03 标准安全组牵头人 9 人，行数即人数（该组 19 条任务）",
        roster.get("row_count") == 9 and len(names) == 9,
        str(names),
    )
    check(
        "F7-03 九人姓名与金标逐名一致",
        set(names)
        == {
            "吴晓东",
            "李建华",
            "周文斌",
            "孙立群",
            "张国栋",
            "王振国",
            "赵明辉",
            "陈志远",
            "project_lead_b",
        },
        str(sorted(names)),
    )
    check(
        "F7-03 caliber 点明不要拿任务条数当人数",
        "不要拿任务条数当人数" in str(roster.get("caliber")),
        str(roster.get("caliber")),
    )
    noroster = await _call(registry, "weekly_person_stats", scope="group_roster")
    check(
        # 缺组名时必须报错并指路，不能默默退化成全库点名。
        "F7-03 group_roster 缺 project_group 时报 missing_project_group 并指路",
        noroster.get("ok") is False and noroster.get("error", {}).get("code") == "missing_project_group",
        str(noroster.get("error")),
    )
    pg = await _call(registry, "weekly_task_query", project_group="标准安全组", limit=200)
    check(
        "F7-03 weekly_task_query 支持 project_group 精确筛，19 条",
        pg.get("ok") is True and str(pg.get("total_count")) == "19",
        str(pg.get("total_count")),
    )

    det = await _call(
        registry,
        "weekly_group_detail_query",
        task="数据资产入表试点推进",
        fields="project_owner_names,project_owner_ids",
    )
    check(
        # 金标是明细表的三人，模型答的「秦怀瑾」来自 task 行的单值列。
        "F5-03 集团明细表负责人为三人且人数由服务端算出",
        str(_first(det, "project_owner_names")) == "胡建国,方永康,邓少华"
        and str(_first(det, "project_owner_count")) == "3",
        str(det.get("rows")),
    )
    check(
        "F5-03 caliber 点明该列与 task 上同名单值列并非同一数据",
        "并非同一个数据" in str(det.get("caliber")),
        str(det.get("caliber")),
    )
    task_row = await _call(registry, "weekly_task_query", keyword="数据资产入表试点推进", limit=5)
    single = [r.get("project_owner_name") for r in task_row.get("rows") or [] if r.get("id") == 97]
    check(
        # 两列确实不一致这件事本身要锁住：断言只写一边，改坏另一边不会被发现。
        "F5-03 task 行上的单值列确为「秦怀瑾」，两列不一致是数据事实",
        single == ["秦怀瑾"],
        str(single),
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

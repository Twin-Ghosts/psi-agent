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

# ruff: noqa: RUF001  中文口径文案里的全角标点是给模型看的字面量, 不能换成半角。
# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio

WORKSPACE = Path(__file__).resolve().parent.parent
REPO_SRC = WORKSPACE.parent.parent / "src"
sys.path.insert(0, str(REPO_SRC))

from psi_agent.session.tool_registry import ToolRegistry  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"

_results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((PASS if condition else FAIL, name, detail))


async def _call(registry: ToolRegistry, tool: str, **kwargs: Any) -> dict[str, Any]:
    func = registry.get(tool)
    if func is None:
        raise AssertionError(f"tool not registered: {tool}")
    return json.loads(await func(**kwargs))


async def run() -> int:
    registry = await ToolRegistry.load(WORKSPACE / "tools", "smoke")

    expected_tools = {
        "weekly_schema",
        "weekly_task_query",
        "weekly_task_detail",
        "weekly_progress_history",
        "weekly_aggregate",
        "weekly_milestone_query",
        "weekly_workflow_query",
        "weekly_attachment_query",
        "weekly_import_audit",
        "weekly_freshness",
        "weekly_health",
    }
    loaded = set(registry.tools)
    check(
        "所有 11 个工具注册，且无 helper 泄漏为工具",
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

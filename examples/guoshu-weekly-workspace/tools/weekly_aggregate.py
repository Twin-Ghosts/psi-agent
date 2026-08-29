from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

_MCP_PATH = Path(__file__).resolve().parent / "_weekly_mcp.py"
_MODULE_NAME = f"guoshu_weekly_tool__weekly_mcp_{hashlib.sha256(str(_MCP_PATH).encode()).hexdigest()[:12]}"
_module = sys.modules.get(_MODULE_NAME)
if _module is None:
    _module = types.ModuleType(_MODULE_NAME)
    _module.__file__ = str(_MCP_PATH)
    sys.modules[_MODULE_NAME] = _module
    exec(compile(_MCP_PATH.read_text(encoding="utf-8"), str(_MCP_PATH), "exec"), _module.__dict__)
_call = _module.__dict__["call"]
_invalid = _module.__dict__["invalid_argument"]


async def weekly_aggregate(group_by: str, board: str = "", metric: str = "count", top: int = 0) -> str:
    """Aggregate formal tasks by one dimension.

    Empty groups are preserved (the service puts the caliber on the JOIN's ON
    clause, per R-02/R-08), so a zero row means genuinely zero tasks -- do not
    treat a missing group as zero without checking here first.

    category and primary_category are two different questions: tasks only attach
    to 二级分类, and 一级分类 is reached through the parent_id hop. Asking for
    分类 with group_by="category" answers with 47 buckets where the question
    wanted 6.

    Args:
        group_by: One of board / category / primary_category / status /
            project_group / owner.
        board: Optional board code or name to scope the aggregation. For
            primary_category this scopes the category tree's own board, which is
            a different path from the task's board.
        metric: Only "count" is supported.
        top: When > 0, hard-cuts to that many groups in SQL and says in the
            caliber how many groups exist in total. Use it for "前 N 个" so the
            row count is the answer -- ties past the boundary are excluded by
            the question, not missing from the data.
    """
    if not group_by.strip():
        return _invalid("group_by must not be empty")
    try:
        cut = max(0, int(top))
    except TypeError, ValueError:
        return _invalid("top must be an integer")
    return await _call(
        "weekly_aggregate",
        {"group_by": group_by, "board": board, "metric": metric, "top": cut},
    )


async def weekly_freshness() -> str:
    """Report each board's latest progress time and formal task count.

    Call this before answering any relative-time question ("this week", "recently").
    Anchor to these snapshot times, never to the machine wall clock.
    """
    return await _call("weekly_freshness", {})


async def weekly_import_audit(limit: int = 200, reconcile_rows: bool = False) -> str:
    """Reconcile Excel import batches against distinct snapshot dates (R-09/R-10).

    Compare batch_count with distinct_dates and distinct_import_times to tell a
    single import from repeated ones.

    Args:
        limit: Max batch rows to return, capped at 200.
        reconcile_rows: When True, also look up what each batch actually landed
            (via task_progress.import_id) and compare it against the batch's own
            changed_tasks. Required for "声明与实际对不上" questions -- the
            default path reports the declared number only and cannot tell you
            whether it is true.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call(
        "weekly_import_audit",
        {"limit": bounded, "reconcile_rows": bool(reconcile_rows)},
    )


async def weekly_scale(by: str = "board", mode: str = "totals", year: int = 2026) -> str:
    """Cross-section formal tasks over several child tables at once, de-duplicated.

    Use this instead of calling weekly_aggregate once per dimension: joining the
    child tables separately and pasting the numbers together is where fan-out
    creeps in. Every child count here is COUNT(DISTINCT ...), so per-group
    milestones sum to the whole-library milestone total -- if your numbers sum to
    more than that, they were multiplied by another JOIN.

    totals and completeness answer different questions: totals gives child-row
    counts (how many milestones), completeness gives task counts (how many tasks
    have at least one milestone). Do not use one to answer the other.

    Args:
        by: Grouping axis: board / project_group / primary_category.
        mode: "totals" tasks plus milestone / attachment / annual-goal counts.
            "completeness" how many tasks have a goal / milestone / progress.
            "intensity" published progress rows and rows per task, with
            zero-period tasks kept in the denominator.
        year: Which year the annual-goal column looks at. Ignored by intensity.
    """
    try:
        yr = int(year)
    except TypeError, ValueError:
        return _invalid("year must be an integer")
    return await _call("weekly_scale", {"by": by, "mode": mode, "year": yr})

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


async def weekly_year_goal_query(task: str = "", year: int = 0, limit: int = 200) -> str:
    """List annual goals and milestone summaries. One row per task per year.

    Use this for "what is task X's 2026 goal" and for board-wide goal listings.
    Read total_count / total_tasks for counting questions -- rows may be truncated
    at 200 while the totals stay exact.

    Args:
        task: Task id or name; empty covers every formal task.
        year: Four-digit year; 0 covers all years.
        limit: Max rows, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
        yr = max(0, int(year))
    except TypeError, ValueError:
        return _invalid("year and limit must be integers")
    return await _call(
        "weekly_year_goal_query",
        {"task": task, "year": yr, "limit": bounded},
    )


async def weekly_year_goal_stats(
    scope: str = "by_year",
    year: int = 0,
    year_to: int = 0,
    min_years: int = 3,
    in_progress_only: bool = False,
    board: str = "",
    top: int = 8,
) -> str:
    """Aggregate annual-goal coverage: which years are set, and who lacks a goal.

    Use this instead of listing goals and counting by hand. Coverage counts tasks
    that have no goal row at all, which listing goals cannot show.

    Args:
        scope: by_year (goals and tasks per year) / coverage (share of formal tasks
            holding a goal for year) / missing (tasks without one) /
            missing_by_group (missing counts per 专项组) / span (average years per
            task, plus tasks reaching min_years) / multi_year (tasks holding goals
            in both year and year_to).
        year: Primary year. Required except for by_year and span.
        year_to: Second year, for multi_year.
        min_years: Threshold for span; inclusive.
        in_progress_only: For missing, keep only 在办任务 (status 0 未开始 and
            1 进行中). "在办任务还没定目标" asks about that subset -- 已完成 or
            已暂停 tasks without a goal are not a gap and inflate the row set.
        board: Optional board code or name; scopes every scope to one board.
            Use it for "某看板哪些任务没设目标" rather than filtering the
            whole-library rows by eye, which silently drops the total_count.
        top: Row cap for the listing scopes.
    """
    try:
        bounded = max(1, min(200, int(top)))
        yr = max(0, int(year))
        yr2 = max(0, int(year_to))
        span = max(1, int(min_years))
    except TypeError, ValueError:
        return _invalid("year, year_to, min_years and top must be integers")
    return await _call(
        "weekly_year_goal_stats",
        {
            "scope": scope,
            "year": yr,
            "year_to": yr2,
            "min_years": span,
            "in_progress_only": bool(in_progress_only),
            "board": board,
            "top": bounded,
        },
    )


async def weekly_milestone_stats(
    scope: str = "summary",
    by: str = "category",
    year: int = 0,
    category: str = "",
    min_total: int = 0,
    kind: str = "task_done_milestones_open",
    top: int = 8,
) -> str:
    """Aggregate milestone completion. weekly_milestone_query only lists rows.

    Milestone status is a two-value code: 1 已完成, 0 未完成. Completion rates come
    from the server; do not derive them by counting listed rows.

    Args:
        scope: summary (totals and finish rate) / by_dimension (grouped by `by`) /
            deleted (soft-delete audit) / per_task (counts per task, zero-milestone
            tasks kept) / mismatch (task status vs milestone status disagreements).
        by: Dimension for by_dimension: year / category / group_name / status /
            task_status.
        year: Restrict to one milestone year; 0 covers all.
        category: Restrict to one milestone category.
        min_total: For by_dimension, drop buckets below this count; inclusive.
        kind: For mismatch: task_done_milestones_open or milestones_done_task_open.
        top: Row cap for the listing scopes.
    """
    try:
        bounded = max(1, min(200, int(top)))
        yr = max(0, int(year))
        floor = max(0, int(min_total))
    except TypeError, ValueError:
        return _invalid("year, min_total and top must be integers")
    return await _call(
        "weekly_milestone_stats",
        {
            "scope": scope,
            "by": by,
            "year": yr,
            "category": category,
            "min_total": floor,
            "kind": kind,
            "top": bounded,
        },
    )

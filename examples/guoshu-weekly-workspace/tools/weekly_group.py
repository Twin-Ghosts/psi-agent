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


async def weekly_group_detail_query(
    task: str = "",
    fields: str = "",
    contains: str = "",
    field: str = "",
    limit: int = 200,
) -> str:
    """Query the group board's own detail table: goals, measures, owners, due text.

    The group board (集团组) keeps 目标成果 / 实施举措 / 进度成效 / 完成时间 and its
    owner columns in a separate table. weekly_task_query does not carry any of
    those columns, so use this tool for group-board questions about them.

    completion_time is display text like "2026年内" or "2026Q4", not a date. Filter
    it with contains, and never compute date arithmetic on it.

    Args:
        task: Task id or name; empty covers the whole board.
        fields: Comma-separated columns to return; empty returns the common set.
        contains: Substring filter applied to the column named by field.
        field: Which column contains filters on; required when contains is set.
        limit: Max rows, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    if contains.strip() and not field.strip():
        return _invalid("field is required when contains is set")
    return await _call(
        "weekly_group_detail_query",
        {
            "task": task,
            "fields": fields,
            "contains": contains,
            "field": field,
            "limit": bounded,
        },
    )


async def weekly_group_owner_query(person: str = "", role: str = "lead", limit: int = 200) -> str:
    """Find group-board tasks by owner, matching multi-value owner columns exactly.

    The owner columns hold comma-separated values, so substring matching collides
    across people. The server matches per element instead.

    Lead (牵头人) and project owner (项目负责人) are different roles on different
    columns. Pick the one the question asks about; do not merge them.

    Args:
        person: Person id or name; empty lists every task's owners for the role.
        role: lead or project.
        limit: Max rows, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call(
        "weekly_group_owner_query",
        {"person": person, "role": role, "limit": bounded},
    )


async def weekly_group_history(
    task: str = "",
    version_no: int = 0,
    by: str = "",
    latest_only: bool = False,
    date_from: str = "",
    date_to: str = "",
    last_days: int = 0,
    limit: int = 200,
) -> str:
    """Query the group board's progress history -- it lives in its own table.

    weekly_progress_history and weekly_progress_range return nothing for group
    tasks: the group board's progress is not in task_progress at all. This is the
    only entry point for it.

    Read total_count / total_tasks for counting questions -- rows may be truncated
    at 200 while the totals stay exact. Relative windows are anchored to the data
    snapshot date on the server, so do not compute dates yourself.

    Args:
        task: Task id or name; empty covers the whole board.
        version_no: Return one specific period (larger is newer, per task).
        by: Empty lists rows; year / month / quarter / task / reporter counts them.
        latest_only: Keep only each task's newest published period.
        date_from: Inclusive start on report time, YYYY-MM-DD.
        date_to: Inclusive end on report time, YYYY-MM-DD.
        last_days: Window of N days ending at the snapshot date; 0 disables it.
        limit: Max rows, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
        version = max(0, int(version_no))
        days = max(0, int(last_days))
    except TypeError, ValueError:
        return _invalid("version_no, last_days and limit must be integers")
    return await _call(
        "weekly_group_history",
        {
            "task": task,
            "version_no": version,
            "by": by,
            "latest_only": bool(latest_only),
            "date_from": date_from,
            "date_to": date_to,
            "last_days": days,
            "limit": bounded,
        },
    )


async def weekly_group_stats(scope: str = "owners", top: int = 8, min_rounds: int = 0) -> str:
    """Aggregate stats over the group board that listing rows cannot answer.

    Use this instead of fetching rows and counting by hand: the counts here are
    exact and cost one call.

    Args:
        scope: owners (multi vs single lead, distinct leads) / separators (how the
            multi-value owner column is delimited -- comma, ideographic comma,
            both, or a single person with no delimiter at all) / owner_widths
            (how many people share one owner cell, widest first) /
            completion_time (ISO vs free text vs blank) / field_lengths
            (target_result char stats) / attachments (per-task counts,
            zero-attachment tasks kept) / history_rounds (published periods
            per task).
        top: Row cap for the listing scopes.
        min_rounds: For history_rounds, also count tasks with at least this many
            periods. Inclusive: "at least 5" means 5 or more.
    """
    try:
        bounded = max(1, min(200, int(top)))
        threshold = max(0, int(min_rounds))
    except TypeError, ValueError:
        return _invalid("top and min_rounds must be integers")
    return await _call(
        "weekly_group_stats",
        {"scope": scope, "top": bounded, "min_rounds": threshold},
    )

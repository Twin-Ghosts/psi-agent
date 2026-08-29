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


async def weekly_progress_history(task: str, published_only: bool = True, limit: int = 200) -> str:
    """Return one task's progress versions, newest first.

    version_no is unique and monotonically increasing, so the first row is the
    current period. Draft rows (is_published = 0) are excluded by default and
    must not be reported as formal progress.

    Args:
        task: Task id or name.
        published_only: True keeps only formally published progress.
        limit: Max versions to return, capped at 200.
    """
    if not task.strip():
        return _invalid("task must not be empty")
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call(
        "weekly_progress_history",
        {"task": task, "published_only": bool(published_only), "limit": bounded},
    )


async def weekly_milestone_query(year: str = "", status: str = "", limit: int = 200) -> str:
    """List milestones, re-checked against the formal-task caliber (R-17).

    Args:
        year: Four-digit year, empty for all years.
        status: 0未完成 / 1已完成, empty for both.
        limit: Max rows to return, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call(
        "weekly_milestone_query",
        {"year": year, "status": status, "limit": bounded},
    )


async def weekly_workflow_query(task: str = "", limit: int = 200) -> str:
    """Trace the approval action LOG (who did what, at which node).

    This is the action history, not the submission forms. For submission status
    or round counts use weekly_submission_query -- the action log cannot be
    aggregated into submission status.

    Approval opinions are permission-gated (R-04/R-14): when the service returns
    "[按权限不展示]", say the field is withheld by permission -- do not guess at
    its contents or retry to get around it.

    Args:
        task: Task id or name; empty returns recent actions across all tasks.
        limit: Max rows to return, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call("weekly_workflow_query", {"task": task, "limit": bounded})


async def weekly_submission_query(
    task: str = "",
    reporter: str = "",
    status: str = "",
    exclude_status: str = "",
    limit: int = 200,
) -> str:
    """Query approval submission forms: round_no, status, reporter, signer.

    Use this for "how many submissions / what status / whose submissions are not
    yet approved". Returns a status_breakdown alongside the rows. The draft
    snapshot (payload) is never returned and must not be reported as formal data.

    Args:
        task: Task id or name; empty covers all tasks.
        reporter: Reporter id or name.
        status: Keep only this status.
        exclude_status: Drop this status, e.g. "approved" for pending ones.
        limit: Max rows to return, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call(
        "weekly_submission_query",
        {
            "task": task,
            "reporter": reporter,
            "status": status,
            "exclude_status": exclude_status,
            "limit": bounded,
        },
    )


async def weekly_owner_roles(person: str) -> str:
    """Count one person's formal tasks split by role.

    Returns as_owner / as_project_owner / as_lead_owner / any_role counts only.
    For the actual task LIST use weekly_task_query with the owner filter --
    this tool answers "how many", not "which ones".

    Args:
        person: User id or name.
    """
    if not person.strip():
        return _invalid("person must not be empty")
    return await _call("weekly_owner_roles", {"person": person})


async def weekly_field_completeness(field: str = "") -> str:
    """Count how many formal tasks have a given field filled in (R-07 / R-19).

    Use this for "how many tasks have an overall goal / a named owner" instead of
    listing every task and counting by hand. Call with an empty field to see which
    columns are supported. Empty strings count as missing, not filled.

    Args:
        field: Column to measure; empty lists the supported columns.
    """
    return await _call("weekly_field_completeness", {"field": field})


async def weekly_progress_coverage() -> str:
    """Summarise progress history depth: row count, tasks covered, date span, max version.

    Use this for "how far back does the history go" or "how many progress records
    are there in total" -- one call instead of walking every task.
    """
    return await _call("weekly_progress_coverage", {})


async def weekly_task_ranking(metric: str = "attachments", top: int = 5) -> str:
    """Rank formal tasks by child-record count: which task has the most X.

    Args:
        metric: attachments / progress / milestones / submissions.
        top: How many rows to return, 1..50.
    """
    try:
        bounded = max(1, min(50, int(top)))
    except TypeError, ValueError:
        return _invalid("top must be an integer")
    return await _call("weekly_task_ranking", {"metric": metric, "top": bounded})


async def weekly_attachment_query(task: str = "", limit: int = 200) -> str:
    """List attachments. storage_path is never returned and must not be requested.

    Args:
        task: Task id or name; empty lists across all formal tasks.
        limit: Max rows to return, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call("weekly_attachment_query", {"task": task, "limit": bounded})


async def weekly_health() -> str:
    """Check that the weekly取数 service is reachable and report table row counts.

    Use this when a query fails and you need to tell a connection problem from an
    empty result. Never edit .env or ask the user for a token.
    """
    return await _call("weekly_health", {})

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


async def weekly_progress_history(task: str, published_only: bool = True, limit: int = 30) -> str:
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


async def weekly_milestone_query(year: str = "", status: str = "", limit: int = 50) -> str:
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


async def weekly_workflow_query(task: str = "", limit: int = 50) -> str:
    """Trace approval submissions and action history for a task.

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


async def weekly_attachment_query(task: str = "", limit: int = 50) -> str:
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

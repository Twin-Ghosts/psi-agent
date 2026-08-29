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


async def weekly_aggregate(group_by: str, board: str = "", metric: str = "count") -> str:
    """Aggregate formal tasks by one dimension.

    Empty groups are preserved (the service puts the caliber on the JOIN's ON
    clause, per R-02/R-08), so a zero row means genuinely zero tasks -- do not
    treat a missing group as zero without checking here first.

    Args:
        group_by: One of board / category / status / project_group / owner.
        board: Optional board code or name to scope the aggregation.
        metric: Only "count" is supported.
    """
    if not group_by.strip():
        return _invalid("group_by must not be empty")
    return await _call("weekly_aggregate", {"group_by": group_by, "board": board, "metric": metric})


async def weekly_freshness() -> str:
    """Report each board's latest progress time and formal task count.

    Call this before answering any relative-time question ("this week", "recently").
    Anchor to these snapshot times, never to the machine wall clock.
    """
    return await _call("weekly_freshness", {})


async def weekly_import_audit(limit: int = 30) -> str:
    """Reconcile Excel import batches against distinct snapshot dates (R-09/R-10).

    Compare batch_count with distinct_dates and distinct_import_times to tell a
    single import from repeated ones.

    Args:
        limit: Max batch rows to return, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(limit)))
    except TypeError, ValueError:
        return _invalid("limit must be an integer")
    return await _call("weekly_import_audit", {"limit": bounded})

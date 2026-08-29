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


async def weekly_person_stats(scope: str = "workload", role: str = "lead_owner", top: int = 200) -> str:
    """Aggregate formal tasks by person: workload, cross-group spread, id formats.

    weekly_owner_roles answers "how many does THIS person have"; this answers the
    population-level questions. Every count is computed server-side -- counting
    people by reading rows back is the biggest source of wrong answers here.

    Args:
        scope: workload (tasks per person, ties ordered by name) / workload_summary
            (global average, distinct head-count, max and min) / single_task (people
            holding exactly one) / cross_group (people spanning 2+ 专项组, with the
            group list) / dual_role (people who are both 牵头人 and 项目负责人) /
            id_format (owner_user_id written as 纯数字工号 / u 前缀 / NDG 域账号) /
            id_variants (same name carrying more than one id; 0 rows means none
            exist) / id_longest (one row per DISTINCT identifier ordered by
            character length, with task_count beside it; the question asks which
            identifier is longest, so an id held by 3 tasks still counts once,
            and equal lengths are genuine ties to be stated together) /
            reporters (progress rounds filed per person) / reporter_count (distinct
            filers) / reviewers (progress rows reviewed per person) / self_review
            (rows where filer and reviewer are the same id).
        role: Person column to group by: lead_owner or project_owner.
        top: Row cap, capped at 200.
    """
    try:
        bounded = max(1, min(200, int(top)))
    except TypeError, ValueError:
        return _invalid("top must be an integer")
    return await _call(
        "weekly_person_stats",
        {"scope": scope, "role": role, "top": bounded},
    )

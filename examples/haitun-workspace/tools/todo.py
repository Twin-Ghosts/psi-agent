"""Manage the current session task list (plan-then-execute for multi-step work)."""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _todo_store as _store


async def todo(
    todos: list[dict[str, str]] | None = None,
    merge: bool = False,
    workspace: str = "",
) -> str:
    """Manage your task list for the current session.

    Use for complex work with **3+ steps** or **multiple sub-tasks**. The agent
    decides when to create or update the list — users do not need to say
    "break this down". Pair with ``skills/task-planning/SKILL.md``.

    **Read:** call with empty ``todos`` (default).

    **Write:** pass ``todos`` as an array, e.g.
    ``[{"id":"1","content":"…","status":"in_progress"}]``.

    - ``merge=false`` (default): replace the entire list with a fresh plan.
    - ``merge=true``: update existing items by ``id``, append new ones.

    Each item: ``{id: string, content: string, status}`` where ``status`` is
    ``pending`` | ``in_progress`` | ``completed`` | ``cancelled``.

    List order is priority. Only **one** item should be ``in_progress`` at a
    time. Mark items ``completed`` as soon as they finish. If a step fails,
    mark it ``cancelled`` and add a revised item via ``merge=true``.

    Always returns the full current list and summary counts.

    Args:
        todos: Array of task items, or omit to read the current list.
        merge: When true, merge by id; when false, replace the whole list.
        workspace: Workspace root. Empty = current workspace.

    Returns:
        JSON with ok, session_id, todos[], summary{{total, pending, …}}, …
    """
    if todos is None:
        result = await _store.read_todos(workspace_raw=workspace)
    else:
        parsed: Any = todos
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError as exc:
                result = {"ok": False, "message": f"todos must be a JSON array: {exc}"}
                return json.dumps(result, ensure_ascii=False)
        if not isinstance(parsed, list):
            result = {"ok": False, "message": "todos must be a JSON array"}
        else:
            result = await _store.write_todos(todos=parsed, merge=merge, workspace_raw=workspace)
    return json.dumps(result, ensure_ascii=False)

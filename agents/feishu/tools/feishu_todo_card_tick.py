"""Handle one tick on a TODO-list card: mark the linked Feishu task complete.

Dispatched by the card's ``action_handlers`` map, one row at a time. The row's visual
state (``● ~~已完成~~``) is already applied by the Channel before this runs, so this tool
only has to move the *authoritative* state — the Feishu task — and report what happened.
"""

from __future__ import annotations

import json
import time
from typing import Any

import _feishu_api_impl as _api


def _parse_action(card_action_json: str) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(card_action_json, str) or not card_action_json.strip():
        return None, "[Error] card_action_json is required (pass the <feishu_card_action> payload)"
    try:
        payload = json.loads(card_action_json)
    except ValueError as exc:
        return None, f"[Error] card_action_json is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "[Error] card_action_json must be a JSON object"
    return payload, ""


def _action_value(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    value = action.get("value") if isinstance(action, dict) else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def feishu_todo_card_tick(card_action_json: str = "", user_key: str = "") -> str:
    """Mark the Feishu task behind one ticked TODO row complete.

    The handler for ``feishu_todo_card_send`` rows. Session injects the
    ``<feishu_card_action>`` payload as ``card_action_json``; the clicked row's
    ``task_guid`` and title come from its ``value``.

    Completion is written as ``task.completed_at`` = now in **milliseconds** with
    ``update_fields: ["completed_at"]`` — without ``update_fields`` Feishu returns success
    and changes nothing. A row carrying no ``task_guid`` is reported as ticked-only, since
    there is no task to move; that is not an error.

    The card itself is already updated by the time this runs. Do not re-send or edit it,
    and do not announce the click — reply only if the task update failed.

    Fast clicking is coalesced by the Channel: if the payload arrives wrapped in
    ``<feishu_card_action_batch>``, call this tool once per ``<feishu_card_action>`` inside
    it (skipping one silently loses that task's completion), then send at most one summary
    reply for the whole batch.

    Args:
        card_action_json: The ``<feishu_card_action>`` JSON (injected by Session).
        user_key: The clicker's open_id. Pass it so the task is completed as that person
            when the bot's own token is not a task member.
    """
    payload, error = _parse_action(card_action_json)
    if payload is None:
        return error
    value = _action_value(payload)
    task_guid = str(value.get("task_guid") or "").strip()
    title = str(value.get("todo_title") or "").strip() or "该待办"
    if not task_guid:
        return json.dumps(
            {"ok": True, "ticked": True, "task_updated": False, "reason": "row has no task_guid", "title": title},
            ensure_ascii=False,
        )

    result = await _api.call_api_impl(
        "PATCH",
        "/open-apis/task/v2/tasks/:task_guid",
        body_json=json.dumps(
            {
                "task": {"completed_at": str(int(time.time() * 1000))},
                "update_fields": ["completed_at"],
            },
            ensure_ascii=False,
        ),
        paths_json=json.dumps({"task_guid": task_guid}, ensure_ascii=False),
        prefer="tenant",
        user_key=user_key,
    )
    return json.dumps(result, ensure_ascii=False, default=str)

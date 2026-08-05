"""勾选一条 SOP 项: 写明细完成状态, 再从明细整体重算该人的总览行。

卡片的原地重绘由框架完成, 本工具不发卡、不改卡。连点会被合并成
<feishu_card_action_batch>, 里面每条都要各调一次本工具(漏掉就丢一项完成)。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_runtime as _rt
import _rookie_sop_store as _store

_HANDLER = "rookie_sop_tick"


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _resolve_context(payload: dict[str, Any]) -> dict[str, Any]:
    dispatch = _as_dict(payload.get("dispatch"))
    handler = str(dispatch.get("handler") or "").strip()
    if handler and handler != _HANDLER:
        return {"error": f"unexpected handler {handler!r}; expected {_HANDLER!r}"}
    if handler == _HANDLER and dispatch.get("matched") is False:
        return {"error": "dispatch.matched is false; do not invent a handler"}

    action = _as_dict(payload.get("action"))
    value = action.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    value = _as_dict(value)

    business = _as_dict(payload.get("business_context"))
    source = _as_dict(payload.get("source"))
    operator = str(source.get("operator_open_id") or source.get("open_id") or "").strip()

    item_id = str(value.get("item_id") or "").strip()
    if not item_id:
        action_name = str(value.get("action") or action.get("action_id") or "")
        if action_name.startswith("rookie_tick_"):
            item_id = action_name[len("rookie_tick_") :]

    return {
        "error": "",
        "open_id": str(business.get("open_id") or "").strip() or operator,
        "name": str(business.get("name") or "").strip(),
        "item_id": item_id,
        "app_token": str(business.get("app_token") or "").strip(),
        "detail_table_id": str(business.get("detail_table_id") or "").strip(),
        "overview_table_id": str(business.get("overview_table_id") or "").strip(),
    }


async def rookie_sop_tick(card_action_json: str = "") -> str:
    """Record one ticked onboarding SOP item, then recompute that person's overview row.

    Call this for a ``<feishu_card_action>`` whose ``dispatch.handler`` is
    ``rookie_sop_tick``. Pass the **entire** JSON object inside the tag. The card has
    already been redrawn by the framework — do not re-send it, do not narrate the click.
    Finish with zero assistant content unless this tool reports an error.

    If the payload arrived wrapped in ``<feishu_card_action_batch>``, call this once per
    ``<feishu_card_action>`` inside it (skipping one silently loses that item), then send
    at most one short summary for the whole batch.

    Args:
        card_action_json: Full ``<feishu_card_action>`` payload JSON string.
    """
    payload = _store._parse_result(card_action_json)
    if not payload:
        return json.dumps({"ok": False, "error": "card_action_json must be a JSON object"}, ensure_ascii=False)

    ctx = _resolve_context(payload)
    if ctx.get("error"):
        return json.dumps({"ok": False, "error": ctx["error"]}, ensure_ascii=False)
    if not ctx["open_id"] or not ctx["item_id"]:
        return json.dumps({"ok": False, "error": "cannot resolve open_id / item_id"}, ensure_ascii=False)

    state = await _rt.load_state()
    app_token = ctx["app_token"] or str(state.get("app_token") or "")
    detail_table = ctx["detail_table_id"] or str(state.get("detail_table_id") or "")
    overview_table = ctx["overview_table_id"] or str(state.get("overview_table_id") or "")
    if not app_token or not detail_table:
        return json.dumps({"ok": False, "error": "rookie SOP base is not initialised"}, ensure_ascii=False)

    bitable = _rt.bitable_adapter()
    today = date.today()
    marked = await _store.mark_done(
        bitable, app_token, detail_table, open_id=ctx["open_id"], item_id=ctx["item_id"], today=today
    )
    if marked.get("ok") is not True:
        return json.dumps({"ok": False, "error": marked.get("error") or "mark_done failed"}, ensure_ascii=False)

    rows = await _store.fetch_detail(bitable, app_token, detail_table, ctx["open_id"])
    role = ""
    for row in rows:
        label = str(row.get("适用角色") or "")
        if label in {"研发", "非研发"}:
            role = "dev" if label == "研发" else "nondev"
            break

    overview_updated = False
    overview_skipped_reason = ""
    if overview_table:
        overview = await _store.recompute_overview(
            bitable,
            app_token,
            overview_table,
            open_id=ctx["open_id"],
            name=ctx["name"] or ctx["open_id"],
            role=role,
            rows=rows,
            today=today,
        )
        overview_updated = bool(overview.get("ok"))
    else:
        overview_skipped_reason = "no overview_table_id available"

    result: dict[str, Any] = {
        "ok": True,
        "item_id": ctx["item_id"],
        "already_done": bool(marked.get("already_done")),
        "overview_updated": overview_updated,
    }
    duplicates = marked.get("duplicates")
    if isinstance(duplicates, int) and duplicates > 0:
        result["duplicates"] = duplicates
    if overview_skipped_reason:
        result["overview_skipped_reason"] = overview_skipped_reason
    return json.dumps(result, ensure_ascii=False)

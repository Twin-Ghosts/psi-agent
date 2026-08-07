"""把新人在详情页文档里勾的项同步回明细表 —— 由文档变更事件驱动, 不轮询。

刻意为之: 事件只说「这个文档被编辑了」, 不说哪一项被勾, 所以每次都整份读回来
对比。代价是一次读取, 换来的是不必轮询 —— 10 个新人若每 5 分钟轮询一次是每天
2880 次调用, 事件驱动只有实际勾选那几十次。

只认「未完成 → 勾上」一个方向(见 _rookie_sop_doc.diff_state 的说明): 允许取消
勾选就撤销完成记录, 等于让新人一取消就能抹掉数据, HR 日报会变得不可信。
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

import _rookie_sop_doc as _doc
import _rookie_sop_docapi as _docapi
import _rookie_sop_progress as _p
import _rookie_sop_runtime as _rt
import _rookie_sop_store as _store
from feishu_api import feishu_api


def _doc_id_of(payload: dict[str, Any]) -> str:
    """从文档变更事件里取 document_id。

    飞书这类事件把 token 放在 file_token, 也可能直接给 document_id ——
    两种都认, 认不出就返回空串让调用方报错而不是猜。
    """
    for key in ("document_id", "file_token", "token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def rookie_sop_sync_doc(document_id: str = "", event_payload_json: str = "", open_id: str = "") -> str:
    """Sync a new hire's ticked items from their onboarding checklist doc into the detail table.

    Fired by the ``haitun.rookie.doc_edited`` trigger with ``fire=tool`` when the doc
    changes, so no LLM is involved and no polling is needed. The event only says *the doc
    changed*, never which row, so the whole doc is read back and compared.

    Only ``未完成 → ticked`` is applied. Un-ticking never revokes a completion: letting a
    new hire erase recorded progress by un-ticking would make the HR digest untrustworthy.

    Args:
        document_id: The checklist doc. Empty → read from ``event_payload_json``.
        event_payload_json: Event envelope payload (injected by Session).
        open_id: Whose checklist this is. Empty → resolved from the state file's doc index.
    """
    payload = _store._parse_result(event_payload_json) if event_payload_json else {}
    doc_id = (document_id or "").strip() or _doc_id_of(payload)
    if not doc_id:
        return json.dumps({"ok": False, "error": "no document_id in args or event payload"}, ensure_ascii=False)

    state = await _rt.load_state()
    app_token = str(state.get("app_token") or "")
    detail_table = str(state.get("detail_table_id") or "")
    overview_table = str(state.get("overview_table_id") or "")
    if not app_token or not detail_table:
        return json.dumps({"ok": False, "error": "rookie SOP base is not initialised"}, ensure_ascii=False)

    # 文档索引: {document_id: open_id} —— 事件只带文档 token, 得反查是谁的清单
    docs = state.get("docs")
    docs = docs if isinstance(docs, dict) else {}
    target = (open_id or "").strip() or str(docs.get(doc_id) or "").strip()
    if not target:
        return json.dumps(
            {"ok": False, "error": f"cannot map document {doc_id} to an open_id; state has {len(docs)} doc(s)"},
            ensure_ascii=False,
        )

    read = await _docapi.read_blocks(feishu_api, doc_id)
    if read.get("ok") is not True:
        return json.dumps({"ok": False, "error": f"read doc failed: {read.get('error')}"}, ensure_ascii=False)

    doc_state = _doc.read_doc_state(read.get("blocks") or [])
    bitable = _rt.bitable_adapter()
    rows, truncated = await _store.fetch_detail(bitable, app_token, detail_table, target)
    newly_ticked = _doc.diff_state(doc_state, rows)

    today = date.today()
    marked: list[str] = []
    failures: list[dict[str, str]] = []
    for item_id in newly_ticked:
        done = await _store.mark_done(
            bitable, app_token, detail_table, open_id=target, item_id=item_id, today=today
        )
        if done.get("ok") is not True:
            failures.append({"item_id": item_id, "error": str(done.get("error") or "mark_done failed")})
            continue
        marked.append(item_id)

    overview_updated = False
    if marked and overview_table:
        rows, _ = await _store.fetch_detail(bitable, app_token, detail_table, target)
        role_label = next(
            (str(r.get("适用角色") or "") for r in rows if str(r.get("适用角色") or "") in {"研发", "非研发"}), ""
        )
        role = "dev" if role_label == "研发" else "nondev" if role_label == "非研发" else ""
        name = next((str(r.get("姓名") or "") for r in rows if r.get("姓名")), target)
        recomputed = await _store.recompute_overview(
            bitable,
            app_token,
            overview_table,
            open_id=target,
            name=name,
            role=role,
            rows=rows,
            today=today,
        )
        overview_updated = recomputed.get("ok") is True

    progress = _p.summarize(rows, today)
    result: dict[str, Any] = {
        "ok": not failures,
        "document_id": doc_id,
        "open_id": target,
        "ticked_in_doc": len(doc_state),
        "newly_synced": marked,
        "progress": f"{progress.done}/{progress.total}",
        "overview_updated": overview_updated,
    }
    if failures:
        result["failures"] = failures
    if truncated or read.get("truncated"):
        result["truncated"] = True
    return json.dumps(result, ensure_ascii=False)

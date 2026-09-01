"""Prepare a writer-confirmed positive-negative-list case from a private chat."""

# ruff: noqa: E402, RUF001

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f
from _assignment_display import resolve_feishu_display_names
from _positive_negative_list.dedupe import (
    build_cross_source_fingerprint,
    make_source_key,
    release_source_key,
    reserve_source_key,
)
from _positive_negative_list.drafts import delete_draft_body, save_draft
from _positive_negative_list.models import CaseDraft
from _positive_negative_list.validation import validate_case

from psi_agent._appdata import resolve_appdata_root as _resolve_appdata_root
from psi_agent.session.runtime_context import get_session_id as _get_session_id


def _preview_digest(case: CaseDraft) -> str:
    # ``workflow`` changes while a card is being written and is not part of
    # the user-visible confirmation snapshot.
    snapshot = case.to_mapping()
    snapshot.pop("workflow", None)
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


async def _confirmation_card(case: CaseDraft, digest: str) -> dict[str, Any]:
    nature = {"positive": "正面行为", "negative": "负面行为"}.get(case.nature, case.nature)
    subject_ids = {
        identity.strip() for identity in case.subject_user_key.replace("，", ",").split(",") if identity.strip()
    }
    names = await resolve_feishu_display_names(subject_ids, _f.get_users_batch_impl)
    subject_display = "、".join(names.get(identity, "姓名未解析") for identity in sorted(subject_ids))
    if not subject_display:
        subject_display = "姓名未提供"
    teaching = ""
    if case.nature == "negative":
        teaching = (
            f"\n**正确做法**：{case.correct_behavior}"
            f"\n**立即补救**：{case.immediate_remedy}"
            f"\n**预防措施**：{case.prevention}"
        )
    action_value = {"action": "positive_negative_case_confirm", "case_id": case.case_id, "preview_digest": digest}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "正负面清单记录确认"}},
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**涉事人**：{subject_display}\n"
                    f"**发生时间**：{case.occurred_at}\n"
                    f"**行为性质**：{nature}\n"
                    f"**分类**：{case.category}\n"
                    f"**行为事实**：{case.fact_summary}\n"
                    f"**证据状态**：{', '.join(case.evidence_sources)}"
                    f"{teaching}\n\n确认后将写入 HaiTun 机器人独立测试表，不会修改正式总表。当前不计分、不进入绩效。"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认写入"},
                        "type": "primary",
                        "value": action_value,
                    }
                ],
            },
        ],
    }


async def positive_negative_case_prepare(
    case_json: str,
    source_type: str = "feishu_private_chat",
    source_event_id: str = "",
    source_message_id: str = "",
    user_key: str = "",
) -> str:
    if source_type != "feishu_private_chat" or not user_key.strip():
        return _f.dumps_result({"ok": False, "error": "一期只接受可信的人工飞书私聊身份"})
    try:
        raw: dict[str, Any] = json.loads(case_json)
        if not isinstance(raw, dict):
            raise ValueError("case_json must be an object")
        supplied_writer = raw.get("writer_user_key")
        if supplied_writer not in (None, "", user_key):
            raise ValueError("writer identity does not match trusted sender")
        raw["writer_user_key"] = user_key
        raw["workflow"] = "ready_for_confirmation"
        raw["source_type"] = source_type
        raw["source_event_id"] = source_event_id
        raw["source_message_id"] = source_message_id
        raw["source_session_id"] = _get_session_id()
        case = CaseDraft.from_mapping(raw)
        errors = validate_case(case)
        if errors:
            return _f.dumps_result({"ok": False, "errors": list(errors)})
        source_key = make_source_key(source_type, source_event_id or None, source_message_id or None)
        # This fingerprint is private runtime state used only to surface a
        # possible duplicate before writing the isolated test table.  Derive a
        # stable secret from the already configured Feishu application and the
        # AppData root, so deploying this skill does not introduce another
        # configuration field.
        root = await _resolve_appdata_root()
        app_identity = str(_f._config() or "")
        dedupe_secret = hashlib.sha256(f"{root}\0{app_identity}\0positive-negative-list".encode()).digest()
        fingerprint = build_cross_source_fingerprint(case, dedupe_secret)
        case_id = f"case_{secrets.token_urlsafe(12)}"
        canonical_id = f"incident_{secrets.token_urlsafe(12)}"
        case = CaseDraft.from_mapping(
            case.to_mapping()
            | {
                "case_id": case_id,
                "source_key": source_key,
                "cross_source_fingerprint": fingerprint,
                "canonical_incident_id": canonical_id,
            }
        )
        reservation = reserve_source_key(root, source_key, case_id)
        if reservation.status not in {"reserved", "idempotent"}:
            return _f.dumps_result({"ok": False, "status": reservation.status, "case_id": reservation.case_id})
        session_id = _get_session_id()
        save_draft(root, user_key, session_id, case_id, case)
        digest = _preview_digest(case)
        card = await _confirmation_card(case, digest)
        try:
            sent = await _f.send_card_impl(
                user_key,
                json.dumps(card, ensure_ascii=False),
                "open_id",
                user_key,
                json.dumps(
                    {"case_id": case_id, "preview_digest": digest, "writer_open_id": user_key}, ensure_ascii=False
                ),
                json.dumps({"positive_negative_case_confirm": "positive_negative_case_confirm"}, ensure_ascii=False),
            )
        except Exception as exc:
            delete_draft_body(root, user_key, case_id)
            release_source_key(root, source_key, case_id)
            return _f.dumps_result({"ok": False, "status": "confirmation_card_failed", "error": str(exc)})
        if not isinstance(sent, dict) or not sent.get("ok"):
            delete_draft_body(root, user_key, case_id)
            release_source_key(root, source_key, case_id)
            message = sent.get("message") if isinstance(sent, dict) else "确认卡发送失败"
            return _f.dumps_result({"ok": False, "status": "confirmation_card_failed", "error": message})
        return _f.dumps_result(
            {
                "ok": True,
                "status": "待写入者确认",
                "case_id": case_id,
                "rule_version": case.rule_version,
                "message_id": sent.get("message_id", ""),
                "confirmation_scope": "写入 HaiTun 机器人独立测试表",
                "preview_digest": digest,
                "preview": case.to_mapping(),
            }
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        allowed_case_fields = [field.name for field in fields(CaseDraft)]
        return _f.dumps_result(
            {
                "ok": False,
                "error": str(exc),
                "allowed_case_fields": allowed_case_fields,
                "hint": "case_json 只能使用上述候选记录字段；展示姓名与飞书 open_id 不是同一字段。",
            }
        )

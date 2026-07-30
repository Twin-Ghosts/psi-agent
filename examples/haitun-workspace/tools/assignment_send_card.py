from __future__ import annotations

import json
from typing import Any

from _assignment_tool_common import CLIENT, dumps_result, invalid_argument
from feishu_message import feishu_message_send_card as _feishu_message_send_card


async def assignment_send_card(
    receive_id: str,
    assignment_id: str,
    receive_id_type: str = "open_id",
    user_key: str = "",
) -> str:
    """Fetch a work assignment and send its authoritative Feishu acceptance card."""
    normalized_receive_id = _required_text(receive_id)
    normalized_assignment_id = _required_text(assignment_id)
    if normalized_receive_id is None:
        return invalid_argument("receive_id must be a non-empty string")
    if normalized_assignment_id is None:
        return invalid_argument("assignment_id must be a non-empty string")

    fetched = await CLIENT.call_tool(
        "assignment_get",
        {"assignment_id": normalized_assignment_id},
        retryable=True,
    )
    if not fetched.get("ok"):
        return dumps_result(fetched)
    assignment = fetched.get("result")
    if not isinstance(assignment, dict):
        return invalid_argument("Fusion Memory returned an invalid assignment")
    if assignment.get("state") != "assigned":
        return dumps_result(
            {
                "ok": False,
                "error": {
                    "code": "assignment_state_invalid",
                    "message": "Only an assigned work arrangement can be delivered",
                    "retryable": False,
                },
            }
        )
    recipients = assignment.get("recipients")
    if not isinstance(recipients, list):
        return invalid_argument("assignment recipients must be an array")
    recipient_open_ids: set[str] = set()
    for participant in recipients:
        if not isinstance(participant, dict):
            continue
        raw_open_id = participant.get("feishu_open_id")
        if raw_open_id is not None and not isinstance(raw_open_id, str):
            return invalid_argument("assignment recipient feishu_open_id must be a string")
        open_id = _required_text(raw_open_id)
        if open_id is not None:
            recipient_open_ids.add(open_id)
    if normalized_receive_id not in recipient_open_ids:
        return invalid_argument("receive_id must identify an assignment recipient")

    title = _required_text(assignment.get("title"))
    assigner_name = _participant_name(assignment.get("assigner"))
    if title is None or assigner_name is None:
        return invalid_argument("assignment title and assigner are required")

    card = _build_assignment_card(
        assignment=assignment,
        assignment_id=normalized_assignment_id,
        title=title,
        assigner_name=assigner_name,
    )
    business_context = {
        "type": "work_assignment",
        "assignment_id": normalized_assignment_id,
        "title": title,
        "assigner_name": assigner_name,
        "publish_target": "feishu_task",
    }
    action_handlers = {
        "confirm_assignment_receipt": "assignment_accept",
    }
    return await _feishu_message_send_card(
        normalized_receive_id,
        json.dumps(card, ensure_ascii=False),
        receive_id_type,
        user_key,
        json.dumps(business_context, ensure_ascii=False),
        json.dumps(action_handlers, ensure_ascii=False),
    )


def _build_assignment_card(
    *,
    assignment: dict[str, Any],
    assignment_id: str,
    title: str,
    assigner_name: str,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        _plain_text_element(f"任务: {title}\n安排者: {assigner_name}"),
    ]
    original_request = assignment.get("original_request")
    if isinstance(original_request, str) and original_request.strip():
        elements.extend(
            [
                _heading_element("安排者原始内容 (原文或语音转写, 未改写)"),
                _plain_text_element(original_request),
            ]
        )
    analysis: list[str] = []
    _append_labeled_text(analysis, "背景", assignment.get("context"))
    _append_labeled_text(analysis, "期望结果", assignment.get("expected_outcome"))
    _append_items(analysis, "待确认缺口", assignment.get("gaps"))
    _append_items(analysis, "已识别风险", assignment.get("risks"))
    _append_items(analysis, "行动项", assignment.get("action_items"))
    if analysis:
        elements.extend(
            [
                _heading_element("Agent 分析整理 (非安排者原话)"),
                _plain_text_element("\n".join(analysis)),
            ]
        )
    sources = _source_texts(assignment.get("evidence_refs"))
    if sources:
        elements.extend(
            [
                _heading_element("参考资料"),
                _plain_text_element("\n".join(sources)),
            ]
        )
    elements.append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "确认接收并创建飞书任务"},
                    "type": "primary",
                    "value": {"action": "confirm_assignment_receipt", "assignment_id": assignment_id},
                }
            ],
        }
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "新的工作安排"},
            "template": "blue",
        },
        "elements": elements,
    }


def _participant_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return _optional_text(value.get("display_name")) or _optional_text(value.get("user_id"))


def _plain_text_element(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _heading_element(label: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": f"**{label}**"}


def _append_labeled_text(lines: list[str], label: str, value: Any) -> None:
    text = _optional_text(value)
    if text is not None:
        lines.append(f"{label}: {text}")


def _append_items(lines: list[str], label: str, value: Any) -> None:
    if not isinstance(value, list):
        return
    normalized = [text for item in value if (text := _item_text(item))]
    lines.extend(f"{label}: {item}" for item in normalized)


def _source_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    sources: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        uri = _optional_text(item.get("uri")) or _optional_text(item.get("url"))
        if uri:
            sources.append(uri)
    return sources


def _item_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    content = None
    for field in ("description", "action", "title", "name"):
        text = _optional_text(value.get(field))
        if text:
            content = text
            break
    if content is None:
        return None
    details: list[str] = []
    owner = _first_participant_name(value, ("owner", "responsible", "assignee"))
    if owner is not None:
        details.append(f"负责人: {owner}")
    deadline = _first_text(value, ("deadline", "due", "due_at"))
    if deadline is not None:
        details.append(f"截止时间: {deadline}")
    status = _optional_text(value.get("status"))
    if status is not None:
        details.append(f"状态: {status}")
    return " | ".join([content, *details])


def _first_participant_name(value: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        participant = value.get(field)
        text = _optional_text(participant) if isinstance(participant, str) else _participant_name(participant)
        if text is not None:
            return text
    return None


def _first_text(value: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        text = _optional_text(value.get(field))
        if text is not None:
            return text
    return None


def _required_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_text(value: Any) -> str | None:
    return _required_text(value)

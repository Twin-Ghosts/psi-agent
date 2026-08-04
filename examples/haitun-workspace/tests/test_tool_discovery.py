"""Tests for the Haitun workspace tool-discovery meta-tools.

Covers ``_tool_index`` (static AST scan) and the ``tool_search`` /
``tool_search_code`` / ``tool_describe`` tools built on top of it.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

import anyio
import pytest

from psi_agent.session.tool_registry import ToolFunction

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_idx: Any = importlib.import_module("_tool_index")
tool_search: Any = importlib.import_module("tool_search").tool_search
tool_search_code: Any = importlib.import_module("tool_search_code").tool_search_code
tool_describe: Any = importlib.import_module("tool_describe").tool_describe


# ── _tool_index against the real tools/ dir ──────────────────────────────────


async def test_index_finds_known_tools_and_skips_private_files():
    metas = await _idx.index_tools()
    names = {m.name for m in metas}
    # Known public tools are indexed.
    assert "find_files" in names
    assert "fetch" in names
    # The three discovery tools index themselves.
    assert {"tool_search", "tool_search_code", "tool_describe"} <= names
    assert {
        "assignment_upsert",
        "assignment_get",
        "assignment_list",
        "assignment_transition",
        "assignment_feedback",
        "assignment_send_card",
        "assignment_accept",
        "assignment_delivery_refresh",
    } <= names
    # Private helper files (``_fetch_impl.py``) never expose a tool.
    assert "fetch_impl" not in names
    assert all(not n.startswith("_") for n in names)


async def test_assignment_read_tools_are_replayable():
    source = await (anyio.Path(str(TOOLS_DIR)) / "_fusion_memory_mcp.py").read_text(encoding="utf-8")
    assert '"assignment_get"' in source
    assert '"assignment_list"' in source
    assert '"assignment_upsert"' not in source.split("READ_TOOLS", 1)[1].split("}", 1)[0]


async def test_assignment_upsert_forwards_assignment_object(monkeypatch):
    fake_client = _FakeMemoryClient()
    module = _import_assignment_tool_with_fake_client("assignment_upsert", fake_client, monkeypatch)

    out = await module.assignment_upsert(
        json.dumps(
            {
                "title": "同步客户会议后续",
                "assigner": {"user_id": "user-a"},
                "recipients": [{"user_id": "user-b"}],
                "idempotency_key": "feishu-message-1",
            },
            ensure_ascii=False,
        )
    )

    assert json.loads(out)["ok"] is True
    assert fake_client.calls == [
        (
            "assignment_upsert",
            {
                "assignment": {
                    "title": "同步客户会议后续",
                    "assigner": {"user_id": "user-a"},
                    "recipients": [{"user_id": "user-b"}],
                    "idempotency_key": "feishu-message-1",
                }
            },
            False,
        )
    ]


async def test_assignment_upsert_binds_assigner_to_current_feishu_session(monkeypatch):
    fake_client = _FakeMemoryClient()
    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: "feishu-ou_assigner")
    module = _import_assignment_tool_with_fake_client("assignment_upsert", fake_client, monkeypatch)

    out = await module.assignment_upsert(
        json.dumps(
            {
                "title": "处理权限限制",
                "assigner": {
                    "user_id": "ou_wrong",
                    "display_name": "高博",
                    "feishu_open_id": "ou_wrong",
                },
                "recipients": [{"user_id": "user-b"}],
                "gaps": ["截止时间未明确"],
                "risks": ["不能破坏现有飞书任务发布"],
                "action_items": ["提交可评审方案"],
                "evidence_refs": ["https://example.com/source"],
            },
            ensure_ascii=False,
        )
    )

    assert json.loads(out)["ok"] is True
    forwarded = fake_client.calls[0][1]["assignment"]
    assert forwarded["assigner"] == {
        "user_id": "ou_assigner",
        "display_name": "ou_assigner",
        "feishu_open_id": "ou_assigner",
    }
    assert forwarded["gaps"] == [{"description": "截止时间未明确"}]
    assert forwarded["risks"] == [{"description": "不能破坏现有飞书任务发布"}]
    assert forwarded["action_items"] == [{"description": "提交可评审方案"}]
    assert forwarded["evidence_refs"] == [{"uri": "https://example.com/source"}]


async def test_assignment_upsert_documents_required_assignment_shape(monkeypatch):
    module = _import_assignment_tool_with_fake_client("assignment_upsert", _FakeMemoryClient(), monkeypatch)
    docstring = inspect.getdoc(module.assignment_upsert) or ""

    for field in (
        "title",
        "state",
        "assigner",
        "recipients",
        "original_request",
        "context",
        "expected_outcome",
        "evidence_refs",
        "gaps",
        "risks",
        "action_items",
        "idempotency_key",
    ):
        assert field in docstring
    assert "user_id" in docstring
    assert "feishu_open_id" in docstring


async def test_assignment_list_forwards_read_filter(monkeypatch):
    fake_client = _FakeMemoryClient()
    module = _import_assignment_tool_with_fake_client("assignment_list", fake_client, monkeypatch)

    out = await module.assignment_list(participant_user_id="user-b", state="assigned", limit=200)

    assert json.loads(out)["ok"] is True
    assert fake_client.calls == [
        (
            "assignment_list",
            {"participant_user_id": "user-b", "state": "assigned", "limit": 50},
            True,
        )
    ]


async def test_assignment_transition_rejects_invalid_json(monkeypatch):
    fake_client = _FakeMemoryClient()
    module = _import_assignment_tool_with_fake_client("assignment_transition", fake_client, monkeypatch)

    out = await module.assignment_transition("wa-1", "not-json")

    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_argument"
    assert fake_client.calls == []


async def test_assignment_feedback_rejects_invalid_payload_json(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_feedback("ou_assigner", "wa-1", "create", "not-json"))

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert fake_client.calls == []
    assert fake_feishu.card_impl_calls == []


async def test_assignment_feedback_rejects_unknown_action_before_memory_call(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "blocking",
            json.dumps({"raw_content": "请确认截止时间"}, ensure_ascii=False),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert "create, append, assigner_reply, or recipient_confirm" in out["error"]["message"]
    assert fake_client.calls == []
    assert fake_feishu.card_impl_calls == []


async def test_assignment_feedback_rejects_internal_bind_card_action(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "bind_card",
            json.dumps({"card_id": "om_feedback_1"}),
        )
    )

    assert out["ok"] is False
    assert "create, append, assigner_reply, or recipient_confirm" in out["error"]["message"]
    assert fake_client.calls == []


async def test_assignment_feedback_requires_explicit_entry_contract(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    for missing_field in ("author_role", "entry_type", "notification_strategy"):
        payload = {
            "raw_content": "请确认截止时间",
            "author_role": "recipient",
            "entry_type": "question",
            "notification_strategy": "non_blocking",
        }
        del payload[missing_field]
        out = json.loads(
            await module.assignment_feedback(
                "ou_assigner",
                "wa-1",
                "create",
                json.dumps(payload, ensure_ascii=False),
            )
        )
        assert out["ok"] is False
        assert missing_field in out["error"]["message"]

    assert fake_client.calls == []


async def test_assignment_feedback_requires_action_specific_roles_and_entry_types(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    assigner_reply = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "assigner_reply",
            json.dumps(
                {
                    "raw_content": "下周五",
                    "author_role": "assigner",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                },
                ensure_ascii=False,
            ),
        )
    )
    recipient_confirm = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "recipient_confirm",
            json.dumps(
                {
                    "raw_content": "确认",
                    "author_role": "recipient",
                    "entry_type": "reply",
                    "notification_strategy": "record_only",
                },
                ensure_ascii=False,
            ),
        )
    )

    assert assigner_reply["ok"] is False
    assert "entry_type" in assigner_reply["error"]["message"]
    assert recipient_confirm["ok"] is False
    assert "entry_type" in recipient_confirm["error"]["message"]
    assert fake_client.calls == []


async def test_assignment_feedback_rejects_string_attempts_before_memory_call(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认截止时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "attempts": "已检查任务原文",
                    "options": [
                        {"label": "本周", "value": "this_week"},
                        {"label": "下周", "value": "next_week"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert out["error"]["message"] == "payload_json.attempts must be an array of strings"
    assert fake_client.calls == []
    assert fake_feishu.card_impl_calls == []


async def test_assignment_feedback_rejects_notification_strategy_as_entry_type(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认截止时间",
                    "author_role": "recipient",
                    "entry_type": "blocking",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周", "value": "this_week"},
                        {"label": "下周", "value": "next_week"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert out["error"]["message"] == "payload_json.entry_type must be question, reply, confirm, or private_note"
    assert fake_client.calls == []
    assert fake_feishu.card_impl_calls == []


async def test_assignment_feedback_rejects_unknown_notification_strategy(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认截止时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "urgent",
                    "options": [
                        {"label": "本周", "value": "this_week"},
                        {"label": "下周", "value": "next_week"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["message"] == (
        "payload_json.notification_strategy must be blocking, non_blocking, or record_only"
    )
    assert fake_client.calls == []


async def test_assignment_feedback_rejects_more_than_three_blocking_options(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    options = [{"label": f"选项 {index}", "value": f"option_{index}"} for index in range(4)]

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认范围",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": options,
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["message"] == "blocking feedback requires 2-3 concrete options"
    assert fake_client.calls == []


async def test_assignment_feedback_sends_one_card_and_binds_its_message_id(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": None,
                    "entries": [],
                },
            },
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            },
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    payload = {
        "raw_content": "权限范围按哪个团队处理?",
        "author_role": "recipient",
        "entry_type": "question",
        "notification_strategy": "blocking",
        "stage": "方案设计",
        "missing_information": "目标团队范围",
        "why_blocked": "只有安排者能确定业务边界",
        "attempts": ["检查任务原文", "查询项目资料"],
        "impact": "无法确定权限模型覆盖范围",
        "options": [
            {"label": "仅项目成员", "value": "project", "recommended": True},
            {"label": "全组织", "value": "organization"},
        ],
        "private_note": "不要展示这段",
    }

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(payload, ensure_ascii=False),
        )
    )

    assert out["ok"] is True
    assert out["card_id"] == "om_feedback_1"
    assert out["assistant_reply_required"] is True
    assert out["assistant_reply"] == "已提交反馈, 等待安排者处理"
    assert [call[0] for call in fake_client.calls] == ["assignment_feedback", "assignment_feedback"]
    assert fake_client.calls[1][1] == {
        "arrangement_id": "wa-1",
        "action": "bind_card",
        "payload": {"card_id": "om_feedback_1"},
    }
    assert len(fake_feishu.card_impl_calls) == 1
    card_call = fake_feishu.card_impl_calls[0]
    assert card_call["receive_id"] == "ou_assigner"
    assert card_call["user_key"] == "ou_recipient"
    card_text = card_call["card_json"]
    assert "权限范围按哪个团队处理?" in card_text
    assert "Agent 分析" in card_text
    assert "不要展示这段" not in card_text
    assert fake_feishu.card_edit_calls == []


async def test_assignment_feedback_derives_sender_from_current_feishu_session(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": None,
                    "entries": [],
                },
            },
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            },
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(
        fake_client,
        fake_feishu,
        monkeypatch,
        session_id="feishu-ou_recipient",
    )

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认交付时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周内", "value": "this_week"},
                        {"label": "下周内", "value": "next_week"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is True
    assert fake_feishu.card_impl_calls[0]["user_key"] == "ou_recipient"


async def test_assignment_feedback_card_includes_custom_reply_form(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": None,
                    "entries": [],
                },
            },
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 1,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            },
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    await module.assignment_feedback(
        "ou_assigner",
        "wa-1",
        "create",
        json.dumps(
            {
                "raw_content": "请确认交付时间",
                "author_role": "recipient",
                "entry_type": "question",
                "notification_strategy": "blocking",
                "assignment_title": "测试授权流程",
                "stage": "时间确认",
                "options": [
                    {"label": "本周内", "value": "this_week"},
                    {"label": "下周内", "value": "next_week"},
                ],
            },
            ensure_ascii=False,
        ),
    )

    card = json.loads(fake_feishu.card_impl_calls[0]["card_json"])
    forms = [element for element in card["elements"] if element.get("tag") == "form"]
    assert len(forms) == 1
    assert any(
        element.get("tag") == "input" and element.get("name") == "custom_reply" for element in forms[0]["elements"]
    )
    assert any(
        element.get("action_type") == "form_submit"
        and element.get("value", {}).get("action") == "assignment_feedback_reply"
        for element in forms[0]["elements"]
    )
    card_text = json.dumps(card, ensure_ascii=False)
    visible_summary = card["elements"][0]["text"]["content"]
    assert "所属任务: 测试授权流程" in visible_summary
    assert "wa-1" not in visible_summary
    assert "wa-1" in card_text
    assert "本周内" in card_text
    assert "下周内" in card_text
    custom_input = next(element for element in forms[0]["elements"] if element.get("name") == "custom_reply")
    assert custom_input["required"] is True
    business_context = json.loads(fake_feishu.card_impl_calls[0]["business_context_json"])
    assert business_context["reply_target_open_id"] == "ou_assigner"
    assert business_context["stage"] == "时间确认"


async def test_assignment_feedback_rejects_reserved_custom_option_before_memory_call(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周", "value": "this_week"},
                        {"label": "下周", "value": "next_week"},
                        {"label": "其他时间", "value": "custom_time"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert "reserved custom option" in out["error"]["message"]
    assert fake_client.calls == []


async def test_assignment_feedback_rejects_custom_option_flag_before_memory_call(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周", "value": "this_week"},
                        {"label": "下周", "value": "next_week"},
                        {"label": "补充", "value": "supplement", "custom": True},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["message"] == "payload_json.options must not include custom=true"
    assert fake_client.calls == []


async def test_assignment_feedback_rejects_non_boolean_recommended_value(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周", "value": "this_week", "recommended": "yes"},
                        {"label": "下周", "value": "next_week"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["message"] == "payload_json.options[].recommended must be a boolean"
    assert fake_client.calls == []


async def test_assignment_feedback_handles_custom_reply_card_action(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": "om_feedback_1",
                    "entries": [
                        {
                            "version": 1,
                            "author_role": "recipient",
                            "entry_type": "question",
                            "raw_content": "请确认交付时间",
                        },
                        {
                            "version": 2,
                            "author_role": "assigner",
                            "entry_type": "reply",
                            "raw_content": "周一下午",
                        },
                    ],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "type": "assignment_feedback",
            "arrangement_id": "wa-1",
            "reply_target_open_id": "ou_assigner",
            "stage": "时间确认",
            "projection": {
                "assignment_title": "测试授权流程",
                "stage": "时间确认",
                "missing_information": "明确截止时间",
                "why_blocked": "只有安排者能确认",
                "attempts": ["检查任务原文", "查询项目记录"],
                "impact": "实施排期无法确定",
            },
        },
        "source": {
            "operator_open_id": "ou_assigner",
            "sender_open_id": "ou_recipient",
        },
        "action": {
            "form_value": {"custom_reply": "周一下午"},
            "value": {
                "action": "assignment_feedback_reply",
                "arrangement_id": "wa-1",
                "feedback_action": "assigner_reply",
            },
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is True
    assert out["state"] == "updated_waiting_recipient_confirmation"
    assert out["assistant_reply_required"] is False
    assert out["recipient_notified"] is True
    assert fake_client.calls == [
        (
            "assignment_feedback",
            {
                "arrangement_id": "wa-1",
                "action": "assigner_reply",
                "payload": {
                    "assignment_title": "测试授权流程",
                    "stage": "时间确认",
                    "missing_information": "明确截止时间",
                    "why_blocked": "只有安排者能确认",
                    "impact": "实施排期无法确定",
                    "attempts": ["检查任务原文", "查询项目记录"],
                    "raw_content": "周一下午",
                    "author_role": "assigner",
                    "entry_type": "reply",
                    "notification_strategy": "blocking",
                    "updated_understanding": "安排者已补充: 周一下午",
                },
            },
            False,
        )
    ]
    assert len(fake_feishu.card_impl_calls) == 1
    recipient_call = fake_feishu.card_impl_calls[0]
    assert recipient_call["receive_id"] == "ou_recipient"
    assert recipient_call["receive_id_type"] == "open_id"
    assert "周一下午" in recipient_call["card_json"]
    assert "确认更新后的理解" in recipient_call["card_json"]
    assert "所属任务: 测试授权流程" in recipient_call["card_json"]
    assert "缺少的信息: 明确截止时间" in recipient_call["card_json"]
    assert "已核查或尝试: 查询项目记录" in recipient_call["card_json"]
    assert json.loads(recipient_call["business_context_json"]) == {
        "type": "assignment_feedback_recipient_result",
        "arrangement_id": "wa-1",
        "thread_id": "feedback-1",
        "recipient_open_id": "ou_recipient",
        "stage": "时间确认",
        "projection": {
            "assignment_title": "测试授权流程",
            "stage": "时间确认",
            "missing_information": "明确截止时间",
            "why_blocked": "只有安排者能确认",
            "impact": "实施排期无法确定",
            "updated_understanding": "安排者已补充: 周一下午",
            "attempts": ["检查任务原文", "查询项目记录"],
        },
    }
    assert json.loads(recipient_call["action_handlers_json"]) == {"assignment_feedback_confirm": "assignment_feedback"}
    assert [call["message_id"] for call in fake_feishu.card_edit_calls] == ["om_feedback_1"]
    assert "周一下午" in fake_feishu.card_edit_calls[0]["card_json"]
    assert "所属任务: 测试授权流程" in fake_feishu.card_edit_calls[0]["card_json"]
    assert "缺少的信息: 明确截止时间" in fake_feishu.card_edit_calls[0]["card_json"]


async def test_assignment_feedback_recipient_confirmation_updates_both_cards(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "ready_to_execute",
                    "version": 3,
                    "card_id": "om_feedback_assigner",
                    "entries": [
                        {
                            "version": 1,
                            "author_role": "recipient",
                            "entry_type": "question",
                            "raw_content": "请确认交付时间",
                        },
                        {
                            "version": 2,
                            "author_role": "assigner",
                            "entry_type": "reply",
                            "raw_content": "周一下午",
                        },
                        {
                            "version": 3,
                            "author_role": "recipient",
                            "entry_type": "confirm",
                            "raw_content": "已确认更新后的理解",
                        },
                    ],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "message_id": "om_feedback_recipient",
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "type": "assignment_feedback_recipient_result",
            "arrangement_id": "wa-1",
            "thread_id": "feedback-1",
            "recipient_open_id": "ou_recipient",
            "stage": "时间确认",
        },
        "source": {"operator_open_id": "ou_recipient"},
        "action": {
            "value": {
                "action": "assignment_feedback_confirm",
                "arrangement_id": "wa-1",
                "feedback_action": "recipient_confirm",
            }
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is True
    assert out["state"] == "ready_to_execute"
    assert out["assistant_reply_required"] is False
    assert fake_client.calls == [
        (
            "assignment_feedback",
            {
                "arrangement_id": "wa-1",
                "action": "recipient_confirm",
                "payload": {
                    "raw_content": "已确认更新后的理解",
                    "author_role": "recipient",
                    "entry_type": "confirm",
                    "notification_strategy": "record_only",
                    "stage": "时间确认",
                },
            },
            False,
        )
    ]
    assert fake_feishu.card_impl_calls == []
    assert [call["message_id"] for call in fake_feishu.card_edit_calls] == [
        "om_feedback_assigner",
        "om_feedback_recipient",
    ]
    assert all("可继续执行" in call["card_json"] for call in fake_feishu.card_edit_calls)
    assert all("assignment_feedback_confirm" not in call["card_json"] for call in fake_feishu.card_edit_calls)


async def test_assignment_feedback_handles_quick_option_card_action(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "arrangement_id": "wa-1",
            "reply_target_open_id": "ou_assigner",
        },
        "operator_open_id": "ou_assigner",
        "action": {
            "value": {
                "action": "assignment_feedback_reply",
                "arrangement_id": "wa-1",
                "feedback_action": "assigner_reply",
                "selected_value": "this_week",
                "selected_label": "本周内",
            },
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is True
    assert fake_client.calls[0][1]["payload"]["raw_content"] == "本周内"
    assert fake_client.calls[0][1]["payload"]["author_role"] == "assigner"


async def test_assignment_feedback_callback_recovers_missing_memory_card_binding(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": None,
                    "entries": [],
                },
            },
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            },
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "message_id": "om_feedback_1",
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "arrangement_id": "wa-1",
            "reply_target_open_id": "ou_assigner",
        },
        "source": {"operator_open_id": "ou_assigner"},
        "action": {
            "value": {
                "action": "assignment_feedback_reply",
                "arrangement_id": "wa-1",
                "feedback_action": "assigner_reply",
                "selected_value": "this_week",
                "selected_label": "本周内",
            }
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is True
    assert out["card_id"] == "om_feedback_1"
    assert [call[1]["action"] for call in fake_client.calls] == ["assigner_reply", "bind_card"]
    assert fake_feishu.card_impl_calls == []
    assert [call["message_id"] for call in fake_feishu.card_edit_calls] == ["om_feedback_1"]


async def test_assignment_feedback_callback_fails_closed_without_card_id(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": None,
                    "entries": [],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "arrangement_id": "wa-1",
            "reply_target_open_id": "ou_assigner",
        },
        "source": {"operator_open_id": "ou_assigner"},
        "action": {
            "value": {
                "action": "assignment_feedback_reply",
                "arrangement_id": "wa-1",
                "feedback_action": "assigner_reply",
                "selected_value": "this_week",
                "selected_label": "本周内",
            }
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is False
    assert out["error"]["code"] == "feedback_card_binding_missing"
    assert len(fake_client.calls) == 1
    assert fake_feishu.card_impl_calls == []
    assert fake_feishu.card_edit_calls == []


async def test_assignment_feedback_rejects_card_reply_from_another_operator(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)
    card_action = {
        "dispatch": {"matched": True, "handler": "assignment_feedback"},
        "business_context": {
            "arrangement_id": "wa-1",
            "reply_target_open_id": "ou_assigner",
        },
        "source": {"operator_open_id": "ou_other"},
        "action": {
            "form_value": {"custom_reply": "周一下午"},
            "value": {
                "action": "assignment_feedback_reply",
                "arrangement_id": "wa-1",
                "feedback_action": "assigner_reply",
            },
        },
    }

    out = json.loads(await module.assignment_feedback(card_action_json=json.dumps(card_action, ensure_ascii=False)))

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert fake_client.calls == []
    assert fake_feishu.card_edit_calls == []


async def test_assignment_feedback_rejects_blocking_card_without_two_concrete_options(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认交付时间",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "options": [
                        {"label": "本周内", "value": "this_week"},
                        {"label": "其他时间", "value": "custom_time"},
                    ],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert "reserved custom option" in out["error"]["message"]
    assert fake_client.calls == []
    assert fake_feishu.card_impl_calls == []


async def test_assignment_feedback_updates_existing_card_in_place(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 2,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "assigner_reply",
            json.dumps(
                {
                    "raw_content": "先覆盖项目成员。",
                    "author_role": "assigner",
                    "entry_type": "reply",
                    "notification_strategy": "blocking",
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is True
    assert out["state"] == "updated_waiting_recipient_confirmation"
    assert fake_feishu.card_impl_calls == []
    assert [call["message_id"] for call in fake_feishu.card_edit_calls] == ["om_feedback_1"]
    assert "待接收者确认" in fake_feishu.card_edit_calls[0]["card_json"]
    assert "assignment_feedback_confirm" not in fake_feishu.card_edit_calls[0]["card_json"]


async def test_assignment_feedback_card_preserves_shared_entry_history(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "updated_waiting_recipient_confirmation",
                    "version": 3,
                    "card_id": "om_feedback_1",
                    "entries": [
                        {
                            "version": 1,
                            "author_role": "recipient",
                            "entry_type": "question",
                            "raw_content": "最初需要确认权限范围",
                        },
                        {
                            "version": 2,
                            "author_role": "assigner",
                            "entry_type": "private_note",
                            "raw_content": "仅安排者可见的背景",
                        },
                        {
                            "version": 3,
                            "author_role": "assigner",
                            "entry_type": "reply",
                            "raw_content": "先覆盖项目成员",
                        },
                    ],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    await module.assignment_feedback(
        "ou_assigner",
        "wa-1",
        "assigner_reply",
        json.dumps(
            {
                "raw_content": "先覆盖项目成员",
                "author_role": "assigner",
                "entry_type": "reply",
                "notification_strategy": "blocking",
            },
            ensure_ascii=False,
        ),
    )

    card_text = fake_feishu.card_edit_calls[0]["card_json"]
    assert "最初需要确认权限范围" in card_text
    assert "先覆盖项目成员" in card_text
    assert "仅安排者可见的背景" not in card_text


async def test_assignment_feedback_never_notifies_private_notes(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "thread_id": "feedback-1",
                    "arrangement_id": "wa-1",
                    "state": "open",
                    "version": 2,
                    "card_id": "om_feedback_1",
                    "entries": [],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_feedback_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "append",
            json.dumps(
                {
                    "raw_content": "仅安排者可见的背景",
                    "author_role": "assigner",
                    "entry_type": "private_note",
                    "notification_strategy": "record_only",
                },
                ensure_ascii=False,
            ),
        )
    )

    assert out["ok"] is True
    assert out["notified"] is False
    assert fake_feishu.card_impl_calls == []
    assert fake_feishu.card_edit_calls == []


async def test_assignment_send_card_builds_deterministic_actions(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "assignment_id": "wa-123",
                    "title": "同步客户会议后续",
                    "state": "assigned",
                    "assigner": {
                        "display_name": "张浩",
                        "feishu_open_id": "ou_assigner",
                    },
                    "recipients": [{"display_name": "王炜博", "feishu_open_id": "ou_recipient"}],
                    "original_request": "请整理会议结论并给出下一步方案。",
                    "context": "客户已确认一期范围。",
                    "expected_outcome": "提交可评审方案。",
                    "gaps": [{"description": "截止时间暂未指定"}],
                    "risks": [{"description": "不得把推测写成事实"}],
                    "evidence_refs": [{"uri": "https://example.com/source"}],
                    "action_items": [
                        {
                            "description": "准备实施方案",
                            "owner": {"display_name": "王炜博"},
                            "deadline": "2026-08-01",
                        }
                    ],
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = await module.assignment_send_card(
        receive_id="ou_recipient",
        assignment_id="wa-123",
        receive_id_type="open_id",
        user_key="ou_assigner",
    )

    assert json.loads(out)["ok"] is True
    call = fake_feishu.calls[0]
    assert call["receive_id"] == "ou_recipient"
    assert call["receive_id_type"] == "open_id"
    card = json.loads(call["card_json"])
    assert card["header"]["title"]["content"] == "新的工作安排"
    card_text = json.dumps(card, ensure_ascii=False)
    assert "同步客户会议后续" in card_text
    assert "请整理会议结论并给出下一步方案" in card_text
    assert "客户已确认一期范围" in card_text
    assert "提交可评审方案" in card_text
    assert "截止时间暂未指定" in card_text
    assert "不得把推测写成事实" in card_text
    assert "准备实施方案" in card_text
    assert "负责人: 王炜博" in card_text
    assert "截止时间: 2026-08-01" in card_text
    assert "https://example.com/source" in card_text
    assert "安排者原始内容 (原文或语音转写, 未改写)" in card_text
    assert "Agent 分析整理 (非安排者原话)" in card_text
    assert "参考资料" in card_text
    assert card_text.index("安排者原始内容") < card_text.index("Agent 分析整理") < card_text.index("参考资料")
    assert _button_values(card) == [
        {
            "action": "confirm_assignment_receipt",
            "assignment_id": "wa-123",
        }
    ]
    assert json.loads(call["business_context_json"]) == {
        "type": "work_assignment",
        "assignment_id": "wa-123",
        "title": "同步客户会议后续",
        "assigner_name": "张浩",
        "publish_target": "feishu_task",
    }
    assert json.loads(call["action_handlers_json"]) == {
        "confirm_assignment_receipt": "assignment_accept",
    }
    assert fake_client.calls[0] == (
        "assignment_get",
        {"assignment_id": "wa-123"},
        True,
    )


async def test_assignment_send_card_creates_assigner_progress_tracking(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [
            {
                "display_name": "接收者",
                "feishu_open_id": "ou_recipient",
            }
        ],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(
        await module.assignment_send_card(
            receive_id="ou_recipient",
            assignment_id="wa-123",
            receive_id_type="open_id",
            user_key="ou_assigner",
        )
    )

    assert out["ok"] is True
    assert out["delivery_tracking"]["tracked"] is True
    assert len(fake_feishu.calls) == 2
    recipient_card, progress_card = fake_feishu.calls
    assert recipient_card["receive_id"] == "ou_recipient"
    assert progress_card["receive_id"] == "ou_assigner"
    rendered = json.dumps(json.loads(progress_card["card_json"]), ensure_ascii=False)
    assert "任务接收进度" in rendered
    assert "海豚 Agent 权限管理" in rendered
    assert "已发送" in rendered
    assert "已读 (若可获取)" in rendered
    assert "已确认接收" in rendered
    assert "飞书任务已创建" in rendered
    assert "wa-123" not in rendered
    delivery_calls = [call for call in fake_client.calls if call[0] == "assignment_delivery"]
    assert [call[1]["action"] for call in delivery_calls] == [
        "create",
        "claim_send",
        "complete_send",
        "claim_send",
        "complete_send",
    ]
    payload = delivery_calls[0][1]["payload"]
    assert payload["assigner_open_id"] == "ou_assigner"
    assert "assigner_progress_message_id" not in payload
    assert "recipients" not in payload


async def test_assignment_send_card_claims_each_external_send_before_side_effect(
    monkeypatch,
):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
    }
    pending = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": None,
        "progress_status": "pending",
        "card_rendered_revision": 0,
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": None,
                "message_id": None,
                "send_status": "pending",
                "sent_at": None,
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "revision": 1,
        "status": "tracking",
    }
    recipient_claimed = {
        **pending,
        "recipients": [
            {
                **pending["recipients"][0],
                "delivery_open_id": "ou_recipient",
                "send_status": "claimed",
            }
        ],
        "revision": 2,
    }
    recipient_sent = {
        **recipient_claimed,
        "recipients": [
            {
                **recipient_claimed["recipients"][0],
                "message_id": "om_card_1",
                "send_status": "sent",
                "sent_at": "2026-08-03T08:00:00+00:00",
            }
        ],
        "revision": 3,
    }
    progress_claimed = {
        **recipient_sent,
        "progress_status": "claimed",
        "revision": 4,
    }
    completed = {
        **progress_claimed,
        "assigner_progress_message_id": "om_card_2",
        "progress_status": "sent",
        "revision": 5,
    }
    fake_client = _QueueMemoryClient(
        [{"ok": True, "result": assignment}],
        delivery_responses=[
            {"ok": True, "result": pending},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-recipient",
                    "target": "recipient",
                    "delivery": recipient_claimed,
                },
            },
            {"ok": True, "result": recipient_sent},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-progress",
                    "target": "progress",
                    "delivery": progress_claimed,
                },
            },
            {"ok": True, "result": completed},
        ],
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)
    real_send = module._feishu_message_send_card

    async def guarded_send(*args, **kwargs):
        actions = [
            arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
        ]
        expected = "claim_send"
        assert actions[-1] == expected
        return await real_send(*args, **kwargs)

    monkeypatch.setattr(module, "_feishu_message_send_card", guarded_send)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123", user_key="ignored-model-value"))

    assert out["ok"] is True
    assert [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ] == [
        "create",
        "claim_send",
        "complete_send",
        "claim_send",
        "complete_send",
    ]
    assert fake_feishu.calls[0]["receive_id"] == "ou_recipient"
    assert fake_feishu.calls[1]["receive_id"] == "ou_assigner"


async def test_assignment_send_card_rejects_non_assigner_session_before_sending(
    monkeypatch,
):
    fake_client = _QueueMemoryClient([{"ok": True, "result": _assignment_record()}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(
        fake_client,
        fake_feishu,
        monkeypatch,
        session_id="feishu-ou_intruder",
    )

    out = json.loads(await module.assignment_send_card("ou_receiver", "wa-123", user_key="ou_assigner"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_assigner_required"
    assert fake_feishu.calls == []
    assert [name for name, _arguments, _retryable in fake_client.calls] == ["assignment_get"]


async def test_assignment_send_card_reuses_existing_delivery_without_resending(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
    }
    existing = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient",
                "message_id": "om_recipient",
                "send_status": "sent",
            }
        ],
        "status": "tracking",
        "revision": 1,
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_client.delivery = existing
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123", user_key="ou_assigner"))

    assert out["ok"] is True
    assert out["already_sent"] is True
    assert out["delivery_tracking"]["tracked"] is True
    assert fake_feishu.calls == []
    assert [call[1]["action"] for call in fake_client.calls if call[0] == "assignment_delivery"] == [
        "create",
        "claim_send",
        "claim_send",
    ]


@pytest.mark.parametrize("send_status", ["claimed", "failed"])
async def test_assignment_send_card_never_retries_an_unreconciled_recipient_send(
    monkeypatch,
    send_status,
):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_client.delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": None,
        "progress_status": "pending",
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient",
                "message_id": None,
                "send_status": send_status,
            }
        ],
        "revision": 2,
    }
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_delivery_reconciliation_required"
    assert fake_feishu.calls == []


async def test_assignment_send_card_missing_message_id_requires_reconciliation(
    monkeypatch,
):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    async def send_without_message_id(*_args, **_kwargs):
        return json.dumps({"ok": True, "sent": True})

    monkeypatch.setattr(module, "_feishu_message_send_card", send_without_message_id)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123"))

    assert out["ok"] is False
    assert out["sent"] is True
    assert out["error"]["code"] == "assignment_delivery_reconciliation_required"
    assert [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ] == ["create", "claim_send", "fail_send"]


async def test_assignment_send_card_finalizes_claim_when_feishu_raises(
    monkeypatch,
):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    module = _import_assignment_send_card_with_fakes(fake_client, _FakeFeishuMessage(), monkeypatch)

    async def broken_send(*_args, **_kwargs):
        raise RuntimeError("Feishu transport failed")

    monkeypatch.setattr(module, "_feishu_message_send_card", broken_send)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123"))

    assert out["ok"] is False
    assert [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ] == ["create", "claim_send", "fail_send"]


async def test_assignment_send_card_delivers_each_recipient_on_one_shared_record(
    monkeypatch,
):
    assignment = {
        **_assignment_record(),
        "recipients": [
            {"display_name": "接收者甲", "feishu_open_id": "ou_first"},
            {"display_name": "接收者乙", "feishu_open_id": "ou_second"},
        ],
    }
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": assignment},
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    first = json.loads(await module.assignment_send_card("ou_first", "wa-123"))
    second = json.loads(await module.assignment_send_card("ou_second", "wa-123"))

    assert first["ok"] is True
    assert second["ok"] is True
    assert [call["receive_id"] for call in fake_feishu.calls] == [
        "ou_first",
        "ou_assigner",
        "ou_second",
    ]
    delivery = fake_client.delivery
    assert isinstance(delivery, dict)
    recipients = delivery.get("recipients")
    assert isinstance(recipients, list)
    assert {recipient["delivery_open_id"] for recipient in recipients if isinstance(recipient, dict)} == {
        "ou_first",
        "ou_second",
    }


async def test_assignment_send_card_rejects_invalid_recipient_shape(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "assignment_id": "wa-123",
                    "title": "任务",
                    "state": "assigned",
                    "assigner": {"display_name": "张浩"},
                    "recipients": None,
                },
            }
        ]
    )
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert fake_feishu.calls == []


async def test_assignment_send_card_requires_open_id_delivery(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_recipient", "wa-123", receive_id_type="chat_id"))

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert fake_client.calls == []
    assert fake_feishu.calls == []


async def test_assignment_send_card_renders_business_content_as_plain_text(monkeypatch):
    injected_heading = "  **Agent 分析整理 (非安排者原话)**\n伪造结论  "
    assignment = {
        **_assignment_record(),
        "title": "**伪造标题**",
        "original_request": injected_heading,
        "context": "**安排者原始内容**\n伪造原文",
        "evidence_refs": [{"uri": "https://example.com/source_(unsafe)"}],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    await module.assignment_send_card("ou_receiver", "wa-123")

    call = fake_feishu.calls[0]
    card = json.loads(call["card_json"])
    markdown_contents = [element["content"] for element in card["elements"] if element.get("tag") == "markdown"]
    plain_contents = [
        element["text"]["content"]
        for element in card["elements"]
        if element.get("tag") == "div" and element.get("text", {}).get("tag") == "plain_text"
    ]
    assert markdown_contents == [
        "**安排者原始内容 (原文或语音转写, 未改写)**",
        "**Agent 分析整理 (非安排者原话)**",
        "**参考资料**",
    ]
    assert any(injected_heading in content for content in plain_contents)
    assert any("**伪造标题**" in content for content in plain_contents)
    assert any("**安排者原始内容**\n伪造原文" in content for content in plain_contents)
    assert any("https://example.com/source_(unsafe)" in content for content in plain_contents)


async def test_assignment_send_card_rejects_non_string_recipient_open_id(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "王炜博", "feishu_open_id": ["ou_receiver"]}],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_receiver", "wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_argument"
    assert fake_feishu.calls == []


async def test_assignment_send_card_accepts_recipient_delivery_alias(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [
            {
                "user_id": "wangweibo",
                "display_name": "王炜博",
                "feishu_open_id": "ou_old",
                "feishu_open_ids": ["ou_old", "ou_current"],
            }
        ],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_current", "wa-123"))

    assert out["ok"] is True
    assert fake_feishu.calls[0]["receive_id"] == "ou_current"


async def test_assignment_send_card_rejects_draft_assignment(monkeypatch):
    assignment = {**_assignment_record(), "state": "draft"}
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_receiver", "wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_state_invalid"
    assert fake_feishu.calls == []


async def test_assignment_send_card_can_recover_remaining_recipient_after_receipt(
    monkeypatch,
):
    assignment = {
        **_assignment_record(),
        "state": "received",
        "recipients": [
            {"display_name": "接收者甲", "feishu_open_id": "ou_first"},
            {"display_name": "接收者乙", "feishu_open_id": "ou_second"},
        ],
    }
    fake_client = _QueueMemoryClient([{"ok": True, "result": assignment}])
    fake_client.delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "recipients": [
            {
                "open_ids": ["ou_first"],
                "delivery_open_id": "ou_first",
                "message_id": "om_first",
                "send_status": "sent",
            },
            {
                "open_ids": ["ou_second"],
                "delivery_open_id": None,
                "message_id": None,
                "send_status": "pending",
            },
        ],
        "revision": 5,
    }
    fake_feishu = _FakeFeishuMessage()
    module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)

    out = json.loads(await module.assignment_send_card("ou_second", "wa-123"))

    assert out["ok"] is True
    assert [call["receive_id"] for call in fake_feishu.calls] == ["ou_second"]


async def test_assignment_delivery_modules_expose_only_wrapper_coroutines(monkeypatch):
    fake_client = _QueueMemoryClient([])
    fake_feishu = _FakeFeishuMessage()
    send_module = _import_assignment_send_card_with_fakes(fake_client, fake_feishu, monkeypatch)
    accept_module = _import_assignment_accept_with_fakes(
        fake_client,
        _FakeFeishuTask(),
        "feishu-ou_receiver",
        monkeypatch,
    )
    refresh_module = _import_assignment_delivery_refresh_with_fakes(
        fake_client,
        fake_feishu,
        monkeypatch,
    )

    assert _public_coroutines(send_module) == ["assignment_send_card"]
    assert _public_coroutines(accept_module) == ["assignment_accept"]
    assert _public_coroutines(refresh_module) == ["assignment_delivery_refresh"]


async def test_assignment_accept_confirms_publishes_and_records_delivery(monkeypatch):
    assignment = _assignment_record()
    accepted = {**assignment, "state": "received", "revision": 2}
    events: list[tuple[str, Any]] = []
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                    "url": "https://feishu.example/task-1",
                },
            },
        ],
        events=events,
    )
    fake_tasks = _FakeFeishuTask(events=events)
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
        fake_messages=fake_messages,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out == {
        "ok": True,
        "assignment_id": "wa-123",
        "accepted": True,
        "published": True,
        "task_guid": "task-1",
        "url": "https://feishu.example/task-1",
        "discussion_invitation": {
            "enabled": False,
            "message": "任务已发布。要不要和我一起讨论一版可评审的实施方案",
            "sent": True,
        },
    }
    assert events[0] == ("memory", "assignment_get")
    assert events[1] == ("memory", "assignment_transition")
    business_events = [event for event in events if event != ("memory", "assignment_delivery")]
    assert business_events[2] == ("memory", "assignment_publication")
    assert business_events[3] == ("feishu_task", "海豚 Agent 权限管理")
    assert business_events[4] == ("memory", "assignment_publication")
    [task_call] = fake_tasks.calls
    assert task_call["summary"] == "海豚 Agent 权限管理"
    assert "安排者原始内容 (原文或语音转写, 未改写)\n\n请实现多用户权限管理。" in task_call["description"]
    assert "Agent 分析整理 (非安排者原话)" in task_call["description"]
    assert "背景: 现有会话按 open_id 路由。" in task_call["description"]
    assert "期望结果: 先提交可评审方案。" in task_call["description"]
    assert "待确认缺口: 截止时间待确认" in task_call["description"]
    assert "已识别风险: 需兼容已有会话" in task_call["description"]
    assert "行动项: 提交实施方案 | 负责人: 王炜博 | 截止时间: 2026-08-01" in task_call["description"]
    assert "参考资料\n\nhttps://example.com/permission-design" in task_call["description"]
    assert "工作安排编号: wa-123" in task_call["description"]
    assert task_call["due"] == "2026-08-01"
    assert task_call["assignees"] == "ou_receiver,ou_other"
    assert task_call["followers"] == ""
    assert task_call["user_key"] == "ou_receiver"
    assert task_call["identity"] == "bot"
    assert fake_messages.direct_calls == [
        {
            "receive_id": "ou_receiver",
            "text": "任务已发布。要不要和我一起讨论一版可评审的实施方案",
            "receive_id_type": "open_id",
            "on_behalf_of": "",
        }
    ]
    first_transition = fake_client.calls[1]
    assert first_transition == (
        "assignment_transition",
        {
            "assignment_id": "wa-123",
            "transition": {"transition_type": "confirm_receipt", "expected_revision": 1},
        },
        False,
    )
    publication_calls = [call for call in fake_client.calls if call[0] == "assignment_publication"]
    assert publication_calls[0] == (
        "assignment_publication",
        {"assignment_id": "wa-123", "action": "claim", "channel": "feishu_task"},
        False,
    )
    assert publication_calls[1] == (
        "assignment_publication",
        {
            "assignment_id": "wa-123",
            "action": "complete",
            "channel": "feishu_task",
            "claim_token": "claim-1",
            "publication": {
                "task_guid": "task-1",
                "url": "https://feishu.example/task-1",
                "recipient_open_ids": ["ou_receiver", "ou_other"],
                "published_by_open_id": "ou_receiver",
            },
        },
        False,
    )


async def test_assignment_accept_updates_progress_card_with_render_revision(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_receiver"}],
    }
    accepted = {**assignment, "state": "received", "revision": 2}
    delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "recipients": [
            {
                "open_id": "ou_recipient",
                "message_id": "om_recipient",
                "sent_at": "2026-08-03T08:00:00+00:00",
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "progress_status": "sent",
        "card_rendered_revision": 1,
        "revision": 1,
        "status": "tracking",
    }
    received_delivery = {
        **delivery,
        "recipients": [
            {
                **delivery["recipients"][0],
                "read_at": "2026-08-03T08:02:00+00:00",
                "accepted_at": "2026-08-03T08:02:00+00:00",
            }
        ],
        "revision": 2,
        "status": "confirmed",
    }
    received_rendered = {**received_delivery, "card_rendered_revision": 2}
    published_delivery = {
        **received_rendered,
        "task_published_at": "2026-08-03T08:03:00+00:00",
        "revision": 3,
    }
    published_rendered = {**published_delivery, "card_rendered_revision": 3}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                    "url": "https://feishu.example/task-1",
                },
            },
        ],
        delivery_responses=[
            {"ok": True, "result": delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": received_rendered},
            {"ok": True, "result": received_rendered},
            {"ok": True, "result": published_delivery},
            {"ok": True, "result": published_delivery},
            {"ok": True, "result": published_rendered},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        _FakeFeishuTask(),
        "feishu-ou_receiver",
        monkeypatch,
        fake_messages=fake_messages,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is True
    assert [call["message_id"] for call in fake_messages.card_edit_calls] == ["om_progress", "om_progress"]
    accepted_card = fake_messages.card_edit_calls[0]["card_json"]
    published_card = fake_messages.card_edit_calls[1]["card_json"]
    assert "已确认接收: 1/1" in accepted_card
    assert "飞书任务已创建: 否" in accepted_card
    assert "已确认接收: 1/1" in published_card
    assert "飞书任务已创建: 是" in published_card
    delivery_actions = [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ]
    assert delivery_actions == [
        "get",
        "advance",
        "get",
        "mark_card_rendered",
        "get",
        "advance",
        "get",
        "mark_card_rendered",
    ]


async def test_assignment_accept_defers_failed_immediate_progress_update(monkeypatch):
    assignment = {
        **_assignment_record(),
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_receiver"}],
    }
    accepted = {**assignment, "state": "received", "revision": 2}
    delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "recipients": [
            {
                "open_ids": ["ou_receiver"],
                "message_id": "om_recipient",
                "send_status": "sent",
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "revision": 1,
    }
    received_delivery = {
        **delivery,
        "recipients": [
            {
                **delivery["recipients"][0],
                "read_at": "2026-08-03T08:02:00+00:00",
                "accepted_at": "2026-08-03T08:02:00+00:00",
            }
        ],
        "revision": 2,
    }
    published_delivery = {
        **received_delivery,
        "task_published_at": "2026-08-03T08:03:00+00:00",
        "revision": 3,
    }
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                    "url": "https://feishu.example/task-1",
                },
            },
        ],
        delivery_responses=[
            {"ok": True, "result": delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": published_delivery},
        ],
    )
    module = _import_assignment_accept_with_fakes(
        fake_client,
        _FakeFeishuTask(),
        "feishu-ou_receiver",
        monkeypatch,
    )

    async def fail_progress_update(_client, *, assignment_id, title):
        assert assignment_id == "wa-123"
        assert title == "海豚 Agent 权限管理"
        return {
            "ok": False,
            "error": {
                "code": "feishu_card_update_failed",
                "message": "temporary Feishu failure",
                "retryable": True,
            },
        }

    monkeypatch.setattr(module, "_sync_progress_card", fail_progress_update)

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is True
    assert out["published"] is True
    assert out["progress_card_update"] == {
        "updated": False,
        "deferred": True,
        "error": {
            "code": "feishu_card_update_failed",
            "message": "temporary Feishu failure",
            "retryable": True,
        },
    }


async def test_assignment_delivery_refresh_advances_read_status_without_llm(monkeypatch):
    delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient",
                "message_id": "om_recipient",
                "send_status": "sent",
                "sent_at": "2026-08-03T08:00:00+00:00",
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "progress_status": "sent",
        "card_rendered_revision": 0,
        "revision": 1,
        "status": "tracking",
    }
    read_delivery = {
        **delivery,
        "recipients": [{**delivery["recipients"][0], "read_at": "2026-08-03T08:01:00+00:00"}],
        "revision": 2,
    }
    rendered_delivery = {**read_delivery, "card_rendered_revision": 2}
    fake_client = _QueueMemoryClient(
        [{"ok": True, "result": {"assignment_id": "wa-123", "title": "整理上线说明"}}],
        delivery_responses=[
            {"ok": True, "result": [delivery]},
            {"ok": True, "result": delivery},
            {"ok": True, "result": read_delivery},
            {"ok": True, "result": read_delivery},
            {"ok": True, "result": rendered_delivery},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_delivery_refresh_with_fakes(fake_client, fake_messages, monkeypatch)

    out = json.loads(await module.assignment_delivery_refresh(event_payload_json='{"tick":"2026-08-03T08:01"}'))

    assert out["ok"] is True
    assert out["checked"] == 1
    assert out["read_advanced"] == 1
    assert fake_messages.read_status_calls == [
        {
            "message_id": "om_recipient",
            "include_unread": False,
            "page_size": 100,
            "user_key": "",
        }
    ]
    assert len(fake_messages.card_edit_calls) == 1
    assert fake_messages.card_edit_calls[0]["message_id"] == "om_progress"
    assert "已读 (若可获取): 1/1" in fake_messages.card_edit_calls[0]["card_json"]
    delivery_actions = [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ]
    assert delivery_actions == [
        "list_pending",
        "get",
        "advance",
        "get",
        "mark_card_rendered",
    ]


async def test_assignment_delivery_refresh_renders_latest_state_after_revision_conflict(
    monkeypatch,
):
    stale = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient",
                "message_id": "om_recipient",
                "send_status": "sent",
                "sent_at": "2026-08-03T08:00:00+00:00",
                "read_at": "2026-08-03T08:01:00+00:00",
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "progress_status": "sent",
        "card_rendered_revision": 0,
        "revision": 1,
        "status": "tracking",
    }
    latest = {
        **stale,
        "recipients": [
            {
                **stale["recipients"][0],
                "accepted_at": "2026-08-03T08:02:00+00:00",
            }
        ],
        "card_rendered_revision": 3,
        "revision": 3,
    }
    rendered_latest = {**latest, "card_rendered_revision": 3}
    conflict = {
        "ok": False,
        "error": {
            "code": "invalid_request",
            "message": "assignment delivery revision conflict",
            "retryable": False,
        },
    }
    fake_client = _QueueMemoryClient(
        [{"ok": True, "result": _assignment_record()}],
        delivery_responses=[
            {"ok": True, "result": [stale]},
            {"ok": True, "result": stale},
            conflict,
            {"ok": True, "result": latest},
            {"ok": True, "result": rendered_latest},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_delivery_refresh_with_fakes(fake_client, fake_messages, monkeypatch)

    out = json.loads(await module.assignment_delivery_refresh())

    assert out["ok"] is True
    assert [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ] == [
        "list_pending",
        "get",
        "mark_card_rendered",
        "get",
        "mark_card_rendered",
    ]
    assert len(fake_messages.card_edit_calls) == 2
    rendered = json.loads(fake_messages.card_edit_calls[-1]["card_json"])
    assert "已确认接收: 1/1" in rendered["elements"][1]["content"]


async def test_assignment_delivery_refresh_marks_the_exact_rendered_revision(
    monkeypatch,
):
    dirty = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "card_rendered_revision": 1,
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient",
                "message_id": "om_recipient",
                "send_status": "sent",
                "sent_at": "2026-08-03T08:00:00+00:00",
                "read_at": "2026-08-03T08:01:00+00:00",
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "revision": 3,
        "status": "tracking",
    }
    rendered = {**dirty, "card_rendered_revision": 3}
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {"assignment_id": "wa-123", "title": "整理上线说明"},
            }
        ],
        delivery_responses=[
            {"ok": True, "result": [dirty]},
            {"ok": True, "result": dirty},
            {"ok": True, "result": rendered},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_delivery_refresh_with_fakes(fake_client, fake_messages, monkeypatch)

    out = json.loads(await module.assignment_delivery_refresh())

    assert out["ok"] is True
    assert out["card_updates"] == 1
    assert [
        arguments["action"] for name, arguments, _retryable in fake_client.calls if name == "assignment_delivery"
    ] == ["list_pending", "get", "mark_card_rendered"]
    mark_call = fake_client.calls[-1]
    assert mark_call[1]["payload"] == {"expected_revision": 3}


async def test_assignment_delivery_refresh_recovers_published_task_from_assignment(
    monkeypatch,
):
    delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "card_rendered_revision": 1,
        "recipients": [],
        "task_published_at": None,
        "revision": 1,
    }
    published = {
        **delivery,
        "task_published_at": "2026-08-03T08:02:00+00:00",
        "revision": 2,
    }
    rendered = {**published, "card_rendered_revision": 2}
    assignment = {
        "assignment_id": "wa-123",
        "title": "整理上线说明",
        "delivery_records": [
            {
                "channel": "feishu_task",
                "status": "published",
                "task_guid": "task-1",
            }
        ],
    }
    fake_client = _QueueMemoryClient(
        [{"ok": True, "result": assignment}],
        delivery_responses=[
            {"ok": True, "result": [delivery]},
            {"ok": True, "result": delivery},
            {"ok": True, "result": published},
            {"ok": True, "result": published},
            {"ok": True, "result": rendered},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_delivery_refresh_with_fakes(fake_client, fake_messages, monkeypatch)

    out = json.loads(await module.assignment_delivery_refresh())

    assert out["ok"] is True
    task_event = next(
        call for call in fake_client.calls if call[0] == "assignment_delivery" and call[1].get("action") == "advance"
    )
    assert task_event[1]["payload"]["event"] == "task_published"
    assert "飞书任务已创建: 是" in fake_messages.card_edit_calls[0]["card_json"]


async def test_assignment_delivery_refresh_isolates_one_broken_delivery(
    monkeypatch,
):
    broken = {"assignment_id": "wa-broken", "revision": 1}
    healthy = {
        "assignment_id": "wa-healthy",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "card_rendered_revision": 1,
        "recipients": [],
        "task_published_at": None,
        "revision": 1,
    }
    fake_client = _QueueMemoryClient(
        [
            RuntimeError("one assignment lookup failed"),
            {
                "ok": True,
                "result": {"assignment_id": "wa-healthy", "title": "健康任务"},
            },
        ],
        delivery_responses=[
            {"ok": True, "result": [broken, healthy]},
            {"ok": True, "result": healthy},
        ],
    )
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_delivery_refresh_with_fakes(fake_client, fake_messages, monkeypatch)

    out = json.loads(await module.assignment_delivery_refresh())

    assert out["ok"] is True
    assert out["checked"] == 1
    assert out["errors"][0]["assignment_id"] == "wa-broken"
    assert out["errors"][0]["code"] == "operation_failed"


async def test_assignment_accept_rejects_non_recipient(monkeypatch):
    fake_client = _QueueMemoryClient([{"ok": True, "result": _assignment_record()}])
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_intruder",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_recipient_required"
    assert len(fake_client.calls) == 1
    assert fake_tasks.calls == []


async def test_assignment_accept_preserves_memory_publication_error(monkeypatch):
    assignment = {**_assignment_record(), "state": "received"}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": False,
                "error": {
                    "code": "assignment_schema_migration_required",
                    "message": "Apply Fusion Memory Postgres migration 008_assignment_publications.sql",
                    "retryable": False,
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["published"] is False
    assert out["error"] == {
        "code": "assignment_schema_migration_required",
        "message": "Apply Fusion Memory Postgres migration 008_assignment_publications.sql",
        "retryable": False,
    }
    assert fake_tasks.calls == []


async def test_assignment_accept_uses_current_operator_alias_for_feishu_task(monkeypatch):
    assignment = {
        **_assignment_record(),
        "state": "received",
        "recipients": [
            {
                "user_id": "wangweibo",
                "display_name": "王炜博",
                "feishu_open_id": "ou_old",
                "feishu_open_ids": ["ou_old", "ou_current"],
            }
        ],
    }
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_current",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is True
    assert fake_tasks.calls[0]["assignees"] == "ou_current"
    complete_call = next(
        call
        for call in fake_client.calls
        if call[0] == "assignment_publication" and call[1].get("action") == "complete"
    )
    assert complete_call[1]["publication"]["recipient_open_ids"] == ["ou_current"]


async def test_assignment_accept_keeps_receipt_when_feishu_publish_fails(monkeypatch):
    assignment = _assignment_record()
    accepted = {**assignment, "state": "received", "revision": 2}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {"ok": True, "result": {"status": "failed", "channel": "feishu_task"}},
        ]
    )
    fake_tasks = _FakeFeishuTask(result={"ok": False, "code": 999, "message": "Feishu unavailable"})
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["published"] is False
    assert out["error"]["code"] == "feishu_task_publish_failed"
    failed_call = next(
        call for call in fake_client.calls if call[0] == "assignment_publication" and call[1].get("action") == "fail"
    )
    assert failed_call == (
        "assignment_publication",
        {
            "assignment_id": "wa-123",
            "action": "fail",
            "channel": "feishu_task",
            "claim_token": "claim-1",
            "publication": {
                "error_code": "999",
                "error_message": "Feishu unavailable",
                "published_by_open_id": "ou_receiver",
            },
        },
        False,
    )


async def test_assignment_accept_does_not_duplicate_published_task(monkeypatch):
    assignment = {
        **_assignment_record(),
        "state": "received",
    }
    delivery = {
        "assignment_id": "wa-123",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": "om_progress",
        "progress_status": "sent",
        "card_rendered_revision": 1,
        "recipients": [
            {
                "open_id": "ou_receiver",
                "message_id": "om_recipient",
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "revision": 1,
    }
    received_delivery = {
        **delivery,
        "recipients": [
            {
                **delivery["recipients"][0],
                "read_at": "2026-08-03T08:02:00+00:00",
                "accepted_at": "2026-08-03T08:02:00+00:00",
            }
        ],
        "revision": 2,
    }
    received_rendered = {**received_delivery, "card_rendered_revision": 2}
    published_delivery = {
        **received_rendered,
        "task_published_at": "2026-08-03T08:03:00+00:00",
        "revision": 3,
    }
    published_rendered = {**published_delivery, "card_rendered_revision": 3}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": False,
                    "claim_token": None,
                    "publication": {
                        "status": "published",
                        "channel": "feishu_task",
                        "task_guid": "task-existing",
                        "url": "https://feishu.example/task-existing",
                    },
                },
            },
        ],
        delivery_responses=[
            {"ok": True, "result": delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": received_delivery},
            {"ok": True, "result": received_rendered},
            {"ok": True, "result": received_rendered},
            {"ok": True, "result": published_delivery},
            {"ok": True, "result": published_delivery},
            {"ok": True, "result": published_rendered},
        ],
    )
    fake_tasks = _FakeFeishuTask()
    fake_messages = _FakeFeishuMessage()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
        fake_messages=fake_messages,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is True
    assert out["already_published"] is True
    assert out["task_guid"] == "task-existing"
    assert out["discussion_invitation"] == {
        "enabled": False,
        "message": "任务已发布。要不要和我一起讨论一版可评审的实施方案",
        "sent": False,
    }
    assert len(fake_client.calls) == 10
    assert fake_tasks.calls == []
    assert fake_messages.direct_calls == []
    assert [call["message_id"] for call in fake_messages.card_edit_calls] == ["om_progress", "om_progress"]
    assert "已确认接收: 1/1" in fake_messages.card_edit_calls[0]["card_json"]
    assert "飞书任务已创建: 是" in fake_messages.card_edit_calls[1]["card_json"]


async def test_assignment_accept_rejects_malformed_published_record(monkeypatch):
    assignment = {**_assignment_record(), "state": "received"}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": False,
                    "claim_token": None,
                    "publication": {
                        "status": "published",
                        "channel": "other_channel",
                        "task_guid": "",
                    },
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_publication_invalid"
    assert fake_tasks.calls == []


async def test_assignment_accept_rejects_claim_for_wrong_channel(monkeypatch):
    assignment = {**_assignment_record(), "state": "received"}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "other_channel"},
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_publication_invalid"
    assert fake_tasks.calls == []


async def test_assignment_accept_requires_reconciliation_after_failed_delivery(monkeypatch):
    assignment = {
        **_assignment_record(),
        "state": "received",
    }
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": False,
                    "claim_token": None,
                    "publication": {
                        "status": "failed",
                        "channel": "feishu_task",
                        "error_message": "Feishu unavailable",
                    },
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["error"]["code"] == "assignment_publication_reconciliation_required"
    assert out["error"]["retryable"] is False
    assert fake_tasks.calls == []


async def test_assignment_accept_requires_reconciliation_for_unfinished_claim(monkeypatch):
    assignment = _assignment_record()
    accepted = {**assignment, "state": "received", "revision": 2}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": False,
                    "claim_token": None,
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
        ]
    )
    module = _import_assignment_accept_with_fakes(
        fake_client,
        _FakeFeishuTask(),
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["error"]["code"] == "assignment_publication_reconciliation_required"
    assert out["error"]["retryable"] is False


async def test_assignment_accept_reports_created_task_when_memory_completion_fails(monkeypatch):
    assignment = {**_assignment_record(), "state": "received"}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": False,
                "error": {
                    "code": "postgres_unavailable",
                    "message": "Memory unavailable",
                    "retryable": True,
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["published"] is True
    assert out["delivery_recorded"] is False
    assert out["task_guid"] == "task-1"
    assert out["error"]["code"] == "assignment_delivery_record_failed"
    assert out["error"]["retryable"] is False
    assert len(fake_tasks.calls) == 1


async def test_assignment_accept_requires_published_memory_finalization(monkeypatch):
    assignment = {**_assignment_record(), "state": "received"}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-1",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {"ok": True, "result": {"status": "claimed", "channel": "feishu_task"}},
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["accepted"] is True
    assert out["published"] is True
    assert out["delivery_recorded"] is False
    assert out["error"]["code"] == "assignment_delivery_record_failed"
    assert out["error"]["retryable"] is False
    assert len(fake_tasks.calls) == 1


async def test_assignment_accept_recovers_when_another_gateway_confirmed_receipt(monkeypatch):
    assignment = _assignment_record()
    accepted = {**assignment, "state": "received", "revision": 2}
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "retryable": False,
                },
            },
            {"ok": True, "result": accepted},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-2",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is True
    assert len(fake_tasks.calls) == 1
    assert [call[0] for call in fake_client.calls if call[0] != "assignment_delivery"] == [
        "assignment_get",
        "assignment_transition",
        "assignment_get",
        "assignment_publication",
        "assignment_publication",
    ]


async def test_assignment_accept_rechecks_recipient_after_transition_conflict(monkeypatch):
    assignment = _assignment_record()
    reassigned = {
        **assignment,
        "state": "received",
        "revision": 2,
        "recipients": [{"display_name": "高博", "feishu_open_id": "ou_other"}],
    }
    fake_client = _QueueMemoryClient(
        [
            {"ok": True, "result": assignment},
            {
                "ok": False,
                "error": {"code": "invalid_request", "retryable": False},
            },
            {"ok": True, "result": reassigned},
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-3",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                },
            },
        ]
    )
    fake_tasks = _FakeFeishuTask()
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out["ok"] is False
    assert out["error"]["code"] == "assignment_recipient_required"
    assert [call[0] for call in fake_client.calls] == [
        "assignment_get",
        "assignment_transition",
        "assignment_get",
    ]
    assert fake_tasks.calls == []


async def test_work_assignment_skill_documents_generic_assignment_flow():
    skill_path = WORKSPACE_ROOT / "skills" / "work-assignment-delegation" / "SKILL.md"
    source = await anyio.Path(str(skill_path)).read_text(encoding="utf-8")
    assert "assignment_upsert" in source
    assert "assignment_transition" in source
    assert "不只限于开发任务" in source
    assert "不能把推测写成确定事实" in source
    assert "先调用 `assignment_list`" in source
    assert "再调用 `memory_search`" in source
    assert "`original_request` 必须逐字保存" in source
    assert "不要为同一逻辑任务生成新的 `idempotency_key`" in source
    assert "让/叫/安排/请" in source
    assert "写/做/处理/整理/实现/准备/提交/跟进" in source
    assert "看一看/看下/检查/验证/反馈/排查" in source
    assert "明确只是转达/带话/发一条普通消息" in source
    for field in ("context", "evidence_refs", "gaps", "risks", "action_items"):
        assert f"`{field}`" in source


async def test_work_assignment_skill_documents_feedback_lifecycle():
    source = await (anyio.Path(str(WORKSPACE_ROOT)) / "skills" / "work-assignment-delegation" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "`assignment_feedback`" in source
    assert "缺少只有安排者才能提供的必要信息" in source
    assert "可逆、明确记录的假设" in source
    assert "`updated_waiting_recipient_confirmation`" in source
    assert "不能自动恢复执行" in source
    assert "不能塞进 `assignment_transition`" in source
    assert "同一反馈对象只维护一条线程" in source
    assert "接收者结果卡" in source
    assert "不能创建新的反馈线程" in source
    assert "完整 `card_action_json`" in source
    assert "禁止调用 `tool_describe`、`tool_search_code`、`read` 或 `bash`" in source
    assert "成功后静默结束" in source
    assert "没有截止时间" in source
    assert "不得主动调用 `assignment_delivery_refresh`" in source


async def test_work_assignment_skill_documents_recipient_plan_flow():
    skill_path = WORKSPACE_ROOT / "skills" / "work-assignment-delegation" / "SKILL.md"
    source = await anyio.Path(str(skill_path)).read_text(encoding="utf-8")
    assert "接收者流程" in source
    assert "安排者原始内容" in source
    assert "非安排者原话" in source
    assert "可评审方案" in source
    assert "assignment_accept" in source
    assert "发布失败不撤销接收" in source
    assert 'transition_type: "submit_plan"' in source
    assert 'transition_type: "close"' in source
    assert "closure_reason" in source
    assert "不要调用 `closed_without_plan`" in source


async def test_work_assignment_skill_documents_scenario_templates():
    skill_path = WORKSPACE_ROOT / "skills" / "work-assignment-delegation" / "SKILL.md"
    source = await anyio.Path(str(skill_path)).read_text(encoding="utf-8")
    assert "场景模板" in source
    assert "通用工作安排" in source
    assert "开发任务" in source
    assert "交接或同步" in source
    assert "只改变表达和重点" in source
    assert "不得改变已确认事实" in source


async def test_index_does_not_execute_tool_modules(monkeypatch):
    # Indexing must be pure AST parsing: importing a tool module could trigger
    # side effects (e.g. connecting to an MCP server). Guard by making import
    # of a side-effectful module explode; index_tools must not touch it.
    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "_mcp":
            raise AssertionError("index_tools must not import tool modules")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    metas = await _idx.index_tools()
    assert metas  # still produced a full index


# ── extraction on a synthetic tools dir ──────────────────────────────────────


async def _write(dir_path: anyio.Path, name: str, body: str) -> None:
    await (dir_path / name).write_text(body, encoding="utf-8")


class _FakeMemoryClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], *, retryable: bool) -> dict[str, Any]:
        self.calls.append((name, arguments, retryable))
        return {"ok": True, "result": {"name": name, "arguments": arguments}}


class _QueueMemoryClient:
    def __init__(
        self,
        responses: list[dict[str, Any] | Exception],
        *,
        events: list[tuple[str, Any]] | None = None,
        delivery_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.events = events
        self.delivery_responses = list(delivery_responses) if delivery_responses is not None else None
        self.delivery: dict[str, Any] | None = None
        self.delivery_claims = 0

    async def call_tool(self, name: str, arguments: dict[str, Any], *, retryable: bool) -> dict[str, Any]:
        self.calls.append((name, arguments, retryable))
        if self.events is not None:
            self.events.append(("memory", name))
        if name == "assignment_delivery" and self.delivery_responses is None:
            return self._default_delivery_call(arguments)
        if name == "assignment_delivery":
            if not self.delivery_responses:
                raise AssertionError("unexpected assignment_delivery call")
            return self.delivery_responses.pop(0)
        if not self.responses:
            raise AssertionError(f"unexpected Memory call: {name}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def _default_delivery_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action")
        payload = arguments.get("payload") or {}
        if action == "create":
            if self.delivery is None:
                self.delivery = {
                    "assignment_id": arguments.get("assignment_id"),
                    "assigner_open_id": payload.get("assigner_open_id"),
                    "assigner_progress_message_id": None,
                    "progress_status": "pending",
                    "card_rendered_revision": 0,
                    "recipients": [],
                    "task_published_at": None,
                    "status": "tracking",
                    "revision": 1,
                }
            return {"ok": True, "result": self.delivery}
        if self.delivery is None:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "assignment delivery not found",
                    "retryable": False,
                },
            }
        if action == "get":
            return {"ok": True, "result": self.delivery}
        target = payload.get("target")
        recipient_open_id = payload.get("recipient_open_id")
        recipient: dict[str, Any] | None = None
        if target == "recipient":
            recipient = next(
                (item for item in self.delivery["recipients"] if recipient_open_id in item.get("open_ids", [])),
                None,
            )
            if recipient is None:
                recipient = {
                    "open_ids": [recipient_open_id],
                    "delivery_open_id": None,
                    "message_id": None,
                    "send_status": "pending",
                    "sent_at": None,
                    "read_at": None,
                    "accepted_at": None,
                }
                self.delivery["recipients"].append(recipient)
        if action == "claim_send":
            if target == "progress":
                status = self.delivery["progress_status"]
            else:
                assert recipient is not None
                status = recipient["send_status"]
            if status != "pending":
                return {
                    "ok": True,
                    "result": {
                        "acquired": False,
                        "claim_token": None,
                        "target": target,
                        "delivery": self.delivery,
                    },
                }
            self.delivery_claims += 1
            claim_token = f"claim-{self.delivery_claims}"
            if target == "progress":
                self.delivery["progress_status"] = "claimed"
            else:
                assert recipient is not None
                recipient["send_status"] = "claimed"
                recipient["delivery_open_id"] = recipient_open_id
            self.delivery["revision"] += 1
            return {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": claim_token,
                    "target": target,
                    "delivery": self.delivery,
                },
            }
        if action in {"complete_send", "fail_send"}:
            status = "sent" if action == "complete_send" else "failed"
            if target == "progress":
                self.delivery["progress_status"] = status
                if status == "sent":
                    self.delivery["assigner_progress_message_id"] = payload.get("message_id")
            else:
                assert recipient is not None
                recipient["send_status"] = status
                if status == "sent":
                    recipient["message_id"] = payload.get("message_id")
                    recipient["sent_at"] = "2026-08-03T08:00:00+00:00"
            self.delivery["revision"] += 1
            return {"ok": True, "result": self.delivery}
        raise AssertionError(f"unexpected assignment_delivery action: {action}")


class _FakeFeishuMessage:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.direct_calls: list[dict[str, str]] = []
        self.card_impl_calls: list[dict[str, str]] = []
        self.card_edit_calls: list[dict[str, str]] = []
        self.read_status_calls: list[dict[str, Any]] = []

    async def feishu_message_send_card(
        self,
        receive_id: str,
        card_json: str,
        receive_id_type: str = "chat_id",
        user_key: str = "",
        business_context_json: str = "{}",
        action_handlers_json: str = "{}",
    ) -> str:
        self.calls.append(
            {
                "receive_id": receive_id,
                "card_json": card_json,
                "receive_id_type": receive_id_type,
                "user_key": user_key,
                "business_context_json": business_context_json,
                "action_handlers_json": action_handlers_json,
            }
        )
        return json.dumps(
            {
                "ok": True,
                "sent": True,
                "message_id": f"om_card_{len(self.calls)}",
            },
            ensure_ascii=False,
        )

    async def send_message_impl(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str,
        on_behalf_of: str = "",
    ) -> dict[str, Any]:
        self.direct_calls.append(
            {
                "receive_id": receive_id,
                "text": text,
                "receive_id_type": receive_id_type,
                "on_behalf_of": on_behalf_of,
            }
        )
        return {"ok": True, "sent": True}

    async def send_card_impl(
        self,
        receive_id: str,
        card_json: str,
        receive_id_type: str,
        user_key: str | None = None,
        business_context_json: str = "{}",
        action_handlers_json: str = "{}",
    ) -> dict[str, Any]:
        self.card_impl_calls.append(
            {
                "receive_id": receive_id,
                "card_json": card_json,
                "receive_id_type": receive_id_type,
                "user_key": user_key or "",
                "business_context_json": business_context_json,
                "action_handlers_json": action_handlers_json,
            }
        )
        return {"ok": True, "sent": True, "message_id": "om_feedback_1"}

    async def edit_card_impl(self, message_id: str, card_json: str, user_key: str = "") -> dict[str, Any]:
        self.card_edit_calls.append({"message_id": message_id, "card_json": card_json, "user_key": user_key})
        return {"ok": True, "message_id": message_id, "edited": True}

    async def read_status_impl(
        self,
        message_id: str,
        include_unread: bool = True,
        page_size: int = 100,
        user_key: str = "",
    ) -> dict[str, Any]:
        self.read_status_calls.append(
            {
                "message_id": message_id,
                "include_unread": include_unread,
                "page_size": page_size,
                "user_key": user_key,
            }
        )
        return {
            "ok": True,
            "message_id": message_id,
            "read_users": [{"open_id": "ou_recipient", "read_time": "1785744060000"}],
        }


class _FakeFeishuTask:
    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        events: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.result = result or {
            "ok": True,
            "task_guid": "task-1",
            "url": "https://feishu.example/task-1",
        }
        self.calls: list[dict[str, str]] = []
        self.events = events

    async def feishu_task_create(
        self,
        summary: str,
        description: str = "",
        due: str = "",
        assignees: str = "",
        followers: str = "",
        user_key: str = "",
        identity: str = "",
    ) -> str:
        self.calls.append(
            {
                "summary": summary,
                "description": description,
                "due": due,
                "assignees": assignees,
                "followers": followers,
                "user_key": user_key,
                "identity": identity,
            }
        )
        if self.events is not None:
            self.events.append(("feishu_task", summary))
        return json.dumps(self.result, ensure_ascii=False)


def _assignment_record() -> dict[str, Any]:
    return {
        "assignment_id": "wa-123",
        "title": "海豚 Agent 权限管理",
        "state": "assigned",
        "assigner": {"display_name": "张浩", "feishu_open_id": "ou_assigner"},
        "recipients": [
            {"display_name": "王炜博", "feishu_open_id": "ou_receiver"},
            {"display_name": "高博", "feishu_open_id": "ou_other"},
        ],
        "original_request": "请实现多用户权限管理。",
        "context": "现有会话按 open_id 路由。",
        "expected_outcome": "先提交可评审方案。",
        "gaps": [{"description": "截止时间待确认"}],
        "risks": [{"description": "需兼容已有会话"}],
        "action_items": [
            {
                "description": "提交实施方案",
                "owner": {"display_name": "王炜博"},
                "deadline": "2026-08-01",
            }
        ],
        "evidence_refs": [{"uri": "https://example.com/permission-design"}],
        "delivery_records": [],
        "revision": 1,
    }


def _button_values(card: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for element in card.get("elements", []):
        if not isinstance(element, dict):
            continue
        for action in element.get("actions", []):
            if isinstance(action, dict) and isinstance(action.get("value"), dict):
                values.append(action["value"])
    return values


def _public_coroutines(module: types.ModuleType) -> list[str]:
    return sorted(
        name for name in dir(module) if not name.startswith("_") and inspect.iscoroutinefunction(getattr(module, name))
    )


def _import_assignment_tool_with_fake_client(name: str, fake_client: _FakeMemoryClient, monkeypatch) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    sys.modules.pop(name, None)
    assignment_common = TOOLS_DIR / "_assignment_tool_common.py"
    common_name = (
        f"fusion_memory_tool__assignment_tool_common_{hashlib.sha256(str(assignment_common).encode()).hexdigest()[:12]}"
    )
    fake_common_module = types.ModuleType(common_name)
    fake_common_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, common_name, fake_common_module)
    sys.modules.pop("_assignment_tool_common", None)
    return importlib.import_module(name)


def _import_assignment_send_card_with_fakes(
    fake_client: _QueueMemoryClient,
    fake_feishu: _FakeFeishuMessage,
    monkeypatch,
    *,
    session_id: str = "feishu-ou_assigner",
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_module = types.ModuleType("feishu_message")
    fake_module.__dict__["feishu_message_send_card"] = fake_feishu.feishu_message_send_card
    monkeypatch.setitem(sys.modules, "feishu_message", fake_module)
    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: session_id)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("assignment_send_card", None)
    return importlib.import_module("assignment_send_card")


def _import_assignment_feedback_with_fakes(
    fake_client: _QueueMemoryClient,
    fake_feishu: _FakeFeishuMessage,
    monkeypatch,
    *,
    session_id: str = "feishu-ou_recipient",
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_feishu_module = types.ModuleType("_feishu_impl")
    fake_feishu_module.__dict__["send_card_impl"] = fake_feishu.send_card_impl
    fake_feishu_module.__dict__["edit_card_impl"] = fake_feishu.edit_card_impl
    monkeypatch.setitem(sys.modules, "_feishu_impl", fake_feishu_module)
    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: session_id)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("assignment_feedback", None)
    return importlib.import_module("assignment_feedback")


def _import_assignment_accept_with_fakes(
    fake_client: _QueueMemoryClient,
    fake_tasks: _FakeFeishuTask,
    session_id: str,
    monkeypatch,
    *,
    fake_messages: _FakeFeishuMessage | None = None,
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_task_module = types.ModuleType("feishu_task")
    fake_task_module.__dict__["_feishu_task_create_once"] = fake_tasks.feishu_task_create
    monkeypatch.setitem(sys.modules, "feishu_task", fake_task_module)
    fake_messages = fake_messages or _FakeFeishuMessage()
    fake_feishu_impl_module = types.ModuleType("_feishu_impl")
    fake_feishu_impl_module.__dict__["send_message_impl"] = fake_messages.send_message_impl
    fake_feishu_impl_module.__dict__["edit_card_impl"] = fake_messages.edit_card_impl
    monkeypatch.setitem(sys.modules, "_feishu_impl", fake_feishu_impl_module)
    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: session_id)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("_assignment_delivery", None)
    sys.modules.pop("assignment_accept", None)
    return importlib.import_module("assignment_accept")


def _import_assignment_delivery_refresh_with_fakes(
    fake_client: _QueueMemoryClient,
    fake_messages: _FakeFeishuMessage,
    monkeypatch,
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_feishu_impl_module = types.ModuleType("_feishu_impl")
    fake_feishu_impl_module.__dict__["read_status_impl"] = fake_messages.read_status_impl
    fake_feishu_impl_module.__dict__["edit_card_impl"] = fake_messages.edit_card_impl
    monkeypatch.setitem(sys.modules, "_feishu_impl", fake_feishu_impl_module)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("_assignment_delivery", None)
    sys.modules.pop("assignment_delivery_refresh", None)
    return importlib.import_module("assignment_delivery_refresh")


async def test_extract_signature_and_docstring(tmp_path):
    d = anyio.Path(str(tmp_path))
    await _write(
        d,
        "sample.py",
        (
            "async def sample(a: str, b: int = 3, flag: bool = False,\n"
            "                 items: list[str] | None = None) -> str:\n"
            '    """Do a sample thing.\n'
            "\n"
            "    More detail here.\n"
            "\n"
            "    Args:\n"
            "        a: first.\n"
            "    Returns:\n"
            "        text.\n"
            '    """\n'
            "    return a\n"
        ),
    )
    metas = await _idx.index_tools(d)
    assert len(metas) == 1
    m = metas[0]
    assert m.name == "sample"
    assert m.file == "sample.py"
    assert m.signature == "sample(a: str, b: int = 3, flag: bool = False, items: list[str] | None = None)"
    assert m.summary == "Do a sample thing."
    # description stops before Args:/Returns:
    assert "More detail here." in m.description
    assert "first" not in m.description
    assert "Args:" in m.docstring


async def test_syntax_error_file_is_skipped(tmp_path):
    d = anyio.Path(str(tmp_path))
    await _write(d, "good.py", 'async def good() -> str:\n    """Good."""\n    return "x"\n')
    await _write(d, "broken.py", "async def broken( : oops\n")
    metas = await _idx.index_tools(d)
    assert {m.name for m in metas} == {"good"}


async def test_only_async_top_level_public_functions(tmp_path):
    d = anyio.Path(str(tmp_path))
    await _write(
        d,
        "mixed.py",
        (
            "def sync_fn():\n    return 1\n\n"
            "async def _private():\n    return 1\n\n"
            'async def real_tool() -> str:\n    """Real."""\n    return "x"\n'
        ),
    )
    metas = await _idx.index_tools(d)
    assert {m.name for m in metas} == {"real_tool"}


# ── tool_search ──────────────────────────────────────────────────────────────


async def test_tool_search_matches_known_tool():
    out = await tool_search("fetch url markdown")
    assert "fetch" in out


async def test_tool_search_empty_result():
    out = await tool_search("zzz_nonexistent_keyword_qqq")
    assert "no tools match" in out


async def test_tool_search_limit_truncates():
    out = await tool_search("", limit=3)
    lines = [ln for ln in out.splitlines() if " — " in ln and not ln.startswith("[")]
    assert len(lines) == 3
    assert "Truncated at 3" in out


# ── tool_search_code ─────────────────────────────────────────────────────────


async def test_tool_search_code_finds_line():
    out = await tool_search_code(r"def fetch\(")
    assert "fetch.py:" in out
    assert "def fetch(" in out


async def test_tool_search_code_invalid_regex_falls_back():
    out = await tool_search_code("fetch(")  # unbalanced paren -> invalid regex
    assert "Invalid regex" in out
    assert "fetch.py:" in out


async def test_tool_search_code_limit_truncates():
    out = await tool_search_code("import", limit=2)
    hits = [ln for ln in out.splitlines() if ":" in ln and not ln.startswith("[")]
    assert len(hits) == 2
    assert "Truncated at 2" in out


# ── tool_describe ────────────────────────────────────────────────────────────


async def test_tool_describe_known_tool():
    out = await tool_describe("find_files")
    assert "Tool: find_files" in out
    assert "File: find_files.py" in out
    assert "Signature: async def find_files(" in out
    assert "glob pattern" in out


async def test_tool_describe_unknown_suggests():
    out = await tool_describe("fetc")
    assert "no tool named 'fetc'" in out
    assert "fetch" in out


async def test_tool_describe_unknown_no_suggestion():
    out = await tool_describe("zzz_nope_qqq")
    assert "no tool named 'zzz_nope_qqq'" in out
    assert "tool_search" in out


# ── tools load cleanly into the framework registry ───────────────────────────


async def test_discovery_tools_are_valid_tool_functions():
    for name in ("tool_search", "tool_search_code", "tool_describe"):
        mod = importlib.import_module(name)
        func = getattr(mod, name)
        tf = ToolFunction.from_callable(func)
        assert tf.name == name
        assert tf.description
        assert tf.parameters["type"] == "object"

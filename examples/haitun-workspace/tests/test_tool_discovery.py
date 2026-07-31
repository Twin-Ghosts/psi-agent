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
        "assignment_send_card",
        "assignment_accept",
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


async def test_assignment_send_card_builds_deterministic_actions(monkeypatch):
    fake_client = _QueueMemoryClient(
        [
            {
                "ok": True,
                "result": {
                    "assignment_id": "wa-123",
                    "title": "同步客户会议后续",
                    "state": "assigned",
                    "assigner": {"display_name": "张浩"},
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
    [call] = fake_feishu.calls
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
    assert fake_client.calls == [("assignment_get", {"assignment_id": "wa-123"}, True)]


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

    [call] = fake_feishu.calls
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

    assert _public_coroutines(send_module) == ["assignment_send_card"]
    assert _public_coroutines(accept_module) == ["assignment_accept"]


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
    module = _import_assignment_accept_with_fakes(
        fake_client,
        fake_tasks,
        "feishu-ou_receiver",
        monkeypatch,
    )

    out = json.loads(await module.assignment_accept("wa-123"))

    assert out == {
        "ok": True,
        "assignment_id": "wa-123",
        "accepted": True,
        "published": True,
        "task_guid": "task-1",
        "url": "https://feishu.example/task-1",
    }
    assert events[0] == ("memory", "assignment_get")
    assert events[1] == ("memory", "assignment_transition")
    assert events[2] == ("memory", "assignment_publication")
    assert events[3] == ("feishu_task", "海豚 Agent 权限管理")
    assert events[4] == ("memory", "assignment_publication")
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
    first_transition = fake_client.calls[1]
    assert first_transition == (
        "assignment_transition",
        {
            "assignment_id": "wa-123",
            "transition": {"transition_type": "confirm_receipt", "expected_revision": 1},
        },
        False,
    )
    assert fake_client.calls[2] == (
        "assignment_publication",
        {"assignment_id": "wa-123", "action": "claim", "channel": "feishu_task"},
        False,
    )
    assert fake_client.calls[3] == (
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
    assert fake_client.calls[2][1]["publication"]["recipient_open_ids"] == ["ou_current"]


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
    assert fake_client.calls[3] == (
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
    assert out["already_published"] is True
    assert out["task_guid"] == "task-existing"
    assert len(fake_client.calls) == 2
    assert fake_tasks.calls == []


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
    assert [call[0] for call in fake_client.calls] == [
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
    for field in ("context", "evidence_refs", "gaps", "risks", "action_items"):
        assert f"`{field}`" in source
    assert "没有截止时间" in source


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
        responses: list[dict[str, Any]],
        *,
        events: list[tuple[str, Any]] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        self.events = events

    async def call_tool(self, name: str, arguments: dict[str, Any], *, retryable: bool) -> dict[str, Any]:
        self.calls.append((name, arguments, retryable))
        if self.events is not None:
            self.events.append(("memory", name))
        if not self.responses:
            raise AssertionError(f"unexpected Memory call: {name}")
        return self.responses.pop(0)


class _FakeFeishuMessage:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

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
        return json.dumps({"ok": True, "sent": True}, ensure_ascii=False)


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
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_module = types.ModuleType("feishu_message")
    fake_module.__dict__["feishu_message_send_card"] = fake_feishu.feishu_message_send_card
    monkeypatch.setitem(sys.modules, "feishu_message", fake_module)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("assignment_send_card", None)
    return importlib.import_module("assignment_send_card")


def _import_assignment_accept_with_fakes(
    fake_client: _QueueMemoryClient,
    fake_tasks: _FakeFeishuTask,
    session_id: str,
    monkeypatch,
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    fake_mcp_module = types.ModuleType(mcp_module_name)
    fake_mcp_module.__dict__["CLIENT"] = fake_client
    monkeypatch.setitem(sys.modules, mcp_module_name, fake_mcp_module)
    fake_task_module = types.ModuleType("feishu_task")
    fake_task_module.__dict__["_feishu_task_create_once"] = fake_tasks.feishu_task_create
    monkeypatch.setitem(sys.modules, "feishu_task", fake_task_module)
    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: session_id)
    sys.modules.pop("_assignment_tool_common", None)
    sys.modules.pop("assignment_accept", None)
    return importlib.import_module("assignment_accept")


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

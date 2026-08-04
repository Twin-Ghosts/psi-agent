"""Tests for the Haitun workspace tool-discovery meta-tools.

Covers ``_tool_index`` (static AST scan) and the ``tool_search`` /
``tool_search_code`` / ``tool_describe`` tools built on top of it.
"""

from __future__ import annotations

import builtins
import hashlib
import importlib
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
    read_tools = source.split("READ_TOOLS", 1)[1].split("}", 1)[0]
    assert '"assignment_get"' in read_tools
    assert '"assignment_list"' in read_tools
    assert '"assignment_upsert"' not in read_tools


async def test_assignment_upsert_binds_session_identity_and_normalizes_fields(monkeypatch):
    memory = _MemoryStub(
        assignment_upsert=[{"ok": True, "result": {"assignment_id": "wa-1"}}],
    )
    module = _import_assignment_module("assignment_upsert", memory, monkeypatch)

    result = json.loads(
        await module.assignment_upsert(
            json.dumps(
                {
                    "title": "整理会议结论",
                    "assigner": {"user_id": "untrusted"},
                    "recipients": [{"user_id": "recipient"}],
                    "gaps": ["截止时间待确认"],
                    "risks": ["不要把推测写成事实"],
                    "action_items": ["提交方案"],
                    "evidence_refs": ["https://example.com/source"],
                },
                ensure_ascii=False,
            )
        )
    )

    assert result["ok"] is True
    forwarded = memory.calls[0][1]["assignment"]
    assert forwarded["assigner"] == {
        "user_id": "ou_assigner",
        "display_name": "ou_assigner",
        "feishu_open_id": "ou_assigner",
    }
    assert forwarded["gaps"] == [{"description": "截止时间待确认"}]
    assert forwarded["risks"] == [{"description": "不要把推测写成事实"}]
    assert forwarded["action_items"] == [{"description": "提交方案"}]
    assert forwarded["evidence_refs"] == [{"uri": "https://example.com/source"}]


async def test_assignment_feedback_validates_before_memory_calls(monkeypatch):
    memory = _MemoryStub()
    module = _import_assignment_module("assignment_feedback", memory, monkeypatch)

    invalid_json = json.loads(await module.assignment_feedback("ou_assigner", "wa-1", "create", "not-json"))
    unknown_action = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "bind_card",
            json.dumps({"raw_content": "内部动作不应公开"}, ensure_ascii=False),
        )
    )
    malformed_blocking = json.loads(
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
                    "options": [{"label": "仅当前团队", "value": "team"}],
                },
                ensure_ascii=False,
            ),
        )
    )

    assert {invalid_json["error"]["code"], unknown_action["error"]["code"]} == {"invalid_argument"}
    assert malformed_blocking["error"]["code"] == "invalid_argument"
    assert memory.calls == []


async def test_assignment_feedback_sends_and_binds_one_blocking_card(monkeypatch):
    memory = _MemoryStub(
        assignment_feedback=[
            {"ok": True, "result": _feedback_thread()},
            {"ok": True, "result": _feedback_thread(card_id="om_feedback")},
        ],
    )
    feishu = _FeishuStub()
    module = _import_assignment_module("assignment_feedback", memory, monkeypatch, feishu=feishu)

    result = json.loads(
        await module.assignment_feedback(
            "ou_assigner",
            "wa-1",
            "create",
            json.dumps(
                {
                    "raw_content": "请确认权限范围",
                    "author_role": "recipient",
                    "entry_type": "question",
                    "notification_strategy": "blocking",
                    "attempts": ["核查任务原文"],
                    "options": [
                        {"label": "仅当前团队", "value": "team", "recommended": True},
                        {"label": "整个组织", "value": "organization"},
                    ],
                    "private_note": "仅 Agent 可见",
                },
                ensure_ascii=False,
            ),
        )
    )

    assert result["ok"] is True
    assert result["card_id"] == "om_feedback"
    assert len(feishu.sent_cards) == 1
    assert "请确认权限范围" in feishu.sent_cards[0]["card_json"]
    assert "仅 Agent 可见" not in feishu.sent_cards[0]["card_json"]
    assert [call[1]["action"] for call in memory.calls] == ["create", "bind_card"]


async def test_assignment_send_card_claims_before_each_external_send(monkeypatch):
    pending = _delivery()
    recipient_claimed = _delivery(recipient_status="claimed", revision=2)
    recipient_sent = _delivery(recipient_status="sent", recipient_message_id="om_recipient", revision=3)
    progress_claimed = _delivery(recipient_status="sent", progress_status="claimed", revision=4)
    complete = _delivery(
        recipient_status="sent",
        recipient_message_id="om_recipient",
        progress_status="sent",
        progress_message_id="om_progress",
        revision=5,
    )
    memory = _MemoryStub(
        assignment_get=[{"ok": True, "result": _assignment()}],
        assignment_delivery=[
            {"ok": True, "result": pending},
            _claim("recipient", recipient_claimed),
            {"ok": True, "result": recipient_sent},
            _claim("progress", progress_claimed),
            {"ok": True, "result": complete},
        ],
    )
    feishu = _FeishuStub()
    module = _import_assignment_module("assignment_send_card", memory, monkeypatch, feishu=feishu)

    result = json.loads(await module.assignment_send_card("ou_recipient", "wa-1"))

    assert result["ok"] is True
    assert [call[1]["action"] for call in memory.calls if call[0] == "assignment_delivery"] == [
        "create",
        "claim_send",
        "complete_send",
        "claim_send",
        "complete_send",
    ]
    assert [call["receive_id"] for call in feishu.cards] == ["ou_recipient", "ou_assigner"]
    recipient_card = json.loads(feishu.cards[0]["card_json"])
    assert _button_values(recipient_card) == [{"action": "confirm_assignment_receipt", "assignment_id": "wa-1"}]
    assert "wa-1" not in feishu.cards[1]["card_json"]


async def test_assignment_accept_publishes_once_and_invites_discussion(monkeypatch):
    accepted = {**_assignment(), "state": "received", "revision": 2}
    memory = _MemoryStub(
        assignment_get=[{"ok": True, "result": _assignment()}],
        assignment_transition=[{"ok": True, "result": accepted}],
        assignment_publication=[
            {
                "ok": True,
                "result": {
                    "acquired": True,
                    "claim_token": "claim-publication",
                    "publication": {"status": "claimed", "channel": "feishu_task"},
                },
            },
            {
                "ok": True,
                "result": {
                    "status": "published",
                    "channel": "feishu_task",
                    "task_guid": "task-1",
                    "url": "https://example.com/task-1",
                },
            },
        ],
    )
    feishu = _FeishuStub()
    module = _import_assignment_module(
        "assignment_accept",
        memory,
        monkeypatch,
        feishu=feishu,
        task=feishu,
        session_id="feishu-ou_recipient",
    )

    result = json.loads(await module.assignment_accept("wa-1"))

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["published"] is True
    assert len(feishu.tasks) == 1
    assert feishu.tasks[0]["assignees"] == "ou_recipient"
    assert feishu.messages[0]["text"] == "任务已发布。要不要和我一起讨论一版可评审的实施方案"
    assert [call[1]["action"] for call in memory.calls if call[0] == "assignment_publication"] == [
        "claim",
        "complete",
    ]


async def test_assignment_accept_reuses_existing_publication(monkeypatch):
    memory = _MemoryStub(
        assignment_get=[{"ok": True, "result": {**_assignment(), "state": "received"}}],
        assignment_publication=[
            {
                "ok": True,
                "result": {
                    "acquired": False,
                    "claim_token": None,
                    "publication": {
                        "status": "published",
                        "channel": "feishu_task",
                        "task_guid": "task-existing",
                        "url": "https://example.com/task-existing",
                    },
                },
            }
        ],
    )
    feishu = _FeishuStub()
    module = _import_assignment_module(
        "assignment_accept",
        memory,
        monkeypatch,
        feishu=feishu,
        task=feishu,
        session_id="feishu-ou_recipient",
    )

    result = json.loads(await module.assignment_accept("wa-1"))

    assert result["ok"] is True
    assert result["already_published"] is True
    assert result["task_guid"] == "task-existing"
    assert feishu.tasks == []


async def test_assignment_delivery_refresh_advances_read_status(monkeypatch):
    pending = _delivery(recipient_status="sent", recipient_message_id="om_recipient")
    memory = _MemoryStub(
        assignment_delivery=[{"ok": True, "result": [pending]}],
        assignment_get=[{"ok": True, "result": _assignment()}],
    )
    feishu = _FeishuStub()
    module = _import_assignment_module("assignment_delivery_refresh", memory, monkeypatch, feishu=feishu)
    advanced: list[tuple[str, str]] = []

    async def _advance(_client, *, assignment_id, event, recipient_open_id=""):
        advanced.append((event, recipient_open_id))
        return {"ok": True, "result": {"assignment_id": assignment_id}}

    async def _sync(_client, *, assignment_id, title):
        assert (assignment_id, title) == ("wa-1", "整理会议结论")
        return {"ok": True, "updated": True}

    monkeypatch.setattr(module, "_advance_delivery", _advance)
    monkeypatch.setattr(module, "_sync_progress_card", _sync)

    result = json.loads(await module.assignment_delivery_refresh())

    assert result == {"ok": True, "checked": 1, "read_advanced": 1, "card_updates": 1, "errors": []}
    assert advanced == [("read", "ou_recipient")]
    assert feishu.reads == ["om_recipient"]


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


class _MemoryStub:
    def __init__(self, **responses: list[dict[str, Any]]) -> None:
        self.responses = {name: list(values) for name, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], *, retryable: bool) -> dict[str, Any]:
        self.calls.append((name, arguments, retryable))
        queue = self.responses.get(name)
        if queue:
            return queue.pop(0)
        return {
            "ok": False,
            "error": {"code": "not_configured", "message": f"no response for {name}", "retryable": False},
        }


class _FeishuStub:
    def __init__(self) -> None:
        self.cards: list[dict[str, str]] = []
        self.sent_cards: list[dict[str, str]] = []
        self.edits: list[dict[str, str]] = []
        self.messages: list[dict[str, str]] = []
        self.reads: list[str] = []
        self.tasks: list[dict[str, str]] = []

    async def feishu_message_send_card(
        self,
        receive_id: str,
        card_json: str,
        receive_id_type: str = "chat_id",
        user_key: str = "",
        business_context_json: str = "{}",
        action_handlers_json: str = "{}",
    ) -> str:
        self.cards.append(
            {
                "receive_id": receive_id,
                "card_json": card_json,
                "receive_id_type": receive_id_type,
                "user_key": user_key,
                "business_context_json": business_context_json,
                "action_handlers_json": action_handlers_json,
            }
        )
        return json.dumps({"ok": True, "sent": True, "message_id": f"om_{len(self.cards)}"})

    async def send_card_impl(
        self,
        receive_id: str,
        card_json: str,
        receive_id_type: str,
        user_key: str | None = None,
        business_context_json: str = "{}",
        action_handlers_json: str = "{}",
    ) -> dict[str, Any]:
        self.sent_cards.append(
            {
                "receive_id": receive_id,
                "card_json": card_json,
                "receive_id_type": receive_id_type,
                "user_key": user_key or "",
                "business_context_json": business_context_json,
                "action_handlers_json": action_handlers_json,
            }
        )
        return {"ok": True, "sent": True, "message_id": "om_feedback"}

    async def edit_card_impl(self, message_id: str, card_json: str, user_key: str = "") -> dict[str, Any]:
        self.edits.append({"message_id": message_id, "card_json": card_json, "user_key": user_key})
        return {"ok": True, "edited": True, "message_id": message_id}

    async def send_message_impl(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str,
        on_behalf_of: str = "",
    ) -> dict[str, Any]:
        self.messages.append(
            {
                "receive_id": receive_id,
                "text": text,
                "receive_id_type": receive_id_type,
                "on_behalf_of": on_behalf_of,
            }
        )
        return {"ok": True, "sent": True}

    async def read_status_impl(
        self,
        message_id: str,
        include_unread: bool = True,
        page_size: int = 100,
        user_key: str = "",
    ) -> dict[str, Any]:
        self.reads.append(message_id)
        return {"ok": True, "read_users": [{"open_id": "ou_recipient"}]}

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
        self.tasks.append(
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
        return json.dumps({"ok": True, "task_guid": "task-1", "url": "https://example.com/task-1"})


def _assignment() -> dict[str, Any]:
    return {
        "assignment_id": "wa-1",
        "title": "整理会议结论",
        "state": "assigned",
        "assigner": {"display_name": "安排者", "feishu_open_id": "ou_assigner"},
        "recipients": [{"display_name": "接收者", "feishu_open_id": "ou_recipient"}],
        "original_request": "请整理会议结论。",
        "context": "会议已经结束。",
        "expected_outcome": "提交可评审方案。",
        "action_items": [{"description": "提交方案", "deadline": "2026-08-08"}],
        "delivery_records": [],
        "revision": 1,
    }


def _feedback_thread(card_id: str | None = None) -> dict[str, Any]:
    return {
        "thread_id": "feedback-1",
        "arrangement_id": "wa-1",
        "state": "open",
        "version": 1,
        "card_id": card_id,
        "entries": [],
    }


def _delivery(
    *,
    recipient_status: str = "pending",
    recipient_message_id: str | None = None,
    progress_status: str = "pending",
    progress_message_id: str | None = None,
    revision: int = 1,
) -> dict[str, Any]:
    return {
        "assignment_id": "wa-1",
        "assigner_open_id": "ou_assigner",
        "assigner_progress_message_id": progress_message_id,
        "progress_status": progress_status,
        "card_rendered_revision": 0,
        "recipients": [
            {
                "open_ids": ["ou_recipient"],
                "delivery_open_id": "ou_recipient" if recipient_status != "pending" else None,
                "message_id": recipient_message_id,
                "send_status": recipient_status,
                "read_at": None,
                "accepted_at": None,
            }
        ],
        "task_published_at": None,
        "revision": revision,
    }


def _claim(target: str, delivery: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "result": {
            "acquired": True,
            "claim_token": f"claim-{target}",
            "target": target,
            "delivery": delivery,
        },
    }


def _button_values(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action["value"]
        for element in card.get("elements", [])
        if isinstance(element, dict)
        for action in element.get("actions", [])
        if isinstance(action, dict) and isinstance(action.get("value"), dict)
    ]


def _import_assignment_module(
    name: str,
    memory: _MemoryStub,
    monkeypatch,
    *,
    feishu: _FeishuStub | None = None,
    task: _FeishuStub | None = None,
    session_id: str = "feishu-ou_assigner",
) -> Any:
    mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
    mcp_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(mcp_path).encode()).hexdigest()[:12]}"
    mcp_module = types.ModuleType(mcp_name)
    mcp_module.__dict__["CLIENT"] = memory
    monkeypatch.setitem(sys.modules, mcp_name, mcp_module)

    feishu = feishu or _FeishuStub()
    message_module = types.ModuleType("feishu_message")
    message_module.__dict__["feishu_message_send_card"] = feishu.feishu_message_send_card
    monkeypatch.setitem(sys.modules, "feishu_message", message_module)
    impl_module = types.ModuleType("_feishu_impl")
    impl_module.__dict__.update(
        {
            "send_card_impl": feishu.send_card_impl,
            "edit_card_impl": feishu.edit_card_impl,
            "send_message_impl": feishu.send_message_impl,
            "read_status_impl": feishu.read_status_impl,
        }
    )
    monkeypatch.setitem(sys.modules, "_feishu_impl", impl_module)
    task_module = types.ModuleType("feishu_task")
    task_module.__dict__["_feishu_task_create_once"] = (task or feishu).feishu_task_create
    monkeypatch.setitem(sys.modules, "feishu_task", task_module)

    runtime_context = importlib.import_module("psi_agent.session.runtime_context")
    monkeypatch.setattr(runtime_context, "get_session_id", lambda: session_id)
    for module_name in ("_assignment_tool_common", "_assignment_delivery", name):
        sys.modules.pop(module_name, None)
    return importlib.import_module(name)


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

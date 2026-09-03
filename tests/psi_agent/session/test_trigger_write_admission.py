"""件二A 写入准入 —— trigger 无变更不写历史 + 空 filter 语义收口.

两组判据(设计见 ``docs/superpowers/specs/2026-09-03-request-assembly-budget-design.md``
H 段第 5 节):

1. **无变更不写历史**:``fire=tool`` 的定时 trigger 每几分钟写 2 行,无论有没有
   实际变更。无变更的行不携带信息,只把历史推长、把每回合装配变贵。
2. **空 filter 不再放行一切**:``filter: {}`` 曾经等于「匹配一切」,raw 路
   (``raw_event`` + 空 ``raw_filter``)会绕过 normalized 路已经拒绝掉的收窄。

判据设计:正向(有变更照写)与负向(无变更不重建)成对出现——只验「不写」会把
「永远不写」也判绿。历史用**归档后不重建**验,比验行数增长更强。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import anyio
import pytest

from psi_agent.session.agent import SessionAgent
from psi_agent.session.ai_client import AiClient
from psi_agent.session.conversation import Conversation
from psi_agent.session.event_protocol import (
    EVENT_FEISHU_CHAT_MEMBER_ADDED,
    filter_matches,
    parse_event_envelope,
)
from psi_agent.session.tool_registry import FileEntry, ToolFunction, ToolRegistry
from psi_agent.session.trigger_registry import (
    MATCH_ALL,
    TriggerRegistry,
    tool_result_is_noop,
)

_DELIVERY_EVENT = "haitun.assignment.delivery_check"


async def _write_trigger(triggers_dir: Path, name: str, header_body: str) -> None:
    trig_dir = triggers_dir / name
    await anyio.Path(trig_dir).mkdir(parents=True)
    await anyio.Path(trig_dir / "TRIGGER.md").write_text(
        textwrap.dedent(header_body),
        encoding="utf-8",
    )


def _make_agent(tmp_path: Path, tools: ToolRegistry, registry: TriggerRegistry) -> SessionAgent:
    conversation = Conversation(path=tmp_path / "histories" / "s1.jsonl")
    return SessionAgent(
        ai_client=AiClient("http://nonexistent/v1"),
        conversation=conversation,
        tool_registry=tools,
        trigger_registry=registry,
        workspace_path=tmp_path,
    )


def _registry_with_tool(name: str, func: Any) -> ToolRegistry:
    tools = ToolRegistry()
    tools._files[f"{name}.py"] = FileEntry(
        file_hash="h",
        tools={name: ToolFunction.from_callable(func)},
        funcs={name: func},
        fresh=True,
    )
    return tools


def _envelope(event: str, payload: dict[str, Any], **extra: Any) -> Any:
    return parse_event_envelope({"schema_version": 1, "source": "haitun", "event": event, "payload": payload, **extra})


# --------------------------------------------------------------------------
# 1. 无变更不写历史
# --------------------------------------------------------------------------


def test_tool_result_is_noop_reads_counters() -> None:
    """``ok`` 且所有变更计数为 0 且无 errors → 无变更。"""
    assert tool_result_is_noop(json.dumps({"ok": True, "checked": 3, "read_advanced": 0, "card_updates": 0}))
    # 有变更
    assert not tool_result_is_noop(json.dumps({"ok": True, "checked": 3, "read_advanced": 1, "card_updates": 0}))
    assert not tool_result_is_noop(json.dumps({"ok": True, "checked": 3, "read_advanced": 0, "card_updates": 2}))
    # 有 errors → 值得留痕。刻意带上一个已识别的零计数, 否则会被「无计数键」那条
    # 分支提前判掉, 这条断言就吃不到 errors 判定的劲(变异复核实测过).
    assert not tool_result_is_noop(
        json.dumps({"ok": True, "checked": 1, "read_advanced": 0, "errors": [{"code": "x"}]})
    )
    # 失败不算无变更(同样带零计数, 让 ok 判定真正吃劲)
    assert not tool_result_is_noop(json.dumps({"ok": False, "read_advanced": 0, "card_updates": 0}))
    # 非 JSON / 非对象 / 无计数键 → 不敢判定无变更,照写
    assert not tool_result_is_noop("plain text result")
    assert not tool_result_is_noop(json.dumps([1, 2]))
    assert not tool_result_is_noop(json.dumps({"ok": True}))
    assert not tool_result_is_noop(json.dumps({"ok": True, "checked": 5}))


@pytest.mark.anyio
async def test_noop_trigger_does_not_rebuild_archived_history(tmp_path: Path) -> None:
    """负向判据(比验行数增长更强):把历史归档清空后,无变更的 trigger 不重建它。"""

    async def refresh(event_payload_json: str = "{}") -> str:
        return json.dumps({"ok": True, "checked": 2, "read_advanced": 0, "card_updates": 0, "errors": []})

    tools = _registry_with_tool("assignment_delivery_refresh", refresh)
    await _write_trigger(
        tmp_path / "triggers",
        "delivery",
        f"""\
        ---
        name: delivery
        event: {_DELIVERY_EVENT}
        source: haitun
        filter: {MATCH_ALL}
        fire: tool
        tool: assignment_delivery_refresh
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    agent = _make_agent(tmp_path, tools, registry)
    history = anyio.Path(str(tmp_path / "histories" / "s1.jsonl"))

    env = _envelope(_DELIVERY_EVENT, {"tick": "t1"}, idempotency_key="k1")
    async with agent._lock:
        assert await registry.dispatch(env, agent) == ["delivery"]

    # 归档:文件与内存历史一起清空,模拟运维把历史挪走。
    if await history.exists():
        await history.unlink()
    agent._conversation.messages.clear()

    env2 = _envelope(_DELIVERY_EVENT, {"tick": "t2"}, idempotency_key="k2")
    async with agent._lock:
        assert await registry.dispatch(env2, agent) == ["delivery"]

    assert not await history.exists(), "无变更的 trigger 重建了已归档的历史文件"
    assert agent._conversation.messages == []


@pytest.mark.anyio
async def test_changed_trigger_still_writes_two_rows(tmp_path: Path) -> None:
    """正向判据:有实际变更时照写 user + assistant 两行,功能不能被改没。"""

    async def refresh(event_payload_json: str = "{}") -> str:
        return json.dumps({"ok": True, "checked": 2, "read_advanced": 1, "card_updates": 0, "errors": []})

    tools = _registry_with_tool("assignment_delivery_refresh", refresh)
    await _write_trigger(
        tmp_path / "triggers",
        "delivery",
        f"""\
        ---
        name: delivery
        event: {_DELIVERY_EVENT}
        source: haitun
        filter: {MATCH_ALL}
        fire: tool
        tool: assignment_delivery_refresh
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    agent = _make_agent(tmp_path, tools, registry)
    history = anyio.Path(str(tmp_path / "histories" / "s1.jsonl"))

    env = _envelope(_DELIVERY_EVENT, {"tick": "t1"}, idempotency_key="k1")
    async with agent._lock:
        assert await registry.dispatch(env, agent) == ["delivery"]

    assert len(agent._conversation.messages) == 2
    assert agent._conversation.messages[0]["role"] == "user"
    assert agent._conversation.messages[1]["role"] == "assistant"
    assert await history.exists()
    lines = [ln for ln in (await history.read_text(encoding="utf-8")).splitlines() if ln.strip()]
    assert len(lines) == 2


@pytest.mark.anyio
async def test_tool_error_is_written_not_swallowed(tmp_path: Path) -> None:
    """工具报错必须留痕——不能被「无变更不写」顺手吞掉。

    注意断言的是「两行照写」而非错误正文:``visibility: silent`` 下 assistant 行
    历来只写 ``ok``(本次未改动这一点),错误正文只在日志里。
    """

    async def refresh(event_payload_json: str = "{}") -> str:
        raise RuntimeError("boom")

    tools = _registry_with_tool("assignment_delivery_refresh", refresh)
    await _write_trigger(
        tmp_path / "triggers",
        "delivery",
        f"""\
        ---
        name: delivery
        event: {_DELIVERY_EVENT}
        source: haitun
        filter: {MATCH_ALL}
        fire: tool
        tool: assignment_delivery_refresh
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    agent = _make_agent(tmp_path, tools, registry)

    env = _envelope(_DELIVERY_EVENT, {"tick": "t1"}, idempotency_key="k1")
    async with agent._lock:
        await registry.dispatch(env, agent)

    assert len(agent._conversation.messages) == 2
    assert [m["role"] for m in agent._conversation.messages] == ["user", "assistant"]
    history = anyio.Path(str(tmp_path / "histories" / "s1.jsonl"))
    assert await history.exists(), "工具报错被「无变更不写」吞掉了"


# --------------------------------------------------------------------------
# 2. 空 filter 语义收口
# --------------------------------------------------------------------------


def test_filter_matches_empty_filter_no_longer_matches_everything() -> None:
    """``all([])`` 曾让空 filter 放行一切;现在空 filter 不匹配。"""
    assert not filter_matches({"chat_id": "oc_1"}, {})
    assert not filter_matches({}, {})
    # 显式 MATCH_ALL 才放行
    assert filter_matches({"chat_id": "oc_1"}, MATCH_ALL)
    # 收窄仍然照常工作
    assert filter_matches({"chat_id": "oc_1"}, {"chat_id": "oc_1"})
    assert not filter_matches({"chat_id": "oc_1"}, {"chat_id": "oc_other"})


@pytest.mark.anyio
async def test_raw_path_cannot_bypass_narrowed_filter(tmp_path: Path) -> None:
    """实测发生过的那条路径:normalized 路按 chat_id 拒绝后,raw 路空 raw_filter 全放。"""
    await _write_trigger(
        tmp_path / "triggers",
        "gaoshuai-newtask",
        f"""\
        ---
        name: gaoshuai-newtask
        source: feishu
        event: {EVENT_FEISHU_CHAT_MEMBER_ADDED}
        filter:
          chat_id: oc_8838
        raw_event: im.message.receive_v1
        fire: prompt
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    other = parse_event_envelope(
        {
            "schema_version": 1,
            "source": "feishu",
            "event": EVENT_FEISHU_CHAT_MEMBER_ADDED,
            "payload": {"chat_id": "oc_someone_else"},
            "raw_event": "im.message.receive_v1",
            "raw_payload": {"chat_id": "oc_someone_else"},
        }
    )
    assert registry.match(other) == [], "raw 路绕过了 normalized 路的收窄"

    # 同一个 trigger 对本来该命中的会话仍然命中(不能把功能改没)。
    target = parse_event_envelope(
        {
            "schema_version": 1,
            "source": "feishu",
            "event": EVENT_FEISHU_CHAT_MEMBER_ADDED,
            "payload": {"chat_id": "oc_8838"},
        }
    )
    assert [t.name for t in registry.match(target)] == ["gaoshuai-newtask"]


@pytest.mark.anyio
async def test_empty_filter_trigger_never_fires(tmp_path: Path) -> None:
    """``filter: {}``(旧生产写法)在新语义下不再匹配任何事件。"""
    await _write_trigger(
        tmp_path / "triggers",
        "legacy-empty",
        f"""\
        ---
        name: legacy-empty
        event: {_DELIVERY_EVENT}
        source: haitun
        filter: {{}}
        fire: tool
        tool: noop
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    env = _envelope(_DELIVERY_EVENT, {"tick": "t1"})
    assert registry.match(env) == []


@pytest.mark.anyio
async def test_match_all_filter_fires_for_any_payload(tmp_path: Path) -> None:
    """显式 ``filter: {match: all}`` 是新的「放行一切」写法。"""
    await _write_trigger(
        tmp_path / "triggers",
        "explicit-all",
        f"""\
        ---
        name: explicit-all
        event: {_DELIVERY_EVENT}
        source: haitun
        filter: {MATCH_ALL}
        fire: tool
        tool: noop
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    assert [t.name for t in registry.match(_envelope(_DELIVERY_EVENT, {"tick": "t1"}))] == ["explicit-all"]
    assert [t.name for t in registry.match(_envelope(_DELIVERY_EVENT, {"anything": "else"}))] == ["explicit-all"]


@pytest.mark.anyio
async def test_raw_filter_inherits_nothing_and_requires_own_match_all(tmp_path: Path) -> None:
    """raw 路要放行一切也必须显式声明,不能靠留空。"""
    await _write_trigger(
        tmp_path / "triggers",
        "raw-explicit",
        f"""\
        ---
        name: raw-explicit
        source: feishu
        event: feishu.hr.user_created
        filter: {MATCH_ALL}
        raw_event: contact.user.created_v3
        raw_filter: {MATCH_ALL}
        fire: tool
        tool: noop
        visibility: silent
        ---
        """,
    )
    registry = await TriggerRegistry.load(tmp_path / "triggers")
    # normalized 路命中
    env = parse_event_envelope(
        {
            "schema_version": 1,
            "source": "feishu",
            "event": "feishu.hr.user_created",
            "payload": {"open_id": "ou_1"},
        }
    )
    assert [t.name for t in registry.match(env)] == ["raw-explicit"]
    # normalized event 不同,只能走 raw 路——显式 MATCH_ALL 下命中
    raw_only = parse_event_envelope(
        {
            "schema_version": 1,
            "source": "feishu",
            "event": "feishu.something.else",
            "payload": {},
            "raw_event": "contact.user.created_v3",
            "raw_payload": {"open_id": "ou_2"},
        }
    )
    assert [t.name for t in registry.match(raw_only)] == ["raw-explicit"]

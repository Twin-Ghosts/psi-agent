"""Fusion Memory envelope unwrapping: a server error must never arrive as a success.

Two layers had to be fixed together. ``_normalize_result`` turned a reply split across
content blocks into a *string* and derived ``ok`` purely from the transport's ``isError``,
so a tool that reported its own failure was announced as a success carrying unusable
data. ``result_object`` then returned ``None`` for that, and callers collapsed several
distinct causes into one generic "invalid record" message — which is what hid the real
rejection from an operator staring at a card that would not work.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load(name: str) -> Any:
    """Load a tools/ helper under a unique name so tests never share module state."""
    path = TOOLS_DIR / f"{name}.py"
    module_name = f"{name}_{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    original = list(sys.path)
    sys.path.insert(0, str(TOOLS_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original
    return module


@dataclass
class _Block:
    text: str


@dataclass
class _Result:
    content: list[_Block]
    isError: bool = False  # noqa: N815 — mirrors the MCP SDK field name
    structuredContent: dict[str, Any] | None = None  # noqa: N815


def _blocks(*texts: str) -> list[_Block]:
    return [_Block(text) for text in texts]


def test_reply_split_across_blocks_is_still_parsed_as_one_document() -> None:
    """跨块的回复仍是一份 JSON: 只在「恰好一块」时才解析会把 payload 退化成字符串。"""
    mcp = _load("_fusion_memory_mcp")
    normalized = mcp._normalize_result(_Result(_blocks('{"ok": true,', '"result": {"state": "assigned"}}')))

    assert normalized["ok"] is True
    assert normalized["result"] == {"ok": True, "result": {"state": "assigned"}}


def test_inner_failure_is_reported_as_an_outer_failure() -> None:
    """工具自报失败不能被当成成功: 每个调用方都先看外层 ok, 内层错误会被整条丢掉。"""
    mcp = _load("_fusion_memory_mcp")
    error = {"code": "invalid_request", "message": "revision conflict"}
    normalized = mcp._normalize_result(_Result(_blocks(f'{{"ok": false, "error": {error!r}}}'.replace("'", '"'))))

    assert normalized["ok"] is False
    assert normalized["error"] == error
    # 原始 payload 要留着, 否则排障时看不到服务端还说了什么。
    assert normalized["result"] == {"ok": False, "error": error}


def test_non_json_text_still_comes_back_as_text() -> None:
    """不是 JSON 的回复照原样带回, 不能因为解析失败就报错或丢内容。"""
    mcp = _load("_fusion_memory_mcp")
    normalized = mcp._normalize_result(_Result(_blocks("upstream is down")))

    assert normalized == {"ok": True, "result": "upstream is down"}


def test_transport_error_stays_an_error() -> None:
    mcp = _load("_fusion_memory_mcp")
    normalized = mcp._normalize_result(_Result(_blocks("boom"), isError=True))

    assert normalized["ok"] is False


def test_unwrap_reason_names_a_degraded_payload() -> None:
    """字符串 payload 要说清它是字符串并带上原文, 否则和「服务端拒了」分不开。"""
    common = _load("_assignment_tool_common")
    obj, reason = common.result_object_or_reason({"ok": True, "result": "not an object"})

    assert obj is None
    assert "str" in reason
    assert "not an object" in reason


def test_unwrap_reason_carries_the_servers_error() -> None:
    """内层错误必须原样冒出来: 这一句就是操作者唯一能看到的真因。"""
    common = _load("_assignment_tool_common")
    obj, reason = common.result_object_or_reason(
        {"ok": True, "result": {"ok": False, "error": {"code": "forbidden", "message": "not a participant"}}}
    )

    assert obj is None
    assert "forbidden" in reason
    assert "not a participant" in reason


def test_unwrap_distinguishes_missing_payload_from_missing_record() -> None:
    common = _load("_assignment_tool_common")

    _, no_payload = common.result_object_or_reason({"ok": True})
    _, no_record = common.result_object_or_reason({"ok": True, "result": {"ok": True, "result": None}})

    assert no_payload != no_record
    assert "no payload" in no_payload
    assert "no record" in no_record


def test_unwrap_peels_a_double_envelope_and_a_bare_object() -> None:
    """两种形状都得支持: 带内层信封的, 和直接就是记录的。"""
    common = _load("_assignment_tool_common")
    record = {"assignment_id": "wa_1", "state": "assigned"}

    assert common.result_object_or_reason({"ok": True, "result": {"ok": True, "result": record}}) == (record, None)
    assert common.result_object_or_reason({"ok": True, "result": record}) == (record, None)


def test_result_object_still_returns_just_the_object() -> None:
    """老签名不能变: 仓里还有一批调用方只要对象。"""
    common = _load("_assignment_tool_common")
    record = {"assignment_id": "wa_1"}

    assert common.result_object({"ok": True, "result": {"ok": True, "result": record}}) == record
    assert common.result_object({"ok": True, "result": "degraded"}) is None

"""Cap on a single tool result, at the write site and on the wire.

A live session was wedged permanently by two ``feishu_api`` rows totalling
3,068,236 characters — 90.5% of a 3,389,280-character request the provider
rejected outright.  Compaction could not rescue it: the compaction signal only
arrives after a stream completes, and the request never got that far, so every
retry rebuilt the same payload and a restart changed nothing.

Two layers are pinned here: ``agent.py`` caps what is written, and
``project_history_for_wire`` caps what goes out — the second matters because oversized
rows written before the cap existed are already on disk.
"""

from __future__ import annotations

import pytest

from psi_agent.session.history_display import (
    MAX_TOOL_RESULT_CHARS,
    project_history_for_wire,
    truncate_tool_result,
)


def test_short_result_passes_through_untouched() -> None:
    text = '{"ok": true, "items": []}'
    assert truncate_tool_result(text) is text


def test_result_at_the_limit_is_not_truncated() -> None:
    text = "x" * MAX_TOOL_RESULT_CHARS
    assert truncate_tool_result(text) == text


def test_oversized_result_keeps_the_head_and_says_so() -> None:
    text = "y" * (MAX_TOOL_RESULT_CHARS + 1)
    out = truncate_tool_result(text)
    assert out.startswith("y" * MAX_TOOL_RESULT_CHARS)
    assert len(out) < len(text) + 200
    # The original length must survive into the notice: without it the model
    # cannot tell a 21K result from a 2.3M one, and so cannot judge how much
    # narrower its next query needs to be.
    assert str(len(text)) in out
    assert str(MAX_TOOL_RESULT_CHARS) in out


def test_field_sized_result_collapses() -> None:
    """The measured offender: 2,343,193 characters in one row."""
    out = truncate_tool_result("z" * 2_343_193)
    assert len(out) <= MAX_TOOL_RESULT_CHARS + 200
    assert "2343193" in out


def test_custom_limit_is_honoured() -> None:
    out = truncate_tool_result("a" * 50, limit=10)
    assert out.startswith("a" * 10)
    assert "a" * 11 not in out


@pytest.mark.parametrize("role", ["user", "assistant", "system"])
def test_only_tool_rows_are_truncated_on_the_wire(role: str) -> None:
    """A long user message is the user's own text; truncating it would edit them."""
    long_text = "q" * (MAX_TOOL_RESULT_CHARS + 500)
    out = project_history_for_wire([{"role": role, "content": long_text}])
    assert out[0]["content"] == long_text


def test_wire_projection_truncates_oversized_tool_row() -> None:
    long_text = "w" * (MAX_TOOL_RESULT_CHARS + 500)
    out = project_history_for_wire(
        [
            {"role": "system", "content": "SYS"},
            {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": long_text},
        ]
    )
    tool_row = next(m for m in out if m["role"] == "tool")
    assert len(tool_row["content"]) < len(long_text)
    assert tool_row["content"].startswith("w" * MAX_TOOL_RESULT_CHARS)
    # Identity fields must survive: a tool result whose ``tool_call_id`` no
    # longer matches its call is rejected by the provider just as hard as an
    # oversized one.
    assert tool_row["tool_call_id"] == "c1"
    assert tool_row["name"] == "read"


def test_wire_projection_leaves_non_string_tool_content_alone() -> None:
    """Only ``str`` is capped; a structured payload has no meaningful prefix."""
    payload = {"items": list(range(10))}
    out = project_history_for_wire([{"role": "tool", "tool_call_id": "c1", "content": payload}])
    assert out[0]["content"] == payload


def test_wire_projection_is_idempotent() -> None:
    """Projecting an already-truncated row must not stack a second notice."""
    long_text = "e" * (MAX_TOOL_RESULT_CHARS + 500)
    once = project_history_for_wire([{"role": "tool", "tool_call_id": "c1", "content": long_text}])
    twice = project_history_for_wire([{"role": "tool", "tool_call_id": "c1", "content": once[0]["content"]}])
    assert twice[0]["content"] == once[0]["content"]

"""Per-turn volatile context, delivered at the tail of the request.

The system prompt is built once per Session and then reused verbatim, which
froze every "now" it contained: a Session opened on the 24th kept reporting the
24th for days, under whatever ``Time zone`` label happened to be correct at
build time. Re-rendering the prompt would fix the clock at the price of a full
workspace rescan per turn, and of a request prefix that never repeats — which
rules out prompt caching however it is configured. So the volatile block rides
on the turn's own user message instead. These tests pin that contract — where
the block lands, that it never rewrites a stored row, and the failure modes
that must degrade to "no block" rather than to a broken turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from psi_agent.session.conversation import Conversation
from psi_agent.session.history_display import TURN_CONTEXT_KEY, messages_for_ai
from psi_agent.session.system_prompt import SystemPrompt


@pytest.mark.anyio
async def test_turn_context_rendered_from_builder() -> None:
    async def turn_context_builder() -> str:
        return "Date: 2026-07-29"

    sp = SystemPrompt(turn_context_fn=turn_context_builder)

    assert await sp.turn_context() == "Date: 2026-07-29"


@pytest.mark.anyio
async def test_no_builder_yields_no_block() -> None:
    assert await SystemPrompt().turn_context() == ""


@pytest.mark.anyio
async def test_builder_failure_degrades_to_no_block() -> None:
    """Losing a clock line beats losing the turn."""

    async def turn_context_builder() -> str:
        raise RuntimeError("workspace scan blew up")

    sp = SystemPrompt(turn_context_fn=turn_context_builder)

    assert await sp.turn_context() == ""


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["", "   ", None, 42])
async def test_unusable_builder_result_yields_no_block(bad: object) -> None:
    async def turn_context_builder() -> object:
        return bad

    sp = SystemPrompt(turn_context_fn=turn_context_builder)

    assert await sp.turn_context() == ""


@pytest.mark.anyio
async def test_ensure_does_not_touch_prompt_on_a_later_turn() -> None:
    """The prompt is the front of the request; nothing may rewrite it per turn."""

    async def builder() -> str:
        return "REBUILT"

    async def turn_context_builder() -> str:
        return "Date: 2026-07-29"

    sp = SystemPrompt(builder=builder, turn_context_fn=turn_context_builder)
    conv = Conversation(
        messages=[
            {"role": "system", "content": "STABLE PROMPT"},
            {"role": "user", "content": "hi"},
        ]
    )

    await sp.ensure(conv)

    assert conv.messages[0] == {"role": "system", "content": "STABLE PROMPT"}


@pytest.mark.anyio
async def test_checker_still_rebuilds_whole_prompt() -> None:
    async def builder() -> str:
        return "REBUILT"

    async def checker() -> bool:
        return True

    sp = SystemPrompt(builder=builder, checker=checker)
    conv = Conversation(
        messages=[
            {"role": "system", "content": "OLD"},
            {"role": "user", "content": "hi"},
        ]
    )

    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "REBUILT"


@pytest.mark.anyio
async def test_builder_loaded_from_workspace(tmp_path: Path) -> None:
    systems_dir = tmp_path / "systems"
    await anyio.Path(str(systems_dir)).mkdir()
    await anyio.Path(str(systems_dir / "system.py")).write_text(
        "async def system_prompt_builder() -> str:\n"
        '    return "built"\n'
        "\n"
        "async def turn_context_builder() -> str:\n"
        '    return "Date: 2026-07-29"\n'
    )

    sp = await SystemPrompt.from_workspace(tmp_path, "test_session")

    assert await sp.turn_context() == "Date: 2026-07-29"


@pytest.mark.anyio
async def test_workspace_without_builder_yields_no_block(tmp_path: Path) -> None:
    systems_dir = tmp_path / "systems"
    await anyio.Path(str(systems_dir)).mkdir()
    await anyio.Path(str(systems_dir / "system.py")).write_text(
        'async def system_prompt_builder() -> str:\n    return "built"\n'
    )

    sp = await SystemPrompt.from_workspace(tmp_path, "test_session")

    assert await sp.turn_context() == ""


# -- wire projection -----------------------------------------------------------


def test_block_folds_in_after_the_message_body() -> None:
    """After, not before: a prefix would shift every byte of the turn."""
    projected = messages_for_ai(
        [
            {"role": "system", "content": "PROMPT"},
            {"role": "user", "content": "what time is it", TURN_CONTEXT_KEY: "Date: 2026-07-29"},
        ]
    )

    assert projected[1] == {"role": "user", "content": "what time is it\n\nDate: 2026-07-29"}


def test_block_never_reaches_the_stored_row() -> None:
    """Storing it out-of-band is the whole point — the row must stay as written."""
    stored: list[dict[str, Any]] = [
        {"role": "system", "content": "PROMPT"},
        {"role": "user", "content": "hi", TURN_CONTEXT_KEY: "Date: 2026-07-29"},
    ]

    messages_for_ai(stored)

    assert stored[1] == {"role": "user", "content": "hi", TURN_CONTEXT_KEY: "Date: 2026-07-29"}


def test_earlier_turns_project_byte_identically() -> None:
    """Only the newest turn carries a block, so the request prefix is stable."""
    first = messages_for_ai(
        [
            {"role": "system", "content": "PROMPT"},
            {"role": "user", "content": "turn one", TURN_CONTEXT_KEY: "Date: 2026-07-28"},
        ]
    )
    second = messages_for_ai(
        [
            {"role": "system", "content": "PROMPT"},
            {"role": "user", "content": "turn one", TURN_CONTEXT_KEY: "Date: 2026-07-28"},
            {"role": "assistant", "content": "reply one"},
            {"role": "user", "content": "turn two", TURN_CONTEXT_KEY: "Date: 2026-07-29"},
        ]
    )

    assert second[: len(first)] == first


def test_empty_body_projects_to_the_block_alone() -> None:
    projected = messages_for_ai([{"role": "user", "content": "", TURN_CONTEXT_KEY: "Date: 2026-07-29"}])

    assert projected[0]["content"] == "Date: 2026-07-29"


def test_multimodal_content_is_left_intact() -> None:
    """No single place to append to; dropping the block beats corrupting blocks."""
    blocks = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "u"}}]
    projected = messages_for_ai([{"role": "user", "content": blocks, TURN_CONTEXT_KEY: "Date: 2026-07-29"}])

    assert projected[0]["content"] == blocks
    assert TURN_CONTEXT_KEY not in projected[0]


def test_blank_block_is_not_folded_in() -> None:
    projected = messages_for_ai([{"role": "user", "content": "hi", TURN_CONTEXT_KEY: "   "}])

    assert projected[0] == {"role": "user", "content": "hi"}


def test_block_folds_in_after_compaction_too() -> None:
    """Compaction rebuilds the list from a different branch of the projection."""
    projected = messages_for_ai(
        [
            {"role": "system", "content": "PROMPT"},
            {"role": "user", "content": "old"},
            {"role": "compacted", "content": "summary"},
            {"role": "user", "content": "new", TURN_CONTEXT_KEY: "Date: 2026-07-29"},
        ]
    )

    assert projected[-1] == {"role": "user", "content": "new\n\nDate: 2026-07-29"}

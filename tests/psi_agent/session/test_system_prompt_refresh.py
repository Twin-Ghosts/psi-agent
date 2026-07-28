"""Per-turn refresh of the system prompt's volatile tail.

The prompt is built once per Session and then reused verbatim, which froze
every "now" it contained: a Session opened on the 24th kept reporting the
24th for days, under whatever ``Time zone`` label happened to be correct at
build time. These tests pin the refresh contract — including the cases where
refreshing must *not* happen, since a truncated system prompt is far worse
than a stale clock.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from psi_agent.session.conversation import Conversation
from psi_agent.session.system_prompt import SystemPrompt

BOUNDARY = "\n<!-- BOUNDARY -->\n"


def _conv(system: str | None, *rest: dict[str, object]) -> Conversation:
    messages: list[dict[str, object]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.extend(rest)
    return Conversation(messages=messages)


@pytest.mark.anyio
async def test_refresh_replaces_tail_and_keeps_prefix() -> None:
    async def refresher(current: str) -> str:
        head, sep, _ = current.partition(BOUNDARY)
        return head + sep + "Date: 2026-07-28"

    sp = SystemPrompt(refresher=refresher)
    conv = _conv("STABLE" + BOUNDARY + "Date: 2026-07-24", {"role": "user", "content": "hi"})

    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "STABLE" + BOUNDARY + "Date: 2026-07-28"
    # The cached prefix must survive byte-for-byte, else prompt caching misses.
    assert conv.messages[0]["content"].startswith("STABLE" + BOUNDARY)
    assert conv.messages[1] == {"role": "user", "content": "hi"}


@pytest.mark.anyio
async def test_first_turn_builds_and_does_not_refresh() -> None:
    calls: list[str] = []

    async def builder() -> str:
        calls.append("builder")
        return "FRESH" + BOUNDARY + "now"

    async def refresher(current: str) -> str:
        calls.append("refresher")
        return current

    sp = SystemPrompt(builder=builder, refresher=refresher)
    conv = _conv(None)

    await sp.ensure(conv)

    assert calls == ["builder"]
    assert conv.messages[0]["content"] == "FRESH" + BOUNDARY + "now"


@pytest.mark.anyio
async def test_full_rebuild_wins_over_refresh() -> None:
    """A checker-driven rebuild already produces a current tail; refreshing
    on top of it would be wasted work."""
    calls: list[str] = []

    async def builder() -> str:
        calls.append("builder")
        return "REBUILT" + BOUNDARY + "now"

    async def checker() -> bool:
        return True

    async def refresher(current: str) -> str:
        calls.append("refresher")
        return current

    sp = SystemPrompt(builder=builder, checker=checker, refresher=refresher)
    conv = _conv("OLD" + BOUNDARY + "stale", {"role": "user", "content": "hi"})

    await sp.ensure(conv)

    assert calls == ["builder"]
    assert conv.messages[0]["content"] == "REBUILT" + BOUNDARY + "now"


@pytest.mark.anyio
async def test_no_refresher_leaves_prompt_untouched() -> None:
    sp = SystemPrompt()
    conv = _conv("AS IS" + BOUNDARY + "stale", {"role": "user", "content": "hi"})

    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "AS IS" + BOUNDARY + "stale"


@pytest.mark.anyio
async def test_refresher_failure_keeps_previous_prompt() -> None:
    async def refresher(current: str) -> str:
        raise RuntimeError("workspace scan blew up")

    sp = SystemPrompt(refresher=refresher)
    conv = _conv("KEEP" + BOUNDARY + "stale", {"role": "user", "content": "hi"})

    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "KEEP" + BOUNDARY + "stale"


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["", None, 42])
async def test_refresher_returning_unusable_value_is_ignored(bad: object) -> None:
    """An empty or non-string result would blank the system prompt."""

    async def refresher(current: str) -> object:
        return bad

    sp = SystemPrompt(refresher=refresher)
    conv = _conv("KEEP" + BOUNDARY + "stale", {"role": "user", "content": "hi"})

    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "KEEP" + BOUNDARY + "stale"


@pytest.mark.anyio
async def test_refresh_skipped_when_head_is_not_system() -> None:
    """Compaction can leave a history whose first message is not the prompt."""
    called = False

    async def refresher(current: str) -> str:
        nonlocal called
        called = True
        return "REPLACED"

    sp = SystemPrompt(refresher=refresher)
    conv = Conversation(messages=[{"role": "user", "content": "no system here"}])

    await sp.ensure(conv)

    assert not called
    assert conv.messages[0] == {"role": "user", "content": "no system here"}


@pytest.mark.anyio
async def test_refresher_loaded_from_workspace(tmp_path: Path) -> None:
    systems_dir = tmp_path / "systems"
    await anyio.Path(str(systems_dir)).mkdir()
    await anyio.Path(str(systems_dir / "system.py")).write_text(
        "async def system_prompt_builder() -> str:\n"
        '    return "built"\n'
        "\n"
        "async def system_prompt_dynamic_suffix(current: str) -> str:\n"
        '    return current + " REFRESHED"\n'
    )

    sp = await SystemPrompt.from_workspace(tmp_path, "test_session")
    conv = _conv("prompt", {"role": "user", "content": "hi"})
    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "prompt REFRESHED"


@pytest.mark.anyio
async def test_workspace_without_refresher_is_a_noop(tmp_path: Path) -> None:
    systems_dir = tmp_path / "systems"
    await anyio.Path(str(systems_dir)).mkdir()
    await anyio.Path(str(systems_dir / "system.py")).write_text(
        'async def system_prompt_builder() -> str:\n    return "built"\n'
    )

    sp = await SystemPrompt.from_workspace(tmp_path, "test_session")
    conv = _conv("prompt", {"role": "user", "content": "hi"})
    await sp.ensure(conv)

    assert conv.messages[0]["content"] == "prompt"

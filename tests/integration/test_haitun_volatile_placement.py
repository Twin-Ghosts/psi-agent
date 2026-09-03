"""profile / advice / policy belong at the request tail, not in the prompt.

A temporary production change (M1, 2026-09-02) spliced these three blocks into
the system prompt behind a ``<!-- HAITUN_CACHE_BOUNDARY -->`` marker. That
revived a design this repo had already argued down and deleted (AGENTS.md 坑
19): the system prompt is ``messages[0]``, so a block that changes per turn
sits *ahead of the entire history* however late in the prompt it appears, and
every cached turn behind it is invalidated. Measured on production: 181218
stable prefix chars, 880 changing chars behind the marker.

``turn_context`` is the mechanism that was already in place for exactly this —
stored out-of-band on the turn's own user message and folded in only when
projecting for the AI, so stored history rows stay byte-identical.

These tests pin the placement for both agent packs, and that the marker is
gone rather than merely unused.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

from psi_agent.session.history_display import TURN_CONTEXT_KEY, project_history_for_wire

REPO_ROOT = Path(__file__).parents[2]
PACKS = ("feishu", "desktop")

PROFILE_HEADING = "## 当前知识点学习画像"
POLICY_HEADING = "## 强制监督规则"
LEARNING_MESSAGE = "深入讲 Python 原理"


def _load_pack_system(pack: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    workspace = REPO_ROOT / "agents" / pack
    monkeypatch.syspath_prepend(str(workspace / "systems"))
    monkeypatch.syspath_prepend(str(workspace / "tools"))
    spec = importlib.util.spec_from_file_location(f"volatile_{pack}_system", workspace / "systems" / "system.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _learning_message(**extra: Any) -> dict[str, Any]:
    return {"role": "user", "content": LEARNING_MESSAGE, "user_id": "placement-probe", **extra}


# Shaped to the real advice schema: ``render_advice_prompt`` returns "" unless
# the source is usable and the turn is classified as learning.
ADVICE = {
    "diagnostics": {"source": "live", "evidence": []},
    "classification": {"is_learning": True, "domain": "programming", "topic": "先把执行模型讲清楚"},
}
ADVICE_MARKER = "先把执行模型讲清楚"


# -- the prompt must not carry volatile text ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_prompt_carries_no_profile_or_policy(pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_pack_system(pack, monkeypatch)

    prompt = await module.system_prompt_builder(_learning_message(), workspace_raw=str(tmp_path))

    assert PROFILE_HEADING not in prompt
    assert POLICY_HEADING not in prompt


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_prompt_carries_no_supervisor_advice(pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_pack_system(pack, monkeypatch)

    prompt = await module.system_prompt_builder(
        _learning_message(supervisor_advice=ADVICE), workspace_raw=str(tmp_path)
    )

    assert ADVICE_MARKER not in prompt


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_prompt_is_byte_identical_whatever_the_turn_carries(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prefix is the asset being protected: same Session, same prompt bytes."""
    module = _load_pack_system(pack, monkeypatch)

    plain = await module.system_prompt_builder({"role": "user", "content": "hi"}, workspace_raw=str(tmp_path))
    learning = await module.system_prompt_builder(_learning_message(), workspace_raw=str(tmp_path))
    advised = await module.system_prompt_builder(
        _learning_message(supervisor_advice=ADVICE), workspace_raw=str(tmp_path)
    )

    assert plain == learning == advised


# -- the turn context must carry it instead -------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_turn_context_carries_profile_and_policy(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_pack_system(pack, monkeypatch)

    block = await module.turn_context_builder(_learning_message(), workspace_raw=str(tmp_path))

    assert PROFILE_HEADING in block
    assert POLICY_HEADING in block


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_turn_context_carries_supervisor_advice(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_pack_system(pack, monkeypatch)

    block = await module.turn_context_builder(_learning_message(supervisor_advice=ADVICE), workspace_raw=str(tmp_path))

    assert ADVICE_MARKER in block


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_turn_context_still_carries_the_clock(pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing volatile payload must survive the move."""
    module = _load_pack_system(pack, monkeypatch)

    block = await module.turn_context_builder(_learning_message(), workspace_raw=str(tmp_path))

    assert "Current Date & Time" in block


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_turn_context_without_a_message_is_still_usable(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Schedule turns call in with no user message; the clock must still ship."""
    module = _load_pack_system(pack, monkeypatch)

    block = await module.turn_context_builder(None, workspace_raw=str(tmp_path))

    assert "Current Date & Time" in block
    assert PROFILE_HEADING not in block


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_a_plain_turn_keeps_its_profile_block_out_of_the_prompt(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any non-blank text opens a topic — pre-existing behaviour, unchanged here.

    What this card changes is only *where* the resulting block is delivered, so
    the invariant to pin is the placement, not whether a short message qualifies.
    """
    module = _load_pack_system(pack, monkeypatch)
    plain = {"role": "user", "content": "hi", "user_id": "placement-probe"}

    block = await module.turn_context_builder(plain, workspace_raw=str(tmp_path))
    prompt = await module.system_prompt_builder(plain, workspace_raw=str(tmp_path))

    assert PROFILE_HEADING in block
    assert PROFILE_HEADING not in prompt


# -- stored history rows stay byte-identical ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_earlier_history_projects_identically_as_the_profile_moves(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the move: turn two's volatile text cannot disturb turn one.

    Runs the real pack builders and then the real kernel projection, so this
    covers the whole path rather than the projection alone.
    """
    module = _load_pack_system(pack, monkeypatch)

    prompt = await module.system_prompt_builder(_learning_message(), workspace_raw=str(tmp_path))
    first_block = await module.turn_context_builder(_learning_message(), workspace_raw=str(tmp_path))
    second_block = await module.turn_context_builder(
        _learning_message(supervisor_advice=ADVICE), workspace_raw=str(tmp_path)
    )

    history: list[dict[str, Any]] = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "turn one", TURN_CONTEXT_KEY: first_block},
    ]
    first_projection = project_history_for_wire(history)
    grown = [
        *history,
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "turn two", TURN_CONTEXT_KEY: second_block},
    ]

    second_projection = project_history_for_wire(grown)

    assert second_projection[: len(first_projection)] == first_projection
    assert ADVICE_MARKER in second_projection[-1]["content"]


@pytest.mark.anyio
@pytest.mark.parametrize("pack", PACKS)
async def test_volatile_text_never_reaches_the_stored_row(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored out-of-band is what keeps the row stable on disk as well as on the wire."""
    module = _load_pack_system(pack, monkeypatch)
    block = await module.turn_context_builder(_learning_message(), workspace_raw=str(tmp_path))
    stored: list[dict[str, Any]] = [{"role": "user", "content": "hi", TURN_CONTEXT_KEY: block}]

    project_history_for_wire(stored)

    assert stored[0] == {"role": "user", "content": "hi", TURN_CONTEXT_KEY: block}


# -- the boundary marker is gone, not merely unused ----------------------------


@pytest.mark.parametrize("pack", PACKS)
def test_boundary_constant_is_absent_from_the_source(pack: str) -> None:
    """Matched loosely on purpose: a split or concatenated literal still counts.

    A previous rename in this repo reported "0 remaining" from a regex that only
    matched the whole path in one piece, while ten segmented occurrences stayed
    behind. So this looks for the distinctive stem with any non-word noise
    allowed between the parts.
    """
    source = (REPO_ROOT / "agents" / pack / "systems" / "system.py").read_text(encoding="utf-8")

    assert not re.search(r"HAITUN\W*_?\W*CACHE\W*_?\W*BOUNDARY", source)
    assert not re.search(r"CACHE\W*_?\W*BOUNDARY", source)
    assert "spliced_at_boundary" not in source

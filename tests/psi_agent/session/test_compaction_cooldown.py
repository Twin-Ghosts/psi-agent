"""Cooldown gate on repeated compactions (``SessionAgent._maybe_compact``).

The compaction signal only reports that ``prompt_tokens`` exceeded the
threshold.  Compaction cannot shrink the system prompt, so when the prompt alone
is a large share of the threshold the signal re-fires every turn.  These tests
pin the gate that stops the session from re-summarizing back to back.
"""

from __future__ import annotations

from typing import Any

import pytest

from psi_agent.session.agent import COMPACTION_COOLDOWN_FRACTION, SessionAgent
from psi_agent.session.ai_client import AiClient
from psi_agent.session.conversation import Conversation
from psi_agent.session.system_prompt import SystemPrompt

THRESHOLD = 100_000
REQUIRED = int(THRESHOLD * COMPACTION_COOLDOWN_FRACTION)


def _agent(calls: list[list[dict[str, Any]]]) -> SessionAgent:
    async def compaction_fn(history: list[dict[str, Any]], complete_fn: Any) -> str:
        calls.append(list(history))
        return "SUMMARY"

    return SessionAgent(
        ai_client=AiClient(ai_socket="http://127.0.0.1:1"),
        conversation=Conversation(
            messages=[
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "a"},
            ]
        ),
        system_prompt=SystemPrompt(builder=lambda: "SYS", compaction_fn=compaction_fn),
    )


def test_gate_open_on_first_compaction() -> None:
    agent = _agent([])
    assert agent._compaction_cooldown_elapsed(THRESHOLD + 1, THRESHOLD) is True


def test_gate_blocks_when_growth_below_required() -> None:
    agent = _agent([])
    agent._tokens_at_last_compaction = THRESHOLD
    assert agent._compaction_cooldown_elapsed(THRESHOLD + REQUIRED - 1, THRESHOLD) is False


def test_gate_opens_once_growth_reaches_required() -> None:
    agent = _agent([])
    agent._tokens_at_last_compaction = THRESHOLD
    assert agent._compaction_cooldown_elapsed(THRESHOLD + REQUIRED, THRESHOLD) is True


@pytest.mark.parametrize(("tokens", "threshold"), [(0, THRESHOLD), (THRESHOLD, 0), (0, 0)])
def test_gate_fails_open_without_usable_numbers(tokens: int, threshold: int) -> None:
    """An older AI layer omits the numbers; behaviour must not silently change."""
    agent = _agent([])
    agent._tokens_at_last_compaction = THRESHOLD
    assert agent._compaction_cooldown_elapsed(tokens, threshold) is True


@pytest.mark.anyio
async def test_second_signal_skipped_then_allowed_after_growth() -> None:
    calls: list[list[dict[str, Any]]] = []
    agent = _agent(calls)

    await agent._maybe_compact(THRESHOLD + 1, THRESHOLD)
    assert len(calls) == 1
    assert agent._conversation.messages[-1]["role"] == "compacted"

    # Same turn size again -> gate holds, no second summary appended.
    await agent._maybe_compact(THRESHOLD + 2, THRESHOLD)
    assert len(calls) == 1
    assert sum(1 for m in agent._conversation.messages if m["role"] == "compacted") == 1

    # Enough new context accrued -> compaction runs again.
    await agent._maybe_compact(THRESHOLD + 1 + REQUIRED, THRESHOLD)
    assert len(calls) == 2
    assert sum(1 for m in agent._conversation.messages if m["role"] == "compacted") == 2


@pytest.mark.anyio
async def test_failed_compaction_does_not_arm_cooldown() -> None:
    """A failure shrank nothing, so the next signal must still get through."""

    async def failing_fn(history: list[dict[str, Any]], complete_fn: Any) -> str:
        raise RuntimeError("boom")

    agent = SessionAgent(
        ai_client=AiClient(ai_socket="http://127.0.0.1:1"),
        conversation=Conversation(messages=[{"role": "system", "content": "SYS"}]),
        system_prompt=SystemPrompt(builder=lambda: "SYS", compaction_fn=failing_fn),
    )

    await agent._maybe_compact(THRESHOLD + 1, THRESHOLD)
    assert agent._tokens_at_last_compaction is None
    assert agent._compaction_cooldown_elapsed(THRESHOLD + 2, THRESHOLD) is True

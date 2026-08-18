from datetime import UTC, datetime

import anyio
import pytest

from psi_agent.memory.models import CompletedTurnInput, SourceMessageInput
from psi_agent.memory.outbox import DurableTurnOutbox


def make_turn() -> CompletedTurnInput:
    completed = datetime(2026, 8, 18, 1, 2, 3, tzinfo=UTC)
    return CompletedTurnInput(
        turn_id="dolphin:session-a:0",
        project_id="project-a",
        conversation_id="session-a",
        source_name="dolphin-agent",
        source_session_id="session-a",
        source_turn_index=None,
        user_message=SourceMessageInput("dolphin:session-a:0:user", "hello", completed, None),
        assistant_message=SourceMessageInput("dolphin:session-a:0:assistant", "hi", completed, None),
        completed_at=completed,
    )


@pytest.mark.anyio
async def test_enqueue_persists_before_transport(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")

    await outbox.enqueue(make_turn(), "dolphin:session-a:0")

    items = await outbox.peek()
    assert len(items) == 1
    assert items[0].idempotency_key == "dolphin:session-a:0"
    assert items[0].turn.source_turn_index == 0
    assert await anyio.Path(tmp_path / "outbox.jsonl").is_file()


@pytest.mark.anyio
async def test_mark_committed_hides_pending_but_keeps_record(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")
    await outbox.enqueue(make_turn(), "dolphin:session-a:0")

    await outbox.mark_committed(("dolphin:session-a:0",), "receipt-1")

    assert await outbox.peek() == ()
    raw = await anyio.Path(tmp_path / "outbox.jsonl").read_text()
    assert '"state":"committed"' in raw
    assert '"receipt_id":"receipt-1"' in raw

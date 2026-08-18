import pytest

from psi_agent.memory.connector import MemoryTurnConnector
from psi_agent.memory.mcp_client import MemoryMcpError
from psi_agent.memory.models import CompletedTurnInput
from psi_agent.memory.outbox import DurableTurnOutbox


def hook_messages(user_content: object = "hello", assistant_content: object = "hi") -> tuple[dict, dict]:
    return {"role": "user", "content": user_content}, {"role": "assistant", "content": assistant_content}


class FakeClient:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, tuple[CompletedTurnInput, ...]]] = []

    async def add_batch(self, idempotency_key: str, turns: tuple[CompletedTurnInput, ...]) -> dict:
        self.calls.append((idempotency_key, turns))
        if self.error is not None:
            raise self.error
        return {"receipt_id": "receipt-1", "items": [{"turn_id": turn.turn_id} for turn in turns]}


@pytest.mark.anyio
async def test_unavailable_send_leaves_item_pending(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")
    client = FakeClient(error=ConnectionError("offline"))
    connector = MemoryTurnConnector(
        outbox,
        client,
        project_id="project-a",
        source_name="dolphin-agent",
        session_id="session-a",
    )

    await connector.after_turn(*hook_messages())

    assert len(await outbox.peek()) == 1
    assert client.calls


@pytest.mark.anyio
async def test_successful_receipt_marks_entire_batch_committed(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")
    client = FakeClient()
    connector = MemoryTurnConnector(
        outbox,
        client,
        project_id="project-a",
        source_name="dolphin-agent",
        session_id="session-a",
    )

    await connector.after_turn(*hook_messages())

    assert await outbox.peek() == ()
    assert len(client.calls) == 1


@pytest.mark.anyio
async def test_conflict_stops_queue_without_skipping_later_turns(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")
    await outbox.enqueue(
        MemoryTurnConnector.build_turn("hello", "hi", "project-a", "dolphin-agent", "session-a", 0),
        "dolphin-agent:session-a:0",
    )
    await outbox.enqueue(
        MemoryTurnConnector.build_turn("second", "answer", "project-a", "dolphin-agent", "session-a", 1),
        "dolphin-agent:session-a:1",
    )
    client = FakeClient()
    connector = MemoryTurnConnector(
        outbox,
        client,
        project_id="project-a",
        source_name="dolphin-agent",
        session_id="session-a",
    )
    client.error = MemoryMcpError("conflict", code="conflict", retryable=False)

    with pytest.raises(MemoryMcpError, match="conflict"):
        await connector.flush()

    assert len(await outbox.peek()) == 2


@pytest.mark.anyio
async def test_non_text_turn_is_skipped_without_enqueue(tmp_path) -> None:
    outbox = DurableTurnOutbox(tmp_path / "outbox.jsonl")
    client = FakeClient()
    connector = MemoryTurnConnector(
        outbox,
        client,
        project_id="project-a",
        source_name="dolphin-agent",
        session_id="session-a",
    )

    await connector.after_turn(*hook_messages([{"type": "text", "text": "hello"}], "hi"))

    assert await outbox.peek() == ()
    assert client.calls == []

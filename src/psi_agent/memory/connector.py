"""Completed-turn hook adapter for the external memory MCP service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

from psi_agent.memory.mcp_client import MemoryMcpError
from psi_agent.memory.models import CompletedTurnInput, SourceMessageInput
from psi_agent.memory.outbox import DurableTurnOutbox


class BatchMemoryClient(Protocol):
    async def add_batch(self, idempotency_key: str, turns: tuple[CompletedTurnInput, ...]) -> dict[str, Any]: ...


class MemoryTurnConnector:
    def __init__(
        self,
        outbox: DurableTurnOutbox,
        client: BatchMemoryClient,
        *,
        project_id: str,
        source_name: str,
        session_id: str,
        batch_limit: int = 100,
    ) -> None:
        if batch_limit < 1:
            raise ValueError("batch_limit must be positive")
        self.outbox = outbox
        self.client = client
        self.project_id = project_id
        self.source_name = source_name
        self.session_id = session_id
        self.batch_limit = batch_limit

    async def after_turn(self, user_message: dict[str, Any], assistant_message: dict[str, Any]) -> None:
        user_content = user_message.get("content")
        assistant_content = assistant_message.get("content")
        if (
            not isinstance(user_content, str)
            or not user_content.strip()
            or not isinstance(assistant_content, str)
            or not assistant_content.strip()
        ):
            logger.warning("memory turn skipped: turn_not_memory_eligible")
            return
        records = await self.outbox.all_items()
        next_index = (
            max(
                (item.turn.source_turn_index for item in records if item.turn.source_turn_index is not None),
                default=-1,
            )
            + 1
        )
        turn = self.build_turn(
            user_content,
            assistant_content,
            self.project_id,
            self.source_name,
            self.session_id,
            next_index,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        key = turn.turn_id
        await self.outbox.enqueue(turn, key)
        try:
            await self.flush()
        except Exception as error:
            logger.warning(f"memory turn delivery deferred: {type(error).__name__}")

    async def flush(self) -> None:
        while pending := await self.outbox.peek():
            batch = pending[: self.batch_limit]
            key = batch[0].idempotency_key
            try:
                response = await self.client.add_batch(key, tuple(item.turn for item in batch))
            except Exception:
                raise
            receipt_id = response.get("receipt_id") if isinstance(response, dict) else None
            if not isinstance(receipt_id, str) or not receipt_id:
                raise MemoryMcpError(
                    "memory service returned an invalid receipt",
                    code="integrity_error",
                    retryable=False,
                )
            await self.outbox.mark_committed(tuple(item.idempotency_key for item in batch), receipt_id)

    @staticmethod
    def build_turn(
        user_content: str,
        assistant_content: str,
        project_id: str,
        source_name: str,
        session_id: str,
        source_turn_index: int,
        *,
        user_message: dict[str, Any] | None = None,
        assistant_message: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CompletedTurnInput:
        captured = (now or datetime.now(UTC)).astimezone(UTC)
        turn_id = f"{source_name}:{session_id}:{source_turn_index}"
        return CompletedTurnInput(
            turn_id=turn_id,
            project_id=project_id,
            conversation_id=session_id,
            source_name=source_name,
            source_session_id=session_id,
            source_turn_index=source_turn_index,
            user_message=SourceMessageInput(
                f"{turn_id}:user",
                user_content,
                _message_time(user_message, captured),
                _language(user_message),
            ),
            assistant_message=SourceMessageInput(
                f"{turn_id}:assistant",
                assistant_content,
                _message_time(assistant_message, captured),
                _language(assistant_message),
            ),
            completed_at=captured,
        )


def _message_time(message: dict[str, Any] | None, fallback: datetime) -> datetime:
    value = message.get("created_at") if message is not None else None
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    return fallback


def _language(message: dict[str, Any] | None) -> str | None:
    value = message.get("language") if message is not None else None
    return value if isinstance(value, str) else None

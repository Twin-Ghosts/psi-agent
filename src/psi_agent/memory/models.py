"""Local wire models for completed-turn memory delivery.

These models intentionally mirror only the MCP boundary.  They do not import
the memory service's domain package, which keeps Dolphin-Agent independently
installable and testable with a fake client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

DeliveryState = Literal["pending", "committed"]


@dataclass(frozen=True, slots=True)
class SourceMessageInput:
    message_id: str
    content: str
    created_at: datetime
    language: str | None

    def __post_init__(self) -> None:
        if not self.message_id.strip() or not self.content.strip():
            raise ValueError("source message id and content must be non-empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("source message timestamp must include timezone")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class CompletedTurnInput:
    turn_id: str
    project_id: str
    conversation_id: str
    source_name: str
    source_session_id: str | None
    source_turn_index: int | None
    user_message: SourceMessageInput
    assistant_message: SourceMessageInput
    completed_at: datetime

    def __post_init__(self) -> None:
        for value in (self.turn_id, self.project_id, self.conversation_id, self.source_name):
            if not value.strip():
                raise ValueError("completed turn identifiers must be non-empty")
        if self.source_turn_index is not None and self.source_turn_index < 0:
            raise ValueError("source turn index must be non-negative")
        if self.user_message.message_id == self.assistant_message.message_id:
            raise ValueError("source message IDs must differ")
        if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
            raise ValueError("completed timestamp must include timezone")
        object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))

    def to_wire(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "source_name": self.source_name,
            "source_session_id": self.source_session_id,
            "source_turn_index": self.source_turn_index,
            "user_message": _message_to_wire(self.user_message),
            "assistant_message": _message_to_wire(self.assistant_message),
            "completed_at": self.completed_at.isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> CompletedTurnInput:
        return cls(
            turn_id=payload["turn_id"],
            project_id=payload["project_id"],
            conversation_id=payload["conversation_id"],
            source_name=payload["source_name"],
            source_session_id=payload.get("source_session_id"),
            source_turn_index=payload.get("source_turn_index"),
            user_message=_message_from_wire(payload["user_message"]),
            assistant_message=_message_from_wire(payload["assistant_message"]),
            completed_at=_parse_timestamp(payload["completed_at"]),
        )


@dataclass(frozen=True, slots=True)
class OutboxItem:
    idempotency_key: str
    turn: CompletedTurnInput
    state: DeliveryState = "pending"
    receipt_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "state": self.state,
            "receipt_id": self.receipt_id,
            "turn": self.turn.to_wire(),
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> OutboxItem:
        state = payload["state"]
        if state not in ("pending", "committed"):
            raise ValueError("unknown outbox state")
        return cls(
            idempotency_key=payload["idempotency_key"],
            state=state,
            receipt_id=payload.get("receipt_id"),
            turn=CompletedTurnInput.from_wire(payload["turn"]),
        )


def _message_to_wire(message: SourceMessageInput) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "content": message.content,
        "created_at": message.created_at.isoformat().replace("+00:00", "Z"),
        "language": message.language,
    }


def _message_from_wire(payload: dict[str, Any]) -> SourceMessageInput:
    return SourceMessageInput(
        message_id=payload["message_id"],
        content=payload["content"],
        created_at=_parse_timestamp(payload["created_at"]),
        language=payload.get("language"),
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)

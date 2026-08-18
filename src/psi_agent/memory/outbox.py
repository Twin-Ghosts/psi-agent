"""Durable caller-side queue for completed memory Turns."""

from __future__ import annotations

import json
from dataclasses import replace

import anyio

from psi_agent.memory.models import CompletedTurnInput, OutboxItem


class DurableTurnOutbox:
    """Persist a complete queue replacement before any network delivery."""

    def __init__(self, path: str | anyio.Path) -> None:
        self.path = path if isinstance(path, anyio.Path) else anyio.Path(str(path))
        self._lock = anyio.Lock()

    async def enqueue(self, turn: CompletedTurnInput, idempotency_key: str) -> OutboxItem:
        async with self._lock:
            records = await self._read_all()
            for record in records:
                if record.idempotency_key == idempotency_key:
                    return record
            if turn.source_turn_index is None:
                turn = replace(turn, source_turn_index=self._next_turn_index(records))
            item = OutboxItem(idempotency_key=idempotency_key, turn=turn)
            await self._write_all((*records, item))
            return item

    async def peek(self) -> tuple[OutboxItem, ...]:
        async with self._lock:
            return tuple(item for item in await self._read_all() if item.state == "pending")

    async def all_items(self) -> tuple[OutboxItem, ...]:
        async with self._lock:
            return tuple(await self._read_all())

    async def mark_committed(self, idempotency_keys: tuple[str, ...], receipt_id: str) -> None:
        async with self._lock:
            keys = set(idempotency_keys)
            records = await self._read_all()
            updated = tuple(
                replace(item, state="committed", receipt_id=receipt_id) if item.idempotency_key in keys else item
                for item in records
            )
            await self._write_all(updated)

    async def _read_all(self) -> tuple[OutboxItem, ...]:
        if not await self.path.is_file():
            return ()
        text = await self.path.read_text(encoding="utf-8")
        records: list[OutboxItem] = []
        for line in text.splitlines():
            if line.strip():
                records.append(OutboxItem.from_wire(json.loads(line)))
        return tuple(records)

    async def _write_all(self, records: tuple[OutboxItem, ...]) -> None:
        parent = self.path.parent
        if not await parent.is_dir():
            await parent.mkdir(parents=True)
        temporary = anyio.Path(str(self.path) + ".tmp")
        content = "".join(
            json.dumps(record.to_wire(), ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
        )
        await temporary.write_text(content, encoding="utf-8")
        await temporary.replace(str(self.path))

    @staticmethod
    def _next_turn_index(records: tuple[OutboxItem, ...]) -> int:
        indexes = [item.turn.source_turn_index for item in records if item.turn.source_turn_index is not None]
        return max(indexes, default=-1) + 1

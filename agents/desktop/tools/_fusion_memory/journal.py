from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    span_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    line_no: int
    speaker: Literal["user", "assistant"]
    content: str
    content_hash: str
    timestamp: str | None
    source_uri: str


@dataclass(frozen=True, slots=True)
class ScopeClear:
    clear_id: str
    workspace_id: str
    session_id: str | None
    timestamp: str


@dataclass(frozen=True, slots=True)
class ReplayReport:
    records: int = 0
    inserted: int = 0
    duplicates: int = 0
    scope_clears: int = 0
    skipped_records: int = 0
    skipped_tail: int = 0


class JournalConflictError(ValueError):
    """An ID already exists with different canonical record bytes."""


def span_to_record(span: EvidenceSpan) -> dict[str, object]:
    return {"record_type": "evidence_span", "schema_version": 1, **asdict(span)}


def canonical_json(record: dict[str, object]) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _clear_to_record(clear: ScopeClear) -> dict[str, object]:
    return {"record_type": "scope_clear", "schema_version": 1, **asdict(clear)}


_SPAN_FIELDS = {f.name for f in EvidenceSpan.__dataclass_fields__.values()}
_CLEAR_FIELDS = {f.name for f in ScopeClear.__dataclass_fields__.values()}


class JsonlJournal:
    _process_lock = threading.RLock()

    def __init__(self, path: str | os.PathLike[str], *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fsync = fsync
        self._lock = self._process_lock
        self._span_records: dict[str, bytes] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._recover_tail()
            self._refresh_index()

    def _recover_tail(self) -> None:
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        tail_start = data.rfind(b"\n") + 1
        tail = data[tail_start:]
        try:
            record = json.loads(tail.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            self._quarantine_tail(tail)
            with self.path.open("r+b") as fh:
                fh.truncate(tail_start)
            return
        if not self._recognized_record(record):
            self._quarantine_tail(tail)
            with self.path.open("r+b") as fh:
                fh.truncate(tail_start)
            return
        with self.path.open("ab") as fh:
            fh.write(b"\n")
            if self.fsync:
                fh.flush()
                os.fsync(fh.fileno())

    def _quarantine_tail(self, tail: bytes) -> None:
        for _ in range(20):
            name = f"{self.path.name}.partial-{uuid.uuid4().hex[:12]}"
            destination = self.path.with_name(name)
            try:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(tail)
                    if self.fsync:
                        fh.flush()
                        os.fsync(fh.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return
        raise FileExistsError("could not allocate unique partial journal path")

    @staticmethod
    def _recognized_record(record: object) -> bool:
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            return False
        kind = record.get("record_type")
        if kind == "evidence_span":
            return record.keys() >= _SPAN_FIELDS and record.get("speaker") in {"user", "assistant"}
        if kind == "scope_clear":
            return record.keys() >= _CLEAR_FIELDS
        return False

    def _refresh_index(self) -> None:
        self._span_records.clear()
        if not self.path.exists():
            return
        for line in self.path.read_bytes().splitlines():
            try:
                record = json.loads(line.decode("utf-8"))
            except UnicodeDecodeError, json.JSONDecodeError:
                continue
            if self._recognized_record(record) and record.get("record_type") == "evidence_span":
                self._span_records[str(record["span_id"])] = canonical_json(record)

    def append_spans(self, spans: list[EvidenceSpan] | tuple[EvidenceSpan, ...] | object) -> list[EvidenceSpan]:
        batch = list(spans)  # type: ignore[arg-type]
        with self._lock:
            self._recover_tail()
            self._refresh_index()
            pending: dict[str, tuple[EvidenceSpan, bytes]] = {}
            for span in batch:
                record_bytes = canonical_json(span_to_record(span))
                existing = self._span_records.get(span.span_id)
                if existing is not None and existing != record_bytes:
                    raise JournalConflictError(f"span_id conflict: {span.span_id}")
                prior = pending.get(span.span_id)
                if prior is not None and prior[1] != record_bytes:
                    raise JournalConflictError(f"span_id conflict: {span.span_id}")
                pending.setdefault(span.span_id, (span, record_bytes))
            new = [(item, data) for sid, (item, data) in pending.items() if sid not in self._span_records]
            if new:
                with self.path.open("ab") as fh:
                    for _, data in new:
                        fh.write(data + b"\n")
                    if self.fsync:
                        fh.flush()
                        os.fsync(fh.fileno())
                self._span_records.update((item.span_id, data) for item, data in new)
            return [item for item, _ in new]

    def append_scope_clear(self, workspace_id: str, session_id: str | None = None) -> ScopeClear:
        clear = ScopeClear(uuid.uuid4().hex, workspace_id, session_id, datetime.now(UTC).isoformat())
        data = canonical_json(_clear_to_record(clear)) + b"\n"
        with self._lock:
            self._recover_tail()
            with self.path.open("ab") as fh:
                fh.write(data)
                if self.fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        return clear

    def replay(
        self, on_span: Callable[[EvidenceSpan], object], on_clear: Callable[[ScopeClear], object]
    ) -> ReplayReport:
        counts = [0, 0, 0, 0, 0, 0]
        if not self.path.exists():
            return ReplayReport()
        data = self.path.read_bytes()
        tail = 0 if data.endswith(b"\n") or not data else 1
        for line in data.splitlines():
            counts[0] += 1
            try:
                record = json.loads(line.decode("utf-8"))
                if not self._recognized_record(record):
                    raise ValueError
                if record["record_type"] == "evidence_span":
                    on_span(EvidenceSpan(**{k: record[k] for k in _SPAN_FIELDS}))
                    counts[1] += 1
                else:
                    on_clear(ScopeClear(**{k: record[k] for k in _CLEAR_FIELDS}))
                    counts[3] += 1
            except UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError, ValueError:
                counts[4] += 1
        counts[5] = tail
        return ReplayReport(*counts)

    def iter_active_spans(self):
        active: OrderedDict[str, EvidenceSpan] = OrderedDict()

        def add(item: EvidenceSpan) -> None:
            active[item.span_id] = item

        def clear(item: ScopeClear) -> None:
            for sid, value in list(active.items()):
                if value.workspace_id == item.workspace_id and (
                    item.session_id is None or value.session_id == item.session_id
                ):
                    del active[sid]

        self.replay(add, clear)
        return iter(active.values())

    def copy_to(self, destination: str | os.PathLike[str]) -> None:
        with self._lock:
            shutil.copyfile(self.path, destination)

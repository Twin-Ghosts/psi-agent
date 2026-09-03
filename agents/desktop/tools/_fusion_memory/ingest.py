from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anyio
from anyio import to_thread

from psi_agent._appdata import resolve_history_read_path

from .journal import EvidenceSpan
from .store import IngestCheckpoint, MemoryStore

_TRANSFER_MARKER = re.compile(r"\[\s*(?:SEND|RECV)\s*:\s*[^\]\n]*?\]", re.IGNORECASE)
_MAX_ROUND_MESSAGES = frozenset({"[max tool rounds reached]"})


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    normalized: str
    workspace_id: str


@dataclass(frozen=True, slots=True)
class HistorySource:
    session_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class IngestReport:
    files_scanned: int = 0
    completed_turns: int = 0
    spans_appended: int = 0
    spans_indexed: int = 0
    rescanned_files: int = 0


def normalize_workspace(path: str | Path) -> str:
    value = os.path.realpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(value) if sys.platform == "win32" else value


def workspace_scope(path: str | Path) -> WorkspaceScope:
    normalized = normalize_workspace(path)
    return WorkspaceScope(normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest())


def _visible_content(row: dict[str, object]) -> str:
    value = row.get("content", "")
    if not isinstance(value, str):
        return ""
    return _TRANSFER_MARKER.sub("", value)


def _kind(row: dict[str, object]) -> str:
    if "kind" in row:
        return "chat" if row.get("kind") == "chat" else ""
    if "chat_type" in row:
        return "chat" if row.get("chat_type") == "common" else ""
    return "chat"


def _timestamp(row: dict[str, object]) -> str | None:
    value = row.get("timestamp", row.get("created_at"))
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


def _turn_id(
    session_id: str,
    user_line: int,
    assistant_line: int,
    user_content_hash: str,
    assistant_content_hash: str,
) -> str:
    seed = f"{session_id}|{user_line}|{assistant_line}|{user_content_hash}|{assistant_content_hash}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _span_id(scope: WorkspaceScope, session_id: str, line_no: int, speaker: str, content: str) -> tuple[str, str]:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    seed = f"{scope.workspace_id}|{session_id}|{line_no}|{speaker}|{content_hash}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest(), content_hash


def _read_rows(path: Path, start_line: int = 1) -> Iterator[tuple[int, dict[str, object]]]:
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if line_no < start_line:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError, UnicodeDecodeError:
                continue
            if isinstance(value, dict):
                yield line_no, value


def parse_completed_turns(
    scope: WorkspaceScope,
    source: HistorySource,
    start_line: int = 1,
    max_turns: int | None = None,
) -> list[EvidenceSpan]:
    if max_turns is not None and max_turns <= 0:
        return []
    result: list[EvidenceSpan] = []
    pending: tuple[int, dict[str, object], str] | None = None
    path = Path(source.path)
    completed = 0
    for line_no, row in _read_rows(path, start_line):
        role = row.get("role")
        kind = _kind(row)
        if role == "user":
            pending = None
            visible = _visible_content(row)
            if kind == "chat" and visible.strip():
                pending = (line_no, row, visible)
            continue
        if role != "assistant":
            continue
        visible = _visible_content(row)
        tool_calls = row.get("tool_calls") or row.get("tools")
        if kind != "chat" or not visible.strip() or tool_calls:
            continue
        if visible.strip() == "HEARTBEAT_OK" or visible.strip().casefold() in _MAX_ROUND_MESSAGES:
            continue
        if pending is None:
            continue
        user_line, user_row, user_text = pending
        user_id, user_hash = _span_id(scope, source.session_id, user_line, "user", user_text)
        assistant_id, assistant_hash = _span_id(scope, source.session_id, line_no, "assistant", visible)
        turn_id = _turn_id(source.session_id, user_line, line_no, user_hash, assistant_hash)
        result.extend(
            (
                EvidenceSpan(
                    span_id=user_id,
                    workspace_id=scope.workspace_id,
                    session_id=source.session_id,
                    turn_id=turn_id,
                    line_no=user_line,
                    speaker="user",
                    content=user_text,
                    content_hash=user_hash,
                    timestamp=_timestamp(user_row),
                    source_uri=f"history://{path.as_posix()}#L{user_line}",
                ),
                EvidenceSpan(
                    span_id=assistant_id,
                    workspace_id=scope.workspace_id,
                    session_id=source.session_id,
                    turn_id=turn_id,
                    line_no=line_no,
                    speaker="assistant",
                    content=visible,
                    content_hash=assistant_hash,
                    timestamp=_timestamp(row),
                    source_uri=f"history://{path.as_posix()}#L{line_no}",
                ),
            )
        )
        pending = None
        completed += 1
        if max_turns is not None and completed >= max_turns:
            break
    return result


async def _owned_history_source(
    scope: WorkspaceScope,
    session_id: str,
    path: Path,
    appdata_root: Path,
) -> tuple[str, HistorySource] | None:
    if re.fullmatch(r"[a-zA-Z0-9_-]+", session_id) is None:
        return None
    allowed_roots = (Path(appdata_root) / "histories", Path(scope.normalized) / "histories")
    key, expected = await to_thread.run_sync(_history_ownership, path, allowed_roots, session_id)
    if key not in expected or not await anyio.Path(path).is_file():
        return None
    return key, HistorySource(session_id, path)


def _history_ownership(path: Path, allowed_roots: tuple[Path, Path], session_id: str) -> tuple[str, set[str]]:
    key = os.path.normcase(os.path.realpath(os.fspath(path)))
    expected = {
        normalized
        for root in allowed_roots
        if (normalized := os.path.normcase(os.path.abspath(os.fspath(root / f"{session_id}.jsonl"))))
        == os.path.normcase(os.path.realpath(os.fspath(root / f"{session_id}.jsonl")))
    }
    return key, expected


async def discover_current_history(
    scope: WorkspaceScope,
    current_session_id: str,
    appdata_root: Path,
) -> HistorySource | None:
    if re.fullmatch(r"[a-zA-Z0-9_-]+", current_session_id) is None:
        return None
    current_path = Path(
        str(
            await resolve_history_read_path(
                appdata_root=str(appdata_root), workspace=scope.normalized, session_id=current_session_id
            )
        )
    )
    owned = await _owned_history_source(scope, current_session_id, current_path, appdata_root)
    return owned[1] if owned else None


def _prefix_hash(path: Path, line_count: int) -> str:
    lines = path.read_bytes().splitlines(keepends=True)
    return hashlib.sha256(b"".join(lines[:line_count])).hexdigest()


def ingest_confirmed_turn(
    store: MemoryStore,
    scope: WorkspaceScope,
    source: HistorySource,
    user_message: dict[str, object],
    assistant_message: dict[str, object],
) -> tuple[IngestReport, list[EvidenceSpan]]:
    path = Path(source.path)
    if not path.is_file():
        return IngestReport(), []
    if (
        user_message.get("role") != "user"
        or assistant_message.get("role") != "assistant"
        or _kind(user_message) != "chat"
        or _kind(assistant_message) != "chat"
    ):
        return IngestReport(files_scanned=1), []
    user_text = _visible_content(user_message)
    assistant_text = _visible_content(assistant_message)
    if (
        not user_text.strip()
        or not assistant_text.strip()
        or assistant_message.get("tool_calls")
        or assistant_message.get("tools")
        or assistant_text.strip() == "HEARTBEAT_OK"
        or assistant_text.strip().casefold() in _MAX_ROUND_MESSAGES
    ):
        return IngestReport(files_scanned=1), []

    stat = path.stat()
    checkpoint = store.read_checkpoint(scope.workspace_id, str(path.resolve()))
    checkpoint_valid = bool(
        checkpoint
        and stat.st_size >= checkpoint.file_size
        and _prefix_hash(path, checkpoint.confirmed_line_count) == checkpoint.prefix_hash
    )
    rescanned = int(bool(checkpoint and not checkpoint_valid))
    start_line = checkpoint.confirmed_line_count + 1 if checkpoint_valid and checkpoint else 1
    parsed = parse_completed_turns(scope, source, start_line)
    selected: list[EvidenceSpan] = []
    for offset in range(0, len(parsed), 2):
        turn = parsed[offset : offset + 2]
        if len(turn) == 2 and turn[0].content == user_text and turn[1].content == assistant_text:
            selected = turn
    if not selected:
        return IngestReport(files_scanned=1, rescanned_files=rescanned), []

    new = store.index_spans(selected)
    confirmed_line = selected[-1].line_no
    prior = checkpoint if checkpoint_valid else None
    store.write_checkpoint(
        IngestCheckpoint(
            workspace_id=scope.workspace_id,
            history_path=str(path.resolve()),
            session_id=source.session_id,
            confirmed_line_count=confirmed_line,
            prefix_hash=_prefix_hash(path, confirmed_line),
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            extraction_line=prior.extraction_line if prior else 0,
            embedding_line=prior.embedding_line if prior else 0,
            card_line=prior.card_line if prior else 0,
            updated_at="",
        )
    )
    return IngestReport(1, 1, len(new), len(selected), rescanned), selected

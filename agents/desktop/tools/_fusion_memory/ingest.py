from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anyio
from anyio import to_thread

from psi_agent._appdata import (
    appdata_state_latest_path,
    legacy_state_latest_path,
    resolve_history_read_path,
)
from psi_agent.session.history_display import message_kind, strip_transfer_markers, wire_role

from .journal import EvidenceSpan
from .store import IngestCheckpoint, MemoryStore

_MAX_ROUND_MARKERS = (
    "maximum context length",
    "max rounds",
    "max_rounds",
    "max tool rounds reached",
    "达到最大轮数",
    "达到最大回合",
)


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
    return strip_transfer_markers(value).strip()


def _kind(row: dict[str, object]) -> str:
    return message_kind(row)


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


def _read_rows(path: Path) -> list[tuple[int, dict[str, object]]]:
    rows: list[tuple[int, dict[str, object]]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError, UnicodeDecodeError:
            continue
        if isinstance(value, dict):
            rows.append((line_no, value))
    return rows


def parse_completed_turns(scope: WorkspaceScope, source: HistorySource, start_line: int = 1) -> list[EvidenceSpan]:
    result: list[EvidenceSpan] = []
    pending: tuple[int, dict[str, object], str] | None = None
    path = Path(source.path)
    for line_no, row in _read_rows(path):
        if line_no < start_line:
            continue
        role = wire_role(row.get("role"))
        kind = _kind(row)
        if role == "user":
            pending = None
            visible = _visible_content(row)
            if kind == "chat" and visible:
                pending = (line_no, row, visible)
            continue
        if role != "assistant":
            continue
        visible = _visible_content(row)
        tool_calls = row.get("tool_calls") or row.get("tools")
        if kind != "chat" or not visible or tool_calls:
            continue
        if visible == "HEARTBEAT_OK" or any(marker in visible.lower() for marker in _MAX_ROUND_MARKERS):
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
    return result


async def discover_histories(scope: WorkspaceScope, current_session_id: str, appdata_root: Path) -> list[HistorySource]:
    discovered: dict[str, HistorySource] = {}

    async def add(session_id: str, path: Path) -> None:
        if re.fullmatch(r"[a-zA-Z0-9_-]+", session_id) is None or not await anyio.Path(path).is_file():
            return
        key = await to_thread.run_sync(lambda: os.path.normcase(os.path.realpath(os.fspath(path))))
        discovered.setdefault(key, HistorySource(session_id, path))

    current_path = Path(
        str(
            await resolve_history_read_path(
                appdata_root=str(appdata_root), workspace=scope.normalized, session_id=current_session_id
            )
        )
    )
    await add(current_session_id, current_path)

    state_paths = [Path(str(appdata_state_latest_path(str(appdata_root)))), Path(str(legacy_state_latest_path()))]
    for state_path in state_paths:
        if not state_path.is_file():
            continue
        try:
            snapshot = json.loads(await _async_read_text(state_path))
        except OSError, json.JSONDecodeError, UnicodeDecodeError:
            continue
        sessions = snapshot.get("sessions", []) if isinstance(snapshot, dict) else []
        if not isinstance(sessions, list):
            continue
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session_id = item.get("id")
            workspace = item.get("workspace")
            if not isinstance(session_id, str) or not session_id or not isinstance(workspace, str):
                continue
            if normalize_workspace(workspace) != scope.normalized:
                continue
            path = Path(
                str(
                    await resolve_history_read_path(
                        appdata_root=str(appdata_root), workspace=scope.normalized, session_id=session_id
                    )
                )
            )
            await add(session_id, path)

    for path in (Path(scope.normalized) / "histories").glob("*.jsonl"):
        await add(path.stem, path)
    return list(discovered.values())


async def _async_read_text(path: Path) -> str:
    return await anyio.Path(path).read_text(encoding="utf-8")


def _prefix_hash(path: Path, line_count: int) -> str:
    lines = path.read_bytes().splitlines(keepends=True)
    return hashlib.sha256(b"".join(lines[:line_count])).hexdigest()


def ingest_histories(store: MemoryStore, scope: WorkspaceScope, sources: list[HistorySource]) -> IngestReport:
    files_scanned = completed_turns = appended = indexed = rescanned = 0
    for source in sources:
        path = Path(source.path)
        if not path.is_file():
            continue
        files_scanned += 1
        stat = path.stat()
        checkpoint = store.read_checkpoint(scope.workspace_id, str(path.resolve()))
        checkpoint_valid = bool(
            checkpoint
            and stat.st_size >= checkpoint.file_size
            and _prefix_hash(path, checkpoint.confirmed_line_count) == checkpoint.prefix_hash
        )
        if checkpoint and not checkpoint_valid:
            rescanned += 1
        start_line = checkpoint.confirmed_line_count + 1 if checkpoint_valid and checkpoint else 1
        spans = parse_completed_turns(scope, source, start_line)
        turn_ids = {span.turn_id for span in spans}
        completed_turns += len(turn_ids)
        new = store.index_spans(spans)
        appended += len(new)
        indexed += len(spans)
        prior_confirmed = checkpoint.confirmed_line_count if checkpoint_valid and checkpoint else 0
        confirmed_line = max((span.line_no for span in spans), default=prior_confirmed)
        checkpoint = IngestCheckpoint(
            workspace_id=scope.workspace_id,
            history_path=str(path.resolve()),
            session_id=source.session_id,
            confirmed_line_count=confirmed_line,
            prefix_hash=_prefix_hash(path, confirmed_line),
            file_size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            extraction_line=checkpoint.extraction_line if checkpoint_valid and checkpoint else 0,
            embedding_line=checkpoint.embedding_line if checkpoint_valid and checkpoint else 0,
            updated_at="",
        )
        store.write_checkpoint(checkpoint)
    return IngestReport(files_scanned, completed_turns, appended, indexed, rescanned)

from __future__ import annotations

# ruff: noqa: RUF001
import json
from pathlib import Path

import pytest
from _fusion_memory.ingest import (
    HistorySource,
    discover_histories,
    ingest_histories,
    parse_completed_turns,
    workspace_scope,
)
from _fusion_memory.journal import JsonlJournal
from _fusion_memory.store import MemoryStore


def test_parse_filters_non_raw_rows_and_preserves_history(tmp_path: Path) -> None:
    history = tmp_path / "s1.jsonl"
    rows = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "记住我用 PostgreSQL\n[RECV:/tmp/a.txt]", "kind": "chat", "turn_context": "clock"},
        {"role": "assistant", "reasoning": "thinking", "tool_calls": [{"id": "1"}], "kind": "chat"},
        {"role": "tool", "content": "secret tool output", "kind": "chat"},
        {"role": "assistant", "content": "好的，已记录。[SEND:/tmp/result.md]", "reasoning": "hidden", "kind": "chat"},
        {"role": "user", "content": "heartbeat", "kind": "schedule.silent"},
        {"role": "assistant", "content": "HEARTBEAT_OK", "kind": "schedule.silent"},
        {"role": "user_schedule", "content": "legacy trigger", "chat_type": "schedule"},
        {"role": "assistant_schedule", "content": "legacy output", "chat_type": "schedule"},
        {"role": "user", "content": "tool loop", "kind": "chat"},
        {"role": "assistant", "content": "[Max tool rounds reached]", "kind": "chat"},
        {"role": "user", "content": "unfinished", "kind": "chat"},
    ]
    history.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    before = history.read_bytes()
    scope = workspace_scope(tmp_path)
    spans = parse_completed_turns(scope, HistorySource("s1", history))
    assert [(span.speaker, span.content) for span in spans] == [
        ("user", "记住我用 PostgreSQL"),
        ("assistant", "好的，已记录。"),
    ]
    assert all(span.timestamp is None for span in spans)
    assert history.read_bytes() == before


def test_ingest_is_idempotent_and_updates_checkpoint(tmp_path: Path) -> None:
    history = tmp_path / "s1.jsonl"
    history.write_text(
        json.dumps({"role": "user", "content": "你好", "kind": "chat"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"role": "assistant", "content": "你好！", "kind": "chat"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    scope = workspace_scope(tmp_path)
    journal = JsonlJournal(tmp_path / "evidence.jsonl", fsync=False)
    store = MemoryStore(tmp_path / "memory.sqlite3", journal, scope.workspace_id).open()
    source = HistorySource("s1", history)
    first = ingest_histories(store, scope, [source])
    second = ingest_histories(store, scope, [source])
    assert first.completed_turns == 1 and second.completed_turns == 0
    assert first.spans_appended == 2 and second.spans_appended == 0
    assert store.read_checkpoint(scope.workspace_id, str(history.resolve())) is not None
    history.write_text(json.dumps({"role": "user", "content": "changed", "kind": "chat"}) + "\n", encoding="utf-8")
    third = ingest_histories(store, scope, [source])
    assert third.rescanned_files == 1
    store.close()


def test_full_rescan_cannot_resurrect_tombstoned_history(tmp_path: Path) -> None:
    history = tmp_path / "s1.jsonl"
    history.write_text(
        json.dumps({"role": "user", "content": "secret", "kind": "chat"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "saved", "kind": "chat"})
        + "\n",
        encoding="utf-8",
    )
    scope = workspace_scope(tmp_path)
    journal = JsonlJournal(tmp_path / "evidence.jsonl", fsync=False)
    store = MemoryStore(tmp_path / "memory.sqlite3", journal, scope.workspace_id).open()
    source = HistorySource("s1", history)
    ingest_histories(store, scope, [source])
    journal.append_scope_clear(scope.workspace_id)
    store.rebuild_index()
    store.connection.execute("delete from ingest_checkpoints")
    store.connection.commit()
    ingest_histories(store, scope, [source])
    assert (
        store.get_source_spans(scope.workspace_id, [span.span_id for span in parse_completed_turns(scope, source)])
        == []
    )
    assert list(journal.iter_active_spans()) == []
    store.close()


@pytest.mark.anyio
async def test_discover_histories_filters_gateway_sessions_by_workspace(tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    (appdata / "histories").mkdir(parents=True)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    (workspace_a / "histories").mkdir(parents=True)
    (workspace_b / "histories").mkdir(parents=True)
    (appdata / "histories" / "s1.jsonl").write_text("{}\n", encoding="utf-8")
    (appdata / "histories" / "s2.jsonl").write_text("{}\n", encoding="utf-8")
    (appdata / "histories" / "unowned.jsonl").write_text("{}\n", encoding="utf-8")
    (appdata / "state").mkdir()
    (appdata / "state" / "latest.json").write_text(
        json.dumps(
            {
                "sessions": [{"id": "s1", "workspace": str(workspace_a)}, {"id": "s2", "workspace": str(workspace_b)}],
                "ais": [{"id": "secret", "api_key": "do-not-read"}],
            }
        ),
        encoding="utf-8",
    )
    found = await discover_histories(workspace_scope(workspace_a), "s1", appdata)
    assert {item.session_id for item in found} == {"s1"}

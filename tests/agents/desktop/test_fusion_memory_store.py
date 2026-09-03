from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from _fusion_memory.journal import EvidenceSpan, JsonlJournal, canonical_json, span_to_record
from _fusion_memory.store import IngestCheckpoint, MemoryStore


def make_span(
    span_id: str, workspace_id: str = "workspace-a", content: str = "原始文本", line_no: int = 1
) -> EvidenceSpan:
    return EvidenceSpan(
        span_id=span_id,
        workspace_id=workspace_id,
        session_id="session-1",
        turn_id="turn-1",
        line_no=line_no,
        speaker="assistant",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        timestamp=None,
        source_uri="history:///session-1#L1",
    )


def opened(tmp_path: Path, workspace: str = "workspace-a") -> tuple[JsonlJournal, MemoryStore]:
    journal = JsonlJournal(tmp_path / "evidence.jsonl", fsync=False)
    store = MemoryStore(tmp_path / "memory.sqlite3", journal, workspace).open()
    return journal, store


def test_schema_is_minimal_fts5_wal_and_rebuildable(tmp_path: Path) -> None:
    journal, store = opened(tmp_path)
    journal.append_spans([make_span("span-1")])
    store.replay_journal()
    names = {row[0] for row in store.connection.execute("select name from sqlite_master")}
    assert {"evidence_spans", "memory_items", "summary_cards", "ingest_checkpoints"} <= names
    assert "fts_memory" in names
    assert not {"fact_relations", "event_edges", "entities", "current_views", "entity_profiles"} & names
    assert store.connection.execute("pragma journal_mode").fetchone()[0] == "wal"
    store.connection.execute("delete from evidence_spans")
    store.connection.execute("delete from fts_memory")
    store.connection.commit()
    assert store.rebuild_index().inserted == 1
    assert [row.doc_id for row in store.search_fts("原始", "workspace-a", 5)] == ["span-1"]
    store.close()


def test_index_writes_journal_before_sqlite_and_enforces_scope(tmp_path: Path) -> None:
    _journal, store = opened(tmp_path)
    span = make_span("span-1")
    assert store.index_spans([span]) == [span]
    assert len((tmp_path / "evidence.jsonl").read_bytes().splitlines()) == 1
    with pytest.raises(ValueError):
        store.index_spans([make_span("foreign", "workspace-b")])
    store.close()


def test_incremental_index_does_not_replay_the_full_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal, store = opened(tmp_path)

    def reject_replay():
        raise AssertionError("incremental indexing must not replay the authority")

    monkeypatch.setattr(journal, "iter_active_spans", reject_replay)
    assert store.index_spans([]) == []
    span = make_span("span-1")
    assert store.index_spans([span]) == [span]
    assert store.get_source_spans("workspace-a", ["span-1"]) == [span]
    store.close()


def test_promote_card_embeddings_and_checkpoint_round_trip(tmp_path: Path) -> None:
    _, store = opened(tmp_path)
    store.index_spans([make_span("u", content="用户偏好", line_no=1), make_span("a", content="助手记住", line_no=2)])
    item = store.promote("workspace-a", ["a", "u"], "preference", 0.7)
    same_item = store.promote("workspace-a", ["u", "a"], "preference", 0.7)
    assert item.text == "用户偏好\n助手记住"
    assert same_item.item_id == item.item_id
    assert store.upsert_turn_card("workspace-a", "turn-1", "用户偏好", "助手记住", ["u", "a"])
    assert {row[0] for row in store.connection.execute("select doc_type from fts_memory")} == {
        "evidence",
        "memory_item",
        "summary_card",
    }
    pending = store.pending_embeddings("workspace-a", 10)
    assert {row[0] for row in pending} == {"evidence", "memory_item"}
    assert store.write_embeddings("workspace-a", "text-embedding-v4", {("evidence", "u"): [1.0, 0.0]}) == 1
    checkpoint = IngestCheckpoint(
        "workspace-a", "/history.jsonl", "session-1", 2, "abc", 42, 9, 2, 0, "2026-09-03T12:00:00+00:00"
    )
    store.write_checkpoint(checkpoint)
    assert store.read_checkpoint("workspace-a", "/history.jsonl") == checkpoint
    store.close()


def test_scope_clear_replay_and_source_boundary(tmp_path: Path) -> None:
    journal, store = opened(tmp_path)
    store.index_spans([make_span("local")])
    journal.append_spans([make_span("foreign", "workspace-b")])
    journal.append_scope_clear("workspace-a")
    store.rebuild_index()
    assert list(store.get_source_spans("workspace-a", ["local"])) == []
    with pytest.raises(ValueError):
        store.promote("workspace-a", ["foreign"], "fact", 1.0)
    store.close()


def test_corrupt_sqlite_is_quarantined_and_backup_is_paired(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    journal_path = tmp_path / "evidence.jsonl"
    journal_path.write_bytes(canonical_json(span_to_record(make_span("recover"))) + b"\n")
    db.write_bytes(b"not a database")
    journal = JsonlJournal(journal_path, fsync=False)
    store = MemoryStore(db, journal, "workspace-a").open()
    assert store.get_source_spans("workspace-a", ["recover"])
    assert list(tmp_path.glob("memory.sqlite3.corrupt-*"))
    backup = store.backup_to(tmp_path / "backup")
    assert backup.exists() and (tmp_path / "backup" / "evidence.jsonl").exists()
    store.close()


def test_legacy_database_and_journal_are_backed_up_without_row_conversion(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    journal_path = tmp_path / "evidence.jsonl"
    with sqlite3.connect(db) as conn:
        conn.execute("create table legacy_marker (value text)")
        conn.execute("insert into legacy_marker values ('must-not-convert')")
        conn.execute("pragma user_version = 37")
    journal_path.write_bytes(canonical_json(span_to_record(make_span("journal"))) + b"\n")
    store = MemoryStore(db, JsonlJournal(journal_path, fsync=False), "workspace-a").open()
    names = {row[0] for row in store.connection.execute("select name from sqlite_master")}
    assert "legacy_marker" not in names
    assert store.get_source_spans("workspace-a", ["journal"])
    assert list(tmp_path.glob("memory.sqlite3.legacy-*"))
    assert list(tmp_path.glob("evidence.jsonl.legacy-*"))
    store.close()

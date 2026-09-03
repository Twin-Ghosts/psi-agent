from __future__ import annotations

import json
import threading
from pathlib import Path

import _fusion_memory.runtime as runtime_module
import anyio
import pytest
from _fusion_memory.runtime import get_runtime, reset_runtime_cache_for_tests


@pytest.fixture(autouse=True)
async def clear_runtime_cache() -> None:
    await reset_runtime_cache_for_tests()
    yield
    await reset_runtime_cache_for_tests()


@pytest.mark.anyio
async def test_cache_is_workspace_scoped_and_survives_session_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    appdata = tmp_path / "appdata"
    (appdata / "histories").mkdir(parents=True)
    (appdata / "state").mkdir()
    history = appdata / "histories" / "s1.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "我使用 PostgreSQL", "kind": "chat"}, ensure_ascii=False),
                json.dumps({"role": "assistant", "content": "已记录数据库偏好", "kind": "chat"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (appdata / "state" / "latest.json").write_text(
        json.dumps({"sessions": [{"id": "s1", "workspace": str(workspace_a)}]}), encoding="utf-8"
    )
    monkeypatch.setenv("PSI_APPDATA", str(appdata))
    monkeypatch.setenv("FUSION_MEMORY_JOURNAL_FSYNC", "0")
    runtime_a1 = await get_runtime(str(workspace_a))
    runtime_a2 = await get_runtime(str(workspace_a / "."))
    runtime_b = await get_runtime(str(workspace_b))
    assert runtime_a1 is runtime_a2
    assert runtime_a1 is not runtime_b
    assert runtime_a1.workspace_id != runtime_b.workspace_id
    assert (await runtime_a1.ingest_current_session("s1"))["ok"] is True
    hits = await runtime_a1.search("PostgreSQL")
    assert hits and hits[0].session_id == "s1"
    assert await runtime_b.search("PostgreSQL") == []
    block = await runtime_a1.first_turn_recall("s2", "PostgreSQL")
    assert "PostgreSQL" in block
    assert await runtime_a1.first_turn_recall("s2", "PostgreSQL") == ""


@pytest.mark.anyio
async def test_disabled_runtime_creates_no_memory_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FUSION_MEMORY_ENABLE_JOURNAL", "0")
    runtime = await get_runtime(str(workspace))
    assert not runtime.enabled
    assert not (workspace / ".fusion-memory").exists()
    assert await runtime.search("anything") == []


@pytest.mark.anyio
async def test_concurrent_first_use_creates_one_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FUSION_MEMORY_JOURNAL_FSYNC", "0")
    original = runtime_module._create_runtime
    count = 0
    count_lock = threading.Lock()

    def counted(settings):
        nonlocal count
        with count_lock:
            count += 1
        return original(settings)

    monkeypatch.setattr(runtime_module, "_create_runtime", counted)
    results = []

    async def load() -> None:
        results.append(await get_runtime(str(workspace)))

    async with anyio.create_task_group() as group:
        group.start_soon(load)
        group.start_soon(load)
    assert len(results) == 2 and results[0] is results[1]
    assert count == 1


@pytest.mark.anyio
async def test_extraction_checkpoint_advances_one_bounded_turn_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    appdata = tmp_path / "appdata"
    histories = appdata / "histories"
    histories.mkdir(parents=True)
    history = histories / "s1.jsonl"
    rows = []
    for turn in range(1, 11):
        rows.extend(
            (
                {"role": "user", "content": f"question {turn}", "kind": "chat"},
                {"role": "assistant", "content": f"answer {turn}", "kind": "chat"},
            )
        )
    history.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("PSI_APPDATA", str(appdata))
    monkeypatch.setenv("FUSION_MEMORY_JOURNAL_FSYNC", "0")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_MEMORY_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("PSI_AI_API_KEY", raising=False)
    runtime = await get_runtime(str(workspace))

    assert (await runtime.ingest_current_session("s1"))["ok"] is True
    assert runtime.store is not None
    checkpoint = runtime.store.read_checkpoint(runtime.workspace_id, str(history.resolve()))
    assert checkpoint is not None and checkpoint.extraction_line == 16
    assert runtime.store.conn.execute("select count(*) from summary_cards").fetchone()[0] == 8

    assert (await runtime.ingest_current_session("s1"))["ok"] is True
    checkpoint = runtime.store.read_checkpoint(runtime.workspace_id, str(history.resolve()))
    assert checkpoint is not None and checkpoint.extraction_line == 20
    assert runtime.store.conn.execute("select count(*) from summary_cards").fetchone()[0] == 10

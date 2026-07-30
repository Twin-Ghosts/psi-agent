from __future__ import annotations

import anyio
import pytest

from psi_agent.gateway._summary_manager import SummaryManager
from psi_agent.gateway._title_manager import TitleManager


@pytest.mark.anyio
async def test_title_manager_concurrency():
    persist_count = 0
    active_persists = 0
    max_concurrent_persists = 0

    async def mock_persist():
        nonlocal persist_count, active_persists, max_concurrent_persists
        active_persists += 1
        max_concurrent_persists = max(max_concurrent_persists, active_persists)
        await anyio.sleep(0.05)
        active_persists -= 1
        persist_count += 1

    tm = TitleManager(_persist=mock_persist)

    # Set titles concurrently and verify that the lock prevents concurrent callback execution
    async def set_title(sid, val):
        await tm.set(sid, val)

    async with anyio.create_task_group() as tg:
        tg.start_soon(set_title, "s1", "Title A")
        tg.start_soon(set_title, "s2", "Title B")
        tg.start_soon(set_title, "s3", "Title C")

    assert tm.get_all() == {"s1": "Title A", "s2": "Title B", "s3": "Title C"}
    assert persist_count == 3
    # With a lock guarding the set, there should be absolutely no overlap in execution of mock_persist,
    # meaning max_concurrent_persists must be exactly 1!
    assert max_concurrent_persists == 1


@pytest.mark.anyio
async def test_summary_manager_concurrency():
    persist_count = 0
    active_persists = 0
    max_concurrent_persists = 0

    async def mock_persist():
        nonlocal persist_count, active_persists, max_concurrent_persists
        active_persists += 1
        max_concurrent_persists = max(max_concurrent_persists, active_persists)
        await anyio.sleep(0.05)
        active_persists -= 1
        persist_count += 1

    sm = SummaryManager(_persist=mock_persist)

    # Set summaries concurrently
    async def set_summary(sid, val):
        await sm.set(sid, val)

    async with anyio.create_task_group() as tg:
        tg.start_soon(set_summary, "s1", "Summary A")
        tg.start_soon(set_summary, "s2", "Summary B")
        tg.start_soon(set_summary, "s3", "Summary C")

    assert sm.get_all() == {"s1": "Summary A", "s2": "Summary B", "s3": "Summary C"}
    assert persist_count == 3
    # Lock protects against concurrent overlaps, so max_concurrent_persists is 1
    assert max_concurrent_persists == 1


@pytest.mark.anyio
async def test_title_manager_delete_concurrency():
    tm = TitleManager(_persist=lambda: anyio.sleep(0.01))
    await tm.set("s1", "Old Title")

    async def delete_title(sid):
        await tm.delete(sid)

    async with anyio.create_task_group() as tg:
        tg.start_soon(delete_title, "s1")
        tg.start_soon(delete_title, "s1")  # Concurrent redundant deletes

    assert tm.get_all() == {}


@pytest.mark.anyio
async def test_summary_manager_delete_concurrency():
    sm = SummaryManager(_persist=lambda: anyio.sleep(0.01))
    await sm.set("s1", "Old Summary")

    async def delete_summary(sid):
        await sm.delete(sid)

    async with anyio.create_task_group() as tg:
        tg.start_soon(delete_summary, "s1")
        tg.start_soon(delete_summary, "s1")

    assert sm.get_all() == {}

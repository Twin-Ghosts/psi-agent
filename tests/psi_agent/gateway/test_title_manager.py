from __future__ import annotations

from typing import Any, cast

import anyio
import pytest
from aiohttp import web

from psi_agent.gateway._title_manager import TitleManager


@pytest.mark.anyio
async def test_title_manager_basic_operations() -> None:
    persist_called = 0

    async def mock_persist() -> None:
        nonlocal persist_called
        persist_called += 1

    tm = TitleManager(_persist=mock_persist)

    # Initially empty
    assert tm.get_all() == {}

    # Set title
    await tm.set("session1", "My Session Title")
    assert tm.get_all() == {"session1": "My Session Title"}
    assert persist_called == 1

    # Set another
    await tm.set("session2", "Another Title")
    assert tm.get_all() == {"session1": "My Session Title", "session2": "Another Title"}
    assert persist_called == 2

    # Delete
    await tm.delete("session1")
    assert tm.get_all() == {"session2": "Another Title"}
    assert persist_called == 3

    # Delete non-existent (noop)
    await tm.delete("nonexistent")
    assert tm.get_all() == {"session2": "Another Title"}
    assert persist_called == 3


@pytest.mark.anyio
async def test_title_manager_concurrency() -> None:
    persist_called = 0
    in_persist = False

    async def mock_persist() -> None:
        nonlocal persist_called, in_persist
        # Assert lock exclusivity: no two persist coroutines can run concurrently
        assert not in_persist, "Lock exclusivity violated!"
        in_persist = True
        await anyio.sleep(0.01)
        persist_called += 1
        in_persist = False

    tm = TitleManager(_persist=mock_persist)

    async def task(session_id: str, title: str) -> None:
        await tm.set(session_id, title)

    # Launch concurrent tasks
    async with anyio.create_task_group() as tg:
        for i in range(10):
            tg.start_soon(task, f"session_{i}", f"Title {i}")

    assert len(tm.get_all()) == 10
    assert persist_called == 10


@pytest.mark.anyio
async def test_title_manager_generate_success() -> None:
    # Setup mock AI server that responds with Title SSE chunks
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        # Send data chunk by chunk
        await resp.write(b'data: {"choices":[{"index":0,"delta":{"content":"Generated"},"finish_reason":null}]}\n\n')
        await resp.write(b'data: {"choices":[{"index":0,"delta":{"content":" Title"},"finish_reason":null}]}\n\n')
        await resp.write(b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = cast(Any, site._server).sockets if site._server is not None else []
    assert sockets
    port = sockets[0].getsockname()[1]

    try:
        tm = TitleManager()
        ai_socket = f"http://127.0.0.1:{port}"
        title = await tm.generate("sess1", ai_socket, "User text", "Assistant text")
        assert title == "Generated Title"
        assert tm.get_all() == {"sess1": "Generated Title"}
    finally:
        await runner.cleanup()

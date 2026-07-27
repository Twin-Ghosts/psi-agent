from __future__ import annotations

import socket
from typing import Any

import anyio
import pytest
from aiohttp import web

from psi_agent.session.ai_client import AiClient
from psi_agent.session.protocol import AiDelta


@pytest.mark.anyio
async def test_ai_client_parses_compaction_signal() -> None:
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
        await resp.write(
            b'data: {"id":"test","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":"stop"}],"created":0,"model":"test","object":"chat.completion.chunk"}\n\n'
        )
        await resp.write(
            b'data: {"id":"compaction","choices":[{"index":0,"delta":{},"finish_reason":"compaction_needed"}],"psi_compaction":{"needed":true,"prompt_tokens":50000,"threshold":10000}}\n\n'
        )
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    site = web.SockSite(runner, s)
    await site.start()
    await anyio.sleep(0.05)

    try:
        client = AiClient(ai_socket=f"http://127.0.0.1:{port}")
        deltas: list[AiDelta] = []
        async for d in client.stream({"messages": [{"role": "user", "content": "hi"}], "stream": True}):
            deltas.append(d)

        assert len(deltas) >= 2
        assert deltas[0].content == "Hi"
        assert deltas[0].finish_reason == "stop"
        assert deltas[0].compaction_needed is False
        assert deltas[1].compaction_needed is True
        assert deltas[1].finish_reason == "compaction_needed"
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_ai_client_no_compaction_when_field_absent() -> None:
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
        await resp.write(
            b'data: {"id":"test","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":"stop"}],"created":0,"model":"test","object":"chat.completion.chunk"}\n\n'
        )
        return resp

    app = web.Application()
    app.router.add_post("/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    site = web.SockSite(runner, s)
    await site.start()
    await anyio.sleep(0.05)

    try:
        client = AiClient(ai_socket=f"http://127.0.0.1:{port}")
        deltas: list[AiDelta] = []
        async for d in client.stream({"messages": [{"role": "user", "content": "hi"}], "stream": True}):
            deltas.append(d)

        assert len(deltas) == 1
        assert deltas[0].content == "Hi"
        assert deltas[0].finish_reason == "stop"
        assert deltas[0].compaction_needed is False
    finally:
        await runner.cleanup()

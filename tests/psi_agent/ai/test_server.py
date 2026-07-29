from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import ClientSession, ClientTimeout, web

from psi_agent.ai.server import handle_chat_completions


class _FakeChunk:
    """Minimal stand-in for an any-llm ChatCompletionChunk."""

    def model_dump_json(self) -> str:
        return json.dumps({"id": "x", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})


class _TrackingStream:
    """Async iterator that records whether ``aclose()`` was awaited."""

    def __init__(self, chunks: list[Any], *, raise_after: int | None = None) -> None:
        self._chunks = list(chunks)
        self._i = 0
        self._raise_after = raise_after
        self.closed = False

    def __aiter__(self) -> _TrackingStream:
        return self

    async def __anext__(self) -> Any:
        if self._raise_after is not None and self._i >= self._raise_after:
            raise RuntimeError("upstream boom")
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._i]
        self._i += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True


async def _serve_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: _TrackingStream,
    received_provider_kwargs: dict[str, Any] | None = None,
) -> tuple[web.AppRunner, str]:
    async def fake_acompletion(**kwargs: Any) -> _TrackingStream:
        if received_provider_kwargs is not None:
            received_provider_kwargs.update(kwargs)
        return stream

    monkeypatch.setattr("psi_agent.ai.server.acompletion", fake_acompletion)

    app = web.Application()
    app["provider"] = "openai"
    app["model"] = "test"
    app["api_key"] = "k"
    app["base_url"] = "http://upstream"
    app.router.add_post("/chat/completions", handle_chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    site = web.SockSite(runner, sock)
    await site.start()
    await anyio.sleep(0.1)
    return runner, f"http://127.0.0.1:{sock.getsockname()[1]}"


async def _drain(socket_path: str) -> None:
    body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    timeout = ClientTimeout(total=5)
    async with (
        ClientSession(timeout=timeout) as s,
        s.post(f"{socket_path}/chat/completions", json=body) as resp,
    ):
        assert resp.status == 200
        async for _ in resp.content:
            pass


@pytest.mark.anyio
async def test_upstream_stream_closed_after_normal_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream stream must be closed once the handler finishes streaming."""
    stream = _TrackingStream([_FakeChunk()])
    runner, socket_path = await _serve_handler(tmp_path, monkeypatch, stream)
    try:
        await _drain(socket_path)
        await anyio.sleep(0.05)
        assert stream.closed is True
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_upstream_stream_closed_after_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream stream must be closed even when iteration raises mid-stream."""
    stream = _TrackingStream([_FakeChunk()], raise_after=1)
    runner, socket_path = await _serve_handler(tmp_path, monkeypatch, stream)
    try:
        await _drain(socket_path)
        await anyio.sleep(0.05)
        assert stream.closed is True
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_handler_strips_internal_routing_before_calling_the_external_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider-facing calls must never receive the Router's Session metadata."""

    received_provider_kwargs: dict[str, Any] = {}
    stream = _TrackingStream([_FakeChunk()])
    runner, socket_path = await _serve_handler(tmp_path, monkeypatch, stream, received_provider_kwargs)
    try:
        async with (
            ClientSession() as session,
            session.post(
                f"{socket_path}/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "routing": {"session_id": "private-session"},
                    "temperature": 0.2,
                },
            ) as response,
        ):
            assert response.status == 200
            async for _ in response.content:
                pass
    finally:
        await runner.cleanup()

    assert "routing" not in received_provider_kwargs
    assert received_provider_kwargs["temperature"] == 0.2


@pytest.mark.anyio
async def test_upstream_retry_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler should retry on upstream acompletion failure and succeed if subsequent retry succeeds."""
    call_count = 0
    stream = _TrackingStream([_FakeChunk()])

    async def fake_acompletion(**kwargs: Any) -> _TrackingStream:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("Upstream temporary fail")
        return stream

    monkeypatch.setattr("psi_agent.ai.server.acompletion", fake_acompletion)

    app = web.Application()
    app["provider"] = "openai"
    app["model"] = "test"
    app["api_key"] = "k"
    app["base_url"] = "http://upstream"
    app["max_context_tokens"] = 0
    app.router.add_post("/chat/completions", handle_chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    site = web.SockSite(runner, sock)
    await site.start()
    await anyio.sleep(0.1)

    socket_path = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        await _drain(socket_path)
        assert call_count == 2
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_upstream_retry_failure_streams_error_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If all retry attempts fail, the handler should return an HTTP 200 and stream an SSE error chunk."""
    call_count = 0

    async def fake_acompletion(**kwargs: Any) -> _TrackingStream:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Upstream permanent fail")

    monkeypatch.setattr("psi_agent.ai.server.acompletion", fake_acompletion)

    app = web.Application()
    app["provider"] = "openai"
    app["model"] = "test"
    app["api_key"] = "k"
    app["base_url"] = "http://upstream"
    app["max_context_tokens"] = 0
    app.router.add_post("/chat/completions", handle_chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    site = web.SockSite(runner, sock)
    await site.start()
    await anyio.sleep(0.1)

    socket_path = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        async with (
            ClientSession() as s,
            s.post(f"{socket_path}/chat/completions", json=body) as resp,
        ):
            assert resp.status == 200
            chunks = []
            async for raw_line in resp.content:
                line = raw_line.decode().strip()
                if line.startswith("data: "):
                    chunks.append(json.loads(line[6:]))
            assert len(chunks) == 1
            error_chunk = chunks[0]
            assert error_chunk["choices"][0]["finish_reason"] == "error"
            assert "[Upstream Error]" in error_chunk["choices"][0]["delta"]["content"]
        assert call_count == 3  # Initial attempt + 2 retries = 3 total attempts
    finally:
        await runner.cleanup()

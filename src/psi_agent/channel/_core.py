from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
import anyio
from aiohttp import ClientTimeout
from loguru import logger

from psi_agent._sockets import resolve_connector_and_endpoint
from psi_agent.channel._errors import ChannelError
from psi_agent.channel._markers import SendMarkerScanner, encode_input
from psi_agent.channel._stream import StreamBuffer, iter_sse_events
from psi_agent.channel._types import FileChunk, InputChunk, OutputChunk, ReasoningChunk, TextChunk

_CHAT_PATH = "/chat/completions"
_EVENTS_PATH = "/events"


class _Idle(Enum):
    """「上游静默了」这一个事实的类型。

    用 Enum 单例而不是 ``object()``: 前者能让 ``delta is _IDLE`` 之后的分支被类型检查器
    收窄成 ``dict``, 后者不行 (ty/mypy 都只认 Enum 与 None 这类字面量单例)。
    """

    IDLE = "idle"


_IDLE = _Idle.IDLE


@dataclass
class ChannelCore:
    session_socket: str
    interval: float = 1.0
    idle_drain: float = 5.0
    """上游静默这么多秒后, 把缓冲里的尾巴先发出去 (0 或负数 = 关掉)。

    ``StreamBuffer`` 的窗口是惰性的 —— 只在下一个 delta 到达时才检查。上游在回复末尾
    长时间不出字时 (实测 deepseek 停 50-70 秒才发 ``[DONE]``), 最后攒的那一段就一直
    卡在缓冲里, 用户看到的是一句话断在中间。这个超时让尾巴按秒级送达, 而不是跟着上游
    的停顿走。``interval=0`` 的终端通道本就每个 token 直出, 与此无关。
    """

    @staticmethod
    def _to_chunk(kind: str, text: str) -> OutputChunk:
        # Buffer keys: "text" | "reasoning" | "reasoning:<provenance>".
        if kind == "text" or not kind.startswith("reasoning"):
            return TextChunk(text)
        provenance = kind.split(":", 1)[1] if ":" in kind else None
        return ReasoningChunk(text=text, kind=provenance or None)

    @property
    def _byte_source(self) -> str:
        """出向文件的字节该从哪儿取; 本地 Session 返回 ``""``。

        只有 TCP 地址才填 —— 那是「Session 在另一个容器」的形态 (见生产
        ``PSI_FEISHU_EXTERNAL_SESSIONS``)。Unix socket / 命名管道意味着同机同文件系统,
        此时路径本就可读, 填地址只会让客户端多绕一趟 HTTP 去拿它已经能直接读的字节。
        """
        if self.session_socket.startswith(("http://", "https://")):
            return self.session_socket.rstrip("/")
        return ""

    @staticmethod
    def events_endpoint_from_chat(chat_endpoint: str) -> str:
        """Derive ``…/events`` from the chat-completions endpoint on the same socket."""
        if chat_endpoint.endswith(_CHAT_PATH):
            return chat_endpoint[: -len(_CHAT_PATH)] + _EVENTS_PATH
        return chat_endpoint.rstrip("/") + _EVENTS_PATH

    async def __aenter__(self) -> ChannelCore:
        connector, self._endpoint = resolve_connector_and_endpoint(self.session_socket)
        self._session = aiohttp.ClientSession(connector=connector, timeout=ClientTimeout(total=None))
        return self

    async def __aexit__(self, *args: object) -> None:
        with anyio.CancelScope(shield=True):
            await self._session.close()

    async def post_event(self, envelope: dict[str, object]) -> dict[str, object]:
        """POST a Channel-built envelope to Session ``/events`` (unified forward).

        Returns the JSON body (``ok`` / ``matched`` / ``fired``). Raises
        ``ChannelError`` on non-2xx or invalid JSON.
        """
        url = self.events_endpoint_from_chat(self._endpoint)
        logger.debug(f"POST {url} event={envelope.get('event')!r}")
        async with self._session.post(url, json=envelope) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.warning(f"POST /events HTTP {resp.status}: {text[:500]!r}")
                raise ChannelError(f"POST /events HTTP {resp.status}: {text[:500]}")
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError as e:
                raise ChannelError(f"POST /events invalid JSON: {e}") from e
            if not isinstance(data, dict):
                raise ChannelError("POST /events response must be a JSON object")
            logger.info(
                f"POST /events ok event={envelope.get('event')!r} "
                f"matched={data.get('matched')} fired={data.get('fired')!r}"
            )
            return data

    async def _iter_deltas(self, lines: AsyncIterable[bytes]) -> AsyncGenerator[dict[str, Any] | _Idle]:
        """Yield SSE deltas, injecting ``_IDLE`` when the upstream goes quiet.

        **Why a pump task instead of a timeout on the parser.** Wrapping
        ``iter_sse_events.__anext__()`` in ``anyio.fail_after`` would cancel the read
        *inside* the generator and tear the whole stream down — the same trap
        documented in ``gateway.server._write_chat_sse_with_keepalive``: the caller
        gets an early end while Session is still waiting on the model, and the reply
        is never finished. So the parser runs in its own task and pushes into a
        memory stream; the timeout waits on the **queue**, where expiry costs nothing
        but a tick. ``_IDLE`` is a sentinel object rather than ``None`` because a
        delta can legitimately be an empty dict.

        The pump is skipped — degrading to a plain ``async for`` over the parser,
        byte-for-byte the old path — when either ``idle_drain <= 0`` (opt-out) or
        ``interval <= 0``. The latter matters: unbuffered callers (CLI / REPL and the
        Gateway chat bridge) emit every token as it arrives, so nothing is ever left
        in the buffer for an idle tick to drain and the task would be pure overhead.
        Only the batching channels (Feishu, Telegram) take the pump path.
        """
        if self.idle_drain <= 0 or self.interval <= 0:
            async with aclosing(iter_sse_events(lines)) as events:
                logger.debug("Starting to consume SSE stream")
                async for delta in events:
                    yield delta
            return

        send, recv = anyio.create_memory_object_stream[dict[str, Any]](64)

        async def pump() -> None:
            async with send, aclosing(iter_sse_events(lines)) as events:
                async for delta in events:
                    await send.send(delta)

        # 解析器的异常 (ChannelError: 坏 choices / finish_reason=error) 现在从 pump 任务里
        # 抛出, 会被 task group 包成 ExceptionGroup。调用方与既有测试都按裸 ChannelError
        # 接, 所以单个异常的组要拆开原样重抛 —— 否则 `except ChannelError` 全部漏接。
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(pump)
                logger.debug("Starting to consume SSE stream")
                async with recv:
                    while True:
                        try:
                            with anyio.fail_after(self.idle_drain):
                                delta = await recv.receive()
                        except TimeoutError:
                            yield _IDLE
                            continue
                        except anyio.EndOfStream:
                            break
                        yield delta
        except BaseExceptionGroup as eg:
            if len(eg.exceptions) == 1:
                raise eg.exceptions[0] from None
            raise

    async def post(self, chunks: list[InputChunk]) -> AsyncGenerator[OutputChunk]:
        logger.debug(
            f"{len(chunks)} chunk(s) — "
            f"FileChunks={sum(1 for c in chunks if isinstance(c, FileChunk))} "
            f"TextChunks={sum(1 for c in chunks if isinstance(c, TextChunk))}"
        )

        content = encode_input(chunks)
        body = {"messages": [{"role": "user", "content": content}], "stream": True}

        buffer = StreamBuffer(self.interval)
        scanner = SendMarkerScanner()

        logger.debug(f"POST {self._endpoint} content_len={len(content)}")
        async with self._session.post(self._endpoint, json=body) as resp:
            logger.info(f"HTTP {resp.status}")

            if resp.status != 200:
                msg = await resp.text()
                try:
                    error = json.loads(msg)
                    msg = error.get("error", {}).get("message", msg)
                except Exception:
                    pass
                logger.debug(f"non-200 error: {msg!r}")
                raise ChannelError(msg)

            async for delta in self._iter_deltas(resp.content):
                if delta is _IDLE:
                    for k, t in buffer.drain_if_idle():
                        yield self._to_chunk(k, t)
                    continue

                reasoning_text = delta.get("reasoning") or ""
                content_text = delta.get("content") or ""
                raw_kind = delta.get("kind")
                reasoning_buf_kind = "reasoning"
                if reasoning_text and isinstance(raw_kind, str) and raw_kind.strip():
                    reasoning_buf_kind = f"reasoning:{raw_kind.strip()}"

                for incoming_kind, text in (
                    (reasoning_buf_kind, reasoning_text),
                    ("text", content_text),
                ):
                    if not text:
                        continue

                    for k, t in buffer.switch(incoming_kind):
                        yield self._to_chunk(k, t)

                    if incoming_kind == "text":
                        logger.debug(f"delta.content ({len(text)} chars): {text[:1000]!r}")
                        for file_chunk in scanner.feed(text):
                            # 跨容器时补上取字节的地址; 本地留空 → 客户端照旧直接读路径。
                            # 填在这里而不是 scanner 里: scanner 是纯解码, 不该知道传输地址。
                            file_chunk.source = self._byte_source
                            yield file_chunk
                    else:
                        logger.debug(f"delta.reasoning kind={raw_kind!r} ({len(text)} chars): {text[:1000]!r}")

                    for k, t in buffer.append(text):
                        yield self._to_chunk(k, t)

        logger.debug("SSE stream consumed successfully")
        for k, t in buffer.flush():
            yield self._to_chunk(k, t)

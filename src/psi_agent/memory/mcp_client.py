"""Small official MCP SDK client for the memory service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from psi_agent.memory.models import CompletedTurnInput


class MemoryMcpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "unavailable",
        retryable: bool = True,
        detail_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail_ids = detail_ids


@dataclass(frozen=True, slots=True)
class MemoryMcpClient:
    url: str
    token: str = ""
    timeout_seconds: float = 30.0

    async def add_batch(self, idempotency_key: str, turns: tuple[CompletedTurnInput, ...]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            timeout = httpx.Timeout(self.timeout_seconds)
            async with (
                httpx.AsyncClient(headers=headers, timeout=timeout) as http_client,
                streamable_http_client(self.url, http_client=http_client) as streams,
            ):
                read_stream, write_stream, _session_id = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await self._call(
                        session,
                        {
                            "idempotency_key": idempotency_key,
                            "turns": [turn.to_wire() for turn in turns],
                        },
                    )
        except MemoryMcpError:
            raise
        except Exception as error:
            raise MemoryMcpError("memory service is unavailable", code="unavailable", retryable=True) from error
        return self._parse_result(result)

    async def aclose(self) -> None:
        return None

    async def _call(self, session: ClientSession, arguments: dict[str, Any]) -> Any:
        return await session.call_tool("memory_add_batch", arguments)

    @staticmethod
    def _parse_result(result: Any) -> dict[str, Any]:
        structured = getattr(result, "structuredContent", None)
        if not isinstance(structured, dict):
            raise MemoryMcpError(
                "memory service returned no structured response",
                code="integrity_error",
                retryable=False,
            )
        if structured.get("ok") is False:
            error = structured.get("error")
            if not isinstance(error, dict):
                raise MemoryMcpError(
                    "memory service returned an invalid error",
                    code="integrity_error",
                    retryable=False,
                )
            code = error.get("code")
            if not isinstance(code, str):
                raise MemoryMcpError(
                    "memory service returned an invalid error code",
                    code="integrity_error",
                    retryable=False,
                )
            detail_ids = error.get("detail_ids", ())
            raise MemoryMcpError(
                str(error.get("message", "memory operation failed")),
                code=code,
                retryable=bool(error.get("retryable", False)),
                detail_ids=tuple(item for item in detail_ids if isinstance(item, str)),
            )
        data = structured.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("receipt_id"), str):
            raise MemoryMcpError("memory service returned an invalid receipt", code="integrity_error", retryable=False)
        return data

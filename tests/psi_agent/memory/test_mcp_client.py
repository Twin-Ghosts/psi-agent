import pytest

from psi_agent.memory.mcp_client import MemoryMcpClient, MemoryMcpError


@pytest.mark.anyio
async def test_structured_failure_is_exposed_as_typed_memory_error(monkeypatch) -> None:
    result = type(
        "Result",
        (),
        {
            "isError": False,
            "structuredContent": {
                "ok": False,
                "error": {"code": "conflict", "message": "x", "retryable": False},
            },
        },
    )()

    with pytest.raises(MemoryMcpError) as exc_info:
        MemoryMcpClient._parse_result(result)
    assert exc_info.value.code == "conflict"

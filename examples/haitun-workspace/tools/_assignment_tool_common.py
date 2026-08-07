from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
_mcp_path = TOOLS_DIR / "_fusion_memory_mcp.py"
_mcp_module_name = f"fusion_memory_tool__fusion_memory_mcp_{hashlib.sha256(str(_mcp_path).encode()).hexdigest()[:12]}"
_mcp_module = sys.modules.get(_mcp_module_name)
if _mcp_module is None:
    _mcp_module = types.ModuleType(_mcp_module_name)
    _mcp_module.__file__ = str(_mcp_path)
    sys.modules[_mcp_module_name] = _mcp_module
    exec(compile(_mcp_path.read_text(encoding="utf-8"), str(_mcp_path), "exec"), _mcp_module.__dict__)
CLIENT = _mcp_module.__dict__["CLIENT"]


def dumps_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def parse_json_object(raw: str, field_name: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"{field_name} must be a JSON object string"
    if not isinstance(payload, dict):
        return None, f"{field_name} must be a JSON object"
    return payload, None


def invalid_argument(message: str) -> str:
    return dumps_result(
        {
            "ok": False,
            "error": {"code": "invalid_argument", "message": message, "retryable": False},
        }
    )


def result_object(result: dict[str, Any]) -> dict[str, Any] | None:
    """Unwrap a Fusion Memory payload out of the two envelopes it arrives in.

    ``CLIENT.call_tool`` wraps the MCP transport result, and the Memory tool wraps
    its own ``{"ok", "result"}`` inside that, so a payload sits two levels down:
    ``{"ok": True, "result": {"ok": True, "result": {...}}}``. Peeling only one
    level yields the inner envelope, whose ``state`` is absent — which read as
    ``assignment_state_invalid`` for records that were plainly ``assigned``.

    A payload that is not itself an envelope is returned as-is, so single-envelope
    tools keep working. A failed inner envelope is reported as absent, letting
    callers surface the error instead of treating ``{"ok": False, ...}`` as data.

    Use :func:`result_object_or_reason` when the caller reports the failure to a
    user: this function collapses three distinct causes into one ``None``.
    """
    return result_object_or_reason(result)[0]


def result_object_or_reason(result: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Unwrap like :func:`result_object`, also returning *why* the unwrap failed.

    ``None`` alone cannot be reported honestly: the payload may be a non-object (the
    transport degraded a reply to text), or an inner envelope may carry its own error,
    or a successful inner envelope may hold a non-object. Callers that turned all three
    into one generic "returned an invalid record" message hid the server's real error —
    an operator then had no way to tell a transport fault from a rejected request.

    The reason is a human-readable clause meant to be appended to a caller's message,
    never a machine-readable code.
    """
    payload = result.get("result")
    if not isinstance(payload, dict):
        if payload is None:
            return None, "the response carried no payload"
        excerpt = str(payload).strip().replace("\n", " ")
        if len(excerpt) > 200:
            excerpt = excerpt[:200] + "…"
        return None, f"the response was not an object but {type(payload).__name__}: {excerpt!r}"
    if "ok" not in payload and "result" not in payload:
        return payload, None
    if payload.get("ok") is not True:
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            detail = " ".join(str(part) for part in (code, message) if part)
            return None, f"the response reported an error: {detail or error!r}"
        return None, f"the response reported failure without an error object: {payload!r}"
    inner = payload.get("result")
    if isinstance(inner, dict):
        return inner, None
    if inner is None:
        return None, "the response succeeded but carried no record"
    return None, f"the response succeeded but its record was {type(inner).__name__}, not an object"


def bounded_limit(value: int) -> int:
    try:
        return max(1, min(50, int(value)))
    except TypeError:
        return 20
    except ValueError:
        return 20

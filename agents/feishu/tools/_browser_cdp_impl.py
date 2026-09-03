"""Private helper for the ``browser_cdp`` tool — raw Chrome DevTools Protocol.

Sends one raw CDP command (``{"method": ..., "params": ...}``) to a Chromium browser and
returns its reply. CDP is JSON over a WebSocket, so this module needs:

1. **A browser with the debugging port open.** ``browser_cdp`` shares the *single*
   process browser owned by :mod:`_browser_shared` — the same window the Playwright-MCP
   ``browser_*`` tools drive via ``--cdp-endpoint``. Set ``CDP_ENDPOINT`` to attach to an
   already-running browser instead (``http://host:port`` or ``ws(s)://``); then nothing is
   launched or managed here.
2. **The WebSocket debugger URL.** Chromium exposes it over HTTP (``/json/version``).
   We fetch it and talk to it over a WebSocket via :mod:`aiohttp`.

User-close contract: if the user closes the shared browser, the first call afterwards
surfaces the clear ``_browser_shared.CLOSED_MESSAGE`` instead of relaunching; the next
call relaunches.
"""

from __future__ import annotations

import json
import os
from itertools import count
from typing import Any

import _browser_shared as _shared
import aiohttp
import anyio

_CDP_ENDPOINT_ENV = "CDP_ENDPOINT"  # if set, connect here; do not manage a browser
_DEFAULT_COMMAND_TIMEOUT = float(os.environ.get("CDP_COMMAND_TIMEOUT", "30"))
_ids = count(1)


class CDPError(RuntimeError):
    """Raised when a CDP command fails or the shared browser cannot be reached."""


async def _ws_from_origin(origin: str) -> str:
    """Fetch the browser-level WebSocket debugger URL from an HTTP origin."""
    url = f"{origin.rstrip('/')}/json/version"
    timeout = aiohttp.ClientTimeout(total=5.0)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            if resp.status != 200:
                return ""
            data = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise CDPError(f"Could not read CDP endpoint at {url}: {exc}") from exc
    ws = data.get("webSocketDebuggerUrl")
    return str(ws) if ws else ""


async def ensure_endpoint() -> str:
    """Return a browser-level WS debugger URL.

    With ``CDP_ENDPOINT`` set, resolve it without managing anything. Otherwise ensure the
    shared browser (launch once, reuse; user-close aware — see :mod:`_browser_shared`)
    and return its WebSocket debugger URL.
    """
    override = os.environ.get(_CDP_ENDPOINT_ENV, "").strip()
    if override:
        return await _resolve_override(override)
    try:
        origin = await anyio.to_thread.run_sync(_shared.ensure_origin)
    except _shared.BrowserUnavailableError as exc:
        # User closed the browser mid-task: surface the clear message, do NOT relaunch.
        raise CDPError(str(exc)) from exc
    ws = await _ws_from_origin(origin)
    if not ws:
        raise CDPError("Shared browser is up but did not expose a debugger endpoint.")
    return ws


async def _resolve_override(value: str) -> str:
    """Turn a ``CDP_ENDPOINT`` value into a WebSocket debugger URL."""
    if value.startswith(("ws://", "wss://")):
        return value
    if value.startswith(("http://", "https://")):
        base = value.rstrip("/")
        url = f"{base}/json/version"
        timeout = aiohttp.ClientTimeout(total=5.0)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url) as resp,
            ):
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise CDPError(f"Could not read CDP endpoint at {url}: {exc}") from exc
        ws = data.get("webSocketDebuggerUrl")
        if not ws:
            raise CDPError(f"No webSocketDebuggerUrl at {url}.")
        return str(ws)
    # Bare host:port -> treat as http.
    return await _resolve_override(f"http://{value}")


async def _first_page_ws(base_http: str) -> str | None:
    """Return the WS debugger URL of the first ``page`` target, if any."""
    url = f"{base_http.rstrip('/')}/json"
    timeout = aiohttp.ClientTimeout(total=5.0)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(url) as resp,
        ):
            targets = await resp.json()
    except aiohttp.ClientError, TimeoutError, ValueError:
        return None
    if not isinstance(targets, list):
        return None
    for t in targets:
        if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            return str(t["webSocketDebuggerUrl"])
    return None


def _http_origin(ws_url: str) -> str:
    """Best-effort HTTP origin (``http://host:port``) from a ``ws://host:port/...`` URL."""
    rest = ws_url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    return f"http://{authority}"


async def send_command(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    target: str = "page",
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Send one raw CDP command and return the parsed result dict.

    ``target`` selects the WebSocket endpoint: ``"page"`` (first open page target; the
    default for ``Page.*``/``Runtime.*``/``DOM.*``/``Network.*``) or ``"browser"`` (the
    browser-level endpoint for ``Browser.*``/``Target.*``/``SystemInfo.*``).

    Returns ``{"ok": True, "method", "result"}`` on success, or
    ``{"ok": False, "method", "error"}`` on a CDP-reported error.
    """
    if not method or not isinstance(method, str):
        raise CDPError("A non-empty CDP 'method' string is required (e.g. 'Page.navigate').")

    browser_ws = await ensure_endpoint()
    ws_url = browser_ws
    if target == "page":
        page_ws = await _first_page_ws(_http_origin(browser_ws))
        if page_ws:
            ws_url = page_ws

    request_id = next(_ids)
    payload = json.dumps({"id": request_id, "method": method, "params": params or {}})
    total = timeout_s if timeout_s and timeout_s > 0 else _DEFAULT_COMMAND_TIMEOUT
    timeout = aiohttp.ClientTimeout(total=total + 5.0)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.ws_connect(ws_url) as ws:
            await ws.send_str(payload)
            with anyio.fail_after(total):
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            raise CDPError("CDP WebSocket closed before a reply arrived.")
                        continue
                    data = json.loads(msg.data)
                    if data.get("id") != request_id:
                        continue
                    if "error" in data:
                        return {"ok": False, "method": method, "error": data["error"]}
                    return {"ok": True, "method": method, "result": data.get("result", {})}
            raise CDPError(f"Timed out after {total:.0f}s waiting for a reply to {method!r}.")
    except TimeoutError:
        raise CDPError(f"Timed out after {total:.0f}s waiting for a reply to {method!r}.") from None
    except aiohttp.ClientError as exc:
        # The browser may have been closed by the user mid-command; the agent should
        # tell them rather than retry blindly into a relaunch.
        raise CDPError(
            f"CDP WebSocket request failed: {type(exc).__name__}: {exc}. "
            "若浏览器窗口是被你关闭的, 请告诉用户并等他确认后再继续。"
        ) from exc

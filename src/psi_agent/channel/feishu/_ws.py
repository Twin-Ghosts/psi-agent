"""Running Feishu's long-polling WebSocket from inside an anyio task.

``lark_oapi.ws.Client`` exists and does the hard parts — handshake, endpoint discovery,
ping, frame reassembly, reconnect with backoff — but its only public entry point is
``start()``, which is synchronous: it calls ``loop.run_until_complete`` on a module-level
event loop and then blocks forever in a select. This package is anyio end to end, so
calling it would either block the whole channel or, run in a thread, put every event
handler on a loop the rest of the code can't await into.

So the connection is driven from its own coroutines instead of through ``start()``:

* ``_connect()`` does the handshake, then schedules the receive loop with
  ``loop.create_task`` — using the module-level ``loop`` captured at *import* time, not
  the running one. Under anyio those differ, and the receive loop would be scheduled
  onto a loop that never runs, so the bot would connect and then hear nothing. Rebinding
  that module attribute to the running loop before connecting is what makes the SDK's
  own internals land in the right place.
* ``_ping_loop()`` is kept alive alongside it — without pings Feishu drops the
  connection after roughly two minutes of silence.
* Reconnects are watched rather than assumed: the SDK sets ``_conn`` to ``None`` when a
  connection dies, so a supervisor polls for that and calls the SDK's own reconnect,
  which keeps its backoff and endpoint-rediscovery behaviour.

Everything here is about *transport*. What to do with a decoded event is the dispatcher's
job, and that stays in ``lark_oapi.event``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import anyio
from loguru import logger

# How often the supervisor checks whether the connection went away. The SDK reconnects
# internally on a dead read; this only covers the case where the connection is gone and
# nothing is driving recovery.
_SUPERVISE_INTERVAL_S = 5.0


class WebSocketRunner:
    """Owns one ``lark_oapi.ws.Client`` and keeps it connected for the task's lifetime."""

    def __init__(self, ws_client: Any) -> None:
        self._ws = ws_client
        self._started = anyio.Event()

    async def wait_connected(self, *, wait_seconds: float = 30.0) -> bool:
        """Whether the first handshake completed within ``wait_seconds``.

        Returns a bool rather than raising on timeout: a slow handshake is not fatal —
        the runner keeps retrying — so the caller logs and carries on instead of aborting
        startup.
        """
        with anyio.move_on_after(wait_seconds):
            await self._started.wait()
            return True
        return False

    async def run(self, *, task_status: Any = None) -> None:
        """Connect and stay connected until cancelled.

        Intended to be run in a task group: cancelling the group closes the socket. The
        first successful handshake sets the connected event, so callers can wait for
        readiness instead of sleeping.
        """
        import lark_oapi.ws.client as ws_module  # noqa: PLC0415

        # See the module docstring: the SDK's internals schedule onto this attribute.
        ws_module.loop = asyncio.get_running_loop()

        await self._ws._connect()
        logger.info("Feishu WebSocket connected")
        self._started.set()
        if task_status is not None:
            task_status.started()

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._ping_forever)
                tg.start_soon(self._supervise)
                await anyio.sleep_forever()
        finally:
            with anyio.CancelScope(shield=True):
                await self._close()

    async def _ping_forever(self) -> None:
        """Keep the SDK's ping loop running; restart it if it ever returns.

        The SDK's own loop swallows per-ping errors, so a return means something
        structural — reconnecting is handled by the supervisor, and this just makes sure
        pings resume afterwards rather than staying silently stopped.
        """
        while True:
            try:
                await self._ws._ping_loop()
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as exc:
                logger.debug(f"Feishu ping loop ended — {exc!r}")
            await anyio.sleep(_SUPERVISE_INTERVAL_S)

    async def _supervise(self) -> None:
        """Reconnect through the SDK when the connection is found to be gone."""
        while True:
            await anyio.sleep(_SUPERVISE_INTERVAL_S)
            if getattr(self._ws, "_conn", None) is not None:
                continue
            logger.warning("Feishu WebSocket connection lost — reconnecting")
            try:
                await self._ws._reconnect()
                logger.info("Feishu WebSocket reconnected")
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as exc:
                # Keep looping: the next tick retries. Giving up here would leave the bot
                # silently offline with no path back.
                logger.warning(f"Feishu WebSocket reconnect failed — {exc!r}")

    async def _close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws._disconnect()
        logger.debug("Feishu WebSocket closed")

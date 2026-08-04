"""The channel object: one Feishu app, its event subscriptions, and its send methods.

This is what ``FeishuChannel`` was providing. It exists because ``lark_oapi`` stops at
"here is every API and here is a WebSocket": it has no notion of *a bot* that owns an
identity, dispatches inbound messages through a policy gate, and streams a reply back.

Deliberately scoped to what this package actually uses, so the surface is a handful of
methods rather than a re-implementation of a general-purpose channel:

* ``on(event, handler)`` — subscribe to ``message`` / ``cardAction`` / ``comment`` /
  ``reject``. Callbacks are invoked from the SDK's dispatcher, which runs on the
  WebSocket's loop; callers hop onto their own task via a portal.
* ``send`` / ``stream`` / ``update_card`` — outbound, delegated to ``FeishuSender``.
* ``bot_identity`` — resolved once at startup because the group policy gate can't detect
  an @-mention without the bot's own open_id.

The dispatcher is built once, before connecting: ``lark_oapi`` bakes the handler table
into the ws client at construction, so a handler registered afterwards would never fire.
That ordering constraint is why ``on()`` must be called before ``start()``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from lark_oapi.api.im.v1 import (
    GetMessageRequest,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
)
from loguru import logger

from ._inbound import InboundMessage, PolicyConfig, normalize_message, policy_allows
from ._outbound import FeishuSender, MarkdownStream, SendResult
from ._ws import WebSocketRunner

# Event names this channel accepts, mirroring what the previous SDK exposed so the
# calling code's ``channel.on(...)`` sites keep working unchanged.
_EVENTS = frozenset({"message", "cardAction", "comment", "reject"})


@dataclass
class BotIdentity:
    """The bot's own identity — needed to recognise being @-mentioned."""

    open_id: str = ""
    name: str = ""


class FeishuChannel:
    """One Feishu bot: receives events, applies policy, sends replies."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        policy: PolicyConfig | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._policy = policy or PolicyConfig()
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._bot_identity: BotIdentity | None = None
        self._runner: WebSocketRunner | None = None

        from lark_oapi.client import Client  # noqa: PLC0415

        self._client = Client.builder().app_id(app_id).app_secret(app_secret).build()
        self._sender = FeishuSender(self._client)
        self._dispatcher: Any = None
        self._ws: Any = None

    # ---- properties --------------------------------------------------------

    @property
    def client(self) -> Any:
        """The underlying ``lark_oapi`` client, for callers that need a raw API call."""
        return self._client

    @property
    def sender(self) -> FeishuSender:
        return self._sender

    @property
    def bot_identity(self) -> BotIdentity | None:
        return self._bot_identity

    @property
    def dispatcher(self) -> Any:
        """The SDK event dispatcher — exposed so agent-event defs can add processors."""
        return self._dispatcher

    # ---- subscriptions -----------------------------------------------------

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        """Register a handler. Must be called before :meth:`start`."""
        if event not in _EVENTS:
            raise ValueError(f"unknown Feishu channel event {event!r}; expected one of {sorted(_EVENTS)}")
        self._handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, *args: Any) -> None:
        """Invoke every handler for ``event``, never letting one break the dispatcher.

        Handlers here are the sync shims that schedule real work onto the anyio portal;
        an exception in one would otherwise propagate into the SDK's receive loop and
        take the WebSocket down with it.
        """
        for handler in self._handlers.get(event, []):
            try:
                result = handler(*args)
                if hasattr(result, "__await__"):
                    # A coroutine returned from a dispatcher thread has no loop to run
                    # on. Callers are expected to register sync shims; close it so it
                    # doesn't leak, and say so loudly.
                    result.close()
                    logger.warning(f"{event} handler returned a coroutine; register a sync shim instead")
            except Exception as exc:
                logger.warning(f"{event} handler failed — {exc!r}")

    # ---- inbound wiring ----------------------------------------------------

    def _build_dispatcher(self) -> Any:
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler  # noqa: PLC0415

        builder = EventDispatcherHandler.builder("", "")
        builder = builder.register_p2_im_message_receive_v1(self._on_message_event)
        builder = builder.register_p2_card_action_trigger(self._on_card_action_event)
        return builder.build()

    def _on_message_event(self, data: Any) -> None:
        """Normalize, apply the policy gate, then hand off to the ``message`` handlers."""
        try:
            event = getattr(data, "event", None)
            bot_open_id = self._bot_identity.open_id if self._bot_identity else None
            msg = normalize_message(event, bot_open_id)
            rejected = policy_allows(msg, self._policy)
            if rejected is not None:
                self._emit("reject", rejected)
                return
            self._emit("message", msg)
        except Exception as exc:
            logger.warning(f"inbound message handling failed — {exc!r}")

    def _on_card_action_event(self, data: Any) -> Any:
        """Forward a card-button click to the ``cardAction`` handlers."""
        try:
            self._emit("cardAction", getattr(data, "event", None))
        except Exception as exc:
            logger.warning(f"card action handling failed — {exc!r}")
        return None

    # ---- lifecycle ---------------------------------------------------------

    async def start(self, *, task_group: Any, connect_wait_seconds: float = 30.0) -> None:
        """Resolve identity, connect, and leave the socket running in ``task_group``.

        Identity comes first because the policy gate needs the bot's open_id to tell an
        @-mention from an ordinary group message — connecting first would leave a window
        where group messages are all rejected.
        """
        await self.resolve_bot_identity()
        self._dispatcher = self._build_dispatcher()

        from lark_oapi.ws.client import Client as WsClient  # noqa: PLC0415

        self._ws = WsClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=self._dispatcher,
        )
        self._runner = WebSocketRunner(self._ws)
        await task_group.start(self._runner.run)
        if not await self._runner.wait_connected(wait_seconds=connect_wait_seconds):
            logger.warning(f"Feishu WebSocket not connected within {connect_wait_seconds}s — continuing to retry")

    async def resolve_bot_identity(self) -> BotIdentity | None:
        """Fetch the bot's own open_id/name via ``/bot/v3/info``.

        Not modelled as a typed resource in the SDK, so it goes through the raw-request
        escape hatch. A failure is a warning, not an error: the bot still answers DMs,
        it just can't recognise group @s — which is worth saying plainly in the log
        because the visible symptom ("ignores me in groups") looks unrelated.
        """
        from lark_oapi.core.enum import AccessTokenType, HttpMethod  # noqa: PLC0415
        from lark_oapi.core.model import BaseRequest  # noqa: PLC0415

        req = BaseRequest()
        req.http_method = HttpMethod.GET
        req.uri = "/open-apis/bot/v3/info"
        req.token_types = {AccessTokenType.TENANT}
        try:
            resp = await self._client.arequest(req)
        except Exception as exc:
            logger.warning(f"bot identity resolve failed — {exc!r}")
            return None

        raw = getattr(resp, "raw", None)
        content = getattr(raw, "content", None) if raw is not None else None
        if not content:
            logger.warning("bot identity response was empty")
            return None
        try:
            body = json.loads(bytes(content).decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            logger.warning("bot identity response was not JSON")
            return None
        bot = body.get("bot") if isinstance(body, dict) else None
        if not isinstance(bot, dict):
            logger.warning(f"bot identity response carried no bot object: {body}")
            return None
        self._bot_identity = BotIdentity(
            open_id=str(bot.get("open_id") or ""),
            name=str(bot.get("app_name") or ""),
        )
        return self._bot_identity

    # ---- outbound ----------------------------------------------------------

    async def send(self, to: str, spec: dict[str, Any], opts: dict[str, Any] | None = None) -> SendResult:
        """Send one message. ``spec`` is ``{"text":...}`` / ``{"image":{"source":path}}`` /
        ``{"file":{"source":path}}`` / ``{"card":{...}}`` — the shapes this package uses."""
        opts = opts or {}
        rit = str(opts.get("receive_id_type") or "")
        reply_to = str(opts.get("reply_to") or "")
        if "text" in spec:
            return await self._sender.send_text(to, str(spec["text"]), receive_id_type=rit, reply_to=reply_to)
        if "card" in spec:
            return await self._sender.send_card(to, dict(spec["card"]), receive_id_type=rit)
        for kind in ("image", "file"):
            if kind in spec:
                source = spec[kind].get("source") if isinstance(spec[kind], dict) else None
                if not source:
                    return SendResult(success=False, error=f"{kind} spec has no source")
                return await self._sender.send_file_or_image(to, str(source))
        return SendResult(success=False, error=f"unsupported send spec keys: {sorted(spec)}")

    async def stream(self, to: str, spec: dict[str, Any], opts: dict[str, Any] | None = None) -> SendResult:
        """Stream markdown into a card. ``spec`` is ``{"markdown": producer}``.

        ``producer`` is an async callable taking the stream handle; it appends text as the
        agent yields it. The card is finished even when the producer raises, so an
        interrupted answer doesn't sit there with a blinking cursor forever.
        """
        producer = spec.get("markdown")
        if producer is None:
            return SendResult(success=False, error="stream spec needs a 'markdown' producer")
        opts = opts or {}
        stream = MarkdownStream(
            self._sender,
            to=to,
            receive_id_type=str(opts.get("receive_id_type") or ""),
            reply_to=str(opts.get("reply_to") or ""),
        )
        await stream.start()
        try:
            await producer(stream)
        except Exception:
            await stream.close(interrupted=True)
            raise
        await stream.close()
        return SendResult(success=True, message_id=stream.message_id)

    async def update_card(self, message_id: str, card: dict[str, Any]) -> SendResult:
        """Replace a sent card's content in place (the message_id survives)."""
        content = json.dumps(card, ensure_ascii=False)
        try:
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(PatchMessageRequestBody.builder().content(content).build())
                .build()
            )
            resp = await self._client.im.v1.message.apatch(req)
        except Exception as exc:
            logger.warning(f"update_card {message_id} failed — {exc!r}")
            return SendResult(success=False, error=repr(exc))
        if not resp.success():
            return SendResult(success=False, error=f"code={resp.code} msg={resp.msg}")
        return SendResult(success=True, message_id=message_id)

    # ---- reads -------------------------------------------------------------

    async def fetch_message(self, message_id: str) -> dict[str, Any]:
        """Fetch one message as a plain dict (used to re-read a card before editing it)."""
        try:
            resp = await self._client.im.v1.message.aget(GetMessageRequest.builder().message_id(message_id).build())
        except Exception as exc:
            logger.warning(f"fetch_message {message_id} failed — {exc!r}")
            return {}
        if not resp.success():
            logger.warning(f"fetch_message {message_id} rejected — code={resp.code} msg={resp.msg}")
            return {}
        raw = getattr(resp, "raw", None)
        content = getattr(raw, "content", None) if raw is not None else None
        if not content:
            return {}
        try:
            body = json.loads(bytes(content).decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            return {}
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            return items[0] if isinstance(items[0], dict) else {}
        return {}

    async def download_resource_to_file(
        self,
        file_key: str,
        *,
        resource_type: str,
        message_id: str,
        dest_dir: str,
        file_name: str | None = None,
    ) -> str:
        """Download a message attachment to ``dest_dir`` and return the saved path."""
        req = GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build()
        resp = await self._client.im.v1.message_resource.aget(req)
        if not resp.success():
            raise RuntimeError(f"resource {file_key} download rejected: code={resp.code} msg={resp.msg}")
        name = file_name or getattr(resp, "file_name", "") or file_key
        path = anyio.Path(dest_dir) / name
        payload = getattr(resp, "file", None)
        if payload is None:
            raise RuntimeError(f"resource {file_key} response carried no file")
        await path.write_bytes(payload.read())
        return str(path)


__all__ = ["BotIdentity", "FeishuChannel", "InboundMessage", "PolicyConfig", "SendResult"]

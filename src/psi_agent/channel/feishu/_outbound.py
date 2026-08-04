"""Sending to Feishu: plain messages, media upload, and streamed markdown cards.

``lark_oapi`` models each endpoint but has no opinion about *messages* — it will POST a
message whose content you already serialized, and upload a file whose bytes you already
read. This module is the layer above that: it turns "send this local image to this chat"
into the two-step upload plus send, and "stream this agent's output" into the CardKit
typewriter protocol.

The streaming protocol is the delicate part, so it's spelled out here:

1. Create a card with ``streaming_mode`` on, holding one markdown element.
2. Send that card *by reference* (``content = {"type":"card","data":{"card_id":...}}``)
   so the message and the card stay linked.
3. Patch the element's content as text arrives, each patch carrying a **monotonic
   sequence number**. HTTP responses can land out of order; the sequence is what stops a
   late reply from rewinding text the user already saw.
4. Turn ``streaming_mode`` off at the end so the card stops showing a typing cursor.

Updates are throttled on two thresholds at once (elapsed time OR accumulated
characters), because a token-by-token stream would otherwise issue one API call per
token and hit the per-app rate limit immediately.
"""

from __future__ import annotations

import io
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from lark_oapi.api.cardkit.v1 import (
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    SettingsCardRequest,
    SettingsCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from loguru import logger

from ._inbound import infer_receive_id_type

# The streamed card's single markdown element, and what it shows before the first token
# lands. A card must be non-empty to be sent at all, so this doubles as a "working on it"
# indicator.
_ELEMENT_ID = "stream_md"
_INITIAL_TEXT = "Thinking..."
_TERMINATED_FOOTER = "\n\n— _(generation interrupted)_"

# Throttle thresholds — fire when either is met. 100ms keeps the typewriter feel; 50
# chars keeps a fast burst from waiting on the timer.
_MIN_INTERVAL_S = 0.1
_MIN_CHARS = 50

# Feishu rejects images above 10MB on the image endpoint; larger files go as files.
_IMAGE_MAX_BYTES = 10 * 1024 * 1024

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


@dataclass
class SendResult:
    """Outcome of one send. ``success`` is checked before falling back image→file."""

    success: bool
    message_id: str = ""
    error: str = ""


def _merge_streaming_text(prev: str, chunk: str) -> str:
    """Append ``chunk`` to ``prev``, collapsing any overlap between them.

    An agent stream normally yields disjoint deltas, but a retried or resumed producer
    can re-send a prefix of what it already sent. Concatenating blindly would duplicate
    that text on screen, so the longest suffix/prefix overlap is dropped.
    """
    if not chunk:
        return prev
    if not prev:
        return chunk
    if chunk.startswith(prev):
        return chunk
    if prev.startswith(chunk):
        return prev
    for size in range(min(len(prev), len(chunk)), 0, -1):
        if prev[-size:] == chunk[:size]:
            return prev + chunk[size:]
    return prev + chunk


class _Uploadable(io.BytesIO):
    """An in-memory file that carries a filename.

    The SDK decides "this is multipart" by finding an ``io.IOBase`` in the request body,
    and httpx reads ``.name`` for the multipart ``filename=``. Without the name an image
    uploads as "upload" with no extension, which Feishu rejects.
    """

    def __init__(self, data: bytes, name: str) -> None:
        super().__init__(data)
        self.name = name


class FeishuSender:
    """Sends messages, uploads media, and streams cards through one ``lark_oapi`` client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ---- plain messages ----------------------------------------------------

    async def send_text(self, to: str, text: str, *, receive_id_type: str = "", reply_to: str = "") -> SendResult:
        """Send plain text, as a reply when ``reply_to`` is given."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        return await self._send_content(to, "text", content, receive_id_type=receive_id_type, reply_to=reply_to)

    async def send_card(self, to: str, card: dict[str, Any], *, receive_id_type: str = "") -> SendResult:
        """Send an interactive card given its JSON."""
        content = json.dumps(card, ensure_ascii=False)
        return await self._send_content(to, "interactive", content, receive_id_type=receive_id_type)

    async def _send_content(
        self, to: str, msg_type: str, content: str, *, receive_id_type: str = "", reply_to: str = ""
    ) -> SendResult:
        try:
            if reply_to:
                req = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to)
                    .request_body(ReplyMessageRequestBody.builder().content(content).msg_type(msg_type).build())
                    .build()
                )
                resp = await self._client.im.v1.message.areply(req)
            else:
                rit = receive_id_type or infer_receive_id_type(to)
                req = (
                    CreateMessageRequest.builder()
                    .receive_id_type(rit)
                    .request_body(
                        CreateMessageRequestBody.builder().receive_id(to).msg_type(msg_type).content(content).build()
                    )
                    .build()
                )
                resp = await self._client.im.v1.message.acreate(req)
        except Exception as exc:
            logger.warning(f"send {msg_type} to {to} failed — {exc!r}")
            return SendResult(success=False, error=repr(exc))
        if not getattr(resp, "success", lambda: False)():
            msg = f"code={getattr(resp, 'code', None)} msg={getattr(resp, 'msg', '')}"
            logger.warning(f"send {msg_type} to {to} rejected — {msg}")
            return SendResult(success=False, error=msg)
        data = getattr(resp, "data", None)
        return SendResult(success=True, message_id=str(getattr(data, "message_id", "") or ""))

    # ---- media -------------------------------------------------------------

    async def send_file_or_image(self, to: str, path: str) -> SendResult:
        """Send a local file, preferring the image endpoint when it plausibly is one.

        Feishu renders an image inline but rejects non-images (and oversized images) on
        that endpoint, so the caller's intent ("show this if you can") is expressed as
        try-image-then-file rather than by trusting the extension alone.
        """
        try:
            data = await anyio.Path(path).read_bytes()
        except OSError as exc:
            logger.warning(f"cannot read {path} — {exc!r}")
            return SendResult(success=False, error=repr(exc))

        name = Path(path).name
        if Path(path).suffix.lower() in _IMAGE_SUFFIXES and len(data) <= _IMAGE_MAX_BYTES:
            key = await self._upload_image(data, name)
            if key:
                content = json.dumps({"image_key": key}, ensure_ascii=False)
                result = await self._send_content(to, "image", content)
                if result.success:
                    return result
                logger.debug(f"{name} rejected as image, falling back to file")

        key = await self._upload_file(data, name)
        if not key:
            return SendResult(success=False, error="upload failed")
        content = json.dumps({"file_key": key, "file_name": name}, ensure_ascii=False)
        return await self._send_content(to, "file", content)

    async def _upload_image(self, data: bytes, name: str) -> str:
        try:
            req = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder().image_type("message").image(_Uploadable(data, name)).build()
                )
                .build()
            )
            resp = await self._client.im.v1.image.acreate(req)
        except Exception as exc:
            logger.warning(f"image upload {name} failed — {exc!r}")
            return ""
        payload = getattr(resp, "data", None)
        return str(getattr(payload, "image_key", "") or "")

    async def _upload_file(self, data: bytes, name: str) -> str:
        file_type = _feishu_file_type(name)
        try:
            req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(name)
                    .file(_Uploadable(data, name))
                    .build()
                )
                .build()
            )
            resp = await self._client.im.v1.file.acreate(req)
        except Exception as exc:
            logger.warning(f"file upload {name} failed — {exc!r}")
            return ""
        payload = getattr(resp, "data", None)
        return str(getattr(payload, "file_key", "") or "")

    # ---- cardkit primitives ------------------------------------------------

    async def create_card(self, spec: dict[str, Any]) -> str:
        """Pre-allocate a card and return its ``card_id``."""
        req = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder().type("card_json").data(json.dumps(spec, ensure_ascii=False)).build()
            )
            .build()
        )
        resp = await self._client.cardkit.v1.card.acreate(req)
        if not resp.success():
            raise RuntimeError(f"create_card failed: code={resp.code} msg={resp.msg}")
        card_id = getattr(getattr(resp, "data", None), "card_id", "")
        if not card_id:
            raise RuntimeError("create_card response carried no card_id")
        return str(card_id)

    async def update_card_element(self, card_id: str, element_id: str, content: str, sequence: int) -> None:
        """Patch one element's content under a monotonic ``sequence``."""
        req = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(element_id)
            .request_body(ContentCardElementRequestBody.builder().content(content).sequence(sequence).build())
            .build()
        )
        resp = await self._client.cardkit.v1.card_element.acontent(req)
        if not resp.success():
            # A rejected tick is not fatal: the next one carries the full text anyway, so
            # dropping it costs one frame of smoothness rather than the message.
            logger.debug(f"card element update seq={sequence} rejected — code={resp.code} msg={resp.msg}")

    async def finish_streaming_card(self, card_id: str, sequence: int) -> None:
        """Turn ``streaming_mode`` off so the card stops showing a cursor."""
        settings = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
        req = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(SettingsCardRequestBody.builder().settings(settings).sequence(sequence).build())
            .build()
        )
        resp = await self._client.cardkit.v1.card.asettings(req)
        if not resp.success():
            logger.debug(f"finish_streaming_card rejected — code={resp.code} msg={resp.msg}")


def _feishu_file_type(name: str) -> str:
    """Map a filename to Feishu's ``file_type`` enum.

    Feishu wants its own enum, not the extension: the named document types render with a
    proper icon, and anything unrecognized must go as ``stream`` (passing the extension
    verbatim is rejected).
    """
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix in {"opus", "mp4", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"}:
        return suffix
    guessed, _ = mimetypes.guess_type(name)
    if guessed == "audio/opus":
        return "opus"
    return "stream"


class MarkdownStream:
    """The handle a producer writes into, and the driver of the CardKit protocol.

    A producer only ever calls :meth:`append`. Everything else — creating the card,
    sending it, throttling, sequencing, finishing — happens here so the calling code
    stays a plain ``async for`` over agent output.
    """

    def __init__(
        self,
        sender: FeishuSender,
        *,
        to: str,
        receive_id_type: str = "",
        reply_to: str = "",
        element_id: str = _ELEMENT_ID,
        initial_text: str = _INITIAL_TEXT,
    ) -> None:
        self._sender = sender
        self._to = to
        self._rit = receive_id_type or infer_receive_id_type(to)
        self._reply_to = reply_to
        self._element_id = element_id
        self._initial_text = initial_text

        self._card_id = ""
        self._message_id = ""
        self._content = ""
        self._sequence = 0
        self._flushed_len = 0
        self._last_flush = 0.0

    @property
    def message_id(self) -> str:
        return self._message_id

    async def start(self) -> None:
        """Create the streaming card and post it, so the user sees it immediately."""
        spec = {
            "schema": "2.0",
            "config": {"streaming_mode": True, "summary": {"content": ""}},
            "body": {"elements": [{"tag": "markdown", "element_id": self._element_id, "content": self._initial_text}]},
        }
        self._card_id = await self._sender.create_card(spec)
        content = json.dumps({"type": "card", "data": {"card_id": self._card_id}}, ensure_ascii=False)
        result = await self._sender._send_content(
            self._to, "interactive", content, receive_id_type=self._rit, reply_to=self._reply_to
        )
        if not result.success and self._reply_to:
            # The message being replied to can be gone (recalled, or in a chat the bot
            # just left). Falling back to a fresh message keeps the answer reachable
            # instead of dropping it because its anchor vanished.
            logger.debug("streamed card reply failed, retrying as a fresh message")
            result = await self._sender._send_content(self._to, "interactive", content, receive_id_type=self._rit)
        self._message_id = result.message_id
        self._last_flush = time.monotonic()

    async def append(self, text: str) -> None:
        """Add text to the card, flushing when a throttle threshold is met."""
        if not text:
            return
        self._content = _merge_streaming_text(self._content, text)
        pending = len(self._content) - self._flushed_len
        elapsed = time.monotonic() - self._last_flush
        if pending >= _MIN_CHARS or elapsed >= _MIN_INTERVAL_S:
            await self._flush()

    async def _flush(self) -> None:
        if not self._card_id or self._flushed_len == len(self._content):
            return
        self._sequence += 1
        await self._sender.update_card_element(self._card_id, self._element_id, self._content, self._sequence)
        self._flushed_len = len(self._content)
        self._last_flush = time.monotonic()

    async def close(self, *, interrupted: bool = False) -> None:
        """Flush whatever is left and take the card out of streaming mode.

        Runs even when the producer raised: an interrupted answer that stays in streaming
        mode would show a blinking cursor forever, which reads as "still working" long
        after the agent stopped.
        """
        if interrupted:
            self._content += _TERMINATED_FOOTER
        await self._flush()
        if self._card_id:
            self._sequence += 1
            await self._sender.finish_streaming_card(self._card_id, self._sequence)

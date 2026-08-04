"""Inbound Feishu message normalization.

``lark_oapi`` hands us the raw event exactly as Feishu sent it: ``content`` is a JSON
*string* whose shape depends on ``message_type``, media lives behind file keys, and
mentions are a parallel list of placeholder→id pairs. Everything downstream in this
package wants one flat object instead, so this module is the single place that knows
those wire shapes.

What it produces is deliberately narrow — only the fields this channel actually reads:
identity and conversation facts, a flat ``content_text`` rendering, and a list of media
descriptors to download later. Message types we don't render fall through to a
placeholder rather than raising, so an unfamiliar message shows up as "unsupported"
instead of taking down the handler.

Mentions matter more than they look: ``mentioned_bot`` is what the group policy gate
keys on, and Feishu only tells us the mention's *placeholder* (``@_user_1``) plus the
open_id it resolves to. Matching that against the bot's own open_id is the only way to
know a group message was addressed to us.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# Feishu's group chat_types. A DM is "p2p"; everything else here shares one session.
GROUP_CHAT_TYPES = frozenset({"group", "topic"})


@dataclass
class ResourceDescriptor:
    """One downloadable attachment on a message."""

    file_key: str
    type: str
    file_name: str = ""


@dataclass
class InboundMessage:
    """A Feishu message flattened into the facts this channel consumes."""

    message_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    sender_id: str = ""
    sender_name: str = ""
    content_text: str = ""
    raw_content_type: str = ""
    thread_id: str = ""
    reply_to_message_id: str = ""
    mentioned_bot: bool = False
    mentioned_all: bool = False
    resources: list[ResourceDescriptor] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _loads(content: str) -> dict[str, Any]:
    """Parse a message's ``content`` JSON string, degrading to empty on garbage."""
    try:
        parsed = json.loads(content or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _render_post(body: dict[str, Any]) -> tuple[str, list[ResourceDescriptor]]:
    """Flatten a rich-text ``post`` into text plus any media it embeds.

    A post is a list of paragraphs, each a list of runs. Only the run kinds that carry
    meaning for an agent are rendered; images inside a post become resources so they get
    downloaded like any other attachment.
    """
    lines: list[str] = []
    resources: list[ResourceDescriptor] = []
    paragraphs = body.get("content")
    for para in paragraphs if isinstance(paragraphs, list) else []:
        parts: list[str] = []
        for run in para if isinstance(para, list) else []:
            if not isinstance(run, dict):
                continue
            tag = run.get("tag")
            if tag in {"text", "md"}:
                parts.append(str(run.get("text") or ""))
            elif tag == "a":
                label = str(run.get("text") or "")
                href = str(run.get("href") or "")
                parts.append(f"[{label}]({href})" if href else label)
            elif tag == "at":
                # Keep the @ visible in the flat text; the bot-mention decision is made
                # from the mentions list, not from this rendering.
                name = str(run.get("user_name") or run.get("user_id") or "")
                parts.append(f"@{name}" if name else "@")
            elif tag == "img":
                key = str(run.get("image_key") or "")
                if key:
                    resources.append(ResourceDescriptor(file_key=key, type="image"))
                    parts.append(f'<image key="{key}" />')
        lines.append("".join(parts))
    title = str(body.get("title") or "")
    text = "\n".join(lines)
    return (f"{title}\n{text}" if title else text), resources


# Media message types → (content key holding the file key, resource type). Audio and
# video keep their key in the flat text too: ``_build_chunks`` scans for the audio tag.
_MEDIA_KINDS: dict[str, tuple[str, str]] = {
    "image": ("image_key", "image"),
    "file": ("file_key", "file"),
    "audio": ("file_key", "file"),
    "media": ("file_key", "file"),
    "sticker": ("file_key", "image"),
}


def _render_media(msg_type: str, body: dict[str, Any]) -> tuple[str, list[ResourceDescriptor]]:
    """Render a media message as a placeholder tag and one resource descriptor."""
    key_field, resource_type = _MEDIA_KINDS[msg_type]
    key = str(body.get(key_field) or "")
    if not key:
        return f"<{msg_type} />", []
    name = str(body.get("file_name") or "")
    tag = f'<{msg_type} key="{key}"'
    if name:
        tag += f' name="{name}"'
    tag += " />"
    return tag, [ResourceDescriptor(file_key=key, type=resource_type, file_name=name)]


def render_content(msg_type: str, content: str) -> tuple[str, list[ResourceDescriptor]]:
    """Flatten one message's ``content`` JSON into text + downloadable resources.

    Unknown types render as a bare ``<type />`` placeholder: the caller treats a message
    with no real content as unsupported, which is the honest outcome for a shape we
    can't read, and keeps a new Feishu message type from raising here.
    """
    body = _loads(content)
    if msg_type == "text":
        return str(body.get("text") or ""), []
    if msg_type == "post":
        return _render_post(body)
    if msg_type in _MEDIA_KINDS:
        return _render_media(msg_type, body)
    if msg_type == "interactive":
        return str(body.get("title") or "<interactive />"), []
    return f"<{msg_type} />", []


def normalize_message(event: Any, bot_open_id: str | None) -> InboundMessage:
    """Turn a raw ``im.message.receive_v1`` event into an ``InboundMessage``.

    ``bot_open_id`` decides ``mentioned_bot``. When it's unknown (identity resolution
    failed) no mention can match, which is why an unresolved bot identity shows up as
    "the bot ignores group @s" — the policy gate has nothing to compare against.
    """
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_identity = getattr(sender, "sender_id", None)

    msg_type = str(getattr(message, "message_type", "") or "")
    content_text, resources = render_content(msg_type, getattr(message, "content", "") or "")

    mentions = getattr(message, "mentions", None) or []
    mentioned_bot = False
    for mention in mentions:
        mention_id = getattr(mention, "id", None)
        open_id = getattr(mention_id, "open_id", None)
        if bot_open_id and open_id == bot_open_id:
            mentioned_bot = True
            break

    # ``@all`` doesn't appear in ``mentions``; Feishu marks it inside the content.
    mentioned_all = "@_all" in (getattr(message, "content", "") or "")

    return InboundMessage(
        message_id=str(getattr(message, "message_id", "") or ""),
        chat_id=str(getattr(message, "chat_id", "") or ""),
        chat_type=str(getattr(message, "chat_type", "") or ""),
        sender_id=str(getattr(sender_identity, "open_id", "") or ""),
        content_text=content_text,
        raw_content_type=msg_type,
        thread_id=str(getattr(message, "thread_id", "") or ""),
        reply_to_message_id=str(getattr(message, "parent_id", "") or ""),
        mentioned_bot=mentioned_bot,
        mentioned_all=mentioned_all,
        resources=resources,
    )


@dataclass
class PolicyConfig:
    """Whether a message earns a reply.

    Mirrors the knobs the CLI already exposes. Group chats gate on an explicit
    @-mention so a bot in a busy group doesn't answer every line; DMs are always
    answered because the user is talking to the bot by definition.
    """

    require_mention: bool = True
    respond_to_mention_all: bool = False


@dataclass
class RejectedMessage:
    """Why a message was dropped — handed to the ``reject`` callback for logging."""

    message_id: str
    reason: str


def policy_allows(msg: InboundMessage, policy: PolicyConfig) -> RejectedMessage | None:
    """``None`` when the message should be handled, else why it was rejected."""
    if msg.chat_type not in GROUP_CHAT_TYPES:
        return None
    if not policy.require_mention:
        return None
    if msg.mentioned_bot:
        return None
    if msg.mentioned_all and policy.respond_to_mention_all:
        return None
    return RejectedMessage(message_id=msg.message_id, reason="policy_no_mention")


def infer_receive_id_type(receive_id: str) -> str:
    """Guess Feishu's ``receive_id_type`` from the id's own prefix.

    Feishu ids are self-describing (``oc_`` chat, ``ou_`` user, ``on_`` union), and every
    send site in this package passes one of those. Defaulting to ``chat_id`` keeps the
    common case working when a caller passes something unprefixed.
    """
    if receive_id.startswith("ou_"):
        return "open_id"
    if receive_id.startswith("on_"):
        return "union_id"
    if receive_id.startswith("cli_"):
        return "app_id"
    if "@" in receive_id:
        return "email"
    return "chat_id"


def log_unexpected(where: str, exc: BaseException) -> None:
    """Uniform warning for a degradation that must not propagate."""
    logger.warning(f"{where} failed — {exc!r}")

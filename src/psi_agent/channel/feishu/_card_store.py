"""AppData-backed snapshots for single-use Feishu cards."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio

from psi_agent._appdata import resolve_appdata_root

_SNAPSHOT_VERSION = 2
_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# 多选卡的 per-action 墓碑名里嵌了 action_id, 所以它必须是个安全的文件名片段。
# 不匹配的 action_id 不给它退化成宽松名字 (会撞车), 直接按 hash 落盘。
_ACTION_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass(frozen=True, slots=True)
class CardSnapshot:
    """Card content and server-side callback routing metadata."""

    card: dict[str, Any]
    source: dict[str, Any] = field(default_factory=dict)
    business_context: dict[str, Any] = field(default_factory=dict)
    action_handlers: dict[str, str] = field(default_factory=dict)
    multi_use: bool = False


@dataclass(frozen=True, slots=True)
class CardSnapshotClaim:
    """Result of atomically claiming a card callback."""

    status: Literal["claimed", "already_consumed", "not_found", "invalid"]
    snapshot: CardSnapshot | None = None


def _validate_message_id(message_id: str) -> None:
    if not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError(f"Invalid Feishu message_id: {message_id!r}")


def _action_slug(action_id: str) -> str:
    """A collision-free filename fragment for a card action id."""
    if _ACTION_ID_RE.fullmatch(action_id):
        return action_id
    return "h" + hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:32]


async def _snapshot_path(message_id: str, appdata: str) -> anyio.Path:
    _validate_message_id(message_id)
    root = await resolve_appdata_root(appdata)
    return anyio.Path(root) / "feishu-card-snapshots" / f"{message_id}.json"


async def _write_consumed_marker(path: anyio.Path, status: str) -> None:
    await path.write_text(
        json.dumps({"version": _SNAPSHOT_VERSION, "status": status}) + "\n",
        encoding="utf-8",
    )
    await path.chmod(0o600)


async def save_card_snapshot(
    message_id: str,
    card: dict[str, Any],
    appdata: str = "",
    *,
    source: dict[str, Any] | None = None,
    business_context: dict[str, Any] | None = None,
    action_handlers: dict[str, str] | None = None,
    multi_use: bool = False,
) -> None:
    """Atomically persist the exact card sent to Feishu.

    ``multi_use`` marks a card whose actions are consumed **individually** (a TODO list
    where each row is ticked separately). Its snapshot survives the first callback and is
    rewritten in place by :func:`rewrite_card_snapshot`; single-use cards are unchanged.
    """
    path = await _snapshot_path(message_id, appdata)
    directory = path.parent
    await directory.mkdir(parents=True, exist_ok=True)
    await directory.chmod(0o700)
    consumed = directory / f"{message_id}.consumed"
    if await consumed.exists():
        raise RuntimeError(f"Feishu card {message_id!r} was consumed before its snapshot was saved")

    temporary = directory / f".{message_id}.{uuid.uuid4().hex}.tmp"
    try:
        payload = {
            "version": _SNAPSHOT_VERSION,
            "card": card,
            "source": source or {},
            "business_context": business_context or {},
            "action_handlers": action_handlers or {},
            "multi_use": multi_use,
        }
        await temporary.touch(mode=0o600, exist_ok=False)
        await temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        await temporary.chmod(0o600)
        if await consumed.exists():
            raise RuntimeError(f"Feishu card {message_id!r} was consumed before its snapshot was saved")
        await temporary.replace(path)
        await path.chmod(0o600)
        if await consumed.exists():
            await path.unlink()
            raise RuntimeError(f"Feishu card {message_id!r} was consumed before its snapshot was saved")
    finally:
        with contextlib.suppress(FileNotFoundError):
            await temporary.unlink()


def _parse_snapshot(payload: Any) -> CardSnapshot | None:
    """Validate a persisted payload, or ``None`` when it is unusable."""
    if not isinstance(payload, dict) or payload.get("version") not in {1, _SNAPSHOT_VERSION}:
        return None
    card = payload.get("card")
    if not isinstance(card, dict):
        return None
    if payload.get("version") == 1:
        return CardSnapshot(card=card)
    source = payload.get("source")
    business_context = payload.get("business_context")
    action_handlers = payload.get("action_handlers")
    multi_use = payload.get("multi_use", False)
    if (
        not isinstance(source, dict)
        or not isinstance(business_context, dict)
        or not isinstance(action_handlers, dict)
        or not isinstance(multi_use, bool)
    ):
        return None
    if not all(
        isinstance(action_id, str) and isinstance(handler, str) for action_id, handler in action_handlers.items()
    ):
        return None
    return CardSnapshot(
        card=card,
        source=source,
        business_context=business_context,
        action_handlers=action_handlers,
        multi_use=multi_use,
    )


async def _peek_snapshot(path: anyio.Path) -> CardSnapshot | None:
    """Read a snapshot without claiming it. Used only to learn its mode."""
    try:
        return _parse_snapshot(json.loads(await path.read_text(encoding="utf-8")))
    except OSError, json.JSONDecodeError, UnicodeDecodeError:
        return None


async def peek_card_multi_use(message_id: str, appdata: str = "") -> bool:
    """Whether this card consumes its actions individually. Claims nothing."""
    try:
        snapshot = await _peek_snapshot(await _snapshot_path(message_id, appdata))
    except ValueError:
        return False
    return snapshot is not None and snapshot.multi_use


async def rewrite_card_snapshot(message_id: str, card: dict[str, Any], appdata: str = "") -> bool:
    """Replace a **multi-use** card's stored content, keeping its routing metadata.

    Called after each tick so the next callback sees the already-ticked rows. Without
    this, a second tick would render from the pristine card and silently undo the first.
    """
    path = await _snapshot_path(message_id, appdata)
    snapshot = await _peek_snapshot(path)
    if snapshot is None or not snapshot.multi_use:
        return False
    directory = path.parent
    temporary = directory / f".{message_id}.{uuid.uuid4().hex}.tmp"
    try:
        payload = {
            "version": _SNAPSHOT_VERSION,
            "card": card,
            "source": snapshot.source,
            "business_context": snapshot.business_context,
            "action_handlers": snapshot.action_handlers,
            "multi_use": True,
        }
        await temporary.touch(mode=0o600, exist_ok=False)
        await temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        await temporary.chmod(0o600)
        await temporary.replace(path)
        await path.chmod(0o600)
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            await temporary.unlink()


async def pop_card_snapshot(
    message_id: str,
    appdata: str = "",
    *,
    action_id: str | None = None,
) -> CardSnapshotClaim:
    """Atomically claim a snapshot and retain a durable single-use tombstone.

    A **multi-use** card is claimed per action instead: its snapshot stays in place and
    only ``{message_id}.{action}.consumed`` is created, so the remaining rows keep
    working while a repeat click on the same row is still rejected exactly once.
    """
    path = await _snapshot_path(message_id, appdata)
    await path.parent.mkdir(parents=True, exist_ok=True)
    await path.parent.chmod(0o700)
    consumed = path.parent / f"{message_id}.consumed"
    if await consumed.exists():
        return CardSnapshotClaim(status="already_consumed")

    if action_id is not None:
        peeked = await _peek_snapshot(path)
        if peeked is not None and peeked.multi_use:
            action_tombstone = path.parent / f"{message_id}.{_action_slug(action_id)}.consumed"
            try:
                # touch(exist_ok=False) 是这里唯一的并发闸: 同时点两条会各自建自己的
                # 墓碑而互不影响, 重复点同一条则必然撞上 FileExistsError。
                await action_tombstone.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                return CardSnapshotClaim(status="already_consumed")
            await _write_consumed_marker(action_tombstone, "consumed")
            return CardSnapshotClaim(status="claimed", snapshot=peeked)

    try:
        await path.rename(consumed)
    except FileNotFoundError:
        try:
            await consumed.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            return CardSnapshotClaim(status="already_consumed")
        await _write_consumed_marker(consumed, "not_found")
        return CardSnapshotClaim(status="not_found")

    try:
        payload = json.loads(await consumed.read_text(encoding="utf-8"))
    except json.JSONDecodeError, UnicodeDecodeError:
        await _write_consumed_marker(consumed, "invalid")
        return CardSnapshotClaim(status="invalid")
    snapshot = _parse_snapshot(payload)
    if snapshot is None:
        await _write_consumed_marker(consumed, "invalid")
        return CardSnapshotClaim(status="invalid")
    await _write_consumed_marker(consumed, "consumed")
    return CardSnapshotClaim(status="claimed", snapshot=snapshot)

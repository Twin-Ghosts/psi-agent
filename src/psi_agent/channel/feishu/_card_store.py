"""AppData-backed snapshots for single-use Feishu cards."""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from typing import Any

import anyio

from psi_agent._appdata import resolve_appdata_root

_SNAPSHOT_VERSION = 1
_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _validate_message_id(message_id: str) -> None:
    if not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError(f"Invalid Feishu message_id: {message_id!r}")


async def _snapshot_path(message_id: str, appdata: str) -> anyio.Path:
    _validate_message_id(message_id)
    root = await resolve_appdata_root(appdata)
    return anyio.Path(root) / "feishu-card-snapshots" / f"{message_id}.json"


async def save_card_snapshot(message_id: str, card: dict[str, Any], appdata: str = "") -> None:
    """Atomically persist the exact card sent to Feishu."""
    path = await _snapshot_path(message_id, appdata)
    directory = path.parent
    await directory.mkdir(parents=True, exist_ok=True)
    await directory.chmod(0o700)

    temporary = directory / f".{message_id}.{uuid.uuid4().hex}.tmp"
    try:
        payload = {"version": _SNAPSHOT_VERSION, "card": card}
        await temporary.touch(mode=0o600, exist_ok=False)
        await temporary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        await temporary.chmod(0o600)
        await temporary.replace(path)
        await path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            await temporary.unlink()


async def pop_card_snapshot(message_id: str, appdata: str = "") -> dict[str, Any] | None:
    """Claim and delete a card snapshot, returning it to only one consumer."""
    path = await _snapshot_path(message_id, appdata)
    claimed = path.parent / f".{message_id}.{uuid.uuid4().hex}.consuming"
    try:
        await path.rename(claimed)
    except FileNotFoundError:
        return None

    try:
        try:
            payload = json.loads(await claimed.read_text(encoding="utf-8"))
        except json.JSONDecodeError, UnicodeDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("version") != _SNAPSHOT_VERSION:
            return None
        card = payload.get("card")
        return card if isinstance(card, dict) else None
    finally:
        with contextlib.suppress(FileNotFoundError):
            await claimed.unlink()

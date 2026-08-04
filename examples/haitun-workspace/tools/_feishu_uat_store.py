"""User-access-token storage — the piece ``lark_oapi`` doesn't carry.

The full Open Platform SDK models API requests, not the multi-user token lifecycle
around them: it will attach a UAT you already hold (``RequestOption.user_access_token``)
but has no opinion on where that token lives between turns. The channel-edition SDK
did ship such a store; migrating off it means owning these three pieces ourselves.

Kept deliberately small and behaviour-compatible with what the tools already expect:

* ``UAT`` — one person's tokens plus the two expiry stamps and the granted scopes.
  ``scopes`` matters because authorization is incremental here: a second request may
  need a scope the first one never asked for, and the union is what we re-request.
* ``FileTokenStore`` — one JSON file keyed by open_id, guarded by an asyncio lock so
  two concurrent authorizations can't clobber each other's entry. Writes go through a
  temp file + replace so a crash can't leave a half-written store that locks every
  user out.
* ``uat_needs_refresh`` — refresh five minutes early rather than on expiry, so a
  token doesn't die mid-request.

A malformed or unreadable store degrades to "nobody is authorized" instead of raising:
losing a cached token costs one re-authorization, while an exception here would take
down every Feishu tool at import time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

# Refresh this many seconds before the token actually expires. Feishu's UATs last two
# hours; five minutes of slack is enough for a slow turn to finish on the old token.
_REFRESH_SLACK_SECONDS = 300


@dataclass
class UAT:
    """One user's access token, as cached between turns."""

    access_token: str
    scopes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    refresh_token: str | None = None
    expires_at: float | None = None
    refresh_expires_at: float | None = None
    open_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "scopes": list(self.scopes),
            "open_id": self.open_id,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UAT:
        scopes = data.get("scopes") or []
        raw = data.get("raw") or {}
        return cls(
            access_token=str(data.get("access_token") or ""),
            scopes=[str(s) for s in scopes] if isinstance(scopes, list) else [],
            raw=raw if isinstance(raw, dict) else {},
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            refresh_expires_at=data.get("refresh_expires_at"),
            open_id=data.get("open_id"),
        )


def uat_needs_refresh(uat: UAT, *, slack_seconds: int = _REFRESH_SLACK_SECONDS) -> bool:
    """Whether ``uat`` is close enough to expiry that it should be refreshed now.

    A token with no recorded expiry is left alone: we can't prove it's stale, and
    refreshing on every call would burn the refresh token for nothing.
    """
    if uat.expires_at is None:
        return False
    return uat.expires_at - time.time() <= slack_seconds


class FileTokenStore:
    """UATs persisted to one JSON file, keyed by user (open_id).

    The lock is created lazily rather than in ``__init__`` because the store is built
    at import time, before any event loop exists.
    """

    def __init__(self, path: str) -> None:
        self._path = pathlib.Path(path)
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> dict[str, UAT]:
        """Read the whole store. Any unreadable/corrupt file reads as empty — see module docstring."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, UAT] = {}
        for key, value in parsed.items():
            if isinstance(value, dict):
                with contextlib.suppress(Exception):
                    out[str(key)] = UAT.from_dict(value)
        return out

    def _persist(self, tokens: dict[str, UAT]) -> None:
        """Write via temp file + replace so an interrupted write can't corrupt the store."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {key: uat.to_dict() for key, uat in tokens.items()},
            ensure_ascii=False,
            indent=2,
        )
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    async def get(self, user_id: str) -> UAT | None:
        async with self._get_lock():
            return self._load().get(user_id)

    async def set(self, user_id: str, token: UAT) -> None:
        async with self._get_lock():
            tokens = self._load()
            tokens[user_id] = token
            self._persist(tokens)

    async def delete(self, user_id: str) -> None:
        async with self._get_lock():
            tokens = self._load()
            if tokens.pop(user_id, None) is not None:
                self._persist(tokens)


class InMemoryTokenStore:
    """Non-persistent store — used by tests so they never touch the real token file."""

    def __init__(self) -> None:
        self._tokens: dict[str, UAT] = {}

    async def get(self, user_id: str) -> UAT | None:
        return self._tokens.get(user_id)

    async def set(self, user_id: str, token: UAT) -> None:
        self._tokens[user_id] = token

    async def delete(self, user_id: str) -> None:
        self._tokens.pop(user_id, None)

    def clear(self) -> None:
        self._tokens.clear()

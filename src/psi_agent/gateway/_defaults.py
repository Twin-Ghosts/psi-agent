"""Gateway path defaults (agent / workspace / AppData root).

What this module is for
-----------------------
Callers that create Sessions (spa v1/v2, Feishu, haitun ``sessions_create``, …)
need a shared answer to: "what is the default agent package?" and "what is the
default user workspace?". ``GET /defaults`` and ``SessionManager`` both use
these resolvers.

AppData path helpers live in ``psi_agent._appdata`` (Session-safe; no circular
import). This module re-exports them for existing Gateway / tool call sites.

Soft default (agent)
--------------------
If CLI ``--default-agent`` is empty and ``examples/haitun-workspace`` exists
under cwd, that directory is used so repo-local Gateway open-and-use works.
Otherwise agent stays ``\"\"`` → Session single-root compat (agent ≡ workspace).
"""

from __future__ import annotations

import anyio

from psi_agent._appdata import (
    appdata_history_path,
    appdata_state_dir,
    appdata_state_latest_path,
    appdata_todo_path,
    legacy_history_path,
    legacy_state_latest_path,
    legacy_todo_path,
    resolve_appdata_root,
    resolve_history_read_path,
    resolve_state_read_path,
    resolve_todo_read_path,
)

__all__ = [
    "appdata_history_path",
    "appdata_state_dir",
    "appdata_state_latest_path",
    "appdata_todo_path",
    "legacy_history_path",
    "legacy_state_latest_path",
    "legacy_todo_path",
    "resolve_appdata_root",
    "resolve_default_agent",
    "resolve_default_workspace",
    "resolve_history_read_path",
    "resolve_state_read_path",
    "resolve_todo_read_path",
]


async def resolve_default_workspace(explicit: str = "") -> str:
    """Absolute user workspace path; empty *explicit* → process cwd."""
    raw = explicit.strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    return str(await anyio.Path.cwd())


async def resolve_default_agent(explicit: str = "") -> str:
    """Absolute agent package path, or ``\"\"`` for Session workspace fallback."""
    raw = explicit.strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    # Soft default for developers who start Gateway from the repo root.
    candidate = (await anyio.Path.cwd()) / "examples" / "haitun-workspace"
    if await candidate.is_dir():
        return str(await candidate.resolve())
    return ""

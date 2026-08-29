"""Workspace / agent-package path resolution — mechanism only, no product names.

Shared by Session-spawning runtime code (``SessionManager``) and by Gateway's
``GET /defaults``. Lives outside ``psi_agent.gateway`` for the same reason
``_appdata.py`` does: the managers that create Sessions must not import a
product package.

**This module knows no product concepts** — no tray, no webview, no Windows
drive letters, no desktop login, and in particular no brand folder names. The
two brand literals (the user-workspace folder name and the repo-local agent
package path) are owned by the caller and passed in; see
``gateway/_defaults.py`` for the ToC values.

Three mechanisms live here:

- ``resolve_user_workspace`` — explicit path, else ``{Desktop}/{default_name}``
  (announce only; **no** mkdir).
- ``ensure_workspace_dir`` — mkdir at Session spawn time.
- ``resolve_agent_package`` — explicit path, else a caller-named candidate under
  cwd, else cwd itself when it looks like an agent package (``tools/`` +
  ``skills/``).
"""

from __future__ import annotations

import anyio
import platformdirs


async def resolve_user_workspace(explicit: str = "", *, default_name: str) -> str:
    """Absolute user workspace path (announce only — does not create).

    *explicit* non-empty → resolve that path. Empty → ``{Desktop}/{default_name}``
    via ``platformdirs.user_desktop_dir`` (never hand-written ``%USERPROFILE%``).
    *default_name* is the caller's folder name — this module has no default of
    its own. Directory creation is deferred to ``ensure_workspace_dir``.
    """
    raw = explicit.strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    # Sync platformdirs call is path math only (no IO); fine inside async.
    desktop = anyio.Path(platformdirs.user_desktop_dir())
    ws = desktop / default_name
    return str(await ws.resolve())


async def ensure_workspace_dir(path: str) -> str:
    """Create *path* if missing; return absolute path.

    Call from Session spawn only (``SessionManager.create``), not from
    ``GET /defaults`` / Gateway boot — so a soft default folder appears only
    when the user actually starts a conversation.
    """
    ws = anyio.Path(path.strip())
    await ws.mkdir(parents=True, exist_ok=True)
    return str(await ws.resolve())


async def resolve_agent_package(explicit: str = "", *, repo_candidate: str = "") -> str:
    """Absolute agent package path, or ``""`` for Session workspace fallback.

    1. *explicit* non-empty → resolve that path.
    2. *repo_candidate* (caller-supplied, relative to cwd) when it is a
       directory — the repo-local layout where Gateway starts from the repo root.
    3. cwd itself when it holds ``tools/`` + ``skills/`` — the installed layout,
       where the install dir *is* the agent package.
    4. Otherwise ``""`` → Session single-root compat (agent ≡ workspace).
    """
    raw = explicit.strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    cwd = await anyio.Path.cwd()
    candidate_rel = repo_candidate.strip()
    if candidate_rel:
        candidate = cwd / candidate_rel
        if await candidate.is_dir():
            return str(await candidate.resolve())
    if await (cwd / "tools").is_dir() and await (cwd / "skills").is_dir():
        return str(await cwd.resolve())
    return ""

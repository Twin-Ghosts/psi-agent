"""Gateway path defaults (agent / workspace / AppData root).

What this module is for
-----------------------
Callers that create Sessions (spa v1/v2, Feishu, haitun ``sessions_create``, …)
need a shared answer to: "what is the default agent package?" and "what is the
default user workspace?". ``GET /defaults`` and ``SessionManager`` both use
these resolvers.

AppData root
------------
``resolve_appdata_root`` uses ``platformdirs.user_data_dir`` (never hardcoded
``%AppData%``). Step 4A announced the root; Step 4B relocates **todos** under
``{appdata}/todos/`` with dual-read of the legacy workspace path.

What this is NOT
----------------
- Not history / Gateway ``state/`` relocation (later AppData PRs).
- Tool-side workspace/agent IO is haitun ``tools/_runtime_paths.py``.
- Do not put AppData into Session ContextVars.

Soft default (agent)
--------------------
If CLI ``--default-agent`` is empty and ``examples/haitun-workspace`` exists
under cwd, that directory is used so repo-local Gateway open-and-use works.
Otherwise agent stays ``\"\"`` → Session single-root compat (agent ≡ workspace).
"""

from __future__ import annotations

import os

import anyio
import platformdirs

# Directory name under the OS user-data root (not the Gateway --app-name label).
_APPDATA_APPNAME = "Haitun"
_APPDATA_ENV = "PSI_APPDATA"


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


async def resolve_appdata_root(explicit: str = "") -> str:
    """Absolute AppData (memory) root.

    Priority: *explicit* CLI → ``PSI_APPDATA`` env → ``platformdirs.user_data_dir``.
    Step 4A announced this path; later steps relocate writers under it.
    """
    raw = explicit.strip() or os.environ.get(_APPDATA_ENV, "").strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    # Sync platformdirs call is path math only (no IO); fine inside async.
    return str(await anyio.Path(platformdirs.user_data_dir(appname=_APPDATA_APPNAME, appauthor=False)).resolve())


def legacy_todo_path(workspace: str, session_id: str) -> anyio.Path:
    """Pre-AppData path: ``{workspace}/.psi/todos/{session_id}.json``."""
    return anyio.Path(workspace) / ".psi" / "todos" / f"{session_id}.json"


def appdata_todo_path(appdata_root: str, session_id: str) -> anyio.Path:
    """AppData path (Step 4B): ``{appdata}/todos/{session_id}.json``."""
    return anyio.Path(appdata_root) / "todos" / f"{session_id}.json"


async def resolve_todo_read_path(
    *,
    appdata_root: str,
    workspace: str,
    session_id: str,
) -> anyio.Path:
    """Dual-read: prefer AppData file if present, else legacy workspace file."""
    primary = appdata_todo_path(appdata_root, session_id)
    if await primary.is_file():
        return primary
    legacy = legacy_todo_path(workspace, session_id)
    if await legacy.is_file():
        return legacy
    return primary

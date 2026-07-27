"""Gateway path defaults (agent / workspace / AppData root).

What this module is for
-----------------------
Callers that create Sessions (spa v1/v2, Feishu, haitun ``sessions_create``, …)
need a shared answer to: "what is the default agent package?" and "what is the
default user workspace?". ``GET /defaults`` and ``SessionManager`` both use
these resolvers.

AppData root (Step A)
---------------------
``resolve_appdata_root`` announces where history / Gateway state / todos **will**
live after later PRs. This step only resolves and exposes the path — it does
**not** relocate writers. Prefer ``platformdirs.user_data_dir`` over hardcoded
``%AppData%`` / ``~/Library/...``.

What this is NOT
----------------
- Not history / state / todos relocation (later AppData PRs).
- Tool-side path IO is haitun ``tools/_runtime_paths.py`` (reads
  ``get_workspace()`` / ``get_agent()`` only — never AppData ContextVars).

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
    """Absolute AppData (memory) root — announce only; writers still use old paths.

    Priority: *explicit* CLI → ``PSI_APPDATA`` env → ``platformdirs.user_data_dir``.
    """
    raw = explicit.strip() or os.environ.get(_APPDATA_ENV, "").strip()
    if raw:
        return str(await anyio.Path(raw).resolve())
    # Sync platformdirs call is path math only (no IO); fine inside async.
    return str(await anyio.Path(platformdirs.user_data_dir(appname=_APPDATA_APPNAME, appauthor=False)).resolve())

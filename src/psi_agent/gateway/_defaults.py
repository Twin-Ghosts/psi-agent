"""Gateway path defaults (wiring only; no AppData).

What this module is for
-----------------------
Callers that create Sessions (spa v1/v2, Feishu, haitun ``sessions_create``, …)
need a shared answer to: "what is the default agent package?" and "what is the
default user workspace?". ``GET /defaults`` and ``SessionManager`` both use
these resolvers.

What this is NOT
----------------
- Not AppData / history relocation.
- Tool-side path IO is haitun ``tools/_runtime_paths.py`` (reads
  ``get_workspace()`` / ``get_agent()``).

Soft default
------------
If CLI ``--default-agent`` is empty and ``examples/haitun-workspace`` exists
under cwd, that directory is used so repo-local Gateway open-and-use works.
Otherwise agent stays ``\"\"`` → Session single-root compat (agent ≡ workspace).
"""

from __future__ import annotations

import anyio


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

"""Gateway path defaults (agent / workspace / AppData root) — ToC brand values.

What this module is for
-----------------------
Callers that create Sessions (spa v1/v2, Feishu, haitun ``sessions_create``, …)
need a shared answer to: "what is the default agent package?" and "what is the
default user workspace?". ``GET /defaults`` and ``SessionManager`` both use
these resolvers.

**This module owns only the brand literals.** The mechanism (desktop path math,
mkdir, ``tools/`` + ``skills/`` probing) lives in ``psi_agent._workspace_paths``,
outside this package, so Session-spawning managers can reach it without
importing a product-line package. Splitting it this way keeps the ToC names
(``haitun交付``, ``workspace/tob``) in exactly one place — renaming
the workspace touches only ``gateway/``.

AppData path helpers live in ``psi_agent._appdata`` (Session-safe; no circular
import). This module re-exports them, and ``ensure_workspace_dir``, for existing
Gateway / workspace-tool call sites.

Soft default (agent)
--------------------
If CLI ``--default-agent`` is empty:

1. Prefer ``cwd/workspace/tob`` when present (repo-local Gateway).
2. Else if *cwd itself* looks like a haitun agent package (``tools/`` + ``skills/``
   directories) — the Inno install layout, where ``{app}`` *is* the workspace —
   use cwd. This keeps ``psi-agent.exe gateway`` usable from the install dir
   even without the ``haitun.exe`` launcher flags.
3. Otherwise agent stays ``\"\"`` → Session single-root compat (agent ≡ workspace).

Soft default (workspace)
------------------------
If CLI ``--default-workspace`` is empty, announce ``{Desktop}/haitun交付``
(**path only** — do not mkdir here). Ordinary users get deliverables on the
Desktop without picking a folder; power users override via CLI / spa settings.
Intentional: mkdir only in ``SessionManager.create`` (start chat / new task),
so opening Haitun does not leave an empty Desktop folder. Not AppData.
"""

from __future__ import annotations

from psi_agent._appdata import (
    appdata_history_path,
    appdata_state_dir,
    appdata_state_latest_path,
    appdata_todo_path,
    appdata_todo_segments_path,
    legacy_history_path,
    legacy_state_latest_path,
    legacy_todo_path,
    resolve_appdata_root,
    resolve_history_read_path,
    resolve_state_read_path,
    resolve_todo_read_path,
)
from psi_agent._workspace_paths import (
    ensure_workspace_dir,
    resolve_agent_package,
    resolve_user_workspace,
)

# Soft default under the OS Desktop — layered for non-technical users.
DEFAULT_USER_WORKSPACE_NAME = "haitun交付"
# Repo-local agent package, relative to cwd (developers starting from repo root).
DEFAULT_AGENT_REPO_CANDIDATE = "workspace/tob"

__all__ = [
    "DEFAULT_AGENT_REPO_CANDIDATE",
    "DEFAULT_USER_WORKSPACE_NAME",
    "appdata_history_path",
    "appdata_state_dir",
    "appdata_state_latest_path",
    "appdata_todo_path",
    "appdata_todo_segments_path",
    "ensure_workspace_dir",
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
    """Absolute user workspace path (announce only — does not create).

    Thin brand wrapper over ``_workspace_paths.resolve_user_workspace``: supplies
    ``haitun交付`` as the soft Desktop folder name. Directory creation is
    deferred to ``ensure_workspace_dir`` at Session create time.
    """
    return await resolve_user_workspace(explicit, default_name=DEFAULT_USER_WORKSPACE_NAME)


async def resolve_default_agent(explicit: str = "") -> str:
    """Absolute agent package path, or ``\"\"`` for Session workspace fallback.

    Thin brand wrapper over ``_workspace_paths.resolve_agent_package``: supplies
    ``workspace/tob`` as the repo-local candidate.
    """
    return await resolve_agent_package(explicit, repo_candidate=DEFAULT_AGENT_REPO_CANDIDATE)

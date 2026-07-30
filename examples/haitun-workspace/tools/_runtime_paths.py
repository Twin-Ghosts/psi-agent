"""Step 3 — resolve user-workspace vs agent-package roots for tools.

Session binds ``get_workspace()`` / ``get_agent()`` per turn (see
``psi_agent.session.runtime_context``). Prefer those ContextVars over the
legacy ``WORKSPACE_DIR`` env and the tools-package parent fallback.

**Not AppData memory for files** — relative IO stays on workspace/agent. Todos /
history / Gateway ``state/`` live under AppData (Steps 4B-4D).
"""

from __future__ import annotations

import os
from pathlib import Path

import anyio

try:
    from psi_agent.session.runtime_context import get_agent as _runtime_agent
    from psi_agent.session.runtime_context import get_session_id as _runtime_session_id
    from psi_agent.session.runtime_context import get_workspace as _runtime_workspace
except ImportError:  # pragma: no cover — standalone import without editable install

    def _runtime_workspace() -> str:
        return ""

    def _runtime_agent() -> str:
        return ""

    def _runtime_session_id() -> str:
        return ""


try:
    from psi_agent._private_space import check_read as _guard_check_read
    from psi_agent._private_space import check_write as _guard_check_write
    from psi_agent._private_space import forbidden_dirs as _guard_forbidden_dirs
    from psi_agent._private_space import owns_session as _guard_owns_session
    from psi_agent._private_space import scan_command as _guard_scan_command
except ImportError:  # pragma: no cover — standalone import without editable install
    # Same shape as the runtime_context fallback above: stub out same-signature no-ops
    # rather than setting the module to None — the latter needs a ty: ignore on the
    # assignment, against the zero-suppression rule. Signatures (async included) must
    # match the real implementations.
    async def _guard_check_read(path: str, *, session_id: str) -> str | None:
        return None

    async def _guard_check_write(path: str, *, session_id: str) -> str | None:
        return None

    async def _guard_forbidden_dirs(session_id: str) -> list[str]:
        return []

    def _guard_owns_session(candidate_session_id: str, *, session_id: str) -> bool:
        return True

    async def _guard_scan_command(command: str, *, session_id: str) -> str | None:
        return None


class PrivateSpaceDeniedError(PermissionError):
    """Out-of-bounds access to another user's private space.

    Tools catch it and return ``str(e)`` to the model as their error string. Subclasses
    ``PermissionError`` rather than bare ``Exception`` so tools that already handle
    ``OSError`` treat it as one failed access instead of aborting the whole turn.
    """


def package_fallback() -> str:
    """``examples/haitun-workspace`` when this file lives under ``tools/``."""
    return str(Path(__file__).resolve().parents[1])


def workspace_dir(explicit: str = "") -> str:
    """User workspace root (relative file IO / schedules / todos / flows).

    Priority: explicit arg → ContextVar ``get_workspace()`` → ``WORKSPACE_DIR``
    → package fallback.
    """
    for candidate in (explicit, _runtime_workspace(), os.environ.get("WORKSPACE_DIR", "")):
        text = (candidate or "").strip()
        if text:
            return text
    return package_fallback()


def agent_dir(explicit: str = "") -> str:
    """Agent package root (skills / SOUL / capability files).

    Priority: explicit arg → ContextVar ``get_agent()`` → ``workspace_dir()``
    (empty agent means same root as workspace — Session contract).
    """
    for candidate in (explicit, _runtime_agent()):
        text = (candidate or "").strip()
        if text:
            return text
    return workspace_dir()


def resolve_workspace(raw: str = "") -> anyio.Path:
    """``anyio.Path`` for the user workspace (empty *raw* uses ``workspace_dir``)."""
    return anyio.Path(workspace_dir(raw))


def resolve_agent(raw: str = "") -> anyio.Path:
    """``anyio.Path`` for the agent package root."""
    return anyio.Path(agent_dir(raw))


async def guard_read(path: str | anyio.Path | Path) -> None:
    """Raise ``PrivateSpaceDeniedError`` on an out-of-bounds read; no-op when disabled."""
    reason = await _guard_check_read(str(path), session_id=_runtime_session_id())
    if reason:
        raise PrivateSpaceDeniedError(reason)


async def guard_write(path: str | anyio.Path | Path) -> None:
    """Raise on an out-of-bounds write; stricter than reads (the shared area is read-only)."""
    reason = await _guard_check_write(str(path), session_id=_runtime_session_id())
    if reason:
        raise PrivateSpaceDeniedError(reason)


async def forbidden_dirs() -> list[str]:
    """Directories a walking tool must skip wholesale; empty list when disabled."""
    return await _guard_forbidden_dirs(_runtime_session_id())


async def scan_command(command: str) -> str | None:
    """Out-of-bounds check on a shell command string; refusal text or ``None``.

    Heuristic — see the capability boundary note in ``psi_agent._private_space``.
    """
    return await _guard_scan_command(command, session_id=_runtime_session_id())


def owns_session(candidate_session_id: str) -> bool:
    """Whether *candidate_session_id*'s history belongs to the current session's owner."""
    return _guard_owns_session(candidate_session_id, session_id=_runtime_session_id())


async def resolve_under(root: str | anyio.Path | Path, path: str) -> anyio.Path:
    """Join *path* under *root* when relative; keep absolute paths as-is.

    This is the shared exit of 22 path tools, so one authority check here covers them
    all. Absolute paths used to be **returned verbatim with zero checks**, which let any
    session read another's workspace by absolute path; they are now resolved and passed
    through the guard as well (``owner_of`` expands symlinks and ``..``).

    Async because the guard resolves real paths (disk IO) — callers must ``await``.
    """
    raw = (path or "").strip() or "."
    candidate = Path(raw)
    resolved = anyio.Path(str(candidate)) if candidate.is_absolute() else anyio.Path(str(root)) / raw
    await guard_read(resolved)
    return resolved


async def resolve_user_path(path: str, *, workspace_raw: str = "") -> anyio.Path:
    """Resolve a tool file path against the user workspace."""
    return await resolve_under(workspace_dir(workspace_raw), path)

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
    from psi_agent import _private_space as _guard
except ImportError:  # pragma: no cover — standalone import without editable install
    _guard = None  # ty: ignore[invalid-assignment]


class PrivateSpaceDeniedError(PermissionError):
    """越界访问他人私有空间。工具捕获后把 ``str(e)`` 当错误串返回给模型。

    继承 ``PermissionError`` 而非裸 ``Exception``: 那些已经 ``except OSError`` 的
    工具会自然把它当成一次访问失败, 不至于把整轮对话打断。
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


def guard_read(path: str | anyio.Path | Path) -> None:
    """越界读则抛 ``PrivateSpaceDeniedError``; 守卫未启用时是空操作。"""
    if _guard is None:
        return
    reason = _guard.check_read(str(path), session_id=_runtime_session_id())
    if reason:
        raise PrivateSpaceDeniedError(reason)


def guard_write(path: str | anyio.Path | Path) -> None:
    """越界写则抛 ``PrivateSpaceDeniedError``; 写比读严(公共区亦只读)。"""
    if _guard is None:
        return
    reason = _guard.check_write(str(path), session_id=_runtime_session_id())
    if reason:
        raise PrivateSpaceDeniedError(reason)


def forbidden_dirs() -> list[str]:
    """遍历类工具应整棵跳过的目录(别人的空间); 未启用时空列表。"""
    if _guard is None:
        return []
    return _guard.forbidden_dirs(_runtime_session_id())


def scan_command(command: str) -> str | None:
    """shell 命令串越界检查; 返回拒绝原因或 ``None``(启发式, 见守卫模块说明)。"""
    if _guard is None:
        return None
    return _guard.scan_command(command, session_id=_runtime_session_id())


def owns_session(candidate_session_id: str) -> bool:
    """*candidate_session_id* 的历史是否属于当前会话本人。"""
    if _guard is None:
        return True
    return _guard.owns_session(candidate_session_id, session_id=_runtime_session_id())


def resolve_under(root: str | anyio.Path | Path, path: str) -> anyio.Path:
    """Join *path* under *root* when relative; keep absolute paths as-is.

    这是 22 个路径工具的公共出口, 故隔离判定放在这里一处即覆盖全部。绝对路径此前
    是**原样返回、零检查**, 于是任何会话都能读别人 workspace 的绝对路径 —— 现在同
    样先解析再过守卫(``owner_of`` 内部走 ``realpath``, symlink 与 ``..`` 均被展开)。
    """
    raw = (path or "").strip() or "."
    candidate = Path(raw)
    resolved = anyio.Path(str(candidate)) if candidate.is_absolute() else anyio.Path(str(root)) / raw
    guard_read(resolved)
    return resolved


def resolve_user_path(path: str, *, workspace_raw: str = "") -> anyio.Path:
    """Resolve a tool file path against the user workspace."""
    return resolve_under(workspace_dir(workspace_raw), path)

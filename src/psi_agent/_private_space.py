"""Symmetric per-user file isolation for Feishu sessions.

Gateway runs every Session as an async task in **one process under one uid**, so
file permission bits / setuid / container boundaries are all unavailable. The only
place authority can be decided is the tool-call entry point, hence these pure
predicates that each chokepoint calls explicitly.

**Symmetric model** (as opposed to "a few whitelisted private users"): every
routing key owns ``<root>/<owner>/`` and may only touch that prefix; the
``PUBLIC_DIRNAME`` subdirectory under ``<root>`` is readable by everyone so shared
material has a home. A group chat's owner is ``chat-<chat_id>`` — one space for the
whole group, matching the fact that a group shares one Session context.

**Capability boundary (stated plainly)**: path tools funnel through
``resolve_under`` and are decided deterministically; ``bash`` / ``powershell`` can
only be scanned **heuristically** — that stops a stray ``cat`` of someone else's
directory but not deliberate variable splicing / base64 / relay files. Strong
isolation needs one container per user.

The guard is **off by default**: with ``PSI_WORKSPACE_ROOT`` unset ``enabled()`` is
False and every function degrades to a no-op, byte-identical to the behaviour
before this module existed — so it can be deployed first and switched on later.
"""

from __future__ import annotations

import os
import re

import anyio

# Env var: the parent directory isolation applies under (normally equals the
# Gateway ``--feishu-workspace-root``).
_ROOT_ENV = "PSI_WORKSPACE_ROOT"

# This subdirectory of ``<root>`` is the shared area: readable by every session
# (writes stay confined to one's own space so nobody can overwrite it).
PUBLIC_DIRNAME = "public"

# History / todo files under AppData are named by session_id; owner is derived back
# out of that name.
_SESSION_PREFIX = "feishu-"
_GROUP_SESSION_PREFIX = "feishu-chat-"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def workspace_root() -> str:
    """Isolation parent directory; empty string means the guard is disabled."""
    return os.environ.get(_ROOT_ENV, "").strip()


def enabled() -> bool:
    """Whether the guard is active. Unset ``PSI_WORKSPACE_ROOT`` allows everything."""
    return bool(workspace_root())


def sanitize(token: str) -> str:
    """Reduce an open_id / chat_id to a safe path segment (same rule as FeishuManager)."""
    return _UNSAFE.sub("_", token or "")


def owner_from_session_id(session_id: str) -> str:
    """Derive the owner directory name from *session_id*; ``""`` when not a Feishu one.

    ``feishu-chat-<chat_id>`` maps to ``chat-<chat_id>`` (group) and
    ``feishu-<open_id>`` to ``<open_id>`` (direct message). Sessions created from the
    SPA carry no ``feishu-`` prefix, so they are ownerless and stay unrestricted —
    those are the local user's own sessions.
    """
    sid = (session_id or "").strip()
    if sid.startswith(_GROUP_SESSION_PREFIX):
        return f"chat-{sanitize(sid.removeprefix(_GROUP_SESSION_PREFIX))}"
    if sid.startswith(_SESSION_PREFIX):
        return sanitize(sid.removeprefix(_SESSION_PREFIX))
    return ""


async def owner_dir(owner: str) -> str:
    """Absolute path of *owner*'s space; ``""`` when owner or root is empty."""
    root = workspace_root()
    if not root or not owner:
        return ""
    return str(await anyio.Path(root, owner).resolve())


async def public_dir() -> str:
    """Absolute path of the shared area; ``""`` when disabled."""
    root = workspace_root()
    return str(await anyio.Path(root, PUBLIC_DIRNAME).resolve()) if root else ""


def _is_within(path: str, base: str) -> bool:
    """Whether *path* sits inside *base* (inclusive). Both must already be resolved."""
    if not base:
        return False
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:
        # Different drive letters (Windows) — necessarily not inside.
        return False


def _first_segment(real: str, real_root: str) -> str:
    """First path segment of *real* relative to *real_root*; ``""`` when it is the root.

    Pure string math (no IO), split out as a sync helper because ruff's ASYNC240
    rejects ``os.path`` calls inside async functions even when they never touch the
    disk — extracting is cleaner than a noqa (see the zero-suppression rule).
    """
    rel = os.path.relpath(real, real_root)
    if rel in (".", os.curdir):
        return ""
    return rel.replace("\\", "/").split("/")[0]


async def owner_of(path: str) -> str:
    """Who owns *path*; the shared area and anything outside root are ``""`` (ownerless).

    Resolves the real path first, so symlinks and ``..`` are both expanded — that is
    the non-negotiable precondition for prefix comparison to mean anything.

    Uses ``anyio.Path.resolve()`` rather than ``os.path.realpath``: the latter stats
    the disk, which is synchronous IO inside an async context and violates the
    all-async rule (same call made in ``gateway/_scheduler_manager._workspace_key``).
    """
    root = workspace_root()
    if not root:
        return ""
    real_root = str(await anyio.Path(root).resolve())
    try:
        real = str(await anyio.Path(path).resolve())
    except OSError:
        return ""
    if not _is_within(real, real_root):
        return ""  # Outside root (system dirs / agent package) belongs to nobody.
    first = _first_segment(real, real_root)
    if not first or first == PUBLIC_DIRNAME:
        return ""  # Root itself and the shared area are ownerless.
    return first


async def check_read(path: str, *, session_id: str) -> str | None:
    """``None`` when readable, else the refusal text (tools return it as their error).

    Reads are permissive: one's own space, the shared area, and anything outside root
    (agent package / system paths) all pass. Only **another** user's paths are denied.
    """
    if not enabled():
        return None
    target_owner = await owner_of(path)
    if not target_owner:
        return None
    me = owner_from_session_id(session_id)
    if not me:
        # Ownerless session (SPA-created / run locally) is not subject to isolation.
        return None
    if target_owner == me:
        return None
    return f"[Error] 拒绝访问: {path} 属于另一个用户的私有空间。每位用户的文件互相隔离, 只能访问自己的空间与公共区。"


async def check_write(path: str, *, session_id: str) -> str | None:
    """``None`` when writable, else the refusal text.

    Writes are stricter than reads: the shared area is **not** writable either, or one
    user could overwrite common material and affect everyone.
    """
    if not enabled():
        return None
    me = owner_from_session_id(session_id)
    if not me:
        return None
    root = workspace_root()
    try:
        real = str(await anyio.Path(path).resolve())
    except OSError:
        return None
    if not _is_within(real, str(await anyio.Path(root).resolve())):
        return None  # Outside root stays with the pre-existing logic (agent package…).
    mine = await owner_dir(me)
    if mine and _is_within(real, mine):
        return None
    return f"[Error] 拒绝写入: {path} 不在你的私有空间内。请写到自己的空间(相对路径即可), 公共区与他人空间均为只读。"


async def forbidden_dirs(session_id: str) -> list[str]:
    """Directories a walking tool must skip wholesale (every owner but self and public).

    Used by ``search_content`` / ``find_files``: equivalent to calling ``check_read``
    per candidate, without resolving every single file.
    """
    if not enabled():
        return []
    me = owner_from_session_id(session_id)
    if not me:
        return []
    out: list[str] = []
    try:
        async for child in anyio.Path(workspace_root()).iterdir():
            if child.name in (me, PUBLIC_DIRNAME):
                continue
            if await child.is_dir():
                out.append(str(await child.resolve()))
    except OSError:
        return []
    return sorted(out)


def owns_session(candidate_session_id: str, *, session_id: str) -> bool:
    """Whether *candidate_session_id*'s history belongs to the current session's owner.

    Cross-session history tools (``sessions_list`` / ``session_keyword_search`` …) scan
    the **global** AppData ``histories/*.jsonl``, which is not partitioned by
    workspace. Raw transcripts are often more sensitive than generated files, so they
    must be filtered down to one's own.

    Pure string comparison (no IO), hence sync.
    """
    if not enabled():
        return True
    me = owner_from_session_id(session_id)
    if not me:
        return True
    return owner_from_session_id(candidate_session_id) == me


async def scan_command(command: str, *, session_id: str) -> str | None:
    """Heuristic shell scan: refusal text when another user's space is referenced.

    **This layer is a heuristic, not a sandbox** — spelling out someone else's open_id
    or directory name is caught, but variable splicing (``d=ou_x; cat /ws/$d/f``),
    base64, or ``eval`` get through. It is still worth having: real leaks are almost
    always the blunt "just ls that person's folder" shape. Strong isolation requires
    one container per user.

    Matching requires the other party's directory name to appear as a **whole path
    segment**, not a bare substring — the latter would false-positive on a legitimate
    file like ``/ws/me/ou_other_notes.md`` inside one's own space.
    """
    if not enabled() or not command:
        return None
    me = owner_from_session_id(session_id)
    if not me:
        return None
    for full in await forbidden_dirs(session_id):
        name = os.path.basename(full)
        # Only count a hit when flanked by separators / quotes / whitespace / ends.
        if re.search(rf"(^|[\s'\"/\\=:]){re.escape(name)}([\s'\"/\\]|$)", command):
            return (
                f"[Error] 拒绝执行: 命令中引用了另一个用户的私有空间 ({name})。"
                "每位用户的文件互相隔离, 只能访问自己的空间与公共区。"
            )
    return None


async def blocks_send(path: str, *, open_id: str, chat_id: str = "", chat_type: str = "") -> bool:
    """``[SEND:]`` side check: True means this file must not go to this conversation.

    The channel is a separate process with no ``runtime_context``, but it does have the
    conversation facts — so the owner is derived from open_id / chat here, **without
    depending on workspace contents**.
    """
    if not enabled():
        return False
    me = f"chat-{sanitize(chat_id)}" if chat_type in ("group", "topic") and chat_id else sanitize(open_id)
    if not me:
        return True  # Unidentifiable recipient — refuse conservatively.
    target_owner = await owner_of(path)
    if not target_owner:
        return False  # Shared-area / outside-root artifacts are fine to send.
    return target_owner != me

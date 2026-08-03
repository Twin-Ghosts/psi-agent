"""Feishu/Lark chat (group) tools — find a group the bot belongs to by name,
resolve a member's open_id, create a new group (拉人建群), and run it afterwards
(read its settings, add/remove members).

Use ``feishu_chat_find`` to resolve a human-given group name (e.g. "主群") into a
``chat_id`` before sending messages (the bot must already be a member), or
``feishu_chat_create`` to spin up a brand-new group and pull people into it. Once a
group exists, ``feishu_chat_get`` reads who owns it and how it is configured, and
``feishu_chat_add_members`` / ``feishu_chat_remove_members`` change its roster.
Pair with ``feishu_message`` (send / reply-in-thread / list messages).
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f


async def feishu_chat_find(name: str, exact: bool = False, page_size: int = 50, page_token: str = "") -> str:
    """Find Feishu/Lark groups the bot is in whose name matches ``name``.

    Returns candidate groups as ``{chat_id, name, description}``. If several
    match, all are returned — pick the right ``chat_id`` before sending.

    Args:
        name: Group name (or keyword) to search for.
        exact: When true, keep only groups whose name equals ``name`` exactly.
        page_size: Max groups to return (default 50).
        page_token: Pagination cursor from a previous call's has_more result (optional).
    """
    return _f.dumps_result(await _f.find_chat_impl(name, exact, page_size, page_token))


async def feishu_chat_find_member(
    chat_id: str, name: str = "", exact: bool = False, member_id_type: str = "open_id"
) -> str:
    """Resolve a group member's user id (open_id) by their name.

    Feishu bots can't search all users by name, so this lists the group's members
    (each carries a name + id) and matches by name. Use it to turn a person's name
    into an ``open_id`` before @-mentioning or direct-messaging them. Pages through
    the full roster automatically.

    Returns matches as ``{name, id, member_id_type}``. If several people share the
    name, all are returned — pick the right ``id``.

    Args:
        chat_id: The group's chat_id (from ``feishu_chat_find``). The person must be a member.
        name: Person's name to match. Empty returns the whole roster.
        exact: When true, match the name exactly; otherwise substring match.
        member_id_type: Id form to return — open_id (default), union_id, or user_id.
    """
    return _f.dumps_result(await _f.find_member_id_impl(chat_id, name, exact, member_id_type))


async def feishu_chat_list_members(chat_id: str, member_id_type: str = "open_id") -> str:
    """List every member of a Feishu/Lark group in one call.

    Unlike ``feishu_chat_find_member`` (which searches by a specific name), this
    returns the group's whole roster — use it when you need everyone in the group,
    not just a matched person. Pages through the full roster automatically.

    Returns members as ``{name, id, member_id_type}`` plus a total ``count``.

    Args:
        chat_id: The group's chat_id (from ``feishu_chat_find``). The bot must be a member.
        member_id_type: Id form to return — open_id (default), union_id, or user_id.
    """
    return _f.dumps_result(await _f.list_chat_members_impl(chat_id, member_id_type))


async def feishu_chat_create(
    name: str,
    user_ids: list[str] | None = None,
    description: str = "",
    owner_id: str = "",
    user_id_type: str = "open_id",
) -> str:
    """Create a **new** Feishu/Lark group chat and pull the given people in (拉人建群).

    Use this when there is no existing group to post to — the bot creates the group,
    hands it to the **requester** as owner (``owner_id``), and stays on as an admin so
    you can still send to the returned ``chat_id`` with ``feishu_message_send``. This is
    the missing piece versus ``feishu_message_send``, which can only post to a group
    that already exists.

    Members are given as user ids, not names: resolve names to open_ids first with
    ``feishu_chat_find_member`` (from another group) or ``feishu_department_members``.
    The response includes the new ``chat_id`` and ``invalid_user_ids`` (ids Feishu
    could not add — e.g. outside the app's contact scope).

    Args:
        name: Group name (required).
        user_ids: Members to invite — a list of ids matching ``user_id_type`` (max 50).
            Empty creates a group with just the bot; invite more later.
        description: Group description/topic (optional).
        owner_id: Id (matching ``user_id_type``) of the person to make group owner.
            **Default to the requester** — pass the ``sender_open_id`` from
            ``<feishu_context>`` so the person who asked for the group owns it (the bot
            stays an admin and can keep posting). Pass someone else's id if the requester
            explicitly wants another person to be owner. Leave empty only for a
            bot-authored group with no human requester (the bot then owns it).
        user_id_type: Id form used by user_ids/owner_id — open_id (default), union_id, or user_id.
    """
    return _f.dumps_result(await _f.create_chat_impl(name, user_ids, description, owner_id, user_id_type))


async def feishu_chat_get(chat_id: str, user_id_type: str = "open_id", user_key: str = "") -> str:
    """Read a Feishu/Lark group's **details** — owner, member counts, and settings.

    The question this answers before you act on a group: **who owns it** (only the owner
    or an admin may add/remove members or 置顶 in most groups — pass their ``user_key``
    to those tools), **how many people are in it** (a 500-person group is not somewhere
    to send a test message), and **what it allows** (whether the bot can add members
    at all, whether @所有人 is permitted, whether 保密模式 blocks downloads).

    ``settings`` comes back as readable Chinese pairs (e.g. ``{"谁可以加人": "仅群主和管理员"}``)
    rather than Feishu's bare ``only_owner`` enums. ``owner_is_bot`` is true when the
    group is owned by a bot, which is why no ``owner_id`` is returned — not an error.

    Feishu answers a **non-member** caller with only the name, avatar, counts and status;
    that comes back as ``partial=true``. Don't read a thin result as "这个群没有群主/没有
    设置" — add the bot to the group (or pass a member's ``user_key``) and ask again.

    Args:
        chat_id: The group's chat_id (``oc_...``, from ``feishu_chat_find``).
        user_id_type: Id form for owner/admin ids — open_id (default), union_id, or user_id.
        user_key: A group member's open_id as a fallback identity, for a group the bot
            isn't in (optional).
    """
    return _f.dumps_result(await _f.get_chat_impl(chat_id, user_id_type, user_key))


async def feishu_chat_add_members(
    chat_id: str,
    user_ids: list[str] | None = None,
    member_id_type: str = "open_id",
    succeed_type: int = 1,
    user_key: str = "",
) -> str:
    """Add people (or bots) to an **existing** Feishu/Lark group (拉人进群).

    The counterpart to ``feishu_chat_create``, which can only pull people in at creation
    time: use this to grow a group that already exists — onboarding a new teammate into
    the project group, pulling a reviewer into a discussion.

    Members are given as ids, not names: resolve them first with
    ``feishu_chat_find_member`` (from another group), ``feishu_contact_search``, or
    ``feishu_department_members``. To add a **bot**, pass its App ID with
    ``member_id_type="app_id"``.

    Partial results are the normal case and are reported separately, because the fix
    differs: ``invalid_ids`` (outside the app's scope, or the person has left),
    ``not_existed_ids`` (no such id), and ``pending_approval_ids`` — those people **will**
    join once the owner approves, so don't re-add them.

    Most groups restrict 加人 to the owner and admins, and the bot is neither unless it
    created the group. That failure is Feishu 232017; pass the owner's/admin's
    ``user_key`` to act as them, or ask them to change 「谁可以添加群成员」 to 所有群成员.
    Check first with ``feishu_chat_get`` (``settings["谁可以加人"]``).

    Args:
        chat_id: Target group's chat_id (``oc_...``, from ``feishu_chat_find``).
        user_ids: Ids to add — max 50 users (or 5 bots) per call; duplicates are dropped.
        member_id_type: Id form of user_ids — open_id (default), union_id, user_id, or
            app_id (for bots).
        succeed_type: What to do about ids Feishu can't reach — 1 (default) adds everyone
            reachable and reports the rest; 0 fails the whole call over one bad id;
            2 fails strictly on any unusable id. Leave at 1 unless all-or-nothing matters.
        user_key: The owner's/admin's open_id, to add members as that person when the bot
            lacks the right (optional).
    """
    return _f.dumps_result(await _f.add_chat_members_impl(chat_id, user_ids, member_id_type, succeed_type, user_key))


async def feishu_chat_remove_members(
    chat_id: str,
    user_ids: list[str] | None = None,
    member_id_type: str = "open_id",
    user_key: str = "",
) -> str:
    """Remove people (or bots) from a Feishu/Lark group (移出群成员).

    Use it to clean up a group after someone changes teams or a temporary discussion
    ends. Ids Feishu refused come back in ``invalid_ids`` rather than vanishing, so
    compare ``removed`` against what you asked for before reporting success.

    Two Feishu rules decide whether this works, and both surface as a ``hint``:
    only the **owner**, an admin, or the bot that **created** the group may remove other
    people (232017 — pass that person's ``user_key``; anyone may always remove
    themselves), and the **owner cannot be removed** at all (232076 — transfer ownership
    first). Removing someone is visible to the group and not undoable by this tool
    (they must be re-added), so confirm the right people with
    ``feishu_chat_list_members`` before calling.

    Args:
        chat_id: Target group's chat_id (``oc_...``, from ``feishu_chat_find``).
        user_ids: Ids to remove — max 50 users (or 5 bots) per call.
        member_id_type: Id form of user_ids — open_id (default), union_id, user_id, or
            app_id (for bots).
        user_key: The owner's/admin's open_id, to remove members as that person when the
            bot lacks the right (optional).
    """
    return _f.dumps_result(await _f.remove_chat_members_impl(chat_id, user_ids, member_id_type, user_key))

"""Idempotently open one mentor's TODO ledger base and confine who can see it.

Each mentor gets their **own** base rather than a shared table with a filtered view.
Row-level isolation inside one base needs a custom role, custom roles need advanced
permissions turned on, and advanced permissions cannot be enabled once the base sits in a
wiki or is embedded in a doc (error 1254301). A filter view is not a permission — anyone
holding the link sees the whole table. Splitting per mentor reduces isolation to "who is
this file shared with", which Feishu guarantees natively.

It is a tool rather than a paragraph in a skill because three things must hold together:
opening must be idempotent (a second run must not produce a second ledger, or the data
forks silently), the grant list must be exact (one extra collaborator leaks another
mentor's reports), and ``table_id`` must be resolved rather than guessed (a copied base
does not keep the template's ids).

Args:
    mentor_open_id: The mentor's ``ou_...`` open_id — gets edit access.
    mentor_name: Goes into the ledger name ``TODO台账-<mentor_name>``, which is also the
        idempotency key, so keep it stable across cycles.
    folder_token: The Drive folder all ledgers live in. Required: the lookup that makes
        this idempotent scans exactly this folder.
    boss_open_id: Optional. The boss's open_id — gets read-only access.
    template_app_token: A ledger template base to copy (structure only). Omit to create an
        empty base, which arrives with a single placeholder column and no ledger columns.
    table_name: The todo table's name inside the ledger. Empty = the table whose name
        contains todo / 台账.
    user_key: The sender's open_id (from ``<feishu_context>``). Writes go out under this
        identity, so it must belong to someone who can create files in that folder.
"""

from __future__ import annotations

import _feishu_impl as _f


async def feishu_mentor_ledger_ensure(
    mentor_open_id: str,
    mentor_name: str,
    folder_token: str,
    boss_open_id: str = "",
    template_app_token: str = "",
    table_name: str = "",
    user_key: str = "",
) -> str:
    """Ensure this mentor's TODO ledger exists exactly once, and return its ids.

    Safe to call every cycle: the ledger is located by name inside ``folder_token`` and
    reused when found, so two runs never leave two ledgers. ``created`` tells you which
    happened; ``app_token`` / ``table_id`` are what the rest of the pipeline writes into.

    Access is converged, not reset: the mentor (edit) and the boss (read-only) are added
    when missing and left alone when already correct. Collaborators outside that pair are
    reported in ``unexpected_members`` and **not** removed — revoking access is
    irreversible, so a human decides. The bot is not added here; it reaches the base
    through ``user_key``'s authorization, not as a collaborator of its own.

    Re-run this when a mentor changes or their reports change: grants are re-converged
    while历史数据 stays in the original base (nothing is migrated).
    """
    return _f.dumps_result(
        await _f.ensure_mentor_ledger_impl(
            mentor_open_id=mentor_open_id,
            mentor_name=mentor_name,
            folder_token=folder_token,
            boss_open_id=boss_open_id,
            template_app_token=template_app_token,
            table_name=table_name,
            user_key=user_key,
        )
    )

"""Read the 「请假表」 sub-sheet and decide which days of a window each person is on leave.

Feishu has no endpoint that enumerates leave by person: ``/approval/v4/instances`` only
creates an instance or fetches one by id, and the attendance API returns punch results
(正常/迟到/早退/缺卡), not leave applications. So leave is filed as a sheet the same way
todos are, and this tool turns those rows into a decision.

It is a tool rather than a paragraph in a skill because overlapping-interval arithmetic is
pure logic: left in the model's hands it is a calendar computation redone every cycle, and
one slip assigns work to someone on holiday and books them overdue. The rule for "which
day counts as leave" belongs in code, once.

Args:
    sheet_token: The spreadsheet holding both the todo table and the 「请假表」 sub-sheet
        (the segment in a ``feishu.cn/sheets/<token>`` URL).
    sheet_name: The leave sub-sheet's title. Empty = find the one whose title contains
        请假 / 休假 / leave.
    date_from: First day of the window, ISO (``2026-08-05``). Inclusive.
    date_to: Last day of the window, inclusive. Empty = same as ``date_from``.
    names_json: Optional JSON array of names to restrict the answer to, e.g.
        ``'["张三","李四"]'``. Empty = everyone in the sheet.
    user_key: The sender's open_id (from ``<feishu_context>``).
"""

from __future__ import annotations

import _feishu_impl as _f


async def feishu_leave_query(
    sheet_token: str,
    sheet_name: str = "",
    date_from: str = "",
    date_to: str = "",
    names_json: str = "",
    user_key: str = "",
) -> str:
    """Decide who is on leave on which days of ``[date_from, date_to]``, from the 请假表.

    Call this **before** dispatching a cycle's todos — that ordering is what keeps work off
    people who are away. The fixed rules it applies, so nobody re-derives them:

    - the window and each leave interval are **closed** — both endpoints count as leave;
    - a blank 结束日期 means one day of leave (the start day);
    - a blank 是否整天 means a full day (half days must say 否 / 半天);
    - rows with a missing name or an unparseable date land in ``needs_fix`` and are **not**
      silently dropped — dropping them would turn "filled in wrong" into "not on leave";
    - somebody absent from the sheet is not on leave (a missing entry is not an absence).

    The result carries ``on_leave`` (per person: the hit dates, each leave interval, and
    ``full_period`` when the whole window is covered), ``full_period_names`` for the people
    to skip dispatching entirely, and ``needs_fix`` for the rows a human must repair.
    """
    return _f.dumps_result(
        await _f.query_leave_impl(
            sheet_token=sheet_token,
            sheet_name=sheet_name,
            date_from=date_from,
            date_to=date_to,
            names_json=names_json,
            user_key=user_key,
        )
    )

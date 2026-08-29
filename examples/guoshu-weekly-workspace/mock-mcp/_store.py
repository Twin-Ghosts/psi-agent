"""Read-only query layer over the demo's MySQL mock store.

Every business rule from chapter 七 of the plan lives here, on the *service*
side of the MCP boundary -- the agent cannot bypass or misstate them.  The
formal-task filter (R-01: ``is_deleted = 0 AND workflow_status = 'published'``)
is applied by ``formal_task_clause`` and reported back in each result envelope
so the agent can cite the caliber it actually got.

Placeholders are pymysql's ``%(name)s`` form.  Values are always bound, never
interpolated -- the injection surface stays closed even though the agent cannot
reach this layer directly.
"""

# ruff: noqa: RUF001  中文口径文案里的全角标点是给模型看的字面量, 不能换成半角。
from __future__ import annotations

import re
from typing import Any

import _db
import pymysql

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
"""Dates reach SQL as bound parameters, so this is a clarity guard, not the
injection defense: a malformed date would otherwise be silently coerced by MySQL
and answer a different question than the one asked."""

MAX_ROWS = 200
"""Hard cap on rows returned to the agent.

Chat context is the scarce resource here: an unbounded task dump would crowd
out the conversation it is meant to support.  Truncation is reported via
``has_more`` rather than silently applied.
"""

FORMAL_TASK_CALIBER = "is_deleted = 0 AND workflow_status = 'published'"

# Fields that must never cross the MCP boundary (chapter 7.2 of the plan).
BLOCKED_FIELDS = frozenset({"storage_path", "payload"})

# Fields released only when the caller holds the matching permission.
SENSITIVE_FIELDS = frozenset({"review_comment", "opinion"})

SNAPSHOT_NOTE = "演示数据（weekly_mock 自建库），非集团真实周报"

AS_OF = "2026-08-15"
"""The snapshot's "today" -- every relative time window is measured from here.

Not ``CURDATE()``, and this is the whole point.  The data stops at
``progress_date`` 2026-08-01 while the machine's wall clock is well past it, so
answering "the last 30 days" from the real clock silently slides the window off
the data and returns a smaller count than the truth.  The question bank flags
this as the ``now_instead_of_as_of`` trap.

Fixing the anchor on the service side also removes the model's ability to get it
wrong: it never has to know today's date, and cannot substitute its own.
"""


def as_of_caliber() -> str:
    return f"相对时间窗以数据快照日 {AS_OF} 为基准（非当前系统时间）"


def date_window(
    date_from: str = "",
    date_to: str = "",
    last_days: str | int | None = None,
) -> tuple[str, str]:
    """Resolve a caller's time window into an inclusive ``(from, to)`` date pair.

    ``last_days`` is relative to :data:`AS_OF`.  An empty window means "no time
    filter" and is returned as ``("", "")`` so callers can skip the clause
    entirely rather than binding a wide-open range.
    """
    lo = (date_from or "").strip()
    hi = (date_to or "").strip()
    if last_days is not None and str(last_days).strip():
        try:
            days = int(str(last_days).strip())
        except (TypeError, ValueError) as exc:
            raise QueryError("invalid_argument", f"last_days 必须是整数：{last_days!r}") from exc
        if days <= 0:
            raise QueryError("invalid_argument", f"last_days 必须为正数：{days}")
        conn = connect()
        try:
            row = _one(
                conn,
                "SELECT DATE_SUB(%(as_of)s, INTERVAL %(d)s DAY) AS lo",
                {"as_of": AS_OF, "d": days},
            )
        finally:
            conn.close()
        lo = str(row["lo"]) if row else lo
        hi = hi or AS_OF
    for label, value in (("date_from", lo), ("date_to", hi)):
        if value and not _DATE_RE.match(value):
            raise QueryError("invalid_argument", f"{label} 需为 YYYY-MM-DD：{value!r}")
    return lo, hi


def window_clause(column: str, lo: str, hi: str, params: dict[str, Any], *, prefix: str = "w") -> str:
    """Build the bound SQL fragment for a resolved window. ``column`` is caller-controlled."""
    parts: list[str] = []
    if lo:
        params[f"{prefix}_lo"] = lo
        parts.append(f"{column} >= %({prefix}_lo)s")
    if hi:
        params[f"{prefix}_hi"] = hi
        parts.append(f"{column} <= %({prefix}_hi)s")
    return " AND ".join(parts)


def window_caliber(lo: str, hi: str, *, label: str) -> str:
    if lo and hi:
        return f"{label} 介于 {lo} 与 {hi} 之间（含端点）"
    if lo:
        return f"{label} 自 {lo} 起（含）"
    if hi:
        return f"{label} 截至 {hi}（含）"
    return f"{label} 未设时间过滤"


GROUP_BOARD_CODE = "group"
"""``task_board.code`` for the 集团组 board -- the stable business key, not the id.

The name is editable in the OA, the id is an autoincrement that differs between
the mock and the real store; only ``code`` survives both.
"""


def group_board_join(alias: str = "t", board: str = "b") -> str:
    """Restrict a task query to the 集团组 board, soft-deleted boards excluded."""
    return (
        f"JOIN task_board {board} ON {board}.id = {alias}.board_id "
        f"AND {board}.is_deleted = 0 AND {board}.code = '{GROUP_BOARD_CODE}'"
    )


def group_history_gate(hist: str = "h", alias: str = "t") -> str:
    """The two gates every ``task_group_progress_history`` read must pass.

    Both, not either.  The task must be a formal task (R-01) *and* the history
    row itself must be published: 404 rows exist, 362 pass both, and dropping
    ``is_published`` silently folds 42 un-approved drafts into the answer.  The
    question bank tracks this separately from plain ``publish_gate`` because the
    row-level flag has no counterpart on the task side.
    """
    return f"{formal_task_clause(alias)} AND {hist}.is_published = 1"


GROUP_HISTORY_CALIBER = (
    "任务侧 is_deleted = 0 AND workflow_status = 'published'，"
    "且历史行自身 is_published = 1（两道闸门缺一不可，共 404 行、过闸 362 行）"
)


class QueryError(Exception):
    """A caller-visible failure that carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def connect() -> Any:
    try:
        return _db.connect()
    except pymysql.Error as exc:
        raise QueryError(
            "store_unreachable",
            f"cannot reach {_db.DSN_DESCRIPTION}: {exc.args[-1] if exc.args else exc}",
        ) from exc


def formal_task_clause(alias: str = "t") -> str:
    """R-01: the formal-task caliber, cited by 111 of the 396 test questions."""
    return f"{alias}.is_deleted = 0 AND {alias}.workflow_status = 'published'"


def _scrub(row: dict[str, Any], *, can_read_sensitive: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key in BLOCKED_FIELDS:
            continue
        if key in SENSITIVE_FIELDS and not can_read_sensitive:
            out[key] = "[按权限不展示]"
            continue
        out[key] = value
    return out


def fetch(
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    caliber: str = "",
    can_read_sensitive: bool = False,
    limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """Run a read-only query and wrap it in the standard result envelope."""
    bounded = max(1, min(MAX_ROWS, int(limit)))
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or {})
            # One extra row distinguishes "exactly full" from "truncated".
            raw = cursor.fetchmany(bounded + 1)
            columns = [d[0] for d in cursor.description] if cursor.description else []
    except pymysql.Error as exc:
        raise QueryError("query_failed", str(exc.args[-1] if exc.args else exc)) from exc
    finally:
        conn.close()

    has_more = len(raw) > bounded
    rows = [_scrub(dict(r), can_read_sensitive=can_read_sensitive) for r in raw[:bounded]]
    if rows:
        columns = list(rows[0].keys())
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "has_more": has_more,
        "caliber": caliber or "无附加口径",
        "snapshot_note": SNAPSHOT_NOTE,
    }


def scalar(sql: str, params: dict[str, Any] | None = None, *, caliber: str = "") -> dict[str, Any]:
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params or {})
            row = cursor.fetchone()
    except pymysql.Error as exc:
        raise QueryError("query_failed", str(exc.args[-1] if exc.args else exc)) from exc
    finally:
        conn.close()
    value = None if not row else next(iter(row.values()))
    return {
        "ok": True,
        "value": value,
        "caliber": caliber or "无附加口径",
        "snapshot_note": SNAPSHOT_NOTE,
    }


def _one(conn: Any, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return dict(row) if row else None


def resolve_board(board: str) -> int | None:
    """Map a board code or name to its id. Returns None when unmatched."""
    token = (board or "").strip()
    if not token:
        return None
    conn = connect()
    try:
        row = _one(
            conn,
            "SELECT id FROM task_board WHERE is_deleted = 0 AND (code = %(t)s OR name = %(t)s)",
            {"t": token},
        )
        if row is not None:
            return int(row["id"])
        row = _one(
            conn,
            "SELECT id FROM task_board WHERE is_deleted = 0 AND name LIKE %(like)s",
            {"like": f"%{token}%"},
        )
        return None if row is None else int(row["id"])
    finally:
        conn.close()


def resolve_task(task: str) -> dict[str, Any] | None:
    """Locate one formal task by id, or by name.

    A purely numeric token is an id and nothing else.  Falling through to name
    matching on a numeric miss is what made ``task="2"`` return a *different*
    task: id 2 is not published, so the LIKE branch matched some other row whose
    name merely contained "2", and the caller then reported that row's data as
    task 2's.  A wrong task silently substituted for the requested one is worse
    than an honest miss.
    """
    token = (task or "").strip()
    if not token:
        return None
    conn = connect()
    try:
        if token.isdigit():
            return _one(
                conn,
                f"SELECT * FROM task t WHERE t.id = %(id)s AND {formal_task_clause()}",
                {"id": int(token)},
            )
        row = _one(
            conn,
            f"SELECT * FROM task t WHERE {formal_task_clause()} AND t.task_name = %(name)s",
            {"name": token},
        )
        if row is not None:
            return row
        # Shortest match wins: with substring matching, the shortest name is the
        # least over-specified reading of what the user typed.
        return _one(
            conn,
            f"SELECT * FROM task t WHERE {formal_task_clause()} AND t.task_name LIKE %(like)s "
            "ORDER BY CHAR_LENGTH(t.task_name) LIMIT 1",
            {"like": f"%{token}%"},
        )
    finally:
        conn.close()


def resolve_task_id(task: str) -> int | None:
    """Resolve a foreign-key task_id, WITHOUT the formal-task filter.

    Submissions, attachments and progress rows hang off ``task_id`` as a plain
    foreign key.  Questions about them ("task 2's submissions") are asking about
    that key, not about whether the parent task is currently published -- gating
    on R-01 here silently drops rows that genuinely exist.  Returns the integer
    directly for a numeric token; otherwise falls back to formal-task name
    resolution, which is the only way a name can be turned into an id.
    """
    token = (task or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    found = resolve_task(token)
    return None if found is None else int(found["id"])

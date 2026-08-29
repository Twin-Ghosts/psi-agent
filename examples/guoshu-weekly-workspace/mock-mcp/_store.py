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

from typing import Any

import _db
import pymysql

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

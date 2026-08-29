"""Read-only query layer over the demo's SQLite mock store.

Every business rule from chapter 七 of the plan lives here, on the *service*
side of the MCP boundary -- the agent cannot bypass or misstate them.  The
formal-task filter (R-01: ``is_deleted = 0 AND workflow_status = 'published'``)
is applied by ``formal_task_clause`` and reported back in each result envelope
so the agent can cite the caliber it actually got.
"""

# ruff: noqa: RUF001  中文口径文案里的全角标点是给模型看的字面量, 不能换成半角。
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import _sqlite_compat as compat

DB_PATH = os.environ.get(
    "GUOSHU_WEEKLY_MOCK_DB",
    str(Path(__file__).resolve().parent / "weekly_mock.sqlite3"),
)

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


class QueryError(Exception):
    """A caller-visible failure that carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def connect() -> sqlite3.Connection:
    if not Path(DB_PATH).exists():
        raise QueryError(
            "store_missing",
            f"mock store not built: {DB_PATH}. Run build_sqlite.py first.",
        )
    return compat.connect(DB_PATH, read_only=True)


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
        cursor = conn.execute(compat.rewrite_sql(sql), params or {})
        raw = cursor.fetchmany(bounded + 1)
        columns = [d[0] for d in cursor.description] if cursor.description else []
    except sqlite3.Error as exc:
        raise QueryError("query_failed", str(exc)) from exc
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
        "snapshot_note": "演示数据（weekly_mock 自建库），非集团真实周报",
    }


def scalar(sql: str, params: dict[str, Any] | None = None, *, caliber: str = "") -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(compat.rewrite_sql(sql), params or {}).fetchone()
    except sqlite3.Error as exc:
        raise QueryError("query_failed", str(exc)) from exc
    finally:
        conn.close()
    value = None if row is None else row[0]
    return {
        "ok": True,
        "value": value,
        "caliber": caliber or "无附加口径",
        "snapshot_note": "演示数据（weekly_mock 自建库），非集团真实周报",
    }


def resolve_board(board: str) -> int | None:
    """Map a board code or name to its id. Returns None when unmatched."""
    token = (board or "").strip()
    if not token:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM task_board WHERE is_deleted = 0 AND (code = :t OR name = :t)",
            {"t": token},
        ).fetchone()
        if row is not None:
            return int(row[0])
        row = conn.execute(
            "SELECT id FROM task_board WHERE is_deleted = 0 AND name LIKE :like",
            {"like": f"%{token}%"},
        ).fetchone()
        return None if row is None else int(row[0])
    finally:
        conn.close()


def resolve_task(task: str) -> dict[str, Any] | None:
    """Locate one formal task by id or (fuzzy) name."""
    token = (task or "").strip()
    if not token:
        return None
    conn = connect()
    try:
        if token.isdigit():
            row = conn.execute(
                f"SELECT * FROM task t WHERE t.id = :id AND {formal_task_clause()}",
                {"id": int(token)},
            ).fetchone()
            if row is not None:
                return dict(row)
        row = conn.execute(
            f"SELECT * FROM task t WHERE {formal_task_clause()} AND t.task_name = :name",
            {"name": token},
        ).fetchone()
        if row is not None:
            return dict(row)
        row = conn.execute(
            f"SELECT * FROM task t WHERE {formal_task_clause()} AND t.task_name LIKE :like "
            "ORDER BY LENGTH(t.task_name) LIMIT 1",
            {"like": f"%{token}%"},
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()

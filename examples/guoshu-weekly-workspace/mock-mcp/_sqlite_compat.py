"""MySQL compatibility shims for the demo's SQLite mock store.

The mock dump is a MySQL 8.4 export and the reference SQL in the test set uses
MySQL builtins.  SQLite has none of them, so the demo registers the subset the
weekly data actually needs.  This keeps the *demo's own* store faithful to the
MySQL semantics the real 入口组 service will have; it is not part of the MCP
contract the agent sees.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any

_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _datediff(left: object, right: object) -> int | None:
    a, b = _parse(left), _parse(right)
    if a is None or b is None:
        return None
    return (a.date() - b.date()).days


def _year(value: object) -> int | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.year


def _month(value: object) -> int | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.month


def _quarter(value: object) -> int | None:
    parsed = _parse(value)
    return None if parsed is None else (parsed.month - 1) // 3 + 1


def _date_format(value: object, fmt: object) -> str | None:
    parsed = _parse(value)
    if parsed is None:
        return None
    mapping = {
        "%Y": "%Y",
        "%m": "%m",
        "%d": "%d",
        "%H": "%H",
        "%i": "%M",
        "%s": "%S",
    }
    out = str(fmt)
    for mysql_token, py_token in mapping.items():
        out = out.replace(mysql_token, py_token)
    return parsed.strftime(out)


def _left(value: object, length: str | int | None) -> str | None:
    if value is None:
        return None
    try:
        size = int(length) if length is not None else 0
    except TypeError, ValueError:
        return None
    return str(value)[:size]


def _char_length(value: object) -> int | None:
    return None if value is None else len(str(value))


def _regexp(pattern: object, value: object) -> int:
    """MySQL's `value REGEXP pattern` -- SQLite passes (pattern, value)."""
    if value is None or pattern is None:
        return 0
    try:
        return 1 if re.search(str(pattern), str(value)) else 0
    except re.error:
        return 0


def _find_in_set(needle: object, haystack: object) -> int:
    if needle is None or haystack is None:
        return 0
    parts = [p.strip() for p in str(haystack).split(",")]
    target = str(needle).strip()
    for index, part in enumerate(parts, start=1):
        if part == target:
            return index
    return 0


def _substring_index(value: object, delim: object, count: str | int | None) -> str | None:
    if value is None:
        return None
    text, sep = str(value), str(delim)
    try:
        number = int(count) if count is not None else 0
    except TypeError, ValueError:
        return None
    if number == 0:
        return ""
    parts = text.split(sep)
    if number > 0:
        return sep.join(parts[:number])
    return sep.join(parts[number:])


def _json_unquote(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        parsed = json.loads(text)
    except ValueError, TypeError:
        return text
    return parsed if isinstance(parsed, str) else text


def _json_keys(value: object) -> str | None:
    if value is None:
        return None
    try:
        parsed = json.loads(str(value))
    except ValueError, TypeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return json.dumps(list(parsed.keys()), ensure_ascii=False)


def _json_extract(value: object, path: object) -> str | None:
    if value is None:
        return None
    try:
        parsed = json.loads(str(value))
    except ValueError, TypeError:
        return None
    target = str(path).lstrip("$").lstrip(".")
    if not target:
        return json.dumps(parsed, ensure_ascii=False)
    current: Any = parsed
    for piece in target.replace("]", "").split("."):
        if not piece:
            continue
        if piece.startswith("["):
            if not isinstance(current, list):
                return None
            try:
                current = current[int(piece[1:])]
            except IndexError, TypeError, ValueError:
                return None
            continue
        if isinstance(current, dict) and piece in current:
            current = current[piece]
        else:
            return None
    if isinstance(current, str):
        return json.dumps(current, ensure_ascii=False)
    return json.dumps(current, ensure_ascii=False)


_INTERVAL = re.compile(
    r"\bINTERVAL\s+(\d+)\s+(DAY|MONTH|YEAR|WEEK|HOUR|MINUTE|SECOND)\b",
    re.I,
)
_DATE_ADD = re.compile(r"\bDATE_(ADD|SUB)\s*\(", re.I)


def rewrite_sql(sql: str) -> str:
    """Rewrite MySQL date arithmetic into SQLite `datetime(...)` modifiers.

    ``DATE_SUB(x, INTERVAL 90 DAY)`` -> ``datetime(x, '-90 day')``
    ``DATE_ADD(x, INTERVAL 1 MONTH)`` -> ``datetime(x, '+1 month')``
    """
    out = sql
    while True:
        match = _DATE_ADD.search(out)
        if match is None:
            break
        sign = "-" if match.group(1).upper() == "SUB" else "+"
        open_paren = match.end() - 1
        depth = 0
        close_paren = -1
        for index in range(open_paren, len(out)):
            if out[index] == "(":
                depth += 1
            elif out[index] == ")":
                depth -= 1
                if depth == 0:
                    close_paren = index
                    break
        if close_paren < 0:
            break
        inner = out[open_paren + 1 : close_paren]
        interval = _INTERVAL.search(inner)
        if interval is None:
            # Not a form we handle; neutralise the marker so the loop ends.
            out = out[: match.start()] + "DATETIME_UNSUPPORTED(" + out[match.end() :]
            continue
        base = inner[: interval.start()].rstrip().rstrip(",").strip()
        amount, unit = interval.group(1), interval.group(2).lower()
        replacement = f"datetime({base}, '{sign}{amount} {unit}')"
        out = out[: match.start()] + replacement + out[close_paren + 1 :]
    return out


def connect(db_path: str, *, read_only: bool = True) -> sqlite3.Connection:
    """Open the mock store with MySQL-compatible functions registered."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if read_only else sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    register(conn)
    return conn


def register(conn: sqlite3.Connection) -> None:
    conn.create_function("DATEDIFF", 2, _datediff)
    conn.create_function("YEAR", 1, _year)
    conn.create_function("MONTH", 1, _month)
    conn.create_function("QUARTER", 1, _quarter)
    conn.create_function("DATE_FORMAT", 2, _date_format)
    conn.create_function("LEFT", 2, _left)
    conn.create_function("CHAR_LENGTH", 1, _char_length)
    conn.create_function("REGEXP", 2, _regexp)
    conn.create_function("FIND_IN_SET", 2, _find_in_set)
    conn.create_function("SUBSTRING_INDEX", 3, _substring_index)
    conn.create_function("JSON_UNQUOTE", 1, _json_unquote)
    conn.create_function("JSON_KEYS", 1, _json_keys)
    conn.create_function("JSON_EXTRACT", 2, _json_extract)
    conn.create_function("IFNULL", 2, lambda a, b: b if a is None else a)

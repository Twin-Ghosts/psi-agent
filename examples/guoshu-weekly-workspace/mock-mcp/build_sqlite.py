"""Convert the weekly_mock MySQL dump into a SQLite file for the demo.

The demo has no MySQL available, so the mock MCP service reads SQLite instead.
This changes only where the *demo* stores its own mock rows -- the MCP contract
the agent talks to is unchanged, so switching to the real 入口组 service is
still a URL change.

MySQL-specific DDL (ENGINE, COLLATE, COMMENT, KEY, AUTO_INCREMENT) is dropped;
column names and types are kept so the SQL shapes stay recognisable.
"""

# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DUMP = Path.home() / "Downloads" / "weekly_mock-full-20260817.sql"
DEFAULT_DB = HERE / "weekly_mock.sqlite3"

# MySQL column types -> SQLite affinities.
_TYPE_MAP = [
    (re.compile(r"^bigint|^int\b|^smallint|^tinyint|^mediumint", re.I), "INTEGER"),
    (re.compile(r"^decimal|^numeric|^float|^double", re.I), "REAL"),
    (re.compile(r"^datetime|^timestamp|^date\b|^time\b", re.I), "TEXT"),
    (re.compile(r"^varchar|^char|^text|^longtext|^mediumtext|^json|^enum", re.I), "TEXT"),
]

_SKIP_DDL = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE\s+KEY|KEY|INDEX|CONSTRAINT|FULLTEXT|FOREIGN\s+KEY)\b",
    re.I,
)


def _sqlite_type(mysql_type: str) -> str:
    for pattern, affinity in _TYPE_MAP:
        if pattern.match(mysql_type):
            return affinity
    return "TEXT"


def _split_columns(body: str) -> list[str]:
    """Split a CREATE TABLE body on top-level commas."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            current.append(char)
            if char == "\\" and index + 1 < len(body):
                current.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def convert_create(statement: str) -> tuple[str, str]:
    """Return (table_name, sqlite CREATE TABLE)."""
    match = re.match(r"CREATE TABLE\s+`([^`]+)`\s*\((.*)\)[^)]*$", statement, re.S)
    if match is None:
        raise ValueError(f"unparsed CREATE: {statement[:80]}")
    table = match.group(1)
    columns: list[str] = []
    primary: list[str] = []
    for part in _split_columns(match.group(2)):
        pk = re.match(r"PRIMARY\s+KEY\s*\(([^)]*)\)", part, re.I)
        if pk is not None:
            primary = [c.strip().strip("`") for c in pk.group(1).split(",")]
            continue
        if _SKIP_DDL.match(part):
            continue
        col = re.match(r"`([^`]+)`\s+(\S+)(.*)$", part, re.S)
        if col is None:
            continue
        name, raw_type, rest = col.group(1), col.group(2), col.group(3)
        affinity = _sqlite_type(raw_type)
        pieces = [f'"{name}"', affinity]
        if re.search(r"\bNOT NULL\b", rest, re.I):
            pieces.append("NOT NULL")
        default = re.search(r"\bDEFAULT\s+('(?:[^']|'')*'|[A-Za-z0-9_.()]+)", rest, re.I)
        if default is not None:
            value = default.group(1)
            if value.upper() not in {"CURRENT_TIMESTAMP", "NULL"}:
                pieces.append(f"DEFAULT {value}")
        columns.append(" ".join(pieces))
    if primary:
        quoted = ", ".join(f'"{c}"' for c in primary)
        columns.append(f"PRIMARY KEY ({quoted})")
    body = ",\n  ".join(columns)
    return table, f'CREATE TABLE "{table}" (\n  {body}\n)'


def _unescape(raw: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\\" and index + 1 < len(raw):
            nxt = raw[index + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nxt, nxt))
            index += 2
            continue
        if char == "'" and index + 1 < len(raw) and raw[index + 1] == "'":
            out.append("'")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_values(chunk: str) -> list[list[object]]:
    """Parse the VALUES (...),(...) payload of an INSERT."""
    rows: list[list[object]] = []
    current: list[object] = []
    token: list[str] = []
    in_string = False
    in_row = False
    index = 0
    while index < len(chunk):
        char = chunk[index]
        if in_string:
            if char == "\\" and index + 1 < len(chunk):
                token.append(char)
                token.append(chunk[index + 1])
                index += 2
                continue
            if char == "'":
                if index + 1 < len(chunk) and chunk[index + 1] == "'":
                    token.append("''")
                    index += 2
                    continue
                in_string = False
                current.append(_unescape("".join(token)))
                token = []
                index += 1
                continue
            token.append(char)
            index += 1
            continue
        if char == "(" and not in_row:
            in_row = True
            current = []
            token = []
        elif char == "'" and in_row:
            in_string = True
            token = []
        elif char in ",)" and in_row:
            literal = "".join(token).strip()
            token = []
            if literal:
                if literal.upper() == "NULL":
                    current.append(None)
                else:
                    try:
                        current.append(int(literal))
                    except ValueError:
                        try:
                            current.append(float(literal))
                        except ValueError:
                            current.append(literal)
            if char == ")":
                rows.append(current)
                current = []
                in_row = False
        elif in_row:
            token.append(char)
        index += 1
    return rows


def iter_statements(text: str):
    """Yield top-level SQL statements, respecting quotes."""
    buf: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        buf.append(char)
        if quote is not None:
            if char == "\\" and index + 1 < len(text):
                buf.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'`":
            quote = char
        elif char == ";":
            yield "".join(buf[:-1]).strip()
            buf = []
        index += 1
    tail = "".join(buf).strip()
    if tail:
        yield tail


def main(dump_path: Path, db_path: Path) -> int:
    if not dump_path.exists():
        print(f"dump not found: {dump_path}", file=sys.stderr)
        return 2
    text = dump_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*!.*?\*/;?", "", text, flags=re.S)

    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    created: list[str] = []
    counts: dict[str, int] = {}
    for statement in iter_statements(text):
        if not statement or statement.startswith("--"):
            continue
        upper = statement.upper()
        if upper.startswith("CREATE TABLE"):
            table, ddl = convert_create(statement)
            conn.execute(ddl)
            created.append(table)
            continue
        if upper.startswith("INSERT INTO"):
            head = re.match(
                r"INSERT INTO\s+`([^`]+)`\s*\(([^)]*)\)\s*VALUES\s*(.*)$",
                statement,
                re.S,
            )
            if head is None:
                continue
            table = head.group(1)
            cols = [c.strip().strip("`") for c in head.group(2).split(",")]
            rows = parse_values(head.group(3))
            placeholders = ", ".join("?" * len(cols))
            quoted = ", ".join(f'"{c}"' for c in cols)
            conn.executemany(
                f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
                rows,
            )
            counts[table] = counts.get(table, 0) + len(rows)
    conn.commit()

    print(f"tables created: {len(created)}")
    for table in sorted(created):
        actual = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table:<32} {actual:>6} rows")
    conn.close()
    print(f"\nwrote {db_path}")
    return 0


if __name__ == "__main__":
    dump = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DUMP
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB
    raise SystemExit(main(dump, out))

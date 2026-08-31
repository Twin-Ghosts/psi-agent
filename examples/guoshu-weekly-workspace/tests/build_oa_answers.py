"""Rebuild the oa_biz question set against the local mock store.

国数 gave us oa_biz_200.jsonl: 200 questions carrying `gold_sql` plus an
`expected` value.  The `expected` values cannot be used as our grading key --
they were computed against the real oa_biz database at snapshot 2026-08-18,
while our mock is anchored at 2026-08-15.  Measured, not assumed: of the 74
runnable scalar questions only 3 agree with `expected`, and the gaps are
systematic (B1-01 expects 81 where mock holds 128; owner 刘玮 does not exist in
mock at all, so his questions return 0).  Zero SQL errors, so this is a data
mismatch and not a syntax one.

So we do what the 396-question set already does: execute `gold_sql` against the
mock and treat the result set as that question's reference answer.  What this
measures is tool selection and retrieval accuracy on the new question shapes;
the numbers are mock numbers and do not transfer to the real database.

Two groups are dropped rather than shipped broken:

  * questions touching the 10 tables the mock does not have (key_works,
    key_tasks, oa_calendar_* and friends) -- blocked until 国数 sends the DDL.
  * questions whose mock result is empty or 0 while the question does not ask
    an existence question.  Those grade whether the model says "查不到", not
    whether it can retrieve, so they measure the mock's gaps instead of the
    agent.

Question ids are prefixed `OA-`: the new 200 and the old 396 share 108 ids with
entirely different question text, so an unprefixed merge would silently
overwrite half the set.

Usage:
    python tests/build_oa_answers.py                     # writes next to the source
    python tests/build_oa_answers.py --out other.jsonl
"""

# ruff: noqa: RUF001, RUF003  中文口径文案与注释里的全角标点是给人看的正文, 不能换成半角。
# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parent.parent

DEFAULT_SOURCE = Path.home() / "Downloads" / "oa_biz_200.jsonl"
DEFAULT_OUT = Path.home() / "Downloads" / "oa_biz-mock-answers.jsonl"

MOCK_TABLES = frozenset(
    {
        "task",
        "task_attachment",
        "task_board",
        "task_category",
        "task_group_detail",
        "task_group_progress_history",
        "task_milestone",
        "task_progress",
        "task_progress_import",
        "task_workflow_action",
        "task_workflow_submission",
        "task_year_goal",
    }
)
"""The 12 tables the mock dump actually contains."""

DIFFICULTY = {"简单": "easy", "中等": "medium", "困难": "hard"}

SNAPSHOT_NOTE = (
    "答案由 gold_sql 在本地 mock 库(锚 2026-08-15)执行得出；"
    "原 expected 按国数真实库 2026-08-18 快照，两者不可互换"
)

_TABLE_REF = re.compile(r"(?:FROM|JOIN)\s+`?(\w+)`?", re.IGNORECASE)
_NAMED_PARAM = re.compile(r":(\w+)")


def tables_used(sql: str) -> set[str]:
    return {name.lower() for name in _TABLE_REF.findall(sql or "")}


def _load_db() -> Any:
    """Import the workspace's own _db so we connect exactly as the tools do."""
    spec = importlib.util.spec_from_file_location("_oa_build_db", WORKSPACE / "mock-mcp" / "_db.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load mock-mcp/_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _classify(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if len(rows) == 1 and len(columns) == 1:
        return "scalar"
    return "row" if len(rows) == 1 else "table"


def _is_degenerate(rows: list[dict[str, Any]], columns: list[str], cells: list[list[str]]) -> bool:
    """True when mock simply has no data for this question.

    A single 0 counts as no data too: 「刘玮负责多少个重点任务？」 returns 0 because
    刘玮 is absent from mock, so grading it measures whether the model says
    「查不到」 rather than whether it can retrieve.  Questions that genuinely ask
    an existence question carry `expect_empty` and are kept.
    """
    if not rows:
        return True
    return len(rows) == 1 and len(columns) == 1 and cells[0][0] in {"0", "None", ""}


def build(source: Path, out: Path) -> int:
    db = _load_db()
    records = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]

    kept: list[dict[str, Any]] = []
    blocked: list[tuple[str, str]] = []
    degenerate: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []

    connection = db.connect()
    try:
        for record in records:
            missing = tables_used(record.get("gold_sql", "")) - MOCK_TABLES
            if missing:
                blocked.append((record["id"], "+".join(sorted(missing))))
                continue
            # gold_sql uses :name placeholders; pymysql wants %(name)s.
            bound = _NAMED_PARAM.sub(lambda m: f"%({m.group(1)})s", record["gold_sql"])
            try:
                cursor = connection.cursor()
                cursor.execute(bound, record.get("params") or {})
                rows = cursor.fetchall()
                cursor.close()
            except Exception as exc:
                errors.append((record["id"], f"{type(exc).__name__}: {exc}"[:150]))
                continue

            columns = list(rows[0].keys()) if rows else []
            cells = [["" if value is None else str(value) for value in row.values()] for row in rows]
            if _is_degenerate(rows, columns, cells) and not record.get("expect_empty"):
                degenerate.append((record["id"], record["question"][:40]))
                continue

            kept.append(
                {
                    "id": "OA-" + record["id"],
                    "type_id": record["type_id"],
                    "category": record["category"] + "（oa_biz）",
                    "type": record["type"],
                    "difficulty": DIFFICULTY.get(record["difficulty"], "medium"),
                    "question": record["question"],
                    "kind": _classify(rows, columns),
                    "gold_sql": record["gold_sql"],
                    "gold_answer": {"columns": columns, "rows": cells},
                    "gold_row_count": len(rows),
                    "traps": record.get("traps", []),
                    "oa_expected": record.get("expected"),
                    "snapshot_note": SNAPSHOT_NOTE,
                }
            )
    finally:
        connection.close()

    out.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in kept) + "\n",
        encoding="utf-8",
    )
    _report(kept, blocked, degenerate, errors, source, out)
    return 0 if kept and not errors else 1


def _report(
    kept: list[dict[str, Any]],
    blocked: list[tuple[str, str]],
    degenerate: list[tuple[str, str]],
    errors: list[tuple[str, str]],
    source: Path,
    out: Path,
) -> None:
    print(f"源题库 {source}")
    print(f"入库 {len(kept)} 题 -> {out}")
    print(f"因缺 10 张新表跳过 {len(blocked)} 题；mock 无数据剔除 {len(degenerate)} 题；SQL 报错 {len(errors)}")

    for label, key in (("类别", "category"), ("难度", "difficulty"), ("形态", "kind")):
        counts = collections.Counter(str(item[key]) for item in kept)
        print(f"\n按{label}：")
        for name, count in sorted(counts.items()):
            print(f"  {name:<18} {count}")

    if blocked:
        print("\n卡在缺表的题（按缺哪张表）：")
        for qid, missing in blocked:
            print(f"  {qid:<10} {missing}")
    if degenerate:
        print("\nmock 无数据被剔除的题：")
        for qid, question in degenerate:
            print(f"  {qid:<10} {question}")
    if errors:
        print("\nSQL 报错（需要人看）：")
        for qid, message in errors:
            print(f"  {qid:<10} {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description="用 mock 库重建 oa_biz 题目的参考答案")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="国数给的 oa_biz_200.jsonl")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出的 mock 版答案集")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"源题库不存在：{source}")
        return 2
    return build(source, Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())

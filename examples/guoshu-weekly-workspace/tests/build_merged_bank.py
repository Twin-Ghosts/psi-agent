"""Merge the three question banks into one, with provenance on every question.

The three banks disagree about what a "reference answer" even is, so merging them
without recording that would produce a file that looks uniform and is not:

  nl2sql-396   gold_sql + gold_answer, answers computed on the mock (anchor 2026-08-15)
  oa_biz-200   gold_sql + `expected` computed on 国数's REAL database (anchor 2026-08-18).
               Measured: of the 74 runnable scalar questions only 3 agree with the mock,
               and the gaps are systematic (B1-01 expects 81 where the mock holds 128;
               owner 刘玮 does not exist in the mock at all, so his questions return 0).
               So we grade against mock-recomputed answers and keep `oa_expected`
               alongside for the day the real database is reachable.
  g93          NO gold_sql at all -- prose, signal and refusal answers written by hand.
               Verified separately against the mock; see verification_status.

Every merged record therefore carries `source_bank`, `answer_origin` and
`verification` so a reader can tell how much any given answer is worth.

Usage:
    python tests/build_merged_bank.py
    python tests/build_merged_bank.py --out C:/tmp/merged.jsonl
"""

# ruff: noqa: RUF001, RUF003  中文口径文案里的全角标点是给人看的正文, 不能换成半角。
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
HOME = Path.home()

SRC_396 = HOME / "Downloads" / "nl2sql-answers.jsonl"
SRC_OA_RAW = HOME / "Downloads" / "oa_biz_200.jsonl"
SRC_OA_MOCK = HOME / "Downloads" / "oa_biz-mock-answers.jsonl"
SRC_G93 = WORKSPACE / "tests" / "g93-answers.jsonl"
DEFAULT_OUT = WORKSPACE / "tests" / "merged-bank.jsonl"

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

MOCK_AS_OF = "2026-08-15"
_TABLE_REF = re.compile(r"(?:FROM|JOIN)\s+`?(\w+)`?", re.IGNORECASE)

# 审计确认的参考答案缺陷。每条都经人工抽验或对抗验证核实过，附实测数字。
# 合并时按这里的值修正，并在记录上留 `defect_fixed` 说明改了什么、原值是什么——
# 不留痕的话，日后没人分得清这是原始答案还是我们改过的。
CONFIRMED_FIXES: dict[str, dict[str, Any]] = {
    "E6-04": {
        "drop_limit": True,
        "gold_row_count": 73,
        "note": (
            "gold 用 LIMIT 8 把完整集合截成任意前 8 行（ORDER BY t.id 不是排序维度）；"
            "实测去掉 LIMIT 得 73 行，丢 65 个任务"
        ),
    },
    "I8-04": {
        "drop_limit": True,
        "gold_row_count": 16,
        "note": (
            "同为 LIMIT 8 截断，实测真值 16 行。实现侧反证：server.py 的 status_mismatch "
            "分支无 cap、smoke_test 断言 row_count == 16，此题在 7 次基线全部判错——是 gold 错、代码对"
        ),
    },
    "M3-01": {
        "note": (
            "SUM(s.status='pending') 恒为 0：'pending' 不在 task_workflow_submission.status "
            "值域内（published / pending_audit / pending_leader / rejected / signing / "
            "pending_fill / cancelled）；应改用真实的在途状态枚举"
        ),
    },
    "M3-03": {
        "note": (
            "status <> 'approved' 筛不掉任何行：'approved' 不在该列值域内，"
            "只作为 task_workflow_action.action 存在（兄弟表词汇串味）"
        ),
    },
    "R3-05": {
        "note": "同 M3-03，空过滤谓词",
    },
}

# 并列规则自相矛盾的题：同一问法在兄弟题上用了相反的 tie 规则，问句本身分不出该用哪套。
# 这类不能单方面「修正」——改任一侧都会打翻另一侧，只能标注待出题方裁决。
TIE_DISPUTED: dict[str, str] = {
    "V1-04": (
        "gold 用 LIMIT 1 硬切，兄弟题 F1-01 / L2-02 同问「谁任务最多」却用 HAVING = MAX 保并列；"
        "实测技术组顶端 4 人并列（各 4 条），两套规则给出不同答案"
    ),
    "F1-01": "与 V1-04 互为矛盾侧，见其说明",
    "L2-02": "与 V1-04 互为矛盾侧；问句带「那个」比 V1-04 更像单数，却保并列",
    "B3-02": "gold 保 9 个并列，兄弟题 V2-03 同问「哪个小类挂的任务最多」只取 1 个",
    "V2-03": "与 B3-02 互为矛盾侧；mock 上顶端 9 路并列",
}

# 覆盖率超 100% 这类算术不可能的答案。
IMPOSSIBLE: dict[str, str] = {
    "H5-01": (
        "gold 覆盖率 125/82 = 152.4%，超过 100%：分子数全库有里程碑的任务而分母只数技术组 82，"
        "两者不同源。正确口径是 80/82 = 97.6%"
    ),
}


def tables_used(sql: str) -> set[str]:
    return {name.lower() for name in _TABLE_REF.findall(sql or "")}


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_db() -> Any:
    """Import the workspace's own _db so we connect exactly as the tools do."""
    spec = importlib.util.spec_from_file_location("_merge_db", WORKSPACE / "mock-mcp" / "_db.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load mock-mcp/_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recompute(connection: Any, sql: str) -> dict[str, Any] | None:
    """Run a de-truncated gold_sql and return the answer in the bank's own shape."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    except Exception as exc:  # 跑不通就不改答案，宁可留原值加标记
        print(f"  [warn] 重算失败，保留原答案：{type(exc).__name__}: {exc}"[:160])
        return None
    columns = list(rows[0].keys()) if rows else []
    return {
        "columns": columns,
        "rows": [["" if value is None else str(value) for value in row.values()] for row in rows],
    }


def _apply_fixes(
    qid: str,
    record: dict[str, Any],
    connection: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply a confirmed fix, returning the record plus an audit trail entry."""
    fix = CONFIRMED_FIXES.get(qid)
    if fix is None:
        return record, None
    trail: dict[str, Any] = {"reason": fix["note"]}
    if fix.get("drop_limit"):
        for key in ("gold_sql", "gold_sql_bound"):
            original = record.get(key)
            if not original:
                continue
            stripped = re.sub(r"\s+LIMIT\s+\d+\s*$", "", str(original).strip(), flags=re.IGNORECASE)
            if stripped != str(original).strip():
                trail.setdefault("sql_changed", []).append(key)
                record[key] = stripped
        trail["original_gold_row_count"] = record.get("gold_row_count")
        record["gold_row_count"] = fix["gold_row_count"]
        # 去掉 LIMIT 后原 gold_answer 只剩被截断的那几行，必须重算，否则行数与内容
        # 自相矛盾——这比原来的截断更坏，因为看起来像是修好了。
        recomputed = None
        if connection is not None:
            recomputed = _recompute(connection, str(record.get("gold_sql_bound") or record.get("gold_sql")))
        if recomputed is None:
            trail["gold_answer_stale"] = True
            record["gold_answer_needs_recompute"] = True
        else:
            trail["original_gold_answer_rows"] = len((record.get("gold_answer") or {}).get("rows") or [])
            record["gold_answer"] = recomputed
            trail["gold_answer_recomputed"] = len(recomputed["rows"])
            # 实测行数必须等于审计确认的值，不等就是哪里错了，宁可报出来。
            if len(recomputed["rows"]) != fix["gold_row_count"]:
                trail["row_count_mismatch"] = (
                    f"重算得 {len(recomputed['rows'])} 行，审计确认应为 {fix['gold_row_count']} 行——请复核"
                )
    return record, trail


def _tag(record: dict[str, Any], qid: str) -> None:
    """Attach dispute / impossibility markers so nobody grades against them blindly."""
    if qid in TIE_DISPUTED:
        record["tie_rule_disputed"] = TIE_DISPUTED[qid]
        record["grade_with_care"] = "并列规则与兄弟题冲突，评分前需出题方裁决取 hard_cut 还是 keep_ties"
    if qid in IMPOSSIBLE:
        record["arithmetically_impossible"] = IMPOSSIBLE[qid]
        record["grade_with_care"] = "参考答案本身算术不成立，不要据它判分"


def build_396(records: list[dict[str, Any]], connection: Any | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for src in records:
        qid = str(src.get("id"))
        record = dict(src)
        record, trail = _apply_fixes(qid, record, connection)
        merged = {
            "id": f"W-{qid}",
            "origin_id": qid,
            "source_bank": "nl2sql-396",
            "answer_origin": "mock_executed",
            "as_of": MOCK_AS_OF,
            "grade_mode": "exact_value",
            **{k: v for k, v in record.items() if k != "id"},
        }
        if trail:
            merged["defect_fixed"] = trail
        _tag(merged, qid)
        out.append(merged)
    return out


def build_oa(
    raw: list[dict[str, Any]],
    mock: list[dict[str, Any]],
    connection: Any | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """oa_biz: grade against mock-recomputed answers, keep `expected` for later reconciliation."""
    by_id = {str(r.get("id")): r for r in raw}
    mock_by_id = {str(r.get("id")).replace("OA-", ""): r for r in mock}
    out: list[dict[str, Any]] = []
    dropped: list[tuple[str, str]] = []

    for qid, src in by_id.items():
        missing = tables_used(src.get("gold_sql", "")) - MOCK_TABLES
        if missing:
            dropped.append((qid, "缺表：" + "+".join(sorted(missing))))
            continue
        m = mock_by_id.get(qid)
        if m is None:
            dropped.append((qid, "mock 上无数据（结果为空或 0），构建期已剔除"))
            continue
        record = dict(m)
        record, trail = _apply_fixes(qid, record, connection)
        merged = {
            "id": f"OA-{qid}",
            "origin_id": qid,
            "source_bank": "oa_biz-200",
            "answer_origin": "mock_recomputed",
            "as_of": MOCK_AS_OF,
            "grade_mode": "exact_value",
            # 保留真实库那份，供拿到访问权后一键对账。
            "oa_expected": src.get("expected"),
            "oa_expected_as_of": src.get("as_of"),
            "reconcile_note": (
                "oa_expected 按国数真实库快照算出，与本记录的 gold_answer 不可互换："
                "实测 74 道可执行标量题里仅 3 道两者一致，差异系统性（如 B1-01 真实库 81 / mock 128）"
            ),
            **{k: v for k, v in record.items() if k not in {"id", "oa_expected", "snapshot_note"}},
        }
        if trail:
            merged["defect_fixed"] = trail
        _tag(merged, qid)
        out.append(merged)
    return out, dropped


# g93 的评分方式与另两库根本不同：那两库按值精确比对，这里 60 道是散文。
# 按 grade_mode 分开标注，免得有人拿 exact_value 的判分器去套散文答案。
_G93_GRADE = {
    "fact": "exact_value_in_prose",
    "signal": "assertion_plus_figures",
    "refusal": "refusal_justified",
}

# 核验跑不收敛的题：宁可剔除，也不要留一条没核过的答案冒充已核。
G93_DROPPED: dict[str, str] = {
    "G09": "逐题核验未收敛（跨库取证越查越宽，两次均未出结论），构建期剔除",
}


def build_g93(
    records: list[dict[str, Any]], verification: dict[str, str]
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    out: list[dict[str, Any]] = []
    dropped: list[tuple[str, str]] = []
    for src in records:
        qid = str(src.get("id"))
        if qid in G93_DROPPED:
            dropped.append((qid, G93_DROPPED[qid]))
            continue
        mode = str(src.get("grade_mode") or "fact")
        merged = {
            "id": f"G-{qid}",
            "origin_id": qid,
            "source_bank": "g93",
            # 这一库没有 gold_sql，答案是人写的；verification 才是它的可信度依据。
            "answer_origin": "hand_written",
            "as_of": MOCK_AS_OF,
            "grade_mode": _G93_GRADE.get(mode, "exact_value_in_prose"),
            "g93_grade_mode": mode,
            "verification": verification.get(qid, "unchecked"),
            **{k: v for k, v in src.items() if k not in {"id", "grade_mode"}},
        }
        if merged["verification"] in {"defective", "partly_defective"}:
            merged["grade_with_care"] = "本题参考答案经核验存在错误数字，修正前不要据它判分"
        elif merged["verification"] == "unverifiable":
            merged["grade_with_care"] = "mock 无法判定本题，判分需人工复核"
        elif merged["verification"] == "unchecked":
            # 「没核过」不等于「核过没问题」：这一库无 gold_sql，未核题一律不能当已核用。
            merged["grade_with_care"] = "本题尚未逐条核验，判分前需先核对参考答案里的每个数字"
        out.append(merged)
    return out, dropped


def _load_verification(path: Path) -> dict[str, str]:
    """Read the g93 verification ledger if it exists; absent means unchecked."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="把三个题库合并成一个，并在每条上留可信度出处")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--g93-verification",
        # 台账跟脚本一起进仓：不然换台机器合并出来的 g93 全是 unchecked，可信度无从复现。
        default=str(WORKSPACE / "tests" / "g93-verification.json"),
        help="g93 逐题核验结论（id -> verified/defective/partly_defective/unverifiable）",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="不连库：去掉 LIMIT 的两题只改行数，gold_answer 留标记待重算",
    )
    args = parser.parse_args()

    connection = None
    if not args.no_db:
        try:
            connection = _load_db().connect()
        except Exception as exc:  # 连不上库不该让合并失败，降级成加标记
            print(f"[warn] 连不上 mock 库，去 LIMIT 的题只改行数：{type(exc).__name__}: {exc}"[:160])

    verification = _load_verification(Path(args.g93_verification))
    merged: list[dict[str, Any]] = []
    try:
        merged += build_396(_read(SRC_396), connection)
        oa, dropped = build_oa(_read(SRC_OA_RAW), _read(SRC_OA_MOCK), connection)
    finally:
        if connection is not None:
            connection.close()
    merged += oa
    g93, g93_dropped = build_g93(_read(SRC_G93), verification)
    merged += g93

    out_path = Path(args.out)
    out_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in merged) + "\n",
        encoding="utf-8",
    )

    print(f"合并 {len(merged)} 题 -> {out_path}")
    print()
    for key in ("source_bank", "answer_origin", "grade_mode"):
        counts = collections.Counter(str(item.get(key)) for item in merged)
        print(f"按 {key}：")
        for name, count in counts.most_common():
            print(f"  {name:<24} {count}")
        print()

    fixed = [item for item in merged if item.get("defect_fixed")]
    care = [item for item in merged if item.get("grade_with_care")]
    recompute = [item for item in merged if item.get("gold_answer_needs_recompute")]
    print(f"已按审计修正 {len(fixed)} 题：{', '.join(i['id'] for i in fixed)}")
    print(f"标注需谨慎判分 {len(care)} 题：{', '.join(i['id'] for i in care)}")
    if recompute:
        print(f"\n注意：{len(recompute)} 题去掉 LIMIT 后 gold_answer 已过期，需重算行内容：")
        for item in recompute:
            print(f"  {item['id']:<10} 应为 {item.get('gold_row_count')} 行")
    if not verification:
        print("\n注意：未找到 g93 核验结论，全部标为 unchecked。")
    if g93_dropped:
        print(f"\ng93 剔除 {len(g93_dropped)} 题：")
        for qid, why in g93_dropped:
            print(f"  {qid:<8} {why}")
    still_unchecked = [i["id"] for i in merged if i.get("verification") == "unchecked"]
    if still_unchecked:
        print(f"\n注意：g93 仍有 {len(still_unchecked)} 题未核验：{', '.join(still_unchecked)}")
    print(f"\noa_biz 剔除 {len(dropped)} 题：")
    for qid, why in dropped:
        print(f"  {qid:<8} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

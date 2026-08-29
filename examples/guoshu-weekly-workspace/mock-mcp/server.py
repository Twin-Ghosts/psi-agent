"""Mock weekly-report MCP service for the demo.

This stands in for the 入口组 service until it exists.  It speaks the *same*
semantic tool contract described in chapter 三 of the plan, so switching to the
real service is a `GUOSHU_WEEKLY_MCP_URL` change with no agent-side edits.

Run:
    python server.py --port 18900
"""

# ruff: noqa: RUF001, RUF003  中文口径文案里的全角标点是给模型看的字面量, 不能换成半角。
# ruff: noqa: T201  这是命令行脚本, stdout 就是它的输出通道。
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _store as store
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("guoshu-weekly-mock")

BEARER_TOKEN = os.environ.get("GUOSHU_WEEKLY_MOCK_TOKEN", "demo-token")
SENSITIVE_TOKEN = os.environ.get("GUOSHU_WEEKLY_MOCK_ADMIN_TOKEN", "demo-admin-token")


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(code: str, message: str) -> str:
    return _dump({"ok": False, "error": {"code": code, "message": message}})


def _guard(func_name: str, work) -> str:
    try:
        return _dump(work())
    except store.QueryError as exc:
        return _error(exc.code, str(exc))
    except Exception as exc:
        return _error("internal_error", f"{func_name}: {type(exc).__name__}")


@mcp.tool()
def weekly_schema(board: str = "") -> str:
    """List boards, category trees and the field dictionary with caliber notes.

    Args:
        board: Optional board code (tech/group) or name to scope the category tree.
    """

    def work() -> dict[str, Any]:
        boards = store.fetch(
            "SELECT id, name, code, sort_order FROM task_board WHERE is_deleted = 0 ORDER BY sort_order, id",
            caliber="is_deleted = 0",
        )
        params: dict[str, Any] = {}
        where = "c.is_deleted = 0"
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            where += " AND c.board_id = :bid"
            params["bid"] = board_id
        categories = store.fetch(
            "SELECT c.id, c.board_id, c.parent_id, c.name, c.sort_order "
            f"FROM task_category c WHERE {where} ORDER BY c.board_id, c.parent_id, c.sort_order",
            params,
            caliber="is_deleted = 0；parent_id 为空是一级分类",
        )
        return {
            "ok": True,
            "boards": boards["rows"],
            "categories": categories["rows"],
            "field_notes": {
                "formal_task": store.FORMAL_TASK_CALIBER,
                "status": "0未开始 / 1进行中 / 2已完成 / 3已停用",
                "completion_time": "展示文本，不可做日期运算（R-12）",
                "owner_multi_value": "分管领导等为多值分隔文本，须去空格后匹配（R-13）",
                "blocked_fields": sorted(store.BLOCKED_FIELDS),
                "sensitive_fields": sorted(store.SENSITIVE_FIELDS),
            },
            "snapshot_note": "演示数据（weekly_mock 自建库），非集团真实周报",
        }

    return _guard("weekly_schema", work)


@mcp.tool()
def weekly_task_query(
    board: str = "",
    category: str = "",
    status: str = "",
    owner: str = "",
    keyword: str = "",
    limit: int = 50,
) -> str:
    """Query formal tasks. Applies R-01 (is_deleted=0 AND published) server-side.

    Args:
        board: Board code (tech/group) or name.
        category: Category name, matched loosely.
        status: Business status 0/1/2/3, empty for all.
        owner: Owner or lead name; multi-value columns are matched per R-13.
        keyword: Substring of the task name.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        where = [store.formal_task_clause()]
        params: dict[str, Any] = {}
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            where.append("t.board_id = :bid")
            params["bid"] = board_id
        if category.strip():
            where.append("t.category_id IN (SELECT id FROM task_category WHERE is_deleted = 0 AND name LIKE :cat)")
            params["cat"] = f"%{category.strip()}%"
        if status.strip():
            if status.strip() not in {"0", "1", "2", "3"}:
                return {
                    "ok": False,
                    "error": {"code": "invalid_status", "message": "status 只能是 0/1/2/3"},
                }
            where.append("t.status = :st")
            params["st"] = int(status.strip())
        if owner.strip():
            # R-13: strip spaces on both sides before matching multi-value text.
            token = owner.strip()
            where.append(
                "(REPLACE(IFNULL(t.project_owner_name,''),' ','') LIKE :own "
                "OR REPLACE(IFNULL(t.lead_owner_name,''),' ','') LIKE :own "
                "OR REPLACE(IFNULL(t.owner_user_id,''),' ','') LIKE :own)"
            )
            params["own"] = f"%{token.replace(' ', '')}%"
        if keyword.strip():
            where.append("t.task_name LIKE :kw")
            params["kw"] = f"%{keyword.strip()}%"

        clause = " AND ".join(where)
        total = store.scalar(
            f"SELECT COUNT(*) FROM task t WHERE {clause}",
            params,
            caliber=store.FORMAL_TASK_CALIBER,
        )
        rows = store.fetch(
            "SELECT t.id, t.task_no, t.task_name, t.board_id, t.category_id, t.status, "
            "t.project_owner_name, t.lead_owner_name, t.project_group, "
            "t.latest_progress_time, t.published_at "
            f"FROM task t WHERE {clause} ORDER BY t.board_id, t.sort_order, t.id",
            params,
            caliber=store.FORMAL_TASK_CALIBER,
            limit=limit,
        )
        rows["total_count"] = total["value"]
        return rows

    return _guard("weekly_task_query", work)


@mcp.tool()
def weekly_task_detail(task: str) -> str:
    """Fetch one formal task with its group detail, latest progress and year goal.

    Args:
        task: Task id or task name (fuzzy).
    """

    def work() -> dict[str, Any]:
        found = store.resolve_task(task)
        if found is None:
            return {
                "ok": False,
                "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
            }
        task_id = int(found["id"])
        detail = store.fetch(
            "SELECT * FROM task_group_detail WHERE task_id = :tid",
            {"tid": task_id},
            caliber="completion_time 为展示文本，不可做日期运算（R-12）",
            limit=1,
        )
        progress = store.fetch(
            "SELECT id, task_id, version_no, latest_progress, next_work, progress_date, "
            "report_time, is_published, review_comment "
            "FROM task_progress WHERE task_id = :tid AND is_published = 1 "
            "ORDER BY version_no DESC, id DESC",
            {"tid": task_id},
            caliber="is_published = 1（仅正式发布进展）；review_comment 按权限展示",
            limit=3,
        )
        goal = store.fetch(
            "SELECT * FROM task_year_goal WHERE task_id = :tid ORDER BY year DESC",
            {"tid": task_id},
            caliber="task_id + year 唯一",
            limit=5,
        )
        return {
            "ok": True,
            "task": {k: v for k, v in found.items() if k not in store.BLOCKED_FIELDS},
            "group_detail": detail["rows"],
            "recent_progress": progress["rows"],
            "year_goals": goal["rows"],
            # R-12 must be stated unconditionally: task_group_detail only covers
            # the group board, so hanging this note off that sub-query's caliber
            # silently dropped it for every tech-board task -- exactly the case
            # the rule exists to guard.
            "caliber": (
                f"{store.FORMAL_TASK_CALIBER}；"
                "completion_time 为展示文本，不可做日期运算（R-12）；"
                "review_comment 按权限展示（R-04/R-14）"
            ),
            "snapshot_note": "演示数据（weekly_mock 自建库），非集团真实周报",
        }

    return _guard("weekly_task_detail", work)


@mcp.tool()
def weekly_progress_history(task: str, published_only: bool = True, limit: int = 30) -> str:
    """Return progress versions for one task, newest first (version_no desc).

    Args:
        task: Task id or name.
        published_only: True keeps only is_published=1 rows (formal progress).
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        found = store.resolve_task(task)
        if found is None:
            return {
                "ok": False,
                "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
            }
        where = "task_id = :tid"
        caliber = "按 version_no 倒序，越大越新"
        if published_only:
            where += " AND is_published = 1"
            caliber += "；is_published = 1"
        return store.fetch(
            "SELECT id, task_id, version_no, latest_progress, next_work, progress_date, "
            "report_time, is_published, review_comment "
            f"FROM task_progress WHERE {where} ORDER BY version_no DESC, id DESC",
            {"tid": int(found["id"])},
            caliber=caliber,
            limit=limit,
        )

    return _guard("weekly_progress_history", work)


@mcp.tool()
def weekly_aggregate(group_by: str, board: str = "", metric: str = "count") -> str:
    """Aggregate formal tasks. Uses LEFT JOIN so empty groups still appear (R-02/R-08).

    Args:
        group_by: One of board / category / status / project_group / owner.
        board: Optional board code or name to scope the aggregation.
        metric: Only "count" is supported in the demo.
    """

    def work() -> dict[str, Any]:
        if metric != "count":
            return {
                "ok": False,
                "error": {"code": "unsupported_metric", "message": "演示版仅支持 metric=count"},
            }
        params: dict[str, Any] = {}
        # R-02: the caliber goes on the ON clause so zero-task groups survive.
        scope = store.formal_task_clause()
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            scope += " AND t.board_id = :bid"
            params["bid"] = board_id

        if group_by == "board":
            sql = (
                "SELECT b.name AS group_name, COUNT(t.id) AS cnt FROM task_board b "
                f"LEFT JOIN task t ON t.board_id = b.id AND {scope} "
                "WHERE b.is_deleted = 0 GROUP BY b.id, b.name ORDER BY b.sort_order"
            )
        elif group_by == "category":
            sql = (
                "SELECT c.name AS group_name, COUNT(t.id) AS cnt FROM task_category c "
                f"LEFT JOIN task t ON t.category_id = c.id AND {scope} "
                "WHERE c.is_deleted = 0 GROUP BY c.id, c.name ORDER BY cnt DESC, c.id"
            )
        elif group_by == "status":
            sql = (
                "SELECT CASE t.status WHEN 0 THEN '未开始' WHEN 1 THEN '进行中' "
                "WHEN 2 THEN '已完成' WHEN 3 THEN '已停用' ELSE '未知' END AS group_name, "
                f"COUNT(*) AS cnt FROM task t WHERE {scope} GROUP BY t.status ORDER BY t.status"
            )
        elif group_by == "project_group":
            sql = (
                "SELECT IFNULL(NULLIF(TRIM(t.project_group),''),'(未填)') AS group_name, "
                f"COUNT(*) AS cnt FROM task t WHERE {scope} GROUP BY group_name ORDER BY cnt DESC"
            )
        elif group_by == "owner":
            # R-11: 分管领导栏存在多种填法，先按填法枚举再计数，不做归一化猜测。
            sql = (
                "SELECT IFNULL(NULLIF(TRIM(t.lead_owner_name),''),'(未填)') AS group_name, "
                f"COUNT(*) AS cnt FROM task t WHERE {scope} GROUP BY group_name ORDER BY cnt DESC"
            )
        else:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_group_by",
                    "message": "group_by 支持 board / category / status / project_group / owner",
                },
            }
        result = store.fetch(
            sql,
            params,
            caliber=f"{store.FORMAL_TASK_CALIBER}；LEFT JOIN 保留空分组（R-02/R-08）",
        )
        result["group_by"] = group_by
        return result

    return _guard("weekly_aggregate", work)


@mcp.tool()
def weekly_milestone_query(year: str = "", status: str = "", limit: int = 50) -> str:
    """List milestones, joined back to task to re-check the formal-task caliber (R-17).

    Args:
        year: Four-digit year, empty for all.
        status: 0 未完成 / 1 已完成, empty for all.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        where = ["m.is_deleted = 0", store.formal_task_clause()]
        params: dict[str, Any] = {}
        if year.strip():
            if not year.strip().isdigit():
                return {"ok": False, "error": {"code": "invalid_year", "message": "year 须为数字"}}
            where.append("m.year = :yr")
            params["yr"] = int(year.strip())
        if status.strip():
            if status.strip() not in {"0", "1"}:
                return {"ok": False, "error": {"code": "invalid_status", "message": "status 只能是 0/1"}}
            where.append("m.status = :st")
            params["st"] = int(status.strip())
        return store.fetch(
            "SELECT m.id, m.task_id, t.task_name, m.year, m.category, m.group_name, "
            "m.content, m.status "
            "FROM task_milestone m JOIN task t ON t.id = m.task_id "
            f"WHERE {' AND '.join(where)} ORDER BY m.year DESC, m.id",
            params,
            caliber=f"m.is_deleted = 0 且关联任务满足 {store.FORMAL_TASK_CALIBER}（R-17）",
            limit=limit,
        )

    return _guard("weekly_milestone_query", work)


@mcp.tool()
def weekly_workflow_query(task: str = "", limit: int = 50) -> str:
    """Trace approval submissions and actions. Opinions are permission-gated (R-04/R-14).

    Args:
        task: Task id or name; empty returns the most recent actions overall.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["1 = 1"]
        if task.strip():
            found = store.resolve_task(task)
            if found is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("a.task_id = :tid")
            params["tid"] = int(found["id"])
        return store.fetch(
            "SELECT a.id, a.submission_id, a.task_id, s.round_no, a.node_type, a.action, "
            "a.operator_name, a.opinion, a.created_at "
            "FROM task_workflow_action a "
            "LEFT JOIN task_workflow_submission s ON s.id = a.submission_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY a.task_id, s.round_no, a.id",
            params,
            caliber="opinion 属敏感字段，按权限展示（R-04/R-14）；payload 草稿快照不返回",
            limit=limit,
        )

    return _guard("weekly_workflow_query", work)


@mcp.tool()
def weekly_attachment_query(task: str = "", limit: int = 50) -> str:
    """List attachments without ever returning storage_path (chapter 7.2).

    Args:
        task: Task id or name; empty lists across all formal tasks.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["att.is_deleted = 0"]
        if task.strip():
            found = store.resolve_task(task)
            if found is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("att.task_id = :tid")
            params["tid"] = int(found["id"])
        return store.fetch(
            "SELECT att.id, att.task_id, att.progress_id, att.workflow_submission_id, "
            "att.file_name, att.file_size, att.uploader_id, att.upload_time "
            f"FROM task_attachment att WHERE {' AND '.join(where)} ORDER BY att.id",
            params,
            caliber="is_deleted = 0；storage_path 禁止外泄，不在返回字段内",
            limit=limit,
        )

    return _guard("weekly_attachment_query", work)


@mcp.tool()
def weekly_import_audit(limit: int = 30) -> str:
    """Reconcile Excel import batches: batch count vs distinct import times (R-09/R-10).

    Args:
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        summary = store.fetch(
            "SELECT COUNT(*) AS batch_count, COUNT(DISTINCT data_date) AS distinct_dates, "
            "COUNT(DISTINCT import_time) AS distinct_import_times "
            "FROM task_progress_import",
            caliber="批次数 vs 去重业务快照日期数 vs 去重导入时间数（R-09/R-10）",
            limit=1,
        )
        rows = store.fetch(
            "SELECT id, file_name, data_date, import_time, total_tasks, changed_tasks, status "
            "FROM task_progress_import ORDER BY data_date DESC, id DESC",
            caliber="data_date 为业务快照日期",
            limit=limit,
        )
        rows["reconciliation"] = summary["rows"][0] if summary["rows"] else {}
        return rows

    return _guard("weekly_import_audit", work)


@mcp.tool()
def weekly_freshness() -> str:
    """Report data snapshot dates so the agent anchors relative time to data, not wall clock."""

    def work() -> dict[str, Any]:
        return store.fetch(
            "SELECT b.name AS board_name, MAX(t.latest_progress_time) AS latest_progress, "
            "COUNT(t.id) AS formal_task_count "
            "FROM task_board b "
            f"LEFT JOIN task t ON t.board_id = b.id AND {store.formal_task_clause()} "
            "WHERE b.is_deleted = 0 GROUP BY b.id, b.name ORDER BY b.sort_order",
            caliber=f"{store.FORMAL_TASK_CALIBER}；相对时间须以此快照锚定",
        )

    return _guard("weekly_freshness", work)


@mcp.tool()
def weekly_health() -> str:
    """Verify the mock store is reachable and report its table row counts."""

    def work() -> dict[str, Any]:
        conn = store.connect()
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        finally:
            conn.close()
        return {
            "ok": True,
            "store": store.DB_PATH,
            "table_count": len(counts),
            "row_counts": counts,
            "caliber": store.FORMAL_TASK_CALIBER,
            "snapshot_note": "演示数据（weekly_mock 自建库），非集团真实周报",
        }

    return _guard("weekly_health", work)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock weekly-report MCP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18900)
    args = parser.parse_args()

    if not Path(store.DB_PATH).exists():
        print(f"mock store missing: {store.DB_PATH}", file=sys.stderr)
        print("run: python build_sqlite.py", file=sys.stderr)
        return 2

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"mock weekly MCP on http://{args.host}:{args.port}/mcp", flush=True)
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

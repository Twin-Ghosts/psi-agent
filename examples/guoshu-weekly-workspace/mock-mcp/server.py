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

import _db
import _store as store
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("guoshu-weekly-mock")

BEARER_TOKEN = os.environ.get("GUOSHU_WEEKLY_MOCK_TOKEN", "demo-token")
SENSITIVE_TOKEN = os.environ.get("GUOSHU_WEEKLY_MOCK_ADMIN_TOKEN", "demo-admin-token")


def _caller_may_read_sensitive(ctx: Context | None) -> bool:
    """Decide sensitive-field access from the caller's bearer token.

    R-04/R-14 say approval opinions are returned *by permission* -- blanket
    redaction fails the requirement just as surely as blanket exposure does, and
    it also makes the capability untestable.  The decision is taken here, from
    the transport's Authorization header, because that is the one input the model
    cannot influence: nothing a user or a prompt says can widen this.

    In production the header maps to an OA identity and a row-level policy; the
    demo has two fixed tokens so the two branches are both exercisable.
    """
    if ctx is None:
        return False
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return False
    headers = getattr(request, "headers", None) or {}
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    token = raw.removeprefix("Bearer").removeprefix("bearer").strip()
    return bool(token) and token == SENSITIVE_TOKEN


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
            where += " AND c.board_id = %(bid)s"
            params["bid"] = board_id
        categories = store.fetch(
            "SELECT c.id, c.board_id, c.parent_id, c.name, c.sort_order "
            f"FROM task_category c WHERE {where} ORDER BY c.board_id, c.parent_id, c.sort_order",
            params,
            caliber="is_deleted = 0；parent_id 为空是一级分类",
        )
        # Column lists answer "which fields does table X have" without the agent
        # having to reverse-engineer them from a sample row (it guessed 8 of 9
        # that way and missed is_deleted).  Blocked fields are filtered out here,
        # so they are absent from the schema as well as from the data.
        columns = store.fetch(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
            "DATA_TYPE AS data_type, COLUMN_COMMENT AS comment "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %(db)s AND COLUMN_NAME NOT IN %(blocked)s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            {"db": _db.DB_NAME, "blocked": tuple(sorted(store.BLOCKED_FIELDS))},
            caliber=f"已排除禁止外泄字段：{', '.join(sorted(store.BLOCKED_FIELDS))}",
            limit=store.MAX_ROWS,
        )
        by_table: dict[str, list[str]] = {}
        for row in columns["rows"]:
            by_table.setdefault(str(row["table_name"]), []).append(str(row["column_name"]))
        return {
            "ok": True,
            "boards": boards["rows"],
            "categories": categories["rows"],
            "table_columns": by_table,
            "field_notes": {
                "formal_task": store.FORMAL_TASK_CALIBER,
                "status": "0未开始 / 1进行中 / 2已完成 / 3已停用",
                "completion_time": "展示文本，不可做日期运算（R-12）",
                "owner_multi_value": "分管领导等为多值分隔文本，须去空格后匹配（R-13）",
                "blocked_fields": sorted(store.BLOCKED_FIELDS),
                "sensitive_fields": sorted(store.SENSITIVE_FIELDS),
                "table_columns_note": "已剔除禁止外泄字段，故此清单即可对外引用的全部字段",
            },
            "snapshot_note": store.SNAPSHOT_NOTE,
        }

    return _guard("weekly_schema", work)


@mcp.tool()
def weekly_task_query(
    board: str = "",
    category: str = "",
    status: str = "",
    owner: str = "",
    keyword: str = "",
    limit: int = 200,
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
            where.append("t.board_id = %(bid)s")
            params["bid"] = board_id
        if category.strip():
            where.append("t.category_id IN (SELECT id FROM task_category WHERE is_deleted = 0 AND name LIKE %(cat)s)")
            params["cat"] = f"%{category.strip()}%"
        if status.strip():
            if status.strip() not in {"0", "1", "2", "3"}:
                return {
                    "ok": False,
                    "error": {"code": "invalid_status", "message": "status 只能是 0/1/2/3"},
                }
            where.append("t.status = %(st)s")
            params["st"] = int(status.strip())
        if owner.strip():
            # All six owner columns: three ids and three names. Covering only the
            # name columns for lead/project silently dropped tasks where the
            # person is recorded by id alone (task 150 holds lead_owner_id
            # 'u3208' with lead_owner_name empty) -- 7 rows instead of 8.
            #
            # Ids match exactly; names match as substrings because they are
            # multi-value text. R-13: strip spaces on both sides first.
            token = owner.strip().replace(" ", "")
            where.append(
                "(REPLACE(IFNULL(t.owner_user_id,''),' ','') = %(own_exact)s "
                "OR REPLACE(IFNULL(t.project_owner_id,''),' ','') = %(own_exact)s "
                "OR REPLACE(IFNULL(t.lead_owner_id,''),' ','') = %(own_exact)s "
                "OR REPLACE(IFNULL(t.project_owner_name,''),' ','') LIKE %(own)s "
                "OR REPLACE(IFNULL(t.lead_owner_name,''),' ','') LIKE %(own)s)"
            )
            params["own_exact"] = token
            params["own"] = f"%{token}%"
        if keyword.strip():
            where.append("t.task_name LIKE %(kw)s")
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
def weekly_task_detail(task: str, ctx: Context | None = None) -> str:
    """Fetch one formal task with its group detail, latest progress and year goal.

    Args:
        task: Task id or task name (fuzzy).
    """
    may_read = _caller_may_read_sensitive(ctx)

    def work() -> dict[str, Any]:
        found = store.resolve_task(task)
        if found is None:
            return {
                "ok": False,
                "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
            }
        task_id = int(found["id"])
        detail = store.fetch(
            "SELECT * FROM task_group_detail WHERE task_id = %(tid)s",
            {"tid": task_id},
            caliber="completion_time 为展示文本，不可做日期运算（R-12）",
            limit=1,
        )
        progress = store.fetch(
            "SELECT id, task_id, version_no, latest_progress, next_work, progress_date, "
            "report_time, is_published, review_comment "
            "FROM task_progress WHERE task_id = %(tid)s AND is_published = 1 "
            "ORDER BY version_no DESC, id DESC",
            {"tid": task_id},
            caliber="is_published = 1（仅正式发布进展）；review_comment 按权限展示"
            + ("（本次凭证有权限，原文返回）" if may_read else "（本次凭证无权限，已遮蔽）"),
            can_read_sensitive=may_read,
            limit=3,
        )
        goal = store.fetch(
            "SELECT * FROM task_year_goal WHERE task_id = %(tid)s ORDER BY year DESC",
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
def weekly_progress_history(
    task: str, published_only: bool = True, limit: int = 200, ctx: Context | None = None
) -> str:
    """Return progress versions for one task, newest first (version_no desc).

    Args:
        task: Task id or name.
        published_only: True keeps only is_published=1 rows (formal progress).
        limit: Max rows, capped at 200.
    """
    may_read = _caller_may_read_sensitive(ctx)

    def work() -> dict[str, Any]:
        task_id = store.resolve_task_id(task)
        if task_id is None:
            return {
                "ok": False,
                "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
            }
        where = "task_id = %(tid)s"
        caliber = "按 version_no 倒序，越大越新"
        if published_only:
            where += " AND is_published = 1"
            caliber += "；is_published = 1"
        return store.fetch(
            "SELECT id, task_id, version_no, latest_progress, next_work, progress_date, "
            "report_time, is_published, review_comment "
            f"FROM task_progress WHERE {where} ORDER BY version_no DESC, id DESC",
            {"tid": task_id},
            caliber=caliber
            + ("；review_comment 原文返回（本次凭证有敏感字段权限）" if may_read else "；review_comment 已按权限遮蔽"),
            can_read_sensitive=may_read,
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
            scope += " AND t.board_id = %(bid)s"
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
def weekly_milestone_query(year: str = "", status: str = "", limit: int = 200) -> str:
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
            where.append("m.year = %(yr)s")
            params["yr"] = int(year.strip())
        if status.strip():
            if status.strip() not in {"0", "1"}:
                return {"ok": False, "error": {"code": "invalid_status", "message": "status 只能是 0/1"}}
            where.append("m.status = %(st)s")
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
def weekly_workflow_query(task: str = "", limit: int = 200, ctx: Context | None = None) -> str:
    """Trace approval submissions and actions. Opinions are permission-gated (R-04/R-14).

    Args:
        task: Task id or name; empty returns the most recent actions overall.
        limit: Max rows, capped at 200.
    """
    may_read = _caller_may_read_sensitive(ctx)

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["1 = 1"]
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("a.task_id = %(tid)s")
            params["tid"] = task_id
        return store.fetch(
            "SELECT a.id, a.submission_id, a.task_id, s.round_no, a.node_type, a.action, "
            "a.operator_name, a.opinion, a.created_at "
            "FROM task_workflow_action a "
            "LEFT JOIN task_workflow_submission s ON s.id = a.submission_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY a.task_id, s.round_no, a.id",
            params,
            caliber=(
                "opinion 属敏感字段，按权限展示（R-04/R-14）；"
                + ("本次凭证有敏感字段权限，opinion 原文返回" if may_read else "本次凭证无敏感字段权限，opinion 已遮蔽")
                + "；payload 草稿快照不返回"
            ),
            can_read_sensitive=may_read,
            limit=limit,
        )

    return _guard("weekly_workflow_query", work)


@mcp.tool()
def weekly_attachment_query(task: str = "", limit: int = 200) -> str:
    """List attachments without ever returning storage_path (chapter 7.2).

    Args:
        task: Task id or name; empty lists across all formal tasks.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["att.is_deleted = 0"]
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("att.task_id = %(tid)s")
            params["tid"] = task_id
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
def weekly_submission_query(
    task: str = "",
    reporter: str = "",
    status: str = "",
    exclude_status: str = "",
    limit: int = 200,
) -> str:
    """Query approval submission forms (task_workflow_submission).

    Distinct from weekly_workflow_query, which returns the action *log*: a
    submission carries round_no and its own status, and the action log cannot be
    aggregated into it.  payload (the draft snapshot) is never returned.

    Args:
        task: Task id or name; empty covers all tasks.
        reporter: Reporter id or name, exact match after trimming.
        status: Keep only this submission status.
        exclude_status: Drop this status (e.g. approved, for "not yet approved").
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["t.is_deleted = 0"]
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("s.task_id = %(tid)s")
            params["tid"] = task_id
        if reporter.strip():
            token = reporter.strip()
            where.append("(TRIM(IFNULL(s.reporter_id,'')) = %(rep)s OR TRIM(IFNULL(s.reporter_name,'')) = %(rep)s)")
            params["rep"] = token
        if status.strip():
            where.append("s.status = %(st)s")
            params["st"] = status.strip()
        if exclude_status.strip():
            where.append("s.status <> %(exst)s")
            params["exst"] = exclude_status.strip()

        clause = " AND ".join(where)
        rows = store.fetch(
            "SELECT s.id, s.task_id, t.task_name, s.round_no, s.status, s.submission_kind, "
            "s.reporter_id, s.reporter_name, s.signer_name, s.need_sign, "
            "s.submitted_at, s.completed_at "
            "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
            f"WHERE {clause} ORDER BY s.task_id, s.round_no, s.id",
            params,
            caliber=(
                "task_id + round_no 唯一；payload 草稿快照默认不并入正式数据，不返回；"
                "submission_kind 区分 initial / progress"
            ),
            limit=limit,
        )
        breakdown = store.fetch(
            "SELECT s.status, COUNT(*) AS cnt "
            "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
            f"WHERE {clause} GROUP BY s.status ORDER BY cnt DESC",
            params,
            caliber="按提交单状态分档计数",
            limit=20,
        )
        rows["status_breakdown"] = breakdown["rows"]
        return rows

    return _guard("weekly_submission_query", work)


@mcp.tool()
def weekly_owner_roles(person: str) -> str:
    """Count one person's formal tasks split by the role they hold.

    weekly_task_query's owner filter ORs the three owner columns together, so it
    cannot answer "how many as project owner vs as lead"; this separates them.
    Matching strips spaces first (R-13) and accepts either id or name.

    Args:
        person: User id or name.
    """

    def work() -> dict[str, Any]:
        token = (person or "").strip().replace(" ", "")
        if not token:
            return {"ok": False, "error": {"code": "invalid_argument", "message": "person 不能为空"}}
        clause = store.formal_task_clause()
        # Each role counted separately, plus any_role as the de-duplicated union.
        return store.fetch(
            "SELECT "
            "SUM(REPLACE(IFNULL(t.owner_user_id,''),' ','') = %(p)s) AS as_owner, "
            "SUM(REPLACE(IFNULL(t.project_owner_id,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.project_owner_name,''),' ','') = %(p)s) AS as_project_owner, "
            "SUM(REPLACE(IFNULL(t.lead_owner_id,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.lead_owner_name,''),' ','') = %(p)s) AS as_lead_owner, "
            "SUM(REPLACE(IFNULL(t.owner_user_id,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.project_owner_id,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.project_owner_name,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.lead_owner_id,''),' ','') = %(p)s "
            "  OR REPLACE(IFNULL(t.lead_owner_name,''),' ','') = %(p)s) AS any_role "
            f"FROM task t WHERE {clause}",
            {"p": token},
            caliber=f"{store.FORMAL_TASK_CALIBER}；多值列去空格后匹配（R-13）；any_role 为三角色去重并集",
            limit=1,
        )

    return _guard("weekly_owner_roles", work)


# Fields whose fill-in rate can be asked about (R-07 / R-19). Whitelisted rather
# than interpolated from the argument: the column name reaches SQL as an
# identifier, which no placeholder can bind.
_COMPLETENESS_FIELDS: dict[str, tuple[str, str]] = {
    "overall_goal": ("task", "总体目标"),
    "annual_goals": ("task", "年度目标"),
    "project_owner_name": ("task", "项目负责人"),
    "lead_owner_name": ("task", "分管领导"),
    "project_group": ("task", "项目组"),
    "target_result": ("task_group_detail", "目标成果"),
    "implementation_measure": ("task_group_detail", "实施举措"),
    "progress_effect": ("task_group_detail", "进度成效"),
    "completion_time": ("task_group_detail", "完成时间（文本）"),
}


@mcp.tool()
def weekly_field_completeness(field: str = "") -> str:
    """Count how many formal tasks have a given field filled in (R-07 / R-19).

    Answers "how many tasks have an overall goal / a named project owner" with one
    call. Without this the only route is fetching every task and counting by hand,
    which burns the tool-call budget and tends to run out mid-answer.

    Args:
        field: Column to measure; empty lists the supported columns.
    """

    def work() -> dict[str, Any]:
        token = (field or "").strip()
        if not token:
            return {
                "ok": True,
                "supported_fields": {
                    name: {"table": table, "label": label}
                    for name, (table, label) in sorted(_COMPLETENESS_FIELDS.items())
                },
                "caliber": "传入 field 以统计该字段的填报完整度",
                "snapshot_note": store.SNAPSHOT_NOTE,
            }
        if token not in _COMPLETENESS_FIELDS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_field",
                    "message": f"不支持的字段：{token}；支持 {', '.join(sorted(_COMPLETENESS_FIELDS))}",
                },
            }
        table, label = _COMPLETENESS_FIELDS[token]
        clause = store.formal_task_clause()
        if table == "task":
            sql = (
                f"SELECT COUNT(*) AS total, SUM(t.{token} IS NOT NULL AND t.{token} <> '') AS filled, "
                f"SUM(t.{token} IS NULL OR t.{token} = '') AS missing "
                f"FROM task t WHERE {clause}"
            )
        else:
            # LEFT JOIN so tasks with no detail row count as missing, not vanish (R-08).
            sql = (
                f"SELECT COUNT(*) AS total, SUM(d.{token} IS NOT NULL AND d.{token} <> '') AS filled, "
                f"SUM(d.{token} IS NULL OR d.{token} = '') AS missing "
                f"FROM task t LEFT JOIN {table} d ON d.task_id = t.id WHERE {clause}"
            )
        result = store.fetch(
            sql,
            caliber=(
                f"{store.FORMAL_TASK_CALIBER}；统计「{label}」非空占比（R-07/R-19）；"
                "空字符串按未填计入 missing" + ("；LEFT JOIN 保留无明细行的任务（R-08）" if table != "task" else "")
            ),
            limit=1,
        )
        result["field"] = token
        result["field_label"] = label
        return result

    return _guard("weekly_field_completeness", work)


@mcp.tool()
def weekly_progress_coverage() -> str:
    """Summarise how far back published progress goes and how much it covers.

    Returns row count, tasks covered, earliest/latest progress date and the
    highest version number -- the "how long is the history" question, answered
    without walking every task.
    """

    def work() -> dict[str, Any]:
        return store.fetch(
            "SELECT COUNT(*) AS progress_rows, COUNT(DISTINCT p.task_id) AS tasks_covered, "
            "MIN(p.progress_date) AS earliest, MAX(p.progress_date) AS latest, "
            "MAX(p.version_no) AS max_version "
            "FROM task_progress p JOIN task t ON t.id = p.task_id "
            f"WHERE {store.formal_task_clause()} AND p.is_published = 1",
            caliber=f"{store.FORMAL_TASK_CALIBER}；仅正式发布进展（is_published = 1）",
            limit=1,
        )

    return _guard("weekly_progress_coverage", work)


@mcp.tool()
def weekly_task_ranking(metric: str = "attachments", top: int = 5) -> str:
    """Rank formal tasks by a child-record count (attachments, progress, milestones).

    Answers "which task has the most X" directly. Ties keep the id order used by
    the reference queries, so the ranking is reproducible.

    Args:
        metric: attachments / progress / milestones / submissions.
        top: How many rows to return, 1..50.
    """

    def work() -> dict[str, Any]:
        joins = {
            "attachments": ("task_attachment", "a.is_deleted = 0", "附件数"),
            "progress": ("task_progress", "a.is_published = 1", "正式进展版本数"),
            "milestones": ("task_milestone", "a.is_deleted = 0", "里程碑数"),
            "submissions": ("task_workflow_submission", "1 = 1", "审批提交单数"),
        }
        chosen = joins.get(metric.strip())
        if chosen is None:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_metric",
                    "message": f"metric 支持 {', '.join(sorted(joins))}",
                },
            }
        table, extra, label = chosen
        try:
            bounded = max(1, min(50, int(top)))
        except TypeError, ValueError:
            return {"ok": False, "error": {"code": "invalid_argument", "message": "top 必须是整数"}}
        result = store.fetch(
            f"SELECT t.id, t.task_name, COUNT(a.id) AS cnt "
            f"FROM task t JOIN {table} a ON a.task_id = t.id AND {extra} "
            f"WHERE {store.formal_task_clause()} "
            f"GROUP BY t.id, t.task_name ORDER BY cnt DESC, t.id LIMIT {bounded}",
            caliber=f"{store.FORMAL_TASK_CALIBER}；按{label}降序，并列按 task id 升序",
            limit=bounded,
        )
        result["metric"] = metric.strip()
        result["metric_label"] = label
        return result

    return _guard("weekly_task_ranking", work)


@mcp.tool()
def weekly_import_audit(limit: int = 200) -> str:
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
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %(db)s ORDER BY TABLE_NAME",
                    {"db": _db.DB_NAME},
                )
                tables = [row["t"] for row in cursor.fetchall()]
                # information_schema.TABLE_ROWS is an InnoDB estimate (it read 14
                # for a 158-row table), so count for real.
                counts: dict[str, int] = {}
                for table in tables:
                    cursor.execute(f"SELECT COUNT(*) AS c FROM `{table}`")
                    counts[table] = int(cursor.fetchone()["c"])
        finally:
            conn.close()
        return {
            "ok": True,
            "store": _db.DSN_DESCRIPTION,
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

    try:
        probe = store.connect()
        probe.close()
    except store.QueryError as exc:
        print(f"mock store unreachable: {exc}", file=sys.stderr)
        print("start MySQL and import the dump -- see README", file=sys.stderr)
        return 2

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"mock weekly MCP on http://{args.host}:{args.port}/mcp", flush=True)
    print(f"store: {_db.DSN_DESCRIPTION}", flush=True)
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

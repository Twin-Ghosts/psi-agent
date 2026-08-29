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


# Which date column a time window applies to. Whitelisted for the same reason as
# _COMPLETENESS_FIELDS: it reaches SQL as an identifier, not a bound value.
# The two are not interchangeable -- a progress row filed late has a report_time
# months after the progress_date it covers, and E2-04 turns exactly on that gap.
_PROGRESS_DATE_FIELDS: dict[str, str] = {
    "progress_date": "进展周期",
    "report_time": "上报时间",
}

# Bucket expression and ORDER BY for grouped counts. `bucket` is the SELECT alias.
_PROGRESS_GROUPINGS: dict[str, tuple[str, str]] = {
    "month": ("DATE_FORMAT(p.progress_date, '%%Y-%%m')", "bucket"),
    "quarter": ("CONCAT(YEAR(p.progress_date), 'Q', QUARTER(p.progress_date))", "bucket"),
    "task": ("t.task_name", "progress_count DESC, bucket"),
}

# Buckets over task.created_at -- the setup clock, not the reporting clock.
_CREATED_GROUPINGS: dict[str, str] = {
    "month": "DATE_FORMAT(t.created_at, '%%Y-%%m')",
    "year": "YEAR(t.created_at)",
}

_TURNAROUND_SCOPES = ("summary", "board", "slowest", "pending")

# Columns of task_group_detail a caller may select or filter on. Whitelisted for
# the same reason as _COMPLETENESS_FIELDS: these reach SQL as identifiers.
_GROUP_DETAIL_FIELDS: dict[str, str] = {
    "target_result": "目标成果",
    "implementation_measure": "实施举措",
    "completion_time": "完成时间（展示文本，不可做日期运算）",
    "progress_effect": "进度成效当前正文",
    "lead_owner_names": "牵头人姓名（多值，逗号分隔）",
    "lead_owner_ids": "牵头人 ID（多值，逗号分隔）",
    "project_owner_names": "项目负责人姓名（多值，逗号分隔）",
    "project_owner_ids": "项目负责人 ID（多值，逗号分隔）",
    "project_group": "项目组",
}

# Which multi-value owner column a person lookup searches. The two are distinct
# roles: 唐立本 leads 5 group tasks but is project owner on 3, and collapsing
# them answers a different question (the single_vs_multi_owner_column trap).
_GROUP_OWNER_ROLES: dict[str, tuple[str, str]] = {
    "lead": ("lead_owner_ids", "lead_owner_names"),
    "project": ("project_owner_ids", "project_owner_names"),
}

_GROUP_STATS_SCOPES = (
    "owners",
    "completion_time",
    "field_lengths",
    "attachments",
    "history_rounds",
)

# Buckets over task_group_progress_history.report_time -- the group board keeps
# its progress in its own table, so the _PROGRESS_GROUPINGS over task_progress
# cannot see any of it (R7-02: group_task_progress = 0, group_history = 362).
_GROUP_HISTORY_GROUPINGS: dict[str, tuple[str, str]] = {
    "year": ("YEAR(h.report_time)", "bucket"),
    "month": ("DATE_FORMAT(h.report_time, '%%Y-%%m')", "bucket"),
    "quarter": ("CONCAT(YEAR(h.report_time), 'Q', QUARTER(h.report_time))", "bucket"),
    "task": ("t.task_name", "progress_count DESC, bucket"),
    "reporter": ("h.reporter_id", "progress_count DESC, bucket"),
}

_YEAR_GOAL_SCOPES = ("by_year", "coverage", "missing", "missing_by_group", "span", "multi_year")

# Milestone breakdown dimensions. Whitelisted because they reach SQL as
# identifiers; the labels double as the caliber text.
_MILESTONE_DIMENSIONS: dict[str, str] = {
    "year": "年度",
    "category": "类别",
    "group_name": "承担组",
    "status": "完成状态",
    "task_status": "任务状态",
}

_MILESTONE_STATS_SCOPES = ("summary", "by_dimension", "deleted", "per_task", "mismatch")

_MILESTONE_MISMATCH_KINDS = ("task_done_milestones_open", "milestones_done_task_open")


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
def weekly_progress_range(
    date_from: str = "",
    date_to: str = "",
    last_days: int = 0,
    by: str = "",
    date_field: str = "progress_date",
    limit: int = 200,
    ctx: Context | None = None,
) -> str:
    """Query or count published progress across a time window, over ALL tasks.

    This is the time-axis entry point. Without it the only way to answer "how
    many progress reports in the last 30 days" is to walk every task's history
    one call at a time, which exhausts the tool budget before an answer exists.

    Relative windows are measured from the data snapshot date, not from the
    current clock -- see ``_store.AS_OF``.

    Args:
        date_from: Inclusive start, YYYY-MM-DD. Empty means unbounded.
        date_to: Inclusive end, YYYY-MM-DD. Empty means unbounded.
        last_days: Window of N days ending at the snapshot date. Overrides date_from.
        by: Empty lists rows; ``month`` / ``quarter`` / ``task`` returns counts per group.
        date_field: ``progress_date`` (the period reported on) or ``report_time``
            (when it was submitted). These differ for late filings.
        limit: Max rows, capped at 200.
    """
    may_read = _caller_may_read_sensitive(ctx)

    def work() -> dict[str, Any]:
        if date_field not in _PROGRESS_DATE_FIELDS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_field",
                    "message": f"不支持的 date_field：{date_field}；支持 {', '.join(sorted(_PROGRESS_DATE_FIELDS))}",
                },
            }
        grouping = (by or "").strip().lower()
        if grouping and grouping not in _PROGRESS_GROUPINGS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_group_by",
                    "message": f"不支持的 by：{by}；支持 {', '.join(sorted(_PROGRESS_GROUPINGS))}",
                },
            }

        column = f"p.{date_field}"
        lo, hi = store.date_window(date_from, date_to, last_days or None)
        params: dict[str, Any] = {}
        where = f"{store.formal_task_clause()} AND p.is_published = 1"
        window = store.window_clause(column, lo, hi, params)
        if window:
            where += f" AND {window}"

        field_label = _PROGRESS_DATE_FIELDS[date_field]
        caliber = (
            f"{store.FORMAL_TASK_CALIBER}；仅正式发布进展（is_published = 1）；"
            f"{store.window_caliber(lo, hi, label=field_label)}；{store.as_of_caliber()}"
        )

        if not grouping:
            # Counts must survive truncation: "今年以来报了多少期" is 366 rows, well
            # past MAX_ROWS, and a caller seeing 200 + has_more cannot recover the
            # real total. Both totals are reported because E1 asks for each
            # separately (rows = 期数, tasks = 更新过进展的任务数).
            totals = store.fetch(
                "SELECT COUNT(*) AS total_rows, COUNT(DISTINCT p.task_id) AS total_tasks "
                "FROM task_progress p JOIN task t ON t.id = p.task_id "
                f"WHERE {where}",
                params,
                caliber=caliber,
                limit=1,
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, p.version_no, p.progress_date, "
                "p.report_time, DATEDIFF(p.report_time, p.progress_date) AS lag_days "
                "FROM task_progress p JOIN task t ON t.id = p.task_id "
                f"WHERE {where} ORDER BY p.{date_field} DESC, t.id",
                params,
                caliber=caliber + "；lag_days>0 表示补报更早周期",
                can_read_sensitive=may_read,
                limit=limit,
            )
            first = totals["rows"][0] if totals["rows"] else {}
            rows["total_count"] = first.get("total_rows")
            rows["total_tasks"] = first.get("total_tasks")
            return rows

        expression, order = _PROGRESS_GROUPINGS[grouping]
        select = f"{expression} AS bucket, COUNT(*) AS progress_count"
        if grouping != "task":
            select += ", COUNT(DISTINCT p.task_id) AS task_count"
        return store.fetch(
            f"SELECT {select} FROM task_progress p JOIN task t ON t.id = p.task_id "
            f"WHERE {where} GROUP BY bucket ORDER BY {order}",
            params,
            caliber=caliber + f"；按 {grouping} 分组计数",
            limit=limit,
        )

    return _guard("weekly_progress_range", work)


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
def weekly_task_lifecycle(by: str = "", year: int = 0) -> str:
    """Report when formal tasks were created and how long they took to publish.

    Answers the "when was this set up" axis, which is ``task.created_at`` /
    ``published_at`` -- a different clock from progress reporting. Without this
    the only route is listing tasks and counting dates by hand.

    Args:
        by: Empty returns min/max/avg summary; ``month`` or ``year`` returns counts per bucket.
        year: Restrict to one creation year (0 means all years).
    """

    def work() -> dict[str, Any]:
        grouping = (by or "").strip().lower()
        if grouping and grouping not in _CREATED_GROUPINGS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_group_by",
                    "message": f"不支持的 by：{by}；支持 {', '.join(sorted(_CREATED_GROUPINGS))}",
                },
            }
        params: dict[str, Any] = {}
        where = store.formal_task_clause()
        caliber = store.FORMAL_TASK_CALIBER
        if year:
            where += " AND YEAR(t.created_at) = %(yr)s"
            params["yr"] = int(year)
            caliber += f"；仅 created_at 属于 {int(year)} 年"

        if grouping:
            expression = _CREATED_GROUPINGS[grouping]
            return store.fetch(
                f"SELECT {expression} AS bucket, COUNT(*) AS created_count "
                f"FROM task t WHERE {where} GROUP BY bucket ORDER BY bucket",
                params,
                caliber=caliber + f"；按 created_at 的{grouping}分组",
            )
        return store.fetch(
            "SELECT COUNT(*) AS formal_tasks, MIN(t.created_at) AS earliest_created, "
            "MAX(t.created_at) AS latest_created, "
            "SUM(t.published_at IS NOT NULL) AS with_published_at, "
            "ROUND(AVG(DATEDIFF(t.published_at, t.created_at)), 1) AS avg_days_to_publish, "
            "MAX(DATEDIFF(t.published_at, t.created_at)) AS max_days_to_publish "
            f"FROM task t WHERE {where}",
            params,
            caliber=caliber + "；到发布天数仅统计 published_at 非空的任务",
            limit=1,
        )

    return _guard("weekly_task_lifecycle", work)


@mcp.tool()
def weekly_freshness_distribution(task: str = "", within_days: int = 0, drift: bool = False, limit: int = 200) -> str:
    """Bucket formal tasks by how stale their latest progress is (30/90/180 天/从未).

    Also cross-checks ``task.latest_progress_time`` against the real newest
    published progress row: the denormalised column can drift, and a stale
    freshness answer is indistinguishable from a correct one without this check.

    Args:
        task: Empty returns the distribution; a task id/name returns that task's
            own freshness plus the drift check.
        within_days: When > 0, returns how many formal tasks reported within that
            many days of the snapshot date instead of the fixed buckets. The
            buckets are 30/90/180, so an arbitrary window (E6-03 asks for 7)
            cannot be read off them.
        drift: True lists only the tasks whose ``latest_progress_time`` disagrees
            with their real newest published progress row.
        limit: Max rows for the drift listing, capped at 200.
    """

    def work() -> dict[str, Any]:
        if within_days and not task.strip():
            lo, _ = store.date_window(last_days=within_days)
            return store.fetch(
                "SELECT COUNT(*) AS task_count, MAX(t.latest_progress_time) AS newest_progress, "
                "DATEDIFF(%(as_of)s, MAX(t.latest_progress_time)) AS days_behind "
                f"FROM task t WHERE {store.formal_task_clause()} "
                "AND t.latest_progress_time >= %(lo)s",
                {"as_of": store.AS_OF, "lo": lo},
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；latest_progress_time 不早于 {lo}"
                    f"（{int(within_days)} 天窗）；{store.as_of_caliber()}"
                ),
                limit=1,
            )
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.latest_progress_time, "
                "MAX(p.report_time) AS actual_latest_report, "
                "DATEDIFF(%(as_of)s, t.latest_progress_time) AS days_behind "
                "FROM task t LEFT JOIN task_progress p ON p.task_id = t.id AND p.is_published = 1 "
                f"WHERE t.id = %(tid)s AND {store.formal_task_clause()} "
                "GROUP BY t.id, t.task_name, t.latest_progress_time",
                {"tid": task_id, "as_of": store.AS_OF},
                caliber=f"{store.FORMAL_TASK_CALIBER}；{store.as_of_caliber()}",
                limit=1,
            )
        if drift:
            # The denormalised column vs the real newest published row. Only rows
            # that actually disagree are returned -- a "no drift" answer is then
            # row_count == 0, which is checkable, unlike a full dump.
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.latest_progress_time, "
                "MAX(p.report_time) AS actual_latest_report "
                "FROM task t JOIN task_progress p ON p.task_id = t.id AND p.is_published = 1 "
                f"WHERE {store.formal_task_clause()} "
                "GROUP BY t.id, t.task_name, t.latest_progress_time "
                "HAVING t.latest_progress_time IS NULL AND actual_latest_report IS NOT NULL "
                "OR t.latest_progress_time <> actual_latest_report "
                "ORDER BY t.id",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；仅列出 latest_progress_time 与"
                    "实际最新已发布进展 report_time 不一致的任务"
                ),
                limit=limit,
            )
        buckets = store.fetch(
            "SELECT CASE "
            "WHEN t.latest_progress_time IS NULL THEN '4 从未报进展' "
            "WHEN t.latest_progress_time >= DATE_SUB(%(as_of)s, INTERVAL 30 DAY) THEN '1 30 天内' "
            "WHEN t.latest_progress_time >= DATE_SUB(%(as_of)s, INTERVAL 90 DAY) THEN '2 31-90 天' "
            "WHEN t.latest_progress_time >= DATE_SUB(%(as_of)s, INTERVAL 180 DAY) THEN '3 91-180 天' "
            "ELSE '5 超过 180 天' END AS freshness_bucket, COUNT(*) AS task_count "
            f"FROM task t WHERE {store.formal_task_clause()} "
            "GROUP BY freshness_bucket ORDER BY freshness_bucket",
            {"as_of": store.AS_OF},
            caliber=f"{store.FORMAL_TASK_CALIBER}；{store.as_of_caliber()}",
        )
        # E6-01 asks how current the board is overall, which the buckets do not
        # state: the newest timestamp and how far it lags the snapshot date.
        overall = store.fetch(
            "SELECT MAX(t.latest_progress_time) AS newest_progress, "
            "DATEDIFF(%(as_of)s, MAX(t.latest_progress_time)) AS days_behind "
            f"FROM task t WHERE {store.formal_task_clause()}",
            {"as_of": store.AS_OF},
            limit=1,
        )
        first = overall["rows"][0] if overall["rows"] else {}
        buckets["newest_progress"] = first.get("newest_progress")
        buckets["days_behind"] = first.get("days_behind")
        buckets["as_of"] = store.AS_OF
        return buckets

    return _guard("weekly_freshness_distribution", work)


@mcp.tool()
def weekly_approval_turnaround(scope: str = "summary", top: int = 8) -> str:
    """Measure approval elapsed time: overall, per board, slowest, or still-pending backlog.

    ``pending`` deliberately drops the published filter: a submission stuck in
    approval is by definition not published yet, so gating on R-01 would report
    an empty backlog.

    Args:
        scope: summary / board / slowest / pending.
        top: Row cap for slowest and pending, 1..50.
    """

    def work() -> dict[str, Any]:
        key = (scope or "summary").strip().lower()
        if key not in _TURNAROUND_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的 scope：{scope}；支持 {', '.join(sorted(_TURNAROUND_SCOPES))}",
                },
            }
        try:
            bounded = max(1, min(50, int(top)))
        except TypeError, ValueError:
            return {"ok": False, "error": {"code": "invalid_argument", "message": "top 必须是整数"}}

        done = "s.completed_at IS NOT NULL AND s.submitted_at IS NOT NULL"
        formal = store.formal_task_clause()
        if key == "summary":
            return store.fetch(
                "SELECT COUNT(*) AS completed_rounds, "
                "ROUND(AVG(DATEDIFF(s.completed_at, s.submitted_at)), 1) AS avg_days, "
                "MAX(DATEDIFF(s.completed_at, s.submitted_at)) AS max_days "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                f"WHERE {formal} AND {done}",
                caliber=f"{store.FORMAL_TASK_CALIBER}；仅已完成轮次（completed_at 非空）",
                limit=1,
            )
        if key == "board":
            return store.fetch(
                "SELECT b.name AS board_name, COUNT(*) AS n, "
                "ROUND(AVG(DATEDIFF(s.completed_at, s.submitted_at)), 1) AS avg_days "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "JOIN task_board b ON b.id = t.board_id AND b.is_deleted = 0 "
                f"WHERE {formal} AND {done} GROUP BY b.id, b.name ORDER BY b.sort_order",
                caliber=f"{store.FORMAL_TASK_CALIBER}；仅已完成轮次",
            )
        if key == "slowest":
            return store.fetch(
                "SELECT t.task_name, s.round_no, s.submitted_at, s.completed_at, "
                "DATEDIFF(s.completed_at, s.submitted_at) AS days "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                f"WHERE {formal} AND {done} ORDER BY days DESC, t.id",
                caliber=f"{store.FORMAL_TASK_CALIBER}；仅已完成轮次，按耗时降序",
                limit=bounded,
            )
        return store.fetch(
            "SELECT t.task_name, s.round_no, s.status, s.submitted_at, "
            "DATEDIFF(%(as_of)s, s.submitted_at) AS pending_days "
            "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
            "WHERE t.is_deleted = 0 AND s.completed_at IS NULL AND s.submitted_at IS NOT NULL "
            "ORDER BY pending_days DESC, t.id",
            {"as_of": store.AS_OF},
            caliber=(
                "仅 is_deleted = 0（不加发布闸门：待审提交单本就尚未发布）；"
                f"未完成即 completed_at 为空；{store.as_of_caliber()}"
            ),
            limit=bounded,
        )

    return _guard("weekly_approval_turnaround", work)


@mcp.tool()
def weekly_year_goal_query(task: str = "", year: int = 0, limit: int = 200) -> str:
    """List annual goals and milestone summaries for formal tasks.

    ``task_year_goal`` is unique per (task, year). Only ``weekly_task_detail``
    exposed it before, and only for a single task, so board-wide goal questions
    had no route at all.

    Args:
        task: Task id or name; empty covers every formal task.
        year: Four-digit year; 0 covers every year the task has goals for.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = [store.formal_task_clause()]
        caliber = [store.FORMAL_TASK_CALIBER, "task_id + year 唯一"]

        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            params["tid"] = task_id
            where.append("g.task_id = %(tid)s")
        if year:
            params["yr"] = int(year)
            where.append("g.year = %(yr)s")
            caliber.append(f"仅 {int(year)} 年度")

        clause = " AND ".join(where)
        totals = store.fetch(
            "SELECT COUNT(*) AS total_rows, COUNT(DISTINCT g.task_id) AS total_tasks "
            f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause}",
            params,
            limit=1,
        )
        rows = store.fetch(
            "SELECT g.task_id, t.task_name, g.year, g.current_year_goal, g.milestone_summary "
            f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
            "ORDER BY g.task_id, g.year",
            params,
            caliber="；".join(caliber),
            limit=limit,
        )
        first = totals["rows"][0] if totals["rows"] else {}
        # 313 goal rows board-wide, past the 200 cap, so counting questions need
        # the total rather than a truncated row_count.
        rows["total_count"] = first.get("total_rows")
        rows["total_tasks"] = first.get("total_tasks")
        return rows

    return _guard("weekly_year_goal_query", work)


@mcp.tool()
def weekly_year_goal_stats(
    scope: str = "by_year",
    year: int = 0,
    year_to: int = 0,
    min_years: int = 3,
    top: int = 8,
) -> str:
    """Aggregate annual-goal coverage: which years are set, and who is missing one.

    Args:
        scope: ``by_year`` (goals and tasks per year) / ``coverage`` (share of
            formal tasks holding a goal for ``year``) / ``missing`` (tasks without
            one) / ``missing_by_group`` (missing counts per 专项组) / ``span``
            (average years per task, plus tasks reaching ``min_years``) /
            ``multi_year`` (tasks holding goals in both ``year`` and ``year_to``).
        year: Primary year. Required for every scope except ``by_year`` and ``span``.
        year_to: Second year, for ``multi_year``.
        min_years: Threshold for ``span``. Inclusive.
        top: Row cap for the listing scopes.
    """

    def work() -> dict[str, Any]:
        key = (scope or "by_year").strip().lower()
        if key not in _YEAR_GOAL_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_YEAR_GOAL_SCOPES)}",
                },
            }
        needs_year = key in ("coverage", "missing", "missing_by_group", "multi_year")
        if needs_year and not year:
            return {
                "ok": False,
                "error": {"code": "invalid_argument", "message": f"口径 {key} 需要指定 year"},
            }
        bounded = max(1, min(store.MAX_ROWS, int(top)))
        clause = store.formal_task_clause()
        base = store.FORMAL_TASK_CALIBER

        if key == "by_year":
            return store.fetch(
                "SELECT g.year, COUNT(*) AS goal_count, COUNT(DISTINCT g.task_id) AS task_count "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY g.year ORDER BY g.year",
                caliber=f"{base}；按年度统计目标条数与涉及任务数",
                limit=bounded,
            )

        if key == "span":
            threshold = max(1, int(min_years))
            avg = store.scalar(
                "SELECT ROUND(AVG(yr_cnt), 2) AS avg_years FROM (SELECT COUNT(*) AS yr_cnt "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY g.task_id) x",
                caliber=f"{base}；分母只含已设过目标的任务",
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, COUNT(*) AS year_count, "
                "GROUP_CONCAT(g.year ORDER BY g.year) AS years "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY t.id, t.task_name HAVING year_count >= %(n)s "
                "ORDER BY year_count DESC, t.id",
                {"n": threshold},
                caliber=f"{base}；至少 {threshold} 个年度（含 {threshold}，边界取等）",
                limit=bounded,
            )
            rows["avg_years_per_task"] = avg["value"]
            rows["min_years"] = threshold
            return rows

        if key == "coverage":
            # EXISTS over the whole task table, not a JOIN over goals: the tasks
            # with no goal row are precisely the answer, and an inner join drops
            # them (the missing_goal_as_zero trap).
            return store.fetch(
                "SELECT COUNT(*) AS total_tasks, "
                "SUM(EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s)) AS has_goal, "
                "SUM(NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s)) AS missing_goal, "
                "ROUND(SUM(EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s)) / COUNT(*) * 100, 1) AS coverage_pct "
                f"FROM task t WHERE {clause}",
                {"yr": int(year)},
                caliber=f"{base}；分母为全部正式任务，未设目标的任务计入缺口（不能用 JOIN 丢掉）",
                limit=1,
            )

        if key == "missing":
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.status, t.project_group "
                f"FROM task t WHERE {clause} AND NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s) ORDER BY t.id",
                {"yr": int(year)},
                caliber=f"{base}；{int(year)} 年度无目标行；status 0 未开始 / 1 进行中 / 2 已完成 / 3 已暂停",
                limit=bounded,
            )

        if key == "missing_by_group":
            return store.fetch(
                "SELECT t.project_group, COUNT(*) AS missing_count "
                f"FROM task t WHERE {clause} AND NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s) "
                "GROUP BY t.project_group ORDER BY missing_count DESC, t.project_group",
                {"yr": int(year)},
                caliber=f"{base}；按专项组统计 {int(year)} 年度目标缺口",
                limit=bounded,
            )

        if not year_to:
            return {
                "ok": False,
                "error": {"code": "invalid_argument", "message": "口径 multi_year 需要 year 与 year_to"},
            }
        params = {"yr1": int(year), "yr2": int(year_to)}
        both = store.scalar(
            "SELECT COUNT(*) AS tasks FROM (SELECT g.task_id "
            f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
            "AND g.year IN (%(yr1)s, %(yr2)s) "
            "GROUP BY g.task_id HAVING COUNT(DISTINCT g.year) = 2) x",
            params,
            caliber=f"{base}；两个年度都设了目标",
        )
        rows = store.fetch(
            "SELECT t.id AS task_id, t.task_name, "
            "MAX(CASE WHEN g.year = %(yr1)s THEN g.current_year_goal END) AS goal_year_1, "
            "MAX(CASE WHEN g.year = %(yr2)s THEN g.current_year_goal END) AS goal_year_2 "
            f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
            "AND g.year IN (%(yr1)s, %(yr2)s) GROUP BY t.id, t.task_name "
            "HAVING goal_year_1 IS NOT NULL AND goal_year_2 IS NOT NULL ORDER BY t.id",
            params,
            caliber=f"{base}；{int(year)} 与 {int(year_to)} 两年对照",
            limit=bounded,
        )
        rows["tasks_in_both_years"] = both["value"]
        rows["years"] = [int(year), int(year_to)]
        return rows

    return _guard("weekly_year_goal_stats", work)


@mcp.tool()
def weekly_milestone_stats(
    scope: str = "summary",
    by: str = "category",
    year: int = 0,
    category: str = "",
    min_total: int = 0,
    kind: str = "task_done_milestones_open",
    top: int = 8,
) -> str:
    """Aggregate milestone completion. weekly_milestone_query only lists rows.

    ``status`` is 0 未完成 / 1 已完成 -- a two-value code, so "completed" means
    ``status = 1`` and never a text match.

    Args:
        scope: ``summary`` (totals and finish rate) / ``by_dimension`` (grouped by
            ``by``) / ``deleted`` (soft-delete audit, the one place deleted rows
            are counted) / ``per_task`` (counts per task, zero-milestone tasks
            kept) / ``mismatch`` (task status vs milestone status disagreements).
        by: Dimension for ``by_dimension``: year / category / group_name / status
            / task_status.
        year: Restrict to one milestone year; 0 covers all.
        category: Restrict to one milestone category.
        min_total: For ``by_dimension``, drop buckets below this count. Inclusive.
        kind: For ``mismatch``: ``task_done_milestones_open`` (task marked done
            with milestones still open) or ``milestones_done_task_open``.
        top: Row cap for the listing scopes.
    """

    def work() -> dict[str, Any]:
        key = (scope or "summary").strip().lower()
        if key not in _MILESTONE_STATS_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_MILESTONE_STATS_SCOPES)}",
                },
            }
        bounded = max(1, min(store.MAX_ROWS, int(top)))
        clause = store.formal_task_clause()
        status_note = "m.status 为 0/1 两值码：1 已完成、0 未完成"

        # R-17: milestones must be re-checked against the formal-task caliber, and
        # the milestone row's own soft-delete flag is separate from the task's.
        where = [clause, "m.is_deleted = 0"]
        params: dict[str, Any] = {}
        caliber = [f"m.is_deleted = 0 且关联任务满足 {store.FORMAL_TASK_CALIBER}（R-17）", status_note]
        if year:
            params["yr"] = int(year)
            where.append("m.year = %(yr)s")
            caliber.append(f"仅 {int(year)} 年度里程碑")
        if category.strip():
            params["cat"] = category.strip()
            where.append("m.category = %(cat)s")
            caliber.append(f"仅类别「{category.strip()}」")
        active = " AND ".join(where)

        if key == "deleted":
            # The only scope that counts deleted rows, and it deliberately does
            # not apply the formal-task gate: "how many were soft-deleted" is a
            # question about the table, and filtering by task would undercount.
            return store.fetch(
                "SELECT SUM(m.is_deleted = 0) AS active, SUM(m.is_deleted = 1) AS deleted, "
                "COUNT(*) AS total_rows FROM task_milestone m",
                caliber="全表口径（不加任务闸门）：这是关于表的问题，按任务过滤会少算",
                limit=1,
            )

        if key == "summary":
            return store.fetch(
                "SELECT COUNT(*) AS total, SUM(m.status = 1) AS finished, "
                "SUM(m.status = 0) AS unfinished, "
                "ROUND(SUM(m.status = 1) / COUNT(*) * 100, 1) AS finish_rate_pct "
                f"FROM task_milestone m JOIN task t ON t.id = m.task_id WHERE {active}",
                params,
                caliber="；".join(caliber),
                limit=1,
            )

        if key == "by_dimension":
            dimension = (by or "category").strip().lower()
            if dimension not in _MILESTONE_DIMENSIONS:
                return {
                    "ok": False,
                    "error": {
                        "code": "unsupported_group_by",
                        "message": f"不支持的维度：{by}；支持 {', '.join(sorted(_MILESTONE_DIMENSIONS))}",
                    },
                }
            column = "t.status" if dimension == "task_status" else f"m.{dimension}"
            having = ""
            if min_total:
                params["min_total"] = max(1, int(min_total))
                having = "HAVING total >= %(min_total)s "
                caliber.append(f"仅保留计数不少于 {max(1, int(min_total))} 的分组（边界取等）")
            return store.fetch(
                f"SELECT {column} AS bucket, COUNT(*) AS total, SUM(m.status = 1) AS finished, "
                "ROUND(SUM(m.status = 1) / COUNT(*) * 100, 1) AS finish_rate_pct "
                f"FROM task_milestone m JOIN task t ON t.id = m.task_id WHERE {active} "
                f"GROUP BY bucket {having}ORDER BY total DESC, bucket",
                params,
                caliber="；".join(caliber) + f"；按{_MILESTONE_DIMENSIONS[dimension]}分组",
                limit=bounded,
            )

        if key == "per_task":
            summary = store.fetch(
                "SELECT COUNT(DISTINCT t.id) AS tasks, COUNT(m.id) AS milestones, "
                "ROUND(COUNT(m.id) / COUNT(DISTINCT t.id), 2) AS avg_per_task, "
                "SUM(m.id IS NULL) AS tasks_without_milestone "
                "FROM task t LEFT JOIN task_milestone m ON m.task_id = t.id AND m.is_deleted = 0 "
                f"WHERE {clause}",
                caliber=f"{store.FORMAL_TASK_CALIBER}；分母为全部正式任务（含零里程碑任务）",
                limit=1,
            )
            # LEFT JOIN so the three tasks with no milestone stay visible: H5-01
            # asks for exactly those, and an inner join answers a different question.
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.status AS task_status, "
                "COUNT(m.id) AS milestones, SUM(m.status = 1) AS finished "
                "FROM task t LEFT JOIN task_milestone m ON m.task_id = t.id AND m.is_deleted = 0 "
                f"WHERE {clause} GROUP BY t.id, t.task_name, t.status "
                "ORDER BY milestones DESC, t.id",
                caliber=f"{store.FORMAL_TASK_CALIBER}；LEFT JOIN 保留零里程碑任务（R-08）；{status_note}",
                limit=bounded,
            )
            rows["summary"] = summary["rows"][0] if summary["rows"] else {}
            return rows

        mismatch = (kind or "task_done_milestones_open").strip().lower()
        if mismatch not in _MILESTONE_MISMATCH_KINDS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_kind",
                    "message": f"不支持的比对：{kind}；支持 {', '.join(_MILESTONE_MISMATCH_KINDS)}",
                },
            }
        if mismatch == "task_done_milestones_open":
            extra, having, label = "t.status = 2", "SUM(m.status = 1) < COUNT(*)", "任务已完成但里程碑未全完成"
        else:
            extra, having, label = "t.status = 1", "SUM(m.status = 1) = COUNT(*)", "里程碑全完成但任务仍在办"
        return store.fetch(
            "SELECT t.id AS task_id, t.task_name, t.status AS task_status, "
            "COUNT(*) AS milestones, SUM(m.status = 1) AS finished_milestones "
            f"FROM task_milestone m JOIN task t ON t.id = m.task_id WHERE {active} AND {extra} "
            f"GROUP BY t.id, t.task_name, t.status HAVING {having} ORDER BY t.id",
            params,
            caliber="；".join(caliber) + f"；{label}（task.status 2 已完成 / 1 进行中）",
            limit=bounded,
        )

    return _guard("weekly_milestone_stats", work)


@mcp.tool()
def weekly_group_detail_query(
    task: str = "",
    fields: str = "",
    contains: str = "",
    field: str = "",
    limit: int = 200,
) -> str:
    """Query the 集团组 board's own detail table (target/measures/owners/completion text).

    The group board keeps 目标成果 / 实施举措 / 进度成效 / 完成时间 and its
    multi-value owner columns in ``task_group_detail``, which no other tool
    reaches: ``weekly_task_query`` returns the shared ``task`` columns and simply
    has none of these. Use this for any 集团看板 question about those fields.

    Args:
        task: Task id or name to narrow to one task. Empty covers the whole board.
        fields: Comma-separated columns to return; empty returns the common set.
            Call with an unsupported name to see the supported list.
        contains: Substring filter applied to ``field``. Use for "which tasks are
            due in 2026" -- ``completion_time`` is display text, so this is a text
            match, never date arithmetic (R-12).
        field: Which column ``contains`` filters on. Required when ``contains`` is set.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        requested = [f.strip() for f in (fields or "").split(",") if f.strip()]
        unknown = [f for f in requested if f not in _GROUP_DETAIL_FIELDS]
        if unknown:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_field",
                    "message": f"不支持的字段：{', '.join(unknown)}；支持 {', '.join(sorted(_GROUP_DETAIL_FIELDS))}",
                },
            }
        selected = requested or [
            "target_result",
            "implementation_measure",
            "progress_effect",
            "completion_time",
        ]

        params: dict[str, Any] = {}
        where = [store.formal_task_clause()]
        caliber = [store.FORMAL_TASK_CALIBER, "集团看板（task_board.code = 'group'）"]

        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            params["tid"] = task_id
            where.append("d.task_id = %(tid)s")

        needle = (contains or "").strip()
        if needle:
            column = (field or "").strip()
            if column not in _GROUP_DETAIL_FIELDS:
                return {
                    "ok": False,
                    "error": {
                        "code": "unsupported_field",
                        "message": f"contains 需要指定 field；不支持 {column!r}，"
                        f"支持 {', '.join(sorted(_GROUP_DETAIL_FIELDS))}",
                    },
                }
            params["needle"] = f"%{needle}%"
            where.append(f"d.{column} LIKE %(needle)s")
            caliber.append(f"{column} 含「{needle}」（文本匹配，非日期运算）")
            if column not in selected:
                selected.append(column)

        columns = ", ".join(f"d.{name}" for name in selected)
        if "completion_time" in selected:
            caliber.append("completion_time 为展示文本，不可做日期运算（R-12）")

        return store.fetch(
            f"SELECT d.task_id, t.task_name, {columns} "
            "FROM task_group_detail d JOIN task t ON t.id = d.task_id "
            f"{store.group_board_join()} "
            f"WHERE {' AND '.join(where)} ORDER BY d.task_id",
            params,
            caliber="；".join(caliber),
            limit=limit,
        )

    return _guard("weekly_group_detail_query", work)


@mcp.tool()
def weekly_group_owner_query(person: str = "", role: str = "lead", limit: int = 200) -> str:
    """Find 集团组 tasks by owner, or list the board's owner columns.

    The group board's owners are comma-separated multi-value text, so a plain
    ``LIKE '%name%'`` collides across people (the multivalue_like_collision
    trap). Matching goes through ``FIND_IN_SET`` on the id column instead, which
    is exact per element.

    Args:
        person: Person id or name. Empty lists every task's owners for the role.
        role: ``lead`` (牵头人) or ``project`` (项目负责人). These are different
            roles over different columns and are not interchangeable.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        key = (role or "lead").strip().lower()
        if key not in _GROUP_OWNER_ROLES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_role",
                    "message": f"不支持的角色：{role}；支持 {', '.join(sorted(_GROUP_OWNER_ROLES))}",
                },
            }
        id_column, name_column = _GROUP_OWNER_ROLES[key]
        label = "牵头人" if key == "lead" else "项目负责人"

        params: dict[str, Any] = {}
        where = [store.formal_task_clause()]
        caliber = [store.FORMAL_TASK_CALIBER, f"集团看板 {label}（{id_column}）"]

        token = (person or "").strip()
        if token:
            # The question may name a person while the column stores ids, so try
            # both: FIND_IN_SET is exact per comma element either way, which is
            # what keeps 唐立本 from matching a longer name containing it.
            params["who"] = token
            where.append(f"(FIND_IN_SET(%(who)s, d.{id_column}) > 0 OR FIND_IN_SET(%(who)s, d.{name_column}) > 0)")
            caliber.append(f"FIND_IN_SET 精确匹配「{token}」（逗号多值，不用 LIKE 以免跨人误命中）")
        else:
            where.append(f"d.{id_column} <> ''")
            caliber.append("仅列出该角色非空的任务")

        return store.fetch(
            f"SELECT d.task_id, t.task_name, d.{name_column}, d.{id_column}, "
            f"LENGTH(d.{id_column}) - LENGTH(REPLACE(d.{id_column}, ',', '')) + 1 AS owner_count "
            "FROM task_group_detail d JOIN task t ON t.id = d.task_id "
            f"{store.group_board_join()} "
            f"WHERE {' AND '.join(where)} ORDER BY owner_count DESC, d.task_id",
            params,
            caliber="；".join(caliber),
            limit=limit,
        )

    return _guard("weekly_group_owner_query", work)


@mcp.tool()
def weekly_group_history(
    task: str = "",
    version_no: int = 0,
    by: str = "",
    latest_only: bool = False,
    date_from: str = "",
    date_to: str = "",
    last_days: int = 0,
    limit: int = 200,
) -> str:
    """Query the 集团组 board's progress history (its own table, not task_progress).

    The group board's progress lives in ``task_group_progress_history`` -- 362
    published rows -- while ``task_progress`` holds none of it. So
    ``weekly_progress_history`` and ``weekly_progress_range`` both return empty
    for group tasks, and this is the entry point for them.

    Two gates apply together: the task must be formal (R-01) and the row itself
    must have ``is_published = 1``. Dropping either folds 42 un-approved drafts in.

    Args:
        task: Task id or name. Empty covers the whole board.
        version_no: Return one specific version (per-task, larger is newer).
        by: Empty lists rows; ``year`` / ``month`` / ``quarter`` / ``task`` /
            ``reporter`` returns counts per group.
        latest_only: Keep only each task's newest published version.
        date_from: Inclusive start on ``report_time``, YYYY-MM-DD.
        date_to: Inclusive end on ``report_time``, YYYY-MM-DD.
        last_days: Window of N days ending at the snapshot date, not today.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        grouping = (by or "").strip().lower()
        if grouping and grouping not in _GROUP_HISTORY_GROUPINGS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_group_by",
                    "message": f"不支持的分组：{by}；支持 {', '.join(sorted(_GROUP_HISTORY_GROUPINGS))}",
                },
            }

        params: dict[str, Any] = {}
        where = [store.group_history_gate()]
        caliber = [store.GROUP_HISTORY_CALIBER]

        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            params["tid"] = task_id
            where.append("h.task_id = %(tid)s")
        else:
            # Board scoping is only needed for whole-board queries: a task filter
            # already pins the board, and the extra join would be dead weight.
            where.append(f"b.code = '{store.GROUP_BOARD_CODE}' AND b.is_deleted = 0")

        if version_no:
            params["vno"] = int(version_no)
            where.append("h.version_no = %(vno)s")
            caliber.append(f"仅第 {int(version_no)} 期")

        lo, hi = store.date_window(date_from, date_to, last_days or None)
        window = store.window_clause("h.report_time", lo, hi, params)
        if window:
            # report_time is a datetime and the bound end is a date, so a naive
            # `<=` would cut the final day off at 00:00. Compare on the date part.
            window = store.window_clause("DATE(h.report_time)", lo, hi, params)
            where.append(window)
            caliber.append(store.window_caliber(lo, hi, label="上报时间"))
            if last_days:
                caliber.append(store.as_of_caliber())

        if latest_only:
            where.append(
                "h.version_no = (SELECT MAX(x.version_no) FROM task_group_progress_history x "
                "WHERE x.task_id = h.task_id AND x.is_published = 1)"
            )
            caliber.append("仅各任务最新一期已发布版本")

        joins = f"JOIN task t ON t.id = h.task_id {store.group_board_join()}"
        clause = " AND ".join(where)

        if not grouping:
            totals = store.fetch(
                "SELECT COUNT(*) AS total_rows, COUNT(DISTINCT h.task_id) AS total_tasks "
                f"FROM task_group_progress_history h {joins} WHERE {clause}",
                params,
                caliber="；".join(caliber),
                limit=1,
            )
            rows = store.fetch(
                "SELECT h.task_id, t.task_name, h.version_no, h.progress_effect, "
                "h.completion_time, h.reporter_id, h.report_time "
                f"FROM task_group_progress_history h {joins} WHERE {clause} "
                "ORDER BY h.task_id, h.version_no DESC, h.id DESC",
                params,
                caliber="；".join(caliber),
                limit=limit,
            )
            first = totals["rows"][0] if totals["rows"] else {}
            # Counting questions must survive the 200-row cap: the board has 362
            # published rows, so a caller seeing 200 + has_more cannot recover it.
            rows["total_count"] = first.get("total_rows")
            rows["total_tasks"] = first.get("total_tasks")
            return rows

        expression, order = _GROUP_HISTORY_GROUPINGS[grouping]
        select = f"{expression} AS bucket, COUNT(*) AS progress_count"
        if grouping not in ("task",):
            select += ", COUNT(DISTINCT h.task_id) AS task_count"
        return store.fetch(
            f"SELECT {select} FROM task_group_progress_history h {joins} "
            f"WHERE {clause} GROUP BY bucket ORDER BY {order}",
            params,
            caliber="；".join(caliber) + f"；按 {grouping} 分组计数",
            limit=limit,
        )

    return _guard("weekly_group_history", work)


@mcp.tool()
def weekly_group_stats(scope: str = "owners", top: int = 8, min_rounds: int = 0) -> str:
    """Aggregate stats over the 集团组 board that plain listing cannot answer.

    Args:
        scope: ``owners`` (multi vs single lead, distinct leads),
            ``completion_time`` (ISO vs free text vs blank),
            ``field_lengths`` (target_result char stats),
            ``attachments`` (per-task counts, zero kept),
            ``history_rounds`` (rounds per task, and how many clear ``min_rounds``).
        top: Row cap for the listing scopes.
        min_rounds: For ``history_rounds``, count tasks with at least this many
            published rounds. Inclusive -- "at least 5" means ``>= 5``.
    """

    def work() -> dict[str, Any]:
        key = (scope or "owners").strip().lower()
        if key not in _GROUP_STATS_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_GROUP_STATS_SCOPES)}",
                },
            }
        bounded = max(1, min(store.MAX_ROWS, int(top)))
        clause = store.formal_task_clause()
        join = store.group_board_join()
        base = f"{store.FORMAL_TASK_CALIBER}；集团看板"

        if key == "owners":
            summary = store.fetch(
                "SELECT COUNT(*) AS tasks, "
                "SUM(d.lead_owner_ids LIKE '%%,%%') AS multi_lead, "
                "SUM(d.lead_owner_ids NOT LIKE '%%,%%' AND d.lead_owner_ids <> '') AS single_lead, "
                "SUM(d.lead_owner_ids = '' OR d.lead_owner_ids IS NULL) AS no_lead "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} WHERE {clause}",
                caliber=f"{base}；牵头人多值按逗号判定",
                limit=1,
            )
            # Splitting the multi-value column needs a row source per position;
            # 4 covers the widest cell in this store (max 2 today, headroom kept).
            distinct = store.scalar(
                "SELECT COUNT(*) AS distinct_leads FROM ("
                "SELECT DISTINCT SUBSTRING_INDEX(SUBSTRING_INDEX(d.lead_owner_ids, ',', n.n), ',', -1) AS uid "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                "JOIN (SELECT 1 n UNION SELECT 2 UNION SELECT 3 UNION SELECT 4) n "
                "ON n.n <= LENGTH(d.lead_owner_ids) - LENGTH(REPLACE(d.lead_owner_ids, ',', '')) + 1 "
                f"WHERE {clause} AND d.lead_owner_ids <> '') x",
                caliber=f"{base}；逐元素拆分后去重计数",
            )
            summary["distinct_leads"] = distinct["value"]
            return summary

        if key == "completion_time":
            return store.fetch(
                "SELECT COUNT(*) AS tasks, "
                "SUM(d.completion_time REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') AS iso_date, "
                "SUM(d.completion_time IS NOT NULL AND d.completion_time <> '' "
                "AND d.completion_time NOT REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$') AS free_text, "
                "SUM(d.completion_time IS NULL OR d.completion_time = '') AS blank "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} WHERE {clause}",
                caliber=f"{base}；completion_time 为展示文本，只做格式判别不做日期运算（R-12）",
                limit=1,
            )

        if key == "field_lengths":
            return store.fetch(
                "SELECT COUNT(*) AS tasks, ROUND(AVG(CHAR_LENGTH(d.target_result)), 1) AS avg_chars, "
                "MAX(CHAR_LENGTH(d.target_result)) AS max_chars, "
                "MIN(CHAR_LENGTH(d.target_result)) AS min_chars "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.target_result IS NOT NULL AND d.target_result <> ''",
                caliber=f"{base}；仅统计 target_result 非空的任务；CHAR_LENGTH 按字符非字节",
                limit=1,
            )

        if key == "attachments":
            summary = store.fetch(
                "SELECT COUNT(*) AS tasks, SUM(NOT EXISTS (SELECT 1 FROM task_attachment a "
                "WHERE a.task_id = t.id AND a.is_deleted = 0)) AS no_attachment "
                f"FROM task t {join} WHERE {clause}",
                caliber=f"{base}；附件按 is_deleted = 0 计有效",
                limit=1,
            )
            # LEFT JOIN, so tasks with zero attachments stay in the listing rather
            # than vanishing -- the inner_join_drops_zero trap, and 18 of 46 tasks
            # here have none, which is usually the point of asking.
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, COUNT(a.id) AS attachments "
                f"FROM task t {join} "
                "LEFT JOIN task_attachment a ON a.task_id = t.id AND a.is_deleted = 0 "
                f"WHERE {clause} GROUP BY t.id, t.task_name "
                "ORDER BY attachments ASC, t.id",
                caliber=f"{base}；LEFT JOIN 保留零附件任务（R-08）",
                limit=bounded,
            )
            rows["no_attachment_summary"] = summary["rows"][0] if summary["rows"] else {}
            return rows

        threshold = max(0, int(min_rounds))
        rows = store.fetch(
            "SELECT t.id AS task_id, t.task_name, COUNT(h.id) AS rounds "
            f"FROM task t {join} "
            "LEFT JOIN task_group_progress_history h ON h.task_id = t.id AND h.is_published = 1 "
            f"WHERE {clause} GROUP BY t.id, t.task_name ORDER BY rounds DESC, t.id",
            caliber=f"{store.GROUP_HISTORY_CALIBER}；LEFT JOIN 保留零期任务（R-08）",
            limit=bounded,
        )
        if threshold:
            cleared = store.scalar(
                "SELECT COUNT(*) AS tasks FROM (SELECT h.task_id "
                f"FROM task_group_progress_history h JOIN task t ON t.id = h.task_id {join} "
                f"WHERE {store.group_history_gate()} "
                "GROUP BY h.task_id HAVING COUNT(*) >= %(n)s) x",
                {"n": threshold},
                caliber=f"至少 {threshold} 期（含 {threshold}，边界取等）",
            )
            rows["tasks_at_least"] = {"min_rounds": threshold, "tasks": cleared["value"]}
        return rows

    return _guard("weekly_group_stats", work)


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

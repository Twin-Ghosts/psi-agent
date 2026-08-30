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
    project_group: str = "",
    limit: int = 200,
) -> str:
    """Query formal tasks. Applies R-01 (is_deleted=0 AND published) server-side.

    Args:
        board: Board code (tech/group) or name.
        category: Category name, matched loosely.
        status: Business status 0/1/2/3, empty for all.
        owner: Owner or lead name; multi-value columns are matched per R-13.
        keyword: Substring of the task name.
        project_group: 专项组 name, matched exactly. 专项组 is its own column and
            is not a category or a board -- filtering it through ``category`` or
            ``keyword`` silently returns the wrong set.
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
        if project_group.strip():
            # 专项组是 task 上的独立一列，精确匹配。没有这个过滤器时，
            # 「某组的牵头人都有谁」只能整表拉回来自己筛，而清单封顶 200 行，
            # 模型要么答成全库人名要么说数据被截断。
            where.append("t.project_group = %(pg)s")
            params["pg"] = project_group.strip()

        clause = " AND ".join(where)
        caliber = store.FORMAL_TASK_CALIBER
        if project_group.strip():
            caliber += f"；专项组 = {project_group.strip()}（t.project_group 精确匹配，非分类也非看板）"
        total = store.scalar(
            f"SELECT COUNT(*) FROM task t WHERE {clause}",
            params,
            caliber=caliber,
        )
        rows = store.fetch(
            "SELECT t.id, t.task_no, t.task_name, t.board_id, t.category_id, t.status, "
            "t.project_owner_name, t.lead_owner_name, t.project_group, "
            "t.latest_progress_time, t.published_at "
            f"FROM task t WHERE {clause} ORDER BY t.board_id, t.sort_order, t.id",
            params,
            caliber=caliber,
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
def weekly_aggregate(group_by: str, board: str = "", metric: str = "count", top: int = 0) -> str:
    """Aggregate formal tasks. Uses LEFT JOIN so empty groups still appear (R-02/R-08).

    Args:
        group_by: One of board / category / primary_category / status /
            project_group / owner.
        board: Optional board code or name to scope the aggregation.
        metric: Only "count" is supported in the demo.
        top: When > 0, hard-cut to that many groups after the ordering. "任务最多
            的前 5 个分类" means exactly 5 rows even though 9 categories tie at 5
            tasks -- the cut is the answer, not a truncation to apologise for.
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
        elif group_by == "primary_category":
            # 任务只挂到二级分类，一级分类得经 parent_id 上跳一层。按 category
            # 分组会返回 47 个二级分类，那是另一个问题的答案。
            # 看板过滤落在分类树所属看板上（c.board_id），与 task.board_id 是两条路径。
            board_filter = ""
            if board.strip():
                board_filter = "AND cb.id = %(bid)s"
            sql = (
                "SELECT pc.name AS group_name, COUNT(*) AS cnt FROM task t "
                "JOIN task_category c ON c.id = t.category_id AND c.is_deleted = 0 "
                "JOIN task_board cb ON cb.id = c.board_id AND cb.is_deleted = 0 "
                f"{board_filter} "
                "JOIN task_category pc ON pc.id = c.parent_id AND pc.is_deleted = 0 "
                f"WHERE {store.formal_task_clause()} "
                "GROUP BY pc.id, pc.name ORDER BY cnt DESC, pc.id"
            )
        elif group_by == "status":
            sql = (
                "SELECT CASE t.status WHEN 0 THEN '未开始' WHEN 1 THEN '进行中' "
                "WHEN 2 THEN '已完成' WHEN 3 THEN '已停用' ELSE '未知' END AS group_name, "
                f"COUNT(*) AS cnt FROM task t WHERE {scope} GROUP BY t.status ORDER BY t.status"
            )
        elif group_by == "workflow_status":
            # 唯一不加发布闸门的分组：问的就是审批流转状态分布，把 published 当
            # 前置条件会只剩一档 128，其余六档（未发布的 22 条）全部消失。
            # 与 group_by=status 是两套词汇：这里是审批流转（published /
            # pending_audit / ...），那里是业务进度（未开始 / 进行中 / ...）。
            wf_scope = "t.is_deleted = 0"
            if board.strip():
                wf_scope += " AND t.board_id = %(bid)s"
            sql = (
                "SELECT t.workflow_status AS group_name, COUNT(*) AS cnt FROM task t "
                f"WHERE {wf_scope} GROUP BY t.workflow_status ORDER BY cnt DESC, t.workflow_status"
            )
        elif group_by == "project_group":
            # 组里「几个人牵头」必须由服务端去重，交给模型自己数人名会数错。
            sql = (
                "SELECT IFNULL(NULLIF(TRIM(t.project_group),''),'(未填)') AS group_name, "
                "COUNT(*) AS cnt, "
                "COUNT(DISTINCT NULLIF(TRIM(t.lead_owner_name),'')) AS lead_owner_count, "
                "COUNT(DISTINCT NULLIF(TRIM(t.project_owner_name),'')) AS project_owner_count "
                f"FROM task t WHERE {scope} GROUP BY group_name ORDER BY cnt DESC, group_name"
            )
        elif group_by == "owner":
            # R-11: 分管领导栏存在多种填法，先按填法枚举再计数，不做归一化猜测。
            sql = (
                "SELECT IFNULL(NULLIF(TRIM(t.lead_owner_name),''),'(未填)') AS group_name, "
                f"COUNT(*) AS cnt FROM task t WHERE {scope} GROUP BY group_name ORDER BY cnt DESC, group_name"
            )
        else:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_group_by",
                    "message": "group_by 支持 board / category / primary_category / status / "
                    "workflow_status / project_group / owner",
                },
            }
        caliber = f"{store.FORMAL_TASK_CALIBER}；LEFT JOIN 保留空分组（R-02/R-08）"
        if group_by == "workflow_status":
            caliber = (
                "仅 is_deleted = 0，本口径不加发布闸门（问的就是审批流转状态分布，"
                "加了只会剩 published 一档）；"
                "group_name 是审批流转状态（published / pending_audit / pending_leader / "
                "pending_fill / rejected / signing / cancelled），"
                "与 group_by=status 的业务进度状态（未开始 / 进行中 / 已完成 / 已停用）不是一套词汇；"
                "各档相加等于未删除任务总数 total_tasks，"
                "「尚未发布」= total_tasks - published，不要按在途状态逐项相加（cancelled 既非已发布也非在途）；"
                "published_pct 已由服务端算好，直接引用"
            )
        elif group_by == "primary_category":
            caliber = (
                f"{store.FORMAL_TASK_CALIBER}；按一级分类（二级分类的 parent_id）汇总，"
                "不是二级分类；看板过滤落在分类树所属看板上；"
                "只统计挂到分类树上的任务，未挂分类的任务不进任何一档"
            )
        elif group_by == "project_group":
            caliber += "；lead_owner_count / project_owner_count 已由服务端按人名去重，直接引用该数字，不要自己数人名"
        cut = 0
        if top:
            try:
                cut = max(1, min(store.MAX_ROWS, int(top)))
            except TypeError, ValueError:
                return {"ok": False, "error": {"code": "invalid_argument", "message": "top 必须是整数"}}
            # 截断落在 SQL 里，并在 caliber 写明并列被切掉了几个：模型看到 5 行
            # 就答 5 行，不会因为「还有并列的」而补列成 9 行。
            tied_total = store.scalar(
                f"SELECT COUNT(*) FROM ({sql}) all_groups",
                params,
            )
            sql += f" LIMIT {cut}"
            caliber += (
                f"；按上述定序硬切前 {cut} 组（共 {tied_total['value']} 组）；"
                "边界外与末位并列的分组不属于本题答案，不要补列"
            )
        result = store.fetch(sql, params, caliber=caliber, limit=cut or store.MAX_ROWS)
        result["group_by"] = group_by
        if cut:
            result["total_groups"] = tied_total["value"]
        if group_by == "workflow_status":
            # 「未发布几条」「已发布占比」和分档是同一题的三面，一次给全：分开问
            # 会让模型拿在途各档相加去凑未发布，而 cancelled 两边都不属于。
            wf_where = "t.is_deleted = 0" + (" AND t.board_id = %(bid)s" if board.strip() else "")
            totals = store.fetch(
                "SELECT COUNT(*) AS total_tasks, "
                "SUM(t.workflow_status = 'published') AS published_tasks, "
                "SUM(t.workflow_status <> 'published') AS unpublished_tasks, "
                "ROUND(SUM(t.workflow_status = 'published') / COUNT(*) * 100, 1) AS published_pct "
                f"FROM task t WHERE {wf_where}",
                params,
                limit=1,
            )
            result["totals"] = totals["rows"][0] if totals["rows"] else {}
        return result

    return _guard("weekly_aggregate", work)


@mcp.tool()
def weekly_scale(by: str = "board", mode: str = "totals", year: int = 2026) -> str:
    """Cross-section formal tasks over several child tables at once, de-duplicated.

    Answers "what is the scale of each board / group" and "how complete is the
    data" in one row per group. Every child count is COUNT(DISTINCT ...): joining
    three child tables at once multiplies the rows, so a plain COUNT reports
    milestones x attachments rather than either one.

    Args:
        by: Grouping axis: board / project_group / primary_category.
        mode: ``totals`` tasks plus milestone / attachment / goal counts.
            ``completeness`` how many tasks HAVE a goal / milestone / progress
            (task counts, not child-row counts -- a different question).
            ``intensity`` published progress rows and rows per task.
        year: Which year the annual-goal column looks at. Only used by
            ``totals`` and ``completeness``.
    """

    def work() -> dict[str, Any]:
        axis_key = (by or "board").strip().lower()
        chosen = _SCALE_AXES.get(axis_key)
        if chosen is None:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_by",
                    "message": f"不支持的分组轴：{by}；支持 {', '.join(sorted(_SCALE_AXES))}",
                },
            }
        mode_key = (mode or "totals").strip().lower()
        if mode_key not in _SCALE_MODES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_mode",
                    "message": f"不支持的口径：{mode}；支持 {', '.join(_SCALE_MODES)}",
                },
            }
        axis, order, extra = chosen
        clause = store.formal_task_clause()
        params: dict[str, Any] = {"yr": int(year)}
        base = store.FORMAL_TASK_CALIBER

        if mode_key == "intensity":
            # 分母是任务数而不是进展行数。LEFT JOIN 让零期任务留在分母里，
            # 否则「人均期数」会被抬高（inner_join_drops_zero）。
            return store.fetch(
                f"SELECT {axis} AS bucket, COUNT(DISTINCT t.id) AS tasks, COUNT(p.id) AS progress_rows, "
                "ROUND(COUNT(p.id) / COUNT(DISTINCT t.id), 2) AS rows_per_task "
                f"FROM task t {extra} "
                "LEFT JOIN task_progress p ON p.task_id = t.id AND p.is_published = 1 "
                f"WHERE {clause} GROUP BY {axis} ORDER BY rows_per_task DESC, bucket",
                caliber=(
                    f"{base} 且进展行 p.is_published = 1（两道闸门）；"
                    "rows_per_task = 已发布进展行数 / 任务数，分母含零期任务（LEFT JOIN 保留）；"
                    "该均值由服务端算出，不要拿返回行自己除"
                ),
                limit=store.MAX_ROWS,
            )

        if mode_key == "completeness":
            # 「有目标/有里程碑/有进展的各占多少」问的是任务数，不是子表行数。
            # 用 SUM(EXISTS ...) 而不是 COUNT(DISTINCT 子表键)：后者在子表全空时
            # 也能给 0，但语义要靠 JOIN 保住，多张子表一起 JOIN 就又放大了。
            return store.fetch(
                f"SELECT {axis} AS bucket, COUNT(*) AS tasks, "
                "SUM(EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s)) AS has_goal, "
                "SUM(EXISTS (SELECT 1 FROM task_milestone m "
                "WHERE m.task_id = t.id AND m.is_deleted = 0)) AS has_milestone, "
                "SUM(EXISTS (SELECT 1 FROM task_progress p "
                "WHERE p.task_id = t.id AND p.is_published = 1)) AS has_progress "
                f"FROM task t {extra} WHERE {clause} GROUP BY {axis} ORDER BY {order}",
                params,
                caliber=(
                    f"{base}；has_* 是「有该项的任务数」，不是子表条数（子表条数问 mode=totals）；"
                    f"年度目标按 {int(year)} 年；分母 tasks 为该组全部正式任务"
                ),
                limit=store.MAX_ROWS,
            )

        # totals：三张子表一起 JOIN，每个计数都必须 DISTINCT。
        # 不 DISTINCT 时 milestones 会被 attachments 的行数乘一遍
        # （技术组 294 会算成 1363，fan_out_double_count）。
        return store.fetch(
            f"SELECT {axis} AS bucket, COUNT(DISTINCT t.id) AS tasks, "
            "COUNT(DISTINCT g.task_id) AS with_year_goal, "
            "COUNT(DISTINCT m.id) AS milestones, COUNT(DISTINCT a.id) AS attachments "
            f"FROM task t {extra} "
            "LEFT JOIN task_year_goal g ON g.task_id = t.id AND g.year = %(yr)s "
            "LEFT JOIN task_milestone m ON m.task_id = t.id AND m.is_deleted = 0 "
            "LEFT JOIN task_attachment a ON a.task_id = t.id AND a.is_deleted = 0 "
            f"WHERE {clause} GROUP BY {axis} ORDER BY {order}",
            params,
            caliber=(
                f"{base}；里程碑与附件各自再加自己的 is_deleted = 0；"
                "四个维度一次 JOIN，故每个计数都按主键去重（COUNT(DISTINCT ...)），"
                "各组 milestones 相加等于全库里程碑总数，若比总数大即是被 JOIN 放大了；"
                f"with_year_goal 是「设了 {int(year)} 年度目标的任务数」"
            ),
            limit=store.MAX_ROWS,
        )

    return _guard("weekly_scale", work)


@mcp.tool()
def weekly_milestone_query(task: str = "", year: str = "", status: str = "", limit: int = 200) -> str:
    """List milestones, joined back to task to re-check the formal-task caliber (R-17).

    Args:
        task: Task id or name. Empty covers every formal task -- so "任务 19 有哪些
            里程碑" must pass it, otherwise the answer is the first page of the
            whole board and the row set silently belongs to a different question.
        year: Four-digit year, empty for all.
        status: 0 未完成 / 1 已完成, empty for all.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        where = ["m.is_deleted = 0", store.formal_task_clause()]
        params: dict[str, Any] = {}
        scoped = False
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("m.task_id = %(tid)s")
            params["tid"] = task_id
            scoped = True
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
        clause = " AND ".join(where)
        caliber = f"m.is_deleted = 0 且关联任务满足 {store.FORMAL_TASK_CALIBER}（R-17）"
        # 单任务问「里程碑安排」要按任务内的编排顺序（sort_order）读，那是这份
        # 清单的业务次序；全局浏览才按年度倒序。次序不同会被判成另一个集合。
        order = "m.year, m.sort_order, m.id" if scoped else "m.year DESC, m.id"
        if scoped:
            caliber += "；按 sort_order 给出该任务内的编排顺序；已按 total_count 给全，勿另行截断"
        total = store.scalar(
            f"SELECT COUNT(*) FROM task_milestone m JOIN task t ON t.id = m.task_id WHERE {clause}",
            params,
        )
        rows = store.fetch(
            "SELECT m.id, m.task_id, t.task_name, m.year, m.category, m.group_name, "
            "m.content, m.status, m.sort_order "
            "FROM task_milestone m JOIN task t ON t.id = m.task_id "
            f"WHERE {clause} ORDER BY {order}",
            params,
            caliber=caliber,
            limit=limit,
        )
        rows["total_count"] = total["value"]
        return rows

    return _guard("weekly_milestone_query", work)


@mcp.tool()
def weekly_workflow_query(
    task: str = "",
    action: str = "",
    board: str = "",
    by_task: bool = False,
    scope: str = "",
    limit: int = 200,
    ctx: Context | None = None,
) -> str:
    """Trace approval submissions and actions. Opinions are permission-gated (R-04/R-14).

    Args:
        task: Task id or name; empty returns the most recent actions overall.
        action: Keep only this action (e.g. ``rejected``). Call with an unknown
            value to see the domain -- an out-of-domain word would otherwise
            filter nothing and the full log would pass as a filtered set.
        board: Board code or name to scope by. "集团看板哪些任务被驳回过" needs it.
        by_task: True aggregates per task into action_count instead of listing rows.
        scope: ``by_node_action`` counts every node_type + action pair in one row
            each; ``actions_per_task`` returns the average action count with its
            numerator and denominator.  Either beats counting a truncated listing:
            the log holds 1578 rows and the listing stops at 200.  ``recent``
            orders by the action's own timestamp and carries the task name and
            the submitter -- any "最近谁被驳回了" question needs it, because the
            default listing is ordered by task id and answers no such question.
        limit: Max rows, capped at 200.
    """
    may_read = _caller_may_read_sensitive(ctx)

    def work() -> dict[str, Any]:
        bounded_flow = max(1, min(store.MAX_ROWS, int(limit)))
        scope_key = (scope or "").strip().lower()
        if scope_key not in _WORKFLOW_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(s or '(空)' for s in _WORKFLOW_SCOPES)}",
                },
            }
        if scope_key == "by_node_action":
            # node_type 与 action 是两列，必须两维一起分档：同一个 approved
            # 在 audit / leader / sign 三个节点各有一份，只按 action 分会把
            # 955 条 approved 揉成一档，答不了「哪个节点驳回得多」。
            return store.fetch(
                "SELECT a.node_type, a.action, COUNT(*) AS action_count "
                "FROM task_workflow_action a JOIN task t ON t.id = a.task_id "
                "WHERE t.is_deleted = 0 GROUP BY a.node_type, a.action "
                "ORDER BY action_count DESC, a.node_type, a.action",
                caliber=(
                    "仅 t.is_deleted = 0（动作日志跨发布状态，不加发布闸门）；"
                    "按 node_type + action 两维分档，同一个 action 在不同节点分别计数，"
                    "各档相加等于动作总数 1578；条数由服务端聚合，"
                    "不要翻明细自己数——日志 1578 条而清单封顶 200 行"
                ),
                limit=bounded_flow,
            )
        if scope_key == "actions_per_task":
            return store.fetch(
                "SELECT ROUND(COUNT(*) / COUNT(DISTINCT a.task_id), 2) AS avg_actions, "
                "COUNT(*) AS total_actions, COUNT(DISTINCT a.task_id) AS tasks "
                "FROM task_workflow_action a JOIN task t ON t.id = a.task_id "
                "WHERE t.is_deleted = 0",
                caliber=(
                    "仅 t.is_deleted = 0；分母是有动作记录的任务数（COUNT DISTINCT task_id = 150），"
                    "不是已发布任务数 128；1578 / 150 = 10.52，分子分母一并给出以便核对"
                ),
                limit=1,
            )
        params: dict[str, Any] = {}
        where = ["1 = 1"]
        caliber_extra: list[str] = []
        board_join = ""
        if action.strip():
            domain = {
                str(row["action"]).strip()
                for row in store.fetch(
                    "SELECT DISTINCT action FROM task_workflow_action ORDER BY action",
                    limit=50,
                )["rows"]
                if row["action"] is not None
            }
            token = action.strip()
            if token not in domain:
                return {
                    "ok": False,
                    "error": {
                        "code": "unsupported_action",
                        "message": f"action 不在值域内：{token}；支持 {', '.join(sorted(domain))}",
                    },
                }
            where.append("a.action = %(act)s")
            params["act"] = token
            caliber_extra.append(f"仅 action = {token}")
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            board_join = "JOIN task t ON t.id = a.task_id"
            where.append("t.is_deleted = 0 AND t.board_id = %(bid)s")
            params["bid"] = board_id
            caliber_extra.append("按看板过滤时任务侧只加 is_deleted = 0（动作日志本就跨发布状态）")
        if scope_key == "recent":
            # 「最近有哪些提交单被驳回了」问的是时间序。默认流水按 task_id, round_no
            # 排，最近发生的那条埋在中间，模型只能把 13 条全铺开当答案，答不出「最近」。
            # 顺带把任务名与填报人一并 JOIN 回来：只给 task_id 与操作人，
            # 「谁的单子被谁驳回」还得再查两次。
            if not board.strip():
                where.append("t.is_deleted = 0")
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
                "SELECT a.id, a.task_id, t.task_name, s.round_no, s.reporter_name, s.status, "
                "a.node_type, a.action, a.operator_name, a.opinion, a.created_at AS acted_at "
                "FROM task_workflow_action a JOIN task t ON t.id = a.task_id "
                "JOIN task_workflow_submission s ON s.id = a.submission_id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY a.created_at DESC, t.id",
                params,
                caliber="；".join(
                    [
                        *caliber_extra,
                        "按动作发生时间 acted_at 倒序（并列按任务 id 升序），"
                        "第一行即最近一次；「最近」看的是动作时间，不是任务 id 也不是轮次号",
                        "只含挂在提交单上的动作（INNER JOIN 提交单），"
                        "带任务名与该单填报人，无须再查任务表；"
                        "status 是提交单当前状态，与本行 action 未必同一时刻",
                        "opinion 属敏感字段，按权限展示（R-04/R-14）"
                        + (
                            "；本次凭证有敏感字段权限，opinion 原文返回"
                            if may_read
                            else "；本次凭证无敏感字段权限，opinion 已遮蔽"
                        ),
                    ]
                ),
                can_read_sensitive=may_read,
                limit=bounded_flow,
            )
        if by_task:
            # 「哪些任务被驳回过」问的是任务集合与次数，不是动作流水。逐条明细
            # 里同一任务会出现多次，模型按行数报会把次数当成任务数。
            joins = board_join or "JOIN task t ON t.id = a.task_id"
            if not board.strip():
                where.append("t.is_deleted = 0")
            return store.fetch(
                "SELECT a.task_id, t.task_name, COUNT(*) AS action_count "
                f"FROM task_workflow_action a {joins} "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY a.task_id, t.task_name ORDER BY action_count DESC, a.task_id",
                params,
                caliber="；".join([*caliber_extra, "按任务聚合，action_count 是次数不是任务数；并列按 task id 升序"]),
                limit=limit,
            )
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
            f"FROM task_workflow_action a {board_join} "
            "LEFT JOIN task_workflow_submission s ON s.id = a.submission_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY a.task_id, s.round_no, a.id",
            params,
            caliber="；".join(
                [
                    *caliber_extra,
                    "opinion 属敏感字段，按权限展示（R-04/R-14）"
                    + (
                        "；本次凭证有敏感字段权限，opinion 原文返回"
                        if may_read
                        else "；本次凭证无敏感字段权限，opinion 已遮蔽"
                    ),
                    "payload 草稿快照不返回",
                ]
            ),
            can_read_sensitive=may_read,
            limit=limit,
        )

    return _guard("weekly_workflow_query", work)


@mcp.tool()
def weekly_attachment_query(task: str = "", board: str = "", limit: int = 200) -> str:
    """List attachments without ever returning storage_path (chapter 7.2).

    Args:
        task: Task id or name; empty lists across all formal tasks.
        board: Board code or name (e.g. group / tech) to keep only that board's
            attachments. Without it, asking "what files does the group board hold"
            forces a task-by-task loop over 46 tasks.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        params: dict[str, Any] = {}
        where = ["att.is_deleted = 0"]
        joins = ""
        caliber = [
            "is_deleted = 0；storage_path 禁止外泄，不在返回字段内",
            "file_size 单位是字节，原样报出，不要换算成 KB/MB 也不要写「约」",
        ]
        if task.strip():
            task_id = store.resolve_task_id(task)
            if task_id is None:
                return {
                    "ok": False,
                    "error": {"code": "task_not_found", "message": f"未匹配到正式任务：{task}"},
                }
            where.append("att.task_id = %(tid)s")
            params["tid"] = task_id
        if board.strip():
            # 看板在 task 上，附件表里没有 board_id，所以按看板筛必须 JOIN 回
            # task 并顺带带上任务闸门与任务名——没有任务名的清单答不了「哪些任务」。
            board_id = store.resolve_board(board)
            if board_id is None:
                return {
                    "ok": False,
                    "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"},
                }
            joins = " JOIN task t ON t.id = att.task_id"
            where.append(store.formal_task_clause())
            where.append("t.board_id = %(bid)s")
            params["bid"] = board_id
            caliber.append(f"仅看板 {board.strip()}（看板在 task 上，已 JOIN 回任务并附加正式任务闸门）")
        columns = (
            "att.id, att.task_id, att.progress_id, att.workflow_submission_id, "
            "att.file_name, att.file_size, att.uploader_id, att.upload_time"
        )
        if board.strip():
            columns += ", t.task_name"
        return store.fetch(
            f"SELECT {columns} FROM task_attachment att{joins} "
            f"WHERE {' AND '.join(where)} ORDER BY att.task_id, att.id",
            params,
            caliber="；".join(caliber),
            limit=limit,
        )

    return _guard("weekly_attachment_query", work)


# 附件的聚合口径。deleted / orphan 两档故意不加任务闸门：问的是表本身。
_ATTACHMENT_STATS_SCOPES = (
    "summary",
    "by_ext",
    "largest",
    "by_uploader",
    "uploader_count",
    "by_link",
    "by_progress",
    "zero_attachment",
    "on_open_submission",
    "by_month",
    "deleted",
    "deleted_by_link",
    "orphan",
)


@mcp.tool()
def weekly_attachment_stats(
    scope: str = "summary",
    date_from: str = "",
    top: int = 200,
) -> str:
    """Aggregate attachments: size totals, file types, uploaders, soft-delete audit.

    weekly_attachment_query lists rows and caps at 200, so counting or summing by
    reading rows back understates every total -- there are 454 live attachments on
    formal tasks.  Sizes are returned in bytes AND in MB: the byte figure is the
    authoritative one.

    Args:
        scope: summary (count, total bytes/MB, average) / by_ext (per file
            extension) / largest (biggest files first) / by_uploader (per uploader,
            with size) / uploader_count (distinct uploaders) / by_link (attached to
            progress vs submission vs the task itself) / by_progress (per published
            progress round, attachment-heavy first) / on_open_submission (count on
            submissions that are not yet published) / by_month (uploads per month) /
            deleted (soft-delete audit, whole table) / deleted_by_link (deleted rows
            by attach point) / orphan (rows whose task_id has no task).
        date_from: For by_month, inclusive lower bound YYYY-MM-DD.
        top: Row cap for the listing scopes.
    """

    def work() -> dict[str, Any]:
        key = (scope or "summary").strip().lower()
        if key not in _ATTACHMENT_STATS_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_ATTACHMENT_STATS_SCOPES)}",
                },
            }
        bounded = max(1, min(store.MAX_ROWS, int(top)))
        # 活跃附件的口径：任务侧 R-01 + 附件行自身的软删标记，两道都要。
        live = (
            "FROM task_attachment a JOIN task t ON t.id = a.task_id "
            f"WHERE {store.formal_task_clause()} AND a.is_deleted = 0"
        )
        live_caliber = f"{store.FORMAL_TASK_CALIBER} 且 a.is_deleted = 0（任务闸门 + 附件行软删两道）"
        size_note = "file_size 单位是字节，bytes 列为权威值，MB 列由服务端换算仅供参考"
        # 关联去向的分档表达式，三处口径必须一致，抽出来共用。
        link_case = (
            "CASE WHEN a.progress_id IS NOT NULL THEN '挂在进展' "
            "WHEN a.workflow_submission_id IS NOT NULL THEN '挂在提交单' "
            "ELSE '挂在任务本体' END"
        )

        if key == "summary":
            return store.fetch(
                "SELECT COUNT(*) AS attachment_count, SUM(a.file_size) AS total_bytes, "
                "ROUND(SUM(a.file_size) / 1024 / 1024, 1) AS total_mb, "
                "ROUND(AVG(a.file_size) / 1024, 1) AS avg_kb, "
                f"COUNT(DISTINCT a.task_id) AS tasks_with_attachment {live}",
                caliber=f"{live_caliber}；{size_note}",
                limit=1,
            )

        if key == "by_ext":
            return store.fetch(
                "SELECT LOWER(SUBSTRING_INDEX(a.file_name, '.', -1)) AS ext, COUNT(*) AS n, "
                "SUM(a.file_size) AS total_bytes, "
                f"ROUND(SUM(a.file_size) / 1024 / 1024, 1) AS total_mb {live} "
                "GROUP BY ext ORDER BY n DESC, ext",
                caliber=f"{live_caliber}；按扩展名分档，取文件名最后一段；{size_note}",
                limit=bounded,
            )

        if key == "largest":
            return store.fetch(
                "SELECT a.file_name, a.file_size, "
                "ROUND(a.file_size / 1024 / 1024, 2) AS size_mb, t.task_name "
                f"{live} ORDER BY a.file_size DESC, a.id",
                caliber=f"{live_caliber}；按字节倒序，最大的一条即首行；{size_note}",
                limit=bounded,
            )

        if key == "by_uploader":
            return store.fetch(
                "SELECT a.uploader_id, COUNT(*) AS upload_count, SUM(a.file_size) AS total_bytes, "
                f"ROUND(SUM(a.file_size) / 1024 / 1024, 1) AS total_mb {live} "
                "GROUP BY a.uploader_id ORDER BY upload_count DESC, a.uploader_id",
                caliber=(f"{live_caliber}；按 uploader_id 分组，不是按任务或看板；并列按 ID 定序；{size_note}"),
                limit=bounded,
            )

        if key == "uploader_count":
            return store.fetch(
                f"SELECT COUNT(DISTINCT a.uploader_id) AS uploader_count {live}",
                caliber=f"{live_caliber}；去重上传人数由服务端算，别数返回行",
                limit=1,
            )

        if key == "by_link":
            # 优先级是 progress → submission → 任务本体，一条附件只进一档。
            return store.fetch(
                f"SELECT {link_case} AS link_type, COUNT(*) AS n {live} GROUP BY link_type ORDER BY n DESC, link_type",
                caliber=(
                    f"{live_caliber}；按挂载去向分档，优先级 进展 > 提交单 > 任务本体，"
                    "一条附件只进一档，各档相加等于总数"
                ),
                limit=bounded,
            )

        if key == "by_progress":
            # 「哪些已发布进展带了附件」：闸门在 progress 行上（p.is_published = 1），
            # 与任务闸门是两道。按 (任务, 期号) 聚合，附件多的在前。
            return store.fetch(
                "SELECT t.task_name, p.version_no, COUNT(*) AS attachment_count "
                "FROM task_attachment a JOIN task_progress p ON p.id = a.progress_id "
                "AND p.is_published = 1 JOIN task t ON t.id = a.task_id "
                f"WHERE {store.formal_task_clause()} AND a.is_deleted = 0 "
                "GROUP BY t.id, t.task_name, p.version_no "
                "ORDER BY attachment_count DESC, t.id, p.version_no",
                caliber=(
                    f"{live_caliber} 且 p.is_published = 1（进展行发布闸门，与任务闸门是两道）；"
                    "按任务+期号聚合，同一任务可出现多期；并列按任务 id、期号定序"
                ),
                limit=bounded,
            )

        if key == "zero_attachment":
            # NOT EXISTS 而非 LEFT JOIN ... HAVING COUNT = 0：问的是存在性。
            # 22 条一次列全，别让模型按 128 个任务逐个调 weekly_attachment_query
            # 去看哪个返回空——基线里那正是 51 次重复调用的来源。
            # 分母 128 一并给出：占比要用它，不能拿本次行数当分母。
            total = store.scalar(
                f"SELECT COUNT(*) FROM task t WHERE {store.formal_task_clause()}",
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name FROM task t "
                f"WHERE {store.formal_task_clause()} AND NOT EXISTS "
                "(SELECT 1 FROM task_attachment a WHERE a.task_id = t.id AND a.is_deleted = 0) "
                "ORDER BY t.id",
                caliber=(
                    f"{live_caliber}；一个有效附件都没有的正式任务，按 NOT EXISTS 判定；"
                    "附件软删的任务算「没有」（a.is_deleted = 0 后为空即没有）；"
                    "total_count 是零附件任务数，total_formal_tasks 才是分母"
                ),
                limit=bounded,
            )
            rows["total_formal_tasks"] = total["value"]
            return rows

        if key == "on_open_submission":
            # 「在途」= 提交单状态不是 published。提交单状态另有码值，
            # 不能拿任务的 workflow_status 来判。
            return store.fetch(
                "SELECT COUNT(*) AS attachment_count "
                "FROM task_attachment a "
                "JOIN task_workflow_submission s ON s.id = a.workflow_submission_id "
                f"JOIN task t ON t.id = a.task_id WHERE {store.formal_task_clause()} "
                "AND a.is_deleted = 0 AND s.status <> 'published'",
                caliber=(
                    f"{live_caliber} 且 s.status <> 'published'（在途提交单）；"
                    "提交单状态是自己的一套码值，已发布叫 published，不要拿任务的 workflow_status 判"
                ),
                limit=1,
            )

        if key == "by_month":
            params: dict[str, Any] = {}
            extra = ""
            if date_from.strip():
                params["df"] = date_from.strip()
                extra = " AND a.upload_time >= %(df)s"
            return store.fetch(
                "SELECT DATE_FORMAT(a.upload_time, '%%Y-%%m') AS ym, COUNT(*) AS n, "
                f"ROUND(SUM(a.file_size) / 1024 / 1024, 1) AS total_mb {live}{extra} "
                "GROUP BY ym ORDER BY ym",
                params,
                caliber=(
                    f"{live_caliber}；按 upload_time 的年月分组，升序；"
                    + (f"仅 {date_from.strip()} 起" if date_from.strip() else "未限起始月，含全部历史")
                ),
                limit=bounded,
            )

        if key == "deleted":
            # 软删审计问的是表本身，加任务闸门会少算。
            return store.fetch(
                "SELECT SUM(a.is_deleted = 0) AS active, SUM(a.is_deleted = 1) AS deleted, "
                "COUNT(*) AS total_rows, "
                "SUM(CASE WHEN a.is_deleted = 1 THEN a.file_size ELSE 0 END) AS deleted_bytes, "
                "ROUND(SUM(CASE WHEN a.is_deleted = 1 THEN a.file_size ELSE 0 END) / 1024 / 1024, 1) "
                "AS deleted_mb FROM task_attachment a",
                caliber=f"全表口径（不加任务闸门）：这是关于表的问题，按任务过滤会少算；{size_note}",
                limit=1,
            )

        if key == "deleted_by_link":
            return store.fetch(
                f"SELECT {link_case} AS link_type, COUNT(*) AS n, "
                "ROUND(SUM(a.file_size) / 1024 / 1024, 1) AS total_mb "
                "FROM task_attachment a WHERE a.is_deleted = 1 "
                "GROUP BY link_type ORDER BY n DESC, link_type",
                caliber=f"仅已软删附件（a.is_deleted = 1），全表口径不加任务闸门；{size_note}",
                limit=bounded,
            )

        # orphan: task_id 指向不存在的任务。NOT EXISTS 而非 JOIN，否则孤儿行整批消失。
        return store.fetch(
            "SELECT COUNT(*) AS orphan_count FROM task_attachment a WHERE a.is_deleted = 0 "
            "AND NOT EXISTS (SELECT 1 FROM task t WHERE t.id = a.task_id)",
            caliber=(
                "a.is_deleted = 0 且 task_id 在 task 表中无对应行；"
                "走 NOT EXISTS，用 JOIN 会把孤儿行全部丢掉从而恒等于 0"
            ),
            limit=1,
        )

    return _guard("weekly_attachment_stats", work)


@mcp.tool()
def weekly_submission_query(
    task: str = "",
    reporter: str = "",
    status: str = "",
    exclude_status: str = "",
    status_mismatch: bool = False,
    scope: str = "",
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
        status_mismatch: True 只保留「任务 workflow_status 与其最新一轮提交单状态
            disagrees with its newest submission's status, one row per task. The two
            are separate vocabularies, so the comparison happens server-side.
        scope: Server-side aggregates.  Prefer one of these over reading the
            listing back and counting rows by hand: the listing caps at 200, so a
            hand count only ever sees the first page.
            ``by_kind`` initial vs progress counts.
            ``inflight_count`` in-flight total and the tasks holding them.
            ``inflight_by_board`` in-flight split by board and status.
            ``inflight_multi`` tasks carrying more than one in-flight form.
            ``sign_summary`` need_sign vs not, a different question from
            status = 'signing'.
            ``by_signer`` per-signer counts, blank signer excluded.
            ``sign_turnaround`` average days by need_sign, completed forms only.
            ``rounds_per_task`` average submission rounds, numerator and
            denominator both returned.
            ``published_vs_progress`` published progress forms against published
            progress rows, which live in different tables.
            ``external_ids`` fill rates for the three O2OA identifier columns
            (o2_process_id / o2_work_id / o2_task_id), which the row listing does
            not carry. ``inflight_external`` counts in-flight submissions holding
            an external process id. Empty returns the ordinary listing.
        limit: Max rows, capped at 200.
    """

    def work() -> dict[str, Any]:
        scope_key = (scope or "").strip().lower()
        if scope_key not in _SUBMISSION_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(s or '(空)' for s in _SUBMISSION_SCOPES)}",
                },
            }

        bounded_sub = max(1, min(store.MAX_ROWS, int(limit)))

        if scope_key == "by_kind":
            # 提交单一律只加软删闸门，不加任务发布闸门：在途任务的提交单同样是
            # 提交单，加了发布闸门 312/150 会缩成 310/128，把两条在途任务的单
            # 连同 22 条未发布任务的单一起吞掉。
            return store.fetch(
                "SELECT s.submission_kind, COUNT(*) AS submission_count "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "WHERE t.is_deleted = 0 GROUP BY s.submission_kind "
                "ORDER BY submission_count DESC, s.submission_kind",
                caliber=(
                    "仅 t.is_deleted = 0（提交单不加任务发布闸门：在途任务的提交单同样计入）；"
                    "initial 是初次提交、progress 是进展提交，两档相加等于提交单总数 462；"
                    "各档条数由服务端聚合，不要翻明细自己数——清单封顶 200 行只能看到前一页"
                ),
                limit=2,
            )

        if scope_key == "external_ids":
            # 三列各自的填充率必须一次算完。让模型翻明细数空值，200 行封顶下
            # 它只能看到前一页，算出来的比例是错的。
            return store.fetch(
                "SELECT COUNT(*) AS total, "
                "SUM(s.o2_process_id IS NOT NULL) AS has_process_id, "
                "SUM(s.o2_work_id IS NOT NULL) AS has_work_id, "
                "SUM(s.o2_task_id IS NOT NULL) AS has_task_id, "
                "SUM(s.o2_task_id IS NULL) AS missing_task_id, "
                "ROUND(SUM(s.o2_task_id IS NULL) / COUNT(*) * 100, 1) AS missing_task_id_pct "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "WHERE t.is_deleted = 0",
                caliber=(
                    "仅 t.is_deleted = 0（提交单不加发布闸门：在途单同样带外部标识）；"
                    "o2_process_id / o2_work_id / o2_task_id 是 O2OA 侧的外部标识，"
                    "三列填充率互不相同，不要用其中一列代答另一列；"
                    "缺失率已按 total 算好，直接引用 missing_task_id_pct"
                ),
                limit=1,
            )

        if scope_key in {"inflight_count", "inflight_by_board", "inflight_multi"}:
            # 三档共用同一套在途枚举，只是聚合粒度不同。枚举而非取反：
            # cancelled 那张单既未发布也不在途，status <> 'published' 会多算 1 张。
            placeholders = ", ".join(f"%(f{i})s" for i in range(len(_SUBMISSION_INFLIGHT)))
            flight_params: dict[str, Any] = {f"f{i}": value for i, value in enumerate(_SUBMISSION_INFLIGHT)}
            gate = (
                f"在途 = status IN ({', '.join(_SUBMISSION_INFLIGHT)})，按成员枚举而非取反"
                "（cancelled 既未发布也不在途）；仅 t.is_deleted = 0，不加任务发布闸门"
            )
            if scope_key == "inflight_count":
                return store.fetch(
                    "SELECT COUNT(*) AS inflight_submissions, COUNT(DISTINCT s.task_id) AS tasks "
                    "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                    f"WHERE t.is_deleted = 0 AND s.status IN ({placeholders})",
                    flight_params,
                    caliber=f"{gate}；总数 61 由服务端聚合，不要翻明细自己数（清单封顶 200 行）",
                    limit=1,
                )
            if scope_key == "inflight_by_board":
                # 按看板 + 状态两维分档：rejected 也是在途的一档，漏掉它
                # 各看板就都少算（group 少 4、tech 少 9）。
                return store.fetch(
                    "SELECT b.code AS board_code, s.status, COUNT(*) AS submission_count "
                    "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                    "JOIN task_board b ON b.id = t.board_id AND b.is_deleted = 0 "
                    f"WHERE t.is_deleted = 0 AND s.status IN ({placeholders}) "
                    "GROUP BY b.code, s.status ORDER BY b.code, s.status",
                    flight_params,
                    caliber=(
                        f"{gate}；按看板 + 状态两维分档，rejected 同属在途，各档相加等于在途总数 61；空档不出现即为 0"
                    ),
                    limit=bounded_sub,
                )
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, COUNT(*) AS pending_submissions "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                f"WHERE t.is_deleted = 0 AND s.status IN ({placeholders}) "
                "GROUP BY t.id, t.task_name HAVING pending_submissions > 1 "
                "ORDER BY pending_submissions DESC, t.id",
                flight_params,
                caliber=(
                    f"{gate}；HAVING COUNT(*) > 1 判「同时挂多张在途单」，服务端已聚合，不要按任务逐个调清单去比对"
                ),
                limit=bounded_sub,
            )

        if scope_key in {"sign_summary", "by_signer", "sign_turnaround"}:
            if scope_key == "sign_summary":
                return store.fetch(
                    "SELECT SUM(s.need_sign = 1) AS need_sign, SUM(s.need_sign = 0) AS no_sign, "
                    "COUNT(*) AS total FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                    "WHERE t.is_deleted = 0",
                    caliber=(
                        "仅 t.is_deleted = 0；need_sign 是「这张单要不要会签」的标记，"
                        "155 + 307 = 462 等于提交单总数；它与 status = 'signing'（正在会签，9 张）"
                        "是两个问题，不要拿在途的 signing 张数答「有多少需要会签」"
                    ),
                    limit=1,
                )
            if scope_key == "by_signer":
                # signer_name 为空的不算：那是「没有会签人」不是某个人签了 0 单。
                return store.fetch(
                    "SELECT s.signer_name, COUNT(*) AS signed_count "
                    "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                    "WHERE t.is_deleted = 0 AND s.signer_name IS NOT NULL "
                    "GROUP BY s.signer_name ORDER BY signed_count DESC, s.signer_name",
                    caliber=(
                        "仅 t.is_deleted = 0 且 signer_name 非空（空值是「没有会签人」，"
                        "不是某人签了 0 单）；9 位会签人一次列全，人数与条数都由服务端算"
                    ),
                    limit=bounded_sub,
                )
            # 会签是否更慢：分母只含已完结的单，未完结的没有耗时可算。
            return store.fetch(
                "SELECT s.need_sign, COUNT(*) AS n, "
                "ROUND(AVG(DATEDIFF(s.completed_at, s.submitted_at)), 1) AS avg_days "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "WHERE t.is_deleted = 0 AND s.completed_at IS NOT NULL AND s.submitted_at IS NOT NULL "
                "GROUP BY s.need_sign ORDER BY s.need_sign",
                caliber=(
                    "仅 t.is_deleted = 0 且已完结（completed_at 与 submitted_at 均非空）；"
                    "未完结的单没有耗时，不进分母，所以 n 相加 402 小于总数 462；"
                    "两档均值 14.5 与 14.7 由全量算出，不要拿清单前 200 行的样本均值代答"
                ),
                limit=2,
            )

        if scope_key == "rounds_per_task":
            return store.fetch(
                "SELECT ROUND(COUNT(*) / COUNT(DISTINCT s.task_id), 2) AS avg_rounds, "
                "COUNT(*) AS total_submissions, COUNT(DISTINCT s.task_id) AS tasks "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "WHERE t.is_deleted = 0",
                caliber=(
                    "仅 t.is_deleted = 0；分母是有提交单的任务数（COUNT DISTINCT task_id = 150），"
                    "不是全部任务数也不是已发布任务数 128；462 / 150 = 3.08，"
                    "分子分母一并给出以便核对，不要自己拿别处的任务数去除"
                ),
                limit=1,
            )

        if scope_key == "published_vs_progress":
            # 两个数各有自己的表和闸门，一次给全，避免模型跨两次调用对不上口径。
            return store.fetch(
                "SELECT (SELECT COUNT(*) FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                "WHERE t.is_deleted = 0 AND s.status = 'published' AND s.submission_kind = 'progress') "
                "AS published_progress_submissions, "
                "(SELECT COUNT(*) FROM task_progress p JOIN task t ON t.id = p.task_id "
                "WHERE t.is_deleted = 0 AND p.is_published = 1) AS published_progress_rows",
                caliber=(
                    "两个数不同表不同闸门：已发布进展提交单 272 只数 submission_kind = 'progress' 的单"
                    "（含 initial 会变 400，那答的是另一个问题）；已发布进展行 943 在 task_progress 上"
                    "按 p.is_published = 1 计；集团组的 task_group_progress_history 是第三张表，"
                    "不并入这 943，加进来会得到 1305"
                ),
                limit=1,
            )

        if scope_key == "inflight_external":
            # 「在途」按成员枚举，不用 status <> 'published' 取反：cancelled 那张单
            # 既未发布也不在途，取反会把它算进来（60 vs 59）。
            placeholders = ", ".join(f"%(f{i})s" for i in range(len(_SUBMISSION_INFLIGHT)))
            flight_params = {f"f{i}": value for i, value in enumerate(_SUBMISSION_INFLIGHT)}
            return store.fetch(
                "SELECT COUNT(*) AS inflight_with_process_id, "
                "COUNT(DISTINCT s.task_id) AS tasks "
                "FROM task_workflow_submission s JOIN task t ON t.id = s.task_id "
                f"WHERE t.is_deleted = 0 AND s.o2_process_id IS NOT NULL AND s.status IN ({placeholders})",
                flight_params,
                caliber=(
                    f"在途 = status IN ({', '.join(_SUBMISSION_INFLIGHT)})，按成员枚举；"
                    "不用 status <> 'published' 取反：cancelled 的单既未发布也不在途，"
                    "取反会把它算进来（多 1 张）；仅 t.is_deleted = 0"
                ),
                limit=1,
            )

        if status_mismatch:
            # 最新一轮 = 该任务 round_no 最大的那张单。只加 is_deleted = 0：
            # 任务侧若再加发布闸门，会把「已发布但最新单还在流程里」这批
            # 恰恰是本题答案的行滤掉。
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.workflow_status, "
                "s.round_no, s.status AS latest_submission_status "
                "FROM task t JOIN task_workflow_submission s ON s.task_id = t.id "
                "AND s.round_no = (SELECT MAX(x.round_no) FROM task_workflow_submission x "
                "WHERE x.task_id = t.id) "
                "WHERE t.is_deleted = 0 AND t.workflow_status <> s.status ORDER BY t.id",
                caliber=(
                    "仅 t.is_deleted = 0（不加发布闸门：已发布但最新单仍在流程中的任务正是本题答案）；"
                    "最新一轮取 round_no 最大的提交单；"
                    "任务 workflow_status 与提交单 status 是两套码值，此处按字面不等判定；"
                    "行数即不一致任务总数，按 task id 升序"
                ),
                limit=limit,
            )
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

        # 提交单状态和任务 workflow_status 不是同一套码值：这里给的词若不在值域内，
        # 过滤会静默失效（等价于没过滤），必须显式告诉调用方，否则会把全量当成筛后结果。
        domain = {
            str(row["status"]).strip()
            for row in store.fetch(
                "SELECT DISTINCT status FROM task_workflow_submission ORDER BY status",
                limit=50,
            )["rows"]
            if row["status"] is not None
        }
        unknown = [token for token in (status.strip(), exclude_status.strip()) if token and token not in domain]

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
        rows["status_domain"] = sorted(domain)
        total = store.scalar(
            f"SELECT COUNT(*) AS n FROM task_workflow_submission s JOIN task t ON t.id = s.task_id WHERE {clause}",
            params,
        )
        rows["total_count"] = total.get("value")
        if unknown:
            rows["caliber"] += (
                f"；注意 {'、'.join(unknown)} 不在提交单状态值域 {sorted(domain)} 内，"
                "该过滤条件未筛掉任何行，结果等于未过滤，回答时不要说成「已排除」"
            )
        rows["caliber"] += "；列清单类问题按 total_count 逐条列全，不要只挑几条举例"
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


# 人员维度的聚合口径。每一项都对应一类「让模型自己数人」会数错的问题。
_PERSON_STATS_SCOPES = (
    "workload",
    "workload_summary",
    "single_task",
    "cross_group",
    "dual_role",
    "id_format",
    "id_variants",
    "id_longest",
    "reporters",
    "reporter_count",
    "reviewers",
    "self_review",
    "group_roster",
)

# 人员所在的列。姓名列与 ID 列口径不同，必须分开问。
_PERSON_ROLE_COLUMNS: dict[str, tuple[str, str]] = {
    "lead_owner": ("lead_owner_name", "分管领导（牵头人）"),
    "project_owner": ("project_owner_name", "项目负责人"),
}


@mcp.tool()
def weekly_person_stats(
    scope: str = "workload",
    role: str = "lead_owner",
    project_group: str = "",
    top: int = 200,
) -> str:
    """Aggregate formal tasks by person: workload, cross-group spread, id formats.

    weekly_owner_roles answers "how many does THIS person have"; this answers the
    population-level questions -- who carries the most, how many carry exactly
    one, how many distinct people there are, and whether the id column is
    internally consistent.  Counting people by reading rows back is the single
    biggest source of wrong answers in the F class, so every count here is
    computed server-side.

    Args:
        scope: One of workload / workload_summary / single_task / cross_group /
            dual_role / id_format / id_variants / id_longest / reporters /
            reporter_count / reviewers / self_review / group_roster.
        role: Which person column to group by: lead_owner or project_owner.
        project_group: Required by ``group_roster``, ignored elsewhere: the 专项组
            whose people are wanted, matched exactly.
        top: Row cap for the listing scopes.
    """

    def work() -> dict[str, Any]:
        key = (scope or "workload").strip().lower()
        if key not in _PERSON_STATS_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_PERSON_STATS_SCOPES)}",
                },
            }
        role_key = (role or "lead_owner").strip().lower()
        if role_key not in _PERSON_ROLE_COLUMNS:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_role",
                    "message": f"不支持的角色：{role}；支持 {', '.join(sorted(_PERSON_ROLE_COLUMNS))}",
                },
            }
        column, role_label = _PERSON_ROLE_COLUMNS[role_key]
        bounded = max(1, min(store.MAX_ROWS, int(top)))
        clause = store.formal_task_clause()
        # 姓名为空的行不是「一个叫空的人」，计人头时必须排除，否则人数会多 1。
        named = f"{clause} AND t.{column} IS NOT NULL AND t.{column} <> ''"
        base = f"{store.FORMAL_TASK_CALIBER}；按「{role_label}」分组，姓名为空的行不计入人头"

        if key == "workload":
            return store.fetch(
                f"SELECT t.{column} AS person, COUNT(*) AS task_count "
                f"FROM task t WHERE {named} "
                f"GROUP BY t.{column} ORDER BY task_count DESC, person",
                caliber=f"{base}；并列按姓名定序，取「最多」时留意并列",
                limit=bounded,
            )

        if key == "workload_summary":
            # 平均值必须一次算完：分组后让模型自己求平均，它会拿组内均值当全局均值。
            return store.fetch(
                "SELECT COUNT(*) AS tasks, "
                f"COUNT(DISTINCT t.{column}) AS people, "
                f"ROUND(COUNT(*) / COUNT(DISTINCT t.{column}), 2) AS avg_tasks_per_person, "
                f"MAX(c.task_count) AS max_tasks, MIN(c.task_count) AS min_tasks "
                f"FROM task t JOIN (SELECT t2.{column} AS person, COUNT(*) AS task_count "
                f"FROM task t2 WHERE {named.replace('t.', 't2.')} GROUP BY t2.{column}) c "
                f"ON c.person = t.{column} WHERE {named}",
                caliber=f"{base}；avg_tasks_per_person = 任务数 / 去重人数，为全局均值而非组内均值",
                limit=1,
            )

        if key == "single_task":
            return store.fetch(
                f"SELECT t.{column} AS person, COUNT(*) AS task_count "
                f"FROM task t WHERE {named} "
                f"GROUP BY t.{column} HAVING task_count = 1 ORDER BY person",
                caliber=f"{base}；只带 1 个任务的人，HAVING 由服务端判定",
                limit=bounded,
            )

        if key == "group_roster":
            # 「某组的牵头人都有谁」：19 条任务里只有 9 个人，去重必须落在服务端。
            # 拿任务清单让模型自己数人，它会把同一个人按任务重复计数。
            target = (project_group or "").strip()
            if not target:
                return {
                    "ok": False,
                    "error": {
                        "code": "missing_project_group",
                        "message": "group_roster 需要 project_group；"
                        "先用 weekly_aggregate group_by=project_group 看有哪些组",
                    },
                }
            return store.fetch(
                f"SELECT t.{column} AS person, COUNT(*) AS task_count "
                f"FROM task t WHERE {named} AND t.project_group = %(pg)s "
                f"GROUP BY t.{column} ORDER BY task_count DESC, person",
                {"pg": target},
                caliber=(
                    f"{base}；专项组 = {target}（精确匹配）；"
                    "行数即该组去重后的人数，不要拿任务条数当人数"
                    "（标准安全组 19 条任务只有 9 位牵头人）；"
                    "姓名为空的行已排除，所以 task_count 相加可能小于该组任务数"
                ),
                limit=bounded,
            )

        if key == "cross_group":
            return store.fetch(
                f"SELECT t.{column} AS person, COUNT(DISTINCT t.project_group) AS group_count, "
                "GROUP_CONCAT(DISTINCT t.project_group ORDER BY t.project_group) AS group_list, "
                "COUNT(*) AS task_count "
                f"FROM task t WHERE {named} AND t.project_group IS NOT NULL "
                f"GROUP BY t.{column} HAVING group_count > 1 "
                "ORDER BY group_count DESC, person",
                caliber=f"{base}；跨组人员，group_count 已按专项组去重；仅列跨 2 组以上者",
                limit=bounded,
            )

        if key == "dual_role":
            # 同一个人既牵头又当项目负责人。两个角色各自的计数都由服务端算。
            return store.fetch(
                "SELECT x.person, x.as_lead, x.as_project_owner FROM ("
                "SELECT t.lead_owner_name AS person, COUNT(*) AS as_lead, "
                "(SELECT COUNT(*) FROM task t2 "
                f"WHERE {clause.replace('t.', 't2.')} "
                "AND t2.project_owner_name = t.lead_owner_name) AS as_project_owner "
                f"FROM task t WHERE {clause} AND t.lead_owner_name IS NOT NULL "
                "GROUP BY t.lead_owner_name) x "
                "WHERE x.as_project_owner > 0 ORDER BY x.as_lead DESC, x.person",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；同时担任牵头人与项目负责人的人；"
                    "两个角色各自计数均按正式任务口径，别把两列相加"
                ),
                limit=bounded,
            )

        if key == "id_format":
            # 用户标识是异构的：纯数字工号、u 前缀、NDG 域账号。分档必须落在服务端，
            # 模型按返回行自己分类会把 128 行都算进去而不是有 ID 的那些。
            return store.fetch(
                "SELECT CASE WHEN t.owner_user_id REGEXP '^[0-9]+$' THEN '纯数字工号' "
                "WHEN t.owner_user_id LIKE 'u%%' THEN 'u 前缀账号' "
                "WHEN t.owner_user_id LIKE 'NDG%%' THEN 'NDG 域账号' ELSE '其他' END AS id_format, "
                "COUNT(*) AS task_count FROM task t "
                f"WHERE {clause} AND t.owner_user_id IS NOT NULL AND t.owner_user_id <> '' "
                "GROUP BY id_format ORDER BY task_count DESC, id_format",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；按 owner_user_id 的写法分档；"
                    "仅统计有标识的任务，空标识不进任何档，各档相加不等于任务总数"
                ),
                limit=bounded,
            )

        if key == "id_variants":
            # 「同一个人在不同任务里会不会是不同格式的标识」——空集就是答案，
            # 空集说明不存在，不能反过来说「会」。
            return store.fetch(
                f"SELECT t.{column} AS person, COUNT(DISTINCT t.{column.replace('_name', '_id')}) AS id_variants, "
                f"GROUP_CONCAT(DISTINCT t.{column.replace('_name', '_id')} "
                f"ORDER BY t.{column.replace('_name', '_id')}) AS ids "
                f"FROM task t WHERE {named} "
                f"GROUP BY t.{column} HAVING id_variants > 1 ORDER BY id_variants DESC, person",
                caliber=(f"{base}；同名多标识检查；返回 0 行即该口径下不存在这种人，不要据此说「会出现」"),
                limit=bounded,
            )

        if key in {"reporters", "reporter_count", "reviewers", "self_review"}:
            # 填报人/审核人在 task_progress 上而不在 task 上：任务侧 R-01 之外
            # 还有行级 is_published = 1 这道闸门，两道口径不能混。
            hist = f"FROM task_progress p JOIN task t ON t.id = p.task_id WHERE {clause}"
            gate = f"{store.FORMAL_TASK_CALIBER} 且 p.is_published = 1（任务闸门 + 进展行发布闸门）"

            if key == "reporters":
                return store.fetch(
                    "SELECT p.reporter_id, COUNT(*) AS reported_rounds, "
                    f"COUNT(DISTINCT p.task_id) AS tasks {hist} AND p.is_published = 1 "
                    "GROUP BY p.reporter_id ORDER BY reported_rounds DESC, p.reporter_id",
                    caliber=f"{gate}；按填报人分组，并列按 ID 定序",
                    limit=bounded,
                )
            if key == "reporter_count":
                return store.fetch(
                    f"SELECT COUNT(DISTINCT p.reporter_id) AS reporter_count {hist} AND p.is_published = 1",
                    caliber=f"{gate}；去重填报人数由服务端算，别数返回行",
                    limit=1,
                )
            if key == "reviewers":
                # 审核口径故意不加 is_published：审过但未发布的进展也是审过的。
                return store.fetch(
                    f"SELECT p.reviewer_id, COUNT(*) AS reviewed {hist} AND p.reviewer_id IS NOT NULL "
                    "GROUP BY p.reviewer_id ORDER BY reviewed DESC, p.reviewer_id",
                    caliber=(
                        f"{store.FORMAL_TASK_CALIBER} 且 p.reviewer_id 非空；"
                        "审核口径不加 p.is_published：审过但未发布的进展同样算审过"
                    ),
                    limit=bounded,
                )
            return store.fetch(
                "SELECT t.task_name, p.version_no, p.reporter_id, p.reviewer_id "
                f"{hist} AND p.reviewer_id IS NOT NULL AND p.reporter_id = p.reviewer_id "
                "ORDER BY t.id, p.version_no",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER} 且填报人与审核人为同一 ID；"
                    "按 ID 相等判定，不按姓名；此清单即全部自审记录"
                ),
                limit=bounded,
            )

        # 问的是「标识」而不是「任务」：同一个标识挂 3 个任务只算一个标识。
        # 不去重会返回 128 行、同一个 NDG\emp529 重复三次，模型会把「最长的是
        # 哪一个」答成一串重复项，也数不出到底有几个并列。
        return store.fetch(
            "SELECT t.owner_user_id, CHAR_LENGTH(t.owner_user_id) AS id_length, "
            "COUNT(*) AS task_count "
            f"FROM task t WHERE {clause} AND t.owner_user_id IS NOT NULL AND t.owner_user_id <> '' "
            "GROUP BY t.owner_user_id ORDER BY id_length DESC, t.owner_user_id",
            caliber=(
                f"{store.FORMAL_TASK_CALIBER}；一行一个去重后的标识，task_count 是该标识挂了几个任务；"
                "按标识字符长度倒序；长度相同的多个标识属并列，取「最长」时按并列陈述"
            ),
            limit=bounded,
        )

    return _guard("weekly_person_stats", work)


# Fields whose fill-in rate can be asked about (R-07 / R-19). Whitelisted rather
# than interpolated from the argument: the column name reaches SQL as an
# identifier, which no placeholder can bind.
_COMPLETENESS_FIELDS: dict[str, tuple[str, str]] = {
    "overall_goal": ("task", "总体目标"),
    "annual_goals": ("task", "年度目标"),
    "project_owner_name": ("task", "项目负责人"),
    "lead_owner_name": ("task", "分管领导"),
    "project_group": ("task", "项目组"),
    # 姓名列和 ID 列的完整度不是一回事：project_owner_name 128 条全满，
    # project_owner_id 只有 119 条，缺的那 9 条只能从 ID 列看出来。
    "owner_user_id": ("task", "责任人 ID"),
    "project_owner_id": ("task", "项目负责人 ID"),
    "lead_owner_id": ("task", "分管领导 ID"),
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

# 进展覆盖面的四个口径。unpublished / version_gaps 都是「让模型自己数会数错」
# 的那类：一个要辨两套状态码值，一个要拿最大期号减实际期数。
# unpublished_by_task 再往下一层，落到「哪些任务挂着未发布进展」的逐任务清单。
_COVERAGE_SCOPES = (
    "summary",
    "unpublished",
    "unpublished_by_task",
    "never_reported",
    "version_gaps",
    "latest_round",
    "missing_next",
)

# 「最新一期」的定序键：version_no 大者为新，同期再按 id 兜底，两级都要。
# 只按 progress_date 或只按 id 取最新都会取错行（latest_by_wrong_key）：
# 期号与日期并非单调同向，补报的老期号可能有更晚的 progress_date。
_LATEST_PROGRESS_CTE = (
    "(SELECT p.*, ROW_NUMBER() OVER (PARTITION BY p.task_id "
    "ORDER BY p.version_no DESC, p.id DESC) AS rn "
    "FROM task_progress p WHERE p.is_published = 1)"
)

# 动作日志的聚合口径。日志 1578 条而清单封顶 200 行，所以「各节点各动作多少条」
# 和「人均多少次动作」都必须服务端算完再回，模型翻明细自己数必然只数到第一页。
_WORKFLOW_SCOPES = ("", "by_node_action", "actions_per_task", "recent")

# 提交单的附加口径。空串走通用清单；两个 external 口径专门回答 O2OA 外部标识
# （o2_process_id / o2_work_id / o2_task_id）填得全不全，这三列此前没有任何
# 工具能取到，模型只能答「取不到」。
_SUBMISSION_SCOPES = (
    "",
    "by_kind",
    "inflight_count",
    "inflight_by_board",
    "inflight_multi",
    "sign_summary",
    "by_signer",
    "sign_turnaround",
    "rounds_per_task",
    "published_vs_progress",
    "external_ids",
    "inflight_external",
)

# 「在途」的成员必须列举，不能写成 status <> 'published'：cancelled 那 1 张单
# 既不是已发布也不在途，用取反会把它算进来（60 vs 59，negation_includes_cancelled）。
_SUBMISSION_INFLIGHT = ("pending_fill", "signing", "pending_audit", "pending_leader", "rejected")

# 规模/完整度/进展强度三种横截面。共用一套分组轴，区别只在度量：
#   totals       —— 任务数与各子表条数（全部 COUNT(DISTINCT)，见下）
#   completeness —— 有目标/有里程碑/有进展的任务数（SUM(EXISTS ...)）
#   intensity    —— 进展行数与人均期数（分母是任务数，不是行数）
_SCALE_MODES = ("totals", "completeness", "intensity")

# 规模横截面的分组轴。值是 (分组表达式, 分组定序键, 额外 JOIN)。
_SCALE_AXES: dict[str, tuple[str, str, str]] = {
    # 排序列写成 MIN(b.sort_order)：这里按 b.name 分组，裸 b.sort_order 既不在
    # GROUP BY 里也不是聚合，only_full_group_by 会直接报 1055。
    "board": ("b.name", "MIN(b.sort_order)", "JOIN task_board b ON b.id = t.board_id AND b.is_deleted = 0"),
    "project_group": (
        "IFNULL(NULLIF(TRIM(t.project_group),''),'(未填)')",
        "tasks DESC, bucket",
        "",
    ),
    "primary_category": (
        "pc.name",
        "tasks DESC, bucket",
        "JOIN task_category c ON c.id = t.category_id AND c.is_deleted = 0 "
        "JOIN task_category pc ON pc.id = c.parent_id AND pc.is_deleted = 0",
    ),
}

# 排名口径的三种语义。哪一种都不能由模型按返回行自己拿主意：
#   cut       ——「前 N 条」，并列也要按定序硬切到 N 条；
#   keep_ties ——「并列的都要列出来」，第 N 名有几个并列就返回几行；
#   per_group ——「每组各自的第一名」，一组一行，组内并列按定序取第一。
# 同一份数据在三种语义下行数不同（进展期数前 3 名：cut=3、keep_ties=12），
# 让模型自己在 200 行明细上判断，多返回和少返回都会被判集合不一致。
_RANK_MODES = ("cut", "keep_ties", "per_group")

# 可排名的度量。每项给出：子表、子表自身闸门、计数表达式、中文标签。
# 全部走 LEFT JOIN，零值行才不会被 INNER JOIN 静默丢掉（inner_join_drops_zero）。
_RANK_METRICS: dict[str, tuple[str, str, str, str]] = {
    "progress_rounds": ("task_progress", "x.is_published = 1", "COUNT(x.id)", "已发布进展期数"),
    "milestones": ("task_milestone", "x.is_deleted = 0", "COUNT(x.id)", "里程碑数"),
    "milestones_done": ("task_milestone", "x.is_deleted = 0", "SUM(x.status = 1)", "已完成里程碑数"),
    "attachments": ("task_attachment", "x.is_deleted = 0", "COUNT(x.id)", "附件数"),
    "submissions": ("task_workflow_submission", "1 = 1", "COUNT(x.id)", "审批提交单数"),
    "group_rounds": ("task_group_progress_history", "x.is_published = 1", "COUNT(x.id)", "集团看板成效期数"),
}

# per_group 的分组轴。键是对外口径名，值是 (分组表达式, 分组定序键)。
# 定序键进 ROW_NUMBER 的 ORDER BY 尾部，保证组内并列的裁决可复现。
_RANK_GROUPINGS: dict[str, tuple[str, str]] = {
    "project_group": ("t.project_group", "t.project_group"),
    "board": ("b.name", "b.sort_order"),
    "primary_category": ("pc.name", "pc.id"),
    "status": ("t.status", "t.status"),
}

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
    "completion_time_values",
    "completion_time_formats",
    "field_lengths",
    "attachments",
    "history_rounds",
    "separators",
    "owner_widths",
    "effect_consistency",
)

# 完成时间的「写法」分档。判别顺序即优先级，不能重排：'2026年6月底' 同时命中
# 「含底」与「中文年月」，先判到哪档就算哪档，调换后两档会把 11 拆成 6+5。
# 标准日期与季度用 REGEXP 锚定全串，避免 '2026Q3前' 之类被算成规范写法。
_COMPLETION_TIME_FORMAT_CASE = (
    "CASE "
    "WHEN d.completion_time REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN '标准日期 YYYY-MM-DD' "
    "WHEN d.completion_time REGEXP '^[0-9]{4}Q[1-4]$' THEN '季度 YYYYQn' "
    "WHEN d.completion_time LIKE '%%底%%' THEN '模糊表述（含“底”）' "
    "WHEN d.completion_time LIKE '%%年%%月%%日%%' THEN '中文年月日' "
    "WHEN d.completion_time LIKE '%%年%%月%%' THEN '中文年月' "
    "ELSE '其他' END"
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
    # 滞报榜按任务给最后一次上报距快照日的天数。放进分组白名单而不是单独开
    # 工具：问的仍是这张历史表的分面，只是聚合出的是 lag 而非条数。
    "lag": ("t.id", "lag_days DESC, bucket"),
    # 与提交单的挂接率。同样是这张表的分面，聚合出的是填充数而非条数。
    "linkage": ("t.id", "bucket"),
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
def weekly_field_completeness(field: str = "", list_missing: bool = False, limit: int = 200) -> str:
    """Count how many formal tasks have a given field filled in (R-07 / R-19).

    Answers "how many tasks have an overall goal / a named project owner" with one
    call. Without this the only route is fetching every task and counting by hand,
    which burns the tool-call budget and tends to run out mid-answer.

    Args:
        field: Column to measure; empty lists the supported columns.
        list_missing: Return the rows that are missing the field, not just counts.
        limit: Row cap for ``list_missing``.
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

        if list_missing:
            # 「哪些任务没填」需要的是清单，不是占比。计数问不出是哪 9 条。
            # LEFT JOIN 保留无明细行的任务，它们也算缺项（R-08）。
            join = "" if table == "task" else f"LEFT JOIN {table} d ON d.task_id = t.id "
            col = f"t.{token}" if table == "task" else f"d.{token}"
            gap = f"({col} IS NULL OR {col} = '')"
            missing_rows = store.fetch(
                "SELECT t.id, t.task_name, t.owner_user_id, t.project_owner_id, "
                "t.project_owner_name, t.lead_owner_name "
                f"FROM task t {join}WHERE {clause} AND {gap} ORDER BY t.id",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；列出「{label}」为空的正式任务（R-07/R-19）；"
                    "空字符串按未填计入；此清单即全部缺项，按 total_count 逐条列全"
                ),
                limit=limit,
            )
            total = store.scalar(f"SELECT COUNT(*) AS n FROM task t {join}WHERE {clause} AND {gap}")
            missing_rows["total_count"] = total.get("value")
            missing_rows["field"] = token
            missing_rows["field_label"] = label
            return missing_rows

        # 完整率必须由服务端算：问「完整率是多少」时模型手算 filled / total 会
        # 连百分号带小数位一起自己拿主意，答出 100% 这种与 119/128 相矛盾的数。
        if table == "task":
            expr = f"t.{token} IS NOT NULL AND t.{token} <> ''"
            sql = (
                f"SELECT COUNT(*) AS total, SUM({expr}) AS filled, "
                f"SUM(t.{token} IS NULL OR t.{token} = '') AS missing, "
                f"ROUND(SUM({expr}) / COUNT(*) * 100, 1) AS filled_pct "
                f"FROM task t WHERE {clause}"
            )
        else:
            # LEFT JOIN so tasks with no detail row count as missing, not vanish (R-08).
            expr = f"d.{token} IS NOT NULL AND d.{token} <> ''"
            sql = (
                f"SELECT COUNT(*) AS total, SUM({expr}) AS filled, "
                f"SUM(d.{token} IS NULL OR d.{token} = '') AS missing, "
                f"ROUND(SUM({expr}) / COUNT(*) * 100, 1) AS filled_pct "
                f"FROM task t LEFT JOIN {table} d ON d.task_id = t.id WHERE {clause}"
            )
        result = store.fetch(
            sql,
            caliber=(
                f"{store.FORMAL_TASK_CALIBER}；统计「{label}」非空占比（R-07/R-19）；"
                "空字符串按未填计入 missing；"
                "filled_pct 已按 total 算好（保留一位小数），直接引用，不要自己拿 filled / total 重算"
                + ("；LEFT JOIN 保留无明细行的任务（R-08）" if table != "task" else "")
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
    peak: bool = False,
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
        peak: True returns only the highest-count bucket. The ordering and the
            tie-break run server-side, so the single row is the answer.
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
        group_order = order
        note = f"；按 {grouping} 分组计数"
        tail = ""
        if peak:
            # 「哪个月最高」的裁决落在服务端。按 bucket 时序返回 19 行让模型自己
            # 挑最大值，它会把 2026-02(61) 和名次靠后的月份看成并列。
            group_order = "progress_count DESC, bucket"
            # LIMIT 落在 SQL 里而不是靠 limit 参数截断：靠截断会带出
            # has_more = True，看着像「还有行没给」，与「首行即答案」相冲。
            tail = " LIMIT 1"
            note += "；已按计数降序、并列取 bucket 升序，首行即峰值，勿另行比较"
        return store.fetch(
            f"SELECT {select} FROM task_progress p JOIN task t ON t.id = p.task_id "
            f"WHERE {where} GROUP BY bucket ORDER BY {group_order}{tail}",
            params,
            caliber=caliber + note,
            limit=limit,
        )

    return _guard("weekly_progress_range", work)


@mcp.tool()
def weekly_progress_coverage(scope: str = "summary", project_group: str = "", limit: int = 200) -> str:
    """Summarise how far back published progress goes and how much it covers.

    Args:
        scope: ``summary`` row count, tasks covered, date span, max version_no.
            ``unpublished`` splits unpublished progress by its OWN approval code
            (0 草稿 / 1 待审核 / 2 驳回 / 3 通过) -- a different vocabulary from the
            task's workflow_status. ``unpublished_by_task`` lists the tasks that
            hold unpublished periods while their submission is already published,
            most periods first. ``never_reported`` lists the 55 formal tasks with
            no published row in task_progress at all -- judged by NOT EXISTS, not
            by ``latest_progress_time IS NULL``, which only finds 9 of them.
            ``version_gaps`` tasks missing a period.
            ``latest_round`` gives each task's NEWEST published period with its
            ``next_work`` -- use it for "下一步打算做什么", never the full history
            (a task with 19 periods would otherwise contribute 19 rows and its
            oldest plan reads as current). ``missing_next`` counts tasks whose
            newest period left 下一步 blank.
        project_group: Narrow to one 项目组, for "算力网络组各任务下一步做什么".
        limit: Max rows for the listing scopes, capped at 200.
    """

    def work() -> dict[str, Any]:
        key = (scope or "summary").strip().lower()
        if key not in _COVERAGE_SCOPES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_scope",
                    "message": f"不支持的口径：{scope}；支持 {', '.join(_COVERAGE_SCOPES)}",
                },
            }
        bounded = max(1, min(store.MAX_ROWS, int(limit)))
        clause = store.formal_task_clause()

        if key in ("latest_round", "missing_next"):
            params: dict[str, Any] = {}
            where = [clause]
            caliber = [
                store.FORMAL_TASK_CALIBER,
                "每任务只取最新一期已发布进展（version_no 倒序、同期按 id 兜底）；"
                "不是全部历史，也不是按 progress_date 取最新（补报的老期号可能日期更晚）",
            ]
            if project_group.strip():
                params["pg"] = project_group.strip()
                where.append("TRIM(t.project_group) = %(pg)s")
                caliber.append(f"仅项目组 = {project_group.strip()}")
            latest_where = " AND ".join(where)

            if key == "missing_next":
                return store.fetch(
                    "SELECT COUNT(*) AS tasks_missing_next "
                    f"FROM task t JOIN {_LATEST_PROGRESS_CTE} p ON p.task_id = t.id AND p.rn = 1 "
                    f"WHERE {latest_where} AND (p.next_work IS NULL OR p.next_work = '')",
                    params,
                    caliber="；".join([*caliber, "只看最新一期是否留空，中间某期空着不算"]),
                    limit=1,
                )

            total = store.scalar(
                f"SELECT COUNT(*) FROM task t JOIN {_LATEST_PROGRESS_CTE} p ON p.task_id = t.id AND p.rn = 1 "
                f"WHERE {latest_where} AND p.next_work IS NOT NULL AND p.next_work <> ''",
                params,
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.project_group, p.version_no, "
                "p.next_work, p.progress_date "
                f"FROM task t JOIN {_LATEST_PROGRESS_CTE} p ON p.task_id = t.id AND p.rn = 1 "
                f"WHERE {latest_where} AND p.next_work IS NOT NULL AND p.next_work <> '' "
                "ORDER BY t.id",
                params,
                caliber="；".join(
                    [
                        *caliber,
                        "仅列出最新一期写了下一步的任务；一任务一行，total_count 即任务数",
                        "下一步为空的任务数另用 scope=missing_next",
                    ]
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "unpublished":
            # 未发布进展自己带一套审批状态码值（0 草稿 / 1 待审核 / 2 驳回 /
            # 3 通过），和任务的 workflow_status 不是一回事。分档必须在服务端做，
            # 否则模型会拿 pending_audit 这类任务侧的词来套。
            total = store.scalar(
                "SELECT COUNT(*) FROM task_progress p JOIN task t ON t.id = p.task_id "
                f"WHERE {clause} AND p.is_published = 0",
            )
            rows = store.fetch(
                "SELECT p.status, CASE p.status WHEN 0 THEN '草稿' WHEN 1 THEN '待审核' "
                "WHEN 2 THEN '驳回' WHEN 3 THEN '通过' ELSE '未知' END AS status_label, "
                "COUNT(*) AS cnt FROM task_progress p JOIN task t ON t.id = p.task_id "
                f"WHERE {clause} AND p.is_published = 0 GROUP BY p.status ORDER BY p.status",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；仅未发布进展（p.is_published = 0）；"
                    "status 是进展行自己的审批码值 0 草稿 / 1 待审核 / 2 驳回 / 3 通过，"
                    "不要拿任务的 workflow_status（published、pending_audit 等）来套；"
                    "各档相加等于 total_count"
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "unpublished_by_task":
            # 期数按 version_no 去重：一期可能有多行，COUNT(*) 会把「几期」答成
            # 「几行」（join_fanout_row_inflation）。任务侧只加 is_deleted = 0——
            # 提交单已发布是本题的筛选条件，不是任务的发布闸门。
            total = store.scalar(
                "SELECT COUNT(*) FROM (SELECT t.id FROM task t "
                "JOIN task_workflow_submission s ON s.task_id = t.id AND s.status = 'published' "
                "JOIN task_progress p ON p.task_id = t.id AND p.is_published = 0 "
                "WHERE t.is_deleted = 0 GROUP BY t.id) x",
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, "
                "COUNT(DISTINCT p.version_no) AS unpublished_rounds "
                "FROM task t "
                "JOIN task_workflow_submission s ON s.task_id = t.id AND s.status = 'published' "
                "JOIN task_progress p ON p.task_id = t.id AND p.is_published = 0 "
                "WHERE t.is_deleted = 0 GROUP BY t.id, t.task_name "
                "ORDER BY unpublished_rounds DESC, t.id",
                caliber=(
                    "仅 t.is_deleted = 0；提交单 status = 'published' 且进展行 is_published = 0"
                    "（提交单已发布、进展还挂着未发布，两套码值各判一次）；"
                    "unpublished_rounds 按 version_no 去重，是「期数」不是「行数」；"
                    "并列按 task id 升序，total_count 为符合条件的任务总数"
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "never_reported":
            # 「还有多少条正式任务从来没报过进展」：判据是 task_progress 里有没有
            # 已发布行，走 NOT EXISTS。不能用 t.latest_progress_time IS NULL 代替
            # ——那一列只跟 task_progress 的写入同步，集团看板的 46 条任务成效写在
            # task_group_progress_history，它们 latest_progress_time 有值却在
            # task_progress 里一行都没有，按空值判会答成 9（少 46 条）。
            # 55 = 128 - 73，与 summary 的 tasks_covered 同一套「进展」定义。
            total = store.scalar(
                f"SELECT COUNT(*) FROM task t WHERE {clause} AND NOT EXISTS "
                "(SELECT 1 FROM task_progress p WHERE p.task_id = t.id AND p.is_published = 1)",
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, b.name AS board_name, t.project_group, "
                "EXISTS (SELECT 1 FROM task_group_progress_history h "
                "WHERE h.task_id = t.id AND h.is_published = 1) AS has_group_history "
                f"FROM task t LEFT JOIN task_board b ON b.id = t.board_id WHERE {clause} "
                "AND NOT EXISTS (SELECT 1 FROM task_progress p "
                "WHERE p.task_id = t.id AND p.is_published = 1) "
                "ORDER BY t.id",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；「没报过进展」= task_progress 里没有已发布行"
                    "（NOT EXISTS），total_count 55 = 正式任务 128 - 有进展的 73，"
                    "与 scope=summary 的 tasks_covered 同一套进展定义；"
                    "不要用 latest_progress_time 是否为空来判——那样只得 9 条，"
                    "会漏掉集团看板的 46 条（它们成效写在 task_group_progress_history，"
                    "该列有值但 task_progress 里一行都没有）；"
                    "has_group_history = 1 即该任务在集团历史表里报过，"
                    "真正两张表都没报过的是 9 条（技术组）"
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "version_gaps":
            # 缺号 = 最大期号 - 实际期数。按已发布口径算，未发布的那几期本就
            # 不该占号；HAVING 而非 WHERE，因为判据是聚合结果。
            return store.fetch(
                "SELECT t.id AS task_id, t.task_name, COUNT(*) AS rounds, "
                "MAX(p.version_no) AS max_version, MAX(p.version_no) - COUNT(*) AS missing_count "
                "FROM task_progress p JOIN task t ON t.id = p.task_id "
                f"WHERE {clause} AND p.is_published = 1 "
                "GROUP BY t.id, t.task_name HAVING missing_count <> 0 "
                "ORDER BY missing_count DESC, t.id",
                caliber=(
                    f"{store.FORMAL_TASK_CALIBER}；仅已发布进展；"
                    "missing_count = 最大期号 - 实际期数，非 0 即中间缺号；"
                    "并列按 task id 升序，行数即缺号任务总数"
                ),
                limit=bounded,
            )

        return store.fetch(
            "SELECT COUNT(*) AS progress_rows, COUNT(DISTINCT p.task_id) AS tasks_covered, "
            "MIN(p.progress_date) AS earliest, MAX(p.progress_date) AS latest, "
            "MAX(p.version_no) AS max_version "
            "FROM task_progress p JOIN task t ON t.id = p.task_id "
            f"WHERE {clause} AND p.is_published = 1",
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
def weekly_rank(
    metric: str = "progress_rounds",
    mode: str = "cut",
    top: int = 5,
    ascending: bool = False,
    group_by: str = "",
    board: str = "",
) -> str:
    """Rank formal tasks with the tie rule decided server-side (cut / keep_ties / per_group).

    前 N 条 / 并列全列 / 每组第一 are three different SETS over one metric, and
    their row counts differ. The rule is applied in SQL and stated in caliber, so
    the caller reports the rows as returned rather than padding or trimming them.

    Args:
        metric: progress_rounds / milestones / milestones_done / attachments /
            submissions / group_rounds.
        mode: ``cut`` hard-cuts to top rows, ties ordered by task id. ``keep_ties``
            uses RANK() and keeps every task down to place top. ``per_group``
            takes the first place within each bucket, one row per group.
        top: How many rows for cut, or which place for keep_ties. 1..200.
            per_group ignores it -- the group count IS the row count.
        ascending: True ranks from the bottom. Zero-value rows are kept.
        group_by: Required for per_group: project_group / board /
            primary_category / status.
        board: Board code or name to scope the ranking to. Needed for "每个任务各有
            几个附件" on one board -- without it the listing spans both boards and
            the row count answers a different question.
    """

    def work() -> dict[str, Any]:
        chosen = _RANK_METRICS.get((metric or "").strip())
        if chosen is None:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_metric",
                    "message": f"metric 支持 {', '.join(sorted(_RANK_METRICS))}",
                },
            }
        key = (mode or "cut").strip().lower()
        if key not in _RANK_MODES:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_mode",
                    "message": f"mode 支持 {', '.join(_RANK_MODES)}",
                },
            }
        try:
            bounded = max(1, min(store.MAX_ROWS, int(top)))
        except TypeError, ValueError:
            return {"ok": False, "error": {"code": "invalid_argument", "message": "top 必须是整数"}}

        table, gate, expression, label = chosen
        direction = "ASC" if ascending else "DESC"
        # LEFT JOIN 而非 JOIN：期数为 0 的任务是「最少」那一端的正确答案，
        # INNER JOIN 会把它们整行丢掉（inner_join_drops_zero）。
        join = f"LEFT JOIN {table} x ON x.task_id = t.id AND {gate}"
        clause = store.formal_task_clause()
        base = f"{store.FORMAL_TASK_CALIBER}；按{label}{'升序' if ascending else '降序'}"
        params: dict[str, Any] = {}
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            clause += " AND t.board_id = %(bid)s"
            params["bid"] = board_id
            base += f"；仅看板 {board.strip()}"

        if key == "per_group":
            grouping = _RANK_GROUPINGS.get((group_by or "").strip())
            if grouping is None:
                return {
                    "ok": False,
                    "error": {
                        "code": "unsupported_group_by",
                        "message": f"per_group 需要 group_by，支持 {', '.join(sorted(_RANK_GROUPINGS))}",
                    },
                }
            axis, order_key = grouping
            extra = ""
            if group_by.strip() == "board":
                extra = "JOIN task_board b ON b.id = t.board_id AND b.is_deleted = 0"
            elif group_by.strip() == "primary_category":
                # 一级分类要经二级分类的 parent_id 上跳一层，任务本身只挂到二级。
                extra = (
                    "JOIN task_category c ON c.id = t.category_id AND c.is_deleted = 0 "
                    "JOIN task_category pc ON pc.id = c.parent_id AND pc.is_deleted = 0"
                )
            # top 在这一档没有意义：一组一行，行数由分组数决定。拿 top 当上限
            # 会切掉后面的组，而 caliber 又说「行数等于分组数」，两边对不上，
            # 模型照 caliber 答就答漏了。
            return store.fetch(
                f"SELECT bucket, task_id, task_name, metric_value FROM ("
                f"SELECT {axis} AS bucket, t.id AS task_id, t.task_name, {expression} AS metric_value, "
                f"ROW_NUMBER() OVER (PARTITION BY {order_key} "
                f"ORDER BY {expression} {direction}, t.id) AS rn "
                f"FROM task t {extra} {join} WHERE {clause} AND {axis} IS NOT NULL "
                f"GROUP BY {order_key}, {axis}, t.id, t.task_name) r "
                f"WHERE r.rn = 1 ORDER BY r.bucket",
                params,
                caliber=(
                    f"{base}；每个{group_by.strip()}只返回第一名（一组一行）；"
                    "组内并列按 task id 升序裁决，行数等于分组数，不要把并列的都列出来；"
                    "本档不受 top 影响"
                ),
                limit=store.MAX_ROWS,
            )

        if key == "keep_ties":
            # RANK() 而非 ROW_NUMBER()：问句明说要并列，第 N 名有几个就返回几个，
            # 行数因此可能远大于 top（进展期数前 3 名共 12 行）。
            return store.fetch(
                f"SELECT task_id, task_name, metric_value, rk FROM ("
                f"SELECT t.id AS task_id, t.task_name, {expression} AS metric_value, "
                f"RANK() OVER (ORDER BY {expression} {direction}) AS rk "
                f"FROM task t {join} WHERE {clause} GROUP BY t.id, t.task_name) r "
                f"WHERE r.rk <= %(n)s ORDER BY r.rk, r.task_name",
                {**params, "n": bounded},
                caliber=(
                    f"{base}；RANK() 保留并列，返回到第 {bounded} 名为止的全部任务；"
                    f"行数通常大于 {bounded}，rk 相同即并列，按 row_count 全部列出"
                ),
                limit=store.MAX_ROWS,
            )

        # total_count 是「符合口径的任务总数」，不是本次返回的行数。问「每个任务
        # 各有几个」时 top 只是页大小，答案的集合大小由 total_count 定；两者相等
        # 才说明列全了。
        total = store.scalar(
            f"SELECT COUNT(*) FROM task t WHERE {clause}",
            params,
        )
        result = store.fetch(
            f"SELECT t.id AS task_id, t.task_name, {expression} AS metric_value "
            f"FROM task t {join} WHERE {clause} "
            f"GROUP BY t.id, t.task_name ORDER BY metric_value {direction}, t.id "
            f"LIMIT {bounded}",
            params,
            caliber=(
                f"{base}；硬切前 {bounded} 条，并列按 task id 升序定序；边界外的并列任务不属于本题答案，不要补列；"
                "total_count 为该口径下任务总数，问「每个任务各多少」时应与 row_count 相等，不等即未列全"
            ),
            limit=bounded,
        )
        result["total_count"] = total["value"]
        return result

    return _guard("weekly_rank", work)


@mcp.tool()
def weekly_import_audit(limit: int = 200, reconcile_rows: bool = False, orphans: bool = False) -> str:
    """Reconcile Excel import batches: batch count vs distinct import times (R-09/R-10).

    Args:
        limit: Max rows, capped at 200.
        reconcile_rows: True adds the per-batch declared-vs-landed comparison
            (changed_tasks vs the progress rows actually carrying that import_id).
            Required for "对得上吗" -- the declared figure lives on the batch row
            and the landed figure only exists as a count over task_progress, so
            no listing of either table alone can answer it.
        orphans: True checks the other direction -- progress rows pointing at an
            import batch that no longer exists. "有没有挂在不存在的批次上" is a
            NOT EXISTS question; listing either table alone cannot answer it, and
            the honest answer here is zero, which is a finding, not a failure.
    """

    def work() -> dict[str, Any]:
        summary = store.fetch(
            "SELECT COUNT(*) AS batch_count, COUNT(DISTINCT data_date) AS distinct_dates, "
            "COUNT(DISTINCT import_time) AS distinct_import_times "
            "FROM task_progress_import",
            caliber="批次数 vs 去重业务快照日期数 vs 去重导入时间数（R-09/R-10）",
            limit=1,
        )
        if orphans:
            # NOT EXISTS 而非 LEFT JOIN ... IS NULL：这里只要判定存在性。
            # import_id IS NULL 是「没走导入」不是「挂错批次」，用 SUM 单列出来，
            # 不能混进孤儿数，否则手工填报的进展会全被报成孤儿。
            result = store.fetch(
                "SELECT SUM(p.import_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM task_progress_import i WHERE i.id = p.import_id)) AS orphan_rows, "
                "COUNT(DISTINCT CASE WHEN NOT EXISTS "
                "(SELECT 1 FROM task_progress_import i WHERE i.id = p.import_id) "
                "THEN p.import_id END) AS orphan_batch_ids, "
                "SUM(p.import_id IS NULL) AS rows_without_import "
                "FROM task_progress p",
                caliber=(
                    "孤儿定义：import_id 非空且 task_progress_import 里查不到该批次（NOT EXISTS）；"
                    "import_id IS NULL 是未经导入的手工填报，不算孤儿，另计为 rows_without_import；"
                    "orphan_rows = 0 即引用完整，这是结论本身，不要换口径重算"
                ),
                limit=1,
            )
            result["reconciliation"] = summary["rows"][0] if summary["rows"] else {}
            return result
        if reconcile_rows:
            # LEFT JOIN 不可换成 JOIN：一条进展都没落库的批次（第 20 批声明 43、
            # 实落 0）正是「对不上」的极端情形，INNER JOIN 会把它整行丢掉。
            # actual_tasks 与 actual_rows 分开给：声明的是任务数，落库的行数是
            # 另一回事，拿行数比声明会得出反向结论。
            mismatch = store.scalar(
                "SELECT COUNT(*) FROM (SELECT i.id, i.changed_tasks AS declared, "
                "COUNT(DISTINCT p.task_id) AS actual_tasks FROM task_progress_import i "
                "LEFT JOIN task_progress p ON p.import_id = i.id "
                "GROUP BY i.id, i.changed_tasks HAVING declared <> actual_tasks) x",
            )
            rows = store.fetch(
                "SELECT i.id, i.file_name, i.data_date, i.changed_tasks AS declared_tasks, "
                "COUNT(DISTINCT p.task_id) AS actual_tasks, COUNT(p.id) AS actual_rows, "
                "COUNT(DISTINCT p.task_id) - i.changed_tasks AS task_diff "
                "FROM task_progress_import i LEFT JOIN task_progress p ON p.import_id = i.id "
                "GROUP BY i.id, i.file_name, i.data_date, i.changed_tasks ORDER BY i.id DESC",
                caliber=(
                    "declared_tasks 取自批次行的 changed_tasks（声明值）；"
                    "actual_tasks / actual_rows 由 task_progress.import_id 反查得出，"
                    "前者去重任务数、后者进展行数，二者与声明值是三个不同口径；"
                    "LEFT JOIN 保留零落库批次（否则最极端的对不上那批会消失）；"
                    "mismatched_batches 为声明与实际任务数不等的批次数，勿自行比对"
                ),
                limit=limit,
            )
            rows["reconciliation"] = summary["rows"][0] if summary["rows"] else {}
            rows["mismatched_batches"] = mismatch["value"]
            return rows
        rows = store.fetch(
            "SELECT id, file_name, data_date, import_time, total_tasks, changed_tasks, status "
            "FROM task_progress_import ORDER BY data_date DESC, id DESC",
            caliber=(
                "data_date 为业务快照日期；"
                "changed_tasks 是批次自己声明的数字，未与实际落库行核对，"
                "要核对请用 reconcile_rows=True"
            ),
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
def weekly_freshness_distribution(
    task: str = "",
    within_days: int = 0,
    drift: bool = False,
    stale_days: int = 0,
    recent_days: int = 0,
    by: str = "",
    limit: int = 200,
) -> str:
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
        stale_days: When > 0, lists 在办任务 (status 0 and 1 both count) whose
            newest progress is older than that many days. Never reported (NULL)
            counts as stale and sorts first.
        recent_days: When > 0, lists tasks that DID report within that window,
            newest first. 与 stale_days 是相反的一端。
        by: ``project_group`` 让 stale_days 返回各组滞后计数而不是清单。
        limit: Max rows for the listing branches, capped at 200.
    """

    def work() -> dict[str, Any]:
        if stale_days or recent_days:
            days = int(stale_days or recent_days)
            if days <= 0:
                return {
                    "ok": False,
                    "error": {"code": "invalid_argument", "message": "天数必须为正整数"},
                }
            bounded = max(1, min(store.MAX_ROWS, int(limit)))
            if recent_days:
                # 「最近一周更新过」不加 status 过滤：问的是有没有报进展，
                # 不是任务在不在办。
                return store.fetch(
                    "SELECT t.id, t.task_name, t.status, t.latest_progress_time, "
                    "DATEDIFF(%(as_of)s, t.latest_progress_time) AS days_since "
                    f"FROM task t WHERE {store.formal_task_clause()} "
                    "AND t.latest_progress_time >= DATE_SUB(%(as_of)s, INTERVAL %(d)s DAY) "
                    "ORDER BY t.latest_progress_time DESC, t.id",
                    {"as_of": store.AS_OF, "d": days},
                    caliber=(
                        f"{store.FORMAL_TASK_CALIBER}；仅 latest_progress_time 落在近 {days} 天内；"
                        f"{store.as_of_caliber()}；不加 status 过滤（问的是有无上报，不是是否在办）；"
                        "按上报时间倒序"
                    ),
                    limit=bounded,
                )
            # 「在办」= status IN (0, 1)：0 未开始也在办，只取 1 会漏行
            # （negation_includes_cancelled）。NULL 即从未上报，算滞后且排最前
            # ——它没有天数可算，days_since 返回 NULL 是如实表达而非缺失。
            in_progress = "t.status IN (0, 1)"
            stale = (
                "(t.latest_progress_time IS NULL OR t.latest_progress_time < DATE_SUB(%(as_of)s, INTERVAL %(d)s DAY))"
            )
            params = {"as_of": store.AS_OF, "d": days}
            note = (
                f"{store.FORMAL_TASK_CALIBER}；在办即 status IN (0, 1)（0 未开始同样在办）；"
                f"滞后超过 {days} 天，含从未上报（latest_progress_time 为 NULL）；"
                f"{store.as_of_caliber()}"
            )
            if (by or "").strip() == "project_group":
                return store.fetch(
                    "SELECT t.project_group, COUNT(*) AS stale_count "
                    f"FROM task t WHERE {store.formal_task_clause()} AND {in_progress} AND {stale} "
                    "GROUP BY t.project_group ORDER BY stale_count DESC, t.project_group",
                    params,
                    caliber=note + "；按专项组计数，各组相加等于清单总数",
                    limit=bounded,
                )
            if (by or "").strip():
                return {
                    "ok": False,
                    "error": {
                        "code": "unsupported_group_by",
                        "message": "by 目前只支持 project_group",
                    },
                }
            total = store.scalar(
                f"SELECT COUNT(*) FROM task t WHERE {store.formal_task_clause()} AND {in_progress} AND {stale}",
                params,
            )
            rows = store.fetch(
                "SELECT t.id, t.task_name, t.status, t.latest_progress_time, "
                "DATEDIFF(%(as_of)s, t.latest_progress_time) AS days_since "
                f"FROM task t WHERE {store.formal_task_clause()} AND {in_progress} AND {stale} "
                "ORDER BY t.latest_progress_time IS NOT NULL, t.latest_progress_time, t.id",
                params,
                caliber=note + "；从未上报的排在最前，其 days_since 为空",
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows
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
    in_progress_only: bool = False,
    board: str = "",
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
        in_progress_only: True keeps only 在办任务 (status IN (0, 1)). Required for
            "在办任务还没定目标" -- otherwise 已完成 / 已暂停 tasks inflate the gap list.
        board: Board code or name to scope every scope to. "集团看板哪些任务没设目标"
            needs it: without it the gap list spans both boards and is a different set.
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
        board_params: dict[str, Any] = {}
        if board.strip():
            board_id = store.resolve_board(board)
            if board_id is None:
                return {"ok": False, "error": {"code": "board_not_found", "message": f"未匹配到看板：{board}"}}
            clause += " AND t.board_id = %(bid)s"
            board_params["bid"] = board_id
            base += f"；仅看板 {board.strip()}"

        if key == "by_year":
            return store.fetch(
                "SELECT g.year, COUNT(*) AS goal_count, COUNT(DISTINCT g.task_id) AS task_count "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY g.year ORDER BY g.year",
                board_params,
                caliber=f"{base}；按年度统计目标条数与涉及任务数",
                limit=bounded,
            )

        if key == "span":
            threshold = max(1, int(min_years))
            avg = store.scalar(
                "SELECT ROUND(AVG(yr_cnt), 2) AS avg_years FROM (SELECT COUNT(*) AS yr_cnt "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY g.task_id) x",
                board_params,
                caliber=f"{base}；分母只含已设过目标的任务",
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, COUNT(*) AS year_count, "
                "GROUP_CONCAT(g.year ORDER BY g.year) AS years "
                f"FROM task_year_goal g JOIN task t ON t.id = g.task_id WHERE {clause} "
                "GROUP BY t.id, t.task_name HAVING year_count >= %(n)s "
                "ORDER BY year_count DESC, t.id",
                {**board_params, "n": threshold},
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
                {**board_params, "yr": int(year)},
                caliber=f"{base}；分母为全部正式任务，未设目标的任务计入缺口（不能用 JOIN 丢掉）",
                limit=1,
            )

        if key == "missing":
            # 「在办任务还没定目标」问的是在办那批，已完成/已暂停的没定目标不算缺口。
            # 不加这道过滤会把 status 2/3 的行混进来，集合就对不上了。
            extra = " AND t.status IN (0, 1)" if in_progress_only else ""
            note = (
                "；仅在办任务（status IN (0, 1)，0 未开始同样在办）"
                if in_progress_only
                else "；含全部状态，未按在办过滤"
            )
            total = store.scalar(
                f"SELECT COUNT(*) FROM task t WHERE {clause}{extra} "
                "AND NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s)",
                {**board_params, "yr": int(year)},
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, t.status, t.project_group "
                f"FROM task t WHERE {clause}{extra} AND NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s) ORDER BY t.id",
                {**board_params, "yr": int(year)},
                caliber=f"{base}；{int(year)} 年度无目标行{note}；status 0 未开始 / 1 进行中 / 2 已完成 / 3 已暂停",
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "missing_by_group":
            return store.fetch(
                "SELECT t.project_group, COUNT(*) AS missing_count "
                f"FROM task t WHERE {clause} AND NOT EXISTS (SELECT 1 FROM task_year_goal g "
                "WHERE g.task_id = t.id AND g.year = %(yr)s) "
                "GROUP BY t.project_group ORDER BY missing_count DESC, t.project_group",
                {**board_params, "yr": int(year)},
                caliber=f"{base}；按专项组统计 {int(year)} 年度目标缺口",
                limit=bounded,
            )

        if not year_to:
            return {
                "ok": False,
                "error": {"code": "invalid_argument", "message": "口径 multi_year 需要 year 与 year_to"},
            }
        params = {**board_params, "yr1": int(year), "yr2": int(year_to)}
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
    status: str = "",
    non_empty: str = "",
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
        status: Business status 0/1/2/3 on the task side. Needed for "状态与成效
            描述矛盾的任务" -- the contradiction is 未开始 (0) next to a written
            effect, and the status lives on ``task`` while the effect lives here,
            so neither table alone can express it.
        non_empty: Comma-separated columns required to be non-empty. Pair with
            ``status`` for the contradiction question: without it, tasks that are
            未开始 *and* blank come along and inflate the count.
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

        if status.strip():
            if status.strip() not in {"0", "1", "2", "3"}:
                return {
                    "ok": False,
                    "error": {"code": "invalid_status", "message": "status 只能是 0/1/2/3"},
                }
            params["st"] = int(status.strip())
            where.append("t.status = %(st)s")
            caliber.append(
                f"任务业务状态 = {status.strip()}"
                "（0 未开始 / 1 进行中 / 2 已完成 / 3 已停用，与审批流转状态 workflow_status 不是一回事）"
            )

        required = [f.strip() for f in (non_empty or "").split(",") if f.strip()]
        bad = [f for f in required if f not in _GROUP_DETAIL_FIELDS]
        if bad:
            return {
                "ok": False,
                "error": {
                    "code": "unsupported_field",
                    "message": f"non_empty 不支持：{', '.join(bad)}；支持 {', '.join(sorted(_GROUP_DETAIL_FIELDS))}",
                },
            }
        for column in required:
            where.append(f"d.{column} IS NOT NULL AND d.{column} <> ''")
            caliber.append(f"{column} 非空")
            if column not in selected:
                selected.append(column)

        columns = ", ".join(f"d.{name}" for name in selected)
        if "completion_time" in selected:
            caliber.append("completion_time 为展示文本，不可做日期运算（R-12）")
        # 多值负责人栏一并回人数：问「有几位项目责任人」时，模型按逗号自己数
        # 会漏掉顿号那几行；两种分隔符都要扣。同时点明这一列与 task 上的单值
        # 同名列不是一个东西——集团看板 46 条任务两列的值并不一致。
        for name in ("lead_owner_names", "project_owner_names"):
            if name in selected:
                columns += (
                    f", CHAR_LENGTH(d.{name}) - CHAR_LENGTH(REPLACE(REPLACE(d.{name}, '、', ''), ',', '')) + 1 "
                    f"AS {name.replace('_names', '')}_count"
                )
                caliber.append(
                    f"{name} 的人数已算好（分隔符个数 + 1，顿号与逗号都计入），不要自己按逗号数；"
                    f"这一列是集团看板的多值口径，与 task 表上单值的 {name.replace('_names', '_name')} "
                    "并非同一个数据（46 条任务两列的值不一致），问集团看板的负责人一律用本列"
                )

        clause = " AND ".join(where)
        total = store.scalar(
            "SELECT COUNT(*) FROM task_group_detail d JOIN task t ON t.id = d.task_id "
            f"{store.group_board_join()} WHERE {clause}",
            params,
        )
        rows = store.fetch(
            f"SELECT d.task_id, t.task_name, t.status, {columns} "
            "FROM task_group_detail d JOIN task t ON t.id = d.task_id "
            f"{store.group_board_join()} "
            f"WHERE {clause} ORDER BY d.task_id",
            params,
            caliber="；".join([*caliber, "total_count 为符合条件的任务总数，判断列全看它而非返回行数"]),
            limit=limit,
        )
        rows["total_count"] = total["value"]
        return rows

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
    last_months: int = 0,
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
            ``reporter`` returns counts per group. ``lag`` ranks tasks by how
            many days since their last report -- use it for "谁最久没报".
            ``linkage`` counts how many rows carry a workflow_submission_id, over
            all 404 rows rather than the 362 published ones, since the question is
            a fill rate.
        latest_only: Keep only each task's newest published version.
        date_from: Inclusive start on ``report_time``, YYYY-MM-DD.
        date_to: Inclusive end on ``report_time``, YYYY-MM-DD.
        last_days: Window of N days ending at the snapshot date, not today.
        last_months: Window of N calendar months ending at the snapshot date.
            "最近三个月" means this, not ``last_days=90``: three months back from
            2026-08-15 is 2026-05-15, while 90 days lands on 2026-05-17 and drops
            three May rows. Do not substitute one for the other.
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

        if last_months and last_days:
            return {
                "ok": False,
                "error": {
                    "code": "invalid_argument",
                    "message": "last_days 与 last_months 只能给一个：两者边界不同，同时给会得出第三个窗口",
                },
            }
        if last_months:
            lo, hi = store.month_window(last_months)
            if date_from.strip():
                lo = date_from.strip()
            if date_to.strip():
                hi = date_to.strip()
        else:
            lo, hi = store.date_window(date_from, date_to, last_days or None)
        window = store.window_clause("h.report_time", lo, hi, params)
        if window:
            # report_time is a datetime and the bound end is a date, so a naive
            # `<=` would cut the final day off at 00:00. Compare on the date part.
            window = store.window_clause("DATE(h.report_time)", lo, hi, params)
            where.append(window)
            caliber.append(store.window_caliber(lo, hi, label="上报时间"))
            if last_days or last_months:
                caliber.append(store.as_of_caliber())
            if last_months:
                caliber.append(f"最近 {int(last_months)} 个月按自然月回溯（非 {int(last_months) * 30} 天）")

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

        if grouping == "lag":
            # 滞报天数取 MAX(report_time) 与快照日之差，不是 MIN：问的是「最后
            # 一次报到现在多久」，用最早一期会把老任务全排到榜首。
            # 分母 total_tasks 一并给出：本表只有报过的任务，从未报过的不在这张
            # 榜上，拿行数当「集团组任务数」会少算。
            total = store.scalar(
                f"SELECT COUNT(DISTINCT h.task_id) FROM task_group_progress_history h {joins} WHERE {clause}",
                params,
            )
            rows = store.fetch(
                "SELECT t.id AS task_id, t.task_name, "
                f"DATEDIFF('{store.AS_OF}', MAX(h.report_time)) AS lag_days, "
                "COUNT(*) AS rounds, MAX(h.report_time) AS last_report_time "
                f"FROM task_group_progress_history h {joins} WHERE {clause} "
                "GROUP BY t.id, t.task_name ORDER BY lag_days DESC, t.id",
                params,
                caliber="；".join(
                    [
                        *caliber,
                        f"lag_days = 快照日 {store.AS_OF} 减最后一次上报日（MAX(report_time)，不是最早一期）",
                        "只含报过进展的任务，从未报过的不在榜上；total_tasks 为上榜任务数，勿当作集团组任务总数",
                        "并列按 task id 升序",
                    ]
                ),
                limit=limit,
            )
            rows["total_tasks"] = total["value"]
            return rows

        if grouping == "linkage":
            # 这一档故意不加 is_published 闸门：问的是「有多少行挂上了提交单」，
            # 分母该是表里全部 404 行，用过闸的 362 行会把 42 条草稿的挂接状况
            # 一起丢掉。两个数都回，并写明哪个是哪个。
            bare = " AND ".join(w for w in where if "h.is_published" not in w)
            return store.fetch(
                "SELECT COUNT(*) AS total_rows, "
                "SUM(h.workflow_submission_id IS NOT NULL) AS linked_rows, "
                "SUM(h.workflow_submission_id IS NULL) AS unlinked_rows, "
                "SUM(h.is_published = 1) AS published_rows "
                f"FROM task_group_progress_history h {joins} WHERE {bare}",
                params,
                caliber=(
                    "任务侧 is_deleted = 0 AND workflow_status = 'published'，"
                    "但本档不加历史行的 is_published 闸门：问的是挂接率，"
                    "分母该是表内全部 404 行（过闸的 362 行会漏掉 42 条草稿的挂接状况）；"
                    "linked_rows 按 workflow_submission_id 非空判定，"
                    "0 即这张表整体没有与提交单挂接，不是查不到——"
                    "集团组的成效历史独立于审批提交单，两者没有外键落库"
                ),
                limit=1,
            )

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
            ``separators`` (how project_owner_names is delimited, single-person
            cells counted as their own bucket),
            ``owner_widths`` (people per project_owner_names cell, widest first),
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

        if key == "separators":
            # 多值负责人栏的分隔符是混着填的：半角逗号、顿号、两者并存、以及
            # 只有一个人因此看不出分隔符。分档必须落在服务端，模型按返回行
            # 自己数会把「只有一个人」误判成某种分隔符。
            return store.fetch(
                "SELECT CASE "
                "WHEN d.project_owner_names LIKE '%%、%%' AND d.project_owner_names LIKE '%%,%%' "
                "  THEN '两种并存' "
                "WHEN d.project_owner_names LIKE '%%、%%' THEN '全角顿号' "
                "WHEN d.project_owner_names LIKE '%%,%%' THEN '半角逗号' "
                "ELSE '单人无分隔符' END AS separator_kind, COUNT(*) AS n "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.project_owner_names IS NOT NULL AND d.project_owner_names <> '' "
                "GROUP BY separator_kind ORDER BY n DESC, separator_kind",
                caliber=(
                    f"{base}；按 project_owner_names 里出现的分隔符分档；"
                    "「单人无分隔符」是独立一档不是缺失；仅统计该栏非空的任务"
                ),
                limit=bounded,
            )

        if key == "owner_widths":
            # 人数 = 分隔符个数 + 1，两种分隔符都要扣掉再算，否则顿号那两行会少算。
            return store.fetch(
                "SELECT t.task_name, d.project_owner_names, "
                "CHAR_LENGTH(d.project_owner_names) "
                "- CHAR_LENGTH(REPLACE(REPLACE(d.project_owner_names, '、', ''), ',', '')) "
                "+ 1 AS owner_count "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.project_owner_names IS NOT NULL AND d.project_owner_names <> '' "
                "ORDER BY owner_count DESC, t.id",
                caliber=(f"{base}；owner_count = 分隔符个数 + 1，顿号与逗号都计入；按人数倒序，最多的一条即首行"),
                limit=bounded,
            )

        if key == "completion_time_formats":
            # 「各种写法各有多少条」问的是格式档位，不是去重取值：库里 28 个不同
            # 取值归成 6 档，拿 completion_time_values 的 28 去答会答错一个量级。
            # 判别顺序即优先级，见 _COMPLETION_TIME_FORMAT_CASE 的注释。
            total = store.scalar(
                f"SELECT COUNT(*) FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.completion_time IS NOT NULL AND d.completion_time <> ''",
            )
            rows = store.fetch(
                f"SELECT {_COMPLETION_TIME_FORMAT_CASE} AS fmt, COUNT(*) AS cnt "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.completion_time IS NOT NULL AND d.completion_time <> '' "
                "GROUP BY fmt ORDER BY cnt DESC, fmt",
                caliber=(
                    f"{base}；按写法归档（标准日期 / 季度 / 含「底」的模糊表述 / 中文年月日 / "
                    "中文年月 / 其他），一条只进一档，各档相加等于 total_count；"
                    "档数是写法种类数，不是去重取值数（去重取值另有 completion_time_values）；"
                    "'2026年6月底' 归入含「底」一档而非中文年月，判别按此优先级固定；"
                    "仅统计该栏非空的任务，空值不进任何一档"
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

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

        if key == "completion_time_values":
            # 「都有哪些写法」问的是去重后的取值本身，按文本序排列。让模型翻
            # 46 行明细自己归纳，得到的是它总结出的类别名（「持续推进」这种），
            # 不是库里真实存在的取值。
            total = store.scalar(
                "SELECT COUNT(DISTINCT d.completion_time) "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.completion_time IS NOT NULL AND d.completion_time <> ''",
            )
            rows = store.fetch(
                "SELECT DISTINCT d.completion_time "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                f"WHERE {clause} AND d.completion_time IS NOT NULL AND d.completion_time <> '' "
                "ORDER BY d.completion_time",
                caliber=(
                    f"{base}；去重后的 completion_time 原样取值，按文本升序；"
                    "这是库里真实存在的写法，不要归纳成自己的类别名；"
                    f"共 {total['value']} 种，top 决定返回前几种"
                ),
                limit=bounded,
            )
            rows["total_count"] = total["value"]
            return rows

        if key == "effect_consistency":
            # 明细表的当前成效 vs 历史表最新一期的成效。逐条比对必须在服务端做：
            # 两段长文本靠模型眼看，46 行里会把不一致的说成一致。
            # 历史侧要 is_published = 1，未发布的那几期不是「最新一期」。
            return store.fetch(
                "SELECT d.task_id, t.task_name, x.version_no, "
                "(d.progress_effect = x.progress_effect) AS same "
                f"FROM task_group_detail d JOIN task t ON t.id = d.task_id {join} "
                "JOIN (SELECT h.task_id, h.progress_effect, h.version_no, "
                "ROW_NUMBER() OVER (PARTITION BY h.task_id "
                "ORDER BY h.version_no DESC, h.id DESC) rn "
                "FROM task_group_progress_history h WHERE h.is_published = 1) x "
                "ON x.task_id = d.task_id AND x.rn = 1 "
                f"WHERE {clause} ORDER BY same, d.task_id",
                caliber=(
                    f"{base}；明细表 progress_effect 与历史表最新一期（is_published = 1）逐字比对；"
                    "same = 1 一致、0 不一致；不一致的排在最前，"
                    "先看 same = 0 有几行再下「全部一致」的结论"
                ),
                limit=bounded,
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

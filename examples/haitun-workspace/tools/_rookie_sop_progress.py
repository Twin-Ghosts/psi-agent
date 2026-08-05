"""从明细行算进度、逾期、今日到期, 以及总览表的一行投影 —— 纯逻辑, 不碰飞书。

刻意为之: 总览行永远由本模块从明细整体重算, 不做增量加减。飞书 bitable 的
「查找引用」字段(type 19) API 建不出来、公式列也写不进去, 所以总览只能双写维护;
「整体重算」让任何一次漏写都会在下一次勾选时自愈, 不累积漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

STATUS_TODO = "未完成"
STATUS_DONE = "已完成"
STATUS_NA = "不适用"

ROLE_LABELS = {"dev": "研发", "nondev": "非研发", "": "待确认"}


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    percent: int = 0
    overdue: list[dict[str, Any]] = field(default_factory=list)
    due_today: list[dict[str, Any]] = field(default_factory=list)
    next_due: dict[str, Any] | None = None
    all_done: bool = False


def _due(row: dict[str, Any]) -> date | None:
    value = row.get("截止日")
    return value if isinstance(value, date) else None


def summarize(rows: list[dict[str, Any]], today: date) -> Progress:
    """分母只算「适用」的行 —— 不适用的既不进分子也不进分母。"""
    applicable = [r for r in rows if str(r.get("状态") or "") != STATUS_NA]
    done_rows = [r for r in applicable if str(r.get("状态") or "") == STATUS_DONE]
    todo_rows = [r for r in applicable if str(r.get("状态") or "") != STATUS_DONE]

    total = len(applicable)
    done = len(done_rows)
    percent = round(done * 100 / total) if total else 0

    overdue: list[dict[str, Any]] = []
    due_today: list[dict[str, Any]] = []
    future: list[dict[str, Any]] = []
    for row in todo_rows:
        due = _due(row)
        if due is None:
            future.append(row)
        elif due < today:
            overdue.append(row)
        elif due == today:
            due_today.append(row)
        else:
            future.append(row)

    future.sort(key=lambda r: (_due(r) or date.max))
    return Progress(
        done=done,
        total=total,
        percent=percent,
        overdue=overdue,
        due_today=due_today,
        next_due=future[0] if future else None,
        # 刻意为之: total==0 不算「全部完成」, 否则空清单会误发出新手村卡
        all_done=bool(total) and done == total,
    )


def overview_fields(
    rows: list[dict[str, Any]],
    today: date,
    name: str,
    open_id: str,
    role: str,
) -> dict[str, Any]:
    """总览表的一行 —— 纯投影, 删掉重建也不丢信息。"""
    progress = summarize(rows, today)
    onboard = next((r["入职日"] for r in rows if isinstance(r.get("入职日"), date)), None)
    return {
        "open_id": open_id,
        "姓名": name,
        "入职日": onboard,
        "入职第N天": (today - onboard).days + 1 if onboard else 0,
        "角色": ROLE_LABELS.get(role.strip().casefold(), "待确认"),
        "进度": f"{progress.done}/{progress.total}",
        "完成率": progress.percent,
        "逾期项数": len(progress.overdue),
        "逾期项": "、".join(str(r.get("项") or "") for r in progress.overdue),
        "状态": "已出新手村" if progress.all_done else "进行中",
        "最后更新": today,
    }

"""组各类入职卡片的 JSON —— 纯逻辑, 不发消息, 便于单测。

刻意为之: 建在 multi_use 卡之上, 每行一个独立 action。行的 action 名必须唯一且规范,
多选消费就是按它落墓碑的; 撞名或留空会让该行退回整卡去重(点一下整张卡就废)。
外壳沿用 tools/feishu_todo_card.py 的 legacy 形态: update_multi 必须为 true,
否则卡片只对一个查看者更新。
"""

from __future__ import annotations

# ruff: noqa: E402, RUF001
import sys
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_progress as _p

ACTION_TICK_PREFIX = "rookie_tick_"
ACTION_ROLE_DEV = "rookie_role_dev"
ACTION_ROLE_NONDEV = "rookie_role_nondev"
HANDLER_TICK = "rookie_sop_tick"
HANDLER_ROLE = "rookie_sop_role_set"

_EMPTY = "□"
_FILLED = "■"


def _shell(title: str, elements: list[dict[str, Any]], template: str = "blue") -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
        "elements": elements,
    }


def _item_id_of(row: dict[str, Any]) -> str:
    """明细行的 item_id 藏在 记录键 = "{open_id}:{item_id}" 的后半段。"""
    key = str(row.get("记录键") or "")
    return key.rsplit(":", 1)[-1] if ":" in key else key


def _row_elements(row: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """一行的展示 + 按钮; 返回 (elements, action 名或空串)。"""
    title = str(row.get("项") or "").strip()
    acceptance = str(row.get("验收标准") or "").strip()
    done = str(row.get("状态") or "") == _p.STATUS_DONE

    if done:
        lines = [f"{_FILLED} ~~{title}~~"]
        finished = row.get("完成时间")
        if finished is not None:
            lines[0] += f"　✅ {finished}"
        return [{"tag": "markdown", "content": "\n".join(lines)}], ""

    lines = [f"{_EMPTY} **{title}**"]
    if acceptance:
        lines.append(f"验收：{acceptance}")
    action = f"{ACTION_TICK_PREFIX}{_item_id_of(row)}"
    return (
        [
            {"tag": "markdown", "content": "\n".join(lines)},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "标记完成"},
                        "type": "default",
                        "value": {"action": action, "item_id": _item_id_of(row)},
                    }
                ],
            },
        ],
        action,
    )


def _rows_section(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    elements: list[dict[str, Any]] = []
    handlers: dict[str, str] = {}
    for row in rows:
        elements.append({"tag": "hr"})
        row_elements, action = _row_elements(row)
        elements.extend(row_elements)
        if action:
            handlers[action] = HANDLER_TICK
    return elements, handlers


def _footer(sop_url: str) -> list[dict[str, Any]]:
    text = "遇到问题先问 Haitun"
    if sop_url.strip():
        text += f" · [查看完整 SOP]({sop_url.strip()})"
    return [{"tag": "hr"}, {"tag": "markdown", "content": text}]


def module_card(
    module: str,
    rows: list[dict[str, Any]],
    progress_text: str,
    due_text: str,
    sop_url: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": f"{due_text} · 进度 {progress_text}"}]
    rows_elements, handlers = _rows_section(rows)
    elements.extend(rows_elements)
    elements.extend(_footer(sop_url))
    template = "green" if not handlers and rows else "blue"
    return _shell(f"入职路线图 · {module}", elements, template), handlers


def role_card(due_text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """开发环境卡的第一段: 先确认角色, 再决定这部分要不要做。"""
    elements = [
        {"tag": "markdown", "content": f"{due_text}\n\n先确认你的角色，我们再决定这部分要不要做："},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "我是研发"},
                    "type": "primary",
                    "value": {"action": ACTION_ROLE_DEV, "role": "dev"},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "我不是研发"},
                    "type": "default",
                    "value": {"action": ACTION_ROLE_NONDEV, "role": "nondev"},
                },
            ],
        },
    ]
    return (
        _shell("入职路线图 · 开发环境", elements),
        {ACTION_ROLE_DEV: HANDLER_ROLE, ACTION_ROLE_NONDEV: HANDLER_ROLE},
    )


def role_settled_card(
    is_dev: bool,
    rows: list[dict[str, Any]],
    due_text: str,
    sop_url: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    if not is_dev:
        elements = [{"tag": "markdown", "content": "你选择了「非研发」，这部分不需要完成。"}]
        return _shell("入职路线图 · 开发环境 ✅ 不适用", elements, "grey"), {}
    return module_card(
        "开发环境",
        rows,
        f"{sum(1 for r in rows if str(r.get('状态') or '') == _p.STATUS_DONE)}/{len(rows)}",
        due_text,
        sop_url,
    )


def remind_card(
    name: str,
    day_index: int,
    progress: Any,
    sop_url: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    header = f"入职第 {day_index} 天 · 进度 {progress.done}/{progress.total}"
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": header}]
    handlers: dict[str, str] = {}

    for label, rows in (("⚠️ 已逾期", progress.overdue), ("📌 今天到期", progress.due_today)):
        if not rows:
            continue
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": f"**{label} {len(rows)} 项**"})
        for row in rows:
            row_elements, action = _row_elements(row)
            elements.extend(row_elements)
            if action:
                handlers[action] = HANDLER_TICK

    if progress.next_due is not None:
        nxt = progress.next_due
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "markdown",
                "content": f"下一个到期：{nxt.get('模块') or ''}（{nxt.get('截止日')}）",
            }
        )
    elements.extend(_footer(sop_url))
    return _shell("入职提醒", elements, "orange" if progress.overdue else "blue"), handlers


def graduation_card(name: str, total: int) -> tuple[dict[str, Any], dict[str, str]]:
    elements = [
        {
            "tag": "markdown",
            "content": f"🎉 恭喜 {name}，{total} 项全部完成 —— 你出新手村了！",
        }
    ]
    return _shell("出新手村", elements, "green"), {}


def digest_card(
    overview_rows: list[dict[str, Any]],
    table_url: str,
    today_text: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """HR 日报 —— 只读: 表格链接是普通跳转按钮, 不注册任何 action。"""
    total = len(overview_rows)
    percents = [r.get("完成率") for r in overview_rows if isinstance(r.get("完成率"), int)]
    overall = round(sum(percents) / len(percents)) if percents else 0
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"{today_text}\n\n在途新人 {total} 人 · 整体完成率 {overall}%"}
    ]

    lines: list[str] = []
    attention: list[str] = []
    for row in overview_rows:
        name = str(row.get("姓名") or "")
        if str(row.get("状态") or "") == "已出新手村":
            icon = "✅"
            tail = "今日出新手村"
        elif int(row.get("逾期项数") or 0) > 0:
            icon = "⚠️"
            tail = f"逾期 {row.get('逾期项数')} 项（{row.get('逾期项')}）"
            attention.append(f"{name} 逾期 {row.get('逾期项数')} 项")
        else:
            icon = "🕐"
            tail = f"入职第 {row.get('入职第N天')} 天，正常"
        lines.append(f"{icon} **{name}**　{row.get('进度')}　{tail}")

    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "\n".join(lines) if lines else "今日无在途新人。"})
    if attention:
        elements.append({"tag": "markdown", "content": "**需要关注：** " + "；".join(attention)})
    else:
        elements.append({"tag": "markdown", "content": "全部正常，无需关注。"})

    if table_url.strip():
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看详情表格"},
                        "type": "primary",
                        "url": table_url.strip(),
                    }
                ],
            }
        )
    return _shell("新人入职进度日报", elements, "orange" if attention else "blue"), {}

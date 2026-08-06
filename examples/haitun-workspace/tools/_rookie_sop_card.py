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
# 与 rookie_sop_role_set / config/rookie_sop.yaml 里的 id 一致: 角色确认这一项
_ROLE_CONFIRMED_ITEM = "role_confirmed"
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

    # 刻意为之: 按钮文字就是条目名本身, 不在文字里塞 □ ——
    # 框架勾选后把被点按钮整体换成 f"● ~~{按钮文字}~~"(_card_action.py 的
    # _consumed_card_content), 所以标记只能由框架那个 ● 提供; 自己再塞一个 □
    # 会渲染成「● ~~□ 连上 WiFi~~」两个标记并排。按钮本身就是那个可点的方框,
    # 文字写在框里, 于是「文字 + 可交互的框」是同一行而不是上下两块。
    action = f"{ACTION_TICK_PREFIX}{_item_id_of(row)}"
    elements: list[dict[str, Any]] = [
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": title},
                    "type": "default",
                    "value": {"action": action, "item_id": _item_id_of(row)},
                }
            ],
        }
    ]
    if acceptance:
        # 验收标准不能进按钮文字, 否则会一起被划掉; 放按钮下方的小字。
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": f"验收：{acceptance}"}]})
    return elements, action


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


def _role_button(label: str, action: str, role: str) -> dict[str, Any]:
    """一个角色选项 = 一个独立的可点方框, 与条目行同样的形态。"""
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {"action": action, "role": role},
            }
        ],
    }


def role_card(
    due_text: str,
    dev_rows: list[dict[str, Any]] | None = None,
    sop_url: str = "",
    progress_text: str = "",
    role_answered: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """开发环境卡 —— 一张卡装完角色选择与 5 个开发项。

    刻意为之: 不再先发角色卡、答完再发第二张。multi_use 的消费粒度是单个 action,
    点掉一个角色按钮只会退休那一个按钮, 其余行(含 5 个开发项)照旧可点, 所以两类
    动作可以共存于同一张卡。两类 action 各自映射到自己的 handler,
    Channel 按 action 名分发, 互不干扰。
    """
    rows = dev_rows or []
    head = f"{due_text} · 进度 {progress_text}" if progress_text else due_text
    elements: list[dict[str, Any]] = [{"tag": "markdown", "content": head}]
    handlers: dict[str, str] = {}

    # role_confirmed 这一项就是「角色是否已答」本身。角色未答时它由下面那两个角色
    # 按钮代表, 不再单独给它一个勾选按钮 —— 否则同一件事有两个可点的动作, 而点角色
    # 按钮时工具已经把这一项标完成了。答完之后它作为已完成行正常显示。
    role_rows = [r for r in rows if _item_id_of(r) == _ROLE_CONFIRMED_ITEM]
    item_rows = [r for r in rows if _item_id_of(r) != _ROLE_CONFIRMED_ITEM]

    if not role_answered:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**先确认你是不是技术人员**（点一下即可）："})
        elements.append(_role_button("研发人员", ACTION_ROLE_DEV, "dev"))
        elements.append(_role_button("非研发人员", ACTION_ROLE_NONDEV, "nondev"))
        handlers[ACTION_ROLE_DEV] = HANDLER_ROLE
        handlers[ACTION_ROLE_NONDEV] = HANDLER_ROLE
        rows = item_rows
    else:
        # 答完角色: 已完成的 role_confirmed 行摆在最前, 让「这一勾已落地」可见。
        rows = role_rows + item_rows

    rows_elements, rows_handlers = _rows_section(rows)
    elements.extend(rows_elements)
    handlers.update(rows_handlers)
    elements.extend(_footer(sop_url))

    template = "green" if rows and not rows_handlers and role_answered else "blue"
    return _shell("入职路线图 · 开发环境", elements, template), handlers


def role_settled_card(
    is_dev: bool,
    rows: list[dict[str, Any]],
    due_text: str,
    sop_url: str,
    role_confirmed_row: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """角色已答后的开发环境卡。

    刻意为之: 现在角色与 5 个开发项同在一张卡上(见 role_card), 所以角色被点掉后
    框架已经就地把那个按钮改成「● ~~研发人员~~」, 剩下的开发项按钮原样可点 ——
    正常路径下不需要再发一张卡。本函数保留是为了两种场景:
      1) 非研发: 5 项被标不适用, 需要一张终态卡说明「这部分不用做」;
      2) 非研发→研发 的反悔路径: 原卡上两个角色按钮都已被消费, 5 项刚被复活成
         未完成却没有可点的按钮了, 只能补发一张带按钮的新卡
         (edit_card 不重新注册回调, 编辑出来的按钮全是死的)。
    """
    lead_rows = [role_confirmed_row] if role_confirmed_row else []
    if not is_dev:
        elements: list[dict[str, Any]] = []
        for row in lead_rows:
            row_elements, _action = _row_elements(row)
            elements.extend([{"tag": "hr"}, *row_elements])
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "你选择了「非研发人员」，这部分不需要完成。"})
        return _shell("入职路线图 · 开发环境 ✅ 不适用", elements, "grey"), {}
    all_rows = lead_rows + rows
    done = sum(1 for r in all_rows if str(r.get("状态") or "") == _p.STATUS_DONE)
    return role_card(
        due_text,
        dev_rows=all_rows,
        sop_url=sop_url,
        progress_text=f"{done}/{len(all_rows)}",
        role_answered=True,
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

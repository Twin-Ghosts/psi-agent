"""每日 9:30 催办: 只在有逾期或今日到期时发卡; 全部完成则发毕业卡并删掉自己的定时。

由 schedules/rookie-remind-<后8位> 以 fire=tool 调用, 到点不经过 LLM。
按截止日驱动而非发放日 —— SOP 的模块几乎都从 Day 1 开始, 只是截止日不同。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_card as _card
import _rookie_sop_config as _cfg
import _rookie_sop_progress as _p
import _rookie_sop_runtime as _rt
import _rookie_sop_store as _store
from feishu_message import feishu_message_send_card
from schedule_manage import schedule_manage


def decide_remind(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """三条分支: 静默 / 催办 / 毕业。抽成纯函数以便单测。"""
    progress = _p.summarize(rows, today)
    if progress.all_done:
        return {"kind": "graduate", "progress": progress}
    if progress.overdue or progress.due_today:
        return {"kind": "remind", "progress": progress}
    # 刻意为之: 无欠项就不发 —— 让消息量随完成度自然衰减, 避免日推麻木
    return {"kind": "silent", "progress": progress}


async def rookie_sop_remind(open_id: str = "") -> str:
    """Remind one new hire of overdue / due-today onboarding items, or graduate them.

    Fired by that person's ``rookie-remind-<suffix>`` schedule with ``fire=tool``, so no
    LLM is involved. Silent when nothing is overdue or due today. When every applicable
    item is done it sends one graduation card and deletes its own schedule.

    Args:
        open_id: The new hire's Feishu open_id (written into the schedule's tool_args).
    """
    target = (open_id or "").strip()
    if not target:
        return json.dumps({"ok": False, "error": "open_id is required"}, ensure_ascii=False)

    state = await _rt.load_state()
    app_token = str(state.get("app_token") or "")
    detail_table = str(state.get("detail_table_id") or "")
    if not app_token or not detail_table:
        return json.dumps({"ok": False, "error": "rookie SOP base is not initialised"}, ensure_ascii=False)

    bitable = _rt.bitable_adapter()
    rows, truncated = await _store.fetch_detail(bitable, app_token, detail_table, target)
    today = date.today()
    decision = decide_remind(rows, today)
    kind = decision["kind"]
    progress = decision["progress"]

    if kind == "silent":
        result = {"ok": True, "sent": False, "reason": "nothing overdue or due today"}
        if truncated:
            result["truncated"] = True
        return json.dumps(result, ensure_ascii=False)

    cfg = await _store.load_config()
    name = next((str(r.get("姓名") or "") for r in rows if r.get("姓名")), target)
    onboard = next((r["入职日"] for r in rows if isinstance(r.get("入职日"), date)), today)

    if kind == "graduate":
        card, handlers = _card.graduation_card(name, progress.total)
    else:
        card, handlers = _card.remind_card(
            name, _cfg.day_index(onboard, today), progress, str(cfg.get("sop_doc_url") or "")
        )

    business = {
        "type": "rookie_sop",
        "open_id": target,
        "name": name,
        "module": "催办",
        "app_token": app_token,
        "detail_table_id": detail_table,
        "overview_table_id": str(state.get("overview_table_id") or ""),
    }
    sent = _store._parse_result(
        await feishu_message_send_card(
            target,
            json.dumps(card, ensure_ascii=False),
            "open_id",
            "",
            json.dumps(business, ensure_ascii=False),
            json.dumps(handlers, ensure_ascii=False),
            bool(handlers),
        )
    )
    # 不能只看抛不抛异常: send_card 失败时返回 ok=false 的字符串, 更阴的一种是
    # "卡发出去了但回调快照没存下来"(callback_context_saved=false) —— 按钮全是
    # 死的, 必须当失败处理, 否则催办卡等于白发。
    if sent.get("ok") is not True or sent.get("callback_context_saved") is False:
        return json.dumps(
            {
                "ok": False,
                "error": sent.get("message") or sent.get("error") or "feishu_message_send_card failed",
                "kind": kind,
            },
            ensure_ascii=False,
        )

    schedule_result = ""
    if kind == "graduate":
        # 与 rookie_sop_card_send.py 创建时用的命名约定必须一致, 否则这里删的是
        # 一个不存在的名字, 定时任务永远留着。结果也不能吞: 万一真删不掉(比如
        # 名字对不上), 调用方要能看到 "[Error] ..." 而不是一个假的 ok=true。
        schedule_result = await schedule_manage(action="delete", schedule_name=f"rookie-remind-{target[-8:]}")
        if schedule_result.startswith("[Error]"):
            return json.dumps(
                {
                    "ok": False,
                    "sent": True,
                    "kind": kind,
                    "error": f"card sent but schedule delete failed: {schedule_result}",
                },
                ensure_ascii=False,
            )

    result: dict[str, Any] = {
        "ok": True,
        "sent": True,
        "kind": kind,
        "overdue": len(progress.overdue),
        "due_today": len(progress.due_today),
    }
    if schedule_result:
        result["schedule"] = schedule_result
    if truncated:
        result["truncated"] = True
    return json.dumps(result, ensure_ascii=False)

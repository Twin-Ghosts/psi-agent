"""每日 9:30 催办: 只在入职第 1、2 天发卡(绿/红), 第 2 天若仍未完成再给 HR 发一张反馈卡；
全部完成则随时发毕业卡；第 3 天起不再推、并删掉自己的定时(不管完没完成)。

由 schedules/rookie-remind-<后8位> 以 fire=tool 调用, 到点不经过 LLM。
"""

from __future__ import annotations

# ruff: noqa: E402, RUF002
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


def decide_remind(rows: list[dict[str, Any]], today: date, day_index: int) -> dict[str, Any]:
    """四条分支: 毕业 / 催办(第 1 天) / 催办+上报 HR(第 2 天) / 停止(第 3 天起)。

    只推两天, 不是「只要没做完就一直推」——第 3 天起就算还有未完成项也不再发卡,
    调用方(rookie_sop_remind)据此把自己的定时任务删掉, 不能让它继续到点转、
    每次都返回"无事可做"却仍占着一个每天要跑一次的定时。

    "催办+上报" 不代表 HR 反馈卡真的会发出去——那要看 hr_notify_id 是否配置,
    这里只表达"这次催办事件本身值得让 HR 知道", 是否真的通知是调用方的事。
    """
    progress = _p.summarize(rows, today)
    if progress.all_done:
        return {"kind": "graduate", "progress": progress}
    if day_index <= 1:
        return {"kind": "remind", "progress": progress, "notify_hr": False}
    if day_index == 2:
        return {"kind": "remind", "progress": progress, "notify_hr": True}
    # 刻意为之: 第 3 天起不再推 —— 只推两天, 之后是否还没做完是 HR 反馈卡该管的事,
    # 不该让新人天天收到催办卡到无穷。
    return {"kind": "stop", "progress": progress}


async def _delete_own_schedule(target: str) -> str:
    """删掉这个人的 rookie-remind-<后8位> 定时 —— 命名约定必须与创建时一致
    (rookie_sop_card_send.py), 否则删的是个不存在的名字, 任务永远留着。
    """
    return await schedule_manage(action="delete", schedule_name=f"rookie-remind-{target[-8:]}")


async def rookie_sop_remind(open_id: str = "") -> str:
    """Remind one new hire of overdue / due-today onboarding items, or graduate them.

    Fired by that person's ``rookie-remind-<suffix>`` schedule with ``fire=tool``, so no
    LLM is involved. Only ever sends on onboarding day 1 (green) and day 2 (red) — from
    day 3 onward it sends nothing and deletes its own schedule, whether or not every item
    is done, so the schedule doesn't fire forever just to report "nothing to do". Day 2
    additionally sends HR a feedback card when the checklist is still incomplete — skipped
    with a reason when ``hr_notify_id`` is empty (never guessed, never silently dropped).
    Any day, if every applicable item is already done, it sends one graduation card instead
    and deletes its own schedule regardless of day index.

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
    onboard = next((r["入职日"] for r in rows if isinstance(r.get("入职日"), date)), today)
    day = _cfg.day_index(onboard, today)
    decision = decide_remind(rows, today, day)
    kind = decision["kind"]
    progress = decision["progress"]

    if kind == "stop":
        # 第 3 天起不再推 —— 删掉自己这份定时, 而不是继续到点转、天天返回"无事可做"。
        # 删不掉也不算硬失败(人可能已经毕业时被删过一次), 但要如实报告, 不能吞。
        schedule_result = await _delete_own_schedule(target)
        result = {
            "ok": True,
            "sent": False,
            "kind": kind,
            "reason": "past day 2, no more pushes; schedule self-deleted",
            "schedule": schedule_result,
        }
        if truncated:
            result["truncated"] = True
        return json.dumps(result, ensure_ascii=False)

    cfg = await _store.load_config()
    name = next((str(r.get("姓名") or "") for r in rows if r.get("姓名")), target)

    if kind == "graduate":
        card, handlers = _card.graduation_card(name, progress.total)
    else:
        card, handlers = _card.remind_card(name, day, progress, str(cfg.get("sop_doc_url") or ""))

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
        # 结果也不能吞: 万一真删不掉(比如名字对不上), 调用方要能看到
        # "[Error] ..." 而不是一个假的 ok=true。
        schedule_result = await _delete_own_schedule(target)
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

    hr_feedback: dict[str, Any] = {}
    if kind == "remind" and decision.get("notify_hr"):
        # kind == "remind" 已经保证 progress.all_done 为假(decide_remind 先判毕业),
        # 这里不必再判一次 —— 走到这里就是"第 2 天, 还没做完"。
        hr_feedback = await _send_hr_feedback(cfg, name, progress)

    result: dict[str, Any] = {
        "ok": True,
        "sent": True,
        "kind": kind,
        "overdue": len(progress.overdue),
        "due_today": len(progress.due_today),
    }
    if schedule_result:
        result["schedule"] = schedule_result
    if hr_feedback:
        result["hr_feedback"] = hr_feedback
    if truncated:
        result["truncated"] = True
    return json.dumps(result, ensure_ascii=False)


async def _send_hr_feedback(cfg: dict[str, Any], name: str, progress: Any) -> dict[str, Any]:
    """入职第 2 天仍未完成时, 顺带给 HR 发一张反馈卡。

    hr_notify_id 在联调阶段被刻意留空(安全考虑) —— 空的时候必须明确跳过并说明原因,
    不能悄悄不发、也不能猜一个收件人发出去(那比不发更糟: 卡片发给了错的人)。
    """
    hr_target = str(cfg.get("hr_notify_id") or "").strip()
    if not hr_target:
        return {"ok": False, "sent": False, "reason": "hr_notify_id is empty in config/rookie_sop.yaml"}

    card, handlers = _card.hr_feedback_card(name, progress, str(cfg.get("sop_doc_url") or ""))
    sent = _store._parse_result(
        await feishu_message_send_card(
            hr_target,
            json.dumps(card, ensure_ascii=False),
            "open_id",
            "",
            json.dumps({"type": "rookie_sop_hr_feedback", "name": name}, ensure_ascii=False),
            json.dumps(handlers, ensure_ascii=False),
        )
    )
    if sent.get("ok") is not True:
        return {
            "ok": False,
            "sent": False,
            "error": sent.get("message") or sent.get("error") or "feishu_message_send_card failed",
        }
    return {"ok": True, "sent": True}

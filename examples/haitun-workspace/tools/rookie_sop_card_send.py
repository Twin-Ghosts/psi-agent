"""新人入职: 建明细/总览行 + 发全部模块卡 + 建每日催办定时任务。

由 feishu.hr.user_created 触发器 fire=tool 调用(Session 注入 event_payload_json),
也可手动传 open_id 联调。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_card as _card
import _rookie_sop_config as _cfg
import _rookie_sop_docapi as _docapi
import _rookie_sop_runtime as _rt
import _rookie_sop_store as _store
from feishu_api import feishu_api
from feishu_message import feishu_message_send_card
from schedule_manage import schedule_manage


async def rookie_sop_card_send(
    open_id: str = "",
    name: str = "",
    event_payload_json: str = "",
    onboard_date: str = "",
    force_resend: bool = False,
) -> str:
    """Send a new hire the full onboarding SOP as per-module tickable cards.

    Prefer calling with empty ``open_id``/``name`` from a ``feishu.hr.user_created``
    trigger — Session injects ``event_payload_json``. Partially idempotent: a
    repeat call for the same person never re-seeds detail rows and never re-sends
    cards or re-creates the reminder schedule (both are skipped once detail rows
    already exist) — it only recomputes the overview row so it stays accurate.
    Pass ``force_resend=True`` to deliberately re-send every module card and
    re-create the reminder schedule anyway (e.g. manual troubleshooting).

    Args:
        open_id: New hire Feishu open_id (ou_...). Empty → read from event_payload_json.
        name: Display name. Empty → from payload, else the open_id.
        event_payload_json: The event envelope payload (injected by Session).
        onboard_date: 'YYYY-MM-DD'; empty means today.
        force_resend: When true, re-send all module cards and re-create the
            reminder schedule even if detail rows already exist. Default False.
    """
    payload = _store._parse_result(event_payload_json) if event_payload_json else {}
    resolved_open_id = (open_id or "").strip() or str(payload.get("open_id") or "").strip()
    resolved_name = (name or "").strip() or str(payload.get("name") or "").strip() or resolved_open_id
    if not resolved_open_id:
        return json.dumps({"ok": False, "error": "open_id is required"}, ensure_ascii=False)

    try:
        onboard = datetime.strptime(onboard_date.strip(), "%Y-%m-%d").date() if onboard_date.strip() else date.today()
    except ValueError:
        return json.dumps({"ok": False, "error": f"invalid onboard_date {onboard_date!r}"}, ensure_ascii=False)

    cfg = await _store.load_config()
    items = _cfg.load_sop(cfg)
    if not items:
        return json.dumps({"ok": False, "error": "config/rookie_sop.yaml has no items"}, ensure_ascii=False)

    state = await _rt.ensure_base(cfg)
    missing = [k for k in ("app_token", "detail_table_id", "overview_table_id") if not state.get(k)]
    if missing:
        return json.dumps(
            {"ok": False, "error": f"bitable base unavailable, missing {missing}: {state}"}, ensure_ascii=False
        )

    bitable = _rt.bitable_adapter()
    app_token = str(state["app_token"])
    detail_table = str(state["detail_table_id"])
    overview_table = str(state["overview_table_id"])

    # 幂等: 已有明细行就不再建, 免得重复入职事件写出两套
    rows, truncated = await _store.fetch_detail(bitable, app_token, detail_table, resolved_open_id)
    is_first_send = not rows
    if is_first_send:
        seed_raw = await bitable.create_records(
            app_token,
            detail_table,
            json.dumps(
                [
                    _store.detail_row_fields(i, open_id=resolved_open_id, name=resolved_name, onboard=onboard)
                    for i in items
                ],
                ensure_ascii=False,
            ),
        )
        # 不查 ok 就往下走是致命的: 播种被飞书拒绝时明细表还是空的, 但下面仍会
        # 当满员发出模块卡与建定时 —— 新人看见卡却点不出任何已存在的行, 且后续
        # 事件因 is_first_send 已被此次(失败的)调用判过而永远不会再播种一次。
        seed_ok, seed_error = _store._write_ok(seed_raw)
        if not seed_ok:
            return json.dumps(
                {"ok": False, "error": f"create_records rejected: {seed_error}"}, ensure_ascii=False
            )
        rows, truncated_2 = await _store.fetch_detail(bitable, app_token, detail_table, resolved_open_id)
        truncated = truncated or truncated_2

    today = date.today()
    await _store.recompute_overview(
        bitable,
        app_token,
        overview_table,
        open_id=resolved_open_id,
        name=resolved_name,
        role="",
        rows=rows,
        today=today,
    )

    # 卡片与催办定时同样幂等: 重复事件不该把两套卡都摆在新人面前(旧卡仍可点,
    # 会跟新卡的行竞争), 也不该把提醒任务重建一遍。只有首次发或显式 force_resend
    # 才走下面这一段。
    if not _rt.should_send_cards(is_first_send=is_first_send, force_resend=force_resend):
        return json.dumps(
            {
                "ok": True,
                "open_id": resolved_open_id,
                "items": len(items),
                "cards_sent": [],
                "cards_skipped": "detail rows already existed; pass force_resend=True to resend",
            },
            ensure_ascii=False,
        )

    sop_url = str(cfg.get("sop_doc_url") or "")

    # 详情页: 为这个新人建一份自己的清单文档, 只授权他本人, 并订阅其变更。
    # 刻意为之: 原先一次性发 7 张模块卡, 用户反馈观感太糟 —— 改成一张入口卡 +
    # 一个跳转按钮。33 项在文档里一屏勾完, 勾选由文档变更事件同步回明细表。
    # 文档权限能精确到「单文档 + 单人」(bitable 最细只到 base 级, 做不到),
    # 所以每人只看得到自己那一份。
    doc = await _docapi.provision_doc(
        feishu_api,
        open_id=resolved_open_id,
        name=resolved_name,
        rows=rows,
        sop_url=sop_url,
    )
    if not doc.get("document_id"):
        return json.dumps({"ok": False, "error": f"provision doc failed: {doc.get('error')}"}, ensure_ascii=False)
    doc_url = str(doc.get("url") or "")

    # 文档索引: 变更事件只带文档 token, 同步工具要靠它反查是谁的清单。
    state_docs = state.get("docs")
    state_docs = dict(state_docs) if isinstance(state_docs, dict) else {}
    state_docs[str(doc["document_id"])] = resolved_open_id
    state["docs"] = state_docs
    await _rt.save_state(state)

    # 入口卡: 一条消息交代全貌, 没有回调动作(勾选在文档里做)。
    card, handlers = _card.entry_card(resolved_name, rows, doc_url, today)
    business = {
        "type": "rookie_sop",
        "open_id": resolved_open_id,
        "name": resolved_name,
        "module": "入口",
        "app_token": app_token,
        "detail_table_id": detail_table,
        "overview_table_id": overview_table,
        "document_id": doc["document_id"],
    }
    # 发卡结果不能丢: 飞书会因卡片 JSON 不合规整张拒收(实测踩过 200410/11310),
    # 吞掉返回值就会把「卡被拒」报成「已发送」, 新人什么都没收到而工具说成功了。
    sent_raw = await feishu_message_send_card(
        resolved_open_id,
        json.dumps(card, ensure_ascii=False),
        "open_id",
        "",
        json.dumps(business, ensure_ascii=False),
        json.dumps(handlers, ensure_ascii=False),
        False,
    )
    sent_result = _store._parse_result(sent_raw)
    sent: list[str] = []
    failed: list[dict[str, str]] = []
    if sent_result.get("ok") is not True:
        failed.append(
            {
                "module": "入口卡",
                "error": str(sent_result.get("message") or sent_result.get("error") or "send_card failed"),
            }
        )
    else:
        sent.append("入口卡")

    # 每人一份催办定时任务, 落在这个新人自己的 Session workspace 里。结果不能丢:
    # schedule_manage 失败时返回 "[Error] ..." 字符串而不是抛异常, 吞掉它就等于
    # 新人从此收不到提醒却没有任何人知道。重复调用(force_resend 场景)大概率撞见
    # "already exists", 这是预期内的, 不算失败。
    schedule_result = await schedule_manage(
        action="create",
        schedule_name=f"rookie-remind-{resolved_open_id[-8:]}",
        cron="30 9 * * *",
        fire="tool",
        tool="rookie_sop_remind",
        tool_args=json.dumps({"open_id": resolved_open_id}, ensure_ascii=False),
        visibility="silent",
        description=f"{resolved_name} 入职 SOP 每日催办",
    )
    schedule_failed = schedule_result.startswith("[Error]") and "already exists" not in schedule_result

    result: dict[str, Any] = {
        # 有任何一张卡没发出去就不能报 ok —— 否则模型会告诉新人「卡片已送达」,
        # 而对方一张都没收到。
        "ok": not schedule_failed and not failed,
        "open_id": resolved_open_id,
        "items": len(items),
        "cards_sent": sent,
        "schedule": schedule_result,
    }
    if failed:
        result["cards_failed"] = failed
    if truncated:
        result["truncated"] = True
    return json.dumps(result, ensure_ascii=False)

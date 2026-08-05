"""运行时接线: bitable 适配器、base/表的一次性创建、状态文件、卡片编排。

刻意为之: app_token 与两个 table_id 是运行时才有的值, 不能写进 yaml, 所以存
workspace 的 .psi/rookie_sop/base.json (与 feishu_auth 把 token 放
.psi/feishu/uat.json 同一惯例)。
"""

from __future__ import annotations

# ruff: noqa: E402, RUF001, PLC0415
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
import _rookie_sop_store as _store
import _runtime_paths as _paths

_STATE_REL = ".psi/rookie_sop/base.json"
_DEV_MODULE = "开发环境"
_MAX_ROWS_PER_CARD = 40


def bitable_adapter() -> Any:
    """把真实 feishu_bitable_* 工具包成 store 期望的适配器。"""
    import feishu_bitable as _bt

    class _Adapter:
        search_records = staticmethod(_bt.feishu_bitable_search_records)
        create_records = staticmethod(_bt.feishu_bitable_create_records)
        update_records = staticmethod(_bt.feishu_bitable_update_records)

    return _Adapter()


async def load_state() -> dict[str, Any]:
    path = _paths.resolve_workspace() / _STATE_REL
    try:
        text = await path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def save_state(state: dict[str, Any]) -> None:
    path = _paths.resolve_workspace() / _STATE_REL
    await path.parent.mkdir(parents=True, exist_ok=True)
    await path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _module_window(items: list[_cfg.SopItem], module: str) -> int:
    return next((i.window_days for i in items if i.module == module), 1)


def _due_text(onboard: date, window_days: int) -> str:
    if window_days <= 1:
        return "Day 1 截止"
    return f"Day 1-{window_days} 截止（{_cfg.due_date(onboard, window_days)}）"


def plan_module_cards(
    items: list[_cfg.SopItem],
    rows: list[dict[str, Any]],
    onboard: date,
    today: date,
    sop_url: str,
) -> list[dict[str, Any]]:
    """按模块编排要发的卡。开发环境先问角色, 其余直接列勾选行。"""
    modules: list[str] = []
    for item in items:
        if item.module not in modules:
            modules.append(item.module)

    plans: list[dict[str, Any]] = []
    for module in modules:
        window = _module_window(items, module)
        due_text = _due_text(onboard, window)
        if module == _DEV_MODULE:
            card, handlers = _card.role_card(due_text)
            plans.append({"module": module, "card": card, "handlers": handlers, "is_role_card": True})
            continue
        module_rows = [r for r in rows if str(r.get("模块") or "") == module][:_MAX_ROWS_PER_CARD]
        done = sum(1 for r in module_rows if str(r.get("状态") or "") == _p.STATUS_DONE)
        card, handlers = _card.module_card(
            module, module_rows, f"{done}/{len(module_rows)}", due_text, sop_url
        )
        plans.append({"module": module, "card": card, "handlers": handlers, "is_role_card": False})
    return plans


def should_send_cards(*, is_first_send: bool, force_resend: bool) -> bool:
    """卡片/催办是否要发 —— 首次发, 或调用方显式要求强发。"""
    return is_first_send or force_resend


async def ensure_base(cfg: dict[str, Any]) -> dict[str, Any]:
    """首次运行时建 base + 明细表 + 总览表; 之后复用状态文件里的 id。"""
    state = await load_state()
    if state.get("app_token") and state.get("detail_table_id") and state.get("overview_table_id"):
        return state

    import feishu_api as _api
    import feishu_bitable as _bt

    company = str(cfg.get("company_name") or "").strip() or "团队"
    created = _store._parse_result(
        await _api.feishu_api(
            "POST",
            "/open-apis/bitable/v1/apps",
            body_json=json.dumps({"name": f"{company}新人入职进度"}, ensure_ascii=False),
        )
    )
    # feishu_api 走的是通用 _resp_to_result 信封: {ok, code, msg, data} —— 没有
    # "result" 这一层, app_token 在 data.app.app_token 里(飞书原始文档的结构)。
    app_token = str(((created.get("data") or {}).get("app") or {}).get("app_token") or "")
    if not app_token:
        return {"ok": False, "error": f"cannot create bitable base: {created}"}

    detail = _store._parse_result(
        await _bt.feishu_bitable_create_table(
            app_token, "入职明细", json.dumps(_store.DETAIL_FIELDS, ensure_ascii=False)
        )
    )
    overview = _store._parse_result(
        await _bt.feishu_bitable_create_table(
            app_token, "入职总览", json.dumps(_store.OVERVIEW_FIELDS, ensure_ascii=False)
        )
    )
    # feishu_bitable_create_table 是扁平结构 {ok, table_id, name, default_view_id,
    # field_ids} —— table_id 直接在顶层, 同样没有 "result" 包装。
    detail_table_id = str(detail.get("table_id") or "")
    overview_table_id = str(overview.get("table_id") or "")
    # 不落半成品状态: 少了任何一个 table_id, 下次运行会拿着空 id 去调 fetch_detail /
    # create_records, 换回一个不明所以的飞书原始错误, 不如现在就报清楚缺了什么。
    if not detail_table_id or not overview_table_id:
        missing = [
            n for n, v in (("detail_table_id", detail_table_id), ("overview_table_id", overview_table_id)) if not v
        ]
        return {
            "ok": False,
            "error": f"table creation incomplete, missing {missing}: detail={detail}, overview={overview}",
        }
    state = {
        "app_token": app_token,
        "detail_table_id": detail_table_id,
        "overview_table_id": overview_table_id,
        "table_url": f"https://feishu.cn/base/{app_token}",
    }
    await save_state(state)
    return state

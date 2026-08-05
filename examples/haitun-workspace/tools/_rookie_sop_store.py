"""明细表与总览表的读写 —— 唯一碰飞书表格的模块。

刻意为之: bitable 操作通过注入的适配器对象调用(具备 search_records /
create_records / update_records 三个 async 方法), 这样单测传 fake 就能跑,
不需要飞书凭据。日期列(type 5)收发的是毫秒时间戳, 转换只发生在本模块,
上层只见 datetime.date。
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _rookie_sop_config as _cfg
import _rookie_sop_progress as _p
import _runtime_paths as _paths

_CONFIG_PATH = "config/rookie_sop.yaml"

# 飞书字段类型: 1 文本, 2 数字, 3 单选, 5 日期。19(查找引用) API 建不出来。
# 第一列是索引列, 必须是 1/2/5/13/15/20/22 之一 —— 两张表都用文本键列打头。
DETAIL_FIELDS: list[dict[str, Any]] = [
    {"field_name": "记录键", "type": 1},
    {"field_name": "姓名", "type": 1},
    {"field_name": "open_id", "type": 1},
    {"field_name": "模块", "type": 1},
    {"field_name": "项", "type": 1},
    {"field_name": "验收标准", "type": 1},
    {
        "field_name": "状态",
        "type": 3,
        "property": {
            "options": [
                {"name": _p.STATUS_TODO, "color": 1},
                {"name": _p.STATUS_DONE, "color": 0},
                {"name": _p.STATUS_NA, "color": 2},
            ]
        },
    },
    {"field_name": "完成时间", "type": 5},
    {"field_name": "入职日", "type": 5},
    {"field_name": "截止日", "type": 5},
    {"field_name": "Mentor", "type": 1},
    {"field_name": "适用角色", "type": 1},
]

OVERVIEW_FIELDS: list[dict[str, Any]] = [
    {"field_name": "open_id", "type": 1},
    {"field_name": "姓名", "type": 1},
    {"field_name": "入职日", "type": 5},
    {"field_name": "入职第N天", "type": 2},
    {"field_name": "角色", "type": 1},
    {"field_name": "进度", "type": 1},
    {"field_name": "完成率", "type": 2},
    {"field_name": "逾期项数", "type": 2},
    {"field_name": "逾期项", "type": 1},
    {"field_name": "状态", "type": 1},
    {"field_name": "最后更新", "type": 5},
]

_DATE_KEYS = ("完成时间", "入职日", "截止日", "最后更新")


async def load_config() -> dict[str, Any]:
    path = _paths.resolve_agent() / _CONFIG_PATH
    try:
        text = await path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def to_millis(value: date | None) -> int | None:
    if value is None:
        return None
    return int(datetime.combine(value, time()).timestamp() * 1000)


def from_millis(value: Any) -> date | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000).date()


def detail_row_fields(
    item: _cfg.SopItem,
    *,
    open_id: str,
    name: str,
    onboard: date,
    role_label: str = "",
) -> dict[str, Any]:
    return {
        "记录键": f"{open_id}:{item.item_id}",
        "姓名": name,
        "open_id": open_id,
        "模块": item.module,
        "项": item.title,
        "验收标准": item.acceptance,
        "状态": _p.STATUS_TODO,
        "入职日": to_millis(onboard),
        "截止日": to_millis(_cfg.due_date(onboard, item.window_days)),
        "Mentor": "",
        "适用角色": role_label or ("仅研发" if item.dev_only else "全员"),
    }


def _parse_result(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _items_of(raw: str) -> list[dict[str, Any]]:
    payload = _parse_result(raw)
    result = payload.get("result")
    if not isinstance(result, dict):
        return []
    items = result.get("items")
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _row_of(item: dict[str, Any]) -> dict[str, Any]:
    fields = item.get("fields")
    row: dict[str, Any] = dict(fields) if isinstance(fields, dict) else {}
    row["record_id"] = str(item.get("record_id") or "")
    for key in _DATE_KEYS:
        if key in row:
            row[key] = from_millis(row[key])
    return row


def _eq_filter(field_name: str, value: str) -> str:
    return json.dumps(
        {"conjunction": "and", "conditions": [{"field_name": field_name, "operator": "is", "value": [value]}]},
        ensure_ascii=False,
    )


async def fetch_detail(bitable: Any, app_token: str, detail_table_id: str, open_id: str) -> list[dict[str, Any]]:
    raw = await bitable.search_records(app_token, detail_table_id, _eq_filter("open_id", open_id), page_size=500)
    return [_row_of(i) for i in _items_of(raw)]


async def mark_done(
    bitable: Any,
    app_token: str,
    detail_table_id: str,
    *,
    open_id: str,
    item_id: str,
    today: date,
) -> dict[str, Any]:
    key = f"{open_id}:{item_id}"
    raw = await bitable.search_records(app_token, detail_table_id, _eq_filter("记录键", key), page_size=2)
    rows = [_row_of(i) for i in _items_of(raw)]
    if not rows:
        return {"ok": False, "error": f"detail row not found for {key}"}
    row = rows[0]
    if str(row.get("状态") or "") == _p.STATUS_DONE:
        return {"ok": True, "already_done": True, "record_id": row["record_id"]}
    await bitable.update_records(
        app_token,
        detail_table_id,
        json.dumps(
            [{"record_id": row["record_id"], "fields": {"状态": _p.STATUS_DONE, "完成时间": to_millis(today)}}],
            ensure_ascii=False,
        ),
    )
    return {"ok": True, "already_done": False, "record_id": row["record_id"]}


async def mark_module_na(
    bitable: Any,
    app_token: str,
    detail_table_id: str,
    *,
    open_id: str,
    module: str,
    today: date,
) -> dict[str, Any]:
    rows = await fetch_detail(bitable, app_token, detail_table_id, open_id)
    targets = [r for r in rows if str(r.get("模块") or "") == module and str(r.get("状态") or "") != _p.STATUS_DONE]
    if not targets:
        return {"ok": True, "marked": 0}
    await bitable.update_records(
        app_token,
        detail_table_id,
        json.dumps(
            [{"record_id": r["record_id"], "fields": {"状态": _p.STATUS_NA}} for r in targets],
            ensure_ascii=False,
        ),
    )
    return {"ok": True, "marked": len(targets)}


async def recompute_overview(
    bitable: Any,
    app_token: str,
    overview_table_id: str,
    *,
    open_id: str,
    name: str,
    role: str,
    rows: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """从明细整体重算总览行 —— 不做增量, 所以任何漏写都会在下一次调用时自愈。"""
    fields = _p.overview_fields(rows, today, name, open_id, role)
    for key in _DATE_KEYS:
        if key in fields:
            fields[key] = to_millis(fields[key])

    raw = await bitable.search_records(app_token, overview_table_id, _eq_filter("open_id", open_id), page_size=2)
    existing = _items_of(raw)
    if existing:
        record_id = str(existing[0].get("record_id") or "")
        await bitable.update_records(
            app_token,
            overview_table_id,
            json.dumps([{"record_id": record_id, "fields": fields}], ensure_ascii=False),
        )
        return {"ok": True, "created": False, "record_id": record_id, "fields": fields}

    await bitable.create_records(
        app_token, overview_table_id, json.dumps([{"fields": fields}], ensure_ascii=False)
    )
    return {"ok": True, "created": True, "fields": fields}

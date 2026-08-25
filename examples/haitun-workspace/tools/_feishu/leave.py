"""公司 TODO 体系的请假事实源 —— 读「请假表」子表并判定命中日期。

为什么请假要读表格而不是查审批:飞书开放平台的 ``/approval/v4/instances`` 只有
「建实例」和「按 instance_id 单查」,没有按人枚举的接口;考勤接口返回的是打卡结果
(正常/迟到/早退/缺卡),不是请假单。所以「谁在哪天请假」这条事实在当前接口下拿不到,
方案把它变成和 todo 同源的一条人工填报:同一个 spreadsheet 里加一张「请假表」子表。

为什么是工具而不是技能里的一段话:日期区间重叠判定是纯逻辑。写在技能里等于让模型
每个周期心算一次日历,错一次就给休假的人派活并记逾期 —— 错误方向恰好是「加重考核」
且不会立刻暴露。工具把「哪天算请假」固定下来,判定唯一。

漏填等于视为未请假(不做打卡反推兜底:缺卡同时对应出差/外勤/忘打卡/请假,反推会把
前三种误判成请假并静默顺延截止日,那个错误方向是「放宽考核」,比漏判更难发现)。
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import _feishu_impl as _core

from _feishu.sheet import _build_sheet_meta_request, _build_sheet_values_request, _flatten_sheet_cell

#: 「请假表」子表的表名标记 —— 工作表标题命中任一个就算请假表。
_LEAVE_SHEET_MARKERS = ("请假", "leave", "休假")

#: 表头列语义 -> 认列用的标记。顺序即优先级,前面的先命中。
_LEAVE_HEADER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("name", ("姓名", "名字", "负责人", "name", "owner")),
    ("start", ("开始日期", "开始时间", "起始", "start")),
    ("end", ("结束日期", "结束时间", "截止", "end")),
    ("kind", ("类型", "假别", "kind", "type")),
    ("full_day", ("整天", "全天", "是否整天", "full_day")),
    ("note", ("备注", "说明", "note", "remark")),
)

#: 认得出的日期写法。飞书表格里手填日期的形态很杂,统一归一到 ISO。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m-%d", "%m/%d", "%m.%d")

#: 单次查询的最大跨度(天)。防止 date_from/date_to 写反或写成整年时逐日展开炸掉输出。
_MAX_SPAN_DAYS = 366

#: 请假表的读取行数上限。一个周期的请假记录远小于这个数;超了就是表结构不对。
_MAX_LEAVE_ROWS = 500


def _parse_date(text: str) -> datetime.date | None:
    """把一个单元格里的日期写法归一成 ``date``;认不出来返回 None(而不是猜)。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    # 「2026-08-05 10:00」这类带时间的写法:只取日期部分。
    raw = raw.replace("日", "").split(" ")[0].split("T")[0]
    year = datetime.date.today().year
    for fmt in _DATE_FORMATS:
        # 无年份的写法(8.14)先把当前年份补进去再解析,而不是解析完再 replace(year=...):
        # 后者在 3.15 会改行为(CPython #70647),且闰日 2.29 在默认的 1900 年直接解析失败。
        text, pattern = (raw, fmt) if "%Y" in fmt else (f"{year}-{raw}", f"%Y-{fmt}")
        try:
            parsed = datetime.datetime.strptime(text, pattern)
        except ValueError:
            continue
        return parsed.date()
    return None


def _classify_leave_header(header: str) -> str:
    """把请假表的一个表头单元格判成 name/start/end/kind/full_day/note/other。"""
    low = str(header or "").strip().casefold()
    if not low:
        return "other"
    for kind, markers in _LEAVE_HEADER_RULES:
        if any(marker in low for marker in markers):
            return kind
    return "other"


def _header_map(cells: list[Any]) -> dict[str, int]:
    """表头行 -> {语义: 列下标}。同一语义重复出现时取最左那列。"""
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        kind = _classify_leave_header(_flatten_sheet_cell(cell))
        if kind != "other" and kind not in mapping:
            mapping[kind] = index
    return mapping


def _cell(row: list[Any], index: int | None) -> str:
    """按列下标取值;越界或列不存在都返回空串,不抛。"""
    if index is None or index < 0 or index >= len(row):
        return ""
    return _flatten_sheet_cell(row[index])


def _is_full_day(text: str) -> bool:
    """「是否整天」列的取值判定。空着按整天算 —— 请假默认整天,半天要显式写。"""
    raw = str(text or "").strip().casefold()
    if not raw:
        return True
    return raw not in {"否", "no", "false", "n", "0", "半天", "非整天"}


def _overlap_days(
    start: datetime.date, end: datetime.date, date_from: datetime.date, date_to: datetime.date
) -> list[str]:
    """请假区间与查询窗口的交集,逐日展开成 ISO 日期串(闭区间)。"""
    lo = max(start, date_from)
    hi = min(end, date_to)
    if lo > hi:
        return []
    return [(lo + datetime.timedelta(days=offset)).isoformat() for offset in range((hi - lo).days + 1)]


async def _resolve_leave_sheet(token: str, sheet_name: str, user_key: str) -> tuple[str, str, str]:
    """定位请假表的 sheet_id。返回 (sheet_id, title, error)。

    ``sheet_name`` 给了就按标题精确/包含匹配;没给就在所有工作表里找标题含「请假」的
    那张。都找不到时返回错误并**列出实际的工作表标题** —— 让人一眼看出是没建子表
    还是名字不一样,而不是回一句「读不到」。
    """
    meta = await _core._invoke(_build_sheet_meta_request(token), user_key=user_key)
    if not meta.get("ok"):
        return "", "", str(meta.get("error") or meta.get("message") or "sheet meta query failed")
    data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
    sheets = data.get("sheets", []) if isinstance(data, dict) else []
    titles: list[str] = []
    wanted = sheet_name.strip().casefold()
    fallback: tuple[str, str] | None = None
    for sheet in sheets if isinstance(sheets, list) else []:
        if not isinstance(sheet, dict):
            continue
        sheet_id = str(sheet.get("sheet_id") or sheet.get("sheetId") or "")
        title = str(sheet.get("title") or "")
        if not sheet_id:
            continue
        titles.append(title)
        low = title.casefold()
        if wanted:
            if low == wanted:
                return sheet_id, title, ""
            if wanted in low and fallback is None:
                fallback = (sheet_id, title)
        elif fallback is None and any(marker in low for marker in _LEAVE_SHEET_MARKERS):
            fallback = (sheet_id, title)
    if fallback:
        return fallback[0], fallback[1], ""
    hint = "、".join(titles) if titles else "(该表格没有工作表)"
    if wanted:
        return "", "", f"没有名为 {sheet_name!r} 的工作表。实际的工作表: {hint}"
    return "", "", f"没找到「请假表」子表(标题含请假/休假/leave)。实际的工作表: {hint}"


def _wanted_names(names_json: str) -> tuple[set[str], str | None]:
    """解析 names_json 过滤名单。空 = 不过滤,返回表里所有人。"""
    raw = names_json.strip()
    if not raw:
        return set(), None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return set(), f"names_json is not valid JSON: {exc}"
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return set(), 'names_json must be a JSON array of strings, e.g. \'["张三","李四"]\''
    return {item.strip() for item in parsed if item.strip()}, None


def _leave_record(
    row: list[Any],
    columns: dict[str, int],
    date_from: datetime.date,
    date_to: datetime.date,
    row_number: int,
) -> dict[str, Any] | None:
    """把请假表的一行转成一条记录。返回 None = 整行为空,跳过。

    日期缺失或认不出来时**不静默丢**:返回一条带 ``needs_fix`` 的记录,让报表能提示
    补填。静默丢等于把「填错了」变成「没请假」,方向又是加重考核。
    """
    name = _cell(row, columns.get("name")).strip()
    start_text = _cell(row, columns.get("start"))
    end_text = _cell(row, columns.get("end"))
    if not name and not start_text and not end_text:
        return None

    record: dict[str, Any] = {
        "name": name,
        "row": row_number,
        "start": start_text.strip(),
        "end": end_text.strip(),
        "kind": _cell(row, columns.get("kind")).strip(),
        "note": _cell(row, columns.get("note")).strip(),
        "full_day": _is_full_day(_cell(row, columns.get("full_day"))),
    }
    problems: list[str] = []
    if not name:
        problems.append("缺姓名")
    start = _parse_date(start_text)
    # 结束日期空着按「当天请假」算:填了开始没填结束是最常见的填法。
    end = _parse_date(end_text) if end_text.strip() else start
    if start is None:
        problems.append(f"开始日期认不出({start_text.strip() or '空'})")
    # 只在结束日期真填了东西时才报它:空着是合法写法(当天一天),而它认不出来时
    # end 继承的是 start 的 None,报「结束日期认不出()」纯属误导。
    if end is None and end_text.strip():
        problems.append(f"结束日期认不出({end_text.strip()})")
    if start and end and end < start:
        problems.append("结束日期早于开始日期")
        start, end = end, start

    if problems:
        record["needs_fix"] = problems
        record["hit_dates"] = []
        record["hit_days"] = 0
        return record

    assert start is not None and end is not None  # 上面已把 None 的情形归进 problems
    record["start"] = start.isoformat()
    record["end"] = end.isoformat()
    hits = _overlap_days(start, end, date_from, date_to)
    record["hit_dates"] = hits
    record["hit_days"] = len(hits)
    return record


async def query_leave_impl(
    sheet_token: str,
    sheet_name: str = "",
    date_from: str = "",
    date_to: str = "",
    names_json: str = "",
    user_key: str = "",
) -> dict[str, Any]:
    """读「请假表」子表,判定 ``[date_from, date_to]`` 窗口内每人命中的请假日期。

    判定口径(固定,不由模型决定):
    - 区间是**闭区间**,两端都算请假;
    - 结束日期空着 = 当天请假一天;
    - 「是否整天」空着 = 整天(半天要显式写「否」/「半天」);
    - 日期认不出来或缺姓名的行进 ``needs_fix``,不静默丢弃;
    - 表里没有的人 = 没请假(漏填按未请假处理)。
    """
    token = sheet_token.strip()
    if not token:
        return _core._error("sheet_token is required (the segment in a feishu.cn/sheets/<token> URL).")
    start_date = _parse_date(date_from)
    if start_date is None:
        return _core._error(f"date_from 认不出来: {date_from!r}。用 ISO 日期,如 2026-08-05。")
    end_date = _parse_date(date_to) if date_to.strip() else start_date
    if end_date is None:
        return _core._error(f"date_to 认不出来: {date_to!r}。用 ISO 日期,如 2026-08-07。")
    if end_date < start_date:
        return _core._error(f"date_to ({end_date.isoformat()}) 早于 date_from ({start_date.isoformat()})。")
    span = (end_date - start_date).days + 1
    if span > _MAX_SPAN_DAYS:
        return _core._error(f"查询跨度 {span} 天超过上限 {_MAX_SPAN_DAYS} 天;按周期分段查。")

    wanted, problem = _wanted_names(names_json)
    if problem:
        return _core._error(problem)

    sheet_id, sheet_title, error = await _resolve_leave_sheet(token, sheet_name, user_key)
    if error:
        return _core._error(error)

    block_range = f"{sheet_id}!A1:ZZ{_MAX_LEAVE_ROWS}"
    res = await _core._invoke(_build_sheet_values_request(token, block_range), user_key=user_key)
    if not res.get("ok"):
        return res
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    value_range = data.get("valueRange", {}) if isinstance(data, dict) else {}
    raw_rows = value_range.get("values") or []
    rows = [(raw_row if isinstance(raw_row, list) else []) for raw_row in raw_rows]
    if not rows:
        return _core._error(f"工作表「{sheet_title}」是空的,连表头都没有。")

    columns = _header_map(rows[0])
    missing = [kind for kind in ("name", "start") if kind not in columns]
    if missing:
        headers = "、".join(_flatten_sheet_cell(c) for c in rows[0] if _flatten_sheet_cell(c)) or "(空表头)"
        labels = {"name": "姓名", "start": "开始日期"}
        want = "、".join(labels[kind] for kind in missing)
        return _core._error(f"「{sheet_title}」的表头缺少必需列: {want}。实际表头: {headers}")

    records: list[dict[str, Any]] = []
    needs_fix: list[dict[str, Any]] = []
    for offset, row in enumerate(rows[1:], start=2):
        record = _leave_record(row, columns, start_date, end_date, offset)
        if record is None:
            continue
        if wanted and record["name"] not in wanted:
            continue
        if record.get("needs_fix"):
            needs_fix.append(record)
            continue
        records.append(record)

    # 按人汇总:一个人可能有多条请假记录(分段请假),命中日期取并集。
    by_person: dict[str, dict[str, Any]] = {}
    for record in records:
        person = by_person.setdefault(
            record["name"],
            {"name": record["name"], "hit_dates": [], "leaves": [], "hit_days": 0, "full_period": False},
        )
        person["leaves"].append(
            {
                "start": record["start"],
                "end": record["end"],
                "kind": record["kind"],
                "full_day": record["full_day"],
                "note": record["note"],
                "hit_dates": record["hit_dates"],
            }
        )
        for day in record["hit_dates"]:
            if day not in person["hit_dates"]:
                person["hit_dates"].append(day)
    for person in by_person.values():
        person["hit_dates"].sort()
        person["hit_days"] = len(person["hit_dates"])
        # 整周期请假 = 窗口里每一天都命中 ⇒ 派发环节整块跳过(不派卡、不建任务)。
        person["full_period"] = person["hit_days"] == span

    on_leave = [person for person in by_person.values() if person["hit_days"] > 0]
    on_leave.sort(key=lambda person: (-person["hit_days"], person["name"]))
    return {
        "ok": True,
        "sheet": sheet_id,
        "sheet_title": sheet_title,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "span_days": span,
        "queried_names": sorted(wanted) if wanted else [],
        "on_leave": on_leave,
        "on_leave_names": [person["name"] for person in on_leave],
        "full_period_names": [person["name"] for person in on_leave if person["full_period"]],
        "rows_scanned": max(len(rows) - 1, 0),
        "needs_fix": needs_fix,
        "truncated": len(rows) >= _MAX_LEAVE_ROWS,
    }

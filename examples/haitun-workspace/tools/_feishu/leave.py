"""公司 TODO 体系的请假事实源 —— 枚举假勤审批实例并判定命中日期。

请假事实直接来自**飞书审批**:`GET /open-apis/approval/v4/instances` 按
``approval_code`` + 时间窗枚举出实例 code,再逐个读详情拿申请人与起止日期。
``feishu-leave-audit-board`` 一直走的就是这条路。

为什么是工具而不是技能里的一段话:日期区间重叠判定是纯逻辑。写在技能里等于让模型
每个周期心算一次日历,错一次就给休假的人派活并记逾期 —— 错误方向恰好是「加重考核」
且不会立刻暴露。工具把「哪天算请假」固定下来,判定唯一。

**只算已通过的申请**(见 ``_APPROVED``)。审批中的不算:人还得上班,派活是对的;等它批
下来,下一个周期的 audit 自然会看到。没走审批流程的请假(口头请假、HR 后台直接改的)
这里查不到,按未请假处理、由本人当场申诉 —— 这跟原先靠人工填「请假表」相比少了一个
「必须有人记得填」的失败点。
"""

from __future__ import annotations

import contextlib
import datetime
import json
from typing import Any

import _feishu_impl as _core
from lark_channel.core.enum import AccessTokenType, HttpMethod
from lark_channel.core.model import BaseRequest

#: 算「这天请假」的审批状态。实例详情的 status 是字符串枚举,列表/任务里的
#: process_status 是数字(2 = approved),两种都认。
_APPROVED = {"APPROVED", "approved", "2", 2}

#: 认得出的日期写法。审批表单里的日期控件多半是 ISO 或带时间的 ISO,但表单是人配的,
#: 手填控件什么写法都可能出现,所以沿用一组宽容的格式。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m-%d", "%m/%d", "%m.%d")

#: 审批表单里「起止日期」控件的取名习惯。顺序即优先级。
_START_MARKERS = ("开始", "起始", "start", "from")
_END_MARKERS = ("结束", "截止", "终止", "end", "to")
_RANGE_MARKERS = ("请假时间", "假期", "日期区间", "起止", "range", "dayrange")
_KIND_MARKERS = ("假别", "假期类型", "类型", "kind", "type", "leave")
_REASON_MARKERS = ("事由", "原因", "备注", "说明", "reason", "note")

#: 「请假」复合控件的类型名。飞书假勤模板(本租户实测)把整段请假塞进**一个**控件:
#: ``type=leaveGroupV2``、``value={"start","end","name","reason","interval","unit"}``,
#: 而控件名叫「说明」。于是靠中文控件名找「开始/结束日期」在这类模板上一个都抽不到,
#: 每条申请都会落进 needs_fix —— 表现是「查了请假但谁都算没请」,方向恰好是加重考核。
#: 日期必须从控件**类型**认出来,不能只靠命名习惯。
_LEAVE_GROUP_TYPES = {"leavegroupv2", "leavegroup"}

#: 通用「日期区间」控件的类型名(value 形状见 ``_range_bounds``)。
_RANGE_TYPES = {"dayrange", "dateinterval"}

#: 单次查询的最大跨度(天)。防止 date_from/date_to 写反或写成整年时逐日展开炸掉输出。
_MAX_SPAN_DAYS = 366

#: 一次最多读多少条实例详情。一个周期的假勤申请远小于这个数;超了就报 truncated,
#: 而不是悄悄只算前面几条(那会把请假的人算成没请假)。
_MAX_INSTANCES = 200

#: 列实例的页大小上限(飞书规定 100)。
_PAGE_SIZE = 100


def _parse_date(text: str) -> datetime.date | None:
    """把一个日期写法归一成 ``date``;认不出来返回 None(而不是猜)。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    # 「2026-08-05 10:00」「2026-08-05T10:00:00+08:00」这类带时间的写法:只取日期部分。
    raw = raw.replace("日", "").split(" ")[0].split("T")[0]
    year = datetime.date.today().year
    for fmt in _DATE_FORMATS:
        # 无年份的写法(8.14)先把当前年份补进去再解析,而不是解析完再 replace(year=...):
        # 后者在 3.15 会改行为(CPython #70647),且闰日 2.29 在默认的 1900 年直接解析失败。
        text_, pattern = (raw, fmt) if "%Y" in fmt else (f"{year}-{raw}", f"%Y-{fmt}")
        try:
            parsed = datetime.datetime.strptime(text_, pattern)
        except ValueError:
            continue
        return parsed.date()
    return None


def _epoch_ms(day: datetime.date, *, end_of_day: bool = False) -> str:
    """把一天换成 Unix 毫秒字符串。飞书的 start_time/end_time 两个都要且都是字符串。"""
    moment = datetime.datetime.combine(day, datetime.time.max if end_of_day else datetime.time.min)
    return str(int(moment.timestamp() * 1000))


def _build_list_instances_request(approval_code: str, start_ms: str, end_ms: str, page_token: str = "") -> BaseRequest:
    req = BaseRequest()
    req.http_method = HttpMethod.GET
    req.uri = "/open-apis/approval/v4/instances"
    req.add_query("approval_code", approval_code)
    req.add_query("start_time", start_ms)
    req.add_query("end_time", end_ms)
    req.add_query("page_size", _PAGE_SIZE)
    if page_token:
        req.add_query("page_token", page_token)
    req.token_types = {AccessTokenType.TENANT, AccessTokenType.USER}
    return req


def _overlap_days(
    start: datetime.date, end: datetime.date, date_from: datetime.date, date_to: datetime.date
) -> list[str]:
    """请假区间与查询窗口的交集,逐日展开成 ISO 日期串(闭区间)。"""
    lo = max(start, date_from)
    hi = min(end, date_to)
    if lo > hi:
        return []
    return [(lo + datetime.timedelta(days=offset)).isoformat() for offset in range((hi - lo).days + 1)]


def _widgets(form: Any) -> list[dict[str, Any]]:
    """审批实例详情里的 ``form`` 是 JSON 字符串;解析成控件列表,形状不对就给空表。"""
    parsed: Any = form
    if isinstance(form, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(form)
    if not isinstance(parsed, list):
        return []
    return [w for w in parsed if isinstance(w, dict)]


def _label(widget: dict[str, Any]) -> str:
    return str(widget.get("name") or widget.get("custom_id") or widget.get("id") or "").casefold()


def _flatten_value(value: Any) -> str:
    """把控件 value 拍平成文本。日期区间控件的 value 常是 dict 或 list。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("start", "startTime", "start_time", "value", "text"):
            if value.get(key):
                return _flatten_value(value[key])
    if isinstance(value, list):
        return " ".join(_flatten_value(v) for v in value if v)
    return ""


def _range_bounds(value: Any) -> tuple[str, str]:
    """从一个「日期区间」控件的 value 里取出 (起, 止) 两个原始串。

    飞书的 dayRange/dateInterval 控件 value 形状不统一:可能是
    ``{"start": ..., "end": ...}``、``[{...start}, {...end}]``,也可能是
    ``"2026-08-05 ~ 2026-08-07"`` 这样一个串。三种都认。
    """
    if isinstance(value, dict):
        start = value.get("start") or value.get("startTime") or value.get("start_time") or ""
        end = value.get("end") or value.get("endTime") or value.get("end_time") or ""
        if start or end:
            return _flatten_value(start), _flatten_value(end)
    if isinstance(value, list) and len(value) >= 2:
        return _flatten_value(value[0]), _flatten_value(value[1])
    text = _flatten_value(value)
    for sep in ("~", "至", "—", "->", " - "):
        if sep in text:
            head, _, tail = text.partition(sep)
            return head.strip(), tail.strip()
    return text, ""


def _leave_span(form: Any) -> tuple[str, str, str, str]:
    """从审批表单里抽出 (起, 止, 假别, 事由) 四个原始串。

    先找成对的「开始/结束」控件;没有再找单个「日期区间」控件。都抽不到就回空串,
    由调用方归进 needs_fix —— 抽不到日期的申请**不静默丢**,丢了等于把「请了假」
    变成「没请假」,错误方向是加重考核。
    """
    start = end = kind = reason = ""
    range_value: Any = None
    for widget in _widgets(form):
        label = _label(widget)
        value = widget.get("value")
        widget_type = str(widget.get("type") or "").casefold()
        # 请假复合控件先认:它一个控件就带齐起止/假别/事由,且控件名往往是「说明」,
        # 会被下面的 reason 分支抢走。类型优先于命名,否则这类模板一条都抽不出日期。
        if widget_type in _LEAVE_GROUP_TYPES and isinstance(value, dict):
            head, tail = _range_bounds(value)
            start = start or head
            end = end or tail
            kind = kind or _flatten_value(value.get("name"))
            reason = reason or _flatten_value(value.get("reason"))
            continue
        if not start and any(m in label for m in _START_MARKERS):
            start = _flatten_value(value)
        elif not end and any(m in label for m in _END_MARKERS):
            end = _flatten_value(value)
        elif range_value is None and any(m in label for m in _RANGE_MARKERS):
            range_value = value
        elif not kind and any(m in label for m in _KIND_MARKERS):
            kind = _flatten_value(value)
        elif not reason and any(m in label for m in _REASON_MARKERS):
            reason = _flatten_value(value)
        # 控件类型本身就说明是日期区间时也收下(表单可能没按习惯命名)。
        if range_value is None and widget_type in _RANGE_TYPES:
            range_value = value
    if (not start or not end) and range_value is not None:
        head, tail = _range_bounds(range_value)
        start = start or head
        end = end or tail
    return start, end, kind, reason


def _wanted_names(names_json: str) -> tuple[set[str], str | None]:
    """解析 names_json 过滤名单。空 = 不过滤,返回窗口内所有请假的人。

    名单里可以写姓名也可以写 open_id:审批详情回的申请人是 id,姓名要另外解析,所以
    两种都允许,匹配时任一命中即算。
    """
    raw = names_json.strip()
    if not raw:
        return set(), None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return set(), f"names_json is not valid JSON: {exc}"
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return set(), 'names_json must be a JSON array of strings, e.g. \'["ou_abc","张三"]\''
    return {item.strip() for item in parsed if item.strip()}, None


async def _list_instance_codes(approval_code: str, start_ms: str, end_ms: str, user_key: str) -> tuple[list[str], str]:
    """列出窗口内该定义下的所有实例 code。返回 (codes, error)。

    翻页翻到底才敢说「就这些」—— 半页就下结论会把后面那些请假的人算成没请假。
    """
    codes: list[str] = []
    page_token = ""
    while True:
        request = _build_list_instances_request(approval_code, start_ms, end_ms, page_token)
        res = await _core._invoke(request, user_key=user_key)
        if not res.get("ok"):
            return [], str(res.get("error") or res.get("message") or "列审批实例失败")
        payload = res.get("data")
        data: dict[str, Any] = payload if isinstance(payload, dict) else {}
        # 返回在 instance_code_list 键下(不是 items),而且只有 code 没有内容。
        page = data.get("instance_code_list") or []
        codes.extend(str(code) for code in page if code)
        page_token = str(data.get("page_token") or "")
        if not data.get("has_more") or not page_token or len(codes) >= _MAX_INSTANCES:
            return codes, ""


async def query_leave_impl(
    approval_code: str,
    date_from: str = "",
    date_to: str = "",
    names_json: str = "",
    user_key: str = "",
) -> dict[str, Any]:
    """枚举假勤审批实例,判定 ``[date_from, date_to]`` 窗口内每人命中的请假日期。

    判定口径(固定,不由模型决定):
    - 窗口与请假区间都是**闭区间**,两端都算请假;
    - **只算已通过**的申请;审批中/被拒/撤回的不算(审批中的人还得上班);
    - 结束日期空着 = 当天请假一天;
    - 抽不到日期的申请进 ``needs_fix``,不静默丢弃;
    - 没走审批流程的请假查不到 = 按未请假处理,由本人当场申诉。
    """
    code = approval_code.strip()
    if not code:
        return _core._error(
            "approval_code is required: 假勤审批的定义码。从飞书审批后台取,"
            "或用 feishu_api GET /open-apis/approval/v4/tasks/query 取一条样本,"
            "它返回里的 definition_code (有的给 process_code) 就是它。"
        )
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

    # 窗口按「申请的提交时间」筛,而请假区间可能早于或晚于提交。往前后各放宽 60 天,
    # 免得漏掉「上月底提交、这周生效」的申请 —— 真正的判定在下面按区间交集做。
    margin = datetime.timedelta(days=60)
    codes, error = await _list_instance_codes(
        code, _epoch_ms(start_date - margin), _epoch_ms(end_date + margin, end_of_day=True), user_key
    )
    if error:
        return _core._error(f"列审批实例失败: {error}")

    on_leave: dict[str, dict[str, Any]] = {}
    needs_fix: list[dict[str, Any]] = []
    skipped_not_approved = 0
    for instance_code in codes[:_MAX_INSTANCES]:
        detail = await _core.get_approval_instance_impl(instance_code)
        if not detail.get("ok"):
            needs_fix.append(
                {
                    "instance_code": instance_code,
                    "applicant": "",
                    "needs_fix": [f"读详情失败: {detail.get('error') or detail.get('message') or '未知错误'}"],
                }
            )
            continue
        if detail.get("status") not in _APPROVED:
            skipped_not_approved += 1
            continue

        applicant = str(detail.get("applicant") or "")
        start_text, end_text, kind, reason = _leave_span(detail.get("form"))
        start = _parse_date(start_text)
        # 结束日期空着按「当天请假」算:只请一天时表单常常只填一个日期。
        end = _parse_date(end_text) if end_text.strip() else start
        problems: list[str] = []
        if not applicant:
            problems.append("详情里没有申请人")
        if start is None:
            problems.append(f"开始日期抽不到({start_text.strip() or '空'})")
        if end is None and end_text.strip():
            problems.append(f"结束日期认不出({end_text.strip()})")
        if start and end and end < start:
            problems.append("结束日期早于开始日期")
            start, end = end, start
        if problems:
            needs_fix.append(
                {
                    "instance_code": instance_code,
                    "applicant": applicant,
                    "kind": kind,
                    "start": start_text.strip(),
                    "end": end_text.strip(),
                    "needs_fix": problems,
                }
            )
            continue

        assert start is not None and end is not None  # 上面已把 None 的情形归进 problems
        hits = _overlap_days(start, end, start_date, end_date)
        if not hits:
            continue
        if wanted and applicant not in wanted:
            continue
        person = on_leave.setdefault(
            applicant,
            {"applicant": applicant, "hit_dates": [], "leaves": [], "hit_days": 0, "full_period": False},
        )
        person["leaves"].append(
            {
                "instance_code": instance_code,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "kind": kind,
                "reason": reason,
                "hit_dates": hits,
            }
        )
        for day in hits:
            if day not in person["hit_dates"]:
                person["hit_dates"].append(day)

    for person in on_leave.values():
        person["hit_dates"].sort()
        person["hit_days"] = len(person["hit_dates"])
        # 整周期请假 ⇒ 派发环节整块跳过(不派卡、不建任务)。
        person["full_period"] = person["hit_days"] == span

    people = sorted(on_leave.values(), key=lambda p: (-p["hit_days"], p["applicant"]))
    return {
        "ok": True,
        "approval_code": code,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "span_days": span,
        "queried_names": sorted(wanted) if wanted else [],
        "on_leave": people,
        "on_leave_applicants": [p["applicant"] for p in people],
        "full_period_applicants": [p["applicant"] for p in people if p["full_period"]],
        "instances_scanned": len(codes[:_MAX_INSTANCES]),
        "skipped_not_approved": skipped_not_approved,
        "needs_fix": needs_fix,
        "truncated": len(codes) >= _MAX_INSTANCES,
    }

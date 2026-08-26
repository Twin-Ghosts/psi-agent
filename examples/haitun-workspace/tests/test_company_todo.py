"""The 公司 TODO 体系 surface: leave adjudication, ledger idempotency, three skills.

What earns these tests their own file is that both new tools exist to make a *decision*
unique — the kind of decision that, left to a model, is silently wrong in the direction of
punishing people:

- ``feishu_leave_query`` decides which days count as leave, from 假勤 approval instances.
  Overlapping-interval arithmetic redone every cycle eventually assigns work to somebody on
  holiday and books them overdue, so the closed-interval rule, the blank-end-date default,
  and the "unreadable application goes to needs_fix rather than vanishing" rule are each
  pinned here — a vanished application turns "did file leave" into "did not", the same wrong
  direction. The status filter gets its own tests because counting pending applications as
  leave would silently loosen the whole review, and only counting the string form of
  ``APPROVED`` would miss the numeric one the list endpoints return.
- ``feishu_mentor_ledger_ensure`` decides whether a ledger already exists. Its idempotency
  is a *lookup*, so the test that matters is the negative one: a second run must issue no
  create call at all. And because revoking access is irreversible, unexpected collaborators
  must be reported rather than removed — asserted rather than described.

The skill tests guard what rots quietly: frontmatter shape, and that every tool name the
skills tell the agent to call actually exists (a renamed tool leaves the prose pointing at
nothing, and the failure only shows up in production at 15:00 on a Monday).
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = WORKSPACE_ROOT / "skills"
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_impl: Any = importlib.import_module("_feishu_impl")

SKILLS = ("company-todo-sync", "company-todo-review", "company-todo-audit")

APPROVAL_CODE = "7CE7B5C4-leave"
FOLDER = "fldcnTodo01"
MENTOR = "ou_mentor_li"
BOSS = "ou_boss_wang"


class _Invoke:
    """Answer each call from a queue of canned payloads, recording every request."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[Any] = []

    async def __call__(
        self,
        request: Any,
        user_key: str | None = None,
        prefer: str = "tenant",
        identity: str = "",
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        self.requests.append(request() if callable(request) else request)
        reply = self.replies.pop(0) if self.replies else {"ok": True, "code": 0, "data": {}}
        return reply

    @property
    def uris(self) -> list[str]:
        return [str(req.uri) for req in self.requests]


def _codes(*codes: str) -> dict[str, Any]:
    return {"ok": True, "code": 0, "data": {"instance_code_list": list(codes), "has_more": False}}


def _form(start: str, end: str, kind: str = "年假", reason: str = "") -> str:
    widgets = [
        {"id": "w1", "name": "假别", "type": "radioV2", "value": kind},
        {"id": "w2", "name": "开始日期", "type": "date", "value": start},
        {"id": "w3", "name": "结束日期", "type": "date", "value": end},
        {"id": "w4", "name": "事由", "type": "textarea", "value": reason},
    ]
    return json.dumps(widgets, ensure_ascii=False)


def _detail(applicant: str, form: str, status: str | int = "APPROVED") -> dict[str, Any]:
    """One instance detail. ``status`` takes both shapes Feishu uses: the string enum from
    the detail endpoint and the numeric ``process_status`` from the list endpoints."""
    return {"ok": True, "instance_code": "x", "status": status, "applicant": applicant, "form": form}


def _details(monkeypatch: pytest.MonkeyPatch, *replies: dict[str, Any]) -> list[str]:
    """Stub the instance-detail read, returning the queue of codes it was asked for."""
    asked: list[str] = []
    queue = list(replies)

    async def fake(instance_id: str, user_id_type: str = "open_id") -> dict[str, Any]:
        asked.append(instance_id)
        return queue.pop(0) if queue else {"ok": False, "message": "no more"}

    monkeypatch.setattr(_impl, "get_approval_instance_impl", fake)
    return asked


# ── 请假判定(数据源是假勤审批实例) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_leave_window_is_closed_on_both_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both endpoints count. An off-by-one here books somebody overdue on a day off."""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_zhang", _form("2026-08-05", "2026-08-07")))
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["ok"] is True
    person = out["on_leave"][0]
    assert person["hit_dates"] == ["2026-08-05", "2026-08-06", "2026-08-07"]
    assert person["hit_days"] == 3
    # 整周期请假 ⇒ 派发环节整块跳过(不派卡、不建任务)。
    assert person["full_period"] is True
    assert out["full_period_applicants"] == ["ou_zhang"]


@pytest.mark.asyncio
async def test_only_approved_applications_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """审批中的人还得上班,派活是对的;被拒和撤回的更不算。全算进去等于放宽考核。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1", "c2", "c3", "c4")]))
    _details(
        monkeypatch,
        _detail("ou_pending", _form("2026-08-05", "2026-08-07"), status="PENDING"),
        _detail("ou_rejected", _form("2026-08-05", "2026-08-07"), status="REJECTED"),
        _detail("ou_revoked", _form("2026-08-05", "2026-08-07"), status="REVERTED"),
        _detail("ou_ok", _form("2026-08-06", "2026-08-06")),
    )
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave_applicants"] == ["ou_ok"]
    # 一堆待审批要能看见,否则跟「没人请假」长得一样。
    assert out["skipped_not_approved"] == 3


@pytest.mark.asyncio
async def test_numeric_approved_status_also_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表/任务接口回的是数字 process_status(2 = 通过);两种表示法都得认。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_num", _form("2026-08-06", "2026-08-06"), status=2))
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave_applicants"] == ["ou_num"]


@pytest.mark.asyncio
async def test_blank_end_date_means_one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """只请一天时表单常常只填一个日期;那是「当天一天」,不是「请假到永远」。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_li", _form("2026-08-06", "")))
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    person = out["on_leave"][0]
    assert person["hit_dates"] == ["2026-08-06"]
    assert person["full_period"] is False


@pytest.mark.asyncio
async def test_date_range_widget_is_understood(monkeypatch: pytest.MonkeyPatch) -> None:
    """假勤表单常用一个「请假时间」区间控件,而不是成对的开始/结束控件。"""
    widgets = json.dumps(
        [{"id": "w1", "name": "请假时间", "type": "dayRange", "value": {"start": "2026-08-06", "end": "2026-08-07"}}],
        ensure_ascii=False,
    )
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_range", widgets))
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave"][0]["hit_dates"] == ["2026-08-06", "2026-08-07"]


@pytest.mark.asyncio
async def test_unreadable_dates_go_to_needs_fix_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """静默丢掉抽不到日期的申请 = 把「请了假」变成「没请假」,方向又是加重考核。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1", "c2")]))
    _details(
        monkeypatch,
        _detail("ou_bad", json.dumps([{"id": "w1", "name": "备注", "type": "textarea", "value": "下周想休"}])),
        _detail("ou_good", _form("2026-08-06", "2026-08-06")),
    )
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave_applicants"] == ["ou_good"]
    assert len(out["needs_fix"]) == 1
    assert out["needs_fix"][0]["applicant"] == "ou_bad"
    assert any("开始日期抽不到" in reason for reason in out["needs_fix"][0]["needs_fix"])


@pytest.mark.asyncio
async def test_leave_outside_the_window_is_not_a_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口按提交时间放宽取的实例,所以区间在窗口外的申请必须靠交集判掉。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_last_month", _form("2026-07-01", "2026-07-03")))
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave"] == []
    assert out["needs_fix"] == []


@pytest.mark.asyncio
async def test_two_applications_merge_into_one_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """分段请假是两条申请一个人;命中日期取并集,不是各报一次。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1", "c2")]))
    _details(
        monkeypatch,
        _detail("ou_split", _form("2026-08-05", "2026-08-05", kind="事假")),
        _detail("ou_split", _form("2026-08-07", "2026-08-07", kind="年假")),
    )
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    assert len(out["on_leave"]) == 1
    person = out["on_leave"][0]
    assert person["hit_dates"] == ["2026-08-05", "2026-08-07"]
    assert [leave["kind"] for leave in person["leaves"]] == ["事假", "年假"]
    assert person["full_period"] is False, "中间那天没请假,不能算整周期"


@pytest.mark.asyncio
async def test_person_without_an_application_is_not_on_leave(monkeypatch: pytest.MonkeyPatch) -> None:
    """没走审批 = 按未请假处理,由本人当场申诉。"""
    monkeypatch.setattr(_impl, "_invoke", _Invoke([_codes("c1")]))
    _details(monkeypatch, _detail("ou_zhang", _form("2026-08-06", "2026-08-06")))
    out = await _impl.query_leave_impl(
        APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07", names_json='["ou_sun"]'
    )
    assert out["on_leave"] == []
    assert out["on_leave_applicants"] == []


@pytest.mark.asyncio
async def test_instances_query_carries_code_and_millisecond_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_time/end_time 是 Unix 毫秒**字符串**,两个飞书都要;写错就是空结果。"""
    cap = _Invoke([_codes()])
    monkeypatch.setattr(_impl, "_invoke", cap)
    _details(monkeypatch)
    await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-05", date_to="2026-08-07")
    req = cap.requests[0]
    assert req.uri == "/open-apis/approval/v4/instances"
    query = dict(req.queries)
    assert query["approval_code"] == APPROVAL_CODE
    assert query["start_time"].isdigit() and query["end_time"].isdigit()
    assert int(query["start_time"]) < int(query["end_time"])


@pytest.mark.asyncio
async def test_missing_approval_code_says_where_to_get_it(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke([])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl("", date_from="2026-08-05")
    assert out["ok"] is False
    assert "tasks/query" in out["message"], "要告诉人 approval_code 从哪来"
    assert cap.requests == []


@pytest.mark.asyncio
async def test_reversed_window_is_refused_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke([])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(APPROVAL_CODE, date_from="2026-08-07", date_to="2026-08-05")
    assert out["ok"] is False
    assert cap.requests == [], "参数不合法时不该打出任何请求"


# ── 台账幂等与授权 ────────────────────────────────────────────────────────────


def _folder(*names: tuple[str, str, str]) -> dict[str, Any]:
    files = [{"name": name, "type": kind, "token": token} for name, kind, token in names]
    return {"ok": True, "code": 0, "data": {"files": files, "has_more": False}}


def _tables(*items: tuple[str, str]) -> dict[str, Any]:
    return {
        "ok": True,
        "code": 0,
        "data": {"items": [{"name": name, "table_id": tid} for name, tid in items], "has_more": False},
    }


def _members(*ids: tuple[str, str]) -> dict[str, Any]:
    return {"ok": True, "code": 0, "data": {"items": [{"member_id": mid, "perm": perm} for mid, perm in ids]}}


@pytest.mark.asyncio
async def test_second_run_reuses_ledger_and_issues_no_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等的支点:第二次跑**一个 create/copy 都不该发**,否则两张台账的数据从此分叉。"""
    cap = _Invoke(
        [
            _folder(("TODO台账-李四", "bitable", "bascnExisting")),
            _tables(("todo", "tblTodo1")),
            _members((MENTOR, "edit"), (BOSS, "view")),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四", FOLDER, boss_open_id=BOSS)
    assert out["ok"] is True
    assert out["reused"] is True and out["created"] is False
    assert out["app_token"] == "bascnExisting"
    assert out["table_id"] == "tblTodo1"
    assert not [uri for uri in cap.uris if uri.endswith("/copy") or uri == "/open-apis/bitable/v1/apps"]
    # 权限已经对了就不重复加(重复加会刷通知,还可能把 perm 改回去)。
    assert all(member["already"] for member in out["members"])


@pytest.mark.asyncio
async def test_first_run_copies_template_structure_only(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke(
        [
            _folder(),
            {"ok": True, "code": 0, "data": {"app": {"app_token": "bascnNew01"}}},
            _tables(("说明", "tblDoc"), ("TODO台账", "tblTodo9")),
            _members(),
            {"ok": True, "code": 0, "data": {}},
            {"ok": True, "code": 0, "data": {}},
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(
        MENTOR, "李四", FOLDER, boss_open_id=BOSS, template_app_token="bascnTemplate"
    )
    assert out["created"] is True and out["from_template"] is True
    assert out["name"] == "TODO台账-李四"
    copy_req = next(req for req in cap.requests if str(req.uri).endswith("/copy"))
    assert copy_req.paths["app_token"] == "bascnTemplate"
    assert copy_req.body["without_content"] is True, "模板复制只要结构,带内容会把示例数据带进台账"
    assert copy_req.body["folder_token"] == FOLDER
    # table_id 必须解析而不是取第一张表:模板里可能有说明表,顺序不保证。
    assert out["table_id"] == "tblTodo9"


@pytest.mark.asyncio
async def test_grants_are_exactly_mentor_edit_and_boss_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """多给一个人就是把别人下属的数据泄漏给他,所以这两条 perm 是断言而不是描述。"""
    cap = _Invoke(
        [
            _folder(("TODO台账-李四", "bitable", "bascnExisting")),
            _tables(("todo", "tblTodo1")),
            _members(),
            {"ok": True, "code": 0, "data": {}},
            {"ok": True, "code": 0, "data": {}},
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四", FOLDER, boss_open_id=BOSS)
    adds = [req for req in cap.requests if str(req.uri).endswith("/members") and req.http_method.name == "POST"]
    assert len(adds) == 2, "恰好两方:mentor 与 boss"
    by_id = {req.body["member_id"]: req.body for req in adds}
    assert by_id[MENTOR]["perm"] == "edit"
    assert by_id[BOSS]["perm"] == "view"
    for body in by_id.values():
        # member_type 是 "openid"(无下划线),body 的 type 是「成员是哪一类」。串了飞书不报哪个错。
        assert body["member_type"] == "openid"
        assert body["type"] == "user"
        assert body["perm"] != "full_access", "台账不需要所有者级权限"
    assert out["members_failed"] == []


@pytest.mark.asyncio
async def test_unexpected_collaborators_are_reported_not_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """撤权不可逆,所以多余协作者交人裁决 —— 工具只加不减。"""
    cap = _Invoke(
        [
            _folder(("TODO台账-李四", "bitable", "bascnExisting")),
            _tables(("todo", "tblTodo1")),
            _members((MENTOR, "edit"), (BOSS, "view"), ("ou_stranger", "edit")),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四", FOLDER, boss_open_id=BOSS)
    assert out["unexpected_members"] == ["ou_stranger"]
    assert out["ok"] is True, "多余协作者是要人裁决的事实,不是本次调用失败"
    assert not [req for req in cap.requests if req.http_method.name == "DELETE"]


@pytest.mark.asyncio
async def test_folder_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有固定文件夹就没有幂等的支点 —— 每次跑都可能建出新台账。"""
    cap = _Invoke([])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四", "")
    assert out["ok"] is False
    assert cap.requests == []


@pytest.mark.asyncio
async def test_forbidden_name_character_is_refused_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke([])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四/研发", FOLDER)
    assert out["ok"] is False
    assert "1254031" in out["message"]
    assert cap.requests == []


@pytest.mark.asyncio
async def test_missing_todo_table_reports_actual_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """table_id 解析失败不该让整个调用失败:base 是对的,只是表名不对,值得单独报。"""
    cap = _Invoke(
        [
            _folder(("TODO台账-李四", "bitable", "bascnExisting")),
            _tables(("Sheet1", "tblX")),
            _members((MENTOR, "edit")),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.ensure_mentor_ledger_impl(MENTOR, "李四", FOLDER)
    assert out["ok"] is True
    assert out["table_id"] == ""
    assert "Sheet1" in out["table_error"]


# ── 分块读表的翻页终止(采集这一步靠它) ────────────────────────────────────────
#
# 这组测试存在的理由是一个实测到的死循环:采集技能要求「跟着 has_more 读到底」,而
# Feishu 对**越界区间返回补空的满行**而不是空数组,所以「回的行数少于要的行数」这个
# 判据永远不成立 —— 一张 198 行的表被读到第 51200 行仍然 has_more=True。终止条件必须
# 以工作表自己的 row_count 为准。


def _grid_meta(sheet_id: str, row_count: int) -> dict[str, Any]:
    return {
        "ok": True,
        "code": 0,
        "data": {"sheets": [{"sheet_id": sheet_id, "title": "Sheet1", "grid_properties": {"row_count": row_count}}]},
    }


def _grid_values(sheet_id: str, rows: list[list[str]], rng: str) -> dict[str, Any]:
    return {"ok": True, "code": 0, "data": {"valueRange": {"range": f"{sheet_id}!{rng}", "values": rows}}}


@pytest.mark.asyncio
async def test_grid_read_stops_at_the_sheets_real_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """198 行的表读到第 151 行就该收尾 —— 靠元数据行数,不靠「回了几行」。"""
    padded = [["", ""] for _ in range(50)]  # 飞书对越界区间回的就是这种补空满行
    cap = _Invoke([_grid_meta("46a582", 198), _grid_values("46a582", padded, "A151:ZZ198")])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.read_sheet_grid_impl(token="shtX", range_="46a582", max_rows=50, start_row=151)
    assert out["ok"] is True
    assert out["sheet_row_count"] == 198
    assert out["has_more"] is False, "读到表尾就必须停,否则调用方会一路翻到 5 万行"
    assert out["next_start_row"] is None
    # 请求区间被夹到真实末行,不再问 A151:ZZ200。
    assert cap.requests[-1].paths["range"] == "46a582!A151:ZZ198"


@pytest.mark.asyncio
async def test_grid_read_past_the_end_returns_empty_not_padding(monkeypatch: pytest.MonkeyPatch) -> None:
    """越界不是错误(翻页最后一步天然会问到),但必须回空且 has_more=False。"""
    cap = _Invoke([_grid_meta("46a582", 198)])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.read_sheet_grid_impl(token="shtX", range_="46a582", max_rows=50, start_row=1000)
    assert out["ok"] is True
    assert out["rows"] == [] and out["row_count"] == 0
    assert out["has_more"] is False
    # 越界时连值请求都不该发出去。
    assert len(cap.requests) == 1


@pytest.mark.asyncio
async def test_grid_read_still_pages_while_rows_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    """修翻页终止不能顺手把翻页本身关掉:表还没读完时 has_more 必须为真。"""
    rows = [["x"] for _ in range(50)]
    cap = _Invoke([_grid_meta("46a582", 198), _grid_values("46a582", rows, "A1:ZZ50")])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.read_sheet_grid_impl(token="shtX", range_="46a582", max_rows=50, start_row=1)
    assert out["has_more"] is True
    assert out["next_start_row"] == 51


@pytest.mark.asyncio
async def test_grid_read_without_metadata_falls_back_to_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到元数据行数时退回旧判据,而不是把整张表当空的。"""
    rows = [["x"] for _ in range(50)]
    cap = _Invoke(
        [
            {"ok": True, "code": 0, "data": {"sheets": [{"sheet_id": "46a582", "title": "Sheet1"}]}},
            _grid_values("46a582", rows, "A1:ZZ50"),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.read_sheet_grid_impl(token="shtX", range_="46a582", max_rows=50, start_row=1)
    assert out["row_count"] == 50
    assert "sheet_row_count" not in out
    assert out["has_more"] is True


# ── 三个技能 ──────────────────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body). Only top-level ``key: value`` lines are parsed."""
    assert text.startswith("---\n"), "SKILL.md must start with a YAML frontmatter fence"
    end = text.index("\n---", 4)
    fm: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line or line[0] in " \t":  # skip blanks and continuation/indented lines
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"')
    return fm, text[end + 4 :]


def _public_tool_names() -> set[str]:
    """Public async tool functions (feishu_*/wiki_*/schedule_*) from tools/*.py via AST."""
    names: set[str] = set()
    for py in TOOLS_DIR.glob("*.py"):
        if py.name.startswith("_"):
            continue
        for node in ast.parse(py.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_"):
                names.add(node.name)
    return names


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_frontmatter_is_well_formed(skill: str) -> None:
    path = SKILLS_DIR / skill / "SKILL.md"
    assert path.is_file(), f"missing skills/{skill}/SKILL.md"
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm.get("name") == skill, "frontmatter name must equal dir name"
    assert fm.get("description", "").strip(), f"{skill} needs a non-empty description"
    assert fm.get("category", "").strip(), f"{skill} needs a category"
    assert body.strip(), f"{skill} needs a non-empty body"


@pytest.mark.parametrize("skill", SKILLS)
def test_every_tool_the_skill_names_exists(skill: str) -> None:
    """A renamed tool would leave the prose pointing at nothing, and it fails in production."""
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    available = _public_tool_names()
    referenced = set(re.findall(r"`((?:feishu|wiki|schedule)_[a-z0-9_]+)`", text))
    # 端点表里的域名(如 feishu-task)不是工具;只校验反引号里的下划线命名。
    missing = {name for name in referenced if name not in available}
    assert not missing, f"{skill} 引用了不存在的工具: {sorted(missing)}"


def test_sync_skill_pins_the_two_new_tools() -> None:
    text = (SKILLS_DIR / "company-todo-sync" / "SKILL.md").read_text(encoding="utf-8")
    assert "feishu_leave_query" in text
    assert "feishu_mentor_ledger_ensure" in text


def test_sync_skill_keeps_dispatch_order_task_before_card() -> None:
    """卡片行链接指向任务;任务不存在就没有可点的目标。顺序颠倒过一次就说明这段被改坏了。"""
    text = (SKILLS_DIR / "company-todo-sync" / "SKILL.md").read_text(encoding="utf-8")
    assert text.index("先建飞书任务") < text.index("再发 TODO 卡")
    assert text.index("feishu_leave_query") < text.index("feishu_todo_card_send"), "判假必须在派发之前"


def test_sync_skill_states_wiki_has_no_append() -> None:
    """「只增不覆盖」全靠这条约束;丢了它,汇总页会被整页覆盖而历史索引消失。"""
    text = (SKILLS_DIR / "company-todo-sync" / "SKILL.md").read_text(encoding="utf-8")
    assert "只有整页覆盖" in text and "没有 append" in text
    assert "串行" in text, "同一人汇总页并发改写会丢索引"


def test_audit_skill_lists_all_five_closure_elements() -> None:
    """五要素少一条,「已闭环」这个数字就再也不能用来做判断。"""
    text = (SKILLS_DIR / "company-todo-audit" / "SKILL.md").read_text(encoding="utf-8")
    for marker in ("验收人", "截止时间", "completed_at", "打分", "wiki"):
        assert marker in text, f"闭环五要素缺了 {marker} 那一项"
    assert "不许静默改截止日" in text
    assert "请假顺延" in text and "仍然回流" in text


def test_review_skill_keeps_mentor_score_authoritative() -> None:
    text = (SKILLS_DIR / "company-todo-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "以 mentor 为准" in text or "权威分" in text
    assert "不要替 mentor 打分" in text

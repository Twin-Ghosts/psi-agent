"""The 公司 TODO 体系 surface: leave adjudication, ledger idempotency, three skills.

What earns these tests their own file is that both new tools exist to make a *decision*
unique — the kind of decision that, left to a model, is silently wrong in the direction of
punishing people:

- ``feishu_leave_query`` decides which days count as leave. Overlapping-interval arithmetic
  redone every cycle eventually assigns work to somebody on holiday and books them overdue.
  So the closed-interval rule, the blank-结束日期 default, and the "unparseable row goes to
  needs_fix rather than vanishing" rule are each pinned here. A row that vanishes turns
  "filled in wrong" into "not on leave" — same wrong direction.
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

SHEET_TOKEN = "shtcnLeave01"
LEAVE_SHEET_ID = "lv0001"
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


def _meta(*titles: str) -> dict[str, Any]:
    sheets = [{"sheet_id": LEAVE_SHEET_ID if "请假" in t else f"sh{i}", "title": t} for i, t in enumerate(titles)]
    return {"ok": True, "code": 0, "data": {"sheets": sheets}}


def _values(rows: list[list[str]]) -> dict[str, Any]:
    return {"ok": True, "code": 0, "data": {"valueRange": {"range": f"{LEAVE_SHEET_ID}!A1:F9", "values": rows}}}


_LEAVE_HEADER = ["姓名", "开始日期", "结束日期", "类型", "是否整天", "备注"]


# ── 请假判定 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leave_window_is_closed_on_both_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both endpoints count. An off-by-one here books somebody overdue on a day off."""
    cap = _Invoke(
        [
            _meta("TODO LIST", "请假表"),
            _values([_LEAVE_HEADER, ["张三", "2026-08-05", "2026-08-07", "年假", "", ""]]),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05", date_to="2026-08-07")
    assert out["ok"] is True
    person = out["on_leave"][0]
    assert person["hit_dates"] == ["2026-08-05", "2026-08-06", "2026-08-07"]
    assert person["hit_days"] == 3
    # 整周期请假 ⇒ 派发环节整块跳过(不派卡、不建任务)。
    assert person["full_period"] is True
    assert out["full_period_names"] == ["张三"]


@pytest.mark.asyncio
async def test_blank_end_date_means_one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """填了开始没填结束是最常见的填法;它是「当天请假一天」,不是「请假到永远」。"""
    cap = _Invoke([_meta("请假表"), _values([_LEAVE_HEADER, ["李四", "2026-08-06", "", "事假", "", ""]])])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05", date_to="2026-08-07")
    person = out["on_leave"][0]
    assert person["hit_dates"] == ["2026-08-06"]
    assert person["full_period"] is False


@pytest.mark.asyncio
async def test_unparseable_row_goes_to_needs_fix_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """静默丢掉填错的行 = 把「填错了」变成「没请假」,方向又是加重考核。"""
    cap = _Invoke(
        [
            _meta("请假表"),
            _values(
                [
                    _LEAVE_HEADER,
                    ["王五", "下周一", "", "事假", "", ""],
                    ["", "2026-08-06", "2026-08-06", "病假", "", "忘了写名字"],
                    ["赵六", "2026-08-06", "2026-08-06", "病假", "否", "半天"],
                ]
            ),
        ]
    )
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05", date_to="2026-08-07")
    assert out["on_leave_names"] == ["赵六"]
    assert len(out["needs_fix"]) == 2, "认不出日期的行和缺姓名的行都要报出来"
    assert [row["name"] for row in out["needs_fix"]] == ["王五", ""]
    assert any("开始日期认不出" in " ".join(row["needs_fix"]) for row in out["needs_fix"])
    assert any("缺姓名" in " ".join(row["needs_fix"]) for row in out["needs_fix"])
    # 「是否整天」写「否」= 半天,空着才是整天。
    assert out["on_leave"][0]["leaves"][0]["full_day"] is False


@pytest.mark.asyncio
async def test_person_absent_from_sheet_is_not_on_leave(monkeypatch: pytest.MonkeyPatch) -> None:
    """漏填按未请假处理 —— 宁可漏填时报逾期,由本人当场申诉。"""
    cap = _Invoke([_meta("请假表"), _values([_LEAVE_HEADER, ["张三", "2026-08-06", "2026-08-06", "年假", "", ""]])])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05", date_to="2026-08-07", names_json='["孙七"]')
    assert out["on_leave"] == []
    assert out["on_leave_names"] == []


@pytest.mark.asyncio
async def test_missing_leave_sheet_lists_actual_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """找不到子表时要说出实际有哪些表,否则分不清「没建」还是「名字不一样」。"""
    cap = _Invoke([_meta("TODO LIST", "汇总")])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05")
    assert out["ok"] is False
    assert "TODO LIST" in out["message"] and "汇总" in out["message"]


@pytest.mark.asyncio
async def test_header_missing_required_column_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke([_meta("请假表"), _values([["类型", "备注"], ["年假", "x"]])])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-05")
    assert out["ok"] is False
    assert "姓名" in out["message"] and "开始日期" in out["message"]


@pytest.mark.asyncio
async def test_reversed_window_is_refused_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _Invoke([])
    monkeypatch.setattr(_impl, "_invoke", cap)
    out = await _impl.query_leave_impl(SHEET_TOKEN, date_from="2026-08-07", date_to="2026-08-05")
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

"""新人入职 SOP 卡片与日报。"""

from __future__ import annotations

# ruff: noqa: RUF002, RUF003
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import anyio

HAITUN = Path(__file__).resolve().parents[1]
TOOLS = HAITUN / "tools"


def _load(module_name: str) -> Any:
    """按文件路径加载 workspace 工具模块（它们不是包，靠 sys.path 找同级依赖）。"""
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    path = TOOLS / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"{module_name}_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Py3.14 的 dataclasses 内部按 sys.modules[cls.__module__] 查找命名空间，
    # exec_module 前不注册会在 @dataclass 处抛 AttributeError。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_CFG: dict[str, Any] = {
    "modules": [
        {
            "name": "环境准备",
            "window_days": 1,
            "items": [
                {"id": "wifi", "title": "连上 WiFi", "acceptance": "能上网"},
                {"id": "desk", "title": "找到工位", "acceptance": "知道座位"},
            ],
        },
        {
            "name": "核心制度",
            "window_days": 3,
            "items": [{"id": "attendance", "title": "了解考勤", "acceptance": "知道打卡"}],
        },
        {
            "name": "开发环境",
            "window_days": 7,
            "items": [
                {"id": "git_workflow", "title": "Git 工作流", "acceptance": "会提 PR", "dev_only": True},
            ],
        },
    ]
}


def test_load_sop_flattens_modules_and_marks_dev_only() -> None:
    cfg = _load("_rookie_sop_config")
    items = cfg.load_sop(_CFG)

    assert [i.item_id for i in items] == ["wifi", "desk", "attendance", "git_workflow"]
    assert items[0].module == "环境准备"
    assert items[0].acceptance == "能上网"
    assert items[0].window_days == 1
    assert items[0].dev_only is False
    assert items[3].dev_only is True
    assert items[3].window_days == 7


def test_due_date_is_inclusive_of_the_first_day() -> None:
    cfg = _load("_rookie_sop_config")
    # 「Day 1」窗口 1 天 → 当天截止；「Day 1-3」窗口 3 天 → 入职日 +2
    assert cfg.due_date(date(2026, 8, 5), 1) == date(2026, 8, 5)
    assert cfg.due_date(date(2026, 8, 5), 3) == date(2026, 8, 7)
    # 跨月
    assert cfg.due_date(date(2026, 8, 30), 7) == date(2026, 9, 5)


def test_day_index_counts_natural_days_from_one() -> None:
    cfg = _load("_rookie_sop_config")
    assert cfg.day_index(date(2026, 8, 5), date(2026, 8, 5)) == 1
    assert cfg.day_index(date(2026, 8, 5), date(2026, 8, 7)) == 3
    # 跨月
    assert cfg.day_index(date(2026, 8, 30), date(2026, 9, 2)) == 4


def test_applicable_items_filters_dev_only_unless_role_is_dev() -> None:
    cfg = _load("_rookie_sop_config")
    items = cfg.load_sop(_CFG)

    assert [i.item_id for i in cfg.applicable_items(items, "dev")] == [
        "wifi",
        "desk",
        "attendance",
        "git_workflow",
    ]
    assert [i.item_id for i in cfg.applicable_items(items, "nondev")] == ["wifi", "desk", "attendance"]
    # 角色未确认时也不计入分母
    assert [i.item_id for i in cfg.applicable_items(items, "")] == ["wifi", "desk", "attendance"]


def _row(item_id: str, status: str, due: date, title: str = "", module: str = "环境准备") -> dict[str, Any]:
    return {
        "记录键": f"ou_x:{item_id}",
        "姓名": "张三",
        "open_id": "ou_x",
        "模块": module,
        "项": title or item_id,
        "验收标准": "验收",
        "状态": status,
        "入职日": date(2026, 8, 5),
        "截止日": due,
    }


def test_summarize_counts_done_and_excludes_na_from_denominator() -> None:
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5)),
        _row("desk", p.STATUS_TODO, date(2026, 8, 5)),
        _row("git_workflow", p.STATUS_NA, date(2026, 8, 11), module="开发环境"),
    ]

    got = p.summarize(rows, date(2026, 8, 5))

    # 不适用的行既不进分子也不进分母
    assert (got.done, got.total) == (1, 2)
    assert got.percent == 50
    assert got.all_done is False


def test_summarize_splits_overdue_due_today_and_next_due() -> None:
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_TODO, date(2026, 8, 5)),         # 逾期
        _row("desk", p.STATUS_TODO, date(2026, 8, 7)),         # 今天到期
        _row("attendance", p.STATUS_TODO, date(2026, 8, 9)),   # 未来
        _row("todo_update", p.STATUS_DONE, date(2026, 8, 6)),  # 已完成, 不算逾期
    ]

    got = p.summarize(rows, date(2026, 8, 7))

    assert [r["项"] for r in got.overdue] == ["wifi"]
    assert [r["项"] for r in got.due_today] == ["desk"]
    assert got.next_due is not None and got.next_due["项"] == "attendance"


def test_summarize_all_done_ignores_na_rows() -> None:
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5)),
        _row("git_workflow", p.STATUS_NA, date(2026, 8, 11), module="开发环境"),
    ]

    got = p.summarize(rows, date(2026, 8, 6))

    assert got.all_done is True
    assert (got.done, got.total, got.percent) == (1, 1, 100)


def test_summarize_empty_rows_does_not_divide_by_zero() -> None:
    p = _load("_rookie_sop_progress")
    got = p.summarize([], date(2026, 8, 5))

    assert (got.done, got.total, got.percent) == (0, 0, 0)
    # 没有任何适用项时不能报「全部完成」, 否则会误发出新手村卡
    assert got.all_done is False


def test_overview_fields_projects_a_one_row_summary() -> None:
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5)),
        _row("desk", p.STATUS_TODO, date(2026, 8, 5)),
        _row("attendance", p.STATUS_TODO, date(2026, 8, 7)),
    ]

    got = p.overview_fields(rows, date(2026, 8, 7), "张三", "ou_x", "dev")

    assert got["open_id"] == "ou_x"
    assert got["姓名"] == "张三"
    assert got["角色"] == "研发"
    assert got["进度"] == "1/3"
    assert got["完成率"] == 33
    assert got["逾期项数"] == 1
    assert got["逾期项"] == "desk"
    assert got["状态"] == "进行中"
    assert got["入职第N天"] == 3


def test_overview_fields_marks_graduated_when_all_done() -> None:
    p = _load("_rookie_sop_progress")
    rows = [_row("wifi", p.STATUS_DONE, date(2026, 8, 5))]

    got = p.overview_fields(rows, date(2026, 8, 6), "张三", "ou_x", "nondev")

    assert got["状态"] == "已出新手村"
    assert got["角色"] == "非研发"
    assert got["逾期项数"] == 0
    assert got["逾期项"] == ""


def test_overview_fields_role_unset_shows_pending() -> None:
    p = _load("_rookie_sop_progress")
    rows = [_row("wifi", p.STATUS_TODO, date(2026, 8, 5))]

    got = p.overview_fields(rows, date(2026, 8, 5), "张三", "ou_x", "")

    assert got["角色"] == "待确认"


def _dump(card: dict[str, Any]) -> str:
    return json.dumps(card, ensure_ascii=False)


def test_module_card_gives_each_unfinished_row_its_own_action() -> None:
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5), title="连上 WiFi"),
        _row("desk", p.STATUS_TODO, date(2026, 8, 5), title="找到工位"),
    ]

    card, handlers = c.module_card("环境准备", rows, "1/2", "Day 1 截止", "https://sop.example/doc")

    # 每个未完成行一个唯一 action, 全部指向同一个 handler
    assert handlers == {"rookie_tick_desk": "rookie_sop_tick"}
    rendered = _dump(card)
    # 已完成行渲染成实心 + 删除线且不给按钮
    assert "~~连上 WiFi~~" in rendered
    assert "rookie_tick_wifi" not in rendered
    # 未完成行带验收标准与按钮
    assert "找到工位" in rendered
    assert "验收" in rendered
    assert card["config"]["update_multi"] is True


def test_module_card_all_done_turns_header_green() -> None:
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [_row("wifi", p.STATUS_DONE, date(2026, 8, 5))]

    card, handlers = c.module_card("环境准备", rows, "1/1", "Day 1 截止", "")

    assert handlers == {}
    assert card["header"]["template"] == "green"


def test_role_card_carries_role_choices_and_dev_items_on_one_card() -> None:
    """一张卡装完角色选择与开发项 —— 两类 action 共存于同一个 handlers 里。

    multi_use 按 action 逐个消费, 点掉一个角色按钮只退休那一个, 5 个开发项照旧可点,
    所以不必先发角色卡再发第二张。
    """
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    dev_rows = [
        _row("git_workflow", p.STATUS_TODO, date(2026, 8, 12), title="了解 Git 工作流", module="开发环境"),
        _row("repo_access", p.STATUS_TODO, date(2026, 8, 12), title="开通仓库权限", module="开发环境"),
    ]

    card, handlers = c.role_card("Day 1-7 截止", dev_rows=dev_rows, progress_text="0/2")

    # 角色的两个 action 与开发项的 tick action 同在一张卡上
    assert handlers == {
        "rookie_role_dev": "rookie_sop_role_set",
        "rookie_role_nondev": "rookie_sop_role_set",
        "rookie_tick_git_workflow": "rookie_sop_tick",
        "rookie_tick_repo_access": "rookie_sop_tick",
    }
    rendered = _dump(card)
    assert "研发人员" in rendered
    assert "非研发人员" in rendered
    # 开发项的文字写在按钮里(而不是另起一块 markdown + 通用的「标记完成」按钮)
    assert "了解 Git 工作流" in rendered
    assert "标记完成" not in rendered


def test_role_card_hides_role_confirmed_row_before_the_role_is_answered() -> None:
    """role_confirmed 就是「角色是否已答」本身, 未答时由两个角色按钮代表,
    不再单独给它一个勾选按钮 —— 否则同一件事有两个可点动作。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("role_confirmed", p.STATUS_TODO, date(2026, 8, 12), title="确认是否为技术人员", module="开发环境"),
        _row("git_workflow", p.STATUS_TODO, date(2026, 8, 12), title="了解 Git 工作流", module="开发环境"),
    ]

    _card_json, handlers = c.role_card("Day 1-7 截止", dev_rows=rows, progress_text="0/2")

    assert "rookie_tick_role_confirmed" not in handlers
    assert "rookie_tick_git_workflow" in handlers


def test_role_settled_card_for_nondev_is_terminal_and_has_no_buttons() -> None:
    c = _load("_rookie_sop_card")

    card, handlers = c.role_settled_card(False, [], "Day 1-7 截止", "")

    assert handlers == {}
    rendered = _dump(card)
    assert "不适用" in rendered
    assert "rookie_tick_" not in rendered


def test_role_settled_card_for_dev_expands_the_five_items() -> None:
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("git_workflow", p.STATUS_TODO, date(2026, 8, 11), title="Git 工作流", module="开发环境"),
        _row("repo_access", p.STATUS_TODO, date(2026, 8, 11), title="开通仓库权限", module="开发环境"),
    ]

    _card, handlers = c.role_settled_card(True, rows, "Day 1-7 截止", "")

    assert handlers == {
        "rookie_tick_git_workflow": "rookie_sop_tick",
        "rookie_tick_repo_access": "rookie_sop_tick",
    }


def test_role_settled_card_for_nondev_shows_role_confirmed_as_done_with_no_button() -> None:
    """非研发终态卡也要能看到「角色已确认」这一勾, 但它不能带按钮(一次点击够了)。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    role_row = _row("role_confirmed", p.STATUS_DONE, date(2026, 8, 11), title="确认角色", module="开发环境")

    card, handlers = c.role_settled_card(False, [], "Day 1-7 截止", "", role_row)

    assert handlers == {}
    rendered = _dump(card)
    assert "确认角色" in rendered
    assert "rookie_tick_" not in rendered
    assert "rookie_role_" not in rendered


def test_role_settled_card_for_dev_shows_role_confirmed_done_alongside_five_live_rows() -> None:
    """研发展开卡里, role_confirmed 已完成且不占用一个可点的 handler, 五个开发项仍都可点。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    role_row = _row("role_confirmed", p.STATUS_DONE, date(2026, 8, 11), title="确认角色", module="开发环境")
    rows = [
        _row("git_workflow", p.STATUS_TODO, date(2026, 8, 11), title="Git 工作流", module="开发环境"),
        _row("repo_access", p.STATUS_TODO, date(2026, 8, 11), title="开通仓库权限", module="开发环境"),
    ]

    card, handlers = c.role_settled_card(True, rows, "Day 1-7 截止", "", role_row)

    assert handlers == {
        "rookie_tick_git_workflow": "rookie_sop_tick",
        "rookie_tick_repo_access": "rookie_sop_tick",
    }
    rendered = _dump(card)
    assert "确认角色" in rendered
    assert "rookie_tick_role_confirmed" not in rendered
    # 进度分子分母把 role_confirmed 算进去: 1 个已完成(role_confirmed) + 2 个未完成 = 1/3
    assert "已完成 1 / 3 项" in rendered  # 进度改成纯数字, 不再画 ▰▱ 方块条


def test_remind_card_sections_overdue_and_due_today() -> None:
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_TODO, date(2026, 8, 5), title="连上 WiFi"),
        _row("desk", p.STATUS_TODO, date(2026, 8, 7), title="找到工位"),
        _row("attendance", p.STATUS_TODO, date(2026, 8, 9), title="了解考勤"),
    ]
    progress = p.summarize(rows, date(2026, 8, 7))

    card, handlers = c.remind_card("张三", 3, progress, "")

    rendered = _dump(card)
    assert "已逾期" in rendered
    assert "今天到期" in rendered
    assert "下一个到期" in rendered
    assert "入职第 3 天" in rendered
    # 催办卡也是 multi_use, 逾期与今日到期的行都能直接勾
    assert handlers == {
        "rookie_tick_wifi": "rookie_sop_tick",
        "rookie_tick_desk": "rookie_sop_tick",
    }


def test_digest_card_lists_one_line_per_rookie_and_links_the_table() -> None:
    c = _load("_rookie_sop_card")
    overview = [
        {"姓名": "张三", "进度": "17/18", "逾期项数": 0, "状态": "已出新手村", "入职第N天": 8, "逾期项": ""},
        {"姓名": "李四", "进度": "9/18", "逾期项数": 2, "状态": "进行中", "入职第N天": 5, "逾期项": "工位、校园卡"},
    ]

    card, handlers = c.digest_card(overview, "https://feishu.cn/base/bascnXXX", "8月5日")

    assert handlers == {}
    rendered = _dump(card)
    assert "张三" in rendered and "李四" in rendered
    assert "17/18" in rendered
    assert "工位、校园卡" in rendered
    assert "https://feishu.cn/base/bascnXXX" in rendered
    # 表格链接是普通跳转按钮, 不能是交互 action
    assert "rookie_" not in rendered


def test_graduation_card_has_no_actions() -> None:
    c = _load("_rookie_sop_card")

    card, handlers = c.graduation_card("张三", 18)

    assert handlers == {}
    assert "新手村" in _dump(card)


class _FakeBitable:
    """假的 bitable 适配器: 只记录调用并按预设返回, 不碰飞书。

    返回形状照抄 ``_feishu_impl.py`` 里真实工具的样子 —— 扁平, 没有 "result" 包装:

    - ``search_bitable_records_impl`` → ``{ok, records, count, has_more, page_token, total}``
    - ``create_bitable_records_impl``  → ``{ok, created: [record_id, ...], count}``
    - ``update_bitable_records_impl``  → ``{ok, updated: [record_id, ...], count}``

    ``search_results`` 里每一页默认是「一批 item 字典」(单页, has_more=False,
    向后兼容原有测试); 若要测多页翻页, 传三元组
    ``(items, has_more, page_token)`` 代替。

    ``fail_writes`` 为真时, ``create_records``/``update_records`` 返回
    ``{"ok": False, "message": ...}`` —— 用来测「写被拒绝时调用方不能假装成功」
    这条规则(FIX 3), 不用另起一个假适配器。
    """

    def __init__(
        self,
        search_results: list[list[dict[str, Any]] | tuple[list[dict[str, Any]], bool, str]] | None = None,
        *,
        fail_writes: bool = False,
    ) -> None:
        self._search_results = list(search_results or [])
        self.searches: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._fail_writes = fail_writes

    async def search_records(
        self,
        app_token: str,
        table_id: str,
        filter_json: str = "",
        sort_json: str = "",
        field_names: str = "",
        view_id: str = "",
        page_size: int = 100,
        page_token: str = "",
        automatic_fields: bool = False,
        user_key: str = "",
    ) -> str:
        self.searches.append({"table_id": table_id, "filter_json": filter_json, "page_token": page_token})
        page = self._search_results.pop(0) if self._search_results else []
        if isinstance(page, tuple):
            items, has_more, next_token = page
        else:
            items, has_more, next_token = page, False, ""
        return json.dumps(
            {
                "ok": True,
                "records": items,
                "count": len(items),
                "has_more": has_more,
                "page_token": next_token,
                "total": len(items),
            },
            ensure_ascii=False,
        )

    async def create_records(
        self,
        app_token: str,
        table_id: str,
        records_json: str,
        user_key: str = "",
        identity: str = "",
        validate_fields: bool = True,
    ) -> str:
        records = json.loads(records_json)
        self.creates.append({"table_id": table_id, "records": records})
        if self._fail_writes:
            return json.dumps({"ok": False, "message": "fake create_records rejected"}, ensure_ascii=False)
        created = [f"recNew{n}" for n in range(len(self.creates[-1]["records"]))]
        return json.dumps({"ok": True, "created": created, "count": len(created)}, ensure_ascii=False)

    async def update_records(
        self,
        app_token: str,
        table_id: str,
        records_json: str,
        user_key: str = "",
        identity: str = "",
        validate_fields: bool = True,
    ) -> str:
        records = json.loads(records_json)
        self.updates.append({"table_id": table_id, "records": records})
        if self._fail_writes:
            return json.dumps({"ok": False, "message": "fake update_records rejected"}, ensure_ascii=False)
        updated = [str(r.get("record_id") or "") for r in records if isinstance(r, dict)]
        return json.dumps({"ok": True, "updated": updated, "count": len(updated)}, ensure_ascii=False)


# 两张表里哪些字段是文本(type 1, search_records 回来是富文本片段数组), 哪些是
# 单选/数字/日期(标量) —— 照抄 _rookie_sop_store.py 的 DETAIL_FIELDS/OVERVIEW_FIELDS
# 类型声明。两表都有的字段里只有 状态 类型不同(明细表 3 单选, 总览表 1 文本),
# 所以 _item() 必须按 table 分开查, 不能用一份合并的字段名集合。
_DETAIL_TEXT_FIELDS = {"记录键", "姓名", "open_id", "模块", "项", "验收标准", "Mentor", "适用角色"}
_OVERVIEW_TEXT_FIELDS = {"open_id", "姓名", "角色", "进度", "逾期项", "状态"}


def _wrap_text(value: Any) -> Any:
    """把一个文本字段的值包成飞书 search_records 真实吐出来的富文本片段数组形状。"""
    if value is None:
        return value
    return [{"text": str(value), "type": "text"}]


def _item(record_id: str, fields: dict[str, Any], *, table: str = "detail") -> dict[str, Any]:
    """假造一条 search_records 记录 —— 文本字段(type 1)包成富文本片段数组,
    单选/数字/日期字段保持标量, 照抄真实飞书表格观测到的形状(而不是让测试的假
    数据比真实 API 更老实)。``table`` 选 "detail" 或 "overview" 决定用哪张表的
    文本字段名单, 因为 状态 在两张表里的字段类型不同(明细表单选, 总览表文本)。
    """
    text_fields = _DETAIL_TEXT_FIELDS if table == "detail" else _OVERVIEW_TEXT_FIELDS
    wrapped = {k: (_wrap_text(v) if k in text_fields else v) for k, v in fields.items()}
    return {"record_id": record_id, "fields": wrapped}


def test_row_of_unwraps_richtext_segments_from_a_realistic_record() -> None:
    """回归测试: 直接照抄联调时在真实飞书表格上 search_records 观测到的形状 ——
    文本字段(type 1)是富文本片段数组, 单选(状态, type 3)与日期(入职日/截止日,
    type 5)是标量。撤掉 _row_of 里的 _unwrap_text 会让这条断言失败(见 richtext
    修复报告), 这正是「明细表每个模块卡都是 0/0」那个线上 bug 的根源。
    """
    s = _load("_rookie_sop_store")
    record = {
        "record_id": "recLive1",
        "fields": {
            "模块": [{"text": "环境准备", "type": "text"}],
            "记录键": [{"text": "ou_a6875df821ff538b9db67c2a5cd5f428:wifi", "type": "text"}],
            # 多段文本必须按顺序拼接, 不是只取第一段
            "项": [{"text": "连上 ", "type": "text"}, {"text": "WiFi", "type": "text"}],
            "姓名": [{"text": "王炜博", "type": "text"}],
            "open_id": [{"text": "ou_a6875df821ff538b9db67c2a5cd5f428", "type": "text"}],
            "验收标准": [{"text": "能上网", "type": "text"}],
            "适用角色": [{"text": "全员", "type": "text"}],
            "状态": "未完成",  # 单选(type 3), 已经是纯字符串
            "入职日": 1785945600000,  # 日期(type 5), 毫秒时间戳
            "截止日": 1785945600000,
        },
    }

    row = s._row_of(record)

    assert row["模块"] == "环境准备"
    assert row["记录键"] == "ou_a6875df821ff538b9db67c2a5cd5f428:wifi"
    assert row["项"] == "连上 WiFi"
    assert row["姓名"] == "王炜博"
    assert row["open_id"] == "ou_a6875df821ff538b9db67c2a5cd5f428"
    assert row["验收标准"] == "能上网"
    assert row["适用角色"] == "全员"
    # 单选字段本就是字符串, 不能被误当成富文本再拆一遍
    assert row["状态"] == "未完成"
    # 日期字段的 millis→date 转换不受影响
    assert row["入职日"] == date(2026, 8, 6)
    assert row["截止日"] == date(2026, 8, 6)
    assert row["record_id"] == "recLive1"


def test_row_of_unwrap_text_degrades_sensibly_on_edge_shapes() -> None:
    """_unwrap_text 的防御性分支: 空列表、裸字符串分段、已经是纯字符串、
    None、非列表标量都不能让 _row_of 抛异常, 且都要落到合理的值上。"""
    s = _load("_rookie_sop_store")

    assert s._unwrap_text([]) == ""
    assert s._unwrap_text(["裸字符串", "分段"]) == "裸字符串分段"
    assert s._unwrap_text("已经是纯字符串") == "已经是纯字符串"
    assert s._unwrap_text(None) is None
    assert s._unwrap_text(42) == 42
    # 混杂 dict/裸字符串/脏元素(None)的分段: 按顺序拼接, 脏元素跳过不拼入
    assert s._unwrap_text([{"text": "A", "type": "text"}, "B", None, {"text": "C", "type": "text"}]) == "ABC"


def test_millis_roundtrip_preserves_the_date() -> None:
    s = _load("_rookie_sop_store")
    assert s.from_millis(s.to_millis(date(2026, 8, 5))) == date(2026, 8, 5)
    assert s.to_millis(None) is None
    assert s.from_millis(None) is None
    assert s.from_millis("") is None


def test_detail_fields_put_a_text_key_column_first() -> None:
    s = _load("_rookie_sop_store")
    # 飞书要求索引列(第一列)是 1/2/5/13/15/20/22 之一 —— 这里必须是文本 1
    assert s.DETAIL_FIELDS[0]["field_name"] == "记录键"
    assert s.DETAIL_FIELDS[0]["type"] == 1
    assert s.OVERVIEW_FIELDS[0]["field_name"] == "open_id"
    assert s.OVERVIEW_FIELDS[0]["type"] == 1
    # 「查找引用」(19) API 建不出来, 不许出现
    assert all(f["type"] != 19 for f in s.DETAIL_FIELDS + s.OVERVIEW_FIELDS)


def test_detail_row_fields_seeds_dev_only_items_as_todo_not_na() -> None:
    """开发环境项也种成 未完成 —— 它们与角色选择同在一张卡上、Day 1 就可点。

    早先是种成 不适用 让 Day 1 分母显示 28, 但那与「一张卡上 5 项立即可点」矛盾:
    可点却不计分母, 新人看到的进度会对不上。现在 Day 1 是 33, 选「非研发人员」后
    由 mark_module_na 标不适用、降到 28。
    """
    s = _load("_rookie_sop_store")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    items = cfg.load_sop(_CFG)
    dev_item = next(i for i in items if i.dev_only)
    plain_item = next(i for i in items if not i.dev_only)

    dev_row = s.detail_row_fields(dev_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5))
    plain_row = s.detail_row_fields(plain_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5))

    assert dev_row["状态"] == p.STATUS_TODO
    assert plain_row["状态"] == p.STATUS_TODO


def test_detail_row_fields_never_seeds_role_confirmed_as_na() -> None:
    """role_confirmed 不是 dev_only —— 即便角色未答, 也必须种成 未完成 而不是 不适用,
    且 适用角色 标全员, 不能被当成仅研发项处理(需求 4)。用真实 yaml 的条目, 不是
    _CFG 小样, 因为 role_confirmed 只存在于真实 config/rookie_sop.yaml 里。"""
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    items = _real_items()
    role_item = next(i for i in items if i.item_id == "role_confirmed")
    assert role_item.dev_only is False

    row = s.detail_row_fields(role_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5))

    assert row["状态"] == p.STATUS_TODO
    assert row["适用角色"] == "全员"


def test_detail_row_fields_seeds_dev_only_items_as_todo_once_role_is_known() -> None:
    """一旦落地时角色已知(比如重放事件时已经问过), 开发环境项不用再等复活。"""
    s = _load("_rookie_sop_store")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    dev_item = next(i for i in cfg.load_sop(_CFG) if i.dev_only)

    row = s.detail_row_fields(dev_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5), role_label="研发")

    assert row["状态"] == p.STATUS_TODO


def test_day_one_denominator_counts_dev_items_because_they_are_tickable() -> None:
    """Day 1: 全部 4 项都计入分母(含那个开发项)。

    开发环境的项与角色选择同在一张卡上、Day 1 就可点, 所以必须计入分母 ——
    「可点却不计分母」会让新人看到的进度对不上。选「非研发人员」后
    mark_module_na 才把它们标不适用、分母随之下降。
    """
    s = _load("_rookie_sop_store")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    items = cfg.load_sop(_CFG)
    onboard = date(2026, 8, 5)
    rows = [s.detail_row_fields(i, open_id="ou_x", name="张三", onboard=onboard) for i in items]
    # detail_row_fields 里的日期字段还是 millis, summarize 期望 date —— 用
    # from_millis 转一下, 跟 fetch_detail 真实吐出来的行形状一致。
    for row in rows:
        row["入职日"] = s.from_millis(row["入职日"])
        row["截止日"] = s.from_millis(row["截止日"])

    got = p.summarize(rows, onboard)

    assert got.total == len(items)
    assert got.total == 4
    # 没有任何一行被种成不适用
    assert all(r["状态"] == p.STATUS_TODO for r in rows)


def _real_items() -> list[Any]:
    """加载真实 config/rookie_sop.yaml 展开出的条目 —— 用来验证真实数字, 不是 _CFG 小样。"""
    cfg = _load("_rookie_sop_config")
    s = _load("_rookie_sop_store")
    return cfg.load_sop(anyio.run(s.load_config))


def _real_rows(items: Any, onboard: date, *, done_ids: set[str] = frozenset()) -> list[dict[str, Any]]:
    """按真实条目播种明细行(角色未答), 把 done_ids 里的项直接改成已完成, 供分母/毕业测试用。"""
    s = _load("_rookie_sop_store")
    rows = [s.detail_row_fields(i, open_id="ou_x", name="张三", onboard=onboard) for i in items]
    for row, item in zip(rows, items, strict=True):
        row["入职日"] = s.from_millis(row["入职日"])
        row["截止日"] = s.from_millis(row["截止日"])
        if item.item_id in done_ids:
            row["状态"] = "已完成"
    return rows


def test_real_config_has_23_general_items_and_5_dev_only_items() -> None:
    """钉死 V3 的数字: 22 个通用项 + role_confirmed + 5 个 dev_only = 28。
    非研发分母 23, 研发分母 28。后面的分母测试都建立在这个前提上。"""
    items = _real_items()
    non_dev_only = [i for i in items if not i.dev_only]
    dev_only = [i for i in items if i.dev_only]

    assert len(items) == 28
    assert len(non_dev_only) == 23
    assert len(dev_only) == 5
    assert any(i.item_id == "role_confirmed" for i in non_dev_only)


def test_day_one_denominator_with_real_config_is_28_because_dev_items_are_tickable() -> None:
    """Day 1, 角色未答: 分母是 33 —— 5 个开发项与角色选择同在一张卡上、立即可点,
    所以从落地起就计入分母; role_confirmed 也未完成且计入。
    选「非研发人员」后那 5 项才被标不适用、分母降到 28。
    """
    p = _load("_rookie_sop_progress")
    items = _real_items()
    onboard = date(2026, 8, 5)
    rows = _real_rows(items, onboard)

    got = p.summarize(rows, onboard)

    assert got.total == 28
    assert got.done == 0
    assert got.all_done is False


def test_applicable_total_after_nondev_is_23_with_role_confirmed_done() -> None:
    """选「非研发」后: 5 个开发项标不适用(退出分母), role_confirmed 已完成 ——
    分母仍是 28(27 通用 + role_confirmed), 但分子里包含 role_confirmed 这一项。"""
    p = _load("_rookie_sop_progress")
    cfg = _load("_rookie_sop_config")
    items = _real_items()
    onboard = date(2026, 8, 5)
    rows = _real_rows(items, onboard, done_ids={"role_confirmed"})
    for row, item in zip(rows, items, strict=True):
        if item.dev_only:
            row["状态"] = "不适用"

    got = p.summarize(rows, onboard)
    applicable_ids = {i.item_id for i in cfg.applicable_items(items, "nondev")}

    assert got.total == 23
    assert got.done == 1
    assert "role_confirmed" in applicable_ids
    assert len(applicable_ids) == 23


def test_applicable_total_after_dev_is_28_with_all_five_dev_items_live() -> None:
    """选「研发」后: 5 个开发项复活成未完成(不再是不适用), role_confirmed 已完成 ——
    分母变成 33(27 通用 + role_confirmed + 5 开发项), 全都参与催办/进度计算。"""
    p = _load("_rookie_sop_progress")
    cfg = _load("_rookie_sop_config")
    items = _real_items()
    onboard = date(2026, 8, 5)
    rows = _real_rows(items, onboard, done_ids={"role_confirmed"})
    # 种子行里角色未答时 dev_only 项直接是不适用; 「选研发」会把它们复活成未完成 ——
    # 在这里手动模拟 rookie_sop_role_set 的复活写回, 不是引入新的产品逻辑。
    for row, item in zip(rows, items, strict=True):
        if item.dev_only:
            row["状态"] = "未完成"

    got = p.summarize(rows, onboard)
    applicable_ids = {i.item_id for i in cfg.applicable_items(items, "dev")}

    assert got.total == 28
    assert got.done == 1
    assert len(applicable_ids) == 28
    assert all(i.item_id in applicable_ids for i in items if i.dev_only)


def test_unanswered_role_card_cannot_graduate_even_with_every_general_item_done() -> None:
    """回归测试(需求里点名的毕业漏洞): 新人从没点过角色卡, 27 个通用项全部勾完 ——
    role_confirmed 仍未完成, all_done 必须是 False。撤掉 role_confirmed 这个条目
    (回到旧的 27+5 清单)会让这条断言失败: 分母会退回 27, 27 个通用项做完就等于
    done==total, all_done 会被误判成 True —— 这正是这次要堵住的毕业漏洞。"""
    p = _load("_rookie_sop_progress")
    items = _real_items()
    onboard = date(2026, 8, 5)
    general_done_ids = {i.item_id for i in items if not i.dev_only and i.item_id != "role_confirmed"}
    assert len(general_done_ids) == 22  # 确保真的是「其余全做完」, 不是漏了几项

    rows = _real_rows(items, onboard, done_ids=general_done_ids)

    got = p.summarize(rows, onboard)

    # 分母 28 = 22 通用 + role_confirmed + 5 开发项; 做完 22 个通用项后
    # 仍差 role_confirmed 与 5 个开发项, 所以 all_done 必须是 False。
    assert got.total == 28
    assert got.done == 22
    assert got.all_done is False


def test_fetch_detail_parses_rows_and_converts_dates() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            [
                _item(
                    "rec1",
                    {
                        "记录键": "ou_x:wifi",
                        "姓名": "张三",
                        "open_id": "ou_x",
                        "模块": "环境准备",
                        "项": "连上 WiFi",
                        "状态": p.STATUS_TODO,
                        "入职日": s.to_millis(date(2026, 8, 5)),
                        "截止日": s.to_millis(date(2026, 8, 5)),
                    },
                )
            ]
        ]
    )

    rows, truncated = anyio.run(s.fetch_detail, fake, "app1", "tblDetail", "ou_x")

    assert len(rows) == 1
    assert truncated is False
    assert rows[0]["record_id"] == "rec1"
    assert rows[0]["入职日"] == date(2026, 8, 5)
    assert rows[0]["截止日"] == date(2026, 8, 5)
    # 按 open_id 过滤
    assert "ou_x" in fake.searches[0]["filter_json"]


def test_fetch_detail_follows_has_more_across_pages() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            (
                [_item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO})],
                True,
                "tok1",
            ),
            (
                [_item("rec2", {"记录键": "ou_x:badge", "状态": p.STATUS_TODO})],
                False,
                "",
            ),
        ]
    )

    rows, truncated = anyio.run(s.fetch_detail, fake, "app1", "tblDetail", "ou_x")

    assert [r["record_id"] for r in rows] == ["rec1", "rec2"]
    assert truncated is False
    assert len(fake.searches) == 2
    # 第一次不带 token, 第二次带上第一页返回的 token
    assert fake.searches[0]["page_token"] == ""
    assert fake.searches[1]["page_token"] == "tok1"


def test_fetch_detail_stops_at_max_pages_against_a_server_that_never_stops() -> None:
    """服务端一直回 has_more=True 且每页给一个新 token —— 没有页数上限会永远翻下去。

    这里让假适配器每页都生成一个没见过的 token, 断言 fetch_detail 在
    ``s.MAX_PAGES`` 页之后停手, 并把 truncated=True 报出来而不是假装读完了。
    """
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")

    class _NeverStopsBitable:
        def __init__(self) -> None:
            self.calls = 0

        async def search_records(self, app_token: str, table_id: str, filter_json: str = "", **kwargs: Any) -> str:
            self.calls += 1
            item = _item(f"rec{self.calls}", {"记录键": f"ou_x:item{self.calls}", "状态": p.STATUS_TODO})
            return json.dumps(
                {
                    "ok": True,
                    "records": [item],
                    "count": 1,
                    "has_more": True,
                    "page_token": f"tok{self.calls}",
                    "total": 1,
                },
                ensure_ascii=False,
            )

    fake = _NeverStopsBitable()

    rows, truncated = anyio.run(s.fetch_detail, fake, "app1", "tblDetail", "ou_x")

    assert truncated is True
    assert fake.calls == s.MAX_PAGES
    assert len(rows) == s.MAX_PAGES


def test_mark_done_updates_status_and_completion_time() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable([[_item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO})]])

    out = anyio.run(
        lambda: s.mark_done(fake, "app1", "tblDetail", open_id="ou_x", item_id="wifi", today=date(2026, 8, 6))
    )

    assert out["ok"] is True
    assert fake.updates[0]["records"][0]["record_id"] == "rec1"
    fields = fake.updates[0]["records"][0]["fields"]
    assert fields["状态"] == p.STATUS_DONE
    assert fields["完成时间"] == s.to_millis(date(2026, 8, 6))


def test_mark_done_surfaces_a_rejected_write_instead_of_reporting_ok() -> None:
    """update_records 被飞书拒绝时不能报 ok=true —— 按钮已经被框架吃掉了, 行却
    还停在未完成, 报成功等于让这一行永远点不动了。"""
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [[_item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO})]],
        fail_writes=True,
    )

    out = anyio.run(
        lambda: s.mark_done(fake, "app1", "tblDetail", open_id="ou_x", item_id="wifi", today=date(2026, 8, 6))
    )

    assert out["ok"] is False
    assert "error" in out
    assert out["record_id"] == "rec1"


def test_mark_done_is_idempotent_when_already_done() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable([[_item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_DONE})]])

    out = anyio.run(
        lambda: s.mark_done(fake, "app1", "tblDetail", open_id="ou_x", item_id="wifi", today=date(2026, 8, 6))
    )

    assert out["ok"] is True
    assert out["already_done"] is True
    # 已完成就不再写一次
    assert fake.updates == []


def test_mark_done_reports_duplicates_instead_of_silently_dropping_them() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    # 记录键理应唯一, 但这里模拟重试双写: 同一个键命中两行。
    fake = _FakeBitable(
        [
            [
                _item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO}),
                _item("rec2", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO}),
            ]
        ]
    )

    out = anyio.run(
        lambda: s.mark_done(fake, "app1", "tblDetail", open_id="ou_x", item_id="wifi", today=date(2026, 8, 6))
    )

    assert out["ok"] is True
    assert out["duplicates"] == 1
    # 只标第一行, 但把重复情况报出来而不是悄悄吞掉
    assert fake.updates[0]["records"][0]["record_id"] == "rec1"


def test_mark_module_na_marks_every_row_of_that_module() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            [
                _item("recA", {"记录键": "ou_x:git_workflow", "模块": "开发环境", "状态": p.STATUS_TODO}),
                _item("recB", {"记录键": "ou_x:repo_access", "模块": "开发环境", "状态": p.STATUS_TODO}),
            ]
        ]
    )

    out = anyio.run(
        lambda: s.mark_module_na(fake, "app1", "tblDetail", open_id="ou_x", module="开发环境", today=date(2026, 8, 6))
    )

    assert out["ok"] is True and out["marked"] == 2
    updated = fake.updates[0]["records"]
    assert {r["record_id"] for r in updated} == {"recA", "recB"}
    assert all(r["fields"]["状态"] == p.STATUS_NA for r in updated)


def test_mark_module_na_skips_excluded_item_ids() -> None:
    """role_confirmed 与开发环境同模块但全员适用 —— exclude_item_ids 必须让它躲开
    这次不适用改写(需求 4), 只有真正的 dev_only 行才被标不适用。"""
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            [
                _item("recA", {"记录键": "ou_x:git_workflow", "模块": "开发环境", "状态": p.STATUS_TODO}),
                # 特意留成未完成: 若 exclude_item_ids 不起作用, 这一行会被误标不适用。
                _item("recB", {"记录键": "ou_x:role_confirmed", "模块": "开发环境", "状态": p.STATUS_TODO}),
            ]
        ]
    )

    out = anyio.run(
        lambda: s.mark_module_na(
            fake,
            "app1",
            "tblDetail",
            open_id="ou_x",
            module="开发环境",
            today=date(2026, 8, 6),
            exclude_item_ids=frozenset({"role_confirmed"}),
        )
    )

    assert out["ok"] is True and out["marked"] == 1
    updated = fake.updates[0]["records"]
    assert {r["record_id"] for r in updated} == {"recA"}


def test_recompute_overview_updates_the_existing_row_instead_of_adding_one() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    detail = [
        {
            "记录键": "ou_x:wifi",
            "状态": p.STATUS_DONE,
            "入职日": date(2026, 8, 5),
            "截止日": date(2026, 8, 5),
            "项": "wifi",
        },
        {
            "记录键": "ou_x:desk",
            "状态": p.STATUS_TODO,
            "入职日": date(2026, 8, 5),
            "截止日": date(2026, 8, 5),
            "项": "desk",
        },
    ]
    # 总览里已有该人一行 → 走 update 而不是 create
    fake = _FakeBitable([[_item("recOv", {"open_id": "ou_x"}, table="overview")]])

    out = anyio.run(
        lambda: s.recompute_overview(
            fake, "app1", "tblOverview", open_id="ou_x", name="张三", role="dev", rows=detail, today=date(2026, 8, 7)
        )
    )

    assert out["ok"] is True
    assert fake.creates == []
    fields = fake.updates[0]["records"][0]["fields"]
    assert fields["进度"] == "1/2"
    assert fields["逾期项数"] == 1
    assert fields["入职日"] == s.to_millis(date(2026, 8, 5))


def test_recompute_overview_creates_the_row_when_absent() -> None:
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    detail = [
        {
            "记录键": "ou_x:wifi",
            "状态": p.STATUS_TODO,
            "入职日": date(2026, 8, 5),
            "截止日": date(2026, 8, 5),
            "项": "wifi",
        }
    ]
    fake = _FakeBitable([[]])  # 总览里还没有这一行

    out = anyio.run(
        lambda: s.recompute_overview(
            fake, "app1", "tblOverview", open_id="ou_x", name="张三", role="", rows=detail, today=date(2026, 8, 5)
        )
    )

    assert out["ok"] is True
    assert fake.updates == []
    assert fake.creates[0]["records"][0]["fields"]["open_id"] == "ou_x"


def test_recompute_overview_heals_a_corrupted_row() -> None:
    """人为改坏总览行后, 下一次重算必须把它算回正确值(验证「重算而非增量」)。"""
    s = _load("_rookie_sop_store")
    p = _load("_rookie_sop_progress")
    detail = [
        {"记录键": "ou_x:a", "状态": p.STATUS_DONE, "入职日": date(2026, 8, 5), "截止日": date(2026, 8, 5), "项": "a"},
        {"记录键": "ou_x:b", "状态": p.STATUS_DONE, "入职日": date(2026, 8, 5), "截止日": date(2026, 8, 5), "项": "b"},
    ]
    # 总览行被改成了荒谬的值
    fake = _FakeBitable([[_item("recOv", {"open_id": "ou_x", "进度": "99/99", "逾期项数": 42}, table="overview")]])

    anyio.run(
        lambda: s.recompute_overview(
            fake, "app1", "tblOverview", open_id="ou_x", name="张三", role="nondev", rows=detail, today=date(2026, 8, 6)
        )
    )

    fields = fake.updates[0]["records"][0]["fields"]
    assert fields["进度"] == "2/2"
    assert fields["逾期项数"] == 0
    assert fields["状态"] == "已出新手村"


def test_plan_module_cards_makes_the_dev_module_a_role_card() -> None:
    r = _load("_rookie_sop_runtime")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    items = cfg.load_sop(_CFG)
    rows = [
        {
            "记录键": f"ou_x:{i.item_id}",
            "项": i.title,
            "验收标准": i.acceptance,
            "模块": i.module,
            "状态": p.STATUS_TODO,
            "入职日": date(2026, 8, 5),
            "截止日": cfg.due_date(date(2026, 8, 5), i.window_days),
        }
        for i in items
    ]

    plans = r.plan_module_cards(items, rows, date(2026, 8, 5), date(2026, 8, 5), "")

    by_module = {p_["module"]: p_ for p_ in plans}
    dev_handlers = by_module["开发环境"]["handlers"]
    # 开发环境仍标记为 role_card, 但现在是「一张卡装完」: 角色按钮与开发项同在其中
    assert by_module["开发环境"]["is_role_card"] is True
    assert dev_handlers["rookie_role_dev"] == "rookie_sop_role_set"
    assert dev_handlers["rookie_role_nondev"] == "rookie_sop_role_set"
    assert dev_handlers["rookie_tick_git_workflow"] == "rookie_sop_tick"
    # role_confirmed 由角色按钮代表, 不另给勾选按钮
    assert "rookie_tick_role_confirmed" not in dev_handlers
    # 其余模块直接给勾选行
    assert by_module["环境准备"]["is_role_card"] is False
    assert "rookie_tick_wifi" in by_module["环境准备"]["handlers"]


def test_plan_module_cards_keeps_each_card_within_the_forty_row_cap() -> None:
    r = _load("_rookie_sop_runtime")
    cfg = _load("_rookie_sop_config")
    items = cfg.load_sop(_CFG)
    rows = [
        {
            "记录键": f"ou_x:{i.item_id}",
            "项": i.title,
            "模块": i.module,
            "状态": "未完成",
            "入职日": date(2026, 8, 5),
            "截止日": date(2026, 8, 5),
        }
        for i in items
    ]

    for plan in r.plan_module_cards(items, rows, date(2026, 8, 5), date(2026, 8, 5), ""):
        assert len(plan["handlers"]) <= 40


def test_should_send_cards_only_on_first_send_or_explicit_force() -> None:
    r = _load("_rookie_sop_runtime")

    assert r.should_send_cards(is_first_send=True, force_resend=False) is True
    assert r.should_send_cards(is_first_send=True, force_resend=True) is True
    assert r.should_send_cards(is_first_send=False, force_resend=True) is True
    # 重复事件, 没人显式要求强发 —— 不该再发一遍卡片
    assert r.should_send_cards(is_first_send=False, force_resend=False) is False


def test_ensure_base_refuses_to_persist_an_incomplete_state(monkeypatch: Any) -> None:
    """建表只成功了一半(比如总览表没拿到 table_id)时, 不该把半成品状态存下来。"""
    r = _load("_rookie_sop_runtime")

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    saved: list[dict[str, Any]] = []

    async def _fake_save_state(state: dict[str, Any]) -> None:
        saved.append(state)

    class _FakeApiModule:
        @staticmethod
        async def feishu_api(method: str, uri: str, body_json: str = "") -> str:
            return json.dumps({"ok": True, "code": 0, "data": {"app": {"app_token": "app1"}}}, ensure_ascii=False)

    class _FakeBitableModule:
        @staticmethod
        async def feishu_bitable_create_table(app_token: str, table_name: str, fields_json: str = "") -> str:
            # 明细表建成功, 总览表建失败(没有 table_id) —— 模拟半成品
            if table_name == "入职总览":
                return json.dumps({"ok": True}, ensure_ascii=False)
            return json.dumps({"ok": True, "table_id": "tblDetail"}, ensure_ascii=False)

    monkeypatch.setattr(r, "load_state", _fake_load_state)
    monkeypatch.setattr(r, "save_state", _fake_save_state)
    monkeypatch.setitem(sys.modules, "feishu_api", _FakeApiModule())
    monkeypatch.setitem(sys.modules, "feishu_bitable", _FakeBitableModule())

    out = anyio.run(lambda: r.ensure_base({"company_name": "Haitun"}))

    assert out["ok"] is False
    assert "overview_table_id" in out["error"]
    assert saved == []


def _callback(item_id: str, *, open_id: str = "ou_x", with_business: bool = True) -> str:
    payload: dict[str, Any] = {
        "action": {"value": {"action": f"rookie_tick_{item_id}", "item_id": item_id}},
        "source": {"operator_open_id": open_id},
        "dispatch": {"handler": "rookie_sop_tick", "matched": True},
    }
    if with_business:
        payload["business_context"] = {
            "type": "rookie_sop",
            "open_id": open_id,
            "name": "张三",
            "module": "环境准备",
            "app_token": "app1",
            "detail_table_id": "tblDetail",
            "overview_table_id": "tblOverview",
        }
    return json.dumps(payload, ensure_ascii=False)


def test_resolve_context_prefers_business_context() -> None:
    t = _load("rookie_sop_tick")

    got = t._resolve_context(json.loads(_callback("wifi")))

    assert got["open_id"] == "ou_x"
    assert got["item_id"] == "wifi"
    assert got["detail_table_id"] == "tblDetail"


def test_resolve_context_falls_back_to_operator_and_action_value() -> None:
    t = _load("rookie_sop_tick")

    got = t._resolve_context(json.loads(_callback("desk", with_business=False)))

    # business_context 缺失时仍能拿到点击者与 item_id
    assert got["open_id"] == "ou_x"
    assert got["item_id"] == "desk"
    # 表 id 只能来自 business_context 或状态文件, 这里留空由工具兜底
    assert got["detail_table_id"] == ""


def test_resolve_context_rejects_a_wrong_handler() -> None:
    t = _load("rookie_sop_tick")

    payload = json.loads(_callback("wifi"))
    payload["dispatch"] = {"handler": "something_else", "matched": True}

    got = t._resolve_context(payload)

    assert got["error"]


def _wire_fake_bitable(t: Any, fake: _FakeBitable) -> None:
    """把 t 内部用到的 _rt.bitable_adapter / load_state 换成假的, 免得真去连飞书。"""

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    t._rt.bitable_adapter = lambda: fake
    t._rt.load_state = _fake_load_state


def test_rookie_sop_tick_surfaces_duplicates_instead_of_dropping_them() -> None:
    """mark_done 命中重复行时, rookie_sop_tick 的响应要把 duplicates 报出来。"""
    t = _load("rookie_sop_tick")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            # 1) mark_done 按「记录键」查 —— 命中两行, 模拟重试双写
            [
                _item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO}),
                _item("rec2", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO}),
            ],
            # 2) fetch_detail 按 open_id 查全部明细行
            [_item("rec1", {"记录键": "ou_x:wifi", "模块": "环境准备", "状态": p.STATUS_TODO})],
            # 3) recompute_overview 查总览里是否已有该人的行 —— 没有, 走创建
            [],
        ]
    )
    _wire_fake_bitable(t, fake)

    out = json.loads(anyio.run(lambda: t.rookie_sop_tick(_callback("wifi", with_business=True))))

    assert out["ok"] is True
    assert out["duplicates"] == 1
    # 只标第一行, 但把重复情况报出来而不是悄悄吞掉
    assert fake.updates[0]["records"][0]["record_id"] == "rec1"


def test_rookie_sop_tick_ordinary_case_has_no_duplicates_field() -> None:
    """没有重复行的正常路径不该背上一个多余的 duplicates 字段。"""
    t = _load("rookie_sop_tick")
    p = _load("_rookie_sop_progress")
    fake = _FakeBitable(
        [
            # 1) mark_done 按「记录键」查 —— 只命中一行
            [_item("rec1", {"记录键": "ou_x:wifi", "状态": p.STATUS_TODO})],
            # 2) fetch_detail 按 open_id 查全部明细行
            [_item("rec1", {"记录键": "ou_x:wifi", "模块": "环境准备", "状态": p.STATUS_TODO})],
            # 3) recompute_overview 查总览 —— 没有, 走创建
            [],
        ]
    )
    _wire_fake_bitable(t, fake)

    out = json.loads(anyio.run(lambda: t.rookie_sop_tick(_callback("wifi", with_business=True))))

    assert out["ok"] is True
    assert "duplicates" not in out


def _role_callback(role: str, *, open_id: str = "ou_x") -> str:
    action = "rookie_role_dev" if role == "dev" else "rookie_role_nondev"
    return json.dumps(
        {
            "action": {"value": {"action": action, "role": role}},
            "source": {"operator_open_id": open_id},
            "dispatch": {"handler": "rookie_sop_role_set", "matched": True},
            # 原地更新这张卡要用 message_id(不再补发第二张)
            "message_id": "om_role_card",
            "business_context": {
                "type": "rookie_sop",
                "open_id": open_id,
                "name": "张三",
                "module": "开发环境",
                "app_token": "app1",
                "detail_table_id": "tblDetail",
                "overview_table_id": "tblOverview",
            },
        },
        ensure_ascii=False,
    )


def test_role_context_reads_dev_and_nondev() -> None:
    rs = _load("rookie_sop_role_set")

    assert rs._resolve_role(json.loads(_role_callback("dev")))["role"] == "dev"
    assert rs._resolve_role(json.loads(_role_callback("nondev")))["role"] == "nondev"


def test_role_context_rejects_wrong_handler() -> None:
    rs = _load("rookie_sop_role_set")

    payload = json.loads(_role_callback("dev"))
    payload["dispatch"] = {"handler": "rookie_sop_tick", "matched": True}

    assert rs._resolve_role(payload)["error"]


def _dev_detail_rows(open_id: str = "ou_x", *, status: str = "未完成") -> list[dict[str, Any]]:
    """五条 开发环境 明细行, 供角色回调的端到端测试用。"""
    ids = ["read_agents_md", "setup_dev_env", "git_workflow", "code_review", "repo_access"]
    return [
        _item(
            f"rec_{item_id}",
            {"记录键": f"{open_id}:{item_id}", "模块": "开发环境", "状态": status, "适用角色": "仅研发"},
        )
        for item_id in ids
    ]


def _role_confirmed_row(open_id: str = "ou_x", *, status: str = "未完成") -> dict[str, Any]:
    """role_confirmed 明细行 —— 与开发环境同模块但全员适用, 供角色回调端到端测试用。"""
    return _item(
        "rec_role_confirmed",
        {"记录键": f"{open_id}:role_confirmed", "模块": "开发环境", "状态": status, "适用角色": "全员"},
    )


def test_role_set_surfaces_card_send_failure_as_not_ok(monkeypatch: Any) -> None:
    """卡发送失败(含"发了但快照没存下来")时, 工具必须报 ok=false, 不能报成功。"""
    rs = _load("rookie_sop_role_set")
    fake = _FakeBitable(
        [
            [*_dev_detail_rows(), _role_confirmed_row()],  # fetch_detail 拿开发环境明细行, 用于 适用角色 改写
            [_role_confirmed_row()],  # mark_done(role_confirmed) 按记录键查
            [*_dev_detail_rows(), _role_confirmed_row(status="已完成")],  # 复活/改写后重拉一次明细
        ]
    )
    rs._rt.bitable_adapter = lambda: fake

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    rs._rt.load_state = _fake_load_state

    async def _fake_edit_card_fails(*args: Any, **kwargs: Any) -> str:
        # 现在是原地更新(不再补发第二张卡), 所以失败面是 edit_card 被拒。
        return json.dumps(
            {"ok": False, "message": "Feishu rejected the card patch"}, ensure_ascii=False
        )

    monkeypatch.setattr(rs, "feishu_message_edit_card", _fake_edit_card_fails)

    out = json.loads(anyio.run(lambda: rs.rookie_sop_role_set(_role_callback("dev"))))

    assert out["ok"] is False
    assert "error" in out


def test_role_set_switching_nondev_to_dev_revives_all_five_rows(monkeypatch: Any) -> None:
    """先选非研发(五行标不适用), 再选研发: 新卡必须拿到全部五行活的勾选行, 不能是 0。"""
    rs = _load("rookie_sop_role_set")
    na_rows = _dev_detail_rows(status="不适用")
    fake = _FakeBitable(
        [
            [*na_rows, _role_confirmed_row(status="已完成")],  # fetch_detail: 改写 适用角色 前先拉明细
            [_role_confirmed_row(status="已完成")],  # mark_done(role_confirmed) 按记录键查, 早就已完成
            [*_dev_detail_rows(status="未完成"), _role_confirmed_row(status="已完成")],  # 复活写回后重拉明细
            [],  # recompute_overview 查总览 —— 没有, 走创建
        ]
    )
    rs._rt.bitable_adapter = lambda: fake

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    rs._rt.load_state = _fake_load_state

    sent_cards: list[dict[str, Any]] = []

    async def _fake_send_card_ok(message_id: str, card_json: str, user_key: str = "") -> str:
        # 现在是原地更新: edit_card(message_id, card_json) —— 不再补发第二张卡,
        # 所以这里拿不到 action_handlers_json, 改从卡面数可点按钮。
        card = json.loads(card_json)
        clickable = sum(1 for e in card.get("elements", []) if isinstance(e, dict) and "extra" in e)
        sent_cards.append({"card": card, "clickable": clickable})
        return json.dumps({"ok": True}, ensure_ascii=False)

    monkeypatch.setattr(rs, "feishu_message_edit_card", _fake_send_card_ok)

    out = json.loads(anyio.run(lambda: rs.rookie_sop_role_set(_role_callback("dev"))))

    assert out["ok"] is True
    assert out["dev_items"] == 5
    # 复活写回: 五行 不适用 → 未完成 都要发生, 不是只改了 适用角色 标签
    revive_call = next(u for u in fake.updates if any(f["fields"].get("状态") == "未完成" for f in u["records"]))
    assert len(revive_call["records"]) == 5
    # 重绘后的卡真的带着 5 个可点的方框(不是零行死卡);
    # role_confirmed 已完成、不出按钮, 所以正好 5 个。
    assert sent_cards[0]["clickable"] == 5


def test_decide_remind_stays_silent_when_nothing_is_due() -> None:
    rm = _load("rookie_sop_remind")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5)),
        _row("attendance", p.STATUS_TODO, date(2026, 8, 20)),  # 还早
    ]

    got = rm.decide_remind(rows, date(2026, 8, 7))

    assert got["kind"] == "silent"


def test_decide_remind_fires_when_overdue_or_due_today() -> None:
    rm = _load("rookie_sop_remind")
    p = _load("_rookie_sop_progress")

    overdue = [_row("wifi", p.STATUS_TODO, date(2026, 8, 5))]
    assert rm.decide_remind(overdue, date(2026, 8, 7))["kind"] == "remind"

    due_today = [_row("desk", p.STATUS_TODO, date(2026, 8, 7))]
    assert rm.decide_remind(due_today, date(2026, 8, 7))["kind"] == "remind"


def test_decide_remind_graduates_when_all_applicable_items_are_done() -> None:
    rm = _load("rookie_sop_remind")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 5)),
        _row("git_workflow", p.STATUS_NA, date(2026, 8, 11), module="开发环境"),
    ]

    got = rm.decide_remind(rows, date(2026, 8, 7))

    assert got["kind"] == "graduate"
    assert got["progress"].total == 1


def test_decide_remind_on_empty_rows_is_silent_not_graduate() -> None:
    rm = _load("rookie_sop_remind")

    # 明细还没建好时不能误报毕业
    assert rm.decide_remind([], date(2026, 8, 7))["kind"] == "silent"


def test_active_rookies_keeps_in_progress_and_todays_graduates() -> None:
    dg = _load("rookie_sop_digest")
    overview = [
        {"姓名": "张三", "状态": "进行中", "最后更新": date(2026, 8, 7)},
        {"姓名": "李四", "状态": "已出新手村", "最后更新": date(2026, 8, 7)},  # 今天毕业, 报一次
        {"姓名": "王五", "状态": "已出新手村", "最后更新": date(2026, 8, 1)},  # 早就毕业, 退场
    ]

    got = dg.active_rookies(overview, date(2026, 8, 7))

    assert [r["姓名"] for r in got] == ["张三", "李四"]


def test_active_rookies_on_empty_overview_is_empty() -> None:
    dg = _load("rookie_sop_digest")
    assert dg.active_rookies([], date(2026, 8, 7)) == []


def test_rookie_sop_digest_sends_nothing_on_empty_roster(monkeypatch: Any) -> None:
    """总览表空 —— 兜底对账无事可做, 也不该发卡, 但要报清楚 sent=False 的原因。"""
    dg = _load("rookie_sop_digest")
    fake = _FakeBitable([[], []])  # 对账前后各查一次总览, 都是空的

    async def _fake_load_state() -> dict[str, Any]:
        return {"app_token": "app1", "detail_table_id": "tblDetail", "overview_table_id": "tblOverview"}

    dg._rt.bitable_adapter = lambda: fake
    dg._rt.load_state = _fake_load_state

    async def _fail_if_called(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("feishu_message_send_card must not be called on an empty roster")

    monkeypatch.setattr(dg, "feishu_message_send_card", _fail_if_called)

    out = json.loads(anyio.run(lambda: dg.rookie_sop_digest("ou_hr")))

    assert out == {"ok": True, "sent": False, "reason": "no active rookies"}


def test_rookie_sop_digest_follows_has_more_when_reading_the_overview_table(monkeypatch: Any) -> None:
    """总览表随人数增长会翻页 —— 日报和兜底对账都不能只读第一页。"""
    dg = _load("rookie_sop_digest")
    page1 = _item("recOv1", {"open_id": "ou_x", "姓名": "张三", "状态": "进行中"}, table="overview")
    page2 = _item("recOv2", {"open_id": "ou_y", "姓名": "李四", "状态": "进行中"}, table="overview")
    fake = _FakeBitable(
        [
            ([page1], True, "tok1"),  # 第一次读总览(对账前): 第一页
            ([page2], False, ""),  # 第一次读总览(对账前): 第二页
            ([page1], True, "tok1"),  # 第二次读总览(渲染前): 第一页
            ([page2], False, ""),  # 第二次读总览(渲染前): 第二页
        ]
    )

    async def _fake_load_state() -> dict[str, Any]:
        return {"app_token": "app1", "detail_table_id": "", "overview_table_id": "tblOverview"}

    dg._rt.bitable_adapter = lambda: fake
    dg._rt.load_state = _fake_load_state

    async def _fake_send_card_ok(*args: Any, **kwargs: Any) -> str:
        return json.dumps({"ok": True}, ensure_ascii=False)

    monkeypatch.setattr(dg, "feishu_message_send_card", _fake_send_card_ok)

    out = json.loads(anyio.run(lambda: dg.rookie_sop_digest("ou_hr")))

    assert out == {"ok": True, "sent": True, "rookies": 2}
    # 两次读总览各翻了一页, 一共发出 4 次 search_records
    assert len(fake.searches) == 4


def test_all_five_tools_exist_as_files() -> None:
    for name in (
        "rookie_sop_card_send",
        "rookie_sop_tick",
        "rookie_sop_role_set",
        "rookie_sop_remind",
        "rookie_sop_digest",
    ):
        assert (TOOLS / f"{name}.py").is_file(), f"缺少工具文件 {name}.py"


def test_trigger_and_skill_are_registered() -> None:
    trigger = HAITUN / "triggers" / "rookie-sop-welcome" / "TRIGGER.md"
    skill = HAITUN / "skills" / "feishu-rookie-onboarding" / "SKILL.md"
    assert trigger.is_file()
    assert skill.is_file()

    trigger_text = trigger.read_text(encoding="utf-8")
    # fire=tool: 到点/命中不经过 LLM
    assert "fire: tool" in trigger_text
    assert "tool: rookie_sop_card_send" in trigger_text
    assert "event: feishu.hr.user_created" in trigger_text

    skill_text = skill.read_text(encoding="utf-8")
    assert "rookie_sop_tick" in skill_text
    assert "rookie_sop_role_set" in skill_text


def test_agents_md_documents_the_new_tools() -> None:
    text = (HAITUN / "AGENTS.md").read_text(encoding="utf-8")
    assert "rookie_sop_card_send" in text
    assert "feishu-rookie-onboarding" in text


def test_due_state_marks_late_today_and_future_differently() -> None:
    """DDL 状态标记: 逾期红、当天黄、未来绿; 已完成与不适用有各自的标记。"""
    c = _load("_rookie_sop_card")
    today = date(2026, 8, 6)

    def mark(due: date, status: str = "未完成") -> str:
        return c._due_state({"记录键": "ou_x:a", "项": "某项", "状态": status, "截止日": due}, today)

    assert mark(date(2026, 8, 5)) == "🔴"  # 已逾期
    assert mark(date(2026, 8, 6)) == "🟡"  # 今天到期
    assert mark(date(2026, 8, 9)) == "🟢"  # 还早
    assert mark(date(2026, 8, 5), "已完成") == "✅"  # 完成优先于逾期
    assert mark(date(2026, 8, 5), "不适用") == "⚪"


def test_card_template_takes_the_most_urgent_row() -> None:
    """卡片主题色取最紧急的一行: 有逾期→红, 否则有当天→橙, 全部结束→绿。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    today = date(2026, 8, 6)

    late = _row("a", p.STATUS_TODO, date(2026, 8, 5))
    due_today = _row("b", p.STATUS_TODO, date(2026, 8, 6))
    future = _row("c", p.STATUS_TODO, date(2026, 8, 9))
    done = _row("d", p.STATUS_DONE, date(2026, 8, 5))

    assert c._card_template([late, due_today, future], today) == "red"
    assert c._card_template([due_today, future], today) == "orange"
    assert c._card_template([future], today) == "blue"
    assert c._card_template([done], today) == "green"


def test_row_is_div_with_the_box_in_extra() -> None:
    """一行 = div 左侧文字 + extra 右侧方框(飞书原生的「左文右控件」)。

    刻意不让框架去替换这个 extra: 飞书的 div.extra 只接受
    [img button select_static select_person overflow date_picker ...], 不接受
    markdown, 框架那次 patch 会被拒(230099 / ErrCode 11310), 卡面永不更新。
    完成态改由 rookie_sop_tick 重绘整卡实现。
    """
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    today = date(2026, 8, 6)

    todo_elements, action = c._row_elements(
        _row("wifi", p.STATUS_TODO, date(2026, 8, 9), title="连上 WiFi"), today
    )
    done_elements, done_action = c._row_elements(
        _row("wifi", p.STATUS_DONE, date(2026, 8, 9), title="连上 WiFi"), today
    )

    assert len(todo_elements) == 1
    el = todo_elements[0]
    assert el["tag"] == "div"
    # 条目名留在左侧文字里(框和字挤在一个按钮内很难看), 方框只写一个符号
    assert "连上 WiFi" in el["text"]["content"]
    assert el["extra"]["tag"] == "button"
    assert el["extra"]["text"]["content"] == c._BOX
    assert el["extra"]["value"]["action"] == "rookie_tick_wifi"
    assert action == "rookie_tick_wifi"
    # 已完成行: 文字划掉, 没有 extra 按钮
    done_el = done_elements[0]
    assert "~~连上 WiFi~~" in done_el["text"]["content"]
    assert "extra" not in done_el
    assert done_action == ""


def test_rows_section_is_compact_without_a_rule_between_rows() -> None:
    """行与行之间不插 hr —— 分隔线会把卡片撑得很松散。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("a", p.STATUS_TODO, date(2026, 8, 9), title="甲"),
        _row("b", p.STATUS_TODO, date(2026, 8, 9), title="乙"),
    ]

    elements, handlers = c._rows_section(rows, date(2026, 8, 6))

    assert len(handlers) == 2
    assert all(e["tag"] == "div" for e in elements)
    assert not any(e["tag"] == "hr" for e in elements)


def test_progress_text_is_plain_numbers_and_degrades_safely() -> None:
    c = _load("_rookie_sop_card")

    # 纯数字进度: ▰▱ 方块条会和行尾按钮视觉撞车, 已弃用
    assert c._progress_bar("0/5") == "**已完成 0 / 5 项**"
    assert c._progress_bar("5/5") == "**已完成 5 / 5 项**　🎉"  # 全部做完加个完成信号
    assert "3 / 5" in c._progress_bar("3/5")
    # 非法输入原样返回, 不抛异常
    assert c._progress_bar("0/0") == "0/0"
    assert c._progress_bar("坏数据") == "坏数据"


def _doc_row(
    item_id: str, title: str, status: str, module: str = "环境准备", acceptance: str = "验收"
) -> dict[str, Any]:
    return {
        "记录键": f"ou_x:{item_id}",
        "项": title,
        "验收标准": acceptance,
        "模块": module,
        "状态": status,
        "入职日": date(2026, 8, 5),
        "截止日": date(2026, 8, 9),
    }


def _slots_map(slots: list[tuple[str, str]]) -> dict[str, str]:
    """模拟建文档后拿到的 block_id → "item_id:role" 映射(按 todo 出现顺序配对)。"""
    return {f"blk{n}": f"{item_id}:{role}" for n, (item_id, role) in enumerate(slots)}


def _with_block_ids(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给 todo 块补上 block_id, 模拟从飞书读回来的样子。"""
    out: list[dict[str, Any]] = []
    n = 0
    for b in blocks:
        if b.get("block_type") == 17:
            out.append({**b, "block_id": f"blk{n}"})
            n += 1
        else:
            out.append(b)
    return out


def test_build_doc_blocks_renders_a_todo_per_item_with_done_from_the_table() -> None:
    """文档是明细表的投影: 表里已完成的项, 文档里就是勾上的。

    条目身份靠 block_id 映射而不是正文里的标记 —— 新人看不到任何英文 id。
    """
    d = _load("_rookie_sop_doc")
    rows = [
        _doc_row("wifi", "连上 WiFi", "已完成", acceptance="能上网"),
        _doc_row("desk", "找到工位", "未完成"),
    ]

    blocks, slots = d.build_doc_blocks(rows, name="张三")

    todos = [b for b in blocks if b["block_type"] == d.BLOCK_TODO]
    assert len(todos) == 2
    assert todos[0]["todo"]["style"]["done"] is True
    assert todos[1]["todo"]["style"]["done"] is False
    first = "".join(e["text_run"]["content"] for e in todos[0]["todo"]["elements"])
    assert "连上 WiFi" in first and "能上网" in first
    # 正文里不出现 item_id 这类英文标记
    assert "wifi" not in first
    assert slots == [("wifi", d.ROLE_READ), ("desk", d.ROLE_READ)]


def test_build_doc_blocks_gives_na_items_no_checkbox() -> None:
    """不适用的项不给 todo 框 —— 勾它没有意义。"""
    d = _load("_rookie_sop_doc")
    rows = [_doc_row("git_workflow", "Git 工作流", "不适用", module="开发环境")]

    blocks, slots = d.build_doc_blocks(rows, name="张三")

    assert not [b for b in blocks if b["block_type"] == d.BLOCK_TODO]
    assert slots == []
    assert any("不适用" in str(b) for b in blocks)


def test_read_doc_state_matches_by_block_id_not_by_order() -> None:
    """靠 block_id 映射认条目 —— 新人自己加的块不在映射里, 自然被忽略。"""
    d = _load("_rookie_sop_doc")
    rows = [_doc_row("wifi", "连上 WiFi", "未完成"), _doc_row("desk", "找到工位", "未完成")]
    blocks, slots = d.build_doc_blocks(rows, name="张三")
    stored = _slots_map(slots)
    read_back = _with_block_ids(blocks)
    # 新人自己加了一条笔记 —— 不在映射里
    read_back.append({"block_type": 17, "block_id": "blk_own_note", "todo": {"elements": [], "style": {"done": True}}})
    # 把「找到工位」勾上
    for b in read_back:
        if b.get("block_id") == "blk1":
            b["todo"]["style"]["done"] = True

    state, unclear = d.read_doc_state(read_back, stored)

    assert state == {"wifi": False, "desk": True}
    assert unclear == []


def test_diff_state_only_reports_newly_ticked_items() -> None:
    """只认「未完成 → 完成」一个方向。

    反向不撤销: 让新人取消勾选就能抹掉已完成记录, 会让 HR 日报不可信。
    """
    d = _load("_rookie_sop_doc")
    rows = [
        _doc_row("wifi", "连上 WiFi", "已完成"),
        _doc_row("desk", "找到工位", "未完成"),
        _doc_row("campus_card", "领取校园卡", "未完成"),
    ]
    doc_state = {
        "wifi": False,        # 文档里被取消勾选 —— 不撤销
        "desk": True,         # 新勾上的 —— 要同步
        "campus_card": False, # 没动
        "unknown": True,      # 表里没有这个条目 —— 忽略
    }

    assert d.diff_state(doc_state, rows) == ["desk"]


class _FakeDocApi:
    """假的 feishu_api: 记录调用并按脚本返回, 不碰飞书。"""

    def __init__(self, *, fail: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        self.children: list[dict[str, Any]] = []
        self._n = 0

    async def __call__(self, method: str, path: str, **kwargs: Any) -> str:
        self.calls.append((method, path))
        if self.fail and self.fail in path:
            return json.dumps({"ok": False, "msg": f"boom at {self.fail}"}, ensure_ascii=False)
        if path == "/open-apis/docx/v1/documents" and method == "POST":
            return json.dumps({"ok": True, "data": {"document": {"document_id": "doc123"}}}, ensure_ascii=False)
        if "/children" in path:
            body = json.loads(kwargs.get("body_json") or "{}")
            self.children.append(body)
            # 飞书会在返回体里给出每个新建块的 block_id —— provision_doc 靠它建
            # block_id → item_id 映射, 所以 fake 必须照样给。
            kids = []
            for child in body.get("children") or []:
                self._n += 1
                kids.append({"block_id": f"blk{self._n}", "block_type": child.get("block_type")})
            return json.dumps({"ok": True, "data": {"children": kids}}, ensure_ascii=False)
        if path.endswith("/blocks"):
            return json.dumps({"ok": True, "data": {"items": [], "has_more": False}}, ensure_ascii=False)
        return json.dumps({"ok": True}, ensure_ascii=False)


def test_provision_doc_creates_writes_grants_and_subscribes() -> None:
    """一人一份详情页的四步: 建文档 → 写清单 → 授权给他 → 订阅变更。"""
    da = _load("_rookie_sop_docapi")
    api = _FakeDocApi()
    rows = [_doc_row("wifi", "连上 WiFi", "未完成"), _doc_row("desk", "找到工位", "未完成")]

    out = anyio.run(lambda: da.provision_doc(api, open_id="ou_x", name="张三", rows=rows))

    assert out["ok"] is True
    assert out["document_id"] == "doc123"
    assert out["url"] == "https://feishu.cn/docx/doc123"
    assert out["granted"] is True and out["subscribed"] is True
    paths = [p for _, p in api.calls]
    assert "/open-apis/docx/v1/documents" in paths
    assert any("/children" in p for p in paths)
    assert any("/permissions/doc123/members" in p for p in paths)
    assert any("/files/doc123/subscribe" in p for p in paths)


def test_provision_doc_reports_grant_failure_instead_of_claiming_success() -> None:
    """授权失败意味着新人打不开自己的清单 —— 不能报成功。"""
    da = _load("_rookie_sop_docapi")
    api = _FakeDocApi(fail="/permissions/")
    rows = [_doc_row("wifi", "连上 WiFi", "未完成")]

    out = anyio.run(lambda: da.provision_doc(api, open_id="ou_x", name="张三", rows=rows))

    assert out["ok"] is False
    assert out["granted"] is False
    assert "boom" in out["grant_error"]


def test_provision_doc_reports_subscribe_failure() -> None:
    """订阅失败意味着他勾了没人知道 —— 同样不能报成功。"""
    da = _load("_rookie_sop_docapi")
    api = _FakeDocApi(fail="/subscribe")
    rows = [_doc_row("wifi", "连上 WiFi", "未完成")]

    out = anyio.run(lambda: da.provision_doc(api, open_id="ou_x", name="张三", rows=rows))

    assert out["ok"] is False
    assert out["subscribed"] is False


def test_append_blocks_chunks_large_payloads() -> None:
    """飞书对单次子块数有上限, 33 项 + 分节标题会超, 必须分批。"""
    da = _load("_rookie_sop_docapi")
    api = _FakeDocApi()
    blocks = [{"block_type": 17, "todo": {"elements": [], "style": {}}} for _ in range(120)]

    out = anyio.run(lambda: da.append_blocks(api, "doc123", blocks))

    assert out["ok"] is True and out["written"] == 120
    assert len(api.children) == 3  # 50 + 50 + 20


def test_read_blocks_stops_on_a_repeated_page_token() -> None:
    """服务端若一直回 has_more=true, 不能无限转 —— fire=tool 的同步会挂死整个回合。"""
    da = _load("_rookie_sop_docapi")

    class _Pathological:
        def __init__(self) -> None:
            self.n = 0

        async def __call__(self, method: str, path: str, **kwargs: Any) -> str:
            self.n += 1
            return json.dumps(
                {"ok": True, "data": {"items": [], "has_more": True, "page_token": "same"}}, ensure_ascii=False
            )

    api = _Pathological()
    out = anyio.run(lambda: da.read_blocks(api, "doc123"))

    assert out["ok"] is True
    assert api.n <= 3  # 同一个 token 立刻停, 不是转满 50 页


def test_sync_doc_id_resolves_from_several_payload_shapes() -> None:
    """事件可能把文档 token 放在 file_token 或 document_id, 两种都认; 认不出返回空串。"""
    s = _load("rookie_sop_sync_doc")

    assert s._doc_id_of({"document_id": "doc1"}) == "doc1"
    assert s._doc_id_of({"file_token": "doc2"}) == "doc2"
    assert s._doc_id_of({"token": "doc3"}) == "doc3"
    assert s._doc_id_of({"other": "x"}) == ""
    assert s._doc_id_of({"document_id": "   "}) == ""


def test_sync_doc_refuses_without_a_document_id() -> None:
    s = _load("rookie_sop_sync_doc")

    out = json.loads(anyio.run(lambda: s.rookie_sop_sync_doc()))

    assert out["ok"] is False
    assert "document_id" in out["error"]


def test_entry_card_is_one_card_with_a_doc_link_and_no_callbacks() -> None:
    """入口卡: 一条消息交代全貌, 只有一个跳转按钮, 没有回调动作。

    需求变更的核心 —— 原先一次发 7 张模块卡观感太糟。
    """
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 9), title="连上 WiFi"),
        _row("desk", p.STATUS_TODO, date(2026, 8, 9), title="找到工位"),
        _row("attendance", p.STATUS_TODO, date(2026, 8, 9), title="了解考勤", module="核心制度"),
    ]

    card, handlers = c.entry_card("王炜博", rows, "https://feishu.cn/docx/doc123", date(2026, 8, 6))

    # 没有任何回调 —— 勾选在文档里做, 不在卡上
    assert handlers == {}
    rendered = _dump(card)
    assert "王炜博" in rendered
    assert "已完成 1 / 3 项" in rendered
    # 按模块列出各自欠项
    assert "环境准备" in rendered and "核心制度" in rendered
    # 跳转按钮用 url, 不是 value/callback
    button = next(
        e["actions"][0] for e in card["elements"] if e.get("tag") == "action"
    )
    assert button["url"] == "https://feishu.cn/docx/doc123"
    assert "value" not in button


def test_entry_card_marks_a_fully_done_module_and_an_na_module() -> None:
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [
        _row("wifi", p.STATUS_DONE, date(2026, 8, 9), title="连上 WiFi"),
        _row("git_workflow", p.STATUS_NA, date(2026, 8, 12), title="Git 工作流", module="开发环境"),
    ]

    card, _handlers = c.entry_card("张三", rows, "https://feishu.cn/docx/d", date(2026, 8, 6))

    rendered = _dump(card)
    assert "✅" in rendered  # 环境准备 1/1 全完成
    assert "不适用" in rendered  # 开发环境整模块不适用


def test_entry_card_without_a_doc_url_omits_the_button() -> None:
    """文档还没建好时不该给一个空按钮。"""
    c = _load("_rookie_sop_card")
    p = _load("_rookie_sop_progress")
    rows = [_row("wifi", p.STATUS_TODO, date(2026, 8, 9), title="连上 WiFi")]

    card, handlers = c.entry_card("张三", rows, "", date(2026, 8, 6))

    assert handlers == {}
    assert not [e for e in card["elements"] if e.get("tag") == "action"]


def test_doc_edited_event_and_sync_trigger_are_registered() -> None:
    """文档变更事件与同步触发器都要在位, 否则勾选永远同步不回来。"""
    ev = HAITUN / "channel_events" / "feishu" / "rookie_doc_edited"
    trigger = HAITUN / "triggers" / "rookie-doc-sync" / "TRIGGER.md"
    assert (ev / "EVENT.yaml").is_file()
    assert (ev / "map.py").is_file()
    assert trigger.is_file()

    yaml_text = (ev / "EVENT.yaml").read_text(encoding="utf-8")
    assert "haitun.rookie.doc_edited" in yaml_text
    assert "drive.file.edit_v1" in yaml_text
    assert "platform_map" in yaml_text

    trigger_text = trigger.read_text(encoding="utf-8")
    assert "event: haitun.rookie.doc_edited" in trigger_text
    assert "fire: tool" in trigger_text
    assert "tool: rookie_sop_sync_doc" in trigger_text


def test_doc_edited_mapper_extracts_the_token_and_skips_other_file_types() -> None:
    """映射器: 认出文档 token, 忽略非文档的文件变更, 认不出就不产出信封。"""
    path = HAITUN / "channel_events" / "feishu" / "rookie_doc_edited" / "map.py"
    spec = importlib.util.spec_from_file_location("rookie_doc_edited_map", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    # 正常的文档编辑事件
    out = mod.map_event(
        {
            "header": {"event_id": "evt1"},
            "event": {"file_token": "doc123", "file_type": "docx", "operator_id": {"open_id": "ou_x"}},
        }
    )
    assert len(out) == 1
    assert out[0]["event"] == "haitun.rookie.doc_edited"
    assert out[0]["payload"]["document_id"] == "doc123"
    assert out[0]["payload"]["operator_open_id"] == "ou_x"
    assert out[0]["idempotency_key"] == "feishu:rookie_doc_edited:evt1"

    # 非文档类型(比如表格) —— 不该产出
    assert mod.map_event({"event": {"file_token": "sht1", "file_type": "sheet"}}) == []
    # 认不出 token —— 不该产出空 token 的信封让下游去读一个不存在的文档
    assert mod.map_event({"event": {"file_type": "docx"}}) == []


def _link_row(item_id: str, title: str, url: str, status: str = "未完成") -> dict[str, Any]:
    return {
        "记录键": f"ou_x:{item_id}",
        "项": title,
        "验收标准": "",
        "必读链接": url,
        "模块": "必读材料",
        "状态": status,
        "入职日": date(2026, 8, 5),
        "截止日": date(2026, 8, 7),
    }


def test_link_items_render_as_linked_title_plus_two_checkbox_groups() -> None:
    """必读材料: 超链接挂在**标题文字**上(不单独罗列 URL), 勾选拆成两组。

        📖 企业文化总则          ← 标题本身可点
        ☐ 已阅读
        ☐ 已完全理解   ☐ 未完全理解（会找人问清楚）

    第二组语义互斥, 但飞书 todo 块之间没有互斥机制, 所以互斥由 read_doc_state 裁决。
    """
    d = _load("_rookie_sop_doc")
    url = "https://genuineknowledge.feishu.cn/wiki/JyCLwr60lineYBkkJmQcZf0PnTb"
    rows = [_link_row("read_culture", "企业文化总则", url)]

    blocks, slots = d.build_doc_blocks(rows, name="张三")

    # 标题那一行: 链接挂在文字上, 正文里看不到裸 URL
    title_block = next(b for b in blocks if b["block_type"] == d.BLOCK_TEXT and "企业文化总则" in str(b))
    linked = [e for e in title_block["text"]["elements"] if "link" in str(e.get("text_run", {}))]
    assert linked, "标题应带超链接"
    assert linked[0]["text_run"]["content"] == "企业文化总则"
    assert linked[0]["text_run"]["text_element_style"]["link"]["url"] == url
    # 裸 URL 不该作为独立文字出现
    assert not any(e.get("text_run", {}).get("content") == url for e in title_block["text"]["elements"])

    # 三个勾选框, 各有角色; 正文里没有英文 item id
    todos = [b for b in blocks if b["block_type"] == d.BLOCK_TODO]
    labels = ["".join(e["text_run"]["content"] for e in b["todo"]["elements"]) for b in todos]
    assert len(todos) == 3
    assert "已阅读" in labels[0]
    assert "已完全理解" in labels[1]
    assert "未完全理解" in labels[2]
    assert all("read_culture" not in lab for lab in labels)
    assert slots == [
        ("read_culture", d.ROLE_READ),
        ("read_culture", d.ROLE_GOT_IT),
        ("read_culture", d.ROLE_UNCLEAR),
    ]


def test_link_item_counts_as_done_only_when_read_and_understood() -> None:
    """阅读类要「读过 + 明确表示理解」才算完成 —— 只勾已阅读不算。"""
    d = _load("_rookie_sop_doc")
    rows = [_link_row("read_culture", "企业文化总则", "https://x.example")]
    blocks, slots = d.build_doc_blocks(rows, name="张三")
    stored = _slots_map(slots)

    def state_for(read: bool, got_it: bool, unclear: bool) -> tuple[dict[str, bool], list[str]]:
        got = _with_block_ids(blocks)
        flags = {d.ROLE_READ: read, d.ROLE_GOT_IT: got_it, d.ROLE_UNCLEAR: unclear}
        for b in got:
            if b.get("block_type") != 17:
                continue
            _item, role = stored[b["block_id"]].rsplit(":", 1)
            b["todo"]["style"]["done"] = flags[role]
        return d.read_doc_state(got, stored)

    assert state_for(True, True, False)[0] == {"read_culture": True}
    assert state_for(True, False, False)[0] == {"read_culture": False}  # 读了但没表态
    assert state_for(False, True, False)[0] == {"read_culture": False}  # 没读却说懂了


def test_link_item_unclear_wins_over_got_it_and_is_reported() -> None:
    """两个都勾时以「未完全理解」为准, 并上报给 HR。

    宁可让 HR 多看一眼, 也不要把「没懂」误记成「懂了」。
    """
    d = _load("_rookie_sop_doc")
    rows = [_link_row("read_culture", "企业文化总则", "https://x.example")]
    blocks, slots = d.build_doc_blocks(rows, name="张三")
    stored = _slots_map(slots)
    got = _with_block_ids(blocks)
    for b in got:
        if b.get("block_type") == 17:
            b["todo"]["style"]["done"] = True  # 三个框全勾上

    state, unclear = d.read_doc_state(got, stored)

    assert state == {"read_culture": False}
    assert unclear == ["read_culture"]


def test_real_config_v3_has_the_three_required_readings() -> None:
    """钉死 V3 的真实数字: 28 项、3 个必读链接、5 个 dev_only。"""
    items = _real_items()
    links = [i for i in items if i.url]
    dev_only = [i for i in items if i.dev_only]

    assert len(items) == 28
    assert {i.item_id for i in links} == {"read_culture", "read_todo_spec", "read_dev_spec"}
    assert len(dev_only) == 5
    # 每个必读项都得有真链接, 不能留空
    assert all(i.url.startswith("https://") for i in links)
    # role_confirmed 仍是全员项(非 dev_only), 否则不答角色就能毕业
    assert any(i.item_id == "role_confirmed" and not i.dev_only for i in items)

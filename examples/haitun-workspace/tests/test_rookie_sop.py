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


def test_role_card_offers_exactly_two_choices() -> None:
    c = _load("_rookie_sop_card")

    card, handlers = c.role_card("Day 1-7 截止")

    assert handlers == {
        "rookie_role_dev": "rookie_sop_role_set",
        "rookie_role_nondev": "rookie_sop_role_set",
    }
    rendered = _dump(card)
    assert "我是研发" in rendered
    assert "我不是研发" in rendered


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


def _item(record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"record_id": record_id, "fields": fields}


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


def test_detail_row_fields_seeds_dev_only_items_as_na_when_role_is_unknown() -> None:
    """角色未答时开发环境项要直接种成 不适用, 否则 Day 1 分母会把它们算进去。"""
    s = _load("_rookie_sop_store")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    items = cfg.load_sop(_CFG)
    dev_item = next(i for i in items if i.dev_only)
    plain_item = next(i for i in items if not i.dev_only)

    dev_row = s.detail_row_fields(dev_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5))
    plain_row = s.detail_row_fields(plain_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5))

    assert dev_row["状态"] == p.STATUS_NA
    assert plain_row["状态"] == p.STATUS_TODO


def test_detail_row_fields_seeds_dev_only_items_as_todo_once_role_is_known() -> None:
    """一旦落地时角色已知(比如重放事件时已经问过), 开发环境项不用再等复活。"""
    s = _load("_rookie_sop_store")
    cfg = _load("_rookie_sop_config")
    p = _load("_rookie_sop_progress")
    dev_item = next(i for i in cfg.load_sop(_CFG) if i.dev_only)

    row = s.detail_row_fields(dev_item, open_id="ou_x", name="张三", onboard=date(2026, 8, 5), role_label="研发")

    assert row["状态"] == p.STATUS_TODO


def test_day_one_denominator_excludes_dev_items_before_role_is_chosen() -> None:
    """Day 1: 三个非开发项 + 一个开发项(种成不适用)分母应是 3, 不是 4 ——
    这正是 design 里「角色未答时开发环境不进分母」这条规格落到明细表里的效果。
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

    assert got.total == len([i for i in items if not i.dev_only])
    assert got.total == 3


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
    fake = _FakeBitable([[_item("recOv", {"open_id": "ou_x"})]])

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
    fake = _FakeBitable([[_item("recOv", {"open_id": "ou_x", "进度": "99/99", "逾期项数": 42})]])

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
    # 开发环境先问角色, 不直接列 5 项
    assert by_module["开发环境"]["is_role_card"] is True
    assert by_module["开发环境"]["handlers"] == {
        "rookie_role_dev": "rookie_sop_role_set",
        "rookie_role_nondev": "rookie_sop_role_set",
    }
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


def test_role_set_surfaces_card_send_failure_as_not_ok(monkeypatch: Any) -> None:
    """卡发送失败(含"发了但快照没存下来")时, 工具必须报 ok=false, 不能报成功。"""
    rs = _load("rookie_sop_role_set")
    fake = _FakeBitable(
        [
            _dev_detail_rows(),  # fetch_detail 拿开发环境明细行, 用于 适用角色 改写
            _dev_detail_rows(),  # 复活/改写后重拉一次明细
        ]
    )
    rs._rt.bitable_adapter = lambda: fake

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    rs._rt.load_state = _fake_load_state

    async def _fake_send_card_fails(*args: Any, **kwargs: Any) -> str:
        # send_card_impl 描述的"卡发出去了但快照没存下来"路径: ok=False 且
        # callback_context_saved=False —— 按钮全是死的, 必须当失败处理。
        return json.dumps(
            {
                "ok": False,
                "message": "Feishu card was sent, but its callback context could not be saved",
                "sent": True,
                "callback_context_saved": False,
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(rs, "feishu_message_send_card", _fake_send_card_fails)

    out = json.loads(anyio.run(lambda: rs.rookie_sop_role_set(_role_callback("dev"))))

    assert out["ok"] is False
    assert "error" in out


def test_role_set_switching_nondev_to_dev_revives_all_five_rows(monkeypatch: Any) -> None:
    """先选非研发(五行标不适用), 再选研发: 新卡必须拿到全部五行活的勾选行, 不能是 0。"""
    rs = _load("rookie_sop_role_set")
    na_rows = _dev_detail_rows(status="不适用")
    fake = _FakeBitable(
        [
            na_rows,  # fetch_detail: 改写 适用角色 前先拉明细, 五行都还是 不适用
            _dev_detail_rows(status="未完成"),  # 复活写回后重拉一次明细, 五行都变回未完成
            [],  # recompute_overview 查总览 —— 没有, 走创建
        ]
    )
    rs._rt.bitable_adapter = lambda: fake

    async def _fake_load_state() -> dict[str, Any]:
        return {}

    rs._rt.load_state = _fake_load_state

    sent_cards: list[dict[str, Any]] = []

    async def _fake_send_card_ok(
        receive_id: str,
        card_json: str,
        receive_id_type: str = "chat_id",
        user_key: str = "",
        business_context_json: str = "{}",
        action_handlers_json: str = "{}",
        multi_use: bool = False,
    ) -> str:
        sent_cards.append({"card": json.loads(card_json), "handlers": json.loads(action_handlers_json)})
        return json.dumps(
            {"ok": True, "callback_context_saved": True, "message_id": "om_1", "thread_id": "", "chat_id": ""},
            ensure_ascii=False,
        )

    monkeypatch.setattr(rs, "feishu_message_send_card", _fake_send_card_ok)

    out = json.loads(anyio.run(lambda: rs.rookie_sop_role_set(_role_callback("dev"))))

    assert out["ok"] is True
    assert out["dev_items"] == 5
    # 复活写回: 五行 不适用 → 未完成 都要发生, 不是只改了 适用角色 标签
    revive_call = next(u for u in fake.updates if any(f["fields"].get("状态") == "未完成" for f in u["records"]))
    assert len(revive_call["records"]) == 5
    # 新卡真的带着 5 个可点的 handlers, 不是零行死卡
    assert len(sent_cards[0]["handlers"]) == 5


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
    page1 = _item("recOv1", {"open_id": "ou_x", "姓名": "张三", "状态": "进行中"})
    page2 = _item("recOv2", {"open_id": "ou_y", "姓名": "李四", "状态": "进行中"})
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

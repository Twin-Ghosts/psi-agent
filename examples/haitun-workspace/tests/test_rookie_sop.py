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

    ``search_results`` 里每一页默认是「一批 item 字典」(单页, has_more=False,
    向后兼容原有测试); 若要测多页翻页, 传三元组
    ``(items, has_more, page_token)`` 代替。
    """

    def __init__(
        self,
        search_results: list[list[dict[str, Any]] | tuple[list[dict[str, Any]], bool, str]] | None = None,
    ) -> None:
        self._search_results = list(search_results or [])
        self.searches: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

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
        result = {"items": items, "has_more": has_more, "page_token": next_token}
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False)

    async def create_records(
        self,
        app_token: str,
        table_id: str,
        records_json: str,
        user_key: str = "",
        identity: str = "",
        validate_fields: bool = True,
    ) -> str:
        self.creates.append({"table_id": table_id, "records": json.loads(records_json)})
        return json.dumps({"ok": True}, ensure_ascii=False)

    async def update_records(
        self,
        app_token: str,
        table_id: str,
        records_json: str,
        user_key: str = "",
        identity: str = "",
        validate_fields: bool = True,
    ) -> str:
        self.updates.append({"table_id": table_id, "records": json.loads(records_json)})
        return json.dumps({"ok": True}, ensure_ascii=False)


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

    rows = anyio.run(s.fetch_detail, fake, "app1", "tblDetail", "ou_x")

    assert len(rows) == 1
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

    rows = anyio.run(s.fetch_detail, fake, "app1", "tblDetail", "ou_x")

    assert [r["record_id"] for r in rows] == ["rec1", "rec2"]
    assert len(fake.searches) == 2
    # 第一次不带 token, 第二次带上第一页返回的 token
    assert fake.searches[0]["page_token"] == ""
    assert fake.searches[1]["page_token"] == "tok1"


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

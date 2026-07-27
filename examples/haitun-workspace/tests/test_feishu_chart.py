"""Tests for the Feishu chart tools — parsing, rendering, and doc placement.

Renders are real (matplotlib to a temp PNG) since the whole value of these tools is
that a legible file comes out; the Feishu API calls are faked, so nothing here needs
credentials or network.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_cr: Any = importlib.import_module("_chart_render")
_place: Any = importlib.import_module("_chart_place")
_impl: Any = importlib.import_module("_feishu_impl")
_chart: Any = importlib.import_module("feishu_chart")


@pytest.fixture(autouse=True)
def _workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point chart output at a temp dir so tests never write into the real workspace."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))


# ── Input parsing ──────────────────────────────────────────────────────────────


def test_parse_values_accepts_model_number_formats() -> None:
    assert _cr.parse_values('[1234, "1,234", "85%", "￥1200", 3.5]') == [1234.0, 1234.0, 85.0, 1200.0, 3.5]


def test_parse_values_rejects_non_numeric() -> None:
    with pytest.raises(_cr.ChartDataError, match="must be a number"):
        _cr.parse_values('["abc"]')


def test_parse_values_rejects_bool() -> None:
    with pytest.raises(_cr.ChartDataError, match="boolean"):
        _cr.parse_values("[true]")


def test_parse_labels_rejects_empty() -> None:
    with pytest.raises(_cr.ChartDataError, match="non-empty"):
        _cr.parse_labels("[]")


def test_parse_series_object_preserves_names_and_order() -> None:
    series = _cr.parse_series('{"2025":[1,2],"2026":[3,4]}')
    assert [name for name, _v in series] == ["2025", "2026"]
    assert series[0][1] == [1.0, 2.0]


def test_parse_series_array_autonames() -> None:
    series = _cr.parse_series("[[1,2],[3,4]]")
    assert [name for name, _v in series] == ["系列1", "系列2"]


def test_check_series_length_reports_mismatch() -> None:
    with pytest.raises(_cr.ChartDataError, match="one value per label"):
        _cr.check_series_length([("A", [1.0, 2.0, 3.0])], ["x", "y"])


def test_parse_matrix_validates_shape() -> None:
    with pytest.raises(_cr.ChartDataError, match="2 row labels"):
        _cr.parse_matrix("[[1,2]]", 2, 2)


def test_parse_matrix_validates_row_width() -> None:
    with pytest.raises(_cr.ChartDataError, match="3 column labels"):
        _cr.parse_matrix("[[1,2],[3,4]]", 2, 3)


def test_parse_pairs_keeps_insertion_order() -> None:
    assert _cr.parse_pairs('{"华东":118,"华北":92}') == [("华东", 118.0), ("华北", 92.0)]


def test_parse_pairs_rejects_array() -> None:
    with pytest.raises(_cr.ChartDataError, match="JSON object"):
        _cr.parse_pairs("[1,2]")


def test_parse_point_groups_single_and_named() -> None:
    single = _cr.parse_point_groups("[[1,2],[3,4]]")
    assert single == [("", [[1.0, 2.0], [3.0, 4.0]])]
    named = _cr.parse_point_groups('{"直营":[[1,2]],"加盟":[[3,4]]}')
    assert [name for name, _p in named] == ["直营", "加盟"]


def test_parse_points_requires_enough_dimensions() -> None:
    with pytest.raises(_cr.ChartDataError, match="at least 3 numbers"):
        _cr.parse_points("[[1,2]]", dims=3)


# ── Gantt date handling ────────────────────────────────────────────────────────


def test_parse_gantt_tasks_converts_dates_to_day_offsets() -> None:
    raw = json.dumps(
        [
            {"name": "评审", "start": "2026-08-01", "end": "2026-08-04", "group": "产品"},
            {"name": "开发", "start": "2026-08-05", "days": 10, "group": "研发"},
        ],
        ensure_ascii=False,
    )
    tasks, ticks, today = _parse_gantt(raw, today="2026-08-06")
    # end is inclusive: 08-01..08-04 is four days, not three
    assert tasks[0] == ("评审", 0.0, 4.0, "产品")
    assert tasks[1] == ("开发", 4.0, 10.0, "研发")
    assert ticks[0] == "08-01"
    assert today == 5.0


def _parse_gantt(raw: str, *, start: str = "", today: str = "") -> tuple[Any, Any, Any]:
    return _cr.parse_gantt_tasks(raw, start, today)


def test_parse_gantt_tasks_requires_end_or_days() -> None:
    with pytest.raises(_cr.ChartDataError, match='needs either an "end" date or "days"'):
        _cr.parse_gantt_tasks('[{"name":"a","start":"2026-08-01"}]', "", "")


def test_parse_gantt_tasks_rejects_reversed_range() -> None:
    with pytest.raises(_cr.ChartDataError, match="ends before it starts"):
        _cr.parse_gantt_tasks('[{"name":"a","start":"2026-08-05","end":"2026-08-01"}]', "", "")


def test_parse_gantt_tasks_rejects_bad_date() -> None:
    with pytest.raises(_cr.ChartDataError, match="not a valid date"):
        _cr.parse_gantt_tasks('[{"name":"a","start":"2026-13-40","days":2}]', "", "")


def test_parse_gantt_tasks_honours_explicit_origin() -> None:
    tasks, ticks, _today = _cr.parse_gantt_tasks('[{"name":"a","start":"2026-08-05","days":2}]', "2026-08-01", "")
    assert tasks[0][1] == 4.0
    assert ticks[0] == "08-01"


# ── Chart-type guardrails: refuse to draw a misleading chart ───────────────────


def test_pie_rejects_negative_values() -> None:
    with pytest.raises(_cr.ChartDataError, match="negative"):
        _cr.draw_pie(["a", "b"], [-1.0, 2.0])


def test_pie_rejects_zero_total() -> None:
    with pytest.raises(_cr.ChartDataError, match="sum to 0"):
        _cr.draw_pie(["a", "b"], [0.0, 0.0])


def test_pie_folds_tail_into_other() -> None:
    labels = [f"c{i}" for i in range(10)]
    values = [float(i + 1) for i in range(10)]
    _draw, folded = _cr.draw_pie(labels, values)
    assert folded == 4  # 10 categories, 6 kept


def test_pie_keeps_small_sets_unfolded() -> None:
    _draw, folded = _cr.draw_pie(["a", "b", "c"], [3.0, 2.0, 1.0])
    assert folded == 0


def test_stacked_area_rejects_negatives() -> None:
    with pytest.raises(_cr.ChartDataError, match="negative"):
        _cr.draw_stacked_area(["q1"], [("a", [-1.0])])


def test_stacked_bar_rejects_negatives() -> None:
    with pytest.raises(_cr.ChartDataError, match="negative"):
        _cr.draw_bar(["q1"], [("a", [-1.0])], stacked=True)


def test_percent_requires_stacked() -> None:
    with pytest.raises(_cr.ChartDataError, match="only applies to stacked"):
        _cr.draw_bar(["q1"], [("a", [1.0])], percent=True)


def test_radar_requires_three_axes() -> None:
    with pytest.raises(_cr.ChartDataError, match="at least 3 axes"):
        _cr.draw_radar(["a", "b"], [("x", [1.0, 2.0])])


def test_funnel_requires_positive_first_stage() -> None:
    with pytest.raises(_cr.ChartDataError, match="100% baseline"):
        _cr.draw_funnel(["a", "b"], [0.0, 0.0])


def test_histogram_requires_two_values() -> None:
    with pytest.raises(_cr.ChartDataError, match="at least 2 values"):
        _cr.draw_histogram([1.0])


def test_box_requires_two_observations_per_group() -> None:
    with pytest.raises(_cr.ChartDataError, match="at least 2 values"):
        _cr.draw_box([("a", [1.0])])


def test_combo_requires_both_kinds_of_series() -> None:
    with pytest.raises(_cr.ChartDataError, match="at least one bar series"):
        _cr.draw_combo(["q1"], [], [("rate", [1.0])])


# ── Formatting helpers ─────────────────────────────────────────────────────────


def test_fmt_number_thousands_and_unit() -> None:
    assert _cr._fmt_number(12480.0, "万") == "12,480万"


def test_fmt_number_picks_precision_by_magnitude() -> None:
    assert _cr._fmt_number(0.853) == "0.85"
    assert _cr._fmt_number(4.5) == "4.5"
    assert _cr._fmt_number(120.0) == "120"


def test_linear_fit_recovers_known_slope() -> None:
    slope, intercept = _cr._linear_fit([1.0, 2.0, 3.0], [3.0, 5.0, 7.0])
    assert round(slope, 6) == 2.0
    assert round(intercept, 6) == 1.0


def test_linear_fit_handles_zero_variance() -> None:
    slope, intercept = _cr._linear_fit([2.0, 2.0], [1.0, 3.0])
    assert slope == 0.0
    assert intercept == 2.0


def test_fold_tail_sorts_descending() -> None:
    labels, values, folded = _cr._fold_tail(["a", "b", "c"], [1.0, 3.0, 2.0], 6)
    assert labels == ["b", "c", "a"]
    assert values == [3.0, 2.0, 1.0]
    assert folded == 0


# ── Real rendering: a PNG that actually comes out ──────────────────────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_render_to_png_writes_a_real_png(tmp_path: Path) -> None:
    draw, _folded = _cr.draw_pie(["研发", "市场"], [3.0, 1.0], title="中文标题")
    out = tmp_path / "nested" / "chart.png"
    path = await _cr.render_to_png(draw, str(out))
    data = await anyio.Path(path).read_bytes()
    assert data.startswith(_PNG_MAGIC)  # a real PNG, parent dirs created
    assert len(data) > 5000  # not a blank canvas


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "kwargs"),
    [
        (lambda: _chart.feishu_chart_pie, ('["研发","市场"]', "[3,1]"), {"unit": "人"}),
        (lambda: _chart.feishu_chart_donut, ('["华东","华北"]', "[520,310]"), {"unit": "万"}),
        (lambda: _chart.feishu_chart_funnel, ('["访问","付费"]', "[100,20]"), {}),
        (lambda: _chart.feishu_chart_line, ('["1月","2月"]', '{"A":[1,2]}'), {}),
        (lambda: _chart.feishu_chart_area, ('["1月","2月"]', '{"A":[1,2]}'), {}),
        (lambda: _chart.feishu_chart_stacked_area, ('["Q1","Q2"]', '{"a":[1,2],"b":[2,3]}'), {"percent": True}),
        (lambda: _chart.feishu_chart_column, ('["研发","市场"]', "[42,28]"), {"highlight": 0}),
        (lambda: _chart.feishu_chart_bar, ('["华东区域","华北区域"]', "[520,310]"), {}),
        (lambda: _chart.feishu_chart_grouped_column, ('["Q1","Q2"]', '{"计划":[1,2],"实际":[2,3]}'), {}),
        (lambda: _chart.feishu_chart_stacked_column, ('["Q1","Q2"]', '{"a":[1,2],"b":[2,3]}'), {}),
        (lambda: _chart.feishu_chart_waterfall, ('["期初","新签","流失"]', "[500,220,-90]"), {}),
        (lambda: _chart.feishu_chart_histogram, ("[1,2,2,3,4,5,6]",), {}),
        (lambda: _chart.feishu_chart_box, ('{"研发":[1,2,3],"市场":[2,3,4]}',), {}),
        (lambda: _chart.feishu_chart_scatter, ("[[1,2],[3,4],[5,7]]",), {}),
        (lambda: _chart.feishu_chart_bubble, ("[[1,2,10],[3,4,20]]",), {"size_label": "规模"}),
        (lambda: _chart.feishu_chart_heatmap, ('["周一","周二"]', '["上午","下午"]', "[[1,2],[3,4]]"), {}),
        (lambda: _chart.feishu_chart_radar, ('["技术","沟通","交付"]', '{"张三":[4,3,5]}'), {"max_value": 5}),
        (lambda: _chart.feishu_chart_pareto, ('["A","B","C"]', "[120,85,10]"), {}),
        (lambda: _chart.feishu_chart_combo, ('["1月","2月"]', '{"营收":[1,2]}', '{"毛利率":[30,35]}'), {}),
        (
            lambda: _chart.feishu_chart_gantt,
            ('[{"name":"开发","start":"2026-08-01","days":5,"group":"研发"}]',),
            {"today": "2026-08-03"},
        ),
        (lambda: _chart.feishu_chart_progress, ('{"华东":118,"华北":92}',), {"target": 100}),
    ],
)
async def test_every_chart_tool_renders(tool: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Each tool, called with no document_id, must produce a real PNG on disk."""
    result = json.loads(await tool()(*args, **kwargs))
    assert result["ok"] is True, result.get("message")
    data = await anyio.Path(result["image_path"]).read_bytes()
    assert data.startswith(_PNG_MAGIC)
    assert "no document_id" in result["note"]


@pytest.mark.asyncio
async def test_tool_reports_data_error_as_result_not_exception() -> None:
    result = json.loads(await _chart.feishu_chart_pie('["a","b"]', "[1,2,3]"))
    assert result["ok"] is False
    assert "2 labels but 3 values" in result["message"]


@pytest.mark.asyncio
async def test_scatter_accepts_grouped_input() -> None:
    result = json.loads(await _chart.feishu_chart_scatter('{"直营":[[1,2],[3,4]],"加盟":[[2,1],[4,3]]}'))
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_bubble_rejects_label_count_mismatch() -> None:
    result = json.loads(await _chart.feishu_chart_bubble("[[1,2,3]]", labels_json='["a","b"]'))
    assert result["ok"] is False
    assert "2 labels but 1 bubbles" in result["message"]


@pytest.mark.asyncio
async def test_chart_filename_includes_type_and_title() -> None:
    result = json.loads(await _chart.feishu_chart_pie('["a"]', "[1]", title="人力占比"))
    name = Path(result["image_path"]).name
    assert name.startswith("pie-")
    assert "人力占比" in name
    assert name.endswith(".png")


# ── Placing a chart into a docx as an image block ──────────────────────────────


class _FakeFeishu:
    """Records each _invoke call so the create → upload → patch sequence can be asserted."""

    def __init__(self, *, fail_at: str = "") -> None:
        self.calls: list[Any] = []
        self.fail_at = fail_at

    async def __call__(self, request: Any, user_key: str | None = None, prefer: str = "tenant") -> dict[str, Any]:
        self.calls.append(request)
        uri = getattr(request, "uri", "")
        method = request.http_method.name
        if "medias/upload_all" in uri:
            if self.fail_at == "upload":
                return {"ok": False, "message": "upload rejected"}
            return {"ok": True, "data": {"file_token": "tok_img"}}
        if method == "PATCH":
            if self.fail_at == "patch":
                return {"ok": False, "message": "patch rejected"}
            return {"ok": True, "data": {}}
        if method == "DELETE":
            return {"ok": True, "data": {}}
        if "children" in uri:
            return {"ok": True, "data": {"children": [{"block_id": "blk1"}], "index": 3}}
        return {"ok": True, "data": {}}

    def uris(self) -> list[str]:
        return [getattr(c, "uri", "") for c in self.calls]


@pytest.mark.asyncio
async def test_append_doc_image_runs_create_upload_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(_PNG_MAGIC + b"0" * 100)
    fake = _FakeFeishu()
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = await _impl.append_doc_image_impl("doc1", str(png))
    assert result["ok"] is True
    assert result["block_id"] == "blk1"
    assert result["file_token"] == "tok_img"
    methods = [c.http_method.name for c in fake.calls]
    assert methods == ["POST", "POST", "PATCH"]  # create block, upload media, bind token
    # The upload must target the new block, not a Drive folder.
    upload = fake.calls[1]
    assert upload.body["parent_type"] == "docx_image"
    assert upload.body["parent_node"] == "blk1"
    assert fake.calls[2].body["replace_image"]["token"] == "tok_img"


@pytest.mark.asyncio
async def test_append_doc_image_writes_caption(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(_PNG_MAGIC + b"0" * 100)
    fake = _FakeFeishu()
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = await _impl.append_doc_image_impl("doc1", str(png), "图1：人力分布")  # noqa: RUF001
    assert result["caption_written"] is True
    assert any("blocks/:block_id/children" in u for u in fake.uris()[3:])


@pytest.mark.asyncio
async def test_append_doc_image_cleans_up_after_failed_upload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(_PNG_MAGIC + b"0" * 100)
    fake = _FakeFeishu(fail_at="upload")
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = await _impl.append_doc_image_impl("doc1", str(png))
    assert result["ok"] is False
    # the empty placeholder block must not be left behind
    assert "DELETE" in [c.http_method.name for c in fake.calls]


@pytest.mark.asyncio
async def test_append_doc_image_cleans_up_after_failed_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    png = tmp_path / "c.png"
    png.write_bytes(_PNG_MAGIC + b"0" * 100)
    fake = _FakeFeishu(fail_at="patch")
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = await _impl.append_doc_image_impl("doc1", str(png))
    assert result["ok"] is False
    assert "DELETE" in [c.http_method.name for c in fake.calls]


@pytest.mark.asyncio
async def test_append_doc_image_requires_document_id() -> None:
    result = await _impl.append_doc_image_impl("  ", "x.png")
    assert result["ok"] is False
    assert "document_id" in result["message"]


@pytest.mark.asyncio
async def test_append_doc_image_reports_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFeishu()
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = await _impl.append_doc_image_impl("doc1", "no/such/chart.png")
    assert result["ok"] is False
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_chart_tool_places_into_document(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFeishu()
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = json.loads(
        await _chart.feishu_chart_pie(
            '["研发","市场"]',
            "[3,1]",
            document_id="doc1",
            caption="图1：占比",  # noqa: RUF001
        )
    )
    assert result["ok"] is True
    assert result["block_id"] == "blk1"
    assert result["chart_type"] == "pie"
    assert await anyio.Path(result["image_path"]).exists()


@pytest.mark.asyncio
async def test_chart_tool_keeps_png_when_placement_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFeishu(fail_at="upload")
    monkeypatch.setattr(_impl, "_invoke", fake)
    result = json.loads(await _chart.feishu_chart_pie('["研发","市场"]', "[3,1]", document_id="doc1"))
    assert result["ok"] is False
    # the rendered chart is still usable — say so rather than implying total failure
    assert await anyio.Path(result["image_path"]).exists()
    assert "usable" in result["hint"]


@pytest.mark.asyncio
async def test_pie_reports_folded_slices_to_the_caller() -> None:
    labels = json.dumps([f"部门{i}" for i in range(9)], ensure_ascii=False)
    values = json.dumps(list(range(1, 10)))
    result = json.loads(await _chart.feishu_chart_pie(labels, values))
    assert result["ok"] is True
    assert result["folded_into_other"] == 3

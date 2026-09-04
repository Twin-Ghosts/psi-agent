"""Tests for the ``web_search`` tool (Bocha / 博查) and its private helper.

Covers key/argument validation, request-body construction (count clamp, site
normalisation, summary flag), HTTP/business-error mapping and response
shaping. The network is never touched: every test drives the transport seam
``_web_search_impl._http_post`` with a fake that returns the exact tuple the
real one would.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = WORKSPACE_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

impl = importlib.import_module("_web_search_impl")
web_search = importlib.import_module("web_search").web_search


def _sample_payload() -> dict:
    """A realistic Bocha web-search response (Bing-style webPages envelope)."""
    return {
        "code": 200,
        "data": {
            "_type": "SearchResponse",
            "webPages": {
                "webSearchUrl": "https://bochaai.com/search?q=x",
                "totalEstimatedMatches": 606721,
                "value": [
                    {
                        "name": "阿里巴巴发布2024年ESG报告",
                        "url": "https://www.alibabagroup.com/report",
                        "snippet": "报告显示, 阿里巴巴持续推进减碳与普惠…",
                        "summary": "阿里巴巴发布2024财年ESG报告…",
                        "siteName": "阿里巴巴集团",
                        "dateLastCrawled": "2026-05-01T00:00:00.0000000Z",
                        "language": "zh",
                    }
                ],
            },
        },
    }


def _fake_post(response: tuple) -> object:
    """Transport seam that always returns *response* without any I/O."""

    async def _fake(url: str, headers: dict, body: dict, timeout_s: float):
        del url, headers, body, timeout_s
        return response

    return _fake


@pytest.fixture
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOCHA_API_KEY", "sk-test-key")


async def test_missing_key_is_actionable_without_network(monkeypatch):
    monkeypatch.delenv("BOCHA_API_KEY", raising=False)

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("no request may be sent without a key")

    monkeypatch.setattr(impl, "_http_post", _must_not_run)
    result = await impl.web_search_impl("阿里巴巴 ESG")

    assert result["ok"] is False
    assert "BOCHA_API_KEY" in result["message"]
    assert "serper_google_search" in result["message"]


async def test_empty_query_rejected(_configured, monkeypatch):
    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("no request may be sent for an empty query")

    monkeypatch.setattr(impl, "_http_post", _must_not_run)
    result = await impl.web_search_impl("   ")

    assert result["ok"] is False
    assert "query" in result["message"]


async def test_freshness_is_validated(_configured, monkeypatch):
    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("no request may be sent for an invalid freshness")

    monkeypatch.setattr(impl, "_http_post", _must_not_run)
    result = await impl.web_search_impl("news", freshness="lastHour")

    assert result["ok"] is False
    assert "oneWeek" in result["message"]


async def test_success_shapes_results(_configured, monkeypatch):
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, _sample_payload(), "{}")))
    result = await impl.web_search_impl("阿里巴巴 ESG 报告", count=10)

    assert result["ok"] is True
    assert result["provider"] == "bocha"
    assert result["query"] == "阿里巴巴 ESG 报告"
    assert result["count"] == 1
    assert result["total_estimated"] == 606721
    row = result["results"][0]
    assert row["title"].startswith("阿里巴巴")
    assert row["url"].startswith("https://www.alibabagroup.com")
    assert row["site"] == "阿里巴巴集团"
    assert row["date"]
    assert row["snippet"]
    assert row["summary"]


async def test_tool_layer_returns_parseable_json(_configured, monkeypatch):
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, _sample_payload(), "{}")))
    text = await web_search("阿里巴巴 ESG 报告")

    parsed = json.loads(text)
    assert parsed["ok"] is True
    assert parsed["results"][0]["url"].startswith("https://")


async def test_body_builds_count_site_and_summary(_configured, monkeypatch):
    captured: list[dict] = []

    async def _capture(url: str, headers: dict, body: dict, timeout_s: float):
        del url, headers, timeout_s
        captured.append(body)
        return 200, _sample_payload(), "{}"

    monkeypatch.setattr(impl, "_http_post", _capture)
    result = await impl.web_search_impl(
        "  ESG ", count=999, freshness="oneWeek", summary=False, site="https://zhihu.com/a/b"
    )
    assert result["ok"] is True

    body = captured[0]
    assert body == {
        "query": "ESG",
        "count": 50,  # clamped to Bocha's max
        "freshness": "oneWeek",
        "site": "zhihu.com",  # scheme and path stripped
    }
    assert "summary" not in body  # summary=False omits the flag


async def test_alias_fields_tolerate_schema_drift(_configured, monkeypatch):
    payload = {
        "data": {
            "webPages": {
                "totalEstimatedMatches": 42,
                "value": [
                    {
                        "title": "标题走 title",
                        "link": "https://example.com/x",
                        "content": "片段走 content",
                        "domain": "example.com",
                        "datePublished": "2026-04-30",
                    }
                ],
            }
        }
    }
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, payload, "{}")))
    result = await impl.web_search_impl("drift")

    assert result["total_estimated"] == 42
    row = result["results"][0]
    assert row["title"] == "标题走 title"
    assert row["url"] == "https://example.com/x"
    assert row["snippet"] == "片段走 content"
    assert row["site"] == "example.com"
    assert row["date"] == "2026-04-30"


async def test_flat_results_list_is_accepted(_configured, monkeypatch):
    payload = {"code": 200, "data": {"results": [{"name": "A", "url": "https://a.example/1"}]}}
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, payload, "{}")))
    result = await impl.web_search_impl("flat")

    assert result["ok"] is True
    assert result["count"] == 1


async def test_entries_without_title_or_url_are_dropped(_configured, monkeypatch):
    payload = {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {"name": "usable", "url": "https://a.example/1"},
                    {"snippet": "no title no url"},
                    "not-a-dict",
                    {},
                ]
            }
        },
    }
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, payload, "{}")))
    result = await impl.web_search_impl("clean")

    assert result["count"] == 1
    assert result["results"][0]["title"] == "usable"


async def test_business_error_code_maps_to_message(_configured, monkeypatch):
    payload = {"code": 429, "msg": "请求过于频繁, 请稍后重试"}
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, payload, "{}")))
    result = await impl.web_search_impl("busy")

    assert result["ok"] is False
    assert result["code"] == 429
    assert "请求过于频繁" in result["message"]


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {"message": "unauthorized"}, "BOCHA_API_KEY"),
        (429, {"msg": "rate limited"}, "try again later"),
        (500, {"detail": "boom"}, "HTTP 500"),
    ],
)
async def test_http_errors_are_actionable(_configured, monkeypatch, status, payload, expected):
    monkeypatch.setattr(impl, "_http_post", _fake_post((status, payload, "{}")))
    result = await impl.web_search_impl("err")

    assert result["ok"] is False
    assert expected in result["message"]
    assert result["status"] == status


async def test_transport_failure_reports_as_status_zero(_configured, monkeypatch):
    monkeypatch.setattr(impl, "_http_post", _fake_post((0, None, "timed out after 20s")))
    result = await impl.web_search_impl("net")

    assert result["ok"] is False
    assert "timed out after 20s" in result["message"]


async def test_non_json_success_response_is_rejected(_configured, monkeypatch):
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, None, "<html>oops</html>")))
    result = await impl.web_search_impl("html")

    assert result["ok"] is False
    assert "non-JSON" in result["message"]


async def test_empty_result_set_is_ok(_configured, monkeypatch):
    payload = {"code": 200, "data": {"webPages": {"value": []}}}
    monkeypatch.setattr(impl, "_http_post", _fake_post((200, payload, "{}")))
    result = await impl.web_search_impl("nothing")

    assert result["ok"] is True
    assert result["results"] == []

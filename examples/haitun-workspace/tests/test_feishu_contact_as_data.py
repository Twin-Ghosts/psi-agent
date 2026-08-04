"""Parity: the ``feishu-contact`` skill reaches Feishu the same way the tools did.

The contact domain is the pilot for moving endpoint knowledge out of Python and into
a Markdown table. Migrating it is only defensible if the wire traffic is unchanged —
so each test here builds a request through the generic ``feishu_api`` path driven by
``skills/feishu-contact/SKILL.md``, builds the same call through the hand-written
``_build_*`` helper the dedicated tool uses, and compares what would actually be sent.

Comparing the outgoing ``BaseRequest`` rather than a parsed response is deliberate:
the ``_build_*`` helpers *are* the endpoint knowledge being replaced, so they are the
only honest reference for what "unchanged" means.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from lark_channel.core.enum import AccessTokenType, HttpMethod
from lark_channel.core.model import BaseRequest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_spec: Any = importlib.import_module("_feishu_spec")
_api: Any = importlib.import_module("_feishu_api_impl")
_impl: Any = importlib.import_module("_feishu_impl")

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"


def _shape(req: BaseRequest) -> dict[str, Any]:
    """The part of a request that determines what Feishu receives."""
    return {
        "method": req.http_method,
        "uri": req.uri,
        "paths": dict(req.paths or {}),
        "queries": sorted((k, str(v)) for k, v in (req.queries or [])),
        "body": req.body or None,
        "tokens": set(req.token_types or set()),
    }


class _CapturedInvoke:
    """Stands in for ``_invoke`` and keeps the request instead of sending it."""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[BaseRequest] = []
        self._pages = pages or [{"ok": True, "data": {}}]

    async def __call__(self, request: BaseRequest, **_: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self._pages[min(len(self.requests) - 1, len(self._pages) - 1)]

    @property
    def request(self) -> BaseRequest:
        assert len(self.requests) == 1, f"expected 1 request, got {len(self.requests)}"
        return self.requests[0]


@pytest.fixture(autouse=True)
def _real_skills(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive the generic path from the shipped skill files, not a synthetic fixture."""
    _spec.reset_cache()
    monkeypatch.setattr(_api, "_skills_dir", lambda: str(SKILLS_DIR))
    yield
    _spec.reset_cache()


def _generic(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> _CapturedInvoke:
    cap = _CapturedInvoke(pages)
    monkeypatch.setattr(_impl, "_invoke", cap)
    anyio.run(lambda: _api.call_api_impl(**kwargs))
    return cap


# ----------------------------------------------------------------- read endpoints


def test_find_by_department_matches_dedicated_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _generic(
        monkeypatch,
        pages=[{"ok": True, "data": {"items": [], "has_more": False}}],
        method="GET",
        uri="/open-apis/contact/v3/users/find_by_department",
        query_json=json.dumps(
            {
                "department_id": "od-x",
                "department_id_type": "open_department_id",
                "user_id_type": "open_id",
            }
        ),
    )
    reference = _impl._build_find_by_department_request("od-x", "open_department_id", "open_id", 50, "")
    assert _shape(cap.request) == _shape(reference)


def test_dept_children_matches_dedicated_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _generic(
        monkeypatch,
        pages=[{"ok": True, "data": {"items": [], "has_more": False}}],
        method="GET",
        uri="/open-apis/contact/v3/departments/:department_id/children",
        paths_json=json.dumps({"department_id": "0"}),
        query_json=json.dumps({"department_id_type": "open_department_id"}),
    )
    reference = _impl._build_dept_children_request("0", "open_department_id", 50, "")
    assert _shape(cap.request) == _shape(reference)


def test_page_size_default_comes_from_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedicated tool hard-coded 50; the table now carries that number."""
    cap = _generic(
        monkeypatch,
        pages=[{"ok": True, "data": {"items": [], "has_more": False}}],
        method="GET",
        uri="/open-apis/contact/v3/users/find_by_department",
        query_json=json.dumps({"department_id": "od-x"}),
    )
    assert ("page_size", "50") in [(k, str(v)) for k, v in cap.request.queries]


def test_search_user_gets_a_user_token_without_being_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/search/v1/user`` rejects a tenant token. The old tool knew; the table now does."""
    cap = _generic(
        monkeypatch,
        method="GET",
        uri="/open-apis/search/v1/user",
        query_json=json.dumps({"query": "罗霖"}),
    )
    assert cap.request.token_types == {AccessTokenType.USER}


def test_batch_get_id_body_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _generic(
        monkeypatch,
        method="POST",
        uri="/open-apis/contact/v3/users/batch_get_id",
        query_json=json.dumps({"user_id_type": "open_id"}),
        body_json=json.dumps({"mobiles": ["13800000000"]}),
    )
    assert cap.request.http_method == HttpMethod.POST
    assert cap.request.body == {"mobiles": ["13800000000"]}


def test_user_get_path_placeholder_is_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _generic(
        monkeypatch,
        method="GET",
        uri="/open-apis/contact/v3/users/:user_id",
        paths_json=json.dumps({"user_id": "ou_abc"}),
    )
    assert cap.request.uri == "/open-apis/contact/v3/users/:user_id"
    assert cap.request.paths == {"user_id": "ou_abc"}


# ------------------------------------------------------- constraints the tools held


def test_batch_over_fifty_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feishu caps ``users/batch`` at 50 ids. Exceeding it used to be a decoded 400."""
    cap = _CapturedInvoke()
    monkeypatch.setattr(_impl, "_invoke", cap)
    res = anyio.run(
        lambda: _api.call_api_impl(
            method="GET",
            uri="/open-apis/contact/v3/users/batch",
            query_json=json.dumps({"user_ids": [f"ou_{i}" for i in range(51)]}),
        )
    )
    assert res["ok"] is False
    assert res["code"] == "spec_violation"
    assert cap.requests == []


def test_batch_at_the_limit_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _generic(
        monkeypatch,
        method="GET",
        uri="/open-apis/contact/v3/users/batch",
        query_json=json.dumps({"user_ids": [f"ou_{i}" for i in range(50)]}),
    )
    assert len(cap.request.queries) >= 50


def test_find_by_department_requires_a_department(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting ``department_id`` returns an unhelpful 400 from Feishu; catch it here."""
    cap = _CapturedInvoke()
    monkeypatch.setattr(_impl, "_invoke", cap)
    res = anyio.run(lambda: _api.call_api_impl(method="GET", uri="/open-apis/contact/v3/users/find_by_department"))
    assert res["ok"] is False
    assert "department_id" in " ".join(res["violations"])
    assert cap.requests == []


def test_irreversible_delete_surfaces_its_warning() -> None:
    """The table must carry the confirm-first warning where the model will read it."""
    rule = _spec.rules_for(SKILLS_DIR, "DELETE", "/open-apis/contact/v3/users/ou_abc")
    assert rule is not None
    assert rule.pitfalls


# ------------------------------------------------------------------ paging parity


def test_department_roster_pages_are_concatenated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dedicated tool looped on ``page_token``; the table now declares that."""
    cap = _generic(
        monkeypatch,
        pages=[
            {"ok": True, "data": {"items": [{"open_id": "a"}], "has_more": True, "page_token": "t2"}},
            {"ok": True, "data": {"items": [{"open_id": "b"}], "has_more": False}},
        ],
        method="GET",
        uri="/open-apis/contact/v3/users/find_by_department",
        query_json=json.dumps({"department_id": "od-x"}),
    )
    assert len(cap.requests) == 2
    first, second = cap.requests
    assert ("page_token", "t2") in [(k, str(v)) for k, v in second.queries]
    assert "page_token" not in [k for k, _ in first.queries]
    assert second.uri == first.uri


def test_role_members_use_their_own_items_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``functional_roles/:role_id/members`` returns ``members``, not ``items``."""
    cap = _CapturedInvoke([{"ok": True, "data": {"members": [{"user_id": "u1"}], "has_more": False}}])
    monkeypatch.setattr(_impl, "_invoke", cap)
    res = anyio.run(
        lambda: _api.call_api_impl(
            method="GET",
            uri="/open-apis/contact/v3/functional_roles/:role_id/members",
            paths_json=json.dumps({"role_id": "r1"}),
        )
    )
    assert res["ok"] is True
    assert res["members"] == [{"user_id": "u1"}]


def test_write_endpoints_are_not_paged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A POST that happens to echo ``has_more`` must not be retried as a page."""
    cap = _generic(
        monkeypatch,
        pages=[{"ok": True, "data": {"items": [], "has_more": True, "page_token": "t2"}}],
        method="POST",
        uri="/open-apis/contact/v3/users",
        body_json=json.dumps({"name": "张三", "department_ids": ["od-x"]}),
    )
    assert len(cap.requests) == 1

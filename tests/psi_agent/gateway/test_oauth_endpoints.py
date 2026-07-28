from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from psi_agent.gateway._oauth_manager import OAuthRelay
from psi_agent.gateway.server import _oauth_callback, _oauth_take_code


@dataclass
class _FakeRequest:
    """够用的 web.Request 替身: handler 只碰 ``app`` 与 ``query``。"""

    app: dict[str, Any]
    query: dict[str, str]


def _app(relay: OAuthRelay | None = None) -> dict[str, Any]:
    return {"oauth": relay if relay is not None else OAuthRelay()}


@pytest.mark.anyio
async def test_callback_stores_code_and_shows_success_page() -> None:
    relay = OAuthRelay()
    app = _app(relay)

    resp = await _oauth_callback(_FakeRequest(app=app, query={"code": "c0de", "state": "st"}))
    assert resp.status == 200
    assert "text/html" in resp.content_type
    # 页面必须明说不用复制任何东西 —— 这正是本改动要消除的动作。
    assert "不用复制" in resp.text

    got = await relay.take("st")
    assert got is not None
    assert got.code == "c0de"


@pytest.mark.anyio
async def test_callback_without_state_is_rejected() -> None:
    resp = await _oauth_callback(_FakeRequest(app=_app(), query={"code": "c0de"}))
    assert resp.status == 400


@pytest.mark.anyio
async def test_callback_records_upstream_error() -> None:
    relay = OAuthRelay()
    resp = await _oauth_callback(_FakeRequest(app=_app(relay), query={"state": "st", "error": "access_denied"}))
    assert resp.status == 400
    got = await relay.take("st")
    assert got is not None
    assert got.error == "access_denied"


@pytest.mark.anyio
async def test_callback_with_neither_code_nor_error_is_an_error() -> None:
    relay = OAuthRelay()
    resp = await _oauth_callback(_FakeRequest(app=_app(relay), query={"state": "st"}))
    assert resp.status == 400
    got = await relay.take("st")
    assert got is not None
    assert got.code == ""
    assert got.error


@pytest.mark.anyio
async def test_take_code_returns_code_then_404s() -> None:
    relay = OAuthRelay()
    app = _app(relay)
    await _oauth_callback(_FakeRequest(app=app, query={"code": "c0de", "state": "st"}))

    resp = await _oauth_take_code(_FakeRequest(app=app, query={"state": "st"}))
    assert resp.status == 200
    assert json.loads(resp.text)["code"] == "c0de"

    # 一次性: 第二次取件必须落空, 别让同一个 code 被兑换两次。
    resp2 = await _oauth_take_code(_FakeRequest(app=app, query={"state": "st"}))
    assert resp2.status == 404


@pytest.mark.anyio
async def test_take_code_requires_state() -> None:
    resp = await _oauth_take_code(_FakeRequest(app=_app(), query={}))
    assert resp.status == 400


@pytest.mark.anyio
async def test_take_code_surfaces_error_payload() -> None:
    relay = OAuthRelay()
    app = _app(relay)
    await _oauth_callback(_FakeRequest(app=app, query={"state": "st", "error": "access_denied"}))
    resp = await _oauth_take_code(_FakeRequest(app=app, query={"state": "st"}))
    assert resp.status == 200
    assert json.loads(resp.text)["error"] == "access_denied"

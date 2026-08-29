from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from psi_agent.gateway.feishu._auth import DEV_OPEN_ID_ENV, FeishuAuth, Identity
from psi_agent.gateway.feishu._routes import SID_COOKIE, register_auth_routes


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _app(auth: FeishuAuth) -> web.Application:
    """只贴登录三条路由 —— 不需要 SessionManager, 故不造 task group。"""
    app = web.Application()
    app["feishu_auth"] = auth
    register_auth_routes(app)
    return app


@pytest.mark.anyio
async def test_missing_code_is_400_not_500() -> None:
    client = await _client(_app(FeishuAuth(app_id="cli_x", app_secret="s")))
    try:
        resp = await client.post("/auth/feishu", json={})
        assert resp.status == 400
        assert "error" in await resp.json()
    finally:
        await client.close()


@pytest.mark.anyio
async def test_non_object_body_is_400() -> None:
    client = await _client(_app(FeishuAuth(app_id="cli_x", app_secret="s")))
    try:
        resp = await client.post("/auth/feishu", data="not-json")
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.anyio
async def test_unconfigured_gateway_is_400() -> None:
    """未配 app_secret → 4xx 而非 500。"""
    client = await _client(_app(FeishuAuth()))
    try:
        resp = await client.post("/auth/feishu", json={"code": "whatever"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.anyio
async def test_client_supplied_open_id_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """前端塞 open_id 不该起任何作用 —— 未配凭证时照旧 400, 不会认下这个身份。"""
    monkeypatch.delenv(DEV_OPEN_ID_ENV, raising=False)
    client = await _client(_app(FeishuAuth()))
    try:
        resp = await client.post("/auth/feishu", json={"open_id": "ou_victim"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.anyio
async def test_dev_bypass_unavailable_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """守验收 7: 默认配置下旁路不可用。"""
    monkeypatch.delenv(DEV_OPEN_ID_ENV, raising=False)
    client = await _client(_app(FeishuAuth()))
    try:
        resp = await client.post("/auth/feishu", json={"dev": True})
        assert resp.status == 400
        resp = await client.get("/auth/me")
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.anyio
async def test_dev_bypass_works_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEV_OPEN_ID_ENV, "ou_dev")
    client = await _client(_app(FeishuAuth()))
    try:
        resp = await client.post("/auth/feishu", json={})
        assert resp.status == 200
        assert (await resp.json())["open_id"] == "ou_dev"
        resp = await client.get("/auth/me")
        assert resp.status == 200
        assert (await resp.json())["open_id"] == "ou_dev"
    finally:
        await client.close()


@pytest.mark.anyio
async def test_me_and_logout_with_issued_cookie() -> None:
    auth = FeishuAuth(app_id="cli_x", app_secret="s")
    sid = auth.issue(Identity(open_id="ou_alice", name="Alice"))
    client = await _client(_app(auth))
    try:
        resp = await client.get("/auth/me", cookies={SID_COOKIE: sid})
        assert resp.status == 200
        assert await resp.json() == {"open_id": "ou_alice", "name": "Alice"}

        resp = await client.post("/auth/logout", cookies={SID_COOKIE: sid})
        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}
        assert auth.lookup(sid) is None
    finally:
        await client.close()


@pytest.mark.anyio
async def test_me_rejects_forged_cookie() -> None:
    client = await _client(_app(FeishuAuth(app_id="cli_x", app_secret="s")))
    try:
        resp = await client.get("/auth/me", cookies={SID_COOKIE: "forged-sid"})
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.anyio
async def test_app_id_endpoint_never_leaks_secret() -> None:
    client = await _client(_app(FeishuAuth(app_id="cli_x", app_secret="super-secret")))
    try:
        resp = await client.get("/feishu/app-id")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"app_id": "cli_x"}
        assert "super-secret" not in str(body)
    finally:
        await client.close()

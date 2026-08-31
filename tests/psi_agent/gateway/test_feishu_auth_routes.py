from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from psi_agent.gateway.feishu import _auth
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


class _FakeTokenResp:
    """假的 ``http.post()`` 响应 —— 只用来喂 ``resp.json(content_type=None)``。"""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    async def __aenter__(self) -> _FakeTokenResp:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> object:
        return self._payload


class _FakeRejectingSession:
    """假的 ``ClientSession``: token 换取接口 HTTP 200, body ``code`` 非零。

    这正是飞书真实的失败判据(见 ``_auth.py`` 模块头「上游失败判据」)—— 伪造/过期的
    code 不会让上游连接失败或返回 4xx, 只会在 200 的 body 里带非零 ``code``。
    """

    async def __aenter__(self) -> _FakeRejectingSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> _FakeTokenResp:
        # 故意**带上** access_token: 否则 "Feishu returned no access_token" 那条兜底
        # 也能让用例通过, body ``code`` 这条判据就没被吃劲(实测过: 把 code 检查改成
        # ``if False`` 时用例照样绿)。带上 token 后, 非零 ``code`` 是唯一的拒绝理由。
        return _FakeTokenResp(
            {
                "code": 20005,
                "msg": "The user access token passed is invalid",
                "access_token": "should-not-be-trusted",
            }
        )

    def get(self, url: str, **kwargs: object) -> _FakeTokenResp:
        # user_info 这一跳也要能答: 只有这样, 一旦 body ``code`` 判据失效, 伪造的 code
        # 会**登录成功**并拿到身份 —— 用例失败的原因就是这个真实危害, 而不是假上游
        # 缺方法抛的 AttributeError。少了这个方法, 用例仍会红, 但红在错误的理由上。
        return _FakeTokenResp({"code": 0, "data": {"open_id": "ou_forged", "name": "forged"}})


@pytest.mark.anyio
async def test_forged_code_is_4xx_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """守验收 7 的缺口: 伪造 code 走完整路由必须是 4xx, 不是 500。

    伪造 code 打到真实飞书后, 上游 HTTP 状态仍是 200, 失败信号在 body 的 ``code``
    字段(非零)—— 不是 HTTP 4xx。这里在 ``ClientSession`` 这一层伪造上游, 让
    ``identity_from_code`` 走到真实的 ``AuthError`` 分支, 而不是绕过它直接抛异常,
    这样才能证明路由层真的把 ``AuthError`` 映成了 4xx。
    """
    monkeypatch.setattr(_auth, "ClientSession", lambda *a, **kw: _FakeRejectingSession())
    client = await _client(_app(FeishuAuth(app_id="cli_x", app_secret="s")))
    try:
        resp = await client.post("/auth/feishu", json={"code": "forged-code"})
        assert 400 <= resp.status < 500
        assert "error" in await resp.json()
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

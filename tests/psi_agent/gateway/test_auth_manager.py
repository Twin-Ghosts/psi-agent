"""AuthManager 的响应改形逻辑。

这里只测「网关如何改造云端响应」这一层, 不打真实云端: 用 ``monkeypatch`` 顶掉
``_call``, 断言交给页面的 body。两条回归都源自线上实测出来的空数据/弹回。

顶替一律走 ``monkeypatch.setattr``, 不直接赋值 ``m._call = fake``: 后者的签名与
方法不兼容, 类型检查会拦 (而 ``# type: ignore`` 是 mypy 语法, 本仓库用 ``ty``,
压不住)。走 fixture 还能在用例结束时自动还原。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from psi_agent.gateway._auth_manager import AuthManager


async def _manager(tmp_path: Path) -> AuthManager:
    return await AuthManager.create("https://auth.invalid", appdata_root=str(tmp_path))


def _stub_call(
    monkeypatch: pytest.MonkeyPatch,
    manager: AuthManager,
    body: dict[str, Any],
    status: int = 200,
) -> None:
    """让 ``manager._call`` 恒回 *(status, body)*, 不发真实请求。"""

    async def fake(
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = False,
        retry: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        return status, body

    monkeypatch.setattr(manager, "_call", fake)


@pytest.mark.anyio
async def test_verify_new_user_swaps_temp_token_for_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """注册凭证不下发, 但必须留一个不含凭证的新用户信号。

    回归: 摘掉 ``tempToken`` 却没给替代信号时, 页面判不出「该进建号屏」,
    会把新用户当成登录失败弹回输入页。
    """
    m = await _manager(tmp_path)
    try:
        _stub_call(monkeypatch, m, {"tempToken": "tt-secret", "isNewUser": True})
        status, body = await m.verify(code="123456", phone="13900000000")

        assert status == 200
        assert body["registrationRequired"] is True
        assert "tempToken" not in body, "注册凭证不得下发到页面"
        assert m._pending_temp_token == "tt-secret", "凭证应留在本进程"
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_verify_existing_user_has_no_registration_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = await _manager(tmp_path)
    try:
        _stub_call(monkeypatch, m, {"token": "tok-1", "user": {"id": "u1"}})
        _, body = await m.verify(code="123456", phone="13900000000")

        assert "registrationRequired" not in body, "老用户不该被送进建号屏"
    finally:
        await m.aclose()


@pytest.mark.anyio
async def test_list_devices_survives_bare_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """云端 ``GET /sessions`` 回裸数组, 不能被当成坏响应丢掉。

    回归: ``_call`` 里「非 dict 即 bad_response」把整个设备列表吃掉,
    界面上「管理登录设备」恒为 (0)。
    """
    m = await _manager(tmp_path)
    try:
        # 模拟 _call 对裸数组的信封化
        _stub_call(monkeypatch, m, {"items": [{"id": "s1", "platform": "windows", "current": True}]})
        status, body = await m.list_devices()

        assert status == 200
        assert [d["id"] for d in body["devices"]] == ["s1"]
    finally:
        await m.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        {"devices": [{"id": "s1"}]},
        {"sessions": [{"id": "s1"}]},
        {"items": [{"id": "s1"}]},
    ],
)
async def test_list_devices_normalizes_three_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: dict[str, Any]
) -> None:
    m = await _manager(tmp_path)
    try:
        _stub_call(monkeypatch, m, raw)
        _, body = await m.list_devices()
        assert [d["id"] for d in body["devices"]] == ["s1"]
    finally:
        await m.aclose()

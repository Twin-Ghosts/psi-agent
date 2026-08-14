"""连接与传输层的行为。

与 ``test_auth_manager.py`` 分开: 那边测「云端响应怎么被改造成前端契约」,
这边测「连接怎么建、怎么复用、什么时候重试」。两个关注点, 两个文件。

替换一律走 ``monkeypatch.setattr`` —— 直接赋值 ``m._call = fake`` 的签名不兼容,
``ty`` 会拒, 而本仓库的类型检查器是 ``ty`` 不是 mypy, ``# type: ignore`` 无效。
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest

from psi_agent.gateway._auth_manager import (
    _DNS_CACHE_SECONDS,
    _KEEPALIVE_SECONDS,
    AuthManager,
)


@pytest.mark.anyio
async def test_session_connector_keeps_connection_across_sms_wait(tmp_path: Path) -> None:
    """连接池的 keepalive 必须撑过等短信的间隔, 否则每步都要重新握手。"""
    m = await AuthManager.create("https://example.invalid", appdata_root=str(tmp_path))
    try:
        session = m._ensure_session()
        connector = session.connector
        # 收窄到 TCPConnector: session.connector 的静态类型是 BaseConnector | None,
        # 下面要读的字段只在 TCPConnector 上。isinstance 断言让 ty 自己认出来,
        # 不必写 # type: ignore (本仓库用 ty, 那条注释是 mypy 语法, 压不住)。
        assert isinstance(connector, aiohttp.TCPConnector)
        # 等短信最长约 90s; keepalive 必须比它长, 否则连接在等待期间就被回收了。
        assert _KEEPALIVE_SECONDS > 90.0
        assert connector._keepalive_timeout == _KEEPALIVE_SECONDS
        # 云端地址不变, 没必要每 10s 重新解析一次 DNS。
        assert _DNS_CACHE_SECONDS >= 600
        assert connector._use_dns_cache is True
        # ttl 不在 connector 上, 落在它内部的缓存对象里 (aiohttp 3.14 是
        # _cached_hosts._ttl)。私有属性, 换 aiohttp 版本时这条会先炸 —— 那正是
        # 我们想要的信号: 取值没生效比测试失败更难发现。
        assert connector._cached_hosts._ttl == _DNS_CACHE_SECONDS
        # 同一个 session 复用同一个 connector, 不能每次调用都新建。
        assert m._ensure_session().connector is connector
    finally:
        await m.aclose()

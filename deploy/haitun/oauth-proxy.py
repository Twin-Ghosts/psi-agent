"""OAuth 回调反向代理: 只把 /oauth/callback 与 /oauth/code 放行到 Gateway。

Gateway 自身还挂着 /sessions、/chat/completions 这些能直接驱动 agent 的路由,
所以不能把 Gateway 端口整个暴露到内网。本代理是唯一对外监听的入口, 白名单外的
路径一律 404, 不做任何转发。

用途只有一个: 让用户浏览器点完飞书授权后, 回调能自己落地到 Gateway, 用户不必
从地址栏手抄 code。参见 tools/_oauth_receiver.py 的 gateway 通道。

环境变量:
  OAUTH_PROXY_LISTEN    对外监听地址, 默认 0.0.0.0:8080
  OAUTH_PROXY_UPSTREAM   Gateway 基址, 默认 http://127.0.0.1:8080
"""

from __future__ import annotations

import os

import aiohttp
from aiohttp import web

# 只有这两条路径可以过。/oauth/code 也必须放行: 工具侧用同一个基址轮询取件
# (tools/_oauth_receiver.py 的 _wait_for_code), 少放一条就等于没接通。
ALLOWED_PATHS = frozenset({"/oauth/callback", "/oauth/code"})
# 只转发 OAuth 流程真正用到的查询参数, 其余丢弃。
ALLOWED_QUERY = frozenset({"code", "state", "error", "error_description"})
_UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _upstream() -> str:
    return os.environ.get("OAUTH_PROXY_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")


async def _forward(request: web.Request) -> web.StreamResponse:
    if request.path not in ALLOWED_PATHS:
        return web.Response(status=404, text="Not Found\n")
    params = {k: v for k, v in request.query.items() if k in ALLOWED_QUERY}
    url = f"{_upstream()}{request.path}"
    session: aiohttp.ClientSession = request.app["client"]
    try:
        async with session.get(url, params=params, allow_redirects=False) as resp:
            body = await resp.read()
            content_type = resp.headers.get("Content-Type", "text/plain")
            return web.Response(status=resp.status, body=body, content_type=content_type.split(";")[0])
    except aiohttp.ClientError:
        return web.Response(status=502, text="Bad Gateway\n")


async def _on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession(timeout=_UPSTREAM_TIMEOUT)


async def _on_cleanup(app: web.Application) -> None:
    await app["client"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    # 通配: 白名单判断放在 handler 里, 未放行的路径统一 404。
    app.router.add_route("GET", "/{tail:.*}", _forward)
    return app


def main() -> None:
    listen = os.environ.get("OAUTH_PROXY_LISTEN", "0.0.0.0:8080")
    host, _, port = listen.rpartition(":")
    web.run_app(build_app(), host=host or "0.0.0.0", port=int(port), access_log=None)


if __name__ == "__main__":
    main()

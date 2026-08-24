"""aiohttp server that binds ``agent.handle_request`` to the channel socket."""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from aiohttp import web
from loguru import logger

from psi_agent._sockets import create_site
from psi_agent.session.file_serving import FileServingError, resolve_within_root

if TYPE_CHECKING:
    from psi_agent.session.agent import SessionAgent


def _make_files_handler(agent: SessionAgent) -> web.Handler:
    """``GET /files?path=…`` —— 把 workspace 内的文件当字节流交出去。

    出向跨容器发文件用: channel 在 gateway 容器里, 读不到本容器的文件系统, 于是取字节
    自己上传 (缘由见 ``session.file_serving``)。读取范围严格限在本 Session 的 workspace
    根内, 越界 403。

    与同口的 ``/chat/completions`` 一样不加鉴权: 该端口只在 docker 网络内可达 (生产未
    映射到宿主), 两个端点的暴露面本就相同 —— 单给这一个加密钥, 换不来实际隔离, 却多一
    条「密钥没配好时文件静默发不出去」的排查成本。
    """

    async def handle_files(request: web.Request) -> web.StreamResponse:
        raw = request.query.get("path") or ""
        try:
            resolved = await resolve_within_root(raw, agent.workspace_path)
        except FileServingError as e:
            logger.warning(f"GET /files rejected ({e.status}): {e}")
            return web.json_response({"error": str(e)}, status=e.status)
        except OSError as e:
            logger.warning(f"GET /files failed for {raw!r}: {e}")
            return web.json_response({"error": str(e)}, status=400)
        logger.info(f"GET /files serving {str(resolved)!r}")
        # FileResponse 走 sendfile, 不把整份读进本进程内存。
        return web.FileResponse(
            resolved,
            headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'},
        )

    return handle_files


async def serve_session(*, channel_socket: str, agent: SessionAgent) -> None:
    """Create an aiohttp server that routes channel traffic to the agent.

    - ``POST /chat/completions`` → ``agent.handle_request`` (chat SSE)
    - ``POST /events`` → ``agent.handle_event`` (normalized event envelopes)
    - ``GET /files`` → workspace-confined bytes (outbound cross-container files)
    """
    logger.info(f"Starting session server on {channel_socket}")

    # Large conversation contexts (long histories, tool outputs) routinely exceed
    # aiohttp's 1 MiB default body limit, which would reject the request with
    # HTTPRequestEntityTooLarge before it reaches the agent. Match the gateway
    # and AI-forwarder apps' 100 MiB ceiling so the same payloads flow through.
    app = web.Application(client_max_size=100 * 1024 * 1024)
    app.router.add_post("/chat/completions", agent.handle_request)
    app.router.add_post("/events", agent.handle_event)
    app.router.add_get("/files", _make_files_handler(agent))

    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = create_site(runner, channel_socket)
        await site.start()
    except Exception as e:
        logger.error(f"Failed to start session server on {channel_socket}: {e}")
        with anyio.CancelScope(shield=True):
            await runner.cleanup()
        raise

    logger.info(f"Session server listening on {channel_socket}")

    try:
        await anyio.sleep_forever()
    finally:
        logger.info(f"Shutting down session server on {channel_socket}")
        with anyio.CancelScope(shield=True):
            await runner.cleanup()

"""ToB 路由装配 —— ``register_feishu_routes()`` 与 ``/feishu/*`` ``/oauth/*`` 的 handler。

A7: 与 ``desktop/_routes.py`` 同一个原因搬过来 —— 装配函数留在 ``gateway/server.py`` 时,
骨架为了给它备料必须 ``from psi_agent.gateway.feishu._feishu_manager import FeishuManager``,
于是「骨架不认识产品线」这条只靠纪律维持。现在骨架对本包一无所知。

``/oauth/*`` 两条也在这里: 取件方(实测)全在 ``workspace/tob/tools/`` 一侧, ToC 的登录走
手机号 + 验证码不经过 OAuth 跳转 —— 理由详见 ``_oauth_manager`` 模块头。

``_json`` / ``_error`` 从骨架 import: 方向是产品 → 骨架, 正是允许的那一向。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from aiohttp import web
from loguru import logger

from psi_agent.gateway.feishu._feishu_manager import FeishuManager
from psi_agent.gateway.feishu._oauth_manager import OAuthRelay
from psi_agent.gateway.server import _error, _json
from psi_agent.runtime._scheduler_manager import SchedulerManager
from psi_agent.runtime._session_manager import SessionManager


async def _feishu_route(request: web.Request) -> web.Response:
    """幂等地把一次飞书会话路由到其 Session, 首次见到时按需 spawn。

    body: ``{open_id, chat_id?, chat_type?, ai_id?, workspace?}`` →
    ``201 {open_id, chat_id, session_id, channel_socket, external}``。群聊 (``chat_type`` 为
    group/topic 且 ``chat_id`` 非空) 整群共用一个 Session, 其余按 ``open_id`` 一人一个。channel
    拿回 ``channel_socket`` 连接即得对应会话; ``external`` 为真表示该 Session 跑在**别的容器**里,
    channel 据此不再下载附件到本机 (那边看不见), 改为透传 file_key。
    """
    fm: FeishuManager = request.app["fm"]
    schedm: SchedulerManager = request.app["schedm"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return _error("Request body must be a JSON object", status=400)
        open_id = body.get("open_id") or ""
        chat_id = body.get("chat_id") or ""
        chat_type = body.get("chat_type") or ""
        socket, session_id = await fm.route(
            open_id,
            chat_id=chat_id,
            chat_type=chat_type,
            ai_id=body.get("ai_id"),
            workspace=body.get("workspace"),
        )
        external = fm.is_external(open_id, chat_id=chat_id, chat_type=chat_type)
        # Schedules under this session's workspace belong to its dedicated scheduler
        # Session, not to the user/group session.
        #
        # 外部容器托管的会话本进程没有 Session, ``get_workspace`` 会抛 LookupError (转 404) ——
        # 它的定时任务由那个容器自己加载, 这里无事可做, 故跳过。历史上这里能跑通只是因为
        # 迁移前留下了一个同名本地 Session 兜住了查询; 那个残留一旦被清掉, 路由就会 404。
        sm: SessionManager = request.app["sm"]
        if not external:
            await schedm.ensure(
                sm.get_workspace(session_id),
                ai_id=sm.get_backend_id(session_id),
                agent=sm.get_agent(session_id),
            )
        return _json(
            {
                "open_id": open_id,
                "chat_id": chat_id,
                "session_id": session_id,
                "channel_socket": socket,
                # channel 据此决定附件是自己下载还是透传 file_key 交给对端容器下载。
                "external": external,
            },
            status=201,
        )
    except (TypeError, ValueError, KeyError) as e:
        return _error(str(e), status=400)
    except LookupError as e:
        return _error(str(e), status=404)
    except Exception as e:
        logger.error(f"Unexpected error routing feishu open_id: {e!r}")
        return _error(str(e), status=500)


async def _list_feishu_routes(request: web.Request) -> web.Response:
    fm: FeishuManager = request.app["fm"]
    return _json([asdict(r) for r in fm.list_routes()])


_OAUTH_DONE_HTML = (
    "<!doctype html><meta charset=utf-8><title>授权完成</title>"
    "<body style='font:16px/1.7 system-ui;padding:3rem;text-align:center'>"
    "<h2>{title}</h2><p style='color:#666'>{note}</p></body>"
)


def _oauth_html(title: str, note: str, status: int = 200) -> web.Response:
    return web.Response(
        text=_OAUTH_DONE_HTML.format(title=title, note=note),
        content_type="text/html",
        charset="utf-8",
        status=status,
    )


async def _oauth_callback(request: web.Request) -> web.Response:
    """OAuth 重定向落地点: 收下 ``?code=&state=`` 交给中继, 给用户一个成功页。

    发起方(workspace 工具)随后用同一个 ``state`` 去 ``/oauth/code`` 取回 —— 用户
    因此**不需要**再从地址栏手工复制 code。
    """
    relay: OAuthRelay = request.app["oauth"]
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    error = request.query.get("error", "") or request.query.get("error_description", "")
    if not state:
        return _oauth_html("授权链接不完整", "回调缺少 state 参数, 请回到对话里重新发起授权。", status=400)
    if not code and not error:
        error = "callback carried neither code nor error"
    await relay.deliver(state, code=code, error=error)
    if error:
        return _oauth_html("授权未完成", "可以回到对话里重新发起授权。", status=400)
    return _oauth_html("授权成功 ✅", "可以关掉这个页面, 回到对话继续 —— 不用复制任何东西。")


async def _oauth_take_code(request: web.Request) -> web.Response:
    """发起方取件: ``?state=`` 命中则返回 ``{code}`` 并作废, 未到达返回 404。"""
    relay: OAuthRelay = request.app["oauth"]
    state = request.query.get("state", "")
    if not state:
        return _error("state query parameter is required", status=400)
    pending = await relay.take(state)
    if pending is None:
        return _error("no callback received for this state yet", status=404)
    if pending.error:
        return _json({"state": state, "error": pending.error}, status=200)
    return _json({"state": state, "code": pending.code}, status=200)


def register_feishu_routes(
    app: web.Application,
    *,
    feishu_ai_id: str = "",
    feishu_workspace_root: str = "",
) -> web.Application:
    """ToB: 飞书会话 → Session 的路由表。

    ``FeishuManager`` 复用骨架里的 ``SessionManager``, 但它自己认识飞书 (open_id /
    chat_id / 跨容器会话), 所以建在这里而不是骨架里 —— 桌面端容器不再无条件建它。

    **不碰 ``app["schedm"]``**: 原 ``create_app`` 里 ``scheduler_ai_id or feishu_ai_id``
    那个回落已经在唯一的生产调用点做掉了 (``Gateway.run`` 建 ``SchedulerManager`` 时,
    见 ``gateway/__init__.py``)。调度 Session 由骨架持有, 让产品层回头改它的私有字段
    等于把一个已建好对象的配置权分给两处。
    """
    sm: SessionManager = app["sm"]
    app["fm"] = FeishuManager(_sm=sm, _ai_id=feishu_ai_id, _workspace_root=feishu_workspace_root)
    app["oauth"] = OAuthRelay()
    app["openapi_feishu"] = True
    app.router.add_post("/feishu/route", _feishu_route)
    app.router.add_get("/feishu/routes", _list_feishu_routes)
    # ``/oauth/*`` 跟着 ``OAuthRelay`` 一起归本包: 取件方全在 ToB 一侧。ToC 进程照样有这
    # 两条 —— 唯一的生产入口 ``gateway/__init__.py`` 两条线都贴, 所以行为不变。
    app.router.add_get("/oauth/callback", _oauth_callback)
    app.router.add_get("/oauth/code", _oauth_take_code)

    # ToB 前端的静态挂载点 —— 写法参照 ToC 侧两个 ``add_static``, 但存在性判断用同步的
    # ``pathlib``: 本函数是 ``def`` 而非 ``async def``, 改成协程要动 4 个调用点, 而这里
    # 要的只是「启动时目录在不在」。前缀与 ``feishu-web/vite.config.ts`` 的 ``base``
    # 是同一个字面量, 改一边忘另一边会静默 404 (``dist/`` 不存在时连 static 都不注册)。
    #
    # A7: 目录从**本模块**的 ``__file__`` 推 —— 装配函数搬进本包后不必再 import 包对象
    # 取 ``feishu_pkg.__file__`` (那一圈原是因为调用方住在骨架里)。
    feishu_web_dist = Path(__file__).parent / "feishu-web" / "dist"
    if feishu_web_dist.is_dir():
        logger.info(f"Feishu web enabled, serving {feishu_web_dist}")
        app.router.add_static("/feishu-web/", str(feishu_web_dist), show_index=False)
    else:
        logger.info(f"Feishu web dist absent ({feishu_web_dist}), static mount skipped")
    return app

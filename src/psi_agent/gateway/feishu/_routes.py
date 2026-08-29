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

from psi_agent.gateway.feishu._auth import AuthError, FeishuAuth, Identity, dev_open_id
from psi_agent.gateway.feishu._feishu_manager import FeishuManager
from psi_agent.gateway.feishu._oauth_manager import OAuthRelay
from psi_agent.gateway.server import _error, _json, _read_json
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


SID_COOKIE = "psi_feishu_sid"
"""登录态 cookie 名。``HttpOnly`` 是要点: 页面脚本读不到它, XSS 也偷不走登录态。"""


def current_identity(request: web.Request) -> Identity | None:
    """当前请求的身份, 未登录返回 None。

    唯一来源是 ``HttpOnly`` cookie 里的 sid —— **不读 body/query 里的 open_id**。
    前端能伪造任何字段, 但伪造不出一个签发过的高熵 sid。会话过滤路由 (见下) 全部
    经由本函数取身份, 于是「谁在问」只有一个判据。
    """
    auth: FeishuAuth = request.app["feishu_auth"]
    return auth.lookup(request.cookies.get(SID_COOKIE, ""))


async def _auth_feishu(request: web.Request) -> web.Response:
    """``POST /auth/feishu`` —— body ``{code}`` → ``{open_id, name}`` + 登录 cookie。

    **body 里的 ``open_id`` 一律忽略**: 身份只能是 ``code`` 换回来的。前端传了也不看,
    这是本端点的安全前提。

    ``PSI_FEISHU_DEV_OPEN_ID`` 设了才有开发旁路, 且每次打 WARNING。默认不设置 → 无 code
    就是 400。
    """
    auth: FeishuAuth = request.app["feishu_auth"]
    body = await _read_json(request)
    code = ""
    if isinstance(body, dict):
        code = str(body.get("code") or "")

    if not code:
        # dev_open_id() 自己就打 WARNING (Task 3 已实现), 这里不要再打第二遍:
        # 同一次旁路登录刷两条同义告警, 只会让真正的告警更难被看见。
        bypass = dev_open_id()
        if bypass:
            return _issue_login(Identity(open_id=bypass, name=bypass), auth)
        return _error("missing code", status=400)

    try:
        identity = await auth.identity_from_code(code)
    except AuthError as e:
        # 伪造/过期 code, 或 Gateway 未配凭证 —— 都是 4xx, 不是 500。
        logger.info(f"Feishu login rejected: {e}")
        return _error(str(e), status=400)
    except Exception as e:
        logger.error(f"Unexpected error during Feishu login: {e!r}")
        return _error("login failed", status=500)
    return _issue_login(identity, auth)


def _issue_login(identity: Identity, auth: FeishuAuth) -> web.Response:
    """签发登录 cookie 并回身份。

    ``auth`` 必填而非可选: 两个调用点 (正常登录与开发旁路) 都必须签 cookie, 漏签的表现
    是登录看着成功、下一秒 ``/auth/me`` 401 —— 可选参数只会让这种漏法静默通过。
    """
    resp = _json({"open_id": identity.open_id, "name": identity.name})
    resp.set_cookie(
        SID_COOKIE,
        auth.issue(identity),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return resp


async def _auth_me(request: web.Request) -> web.Response:
    identity = current_identity(request)
    if identity is None:
        return _error("not logged in", status=401)
    return _json({"open_id": identity.open_id, "name": identity.name})


async def _auth_logout(request: web.Request) -> web.Response:
    auth: FeishuAuth = request.app["feishu_auth"]
    auth.revoke(request.cookies.get(SID_COOKIE, ""))
    resp = _json({"status": "ok"})
    resp.del_cookie(SID_COOKIE, path="/")
    return resp


async def _feishu_app_id(request: web.Request) -> web.Response:
    """前端免登要的 appID —— **只给 app_id, 永不给 app_secret**。

    前端因此不必写死 appID (PR 755 把它连同一个真实 open_id 一起硬编码在前端, 上云后
    所有访问者都变成同一个人)。未配置时返回空串而非 404: 前端据此显示「未配置免登」
    这条可读的提示, 而不是撞一个语义不明的 404。
    """
    auth: FeishuAuth = request.app["feishu_auth"]
    return _json({"app_id": auth.app_id})


def register_auth_routes(app: web.Application) -> web.Application:
    """把登录四条路由贴到 *app*。

    与 ``register_feishu_routes`` 分开是为了让单测能只贴这几条 —— 那边会建
    ``FeishuManager``, 要求一个真的 ``SessionManager`` 与 task group。
    """
    app.router.add_post("/auth/feishu", _auth_feishu)
    app.router.add_get("/auth/me", _auth_me)
    app.router.add_post("/auth/logout", _auth_logout)
    app.router.add_get("/feishu/app-id", _feishu_app_id)
    return app


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
    feishu_app_id: str = "",
    feishu_app_secret: str = "",
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
    # 网页应用免登。**Gateway 从此持有 app_secret** —— 与 ``_oauth_manager`` 模块头那句
    # 「Gateway 侧刻意不碰 token 交换: 不知道 app_secret」是一次有意的变更, 不是疏漏:
    # 免登必须由后端拿 code 去换 token, 换的动作只能发生在知道 secret 的一侧, 而这一侧
    # 必须是服务端 (放前端等于公开 secret)。OAuthRelay 那条路径**照旧不碰 token**,
    # 两者互不影响。
    app["feishu_auth"] = FeishuAuth(app_id=feishu_app_id, app_secret=feishu_app_secret)
    register_auth_routes(app)
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

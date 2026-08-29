"""Gateway — lifecycle manager for AI/Session instances over a REST + Web UI surface."""

from __future__ import annotations

import os
import socket
import webbrowser
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import anyio
from aiohttp import web
from loguru import logger

from psi_agent._logging import setup_logging
from psi_agent._sockets import create_site
from psi_agent.gateway._defaults import resolve_appdata_root, resolve_default_agent, resolve_default_workspace
from psi_agent.gateway._state import GatewayState
from psi_agent.gateway.desktop._attention import AttentionHub
from psi_agent.gateway.desktop._auth_manager import AuthManager, resolve_endpoint
from psi_agent.gateway.desktop._free_model import make_key_resolver
from psi_agent.gateway.desktop._routes import register_desktop_routes
from psi_agent.gateway.desktop._spa_shell import DEFAULT_APP_NAME
from psi_agent.gateway.desktop._tray import GatewayTray
from psi_agent.gateway.desktop._webview import GatewayWebView
from psi_agent.gateway.feishu._routes import register_feishu_routes, register_oauth_routes
from psi_agent.gateway.server import create_core_app
from psi_agent.runtime._ai_manager import AIManager
from psi_agent.runtime._router_manager import RouterManager, RouterUpstreamInfo
from psi_agent.runtime._scheduler_manager import SchedulerManager
from psi_agent.runtime._session_manager import SessionManager
from psi_agent.runtime._summary_manager import SummaryManager
from psi_agent.runtime._title_manager import TitleManager


def _random_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


GatewayName = Literal["desktop", "feishu"]
"""``--gateway`` 的取值域。加第三个 gateway 只动这里与 ``ALL_GATEWAYS``。"""

ALL_GATEWAYS: tuple[GatewayName, ...] = ("desktop", "feishu")
"""``--gateway`` 的全集, 也是它的默认值 (顺序即注册顺序: ToC 先贴, ``GET /`` 归它)。"""


def resolve_gateways(selected: Sequence[str]) -> tuple[str, ...]:
    """规整 ``--gateway`` 的取值: 去重保序, 空列表报错。

    两种输入 tyro 都照收不报错, 得在这里自己拦 (实测):

    - ``--gateway`` 后不跟任何值 → ``[]``。那是起了服务但一个前端都没有的状态, 明确拒绝
      而不是静默起个空壳: 用户看不出区别, 只会在访问时拿 404 并以为服务挂了。骨架 REST
      (``/ais`` ``/sessions`` …) 想单独跑不该借这个参数, 那是另一件事。
    - ``--gateway feishu feishu`` → 重复值。去重而不是报错: 意图没有歧义 (要飞书那面),
      报错只是给脚本拼参数的人添麻烦。注册函数被调两次才是真问题 —— 同名路由叠一层。
    """
    if not selected:
        raise ValueError(f"--gateway needs at least one of {{{','.join(ALL_GATEWAYS)}}}; got an empty list")
    return tuple(dict.fromkeys(selected))


async def _redirect_to_feishu_web(request: web.Request) -> web.Response:
    """只挂 ``--gateway feishu`` 时的 ``GET /``: 302 到 ``/feishu-web/``。

    只在不挂 ToC 时注册。ToC 的 ``GET /`` 有 spa-v2 → spa 的降级链 (``desktop/_routes.py``),
    两条线都挂时根路径仍归它, 行为不变。

    刻意用重定向而不是直接返回 ToB 的 index: 静态挂载点 ``/feishu-web/`` 是 ``vite.config.ts``
    的 ``base``, 前端资源路径都以它开头; 在 ``/`` 直接吐 index 会让相对资源请求打到根下而
    404。dist 目录不存在时 ``/feishu-web/`` 本身没注册, 用户跟着跳过去拿 404 —— 与只挂
    ToC 且 dist 缺失时的表现一致 (没有产物就是没有前端), 不再额外造一个假页面。

    **指向 ``index.html`` 而不是目录** (实测): ``add_static(..., show_index=False)`` 对
    ``/feishu-web/`` 这个裸目录回 **403** 而非 index (ToC 侧靠 ``add_static`` 之前另注册
    三条 ``→ index.html`` 的 handler 绕过, 见 ``desktop/_routes.py`` 那句注释)。跳到目录
    会让 ToB 单挂时的首页变成 403; 直接跳文件即可, 无需给飞书侧补那三条 —— 补了会改动
    默认 (两面全挂) 组合的路由集合, 而那一条要求逐条不变。
    """
    return web.Response(status=302, headers={"Location": "/feishu-web/index.html"})


@dataclass
class Gateway:
    """Start the gateway REST + Web UI server."""

    listen: str = ""
    """Listen address. Empty = random high port on 127.0.0.1."""

    socket_path: str = "psi"
    """Prefix for AI/Session socket paths (Unix sockets on POSIX, Named Pipes on Windows).

    **两个 Gateway 同时跑时必须给不同值** (或改 ``--default-workspace``, 见下)。冲突不来自
    共享前缀, 而是**同一个完整管道名**: 同 workspace 的调度 Session id 由 workspace 路径的
    sha256 确定性派生 (``runtime/_scheduler_manager.py``), 两个进程必然算出同一个名字;
    ``_session_manager`` 的去重只在进程内, 抓不到跨进程重名。Windows 上表现为
    ``PermissionError(13, ...)`` / ``[WinError 5] 拒绝访问``。
    """

    gateway: list[GatewayName] = field(default_factory=lambda: list(ALL_GATEWAYS))
    """挂哪些 gateway 的 HTTP 面。**可组合的列表, 空格分隔**; 默认全挂 = 现有行为, 一条路由都不少。

    - ``desktop`` ToC 那面: ``/spa/`` ``/spa-v2/`` ``/ui/*`` ``/workspace/*`` ``/auth/*``。
    - ``feishu``  ToB 那面: ``/feishu/*`` ``/feishu-web/``。单独挂它时 ``GET /`` 302 到
      ``/feishu-web/index.html`` (dist 缺失时那条静态挂载本身没注册, 跟过去拿 404 ——
      没有 ToC 外壳可降级)。

    ``--gateway desktop feishu`` (默认) 两面全挂, 生产入口用的就是这个: 飞书容器起的也是
    ``psi-agent gateway``。只写一个就只挂那一面, 另一面的路由不注册 (404), 开发时省掉另一面
    的前端与 manager。**逗号形式不支持** (``--gateway desktop,feishu`` 会报错); 一个值都不给
    会退出 —— 起了服务却没有任何前端是个没有用处的状态, 见 ``resolve_gateways``。

    列表而非枚举: 「有哪些 gateway」将来会变, 而 ``both`` 这种词只在恰好两个时成立, 加第三个
    就得改枚举。骨架 REST (``/ais`` ``/sessions`` …) 与 ``/oauth/*`` 每种组合下都在 —— 前者是
    各面共用的内核面, 后者的回调地址登记在第三方应用后台, 不随本进程挂了哪面而变。

    **gateway 与 agent 是两个独立维度。** 本参数只决定挂哪些 HTTP 面, agent 包选哪个由
    ``--default-agent`` 决定 (空则软默认, 见该字段), 两者可自由组合: ToC 的 gateway 配 ToB 的
    agent 包是合法的, 不予阻止。
    """

    icon: str | None = None
    """Path to icon image file (png/jpg/ico). Used as favicon, tray icon (--tray), and webview icon (--webview)."""

    app_name: str = DEFAULT_APP_NAME
    """Browser tab / webview / tray label. Injected into SPA index.html at serve time."""

    browser: bool = False
    """Open a browser tab on startup."""

    webview: bool = False
    """Use a native webview window instead of the system browser."""

    tray: bool = False
    """Show a system tray icon (requires --icon)."""

    feishu_ai_id: str = ""
    """飞书 Session 默认挂载的 AI 实例 id。飞书 channel 经 ``POST /feishu/route`` 按需为每个
    飞书用户/群 spawn 独立 Session 时用它作缺省 AI (请求体也可逐次覆盖 ``ai_id``)。空 = 未配,
    此时若请求也不带 ``ai_id`` 则 ``/feishu/route`` 返回 400。"""

    feishu_workspace_root: str = ""
    """飞书各会话独立 workspace 的父目录。私聊每个 open_id 得到 ``<root>/<open_id>`` 子目录,
    群聊每个 chat_id 得到 ``<root>/chat-<chat_id>``, 文件/历史互相隔离。空 = 以 Gateway 进程
    cwd 为父目录。"""

    default_agent: str = ""
    """CLI: default agent package for new Sessions / GET /defaults.

    Empty → soft-default ``agents/feishu`` under cwd when present;
    else cwd when it looks like an install layout (``tools/`` + ``skills/``);
    else Session keeps single-root compat (``agent=\"\"`` → same as workspace).
    """

    default_workspace: str = ""
    """Step 2 CLI: default user workspace for new Sessions / GET /defaults.

    Empty → soft-default ``{Desktop}/haitun交付`` (path announced only; directory
    created on first Session / conversation, not on Gateway boot).
    Not AppData; todos/history/Gateway state live under ``--appdata``.
    """

    appdata: str = ""
    """AppData memory-area root (``GET /defaults.appdata``, env ``PSI_APPDATA``).

    Empty → ``PSI_APPDATA`` → ``platformdirs.user_data_dir(Haitun)``.
    Step 4B: todos write under ``{appdata}/todos/`` (legacy workspace path dual-read).
    Step 4C: history writes under ``{appdata}/histories/`` (legacy dual-read).
    Step 4D: Gateway ``state/`` under ``{appdata}/state/`` (legacy cwd dual-read).
    """

    scheduler_ai_id: str = ""
    """调度 Session 挂载的 AI 实例 id。每个 workspace 会得到一个专用调度 Session
    (对 SPA / state 隐藏), 以 ``active_schedules=("*",)`` 激活该 workspace 下的全部
    定时任务 —— 定时任务从 workspace 加载, 但**触发权是 (session x schedule) 逐条的**,
    一条必须恰好被一个 Session 激活, 否则飞书多用户下一条提醒会被在线会话数乘一遍。

    空 = 回落 ``--feishu-ai-id``; 两者都空则不启动调度 Session (记 warning)。
    """

    auth_endpoint: str = ""
    """云端认证服务地址。**留空即取内置默认值** (账号服务的正式地址)。

    空 ≠ 关闭: 装了包的用户直接 ``psi-agent gateway`` 就该能登录, 要求他先知道并
    手填一个域名, 等于把部署细节转嫁给使用者。要**关掉**认证 (纯本地单用户, 不注册
    ``/auth/*``、不读写本机凭证) 请显式设 ``PSI_AUTH_ENDPOINT=""``。

    启用时客户端只做转发与本机凭证管理: 不持任何供应商密钥 (安装包里放阿里云
    AK/SK 或 Resend key 等于公开发布), 授权判定全在云端 (用户本人即机器管理员,
    客户端侧校验可被绕过)。见 ``_auth_manager.resolve_endpoint``。
    """

    verbose: bool = False
    """Enable DEBUG-level logging."""

    async def run(self) -> None:
        setup_logging(verbose=self.verbose)

        if self.browser and self.webview:
            raise ValueError("--browser and --webview are mutually exclusive")

        # 与上面的互斥校验同处: 都在建 socket / 恢复 state 之前失败, 不留半启动的进程。
        gateways = resolve_gateways(self.gateway)

        addr = self.listen or f"http://127.0.0.1:{_random_port()}"
        logger.info(f"Starting Gateway service on {addr} (socket_path={self.socket_path})")

        # Path defaults: agent/workspace (Step 2) + AppData root announce (Step A).
        agent_default = await resolve_default_agent(self.default_agent)
        workspace_default = await resolve_default_workspace(self.default_workspace)
        appdata_root = await resolve_appdata_root(self.appdata)
        # So in-process Session tools (todo, …) see the same root as GET /defaults.
        os.environ["PSI_APPDATA"] = appdata_root
        logger.info(f"Default agent: {agent_default or '(same as workspace)'}")
        logger.info(f"Default workspace: {workspace_default}")
        logger.info(f"AppData root: {appdata_root}")

        state = await GatewayState.from_appdata(appdata_root)
        snapshot = await state.load()

        async with anyio.create_task_group() as tg:
            aim = AIManager(_prefix=self.socket_path, _tg=tg)
            rm = RouterManager(_aim=aim, _prefix=self.socket_path, _tg=tg)
            sm = SessionManager(
                _aim=aim,
                _rm=rm,
                _prefix=self.socket_path,
                _tg=tg,
                _default_agent=agent_default,
                _default_workspace=workspace_default,
                _appdata=appdata_root,
            )
            tm = TitleManager()
            sum_m = SummaryManager()

            # 认证是**旁挂**的: 不注入 Session 的构造参数, 不写 ContextVar, 不参与
            # _do_persist 的 manager 快照 (凭证不进 state/latest.json —— 那里的
            # api_key 是明文, 登录凭证不再踩这个坑)。地址显式为空则整套不加载。
            #
            # ** 必须建在恢复 AI 之前 **: 免费模型的 socket 在构造时就要拿到 token,
            # 建晚了恢复出来的 socket 会带着哨兵值起来, 第一次对话必然 401。
            authm: AuthManager | None = None
            if resolve_endpoint(self.auth_endpoint):
                authm = await AuthManager.create(self.auth_endpoint, appdata_root=appdata_root, tg=tg)
                # 免费模型的哨兵值换成登录 token。传的是取值函数而不是 token ——
                # socket 重建时要拿到当时的新值, 不是接线那一刻的旧值。
                aim._resolve_key = make_key_resolver(authm.bearer_token, authm.endpoint)
                # 趁用户还没点「获取验证码」, 先把连接建好, 省下 TCP+TLS 两个 RTT。
                await authm.nudge_warm()
            else:
                logger.info("Auth disabled (PSI_AUTH_ENDPOINT set to empty)")

            for cfg in snapshot.get("ais", []):
                try:
                    await aim.create(
                        provider=cfg.get("provider", ""),
                        model=cfg.get("model", ""),
                        api_key=cfg.get("api_key", ""),
                        base_url=cfg.get("base_url", ""),
                        id=cfg.get("id", ""),
                        max_context_tokens=int(cfg.get("max_context_tokens", -1)),
                    )
                    logger.info(f"Restored AI {cfg.get('id', '?')!r}")
                except Exception as e:
                    logger.warning(f"Failed to restore AI {cfg.get('id', '?')!r}: {e!r}")

            for cfg in snapshot.get("routers", []):
                try:
                    await rm.create(
                        name=cfg.get("name", ""),
                        mode=cfg.get("mode", ""),
                        router_ai_id=cfg.get("router_ai_id"),
                        upstreams=[
                            RouterUpstreamInfo(
                                backend_type=item.get("backend_type", ""),
                                backend_id=item.get("backend_id", ""),
                                description=item.get("description", ""),
                            )
                            for item in cfg.get("upstreams", [])
                        ],
                        router_timeout=cfg.get("router_timeout"),
                        target_timeout=cfg.get("target_timeout"),
                        max_context_chars=cfg.get("max_context_chars", 12_000),
                        id=cfg.get("id", ""),
                    )
                    logger.info(f"Restored Router {cfg.get('id', '?')!r}")
                except Exception as e:
                    logger.warning(f"Failed to restore Router {cfg.get('id', '?')!r}: {e!r}")

            for cfg in snapshot.get("sessions", []):
                try:
                    await sm.create(
                        backend_type=cfg.get("backend_type", "ai"),
                        backend_id=cfg.get("backend_id", cfg.get("ai_id", "")),
                        workspace=cfg.get("workspace", ""),
                        agent=cfg.get("agent", "") or agent_default,
                        id=cfg.get("id", ""),
                    )
                    logger.info(f"Restored Session {cfg.get('id', '?')!r}")
                except Exception as e:
                    logger.warning(f"Failed to restore Session {cfg.get('id', '?')!r}: {e!r}")

            for t in snapshot.get("titles", []):
                await tm.set(t["id"], t["title"])

            for row in snapshot.get("summaries", []):
                await sum_m.set(row["id"], row["summary"])

            attention = AttentionHub()
            schedm = SchedulerManager(_sm=sm, _ai_id=self.scheduler_ai_id or self.feishu_ai_id)
            # 骨架 + 按 --gateway 贴各 gateway 的 HTTP 面。**默认两面都贴**: 生产上飞书
            # 容器起的也是 `psi-agent gateway` (同容器里另起一个 `psi-agent channel
            # feishu` 连过来), 默认值一动就是静默的行为回归。开发时只写一个值单挂一面,
            # 省掉另一面的前端与 manager。
            want_desktop = "desktop" in gateways
            want_feishu = "feishu" in gateways
            logger.info(f"Gateways: {' '.join(gateways)} (desktop={want_desktop}, feishu={want_feishu})")
            app = await create_core_app(
                aim,
                sm,
                tm,
                rm=rm,
                default_agent=agent_default,
                default_workspace=workspace_default,
                appdata=appdata_root,
                scheduler_ai_id=self.scheduler_ai_id,
                schedm=schedm,
                sum_m=sum_m,
            )
            if want_desktop:
                await register_desktop_routes(
                    app,
                    favicon_path=self.icon,
                    app_name=self.app_name,
                    attention=attention,
                    authm=authm,
                )
            if want_feishu:
                register_feishu_routes(
                    app,
                    feishu_ai_id=self.feishu_ai_id,
                    feishu_workspace_root=self.feishu_workspace_root,
                )
                # ``GET /`` 的兜底链住在 ToC 那边 (spa-v2 → spa)。只挂飞书时那条链没注册,
                # 根路径得自己交代去处, 否则用户访问裸地址拿到 404 还以为服务没起来。
                if not want_desktop:
                    app.router.add_get("/", _redirect_to_feishu_web)
            else:
                # 只挂 ToC: ``/oauth/*`` 随飞书装配一起没了, 这里补上 —— 回调地址登记在
                # 第三方应用后台, 少这两条就是用户点完授权落到 404。
                register_oauth_routes(app)

            # Restored sessions need a scheduler Session for their workspace too
            # (on demand: skipped when there are no schedules).
            for info in await sm.list_all():
                await schedm.ensure(info.workspace, ai_id=info.backend_id, agent=info.agent)

            async def _do_persist() -> None:
                await state.save(
                    ais=[
                        {
                            "id": info.id,
                            "provider": info.provider,
                            "model": info.model,
                            "api_key": info.api_key,
                            "base_url": info.base_url,
                            "max_context_tokens": info.max_context_tokens,
                        }
                        for info in await aim.list_all()
                    ],
                    sessions=[
                        {
                            "id": info.id,
                            "backend_type": info.backend_type,
                            "backend_id": info.backend_id,
                            "workspace": info.workspace,
                            "agent": info.agent,
                        }
                        for info in await sm.list_all()
                    ],
                    titles=[{"id": sid, "title": title} for sid, title in tm.get_all().items()],
                    summaries=[{"id": sid, "summary": text} for sid, text in sum_m.get_all().items()],
                    routers=[
                        {
                            "id": info.id,
                            "name": info.name,
                            "mode": info.mode,
                            "router_ai_id": info.router_ai_id,
                            "upstreams": [
                                {
                                    "backend_type": item.backend_type,
                                    "backend_id": item.backend_id,
                                    "description": item.description,
                                }
                                for item in info.upstreams
                            ],
                            "router_timeout": info.router_timeout,
                            "target_timeout": info.target_timeout,
                            "max_context_chars": info.max_context_chars,
                        }
                        for info in await rm.list_all()
                    ],
                )

            aim._persist = _do_persist
            rm._persist = _do_persist
            sm._persist = _do_persist
            tm._persist = _do_persist
            sum_m._persist = _do_persist

            await _do_persist()

            runner = web.AppRunner(app)
            try:
                try:
                    await runner.setup()
                    site = create_site(runner, addr)
                    await site.start()
                except Exception as e:
                    logger.error(f"Failed to start Gateway on {addr}: {e!r}")
                    raise

                logger.info(f"Gateway listening on {addr}")

                wv = None
                if self.webview:
                    if self.icon is None:
                        raise ValueError("--webview requires --icon to be set")
                    wv = GatewayWebView(addr, has_tray=self.tray, icon=self.icon, app_name=self.app_name)
                    try:
                        wv.start()
                    except Exception as e:
                        logger.warning(f"Failed to start webview window: {e!r}")

                if self.browser:
                    await anyio.to_thread.run_sync(webbrowser.open, addr)  # ty: ignore

                tray = None
                if self.tray:
                    if self.icon is None:
                        raise ValueError("--tray requires --icon to be set")
                    on_open = wv.show if wv is not None and wv.is_running() else None
                    tray = GatewayTray(addr, self.icon, app_name=self.app_name, on_open=on_open)
                    try:
                        tray.start()
                    except Exception as e:
                        logger.warning(f"Failed to start system tray: {e!r}")

                if wv is not None and wv.is_running():
                    attention.bind(webview=wv)
                if tray is not None and tray.is_running():
                    attention.bind(tray=tray)

                try:
                    if tray is not None and tray.is_running():
                        await anyio.to_thread.run_sync(tray.wait_stop, abandon_on_cancel=True)  # ty: ignore
                    elif wv is not None and wv.is_running():
                        await anyio.to_thread.run_sync(wv.wait_closed, abandon_on_cancel=True)  # ty: ignore
                    else:
                        await anyio.sleep_forever()
                finally:
                    if tray is not None:
                        tray.stop()
                    if wv is not None:
                        wv.stop()
            finally:
                logger.info("Shutting down Gateway")
                with anyio.CancelScope(shield=True):
                    await runner.cleanup()
                    # AuthManager 持有 aiohttp 会话, 必须显式关闭, 否则退出时报
                    # "Unclosed client session"。放 shield 内: 被取消时也要清。
                    if authm is not None:
                        await authm.aclose()
                tg.cancel_scope.cancel()
        logger.info("Gateway shutdown complete")

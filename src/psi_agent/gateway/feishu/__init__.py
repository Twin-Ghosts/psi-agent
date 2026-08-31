"""ToB (飞书) 专属层 —— 飞书会话到 Session 的路由表。

`FeishuManager` 复用骨架的 `SessionManager`, 但它自己认识飞书概念 (open_id /
chat_id / 群聊 vs 私聊 / 跨容器会话), 所以住在产品层而不是骨架里。桌面端容器因此
不再无条件建它 (原 `create_app()` 的 ``app["fm"]`` 那行是无条件的)。

装配入口是本包的 ``_routes.register_feishu_routes()``。**依赖单向**: 本包 import 骨架,
骨架不 import 本包 —— A7 之前这个函数住在 ``gateway/server.py``, 于是骨架为了给它备料反向
import 了 `FeishuManager` (判据命令见 ``gateway/AGENTS.md``「依赖方向」)。
ToB 前端 (`feishu-web/`) 按方案 3.6 归本包, A6 落了脚手架, 本轮补上真实业务: 网页应用的
登录 / 多会话 / IM 双向可见。后端侧新增 9 条业务路由(``register_feishu_routes()`` 里):
``/feishu/auth/login`` ``/feishu/auth/me`` ``/feishu/auth/logout`` ``/feishu/app-id``
``/feishu/sessions``(GET/POST) ``/feishu/sessions/{id}/history``
``/feishu/titles`` ``/feishu/summaries``。骨架 ``/sessions`` 一族语义未改, ToC 不受影响 ——
这批新路由是飞书身份过滤后的独立一层, 不是改写骨架端点。

**九条全部在 ``/feishu/`` 前缀下, 一条都不许占裸 ``/auth/*``**: desktop 那条产品线已注册
``GET /auth/me`` 与 ``POST /auth/logout``, 且它默认就开 (``resolve_endpoint()`` 有内置默认
域名, 只有显式 ``PSI_AUTH_ENDPOINT=""`` 才关)。aiohttp 对同 path 重复 ``add_get`` **不报错**,
各建一个 resource 由先注册者胜出, 而装配顺序是先 desktop 后本包 —— 占裸路由的后果是本包的
handler 静默永不执行 (有效 cookie 拿 401、登出不撤 sid)。
``test_feishu_auth_routes.py::test_auth_routes_survive_desktop_coexistence`` 守这条。
"""

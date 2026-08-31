"""ToB (飞书) 专属层 —— 飞书会话到 Session 的路由表。

`FeishuManager` 复用骨架的 `SessionManager`, 但它自己认识飞书概念 (open_id /
chat_id / 群聊 vs 私聊 / 跨容器会话), 所以住在产品层而不是骨架里。桌面端容器因此
不再无条件建它 (原 `create_app()` 的 ``app["fm"]`` 那行是无条件的)。

装配入口是本包的 ``_routes.register_feishu_routes()``。**依赖单向**: 本包 import 骨架,
骨架不 import 本包 —— A7 之前这个函数住在 ``gateway/server.py``, 于是骨架为了给它备料反向
import 了 `FeishuManager` (判据命令见 ``gateway/AGENTS.md``「依赖方向」)。
ToB 前端 (`feishu-web/`) 按方案 3.6 归本包, A6 落了脚手架, 本轮补上真实业务: 网页应用的
登录 / 多会话 / IM 双向可见。后端侧新增 9 条业务路由(``register_feishu_routes()`` 里):
``/auth/feishu`` ``/auth/me`` ``/auth/logout`` ``/feishu/app-id``
``/feishu/sessions``(GET/POST) ``/feishu/sessions/{id}/history``
``/feishu/titles`` ``/feishu/summaries``。骨架 ``/sessions`` 一族语义未改, ToC 不受影响 ——
这批新路由是飞书身份过滤后的独立一层, 不是改写骨架端点。
"""

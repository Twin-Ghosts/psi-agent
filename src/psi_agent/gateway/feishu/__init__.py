"""ToB (飞书) 专属层 —— 飞书会话到 Session 的路由表。

`FeishuManager` 复用骨架的 `SessionManager`, 但它自己认识飞书概念 (open_id /
chat_id / 群聊 vs 私聊 / 跨容器会话), 所以住在产品层而不是骨架里。桌面端容器因此
不再无条件建它 (原 `create_app()` 的 ``app["fm"]`` 那行是无条件的)。

骨架侧的装配入口是 ``gateway.server.register_feishu_routes()``。骨架**不** import
本包。ToB 前端 (`feishu-web/`) 按方案 3.6 归本包, A6 已落地 —— 但**只是脚手架**:
能构建 / 能起 dev server / 能连本机 gateway, 页面是占位, 零业务。后端侧对应的只有
``register_feishu_routes()`` 里那一个 ``add_static``, 没有任何新业务路由。
"""

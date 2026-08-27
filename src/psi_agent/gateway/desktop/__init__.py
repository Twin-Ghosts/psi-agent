"""ToC (桌面版) 专属层 —— 托盘、webview、本机登录、Windows 盘符、SPA 静态资源。

这里的每个模块都**认识桌面概念**: `pystray` / `pywebview` / `PIL`、Windows 盘符、
本机凭证钥匙串、浏览器 origin。ToB 容器里一个都用不上, 所以它们不该和骨架装在同
一层 —— 判据是「这段代码认识哪些概念」, 不是「当前谁在调用」(见方案 3.2)。

骨架侧的装配入口是 ``gateway.server.register_desktop_routes()``: 它把这一层的
`AttentionHub` / `UIPrefs` / `WorkspaceManager` / `AuthManager` 与 `spa` / `spa-v2`
两棵静态资源树贴到 ``create_core_app()`` 产出的 app 上。骨架**不** import 本包。

`spa/` 与 `spa-v2/` 也在本包内: 它们是 ToC 的前端产物, ToB 前端另有自己的树
(`feishu/feishu-web/`, 见 A6)。
"""

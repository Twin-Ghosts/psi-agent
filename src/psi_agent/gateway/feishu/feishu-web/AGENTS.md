# feishu-web —— ToB 前端脚手架

## 这是什么

飞书侧 (ToB) 的 Web 前端。**当前只是脚手架, 零业务** —— 页面是一个占位。

技术栈与 ToC 的 `spa-v2` 保持一致: Vite + React 19 + TypeScript。

## 边界: 本轮只落三项能力

1. **能构建** —— `npm ci` 装得上, `npm run build` 出得来 `dist/`, `tsc` 无错。
2. **能起开发服务器** —— `npm run dev` 起 vite, 浏览器打开有页面。
3. **能连本地服务端** —— dev proxy 把 `/defaults` 转到本机 gateway, 拿到 200。

`src/App.tsx` 里那一次 `fetch('/defaults')` 就是第 3 条的判据, **不是业务代码, 别当成
数据层的雏形往上堆**。`/defaults` 是特意选的: 它在骨架 `create_core_app()` 里已存在,
所以脚手架没有任何未落地的后端依赖, 可以独立跑通。

**不做**(由其他同事后续开发): 登录 / `/auth/feishu`、会话列表、对话收发、任务与交付物
UI、业务端点封装 (`api.ts`)、成品样式。

**也不含部署**: 上云、Caddy、oauth-proxy 白名单、`dist/` 下发方式都是后续单独的事。

## 常用命令

```bash
npm ci        # 按 package-lock.json 装依赖 (可复现)
npm run dev   # http://127.0.0.1:5173
npm run build # tsc --noEmit 后 vite build → dist/
```

dev 期间要连的 gateway 默认是 `http://127.0.0.1:8765`, 用环境变量 `GATEWAY_ORIGIN`
覆盖。

## 两条容易踩的约定

- **`dist/` 不进 git**(`.gitignore` 已挡), 与 `spa-v2` 的既有做法一致。源码进 git,
  这样产物永远能从源码重建。
- **`vite.config.ts` 的 `base` 必须与后端挂载前缀一致**, 都是 `/feishu-web/`。后端
  挂载点在 `gateway/server.py` 的 `register_feishu_routes()` 里 (`add_static`)。改一边
  忘了另一边, 页面能开但资源全 404 —— 而且 aiohttp 侧 `dist/` 不存在时连 static 都不
  注册, 是静默 404, 不报错。

## 相关位置

- 后端挂载点与飞书路由: `../../server.py` 的 `register_feishu_routes()`
- ToC 前端(技术栈参照): `../../desktop/spa-v2/`

# feishu-web —— ToB 前端

## 这是什么

飞书侧 (ToB) 的 Web 前端 —— 「海豚一号」自建应用的**网页应用**能力, 与同一个应用的机器人
能力共用后端。

技术栈与 ToC 的 `spa-v2` 保持一致: Vite + React 19 + TypeScript。

## 为什么有它: 机器人开不了新会话

飞书机器人侧的 session 是**确定性派生**的 (`_feishu_manager.py` 的 `session_id_for`):
私聊永远是 `feishu-<open_id>` 一条, `route()` 幂等复用 + adopt, 所以同一个人**无法开第二
个会话**。历史落 `{appdata}/histories/{session_id}.jsonl`, 一个 session 一个文件 → 会话
内容与压缩一直往同一份文件里写, 上下文只增不分。

网页应用就是为解决这个: 「新建任务」走 `POST /feishu/sessions` **不传 id**, 后端发新 uuid,
于是新 session + 新 jsonl。

## 三条产品决定 (已拍定, 别再改方案)

1. **同一个人的多个会话共享同一个 workspace** —— session 各自独立 (各自一份 jsonl),
   workspace 共用一个目录。否则每开一个会话就多一个空目录、交付物散落。workspace 由后端
   `FeishuManager.workspace_for(open_id)` 派生, **前端不传 workspace**。
2. **IM 那条 session 在网页里正常显示、可续聊**, 打「来自飞书对话」角标, 双向可见。上下文
   将满的提示只挂在这一条上 (只有它会一直长)。
3. **第一版只做私聊**, 群聊 session (`feishu-chat-*`) 不显示。过滤精确到只滤群聊 ——
   用 `!startsWith('feishu-')` 会把私聊一起滤掉, 与决定 2 冲突。

## 身份与免登

- 免登走官方 JSSDK: `index.html` 同步引 `h5-js-sdk-1.5.35.js` → `h5sdk.ready` →
  `tt.requestAccess({appID, scopeList: [], ...})` 拿 code → `POST /auth/feishu`。
  两级退路见 `src/services/feishuAuth.ts` 模块头 (JSSDK 旧 / 客户端旧 `errno===103`)。
- **appID 从后端 `GET /feishu/app-id` 取, 不写死在前端。**
- **open_id 由后端向飞书换回来**, 前端传什么都不看。登录态是 HttpOnly cookie
  `psi_feishu_sid`。
- 会话一族走 `/feishu/sessions`(服务端按身份过滤), **不走裸 `/sessions`** —— 后者不过滤,
  在浏览器里 filter 只是显示过滤, 谁都能直接打裸路由拿全量。

## 已知敞口

骨架的 `GET /sessions` / `GET /sessions/{id}/history` 在本进程里**仍然无鉴权可达**。本轮
只做到「前端不再用它 + 过滤路由默认拒绝」, 真正封堵要靠 Gateway 前面的反代或骨架中间件,
是另一件事。

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

- 后端挂载点与飞书路由: `../_routes.py` 的 `register_feishu_routes()`
- ToC 前端(技术栈参照): `../../desktop/spa-v2/`

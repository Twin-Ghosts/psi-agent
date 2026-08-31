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
  `tt.requestAccess({appID, scopeList: [], ...})` 拿 code → `POST /feishu/auth/login`。
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
npm run dev   # http://127.0.0.1:5173/feishu-web/
npm run build # tsc --noEmit 后 vite build → dist/
```

dev 期间要连的 gateway 默认是 `http://127.0.0.1:8765`, 用环境变量 `GATEWAY_ORIGIN`
覆盖。

## 本地开发怎么起

**两个进程**, 缺一个都跑不起来。

**1. gateway** —— 开开发旁路, 不配 app_id:

```bash
cd <repo root>
PSI_FEISHU_DEV_OPEN_ID=ou_devtest_001 PYTHONPATH=src \
  python -c "from psi_agent.cli import main; main()" \
  gateway --gateway feishu --listen http://127.0.0.1:8765
```

- `--listen` **必须带 `http://`**。裸 `127.0.0.1:8765` 会掉进 Unix-socket 分支, 在 Windows 上
  直接 `ValueError`(见 `_sockets.py` 的 `create_site`)。
- 不带 `--listen` 时监听的是**随机高位端口**, 启动日志末行的 `Gateway listening on ...` 才是
  真实地址。这时 vite 那边要把 `GATEWAY_ORIGIN` 指到那个端口:
  `GATEWAY_ORIGIN=http://127.0.0.1:<随机端口> npm run dev`。固定 8765 省掉这一步。
- 同时起两个 gateway 要给不同的 `--socket-path`, 否则 scheduler Session 撞同一个管道名。

**2. vite dev server**:

```bash
cd src/psi_agent/gateway/feishu/feishu-web
npm ci && npm run dev   # → http://127.0.0.1:5173/feishu-web/
```

浏览器开 **`http://127.0.0.1:5173/feishu-web/`**(带 `base` 前缀, 少了它 302)。改前端文案
不用刷页面, HMR 会自己更新。

**启动日志末行的 `Local:` 必须是 `5173`。** 不是就别往下走 —— 见下面第三个静默坑。
`strictPort: true` 已经让端口被占时**启动即失败**(`Error: Port 5173 is already in use`),
这时不要换端口凑合, 先把占着 5173 的进程收掉:

```bash
# Windows: 找出占用者(常是另一个 worktree 里忘关的 npm run dev)
netstat -ano | grep ":5173" | grep LISTEN   # 末列是 PID
powershell "Get-CimInstance Win32_Process -Filter 'ProcessId=<PID>' | %{\$_.CommandLine}"
taskkill /F /PID <PID>
```

真要同时开两棵树, 用 `npm run dev -- --port 5273` 并把浏览器地址一起改掉,
**别**把 `strictPort` 改回 `false`。

`psi_feishu_sid` 是 `HttpOnly; SameSite=Lax; Path=/`, 经 proxy 后**能**带上, 不需要额外配
`cookieDomainRewrite`: 5173 与 8765 同为 `127.0.0.1`, 只有端口不同, 而 cookie 不按端口隔离,
`SameSite=Lax` 也只管站点不管端口。实测过。

不想起 vite 也行: `npm run build` 后直接开 `http://127.0.0.1:8765/feishu-web/index.html`,
gateway 自己 `add_static` 服务 `dist/`。代价是每改一行都要重新 build —— 这正是本地开发要
vite 的原因。

## 本地能验什么、不能验什么

**能验**:

- 开发旁路进得去(直接进会话列表), 页面顶部有「开发旁路身份: ou_xxx」告警条。
- 多会话互不串味: 建多个会话各发一句, 切换与刷新后各自只显示自己那句。
- 不设 `PSI_FEISHU_DEV_OPEN_ID` 时页面显示「请在飞书客户端内打开」而不是静默进入。
- proxy + cookie + HMR 这条链路。

**不能验**(别假装验过):

- **飞书客户端内的 `tt.requestAccess`**。本机浏览器没有 JSAPI, `window.h5sdk` 不存在,
  `code → user_access_token → open_id` 整条真免登链路一次都没跑。控制台那句
  `【H5-JS-SDK】: cannot find pc bridge` 就是它不在的证据。只能上云在真机验。
- **跨身份隔离**。旁路身份由后端的一个环境变量决定, 且 `POST /feishu/auth/login` 忽略 body
  里的 `open_id`(那是安全前提), 所以本机造不出第二个身份。这条靠
  `tests/psi_agent/gateway/test_feishu_identity.py` 与
  `tests/integration/test_feishu_web_sessions.py`(两个 sid 两个身份)加云上真机。
- 助手真的回话。本机注册的是假 `api_key`, 发消息后助手侧会报错 —— 不影响上面几条, 那些
  判据只看**用户自己那句话**落在哪个会话里。

## 三个静默坑(都实测踩过)

- **`vite.config.ts` 的 proxy key `'/feishu'` 是前缀匹配, 会把 `/feishu-web/` 一起吞掉。**
  本应用的 `base` 恰好也以 `/feishu` 开头, 于是前端路径连 `/@vite/client` 一起被代理到
  gateway: 打开 5173 拿到的是 gateway 里**上一次 build 的 dist**, 热更新永远不生效, 而
  `/feishu-web/` 带斜杠时 aiohttp 的 `show_index=False` 还会回 **403**。两个表现都不像
  「代理配错了」。现在那条 key 是正则 `'^/feishu(?!-web)'`, **别改回字符串**。
- **`strictPort: false` 会让 dev server 静默换端口, 于是 5173 上是别人的代码。** vite 的默认
  值就是 `false`: 端口被占**不报错**, 自己挪到下一个空闲端口(5173 → 5174 → 5175 ...), 而
  文档、书签、本文件里写的都还是 5173。5173 上活着的那个**别的** dev server(最常见来源:
  另一个 worktree 里忘关的 `npm run dev` —— Windows 上关终端不一定收走 node 进程)照旧应答:
  **页面能开、功能能用、改前端永远不生效**, 因为你看的是另一棵树的源码。唯一线索是 vite 日志
  里 `Port 5173 is in use, trying another one...` 那行, 常被 npm 的输出刷掉。
  与上一条的表现几乎一模一样, 成因完全不同 —— 上一条错在**服务什么内容**, 这条错在
  **服务在哪个端口**。现在 `strictPort: true`, **别改回 `false`**。
  判据: `tests/psi_agent/gateway/test_feishu_web_dev_strict_port.py`。
  确认自己连的是哪棵树(编译产物里带绝对路径, 一眼看出):

  ```bash
  curl -s http://127.0.0.1:5173/feishu-web/src/components/tasks-view.tsx | grep -o '_jsxFileName = "[^"]*"'
  ```

- **旁路的类型判据**: `requestFeishuCode()` 里 `sdkReady()` 必须先于 app_id 检查。反过来写
  时「app_id 为空 + 不在飞书客户端内」(正是本地开发的默认组合)抛的是普通 `Error`, 而
  `useAuth` 的退路只认 `FeishuAuthUnavailable`, 于是旁路整段被跳过, 页面停在「登录失败:
  后端未配置飞书 App ID」。只有这一个组合会踩, 所以它藏得住。

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

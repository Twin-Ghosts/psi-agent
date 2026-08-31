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

## 模型: 用机器人那一个, 网页应用不选

**网页应用没有「模型」这个概念。** 建会话挂哪个 AI 由后端 `GET /feishu/defaults` 给唯一
答案 —— 值就是 gateway 启动时的 `--feishu-ai-id`, 与机器人侧 `FeishuManager` 的缺省 AI 同
一个字段。机器人与网页应用本来就是**同一个 gateway 进程**, 于是两侧模型必然一致。

- **前端不打 `GET /ais`, 也不该有 AI 列表的概念。** 原先的写法是取 `ais[0].id`: 生产上恰好
  只有一条 AI 所以看着没错, 但 appdata 里存了多条时数组顺序无保证, 网页应用会**静默**用上
  一个和机器人不同的模型 —— 会话照样能建能聊, 没有任何报错。`listAis` 已从前端彻底删掉
  (`vite.config.ts` 的 `/ais` 代理也一并删了), 目的是让这件事在结构上不可能, 不是靠纪律。
- **端点只下发 id**, 不给 `api_key`/`base_url`/`provider`/`model` 任何一项。
- **没有兜底。** 拿不到就报错: 兜底触发就意味着悄悄换了模型, 静默走偏比直接报错难查。
- **不要做配置模型的页面或引导。** 飞书这条线是 ToB, AI 由部署者定死, B 端用户不该看见也
  不该改。ToC 的 `spa-v2` 那边用户自带 key、有配置页, 是另一件事, 别把那套搬过来。
- `--feishu-ai-id` 默认空。空的时候**不能建会话是正确行为**, 页面显示的是「本次部署未配置
  AI 实例…请联系管理员为 Gateway 配置 `--feishu-ai-id`」—— 指向部署配置, 不是让用户自己去
  配模型。页面不崩、会话列表照常显示。

判据在 `tests/psi_agent/gateway/test_feishu_defaults.py`(含「多条 AI 且指定的那条不是第一
条」这个唯一能暴出原缺陷的形状)。

### 本地怎么造一个 AI 实例

本机起 gateway 时 `/ais` 是**空的** —— 生产那份 appdata 在服务器上, 本机没有。所以本地开发
要自己造一份: AI 实例持久化在 `{appdata}/state/latest.json` 的 `ais` 数组, gateway 启动时从
`--appdata` 复原, `--feishu-ai-id` 只是**指名用哪一个, 它不创建 AI**。

照着敲(三步, `<...>` 全是占位符, 别把真 key 写进仓库或文档):

```bash
cd <repo root>
export PYTHONPATH=src
export DEV_APPDATA="$PWD/.tmp-dev/appdata"   # 随便一个本地目录, 别用真实 AppData

# 1. 起 gateway (第一次会建空 appdata)
PSI_FEISHU_DEV_OPEN_ID=ou_devtest_001 \
  python -c "from psi_agent.cli import main; main()" \
  gateway --gateway feishu --listen http://127.0.0.1:8765 \
  --appdata "$DEV_APPDATA" --feishu-ai-id dev-ai \
  --feishu-workspace-root "$PWD/.tmp-dev/ws"

# 2. 另开一个终端, POST 一条 AI 进去。id 必须与 --feishu-ai-id 一致。
curl -X POST http://127.0.0.1:8765/ais -H 'Content-Type: application/json' -d '{
  "id": "dev-ai",
  "provider": "<provider>",
  "model": "<model>",
  "api_key": "<your-own-key>",
  "base_url": "<https://your-endpoint>"
}'

# 3. 核对: 这里回的必须是 dev-ai
curl http://127.0.0.1:8765/feishu/defaults
```

AI 落进 `$DEV_APPDATA/state/latest.json` 后就**持久**了 —— 之后每次带同一个 `--appdata`
启动, 第 2 步不用再做。想复现「多条 AI」的场景就多 POST 几条不同 id, 再确认
`/feishu/defaults` 回的仍是 `--feishu-ai-id` 那一条。

**生产那把真 key 一个字符都不许进仓库。** 上面全是占位符, 本地用你自己的 key; 也不要在文档
或代码里写死具体模型名。

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
  gateway --gateway feishu --listen http://127.0.0.1:8765 \
  --appdata "$PWD/.tmp-dev/appdata" --feishu-ai-id dev-ai
```

- `--appdata` 与 `--feishu-ai-id` 是**建会话必需**的: 少了它们 `/feishu/defaults` 回空串,
  页面会说「本次部署未配置 AI 实例」。先按上面「本地怎么造一个 AI 实例」那三步造一条。

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

`psi_feishu_sid` 是 `HttpOnly; SameSite=Lax; Path=/`, 经 proxy 后**能**带上, 不需要额外配
`cookieDomainRewrite`: 5173 与 8765 同为 `127.0.0.1`, 只有端口不同, 而 cookie 不按端口隔离,
`SameSite=Lax` 也只管站点不管端口。实测过。

不想起 vite 也行: `npm run build` 后直接开 `http://127.0.0.1:8765/feishu-web/index.html`,
gateway 自己 `add_static` 服务 `dist/`。代价是每改一行都要重新 build —— 这正是本地开发要
vite 的原因。

## 本地能验什么、不能验什么

**能验**:

- 开发旁路进得去(直接进会话列表)。**提示在 gateway 启动日志里, 不在页面上** —— 启动时那条
  `FeishuAuth dev bypass is ENABLED at startup via PSI_FEISHU_DEV_OPEN_ID=ou_xxx` 就是它。
  页面上原先那条常驻通栏已撤: 旁路只在本机开发时开着, 而开发者就是启动 gateway 的人, 启动
  时喊一声就够, 不必占每个用户的一条通栏。每次旁路登录另有一条 WARNING(旁路**实际被用了**
  的痕迹), 与启动那条并存。
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

## 两个静默坑(都实测踩过)

- **`vite.config.ts` 的 proxy key `'/feishu'` 是前缀匹配, 会把 `/feishu-web/` 一起吞掉。**
  本应用的 `base` 恰好也以 `/feishu` 开头, 于是前端路径连 `/@vite/client` 一起被代理到
  gateway: 打开 5173 拿到的是 gateway 里**上一次 build 的 dist**, 热更新永远不生效, 而
  `/feishu-web/` 带斜杠时 aiohttp 的 `show_index=False` 还会回 **403**。两个表现都不像
  「代理配错了」。现在那条 key 是正则 `'^/feishu(?!-web)'`, **别改回字符串**。
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

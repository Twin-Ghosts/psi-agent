# `deploy/haitun/` —— 生产部署脚本的版本控制副本

## `oauth-proxy.py`

**这份是生产机 `/srv/haitun/psi-agent/oauth-proxy.py` 的版本控制副本, 不是运行中的那份。**

改了这里**不会**对生产产生任何影响。生效需要人工同步:

1. 由负责人批准改动;
2. 拷到生产机 `/srv/haitun/psi-agent/oauth-proxy.py`;
3. 重启栈。**必须连带重建 `oauth-proxy` 容器** —— 它在 compose 里是
   `network_mode: "service:gateway"`, 只重建 `gateway` 时它会显示 `Up` 但网络命名空间
   已经失效。

收进仓库的原因: 它是**公网唯一入口**、决定了哪些路径能从外网打到 Gateway, 而此前只存在
于那一台机器上 —— 一个没有版本控制、没有 review、没有判据的安全关键文件。

### 它在链路里的位置

```
浏览器 / 飞书客户端
      │  443
      ▼
   Caddy (占 80/443, TLS 终止)
      │  反代到 127.0.0.1:8090
      ▼
   oauth-proxy.py  ← 本文件。白名单反代, 白名单外一律 404
      │  转发到 127.0.0.1:8080
      ▼
   Gateway 容器 (与本代理共享 netns, 故上游是 127.0.0.1)
```

Gateway 端口**不对外暴露**, 这一跳是唯一的入口。

### 它为什么必须是白名单

Gateway 上有一批**一行鉴权都没有**的路由, 与飞书网页应用的接口同住一个进程:

| 路由 | 危害 |
| --- | --- |
| `POST /sessions/{id}/chat` | 直接驱动 agent 执行工具, 含 bash。**带鉴权的对等物 `/feishu/sessions/{id}/chat` 是放行的**, 裸的这条不放行 |
| `POST /sessions` | 建 Session |
| `GET /sessions` `GET /sessions/{id}/history` | 读任意会话历史 |
| `GET /workspace/file` | 读 workspace 里的文件 |
| `POST /chat/completions` | 直接用掉模型额度 |

挡住它们的只有这个白名单一层, 所以 `ALLOWED_PATHS` / `ALLOWED_PREFIXES` **只列前端真的
会打的路径**, 多放一条就是白送一份公网暴露面 —— 而多放行**没有任何症状**, 直到有人从
公网打过来。

### 改白名单前先看判据

`tests/deploy/test_oauth_proxy.py`(20 条)双向钉住:

- 该放行的没放行 → 红(清单来自 `feishu-web/api-paths.json`, 前端加端点会被发现);
- 不该放行的放行了 → 红(`test_core_routes_stay_blocked` 逐条列了上表那些);
- 头没双向转发、多条 `Set-Cookie` 丢了、路径穿越能过 → 各有一条。

```bash
# 在仓库根跑。PYTHONPATH=src 与 -o testpaths= 都是必须的, 见 AGENTS.md
PYTHONPATH=src .venv/Scripts/python.exe -m pytest -o testpaths= --no-cov tests/deploy/ -q
```

### 已知没验到的

**生产真机一次没验。** 本轮只在仓库里出代码 + 本地判据, 假上游不是真 Gateway。同步到生产
后至少要量三件事(前两件本地量不到, 见 `feishu-web/AGENTS.md` 的「本地与云上的分叉点」):

1. 真免登能拿到 cookie 并保持登录 —— 本机没有 JSAPI, 整条 `code → open_id` 换取链没跑过;
2. 放行清单逐条可达:
   ```bash
   python scripts/feishu_web_paths.py --print-shell > check-feishu-web-paths.sh
   bash check-feishu-web-paths.sh http://127.0.0.1:8090
   ```
   注意这份清单含 `/sessions` `/titles` `/workspace` 一族 —— 那几条**在这一跳报 FAIL 是
   预期的**(刻意不放行), 不要照着把它们加进白名单。
3. `/feishu-web/` 的静态产物能加载(依赖 Gateway 侧 `dist/` 存在, 不存在时 `add_static`
   静默跳过)。

### 放行范围里有 SSE, 转发层因此是流式的

`POST /feishu/sessions/{session_id}/chat`(带鉴权的聊天流, 能驱动 agent 执行工具)**在放行
范围内**, 它是一条 SSE。

它不是被单独加进白名单的, 而是**被前缀捎带进来的**: `ALLOWED_PREFIXES` 里的
`/feishu/sessions/` 原本是为 `GET /feishu/sessions/{id}/history` 加的, `startswith` 把同一
前缀下的 chat 一起放行了。这一点值得留意 —— 往那个前缀下加路由**不需要动白名单就会自动
对公网可达**, 加的时候要自己判断该不该暴露。

于是转发层用 `web.StreamResponse` 边收边转(`_relay`), 不是把 body 读完再回。三处硬要求,
每处都有判据:

| 要求 | 写错的表现 | 判据 |
| --- | --- | --- |
| 逐块转发, 不自己攒缓冲 | 打字机效果消失, 长回答疑似卡死 | `test_sse_chunks_arrive_before_upstream_finishes` |
| 不设 `Content-Length`, 交给 chunked | 截断, 或客户端等永远补不齐的字节 | `test_sse_response_has_no_content_length` |
| 响应头在 `prepare()` **之前**写完 | `Set-Cookie` 静默丢失, 登录不上 | `test_set_cookie_survives_streaming` |

超时也跟着改了: `ClientTimeout` **不设 `total`**。`total` 管的是「从发出到响应体读完」的
整段时间, 对 SSE 就是一条硬性寿命 —— 原先的 `total=15` 会让生成超过 15 秒的长回答从中间
断掉(实测: 客户端收到前几个 event 后拿到 `ClientPayloadError`, 等不到 `[DONE]`), 而短回答
一切正常, 所以这个缺陷很容易漏。改用 `sock_connect` + `sock_read` 两个闸, 它们量的都是
**间隔**而非总时长, 上游真卡死时仍能断开。由
`test_upstream_timeout_has_no_total_deadline` 钉住。

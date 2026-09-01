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
| `POST /sessions/{id}/chat` | 直接驱动 agent 执行工具, 含 bash |
| `POST /sessions` | 建 Session |
| `GET /sessions` `GET /sessions/{id}/history` | 读任意会话历史 |
| `GET /workspace/file` | 读 workspace 里的文件 |
| `POST /chat/completions` | 直接用掉模型额度 |

挡住它们的只有这个白名单一层, 所以 `ALLOWED_PATHS` / `ALLOWED_PREFIXES` **只列前端真的
会打的路径**, 多放一条就是白送一份公网暴露面 —— 而多放行**没有任何症状**, 直到有人从
公网打过来。

### 改白名单前先看判据

`tests/deploy/test_oauth_proxy.py`(15 条)双向钉住:

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

### 一条已知的设计边界: 响应不流式

本代理把上游响应**一次读完再回**(`await resp.read()`), 不是边收边转。今天放行的路径里
没有一条是流式的 —— 前端唯一走流的是 `chatStream.ts`, 它打 `POST /sessions/{id}/chat`,
而那条刻意不放行。

**所以哪天要放行 chat 一族, 光往 `ALLOWED_PATHS` 加一条是不够的**: SSE 会被这里缓冲成
「等全部生成完才一次性吐给浏览器」, 表现是打字机效果消失、长回答疑似卡死。那时要改成
`web.StreamResponse` 边收边写。记在这里, 因为这个缺陷加白名单时看不出来。

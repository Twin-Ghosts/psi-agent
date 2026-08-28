# Gateway / Workspace 架构重排 —— 交付汇报

**汇报日期**：2026-08-28　**分支**：`refactor/gateway-workspace-evolution` @ `3fa34a4c`　**基线**：`main` @ `64b6273b`（未动）

---

## 一、结论

本轮 **A1–A7 七项 + B1/B2/B5/B6 四项落地并验收通过**（B2 与 A 线共用一次提交），**B3 做过后撤回**，另完成 review 提出的两项：OAuth 中继搬进 `feishu/`、抽出 `workspace/toc`。核心目标达成：

| 目标 | 结果 | 判据 |
|---|---|---|
| 骨架层与产品线解耦 | **达成** | 骨架 `.py` 从 26 → **6** 个；`server.py` 对产品符号的反向 import **8 → 0** |
| 内核不反向依赖产品线 | **达成** | 新建 `runtime/` 包（11 文件），`runtime → gateway` 代码依赖 **0** |
| 两条产品线各自成包 | **达成** | `desktop/` 12 个 `.py`、`feishu/` 5 个 `.py`，互不 import |
| ToB 能力包脱离 `examples/` | **达成** | `examples/` 12 → **11** 个示范件；生产资产独立为 `workspace/tob`（486 文件） |
| ToC 能力包独立 | **达成** | 抽出 `workspace/toc` **266 文件**；提示词里点名却不存在的工具 83 → **0**（见 2.5） |
| 行为零回归 | **达成** | 全量 57 failed / **1481 passed** / 7 skipped，失败集与基线**逐条相同** |
| ToB 前端脚手架 | **达成** | 9 个源文件，构建 654ms，占位页 `GET /defaults` HTTP 200 |

**要负责人拍的事集中在第九章**，共 5 项，都是**动手前必须先定边界**的设计题：出厂内容与用户数据怎么分（9.1）、开发启动方式（9.2）、`SOUL.md` 归谁（9.3）、`toc`/`tob` 的重复成本（9.4）、桌面版要不要长期记忆（9.5）。B3 正是没先定边界就动手、做完发现没换来保护而撤回的例子。

**还差的一件事**：`workspace/toc` 没接上 ToC gateway 真起进程跑过 —— 只验到内核能加载、6 个 hook 全解析、提示词组装正确，没发过一条真消息。等 9.2 定了启动形态才好接（见 7.1）。

---

## 二、改了什么（结构对比）

本轮动了**两块**：`src/` 下的代码结构（A 线）与 `workspace/` + 安装器结构（B 线）。分开讲。

### 2.1 一句话概括

**代码侧**：原来 `gateway/` 一个包里塞了 26 个模块 —— 内核管理器、桌面端（托盘/webview/登录）、飞书端混在一起，且内核管理器反向 import 桌面端的品牌字面量。现在切成三层：内核 `runtime/`、骨架 `gateway/`、产品线 `gateway/{desktop,feishu}/`。

**workspace 侧**：ToB 的能力包原先藏在 `examples/` 里当"示范件"，实际是生产资产（安装器打的就是它）。现在迁出为顶层 `workspace/tob`，并把包内 / 包外文件在安装器里分清落点。

### 2.2 代码结构前后对照（A 线）

```
【改前】main @ 64b6273b                    【改后】HEAD @ 3fa34a4c
src/psi_agent/gateway/                     src/psi_agent/runtime/          ← 新建, 内核
├── _ai_manager.py        ┐               ├── _ai_manager.py        ┐
├── _session_manager.py   │ 10 个          ├── _session_manager.py   │ 10 个 manager
├── _scheduler_manager.py │ manager        ├── _scheduler_manager.py │ 1740 行整体平移
├── ... (另 7 个)          ┘               ├── ... (另 7 个)          ┘
├── _tray.py              ┐               │
├── _webview.py           │               src/psi_agent/gateway/           ← 骨架, 只剩 6 个 .py
├── _auth_manager.py      │ ToC           ├── __init__.py      (装配入口)
├── _workspace_manager.py │ 12 个         ├── server.py        (1031 → 610 行)
├── ... (另 8 个)          ┘               ├── _defaults.py     (品牌字面量唯一落点)
├── _feishu_manager.py    ┐ ToB           ├── _openapi.py / _openapi_core.py
├── ...                   ┘               ├── _oauth_manager.py / _state.py
├── spa/  spa-v2/         (2 棵 SPA)      ├── desktop/         ← ToC 产品包 (12 .py + 2 棵 SPA)
├── server.py  (1031 行)                  └── feishu/          ← ToB 产品包 (4 .py + feishu-web)
└── _openapi.py (915 行)
```

### 2.3 workspace 与安装器结构前后对照（B 线）

```
【改前】main @ 64b6273b                    【改后】HEAD @ 3fa34a4c
examples/                                  examples/                  ← 只剩 11 个示范件
├── haitun-workspace/   ← 生产资产          ├── openclaw-style-workspace/
│   485 文件, 安装器打的就是它,             ├── hermes-style-workspace/
│   却和示范件混住                          ├── ... (另 9 个, 全是真示范)
├── openclaw-style-workspace/               │
├── hermes-style-workspace/                 workspace/                 ← 新建顶层目录
├── ... (另 9 个)                           ├── tob/                   ← 486 文件, 生产资产独立
   合计 12 个                               └── toc/                   ← 269 文件, 桌面版能力包
                                               合计 11 + 2

安装器 haitun.iss                           安装器 haitun.iss
Source: 11 条                               Source: 11 条  ← B3 已撤回, 见 7.3 / 9.1
└── examples\haitun-workspace\*             └── workspace\tob\*
    整目录一把拷贝, 升级时                       整目录一把拷贝, 换了路径没换语义,
    用户数据混在里面分不出来                     出厂内容与用户数据仍混住(留作讨论项)
```

| 指标 | 改前 | 改后 |
|---|---|---|
| `examples/` 下 workspace 个数 | 12（含 1 个生产资产） | **11**（全是真示范件） |
| `workspace/` 顶层目录 | 不存在 | **新建**，含 `tob` + `toc` |
| ToB 能力包文件数 | `examples/haitun-workspace` 485 | `workspace/tob` **486** |
| ToC 能力包文件数 | 不存在 | `workspace/toc` **266**（见 2.5） |
| 该次搬迁改动量 | — | 529 文件，+250 / −243 |
| 安装器 `Source:` 行数 | 11 | **11**（B3 已撤回，见 7.3 / 9.1） |
| `system_prompt.py` 推 agent 根方式 | `__file__` 硬推 4 处 | **接收传入路径**（B6，+328 / −22） |
| hook 契约测试 | 无 | **12×6 实测表**（B5） |

**B3 做过又撤回了，`Source:` 由 14 条回到 11 条** —— B3 原本把 `SOUL.md` / `USER.md` / `schedules\*` 从整目录拷贝里摘出来单独列，想让「哪些是出厂的、哪些是用户的」在安装器里有个落点。撤回的理由是：`{app}\app` 上挂的是 `SwapComponent('app')`，升级时整目录换新，**单独列 `Source:` 并不改变任何换新行为** —— 摘出来的三项照样被换掉。也就是说 B3 只是把清单写长了 3 行，没换来任何保护，反而让人以为这问题已经处理过了。出厂内容与用户数据怎么分，本身是个要先定边界再动手的设计题（升级保数据、用户改过的出厂文件怎么办、`SOUL.md` 归谁），已作为 **9.1** 的讨论项，定下来后单独开 PR 做。

### 2.4 关键数字汇总

| 指标 | 改前 | 改后 |
|---|---|---|
| `gateway/` 骨架层 `.py` 文件数 | 26 | **6** |
| `runtime/` 内核包 `.py` 文件数 | 0（不存在） | **11** |
| `server.py` 行数 | 1031 | **610** |
| `server.py` 反向 import 产品符号 | 8 行 | **0 行**（`_oauth_manager` 已搬进 `feishu/`，见 4.1） |
| `runtime → gateway` 代码依赖 | 内核候选有 2 处指向 ToC | **0** |
| `_openapi` | 915 行单文件 | 拆 4 份（装配 58 + 公共 CORE 16 path + ToC 6 + ToB 4），26 个 path key 并集不变 |
| `examples/` 下 workspace 个数 | 12 | **11** |
| `workspace/` 下能力包个数 | 0（不存在） | **2**（`tob` + `toc`） |
| 安装器 `Source:` 行数 | 11 | **11**（B3 的分包改动已撤回，见 7.3 与 9.1） |
| 提交数 / 改动量 | — | 18 次提交（另加本轮 `toc` 抽取） |

> 〔截图位 1〕启动日志：三行分别显示 `runtime` 管理器创建、`desktop._routes` 挂载、`feishu._routes` 挂载

### 2.5 `workspace/toc` 抽取（本轮新增）

以 `workspace/tob` 为起点，去掉落到飞书的能力，留下通用能力，抽出桌面版能力包 `workspace/toc`。

| 指标 | 数字 |
|---|---|
| 文件数 | **266**（从 tob 的 486 个在库文件里挑，丢弃 220） |
| 工具 `.py` 文件 / 实际注册工具 | **85 文件 / 93 工具**（tob 是 151 文件） |
| skills | **102**（tob 是 145，少 43 个 ToB 专用的） |
| hook 数 | **6/6**（与 tob 一样全解析，`systems/` 只差 3 处提示词文字） |
| 组装出的系统提示词长度 | **131707 字符**，是 tob 的 66% |
| 提示词里点到但包内不存在的工具 | **0**（第一版是 83 个，见下） |
| 提示词里 `feishu` 出现次数 | **6**（tob 是 398），全是刻意留的交叉引用 |

**丢弃规则**（按区块）：`tests/` 67 个全丢（测的是 ToB 行为），`channel_events/` 41 个全丢（飞书事件落库），43 个 ToB 专用 skill 丢，66 个不在通用闭包里的工具丢。

**判定通用与否，用内核自己当裁判，没用我的静态分析。** 起因是我先后用 AST、正则、精确 import 三种办法算依赖闭包，得出 92 / 95 / 93 / 85 四个互相矛盾的数字，连 5 个 `memory_*` 到底算不算通用都对不上。根因是这个仓库里有**三种 import 写法**并存：普通 `import`、`_load_sibling_module("名字")` 字符串加载、`TOOLS_DIR / f"{名字}.py"` 拼路径后 `exec`。后两种任何静态分析都抓不全。改成直接调内核的 `ToolRegistry.load()` 加载两个包再比集合，才拿到可信的数：`toc` 有而 `tob` 没有的工具 **0 个**，`toc` 里的飞书/派工工具名 **0 个**。

**`memory_*` 5 个工具不带**，因为这条链是硬的：`memory_*` → `_fusion_memory_mcp.py:56` → `_fusion_memory_membership.py:14` → `_feishu_impl.py`。「谁的身份在写记忆」是拿飞书 `open_id` 认的，桌面版没有飞书身份，整条链落不了地。桌面版要长期记忆，需要另设一套本地身份，不是把这条链搬过来 —— 列为 **9.5** 讨论项。

**顺手抓出一个纯拷文件会静默带走的缺陷。** `AGENTS.md` / `TOOLS.md` / `IDENTITY.md` / `BOOTSTRAP.md` 这四个文件是被 `_build_bootstrap_files` **整篇原文塞进系统提示词**的。直接拷过来，提示词里就点着 83 个这个包里并不存在的工具名 —— 模型会照着去调，然后拿到报错。所以这四份文档连同 `systems/prompt_sections.py` 里三处无条件注入的段落都得改：删掉 22 行过期工具表、25 条过期 skill 条目、整节 Fusion Memory 与 Channel events，把 `SEND_FILES_SECTION` 的渠道列表和 `SILENT_REPLIES_SECTION` 的飞书卡片例外改掉。83 → 6 → 3 → **0**，每一轮都是重新组装提示词实测出来的。中间还有一次是我自己写的说明文字漏了 —— 我在「本包没有记忆工具」那节里把 5 个工具名写了出来，这段本身又进了提示词，等于换个地方点名。改成不写名字、只讲原因。

---

## 三、逐项任务与验收

### A 线（骨架拆分，7 项）

| 项 | 做了什么 | 验收判据（实测） |
|---|---|---|
| A1 | 切断内核候选对 ToC 的 2 处依赖，品牌字面量收拢到 `_defaults.py` | 该文件成为 `haitun交付` / `workspace/tob` 的唯一落点 |
| A2 | 10 个 manager、1740 行移出 `gateway/` 建 `runtime/` | `runtime → gateway` 依赖归零 |
| A3 | `_openapi.py` 915 行按 path 拆三份 | 26 个 path key 并集与 schema **逐一不变** |
| A4 | 17 参装配函数拆成骨架 + 两个"贴纸" | 桌面端不再构造飞书管理器 |
| A5 | 12 个产品模块 + 2 棵 SPA 落位 `desktop/` 与 `feishu/` | 骨架层剩 5 个装配件 |
| A6 | ToB 前端脚手架 9 个源文件 + 1 个静态挂载点 | S1–S6 六条全过，后端只多 1 个 `add_static` |
| A7 | 两个 `register_*_routes` 搬进产品包 | 骨架反向 import **7 → 0**；路由表逐条不变 |

### B 线（workspace 与内核，5 项）

| 项 | 做了什么 | 验收判据（实测） |
|---|---|---|
| B1/B2 | `examples/haitun-workspace` → `workspace/tob` | 60 处引用清零；**补回 10 个静默消失的测试** |
| B3 | ~~ToC workspace 分包内 / 包外~~ **已撤回** | 做过并逐条核对过 14 个 `Source`、516 文件落点不变，但**撤回了** —— 单独列 `Source:` 不改变 `SwapComponent('app')` 的整目录换新行为，没换来保护。见 2.3 与 9.1 |
| B5 | hook 契约钉成 12×6 实测表 | 实测 12 个 workspace 里**只有 `tob` 满 6 个，其余 11 个只暴露 2–3 个** |
| B6 | 4 处 `__file__` 推根改为接收传入路径 | 5 个 hook 调用点补上 agent 根 |

**B1/B2 的意外收获**：两个测试文件用 `Path("examples").glob("*/systems/system.py")` 做参数化，`haitun-workspace` 一搬走，它的 10 个用例**不报错地消失了** —— pytest 不认为参数化列表变短是错误。这类"静默丢测试"是搬迁类改动的典型陷阱，已补回。

**B5 的判据纠正**：任务书原写"逐个 workspace 断言 6 个 hook 都非 None"，这是**错的判据**。`turn_context_fn` 与 `compaction_fn` 的 `None` 按内核约定**承载语义**（"这个 workspace 没有易变块"），断言全非 None 会一次红 11 个 workspace，钉的是内核并不存在的契约。

---

## 四、骨架层剩下这 6 个文件，各自为什么留下

拆完之后有人会问：既然分了 ToC 包和 ToB 包，为什么骨架层还剩这几个文件？逐个给判据。

| 文件 | 行数 | 它是什么 | 为什么不能进产品包 |
|---|---|---|---|
| `__init__.py` | — | **总装配入口**。先起骨架，再把 ToC / ToB 两张"贴纸"贴上去 | 它要同时认识两条线才能装配，进任一包都会让那个包被另一条反向依赖 |
| `server.py` | 610 | **公共路由** —— 会话、AI、标题、摘要这些两条线都要的接口 | 两条线共用，不归任何一条 |
| `_defaults.py` | 105 | **品牌字面量的唯一落点**（`haitun交付`、`workspace/tob`） | 内核建 Session 要拿这些路径。放进产品包，内核就得反向 import 产品线 —— 正是 A1 消除的那种依赖 |
| `_state.py` | — | 重启后恢复现场用的状态快照 | 两条线共用 |
| `_openapi.py` | 58 | **说明书装订工**（详见 4.2） | 同时 import 三份章节，进任一包即造成两线互相耦合 |
| `_openapi_core.py` | 700 | **说明书公共章节**，16 条两线都注册的接口 | 不归任何一条线 |

`_oauth_manager.py` 本来是第 7 个，本轮已搬进 `feishu/`，详见 4.1。

### 4.1 `_oauth_manager.py` —— 本轮已搬进 `feishu/`

它做的事很小：浏览器跳到 `/oauth/callback` 时，按一个随机串 `state` 把授权码 `code` 存进带 TTL 的内存信箱；发起方用同一个 `state` 从 `/oauth/code` 取走，一次即删。用途是**免去用户从地址栏手工复制授权码**。

我最初把它留在骨架层，理由是"这段代码不认识飞书"。这一半成立 —— 69 行里零飞书字样，不碰 token 交换，不知道 app_secret，也不知道是哪个用户。

**但漏查了实际消费者。补测结果：取件方全在 ToB 一侧，ToC 零调用。**

```
workspace/tob/tools/_oauth_receiver.py:38    _CALLBACK_PATH = "/oauth/callback"
workspace/tob/tools/_oauth_receiver.py:220   client.get(f"{base}/oauth/code", ...)
workspace/tob/tools/_oauth_setup.py          整个文件都在讲飞书后台怎么登记回调
workspace/tob/tools/feishu_auth.py:16        通过 Gateway 的 /oauth/callback 中继
workspace/tob/tools/_feishu/auth.py:21       import _oauth_receiver

desktop/_auth_manager.py:7                   仅注释, 说命名风格与 OAuthRelay 同级
desktop/_auth_manager.py:16                  仅注释, "跳转留给将来的 OAuth"  ← 将来时
```

**ToC 的登录已经做完了，走手机号 + 验证码，不经过 OAuth 跳转。** 我此前拿 `_auth_manager.py:16` 那句"将来复用"当留在骨架层的论据，是用一句注释里的将来计划去支撑一个当下的位置决定 —— 违背方案自己的"先问存在性、不为假想需求预留"。

**结论：按当前实测它应当搬进 `feishu/`。** 这属于"机制通用但当前只有一个消费者"，方案 §3.2 四条判据里"认识什么概念"中立、"先问存在性"指向产品包，判据本身有冲突，不是一边倒。选择搬走的理由是：消费者单一且明确，等 ToC 真要用 OAuth 那天再往上提，那时是有真实需求驱动的移动，成本比现在为假想需求占位低。

**本轮已做**（提交 `456009d3`）。实际改动面：文件搬到 `gateway/feishu/_oauth_manager.py`，`server.py` 去掉 3 处（`app["oauth"]` 赋值 + 两个 handler）与 2 条路由注册、改由 `feishu/_routes.py` 注册，spec 片段从 `CORE_PATHS` 挪进 `FEISHU_PATHS`，另动 3 个测试文件与 `gateway/AGENTS.md` 3 处。

**实测结果**：骨架层 `.py` 由 7 降到 **6**；`server.py` 1031 → **610** 行；骨架层反向 import 产品符号 **8 行 → 0 行**；`_openapi_core` 公共章节 18 → **16** 条，`FEISHU_PATHS` 2 → **4** 条，**26 个 path key 的并集与 schema 逐一不变**；全量失败集合与基线 57 行逐行相同。

**这件事的方法论收获**：方案 §3.2 那四条判据里，「代码认识什么概念」和「先问存在性」会打架 —— 这个文件 69 行零飞书字样，按前者是通用的；消费者全在 ToB，按后者归产品包。**冲突时以「先问存在性」为准。** 我第一版判断错，错在拿一句写着「将来」的注释去支撑一个当下的位置决定。

### 4.2 两个 `_openapi*` 文件是干什么的

**不是注册路由，是生成一份 API 说明书。** 这两件事要分清：

| | 谁干的 | 作用 | 删掉会怎样 |
|---|---|---|---|
| **注册路由** | `server.py` + 两个 `_routes.py` | 告诉服务器"有人访问 `/sessions` 时执行哪个函数" | **接口不能用了** |
| **OpenAPI** | 四个 `_openapi*` 文件 | 生成一份 JSON 说明书，写着本服务有哪些接口、各收什么参数、返回什么 | 接口照样能用，只是**没人知道怎么对接** |

访问 `http://127.0.0.1:8080/openapi.json` 拿到的就是这份说明书，前端和第三方靠它对接。

那三份是说明书的三个章节，第四份是装订工：

```
_openapi_core.py    700 行  →  16 条公共接口的说明   (/sessions /ais /titles ...)
desktop/_openapi.py 111 行  →   6 条 ToC 专属接口     (/ui/* /workspace/*)
feishu/_openapi.py   98 行  →   2 条 ToB 专属接口     (/feishu/route /feishu/routes)
_openapi.py          58 行  →  把上面三份订成一本
```

18 + 6 + 2 = **26 条**，与改前那个 915 行单文件的 path key 集合一字不差〔实测 `build_openapi_spec()` 与 `OPENAPI_SPEC` 都是 26〕。

**改前的毛病**：26 条说明混在一个大字典里。飞书容器想只发布自己那 2 条做不到，只能把 ToC 那 6 条一起发出去 —— 等于把桌面端的内部接口告诉飞书那边的对接方。

**为什么装订工必须在顶层**：`_openapi.py` 第 25–27 行同时 import 了三份章节。要是把它放进 `desktop/`，`desktop` 就 import 了 `feishu`，两条产品线立刻互相耦合 —— 正是本轮花七步消除的那种依赖。它必须站在两条线之上，那个位置就是骨架层。`_openapi_core.py` 同理：装的是两条线**共有**的 16 条，塞进哪个产品包都会让另一条反向依赖。`/oauth/*` 那 2 条本轮已随 `_oauth_manager` 从这里挪进 `FEISHU_PATHS`，所以公共章节由 18 条降到 16 条。

---

## 五、验证（怎么证明没坏）

### 5.1 全量测试

```
57 failed, 1481 passed, 7 skipped, 653 warnings in 350.67s   ← 含 toc 抽取后的最新一次
```

passed 由 1469 涨到 1481，多出的 12 条是 `toc` 进契约表后 3 个 workspace 遍历测试的参数化用例（`len(WORKSPACES)` 12 → 13）—— 从 junit XML 里数出来的：8 条 `test_compact_history_chaining` + 2 条 `test_compaction_prompt_injection` + 2 条 `test_workspace_hook_contract`。

那 57 条**不是回归**，是 Windows 上的既有失败（asyncio 子进程 `NotImplementedError`）。判据不是"失败数相同"而是**失败集逐条相同**：

```
基线 baseline-failures.txt : 57 行, md5 6af0fceab6945fb18c2f85d4efbf326b
本轮 junit-xml 提取        : 57 条
diff --strip-trailing-cr   : IDENTICAL  ← 参数化标记 [asyncio] 完整保留
```

> 〔截图位 2〕全量测试尾部输出 + failure-set diff 的 IDENTICAL 结果

### 5.2 路由表逐条核对

A7 把装配函数整体搬包，最大风险是漏挂路由。用同一份脚本跑改前（`3d687c37`）与改后两棵树，路由表**逐条字节相同**。当前实测：

```
desktop routes: 46    feishu routes: 5    合计 51
```

> 计数会随参数变化（`authm` 为 None 时少 11 条 `/auth/*`；`add_static` 一次注册产生多条 route 记录），所以"条数"本身是约定依赖的；**实质判据是逐条比对，两种参数组合下都相同**。

### 5.3 三个前端产物实测

| 前端 | 产物 | 挂载点 | 实测 |
|---|---|---|---|
| ToC v1 | 72 文件 / 11M | `/spa/` | index + JS + CSS 全 200 |
| ToC v2（默认） | 74 文件 / 7.3M | `/spa-v2/` | index + JS + CSS + PNG 全 200 |
| ToB 脚手架 | 2 文件 / 189K | `/feishu-web/` | index + JS 200，页面显示 `GET /defaults: HTTP 200` |

三份 `dist/` 均被 `.gitignore` 覆盖，**0 个产物被跟踪**，`git status` 无受跟踪改动。

> 〔截图位 3〕ToC 桌面端 `/spa-v2/` 完整对话（含工具调用、标题生成）
> 〔截图位 4〕ToB 占位页 `/feishu-web/` 显示 HTTP 200

### 5.4 三向同步

本轮同步更新 **8 份 AGENTS.md**：根、`gateway/`、`runtime/`、`session/`、`workspace/tob/`、`desktop/spa/`、`desktop/spa-v2/`、`feishu/feishu-web/`。

---

## 六、当前启动形态（团队常问）

**一个进程，两条线都贴。** 这是刻意设计，不是遗留问题。

```python
# gateway/__init__.py:246  骨架先起
app = await create_core_app(aim, sm, tm, rm=rm, ...)
# :257  ToC 贴纸
await register_desktop_routes(app, favicon_path=..., app_name=..., attention=..., authm=...)
# :264  ToB 贴纸
register_feishu_routes(app, feishu_ai_id=..., feishu_workspace_root=...)
```

生产上飞书容器起的也是同一个 `psi-agent gateway`（同容器内另起 `psi-agent channel feishu` 连过来），所以两面都必须贴，少贴哪面都是行为回归。

**本轮拆的是装配函数，不是进程入口** —— 收益在于"谁认识什么"现在写在函数签名里：`register_desktop_routes` 收 `favicon_path/app_name/attention/authm`，`register_feishu_routes` 收 `feishu_ai_id/feishu_workspace_root`，两者零交叉。真要一个纯 ToB 进程，代价是少调一行，不需要重构。

启动命令不变：

```
psi-agent gateway --listen http://127.0.0.1:8080 --browser
```

---

## 七、遗留与建议

### 7.1 `workspace/toc` 本轮已落地，但还没接上 ToC gateway 真跑

**原来的遗留是**：`workspace/` 下只有 `tob` 一个包，它**一身两职** —— 既是 ToC 桌面端出厂的能力包（安装器 `haitun.iss:79` 打的就是它），又是 ToB 飞书机器人的 workspace。实测其构成：

| 项 | 数量 |
|---|---|
| tools 总数 | 140 |
| 其中 `feishu_*` | 28 |
| 其中 `assignment_*`（ToB 派单） | 8 |
| 其中 `handbook_*` / `haibao_*` | 3 |
| 通用 | 101 |
| skills 总数 | 145 |
| 名字含 feishu/飞书 | 30 |

**这不是命名问题，是 ToC 用户在为 ToB 业务付提示词成本** —— 装机用户拿到的包里含 28 个飞书 API 工具、8 个内部派单工具、30 个飞书技能。

**本轮已做**（提交 `80b54129`）：**从现有包减出 ToC**，不按原方案"以 `openclaw-style` 为基线新建"—— 实测该基线只有 5 个 tools、1 个 skill，从它起步等于把 ToC 出厂能力砍到近零，是回退不是演进。落地数字与判定方法见 **2.5**：266 文件、85 工具文件注册 93 个工具、102 个 skill、hook 6/6、提示词点名不存在的工具 0 个。

**上一版写的三条"为什么不在本轮做"，现在的实际情况是**：

- ①「属新增交付物，不在本轮范围」—— 负责人本轮明确要求做，范围已放开。
- ②「与 B4 / T1 绑定」—— 这条**不成立，是我判断错了**。抽 `workspace/toc` 只往库里加一个目录，不改 `.iss` 一行；`.iss` 现在打的仍是 `workspace\tob`。安装器该打哪个包，是 9.1 讨论定了之后的事，两件事不必绑在一起。
- ③「共享层机制未查清」—— **本轮查清了，结论是没有共享层可做**：内核的 `ToolRegistry.load(cls, tools_dir: Path, session_id: str = "")` 只收**一个** `tools_dir`。不改内核就没有多根目录，`toc` 与 `tob` 之间不存在能共享的能力层，抽取必然意味着重复。这个代价是真的，列为 **9.4** 讨论项。

**还差什么**：`toc` 没接上 ToC gateway 真起进程跑过。本轮只验到内核能加载它、6 个 hook 全解析、提示词组装正确，没发过一条真消息。开发启动方式（一进程还是两进程）是 **9.2** 的讨论项，定下来才好接。

### 7.2 其他已知未修（均非本轮引入）

- **命名管道跨文件污染**：`tests/integration/test_gateway.py` 与 `tests/psi_agent/gateway/test_feishu_manager.py` 共用硬编码前缀 `gw-test`，全量跑时前者留下同名管道，后者绑定被 `[WinError 5]` 拒掉。表现为 `test_route_*` 里随机某条失败，单跑该文件 27/27 全绿。该前缀在改动前逐字相同。
- **`{app}\app` 升级时整目录换新**：用户数据（`SOUL.md`/`USER.md`/`schedules`）会被遗弃在 `{app}\app.backup`。本轮既不改善也不恶化；修法属 B4，已推后。
- **运行时产物未 gitignore**：`workspace/tob/` 下的 `charts/`（252 个图表 PNG）、`channel_events/`、`.psi/` 无 gitignore 条目。虽从未被跟踪，但已被 Kanban 内部 checkpoint ref 全树暂存扫进去过 —— 证明风险真实存在。建议补 gitignore。

### 7.3 review 提出的改动（本轮已处理）

- **`_oauth_manager.py` 搬进 `feishu/`** —— 已做，`456009d3`，判据与实测见 4.1。骨架层 7 → 6 个 `.py`。
- **撤回 B3 的安装器分包** —— 已做，`b11cda40`，`Source:` 由 14 条回到 11 条。理由是单独列 `Source:` 并不改变 `SwapComponent('app')` 的整目录换新行为，等于只把清单写长 3 行、没换来保护。出厂内容与用户数据怎么分是设计题，定边界后单独开 PR，见 **9.1**。
- **抽出 `workspace/toc`** —— 已做，`80b54129`，见 2.5 与 7.1。

### 7.4 已推后（负责人决定）

- **B4** 升级保数据、**T1** 真装真升实验 —— 属打包部署，落点取决于 workspace 结构。

---

## 八、我明确没验到的

如实交代，不含糊：

| 项 | 状态 |
|---|---|
| `feishu-web` 的 `npm run dev` 独立 dev server（vite proxy 路径） | **未跑**。只验了构建产物经 gateway 静态挂载的路径 |
| 启动日志里 9 个 `ModuleNotFoundError`（`_feishu_impl` / `_assignment_tool_common` 等） | **未深查**。是工具首次加载的相对 import 失败，随后 refresh 全部 `added` 补回，与本轮改动无关 |
| `tool_registry` 是否支持多 tools 根目录 | **已查（本轮）**。`ToolRegistry.load(cls, tools_dir, session_id="")` 只收一个根，不改内核就没有共享层，见 7.1 ③ 与 9.4 |
| `workspace/toc` 接上 ToC gateway 真跑 | **未跑**。只验到内核能加载、6 个 hook 全解析、提示词组装正确，没起进程发过真消息 |
| `toc` 那 93 个工具逐个调用 | **未验**。判据是能注册、依赖闭包完整，不是运行时行为 |
| `toc` 那 102 个 skill 的正文 | **未逐篇读**。只核了不点名本包不存在的工具 |
| ToC 用户实际用不到那 36 个 ToB 工具的比例 | **未查**。只按前缀数了名字，无调用数据 |
| 139 万字符 SKILL.md 里多少真进了提示词 | **未查**。skills 按需读取，启动只加载索引 |
| Linux / macOS 上的测试表现 | **未跑**。仅 Windows 实测 |

---

## 九、需要讨论的细节

**这一章是要拿到会上定的，不是待办清单。** 共 5 项，每项都是**动手前必须先定边界**的设计题 —— 边界没定就写代码，会像 B3 那样做完再撤回。每项给的是：问题是什么、实测到的事实、可选项与代价、我的倾向。**倾向仅供参考，请负责人裁决。**

| # | 题目 | 为什么必须先讨论 | 卡住谁 |
|---|---|---|---|
| 9.1 | ToC 出厂内容与用户数据混住 | 牵动升级保数据语义，改错会丢用户数据 | B4、T1、安装器 |
| 9.2 | ToC / ToB 开发启动方式：一进程还是两进程 | 决定 `workspace/toc` 怎么接上去 | 7.1 的收尾 |
| 9.3 | `SOUL.md` / `USER.md` 归谁 | 它既是出厂模板又是用户数据，两种身份互斥 | 9.1 的前置 |
| 9.4 | `toc` 与 `tob` 的重复成本 | 内核只收一个 `tools_dir`，重复是结构性的 | 长期维护 |
| 9.5 | 桌面版要不要长期记忆 | 现有实现认飞书身份，桌面版得另设一套 | ToC 产品能力 |

### 9.1 ToC 出厂内容与用户数据混在一起

**问题**：安装器 `haitun.iss` 用一条通配 `Source: workspace\tob\*` 整目录拷贝，出厂内容（`systems/` `tools/` `skills/` 与几份提示词模板）和用户数据（`SOUL.md` `USER.md` `schedules/`）落在同一个 `{app}\app` 下，结构上分不出来。

**实测事实**：`{app}\app` 挂的是 `[Code]` 段的 `SwapComponent('app')`，升级时**整目录换新**。B3 试过把三项摘成独立 `Source:`（11 → 14 条），已撤回 —— 因为 `Flags` 不变的话，单独列出来的三项照样被换掉，**清单长了 3 行，保护一点没多**。

**可选项**：

- **A. 用户数据搬出 `{app}`**，放 `{localappdata}` 或 `{userappdata}`，出厂目录保持整目录可换新。代价：workspace 根目录被拆成两个物理位置，`agent_path` 那套推导要能同时认两个根。
- **B. 留在 `{app}` 内，靠 `Flags: onlyifdoesntexist` + 升级前备份还原**。代价：用户改过的**出厂**文件（比如自己调了 `AGENTS.md`）在升级时是保还是覆盖，得逐文件定策略，容易漏。
- **C. 出厂内容与用户数据分成两个 component**，各自换新策略。代价：Inno 的 component 语义要摸清，且不解决"用户改过出厂文件"这一类。

**倾向 A。** 理由：它是唯一让"出厂目录可以无脑整体换新"成立的选项 —— 这条性质一旦成立，升级逻辑就不必逐文件判断，B / C 都要为每个文件回答"这次要不要覆盖"。代价是路径推导要认两个根，但那是一次性的结构成本，而逐文件策略是每加一个文件都要再想一遍的长期成本。

**注意**：定下来之前 **B4（升级保数据）和 T1（真装真升实验）都动不了**，因为它们改的就是这一处。

### 9.2 ToC / ToB 开发时怎么启动：一个进程还是两个

**问题**：现在 `workspace/toc` 和 `workspace/tob` 两个包都在库里了，但开发时怎么起、起几个，没定。

**实测事实（这条我第一版写错了，已改正）**：我原本写"路径写死指向 `workspace/tob`，现在起进程只能起 ToB 那个包"。**不对。** 实际的解析链是

```
Gateway.default_agent (__init__.py:79, 真参数)
  → resolve_default_agent(explicit)         _defaults.py:99
  → resolve_agent_package(explicit, repo_candidate="workspace/tob")
      1. explicit 非空 → 就用它                ← 传 workspace/toc 这里就中了
      2. 否则 cwd/workspace/tob 是目录 → 用它    ← “写死”其实只是这一层的候选
      3. 否则 cwd 自带 tools/+skills/ → 用 cwd   ← 装机形态
      4. 否则 "" → agent ≡ workspace
```

`workspace/tob` 只是 **explicit 为空时**的仓库内候选（`_defaults.py:66`）。**所以 `toc` 今天就能选中，传 `default_agent` 指过去即可，不需要改任何代码。** 这也意味着下面选项 A 的代价比我原先估的还低 —— 不是"改一处加开关"，是"本来就支持"。

**可选项**：

- **A. 一个进程，启动时传 `default_agent` 选包**。代价：**零改动，机制已在**。缺点是同一时刻只能是一条线，两条线的路由都注册着但只有一个 workspace 在跑，容易让人误判"两条线都活着"。
- **B. 两个进程各占一个端口**，各自带一个包。代价：本地要开两个终端两个端口，前端 dev proxy 要指对；但两条线真正独立，最接近生产形态（ToC 是装机的、ToB 是服务器上的，本来就是两个进程）。
- **C. 一个进程同时挂两个 workspace**。**这条按当前内核不成立** —— 见 9.4，`ToolRegistry.load()` 只收一个 `tools_dir`，要做得改内核。

**倾向 B，但要说清它和 A 不是二选一。** A 的机制已经在了（上面那条解析链），所以现实路径是：**平时用 A**（传 `default_agent` 指向要调的那个包，零成本），**验证与联调用 B**（两个进程各占一个端口，跟生产同形）。生产上这两条线本来就是两个进程 —— ToC 是装机的、ToB 是服务器上的 —— 本地跟生产同形，本地验证才有意义。

**关于"两条线的路由都注册着"这件事，代码里已经有答案了，不必讨论**。`__init__.py:257` 和 `:264` 无条件各贴一面，`:240–244` 的注释写明了为什么：

> 骨架 + 两条产品线各自往上贴。**这一个进程仍然两条线都贴**: `psi-agent gateway` 是唯一的入口, 生产上飞书容器起的也是它 (同容器里另起一个 `psi-agent channel feishu` 连过来), 所以这里少贴哪一面都是行为回归。拆分的收益落在装配函数上 —— 谁认识什么现在写在函数签名里, 只想起一条线的进程 (ToB 容器、测试) 只贴自己那面即可。

所以"一个进程两条线路由都在"是**当前的正确行为**，不是缺陷；A6/A7 的收益是让"只贴一面"变得可能，而不是让默认进程只贴一面。**要讨论的因此只剩一句：本地开发要不要用上这个能力**（起两个各只贴一面的进程），还是就用一个全贴的进程按 `default_agent` 切包。

### 9.3 `SOUL.md` / `USER.md` 到底归谁

**问题**：这两个文件**同时**是出厂模板和用户数据。安装器要放一份初始版本进去（否则新装用户没有），可它们又会被 agent 自己改写、被用户积累内容 —— 一旦改过，就不能再当出厂文件覆盖。

**为什么单列**：9.1 的三个选项都绕不开它。A 要求把用户数据搬出 `{app}`，那"初始版本"是谁在什么时候放进去的（安装器？首次启动时生成？）；B 的 `onlyifdoesntexist` 正好为这种文件设计，但它意味着**出厂模板一旦发布就再也改不动了** —— 老用户永远拿不到新版模板。

**可选项**：**模板与实例分离** —— 出厂只发 `SOUL.template.md`，首次启动时拷成 `SOUL.md`，之后模板照常随版本更新，实例归用户。代价是多一层拷贝逻辑和一次"模板变了要不要提示用户"的产品决策。

**倾向：模板与实例分离。** 这是唯一能让"出厂模板可持续更新"和"用户内容不被覆盖"同时成立的做法，别的选项都得牺牲一头。但它引入的"模板更新了怎么告知用户"是产品问题，不是工程问题，得产品一起定。

### 9.4 `toc` 与 `tob` 的重复成本是结构性的

**问题**：`workspace/toc` 抽出来了，但它和 `tob` 之间**没有共享层**，85 个工具文件、整个 `systems/`、102 个 skill 都是**拷贝**。tob 那边改一处通用工具的 bug，toc 不会跟着变。

**实测事实**：内核的签名是

```python
ToolRegistry.load(cls, tools_dir: Path, session_id: str = "")
```

只收**一个** `tools_dir`。上一版报告把"共享层怎么做"列为未查项，本轮查清了：**不改内核就没有多根目录**，符号链接 / 构建期拷贝那些办法都是在绕这个签名。

**可选项**：

- **A. 接受重复**，靠约定和 review 保持同步。代价：通用工具的修改要手工同步两份，漏一份就是静默的行为分叉。
- **B. 改内核让 `load()` 收多个根**（`tools_dirs: Sequence[Path]`），做成 `_shared` + 各自 `tools/` 两层。代价：动内核公共 API，12 个 workspace 的加载路径都受影响；且同名工具的覆盖优先级要定义清楚。
- **C. 构建期拼包** —— 库里存 `_shared` 与差异部分，打包/启动时合成完整目录。代价：库里的目录不再是运行时的目录，调试时看到的和跑的不是一回事。

**倾向 B，但不是现在。** 理由：这是唯一从结构上消掉问题的选项（A 是靠人守纪律，C 是把问题挪到构建期）。不是现在的理由是：先让 `toc` 在 9.2 定的形态下真跑起来，攒够"哪些文件真的需要共享"的实测，再动内核签名 —— 现在动等于凭猜设计覆盖优先级。**短期先按 A 走，但要明确记下这是欠账，不是终局。**

### 9.5 桌面版要不要长期记忆

**问题**：`memory_*` 那 5 个工具（跨会话长期记忆）没进 `workspace/toc`。

**实测事实**：这条链是硬的 ——

```
memory_*  →  _fusion_memory_mcp.py:56       _load_sibling_module("_fusion_memory_membership")
          →  _fusion_memory_membership.py:14  from _feishu_impl import list_chat_members_impl
          →  _feishu_impl.py
```

「谁的身份在写这条记忆」认的是飞书 `open_id`。桌面版没有飞书身份，整条链落不了地。所以这不是"少拷了几个文件"，是**桌面版缺一套身份**。

**可选项**：

- **A. 桌面版不做长期记忆**，只有会话内上下文。代价：能力上明显弱于 ToB 版。
- **B. 设一套本地身份**（ToC 登录已有手机号 + 验证码，可以拿它当身份锚点），记忆写本地。代价：要新写一套存储与检索，不是把飞书那条链搬过来。
- **C. 桌面版记忆走云端**，用 ToC 账号体系认身份。代价：涉及数据出本机，是合规与产品决策，不只是工程。

**倾向 B。** 理由：ToC 已经有手机号 + 验证码的登录，身份锚点是现成的，不必新造；写本地也避开了 C 的合规问题。但这是个新增能力，不是本轮范围 —— 这里只负责把"为什么 toc 没有记忆工具"讲清楚，别让人以为是抽包时漏了。

---

## 附录：提交序列

```
3fa34a4c merge(gw-ws): a 线 A6/A7 增量集成, 零冲突, 骨架反向 import 由 7 归零
048efdd9 refactor(gateway): A7 两个装配函数搬进产品包, 骨架反向 import 7 行归零, 117 条路由逐条不变
3d687c37 feat(gateway): A6 ToB 前端脚手架 9 个源文件落地, 后端只多 1 个 add_static, S1-S6 全过
24c54bcf docs(tob): code-explainer 技能里 15 处过期坐标改成实测值
b63747da chore(gw-ws): 合并后补三向同步 4 处与 ruff format 2 文件
52e755d3 merge(gw-ws): a 线 A1-A5 与 b 线 B1/B2/B6/B3/B5 集成, 3 处冲突手工消解
3d102482 refactor(gateway): A5 12 个产品模块 + 2 棵 SPA 落位到 desktop/ 与 feishu/
5555475e test(session): B5 hook 契约钉成 12x6 表, 实测 11/12 只暴露 2-3 个而非 6 个
69b19cc8 refactor(installer): B3 ToC workspace 分包内/包外, 14 个 Source 逐条核对, 516 文件落点不变
3f81693f refactor(gateway): A4 17 参装配函数拆成骨架+两个贴纸, 桌面端不再建飞书管理器
8eddfe9f refactor(session): B6 4 处 __file__ 推根改为接收传入路径, 5 个 hook 调用点补上 agent 根
1c8ce23a refactor(gateway): A3 openapi 915 行按 path 分三份, 26 个 path key 并集与 schema 逐一不变
a9099a25 refactor(workspace): haitun-workspace 迁出 examples 为 workspace/tob, 60 处引用清零, 补回静默丢失的 10 个测试
a3077d7d refactor(runtime): A2 10 个 manager 1740 行移出 gateway, runtime 对 gateway 依赖归零
3839acb9 refactor(gateway): A1 切断 runtime 候选对 ToC 的 2 处依赖, 品牌字面量收拢到 1 个文件
e01a70b7 refactor(session): 11 份逐字节相同的 compact_history 收进内核, 12/12 压缩输出逐字节不变
```

设计方案原文：`docs/superpowers/specs/2026-08-26-gateway-workspace-architecture-evolution.md`

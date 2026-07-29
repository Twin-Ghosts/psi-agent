# 开发者对接：`channel_events`（按需反复注册触发事件）

> **交付物**（2026-07-29）：定事能力引入了一块**新的维护空间**。后续开发者接到「用户想要每次有 xx 就提醒/干活」类需求时，**默认动作是来这里注册事件**，而不是改 Session catalog，也不是只写 skill。  
> 设计背景见同目录 `2026-07-29-channel-events-in-agent-package.md`。

---

## 一句话

**有新的、可观测的「定事」需求 → 在 agent 包 `channel_events/<channel>/` 补一条事件定义（≈ 加 tool）→ 重启 Channel；用户订反应再写 `triggers/`（≈ 订 schedule）。**  
Session 只负责 `POST /events` 统一转发 + 按 TRIGGER 开火，**没有**业务事件 catalog 要维护。

---

## 为什么要反复注册

「每次有 xx 就提醒我」**不能**对任意 xx 永远成立：平台不推、Channel 未接通，TRIGGER 写了也不会响。

产品边界：

| 情况 | 开发者做什么 |
|------|----------------|
| xx 已有 `channel_events` | 教 agent / 用 `trigger_manage` 订 TRIGGER 即可 |
| xx 稳定、可观测、值得接通 | **在本目录加事件**（本文重点） |
| xx 是时间点 | 走 `schedules/` + `schedule_manage`，不是这里 |
| xx 不可观测 / 不值得做 | 产品上拒绝或降级，不要假装 invent |

因此：**定事能力的扩展面 = 反复往 `channel_events` 注册**，与工具、skill 同一类「按需加能力」节奏。

---

## 维护入口（在哪改）

```text
examples/haitun-workspace/channel_events/     # 或其他 Session --agent 包根
  README.md
  feishu/
    member_added/          # 示例：有人进群
      EVENT.yaml           # name / source / kind / platform_event
      map.py               # map_event(raw) -> list[envelope]
```

| 文件 | 作用 |
|------|------|
| `EVENT.yaml` | 公布稳定 `name`（TRIGGER 的 `event:`）、`platform_event`（官方推送类型）、`kind` |
| `map.py` | 平台原始载荷 → Session 信封（可一对多，如多名新人） |

框架胶水（一般不用改业务清单）：

- 加载：`src/psi_agent/channel/_event_defs.py`
- Feishu 注册转发：`src/psi_agent/channel/feishu/_agent_events.py`
- 管道：`ChannelCore.post_event` → Session `POST /events`

Feishu Channel 启动需指向同一 agent 包：`--agent` 或 `PSI_AGENT`。

---

## 加一条事件的检查清单

1. 确认有可观测信号（官方 event / 可合成条件）。  
2. 新建 `channel_events/<channel>/<slug>/`。  
3. 写 `EVENT.yaml`（`kind: platform_map` 时填 `platform_event`）。  
4. 写 `map.py`：`def map_event(raw: dict) -> list[dict]`，信封含 `source` / `event` / `payload`（建议带 `raw_event`、`idempotency_key`、`routing.open_id`）。  
5. 飞书后台订好对应事件与权限。  
6. **重启 Channel**（defs 在启动时加载）。  
7. 更新 skill 对照表（如 `skills/feishu-event-remind`）只列**已接通**名。  
8. 需要时补单测（map + load）。

挂钩（提醒文案、调哪个工具）用 `trigger_manage` / `triggers/`，**不要**在 `channel_events` 里写业务动作。

---

## 和 tool / skill / TRIGGER 的分工

| 空间 | 像什么 | 回答什么 |
|------|--------|----------|
| `tools/` | 动作原语 | 能调用什么 |
| `skills/` | 配方 | 怎么教模型用 |
| **`channel_events/`** | **信号源** | **什么事能进总线** |
| `triggers/` | 挂钩规则 | 进总线后干什么 |
| Session `/events` | 管道 | 怎么统一收、怎么发到 TRIGGER |

**Agent 运行时**可写 TRIGGER；**不应**让模型随便 invent `channel_events`（接入层需人审 + 发版/重启）。

---

## 刻意为之（勿当 bug 修）

- Session **无**业务 catalog 硬门槛；未匹配 TRIGGER 时 `matched/fired` 可为空。  
- 官方推送与（预留）合成事件都走**同一** `POST /events`。  
- 任意 NL「xx 事」永远可行 —— **不承诺**；未接通就明确说暂不支持。

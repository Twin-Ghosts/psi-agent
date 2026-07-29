# channel_events — Channel 侧事件定义（agent 包）

> **后续开发者请先读**：仓库交付文档  
> [`docs/superpowers/specs/2026-07-29-channel-events-developer-guide.md`](../../../docs/superpowers/specs/2026-07-29-channel-events-developer-guide.md)  
> （有定事类用户需求时，**默认来这里按需注册事件**，≈ 加 tool。）

事件注册在 **agent 包**，由对应 Channel 进程加载并转发到 Session `POST /events`。  
Session **不**维护业务事件 catalog。

## 布局

```text
channel_events/
  <channel>/                 # feishu | telegram | …
    <event_slug>/
      EVENT.yaml             # name / source / kind / platform_event / description
      map.py                 # kind=platform_map 必填：map_event(raw) -> list[envelope]
```

## 加事件 ≈ 加 tool

按需新增目录即可（改完**重启 Channel**）。`kind=synthetic` 预留「内部条件合成」入口，生产者逻辑后续补。

## 与 TRIGGER

- **channel_events**：什么信号能进总线（生产者 + 命名）
- **triggers/**：进总线后干什么（挂钩）

NL「有人进群提醒我」只写 TRIGGER，不 invent channel_events。

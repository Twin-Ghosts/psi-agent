"""OpenAPI spec 装配 —— 公共骨架 + 各产品线按需往上贴。

原先本文件是一个 915 行的整体 dict, 桌面端和飞书端的端点混在同一份里: 谁想只发布
自己那批端点都做不到, 只能整份发出去。现在按 path key 分成三份, 各自独立演化:

- ``_openapi_core.py``    两条线都注册的端点 (``/ais`` ``/sessions`` ``/titles`` ``/oauth`` …)
- ``_openapi_desktop.py`` ToC 专属 (``/ui/*`` ``/workspace/*``)
- ``_openapi_feishu.py``  ToB 专属 (``/feishu/*``)

``build_openapi_spec()`` 按传入开关组装; ``OPENAPI_SPEC`` 是「全都要」的那份, 与拆分前
的 path key 集合和每个 key 下的 schema 完全一致 —— 现有 ``GET /openapi.json`` 行为不变。
路由注册按消费者分开之后 (A4), 各产品线换成传对应开关即可。
"""

from __future__ import annotations

import json
from typing import Any

from psi_agent.gateway._openapi_core import CORE_PATHS, CORE_RESPONSES, CORE_SCHEMAS
from psi_agent.gateway._openapi_desktop import DESKTOP_PATHS
from psi_agent.gateway._openapi_feishu import FEISHU_PATHS, FEISHU_SCHEMAS


def build_openapi_spec(*, desktop: bool = True, feishu: bool = True) -> dict[str, Any]:
    """组装 spec。``desktop`` / ``feishu`` 决定是否贴上对应产品线的片段。"""
    paths: dict[str, Any] = dict(CORE_PATHS)
    schemas: dict[str, Any] = dict(CORE_SCHEMAS)
    if desktop:
        paths.update(DESKTOP_PATHS)
    if feishu:
        paths.update(FEISHU_PATHS)
        schemas.update(FEISHU_SCHEMAS)
    return {
        "openapi": "3.0.3",
        "info": {"title": "psi-agent Gateway", "version": "1.0.0"},
        "servers": [{"url": "/"}],
        "paths": paths,
        "components": {"schemas": schemas, "responses": dict(CORE_RESPONSES)},
    }


OPENAPI_SPEC = build_openapi_spec()


def render_openapi() -> str:
    return json.dumps(OPENAPI_SPEC)

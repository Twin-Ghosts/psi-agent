"""ToB (飞书) 专属 OpenAPI 片段 —— 飞书会话到 Session 的路由表。

``/feishu/*`` 与三个 ``FeishuRoute*`` schema 只有飞书这条线用得到; ToC 不注册。
"""

from __future__ import annotations

from typing import Any

FEISHU_PATHS: dict[str, Any] = {
    "/feishu/route": {
        "post": {
            "summary": "Route a Feishu chat to its Session (per-chat for groups, per-user for DMs)",
            "operationId": "feishuRoute",
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/FeishuRouteRequest"}}},
            },
            "responses": {
                "201": {
                    "description": "Routed",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/FeishuRoute"}}},
                },
                "400": {"$ref": "#/components/responses/Error"},
                "404": {"$ref": "#/components/responses/Error"},
                "500": {"$ref": "#/components/responses/Error"},
            },
        },
    },
    "/feishu/routes": {
        "get": {
            "summary": "List all Feishu chat -> Session routes",
            "operationId": "listFeishuRoutes",
            "responses": {
                "200": {
                    "description": "List of routes",
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/FeishuRouteEntry"},
                            }
                        }
                    },
                },
            },
        },
    },
}

FEISHU_SCHEMAS: dict[str, Any] = {
    "FeishuRouteRequest": {
        "type": "object",
        "description": ("Needs at least one routing key: open_id (DM) or chat_id with a group/topic chat_type."),
        "properties": {
            "open_id": {
                "type": "string",
                "description": "Sender's open_id. Required unless routing a group chat by chat_id.",
            },
            "chat_id": {
                "type": "string",
                "description": "Feishu chat id. With chat_type group/topic, the whole chat shares one Session.",
            },
            "chat_type": {
                "type": "string",
                "description": "p2p | group | topic. group/topic routes by chat_id, anything else by open_id.",
            },
            "ai_id": {
                "type": "string",
                "description": "Optional, overrides Gateway --feishu-ai-id",
            },
            "workspace": {
                "type": "string",
                "description": (
                    "Optional, defaults to <feishu_workspace_root>/<open_id> (or /chat-<chat_id> for group chats)"
                ),
            },
        },
    },
    "FeishuRoute": {
        "type": "object",
        "properties": {
            "open_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "session_id": {"type": "string"},
            "channel_socket": {"type": "string"},
        },
    },
    "FeishuRouteEntry": {
        "type": "object",
        "description": "One route. Group entries carry chat_id with an empty open_id; DMs the reverse.",
        "properties": {
            "open_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "session_id": {"type": "string"},
        },
    },
}

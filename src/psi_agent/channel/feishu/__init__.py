"""Feishu bot channel."""

from __future__ import annotations

import os
from dataclasses import dataclass

from loguru import logger

from psi_agent._logging import setup_logging

from .client import run_feishu


@dataclass
class ChannelFeishu:
    """Feishu bot channel."""

    session_socket: str
    """Session socket path (Unix/TCP/Named Pipe)。无 gateway_url 时全体共用; Gateway 模式不作兜底。"""

    gateway_url: str | None = None
    """Gateway REST 基址 (如 ``http://127.0.0.1:8080``), 面向**动态任意用户**场景。

    设置后, channel 按发送者 open_id 经 Gateway ``POST /feishu/route`` 幂等拿到独立 Session 的
    ``channel_socket`` 和实际用户数据 ``workspace``。路由/spawn/workspace 决策全在 Gateway
    (``FeishuManager`` / ``SessionManager``), channel 只消费并缓存完整路由。附件写入对应 workspace,
    Agent 回传文件也只能来自该 workspace。Gateway 不可达、缺 open_id 或响应无效时拒绝消息,
    绝不回退共享 ``session_socket``。None(默认)=不启用, 全体共用 ``session_socket`` 和旧下载目录。"""

    app_id: str = ""
    """Feishu app ID (CLI arg > PSI_FEISHU_APP_ID env)."""

    app_secret: str = ""
    """Feishu app secret (CLI arg > PSI_FEISHU_APP_SECRET env)."""

    interval: float = 1.0
    """SSE buffer merge window."""

    allowed_user_ids: list[str] | None = None
    """Whitelist of open_id/user_id. None = allow all."""

    require_mention: bool = True
    """Group chats: only reply when the bot is @-mentioned; DMs unaffected. False replies to every group message."""

    respond_to_mention_all: bool = False
    """Whether to treat @all as a valid mention (default False, so @all does not trigger the bot)."""

    respond_to_comments: bool = True
    """Doc comments: reply when the bot is @-mentioned in a comment. False disables comment subscription."""

    verbose: bool = False
    """Enable DEBUG-level logging."""

    async def run(self) -> None:
        setup_logging(verbose=self.verbose)
        app_id = self.app_id or os.environ.get("PSI_FEISHU_APP_ID", "")
        app_secret = self.app_secret or os.environ.get("PSI_FEISHU_APP_SECRET", "")
        if not app_id:
            raise ValueError("No Feishu app_id. Set --app-id or PSI_FEISHU_APP_ID.")
        if not app_secret:
            raise ValueError("No Feishu app_secret. Set --app-secret or PSI_FEISHU_APP_SECRET.")

        logger.info(f"Starting Feishu bot, connecting to {self.session_socket}")
        await run_feishu(
            session_socket=self.session_socket,
            app_id=app_id,
            app_secret=app_secret,
            interval=self.interval,
            allowed_user_ids=self.allowed_user_ids,
            require_mention=self.require_mention,
            respond_to_mention_all=self.respond_to_mention_all,
            respond_to_comments=self.respond_to_comments,
            gateway_url=self.gateway_url,
        )

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio
from loguru import logger

from psi_agent._appdata import resolve_appdata_root
from psi_agent._logging import setup_logging
from psi_agent.session.agent import SessionAgent
from psi_agent.session.schedule_registry import ACTIVATE_ALL
from psi_agent.session.server import serve_session


@dataclass
class Session:
    """CLI entry point and orchestrator for the Session layer."""

    ai_socket: str
    channel_socket: str
    workspace: str = ""
    """User / legacy single-root directory. Empty → ``Path.cwd()``."""

    agent: str = ""
    """Agent package directory (tools / system).

    Empty → use *workspace* (backward compatible single-root behaviour).
    """

    appdata: str = ""
    """AppData memory root for history JSONL (Step 4C).

    Empty → ``PSI_APPDATA`` / ``platformdirs`` via ``resolve_appdata_root``.
    """

    active_schedules: str = ""
    """触发哪些定时任务, 逗号分隔; ``*`` = 全部。默认一条都不触发。"""

    inactive_schedules: str = ""
    """从上面排除掉的定时任务名, 逗号分隔; 优先于白名单。"""

    max_tool_rounds: int = 128
    session_id: str | None = None
    verbose: bool = False

    async def run(self) -> None:
        setup_logging(verbose=self.verbose)

        workspace_path = Path.cwd() if self.workspace == "" else Path(str(await anyio.Path(self.workspace).resolve()))
        agent_path = workspace_path if self.agent == "" else Path(str(await anyio.Path(self.agent).resolve()))
        appdata_root = self.appdata.strip()
        if not appdata_root:
            appdata_root = await resolve_appdata_root()
        active = self._name_set(self.active_schedules)
        inactive = self._name_set(self.inactive_schedules)

        logger.info(f"Loading workspace from {workspace_path}")
        if agent_path != workspace_path:
            logger.info(f"Loading agent package from {agent_path}")
        logger.info(f"AppData history root: {appdata_root}")
        if active:
            names = "all" if ACTIVATE_ALL in active else sorted(active)
            logger.info(f"Active schedules under {workspace_path / 'schedules'}: {names}")
            if inactive:
                logger.info(f"Excluded schedules: {sorted(inactive)}")

        agent = await SessionAgent.create(
            ai_socket=self.ai_socket,
            workspace_path=workspace_path,
            agent_path=agent_path,
            appdata_root=appdata_root,
            max_tool_rounds=self.max_tool_rounds,
            session_id=self.session_id,
            active_schedules=active,
            inactive_schedules=inactive,
        )

        async with anyio.create_task_group() as task_group:
            agent.start_all(task_group)
            task_group.start_soon(partial(serve_session, channel_socket=self.channel_socket, agent=agent))

    @staticmethod
    def _name_set(raw: str) -> set[str]:
        """逗号分隔的名单字符串 → 名字集合 (去空、去首尾空白)。"""
        names = {part.strip() for part in raw.split(",")}
        names.discard("")
        return names

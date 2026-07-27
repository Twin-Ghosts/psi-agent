from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio
from loguru import logger

from psi_agent._appdata import resolve_appdata_root
from psi_agent._logging import setup_logging
from psi_agent.session.agent import SessionAgent
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

    scheduler: bool = False
    """本 Session 是否是 *workspace* 的调度 Session (刻意为之)。

    ``True`` 才从 ``{workspace}/schedules`` 加载并触发定时任务; 普通用户
    Session 一律 ``False`` -> ``ScheduleRegistry`` 为空、不起 runner。

    为什么: 调度归属 workspace, 不归属 session。飞书按 open_id 给每个用户
    spawn 独立 Session, 若每个 Session 都触发, 一条定时任务会被在线用户数
    乘一遍。Gateway 侧 ``SchedulerManager`` 保证每个 workspace 只有一个
    ``scheduler=True`` 的 Session, 于是「重复触发」在构造期就不存在, 无需
    运行时抢锁。

    单进程 CLI (``psi-agent session``) 默认 ``False``; 需要跑定时任务时显式
    ``--scheduler``。``psi-agent run`` 的 session 配置项同理。
    """

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

        logger.info(f"Loading workspace from {workspace_path}")
        if agent_path != workspace_path:
            logger.info(f"Loading agent package from {agent_path}")
        logger.info(f"AppData history root: {appdata_root}")
        if self.scheduler:
            logger.info(f"Scheduler session — owns schedules under {workspace_path / 'schedules'}")

        agent = await SessionAgent.create(
            ai_socket=self.ai_socket,
            workspace_path=workspace_path,
            agent_path=agent_path,
            appdata_root=appdata_root,
            max_tool_rounds=self.max_tool_rounds,
            session_id=self.session_id,
            scheduler=self.scheduler,
        )

        async with anyio.create_task_group() as task_group:
            agent.start_all(task_group)
            task_group.start_soon(partial(serve_session, channel_socket=self.channel_socket, agent=agent))

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

    scheduler: bool = False
    """本 Session 是否触发 *workspace* 下的**全部**定时任务 (等价 ``--schedules '*'``)。

    便捷开关: 展开成 ``{ACTIVATE_ALL}`` 交给 ``SessionAgent``。要只触发其中几条,
    用 ``schedules`` 逐条指定。
    """

    schedules: str = ""
    """本 Session 激活的定时任务名, 逗号分隔; ``*`` 表示全部。

    **激活是 (session x schedule) 的属性, 不是 session 的 (刻意为之)**: 每个
    Session 都加载 ``{workspace}/schedules`` 的全部条目 (可读、可 refresh), 但
    只为激活的那些起 runner。于是同一 workspace 的不同 Session 可以各自触发不同
    子集 —— 一个整体布尔只能表达「全触发 / 全不触发」, 表达不了「A 条归调度
    Session、B 条归某个用户会话」。

    为什么默认空 (一条都不激活): 飞书按 open_id 给每个用户 spawn 独立 Session,
    一条 schedule 必须**恰好**被一个 Session 激活, 否则提醒会被在线会话数乘一遍。
    Gateway 侧 ``SchedulerManager`` 为每个 workspace 维护唯一一个全量激活的调度
    Session; 单进程 CLI 需要跑定时任务时显式 ``--scheduler`` 或
    ``--schedules 名字1,名字2``。
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
        active_schedules = self._active_schedules()

        logger.info(f"Loading workspace from {workspace_path}")
        if agent_path != workspace_path:
            logger.info(f"Loading agent package from {agent_path}")
        logger.info(f"AppData history root: {appdata_root}")
        if active_schedules:
            names = "all" if ACTIVATE_ALL in active_schedules else sorted(active_schedules)
            logger.info(f"Active schedules under {workspace_path / 'schedules'}: {names}")

        agent = await SessionAgent.create(
            ai_socket=self.ai_socket,
            workspace_path=workspace_path,
            agent_path=agent_path,
            appdata_root=appdata_root,
            max_tool_rounds=self.max_tool_rounds,
            session_id=self.session_id,
            active_schedules=active_schedules,
        )

        async with anyio.create_task_group() as task_group:
            agent.start_all(task_group)
            task_group.start_soon(partial(serve_session, channel_socket=self.channel_socket, agent=agent))

    def _active_schedules(self) -> set[str]:
        """把 ``--scheduler`` / ``--schedules`` 归一成激活名单。

        ``--scheduler`` 是 ``--schedules '*'`` 的便捷写法, 两者并存时取并集。
        """
        names = {part.strip() for part in self.schedules.split(",")}
        names.discard("")
        if self.scheduler:
            names.add(ACTIVATE_ALL)
        return names

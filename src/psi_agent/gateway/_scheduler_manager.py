"""SchedulerManager — 每个 workspace 恰好一个调度 Session。

**为什么 (刻意为之)**

定时任务归属 **workspace**, 不归属 session。飞书 channel 按 open_id 给每个用户
spawn 一个独立 Session (``_feishu_manager.py``), SPA 也可能对同一 workspace 开多
个会话; 若每个 Session 都加载并触发 ``{workspace}/schedules``, 一条定时提醒就会
被在线会话数乘一遍。

本模块让调度**只住在专用 Session 里**: ``ensure(workspace)`` 幂等地为一个
workspace 拿到/创建唯一的 ``scheduler=True`` Session。于是「重复触发」在构造期就
不存在, 不需要运行时抢锁, 也没有「持有者退出后谁接管」的选主问题。

**按需创建**: 只有 workspace 真的存在非空 ``schedules/`` 时才 spawn, 免得 N 个
从不用定时任务的飞书用户各挂一个空调度 Session (每个都要付 tools 加载成本)。
调用方在建 workspace / 路由用户 / 恢复 state 后调 ``ensure``; 用户新建第一个定时
任务后, 下一次 ``ensure`` 会把它拉起来。

**对 SPA / state 完全隐藏**: ``SessionInfo.scheduler=True`` 使其从
``SessionManager.list_all()`` 与 ``state/latest.json`` 中排除。session id 由
workspace 路径确定性派生 (``_workspace_key`` 归一后取 sha256 前 16 位), 因此重启后
``ensure`` 会重建同名 Session, 无需持久化。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

import anyio
from loguru import logger

from psi_agent.gateway._session_manager import SessionManager


@dataclass
class SchedulerManager:
    """按 workspace 去重地持有调度 Session。

    ``_ai_id`` 是调度 Session 挂载的缺省 AI 实例; 为空时 ``ensure`` 直接跳过
    (记 warning) —— 没有 AI 后端时 ``fire=prompt`` 无法工作, 但 spawn 一个连不上
    上游的 Session 更糟。
    """

    _sm: SessionManager
    _ai_id: str = ""
    _routes: dict[str, str] = field(default_factory=dict)
    _lock: anyio.Lock = field(default_factory=anyio.Lock)

    @staticmethod
    async def _workspace_key(workspace: str) -> str:
        """规范化 workspace 路径 - 大小写 / 斜杠差异不该产出两个调度 Session。

        用 ``anyio.Path.resolve()`` 而非 ``os.path.realpath`` —— 后者在 async
        上下文里是同步 IO (会 stat 磁盘), 违反「一切异步」约定。``normcase``
        是纯字符串运算, 无 IO, 可直接用。
        """
        resolved = str(await anyio.Path(workspace).resolve())
        return os.path.normcase(resolved)

    @staticmethod
    def _session_id_from_key(key: str) -> str:
        """由**已规范化**的 workspace key 派生确定性 session id。

        加 ``scheduler-`` 前缀与用户会话 / 飞书会话的命名空间隔离; 用 hash 而非
        路径本身: 路径含分隔符 / 中文 / 超长, 不适合做 socket 文件名。
        """
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"scheduler-{digest}"

    async def ensure(self, workspace: str, *, ai_id: str = "", agent: str = "") -> str:
        """确保 *workspace* 有且仅有一个调度 Session; 返回其 session id (跳过时 ``""``)。

        幂等: 已存在则直接返回。``schedules/`` 不存在或为空时**不** spawn (按需)。
        任何异常都只记 warning 并返回 ``""`` —— 调度起不来不该拖垮建会话 / 收消息
        的主链路。
        """
        if not workspace.strip():
            return ""
        try:
            return await self._do_ensure(workspace, ai_id=ai_id, agent=agent)
        except Exception as e:
            logger.warning(f"SchedulerManager: failed to ensure scheduler for {workspace!r}: {e!r}")
            return ""

    async def _do_ensure(self, workspace: str, *, ai_id: str, agent: str) -> str:
        key = await self._workspace_key(workspace)
        sid = self._session_id_from_key(key)
        async with self._lock:
            logger.debug(f"SchedulerManager: acquired lock for ensure {workspace!r}")
            cached = self._routes.get(key)
            if cached is not None and self._sm.has(cached):
                return cached
            # 路由表未命中但 Session 已在 (重启后 ensure 重建同名, 或并发抢先) → adopt。
            if self._sm.has(sid):
                self._routes[key] = sid
                logger.debug(f"SchedulerManager: adopted existing scheduler session {sid!r}")
                return sid

            if not await self._has_schedules(workspace):
                logger.debug(f"SchedulerManager: no schedules under {workspace!r}; not spawning")
                return ""

            resolved_ai = ai_id or self._ai_id
            if not resolved_ai:
                logger.warning(
                    f"SchedulerManager: {workspace!r} has schedules but no ai_id is configured; "
                    "scheduler session not started"
                )
                return ""

            try:
                await self._sm.create(ai_id=resolved_ai, id=sid, workspace=workspace, agent=agent, scheduler=True)
            except ValueError as e:
                # 并发竞态: 另一路已建同名 (锁内理论不会, 防御性兜底)。
                if "already exists" not in str(e):
                    raise
                logger.debug(f"SchedulerManager: scheduler session {sid!r} raced")

            self._routes[key] = sid
            logger.info(f"SchedulerManager: scheduler session {sid!r} owns schedules of {workspace!r}")
            return sid

    @staticmethod
    async def _has_schedules(workspace: str) -> bool:
        """workspace 下是否有至少一个 ``schedules/*/TASK.md`` (按需 spawn 的判据)。"""
        sched_dir = anyio.Path(workspace) / "schedules"
        if not await sched_dir.is_dir():
            return False
        async for task_dir in sched_dir.iterdir():
            if await task_dir.is_dir() and await (task_dir / "TASK.md").exists():
                return True
        return False

    async def session_id_for(self, workspace: str) -> str:
        """已知的调度 session id, 未创建时返回 ``""`` (诊断 / 测试用)。"""
        return self._routes.get(await self._workspace_key(workspace), "")

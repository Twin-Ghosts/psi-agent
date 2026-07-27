"""进程内 schedules 目录租约 - 保证「一个 workspace 只触发一次」。

**为什么需要它 (刻意为之)**

Gateway 把多个 Session 跑在同一个进程里; 飞书 channel 更是按 open_id 给每个用户
spawn 一个独立 Session (见 ``gateway/_feishu_manager.py``)。而 ``ScheduleRegistry``
过去无条件为自己加载到的每条 schedule 起一个 runner, 于是同一份 ``TASK.md`` 会被
N 个 Session 各跑一遍: 飞书上 N 个用户在线 -> 一条定时提醒被推送 N 次。

调度的正确归属是 **workspace (schedules 目录)**, 不是 session。本模块用
``{schedules 目录 -> 持有者 session}`` 的进程内注册表实现这一点: 同一个目录只有
第一个 ``acquire`` 的 Session 成为持有者并起 runner, 其余 Session 照旧加载
schedules (``schedules`` 属性 / SPA 展示不受影响), 但不触发。

刻意**不**做的事:
- 不做跨进程锁 (文件锁 / DB)。psi-agent 的部署形态是单 Gateway 进程持有全部
  Session; 跨机去重不是本层职责, 加文件锁只会带来陈旧锁文件的运维负担。
- 不做「持有者退出后重新选主」的抢占式选举。``release`` 已覆盖 Session 正常
  结束与崩溃 (``Session.run`` 退出即释放), 后来者下一次 ``acquire`` 自然接管。
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

# schedules 目录 (规范化后的字符串) -> 持有者标识 (session id 或对象身份)。
_holders: dict[str, str] = {}


def _key(schedules_dir: Path | str) -> str:
    """规范化目录 key - 大小写 / 斜杠 / 相对路径不同但同一目录必须撞同一个 key。

    Windows 上 ``C:\\ws\\schedules`` 与 ``c:/ws/schedules`` 是同一目录,
    ``os.path.normcase`` + ``realpath`` 把它们归一, 避免租约被绕过。
    """
    return os.path.normcase(os.path.realpath(str(schedules_dir)))


def acquire(schedules_dir: Path | str, holder: str) -> bool:
    """尝试为 *schedules_dir* 取得触发租约。

    返回 ``True`` 表示 *holder* 成为该目录唯一的调度触发方; ``False`` 表示已有
    其他持有者, 调用方应加载 schedules 但**不要**起 runner。同一 *holder* 重复
    acquire 同一目录是幂等的 (返回 ``True``), 便于重连 / 重启 runner。
    """
    key = _key(schedules_dir)
    current = _holders.get(key)
    if current is None:
        _holders[key] = holder
        logger.info(f"Schedule lease acquired: {schedules_dir!r} -> holder {holder!r}")
        return True
    if current == holder:
        return True
    logger.info(
        f"Schedule lease already held for {schedules_dir!r} by {current!r}; "
        f"{holder!r} loads schedules without firing them"
    )
    return False


def release(schedules_dir: Path | str, holder: str) -> None:
    """释放租约 (仅当 *holder* 确实是当前持有者) - 幂等, 可安全重复调用。"""
    key = _key(schedules_dir)
    if _holders.get(key) == holder:
        del _holders[key]
        logger.info(f"Schedule lease released: {schedules_dir!r} (was {holder!r})")


def holder_of(schedules_dir: Path | str) -> str:
    """当前持有者标识, 无人持有时返回 ``\"\"`` (测试 / 诊断用)。"""
    return _holders.get(_key(schedules_dir), "")


def reset() -> None:
    """清空全部租约 - **仅测试用**, 生产代码禁止调用。"""
    _holders.clear()

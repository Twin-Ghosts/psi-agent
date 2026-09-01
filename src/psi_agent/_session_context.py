"""当前会话 id 的 ContextVar —— 唯一定义处, 给日志与工具层共用。

**为什么住在顶层而不在 ``session/`` 里**: ``_logging.py`` 要把会话 id 拼进日志格式,
于是它必须能 import 到这个 ContextVar。而 ``session/runtime_context.py`` 虽然自身零项目
内依赖, 导入它却会先执行 ``psi_agent/session/__init__.py``, 那里第一件事就是
``from psi_agent._logging import setup_logging`` —— 于是 ``_logging`` 在自己还没初始化完
时被回头 import, 直接 ImportError。这不是「加个 try 就好」的问题, 是依赖方向反了。

所以把 ContextVar 下沉到与 ``_appdata`` / ``_sockets`` / ``_feishu_routing`` 同级的叶子
模块 (零项目内依赖), 让 ``_logging`` 直接 import; ``session/runtime_context`` 原样再导出,
对外 API 一个字没变。**定义仍然只有一份** —— 换成「让 runtime_context 在导入时向
_logging 注册一个 getter」也能绕开环, 但那种接线一旦没执行到就静默退化成没有会话 id,
恰好是本仓反复踩的那类坑 (关键判据放在了线上不生效的地方)。

写入方与可读范围的约定见 ``session/AGENTS.md``: 对话回合与事件派发由
``SessionAgent`` 经 ``runtime_scope`` 绑定; ``ai/server.py`` 额外**只为日志归属**绑定一次
(它是独立的 aiohttp handler, 跨 socket 拿不到调用方的 ContextVar, 只能从
``routing.session_id`` 重新绑)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_session_id: ContextVar[str] = ContextVar("psi_session_id", default="")

# 日志格式里会话 id 缺失时占的位。刻意不是空串: 探针脚本按 ``|`` 切列, 空列会让
# 「没绑会话」与「这行没有会话列」两件事长得一样。
LOG_SESSION_PLACEHOLDER = "-"


def get_session_id() -> str:
    """返回当前异步上下文绑定的会话 id, 未绑定则为 ``""``。"""
    return _session_id.get()


def set_session_id(session_id: str) -> Token[str]:
    """为当前上下文绑定 *session_id*, 返回复位 token。"""
    return _session_id.set(session_id.strip())


def reset_session_id(token: Token[str]) -> None:
    """恢复上一个 ContextVar 值。"""
    _session_id.reset(token)


@contextmanager
def session_id_scope(session_id: str) -> Iterator[None]:
    """在 ``with`` 块内绑定 *session_id* (跨 yield 也有效)。"""
    token = set_session_id(session_id)
    try:
        yield
    finally:
        reset_session_id(token)


def log_session_field() -> str:
    """会话 id 的日志列取值 —— 未绑定时给占位符而不是空串。"""
    return _session_id.get() or LOG_SESSION_PLACEHOLDER


__all__ = [
    "LOG_SESSION_PLACEHOLDER",
    "get_session_id",
    "log_session_field",
    "reset_session_id",
    "session_id_scope",
    "set_session_id",
]

from __future__ import annotations

import glob
import os
import sys
from typing import TYPE_CHECKING

import platformdirs
from loguru import logger

if TYPE_CHECKING:
    # Stub-only in loguru: absent from the runtime module, so it must not be
    # imported unconditionally.
    from loguru import Record

from psi_agent._session_context import log_session_field

_handler_id: int | None = None
_file_handler_id: int | None = None

_DEBUG_MODULES_ENV = "PSI_DEBUG_MODULES"
_DEBUG_LOG_PATH_ENV = "PSI_DEBUG_LOG_PATH"
_APPDATA_ENV = "PSI_APPDATA"

# Keep in sync with ``_appdata._APPDATA_APPNAME`` — deliberately duplicated
# rather than imported: ``_appdata`` is async and this module sits at the bottom
# of the dependency graph with zero in-project imports.
_APPDATA_APPNAME = "Haitun"

_LOG_DIRNAME = "logs"
# One file per process, deliberately: the PID goes in the name. A container
# often runs several psi-agent processes (production's ``launch-gateway.sh``
# starts ``gateway`` and ``channel feishu`` side by side, and the two modules
# worth observing live in different ones). Pointing them at one path loses
# lines: ``enqueue=True`` only serialises writers inside a single process, and
# after rotation the losers keep writing to a renamed inode. Measured 586 of
# 600 lines surviving with two processes and no rotation at all.
#
# The PID is interpolated by us, not by loguru: its file sink only substitutes
# ``{time}`` (see ``loguru._file_sink.FileSink._create_path``), so a literal
# ``{process}`` in the path raises ``KeyError`` on the first write.
_LOG_FILENAME_TEMPLATE = "psi-debug-{pid}.log"

# docker's json-file driver has no rotation in this deployment, so the stderr
# sink must never carry DEBUG. The file sink rotates itself: 20 MB per file, 10
# files kept, gzipped — a 200 MB ceiling *per process*, since each process gets
# its own file (see ``_LOG_FILENAME_TEMPLATE``).
_ROTATION = "20 MB"
_RETENTION = 10
_COMPRESSION = "gz"

_SESSION_EXTRA_KEY = "psi_session"

# 文件 sink 对**未列进 PSI_DEBUG_MODULES 的模块**的级别下限。见 ``_setup_debug_file_sink``
# 里那段注释: 这里曾是 ``False`` (整段关掉), 代价是开着定向 DEBUG 排查时, 别的模块的告警
# 一条都不落盘。
_UNLISTED_FLOOR = "WARNING"

# Session id column. Deliberately **inside the shared ``_FORMAT``**, i.e. it goes
# to stderr as well as the DEBUG file: production runs INFO on stderr, and one
# container multiplexes ~67 Sessions into that single stream. Without this column
# a slow turn cannot be attributed to a person at all — during the 2026-08-31
# latency probe 34 of 123 measured turns had to be discarded for exactly that
# reason. Putting it anywhere that INFO does not reach would be the same mistake
# the raw-SSE logs already made (DEBUG-only, therefore absent when it mattered).
#
# ``{extra[...]}`` rather than a direct ContextVar read: loguru formats records on
# the sink's side — with ``enqueue=True`` that is a different *process*, where the
# ContextVar is unset. The patcher below snapshots the value at call time.
_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<magenta>{extra[" + _SESSION_EXTRA_KEY + "]}</magenta> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def _patch_session(record: Record) -> None:
    """Snapshot the current session id into ``record["extra"]``.

    Runs for every record via ``logger.configure(patcher=...)``, in the *calling*
    context — which is the only place the ContextVar is readable.

    Always writes the key, never conditionally: ``_FORMAT`` names
    ``extra[psi_session]`` unconditionally, and a missing key makes loguru raise
    inside the sink and swallow the line.
    """
    record["extra"][_SESSION_EXTRA_KEY] = log_session_field()


def install_session_patcher() -> None:
    """Make ``extra[psi_session]`` present on every record, from anywhere.

    Called at **import time** of this module (below) as well as from
    ``setup_logging``, and idempotent — ``logger.configure(patcher=...)`` replaces
    the core patcher rather than stacking.

    Import time is deliberate, despite being a side effect: ``_FORMAT`` names the
    key unconditionally, and if a record ever reaches a sink without it, loguru
    raises inside the sink and **drops the line**. Any module logging before its
    component got around to ``setup_logging`` would hit that. Binding it to the
    import of the module that owns ``_FORMAT`` means the key and its provider can
    never be installed separately.

    ``logger.patch`` (a *bound*-logger patcher) still composes on top of this one,
    so the tests that rewrite ``record["name"]`` keep working.
    """
    logger.configure(patcher=_patch_session)


def debug_modules() -> list[str]:
    """Module prefixes routed to the DEBUG file sink, from ``PSI_DEBUG_MODULES``.

    Comma- or semicolon-separated, e.g.
    ``psi_agent.ai.server,psi_agent.channel._core``. Empty when unset — and an
    empty list means the file sink is never installed at all.
    """
    raw = os.environ.get(_DEBUG_MODULES_ENV, "")
    out: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        name = piece.strip()
        if name and name not in out:
            out.append(name)
    return out


def debug_log_path() -> str:
    """Where the DEBUG file sink writes.

    Priority: ``PSI_DEBUG_LOG_PATH`` (explicit file path) → ``PSI_APPDATA``
    ``/logs/psi-debug-<pid>.log`` → ``platformdirs`` user-data dir.

    The returned path always names one process: the derived forms end in the
    caller's PID, and an explicit ``PSI_DEBUG_LOG_PATH`` may contain a ``{pid}``
    placeholder, which is substituted here. Without it the override is used
    verbatim — fine for a single writer, lossy for several (see
    ``_LOG_FILENAME_TEMPLATE``).

    The explicit override exists because ``PSI_APPDATA`` may point inside the
    container layer, where a rotating log eats host disk. It also compensates
    for what this module cannot see: ``setup_logging()`` runs before — and
    synchronously, so it cannot await — ``_appdata.resolve_appdata_root()``,
    which means the ``--appdata`` CLI argument is invisible here. Only the env
    var is.
    """
    pid = os.getpid()
    explicit = os.environ.get(_DEBUG_LOG_PATH_ENV, "").strip()
    if explicit:
        # ``replace`` rather than ``format``: an operator-supplied path is not a
        # format string, and a stray brace in it must not raise.
        return explicit.replace("{pid}", str(pid))
    appdata = os.environ.get(_APPDATA_ENV, "").strip()
    root = appdata or platformdirs.user_data_dir(appname=_APPDATA_APPNAME, appauthor=False)
    return os.path.join(root, _LOG_DIRNAME, _LOG_FILENAME_TEMPLATE.format(pid=pid))


def setup_logging(*, verbose: bool = False) -> int:
    """Install the loguru stderr handler once and return its id.

    Deliberately one-shot: guarded by the module-global ``_handler_id``, the
    first call installs the handler and every subsequent call is a no-op that
    returns the existing id **without** re-applying ``verbose``. Whoever calls
    first wins the level. In ``psi-agent run`` (batch mode) ``Run.run()`` calls
    ``setup_logging(verbose=False)`` first, so batch mode pins **INFO** and each
    component's own ``verbose`` field is ignored — that is the only reason
    production has no DEBUG anywhere. (This used to be ``verbose=True``; #625
    flipped it, and both this docstring and AGENTS.md kept claiming batch mode
    was DEBUG until 2026-08-25.) Running a component standalone lets its own
    ``verbose`` decide.

    Independently of ``verbose``, ``PSI_DEBUG_MODULES`` adds a **second** sink:
    a rotating file that takes DEBUG from the listed modules only. It is the
    supported way to observe raw upstream SSE without turning the whole process
    to DEBUG — the stderr level, and therefore ``docker logs`` volume, is
    untouched. Unset the variable and no file sink is created at all.
    """
    global _handler_id
    # Re-asserted here, not only at import: a caller that ran
    # ``logger.configure()`` for its own reasons would have replaced the patcher,
    # and ``_FORMAT`` cannot survive without it.
    install_session_patcher()
    if _handler_id is None:
        # Must precede the file sink: bare ``remove()`` drops *every* handler,
        # so installing the file sink first would silently delete it while
        # leaving its guard set — i.e. no DEBUG file for the process lifetime.
        logger.remove()
        level = "DEBUG" if verbose else "INFO"
        _handler_id = logger.add(sys.stderr, level=level, format=_FORMAT)
    _setup_debug_file_sink()
    return _handler_id


def _setup_debug_file_sink() -> int | None:
    """Install the rotating DEBUG file sink if ``PSI_DEBUG_MODULES`` is set.

    One-shot under its own guard, separate from ``_handler_id`` on purpose: the
    two sinks answer to different inputs (caller's ``verbose`` vs the process
    environment). Sharing a guard would let whichever component calls first
    decide whether the *file* sink exists.

    ``filter`` is loguru's native per-module level map, so the whitelist needs
    no matching logic of ours.
    """
    global _file_handler_id
    if _file_handler_id is not None:
        return _file_handler_id
    modules = debug_modules()
    if not modules:
        return None
    _file_handler_id = logger.add(
        debug_log_path(),
        level="DEBUG",
        format=_FORMAT,
        # 根规则 ``""`` 管的是**未列出**的模块。刻意是 ``_UNLISTED_FLOOR`` 而不是
        # ``False``: ``False`` 把未列模块**整段**关掉(不是只关 DEBUG), 于是这个文件
        # 里除白名单外一个字都没有。实测代价 —— 生产 14.5 万行里 ``FeishuManager``
        # 零命中, 「adopt 了哪个 session、workspace 对不对」在线上完全查不到, 而那正是
        # 一次排查要的东西。WARNING 起的记录是**告警**, 量小且恰恰是出事时要看的;
        # DEBUG/INFO 仍按白名单收, ``PSI_DEBUG_MODULES`` 控量的语义一字不改。
        filter={"": _UNLISTED_FLOOR, **dict.fromkeys(modules, "DEBUG")},
        rotation=_ROTATION,
        retention=_RETENTION,
        compression=_COMPRESSION,
        enqueue=True,
        encoding="utf-8",
        # ** 不给「一行都没写」的进程建文件 **: 默认 ``delay=False`` 让 ``logger.add``
        # 当场 open() 出文件。而 ``PSI_DEBUG_MODULES`` 是个白名单, 容器里每个
        # psi-agent 进程都装这个 sink, 绝大多数进程一辈子不产生一条命中白名单的
        # DEBUG —— 于是每个 PID 留一个 0 字节文件。实测 ``.psi/appdata/logs/`` 下
        # 攒了 824 个空文件, 目录 ls 都不可用, 真正有内容的那几份反而找不着。
        #
        # ``delay=True`` 把 open() 推到第一条记录, 治的是源头 (不写就不存在), 而不是
        # 事后清理。清现存的那批见 ``prune_empty_debug_logs``。
        delay=True,
    )
    return _file_handler_id


def prune_empty_debug_logs(*, dry_run: bool = False) -> int:
    """删掉日志目录里 0 字节的 ``psi-debug-*.log``, 返回个数 (``dry_run`` 时只数不删)。

    只治**存量**: 新进程由 ``delay=True`` 保证不再产生空文件 (见上)。刻意做成显式
    函数而非 ``setup_logging`` 里的自动动作 —— 那会让「起个进程」带上删别人文件的副
    作用, 而多进程容器里另一个进程可能刚 open() 完还没写第一行, 恰好是 0 字节。

    判据是 ``st_size == 0`` 而非修改时间: 有内容的文件绝不会被碰。压缩产物
    (``.log.gz``) 不在匹配范围内, 空的 gz 本身是有效的 rotation 结果。

    异常一律吞掉并计入跳过: 这是清理动作, 抢不到锁 (Windows 上文件被别的进程
    open 着) 不该把调用方带崩。
    """
    path = debug_log_path()
    directory = os.path.dirname(path) or "."
    matched = 0
    for candidate in glob.glob(os.path.join(directory, "psi-debug-*.log")):
        try:
            if os.path.getsize(candidate) != 0:
                continue
            matched += 1
            if not dry_run:
                os.remove(candidate)
        except OSError:
            continue
    return matched


# Import-time, for the reason spelled out in ``install_session_patcher``: the
# format string and the patcher that feeds it must not be installable separately.
install_session_patcher()

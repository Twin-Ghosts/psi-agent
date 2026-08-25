from __future__ import annotations

import os
import sys

import platformdirs
from loguru import logger

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
_LOG_FILENAME = "psi-debug.log"

# docker's json-file driver has no rotation in this deployment, so the stderr
# sink must never carry DEBUG. The file sink rotates itself: 20 MB per file, 10
# files kept, gzipped — a 200 MB ceiling per container.
_ROTATION = "20 MB"
_RETENTION = 10
_COMPRESSION = "gz"

_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


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
    ``/logs/psi-debug.log`` → ``platformdirs`` user-data dir.

    The explicit override exists because ``PSI_APPDATA`` may point inside the
    container layer, where a rotating log eats host disk. It also compensates
    for what this module cannot see: ``setup_logging()`` runs before — and
    synchronously, so it cannot await — ``_appdata.resolve_appdata_root()``,
    which means the ``--appdata`` CLI argument is invisible here. Only the env
    var is.
    """
    explicit = os.environ.get(_DEBUG_LOG_PATH_ENV, "").strip()
    if explicit:
        return explicit
    appdata = os.environ.get(_APPDATA_ENV, "").strip()
    root = appdata or platformdirs.user_data_dir(appname=_APPDATA_APPNAME, appauthor=False)
    return os.path.join(root, _LOG_DIRNAME, _LOG_FILENAME)


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
        # ``False`` for everything unlisted — a bare dict would still let
        # records from other modules through at the sink's own level.
        filter={"": False, **dict.fromkeys(modules, "DEBUG")},
        rotation=_ROTATION,
        retention=_RETENTION,
        compression=_COMPRESSION,
        enqueue=True,
        encoding="utf-8",
    )
    return _file_handler_id

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

import psi_agent._logging as _logging
from psi_agent._logging import (
    debug_log_path,
    debug_modules,
    install_session_patcher,
    prune_empty_debug_logs,
    setup_logging,
)
from psi_agent._session_context import session_id_scope
from psi_agent.session.runtime_context import get_session_id, runtime_scope


@pytest.fixture(autouse=True)
def _reset_logging_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Clear both one-shot guards and the env vars around every test."""
    monkeypatch.delenv("PSI_DEBUG_MODULES", raising=False)
    monkeypatch.delenv("PSI_DEBUG_LOG_PATH", raising=False)
    monkeypatch.delenv("PSI_APPDATA", raising=False)
    _logging._handler_id = None
    _logging._file_handler_id = None
    yield
    logger.remove()
    _logging._handler_id = None
    _logging._file_handler_id = None


def _read_debug_log(root: Path) -> str:
    """Concatenate the per-PID debug logs under *root*.

    The filename carries the writer's PID, so the exact name is only known at
    runtime — glob rather than name it.
    """
    files = sorted((root / "logs").glob("psi-debug-*.log"))
    assert files, f"no debug log written under {root / 'logs'}"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def test_setup_logging_default_info() -> None:
    handler_id = setup_logging(verbose=False)
    assert isinstance(handler_id, int)
    logger.remove(handler_id)


def test_setup_logging_verbose_debug() -> None:
    handler_id = setup_logging(verbose=True)
    assert isinstance(handler_id, int)
    logger.remove(handler_id)


def test_no_file_sink_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V1: unset ``PSI_DEBUG_MODULES`` must add nothing and create nothing.

    "Default off" is the *absence* of a sink, not a configured value.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    added: list[Any] = []
    real_add = logger.add
    monkeypatch.setattr(logger, "add", lambda *a, **kw: (added.append((a, kw)), real_add(*a, **kw))[1])

    setup_logging(verbose=False)

    assert len(added) == 1, "only the stderr sink may be installed"
    assert _logging._file_handler_id is None
    assert not (tmp_path / "logs").exists()


def test_file_sink_takes_only_listed_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V2: listed modules land in the file; unlisted DEBUG does not."""
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "tests.psi_agent.test_logging")

    setup_logging(verbose=False)
    assert _logging._file_handler_id is not None

    # A bare ``logger.debug`` here records ``name`` as this test module.
    logger.debug("from-a-listed-module")
    # ``patch`` rewrites ``name`` so one test can stand in for another module.
    unlisted = logger.patch(lambda record: record.update(name="psi_agent.session.agent"))
    unlisted.debug("from-an-unlisted-module")

    # ``enqueue=True`` hands records to a worker; remove() flushes and joins it.
    logger.remove()
    text = _read_debug_log(tmp_path)
    assert "from-a-listed-module" in text
    assert "from-an-unlisted-module" not in text


def test_debug_modules_does_not_change_stderr_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V3: the stderr sink keeps its own level — docker logs must not grow."""
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "psi_agent.ai.server")
    levels: list[Any] = []
    real_add = logger.add

    def spy(sink: Any, **kw: Any) -> int:
        levels.append((sink, kw.get("level")))
        return real_add(sink, **kw)

    monkeypatch.setattr(logger, "add", spy)
    setup_logging(verbose=False)

    stderr_levels = [lvl for sink, lvl in levels if not isinstance(sink, str)]
    assert stderr_levels == ["INFO"]


def test_file_sink_is_one_shot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V4: repeated calls must not stack file sinks (batch mode calls it a lot)."""
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "psi_agent.ai.server")

    first = setup_logging(verbose=False)
    file_id = _logging._file_handler_id
    second = setup_logging(verbose=True)

    assert first == second, "stderr sink stays one-shot: first caller wins"
    assert _logging._file_handler_id == file_id


def test_file_sink_rotation_parameters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V5: rotation and retention are the reason DEBUG is safe to enable."""
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "psi_agent.ai.server")
    captured: dict[str, Any] = {}
    real_add = logger.add

    def spy(sink: Any, **kw: Any) -> int:
        if isinstance(sink, str):
            captured.update(kw)
        return real_add(sink, **kw)

    monkeypatch.setattr(logger, "add", spy)
    setup_logging(verbose=False)

    assert captured["rotation"] == "20 MB"
    assert captured["retention"] == 10
    assert captured["compression"] == "gz"
    assert captured["level"] == "DEBUG"


def test_stderr_removal_does_not_wipe_the_file_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``setup_logging``'s bare ``logger.remove()`` must run *first*.

    Installing the file sink before it dropped that sink again while leaving
    ``_file_handler_id`` set, so the one-shot guard blocked any retry — the
    process ended up with no DEBUG file at all, silently.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "tests.psi_agent.test_logging")

    setup_logging(verbose=False)
    logger.debug("survives-setup")
    logger.remove()

    assert "survives-setup" in _read_debug_log(tmp_path)


def test_log_filename_is_per_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two processes in one container must not share a log file.

    Production's ``launch-gateway.sh`` runs ``psi-agent gateway`` and
    ``psi-agent channel feishu`` side by side, and the two modules worth
    observing sit in different processes. Sharing a path drops lines —
    ``enqueue=True`` only serialises writers within a process, and after
    rotation the losers write on into a renamed inode. Measured: 586 of 600
    lines survived with two processes and no rotation at all.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "tests.psi_agent.test_logging")

    assert debug_log_path().endswith(f"psi-debug-{os.getpid()}.log")

    setup_logging(verbose=False)
    logger.debug("pid-scoped")
    logger.remove()

    written = list((tmp_path / "logs").glob("psi-debug-*.log"))
    assert len(written) == 1
    assert str(os.getpid()) in written[0].name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("   ", []),
        ("psi_agent.ai.server", ["psi_agent.ai.server"]),
        (" a , b ", ["a", "b"]),
        ("a;b", ["a", "b"]),
        ("a,a", ["a"]),
    ],
)
def test_debug_modules_parsing(raw: str, expected: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSI_DEBUG_MODULES", raw)
    assert debug_modules() == expected


# -- session id column -------------------------------------------------------
#
# The whole point is that the id reaches **INFO on stderr**, because production
# pins INFO and one container multiplexes ~67 Sessions into that one stream.
# Asserting only against the DEBUG file sink would pass while the feature was
# useless — the exact trap the raw-SSE logs already fell into.


def _capture(level: str = "INFO") -> tuple[list[str], int]:
    """Add a sink that records fully formatted lines. Returns (lines, handler id)."""
    lines: list[str] = []
    handler = logger.add(lines.append, level=level, format=_logging._FORMAT)
    return lines, handler


def test_session_id_appears_on_stderr_format_at_info() -> None:
    """V1: a bound session id lands in the INFO line, not just in DEBUG."""
    install_session_patcher()
    lines, _ = _capture("INFO")

    with session_id_scope("feishu-ou_abc"):
        logger.info("inside")

    assert len(lines) == 1
    assert "feishu-ou_abc" in lines[0]
    # And in the session column specifically — not merely somewhere in the text.
    assert lines[0].split(" | ")[2].strip() == "feishu-ou_abc"


def test_session_id_placeholder_when_unbound() -> None:
    """V2: unbound turns must not blank the column or crash the sink.

    A missing ``extra`` key makes loguru raise *inside* the sink and drop the
    line, so "no session" has to be a real value, not an absent one.
    """
    install_session_patcher()
    lines, _ = _capture("INFO")

    logger.info("outside any turn")

    assert len(lines) == 1, "the line must not be swallowed by a formatting error"
    assert lines[0].split(" | ")[2].strip() == "-"


def test_session_id_is_restored_after_the_scope_exits() -> None:
    """V3: the column tracks the scope; a finished turn must not leak its id."""
    install_session_patcher()
    lines, _ = _capture("INFO")

    with session_id_scope("sid-a"):
        logger.info("a")
    logger.info("after")
    with session_id_scope("sid-b"):
        logger.info("b")

    columns = [line.split(" | ")[2].strip() for line in lines]
    assert columns == ["sid-a", "-", "sid-b"]


def test_session_id_survives_the_enqueued_file_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V4: ``enqueue=True`` formats in another process, where ContextVars are unset.

    This is why the patcher snapshots into ``record["extra"]`` instead of the
    format string reading the ContextVar directly.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "tests.psi_agent.test_logging")

    setup_logging(verbose=False)
    with session_id_scope("feishu-ou_enqueued"):
        logger.debug("through-the-queue")
    logger.remove()  # flushes and joins the worker

    assert "feishu-ou_enqueued" in _read_debug_log(tmp_path)


def test_runtime_scope_feeds_the_log_column() -> None:
    """V5: the *existing* writer (``runtime_scope``) drives the column.

    ``_session_context`` is a new module, so this pins that the Session layer's
    own entry point still shares that one ContextVar rather than a second copy.
    """
    install_session_patcher()
    lines, _ = _capture("INFO")

    with runtime_scope(session_id="feishu-ou_via_runtime", workspace="/w"):
        assert get_session_id() == "feishu-ou_via_runtime"
        logger.info("turn")

    assert lines[0].split(" | ")[2].strip() == "feishu-ou_via_runtime"


# -- empty per-PID log files -------------------------------------------------


def test_no_file_created_until_something_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V6: a process that never logs a whitelisted DEBUG leaves no file.

    Production accumulated 824 zero-byte files this way, one per PID, which made
    the directory unusable and buried the handful of files with content.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "psi_agent.ai.server")

    setup_logging(verbose=False)
    assert _logging._file_handler_id is not None, "the sink is still installed…"

    logger.remove()
    assert not list((tmp_path / "logs").glob("*.log")), "…but no file exists until a record arrives"


def test_file_is_created_once_a_record_arrives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V7: ``delay=True`` must not cost us the log when there *is* output.

    Guards the obvious way to break V6: dropping the sink entirely would also
    make the directory clean.
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", "tests.psi_agent.test_logging")

    setup_logging(verbose=False)
    logger.debug("now-there-is-content")
    logger.remove()

    assert "now-there-is-content" in _read_debug_log(tmp_path)


def test_patcher_is_installed_by_importing_the_module() -> None:
    """V9: a log emitted *before* ``setup_logging`` must not be dropped.

    ``_FORMAT`` names ``extra[psi_session]`` unconditionally, so if the patcher
    is not yet installed loguru raises inside the sink and swallows the record —
    silently. Only observable in a fresh interpreter: every other test in this
    file installs the patcher itself, so none of them would notice the
    import-time call going away.
    """
    program = textwrap.dedent(
        """
        import sys
        from loguru import logger
        from psi_agent._logging import _FORMAT  # the only psi_agent import

        logger.remove()
        seen = []
        logger.add(seen.append, level="INFO", format=_FORMAT)
        logger.info("before setup_logging")
        sys.stdout.write(f"LINES={len(seen)}\\n")
        sys.stdout.write("".join(seen))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": "src"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "LINES=1" in proc.stdout, f"the record was dropped: {proc.stdout!r} {proc.stderr!r}"
    assert "before setup_logging" in proc.stdout
    # loguru reports sink failures on stderr instead of raising — assert on it,
    # or a swallowed line looks identical to a healthy one.
    assert "Logging error in Logger" not in proc.stderr, proc.stderr


def test_prune_removes_only_empty_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """V8: cleaning up the existing 824 files must not touch files with content."""
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    logs = tmp_path / "logs"
    logs.mkdir()
    for pid in range(5):
        (logs / f"psi-debug-{pid}.log").write_text("", encoding="utf-8")
    (logs / "psi-debug-999.log").write_text("real content\n", encoding="utf-8")
    # Rotation products and unrelated files are out of scope.
    (logs / "psi-debug-1.log.gz").write_bytes(b"")
    (logs / "other.log").write_text("", encoding="utf-8")

    assert prune_empty_debug_logs(dry_run=True) == 5
    assert len(list(logs.glob("psi-debug-*.log"))) == 6, "dry run must delete nothing"

    assert prune_empty_debug_logs() == 5

    survivors = sorted(p.name for p in logs.iterdir())
    assert survivors == ["other.log", "psi-debug-1.log.gz", "psi-debug-999.log"]
    assert (logs / "psi-debug-999.log").read_text(encoding="utf-8") == "real content\n"


def test_debug_log_path_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit path wins over AppData; AppData wins over platformdirs."""
    pid = os.getpid()
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    assert debug_log_path() == os.path.join(str(tmp_path), "logs", f"psi-debug-{pid}.log")

    monkeypatch.setenv("PSI_DEBUG_LOG_PATH", "/var/log/psi/explicit.log")
    assert debug_log_path() == "/var/log/psi/explicit.log"

    monkeypatch.setenv("PSI_DEBUG_LOG_PATH", "/var/log/psi/psi-{pid}.log")
    assert debug_log_path() == f"/var/log/psi/psi-{pid}.log"

    monkeypatch.delenv("PSI_DEBUG_LOG_PATH")
    monkeypatch.delenv("PSI_APPDATA")
    fallback = debug_log_path()
    assert fallback.endswith(os.path.join("logs", f"psi-debug-{pid}.log"))
    assert "Haitun" in fallback

"""The child supervisor spawn is off by default, and the before-turn hook pays nothing for it.

Production evidence that condemned the feature (2026-09-01 log survey): 251
before-turn hook timeouts, 240 child processes started, **0** "handle ready" and
**0** "readiness check failed". Two mutually exclusive branches both at zero is
what proves the call never reached a verdict — it hung in ``wait_fn`` and left
only by the kernel's 30s ``system_before_turn`` timeout, because the child
workspace ``haitun-supervisor-workspace`` is not deployed at the path
``ensure_supervisor`` computes.

So the assertions here are about the *cost*, not just the return value: the
30s is spent inside ``wait_fn``, so what has to be pinned is that no ``wait_fn``
exists to call. ``_wait_fn is None`` after a full ``before_turn`` means
``_dependencies()`` was never reached, which is a stronger statement than
"returned None quickly" — a hang would still return None eventually.

Both agent copies are covered. ``agents/feishu`` and ``agents/desktop`` ship
byte-identical ``supervisor.py`` files and production runs that md5; this repo
has already shipped a fix to one side only once, so parity is asserted too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ("feishu", "desktop")
ENV_FLAG = "PSI_HAITUN_SUPERVISOR_CHILD"

# ``supervisor.py`` imports its siblings by bare name, and both agents install a
# module literally named ``supervisor``, so a cached entry from one agent would
# satisfy the other's import and silently test the same file twice.
_SIBLINGS = ("supervisor", "supervisor_protocol", "supervisor_store")


def _load_supervisor(agent: str) -> Any:
    systems = ROOT / "agents" / agent / "systems"
    saved_modules = {k: sys.modules.pop(k) for k in _SIBLINGS if k in sys.modules}
    saved_path = list(sys.path)
    sys.path.insert(0, str(systems))
    name = f"supervisor_{agent}_test"
    try:
        spec = importlib.util.spec_from_file_location(name, systems / "supervisor.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        # ``SupervisorHandle`` is a ``slots=True`` dataclass, and dataclasses
        # resolves its annotations through ``sys.modules[cls.__module__]`` —
        # exec'ing an unregistered module raises ``AttributeError`` there.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved_path
        sys.modules.pop(name, None)
        for k in _SIBLINGS:
            sys.modules.pop(k, None)
        sys.modules.update(saved_modules)


class _Recorder:
    """Stands in for every injected dependency and records what got called."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fn(self, name: str, result: dict[str, Any]):
        async def _call(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            self.calls.append(name)
            return result

        return _call


def _manager(module: Any, workspace: Path, recorder: _Recorder) -> Any:
    return module.SupervisorManager(
        anyio.Path(workspace),
        plan_fn=recorder.fn("plan", {"ok": False}),
        start_fn=recorder.fn("start", {"ok": True}),
        stop_fn=recorder.fn("stop", {"ok": True}),
        wait_fn=recorder.fn("wait", {"ok": True}),
        chat_fn=recorder.fn("chat", {"ok": False, "text": ""}),
    )


def test_both_agents_ship_the_same_supervisor() -> None:
    """A one-sided fix is the failure mode this repo has already shipped once."""
    bodies = {agent: (ROOT / "agents" / agent / "systems" / "supervisor.py").read_bytes() for agent in AGENTS}
    assert bodies["feishu"] == bodies["desktop"]


@pytest.mark.parametrize("agent", AGENTS)
def test_flag_defaults_to_off(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    module = _load_supervisor(agent)

    assert module.is_child_supervisor_enabled() is False
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(ENV_FLAG, value)
        assert module.is_child_supervisor_enabled() is True, value
    for value in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(ENV_FLAG, value)
        assert module.is_child_supervisor_enabled() is False, value


@pytest.mark.anyio
@pytest.mark.parametrize("agent", AGENTS)
async def test_ensure_supervisor_spawns_nothing_by_default(
    agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    module = _load_supervisor(agent)
    recorder = _Recorder()

    handle = await _manager(module, tmp_path, recorder).ensure_supervisor("a" * 64)

    assert handle is None
    # ``plan``/``start`` would spawn the child; ``wait`` is where the 30s went.
    assert recorder.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("agent", AGENTS)
async def test_before_turn_never_resolves_a_socket_waiter(
    agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook's 30s was spent in ``wait_fn``; with the child off there is none.

    ``_wait_fn`` stays ``None`` only if ``_dependencies()`` was never reached,
    so nothing on this path can block on a socket that will never be bound.
    """
    monkeypatch.delenv(ENV_FLAG, raising=False)
    module = _load_supervisor(agent)
    manager = module.SupervisorManager(anyio.Path(tmp_path))
    message = {"user_id": "u-1", "session_id": "s-1", "content": "什么是神经符号推理?"}

    first = await manager.before_turn(message)
    second = await manager.before_turn(message)

    assert first is None, "first eligible turn is warmup-only, unchanged"
    assert second == module.empty_advice(), "degrades exactly as it already did on all 251 timed-out turns"
    assert manager._wait_fn is None
    assert manager._plan_fn is None
    assert manager._start_fn is None
    assert manager._chat_fn is None


@pytest.mark.anyio
@pytest.mark.parametrize("agent", AGENTS)
async def test_flag_re_opens_the_spawn_path(agent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch is a switch: flipping it restores the pre-fix call sequence.

    Without this, a guard that returned ``None`` unconditionally would pass every
    other test in this file.
    """
    monkeypatch.setenv(ENV_FLAG, "1")
    module = _load_supervisor(agent)
    recorder = _Recorder()

    handle = await _manager(module, tmp_path, recorder).ensure_supervisor("b" * 64)

    assert handle is None, "planning still fails (ok=False) — the point is that it was attempted"
    assert recorder.calls == ["plan"]

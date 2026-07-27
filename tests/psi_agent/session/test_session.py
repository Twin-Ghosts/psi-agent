from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from psi_agent.session import Session
from psi_agent.session.schedule_registry import ACTIVATE_ALL
from psi_agent.session.system_prompt import SystemPrompt


@pytest.mark.anyio
async def test_system_py_not_exists(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    await anyio.Path(ws).mkdir()
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert await sp._builder() == ""


@pytest.mark.anyio
async def test_system_py_missing_system_prompt_builder(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    systems = ws / "systems"
    await anyio.Path(systems).mkdir(parents=True)
    await anyio.Path(systems / "system.py").write_text("def unrelated():\n    pass", encoding="utf-8")
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert await sp._builder() == ""


@pytest.mark.anyio
async def test_system_prompt_builder_not_async(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    systems = ws / "systems"
    await anyio.Path(systems).mkdir(parents=True)
    await anyio.Path(systems / "system.py").write_text(
        "def system_prompt_builder():\n    return 'hello'", encoding="utf-8"
    )
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert await sp._builder() == ""


@pytest.mark.anyio
async def test_system_prompt_builder_loads(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    systems = ws / "systems"
    await anyio.Path(systems).mkdir(parents=True)
    await anyio.Path(systems / "system.py").write_text(
        "async def system_prompt_builder() -> str:\n    return 'test prompt'", encoding="utf-8"
    )
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert sp is not None

    result = await sp._builder()
    assert result == "test prompt"


@pytest.mark.anyio
async def test_syntax_error_in_system_py(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    systems = ws / "systems"
    await anyio.Path(systems).mkdir(parents=True)
    await anyio.Path(systems / "system.py").write_text("this is not valid python {{{", encoding="utf-8")
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert await sp._builder() == ""


@pytest.mark.anyio
async def test_rebuild_checker_loads(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    systems = ws / "systems"
    await anyio.Path(systems).mkdir(parents=True)
    await anyio.Path(systems / "system.py").write_text(
        "async def system_prompt_builder() -> str:\n    return 'p'\n\n"
        "async def system_prompt_rebuild_checker() -> bool:\n    return True\n",
        encoding="utf-8",
    )
    sp = await SystemPrompt.from_workspace(ws, "test")
    assert sp is not None
    assert await sp._builder() == "p"
    assert await sp._checker() is True


def test_workspace_empty_string_uses_cwd(tmp_path: Path) -> None:
    session = Session(workspace="", channel_socket=str(tmp_path / "c.sock"), ai_socket=str(tmp_path / "a.sock"))
    assert session.workspace == ""


# ── 激活名单归一 (--scheduler / --schedules) ───────────────────────────────────


def _session(tmp_path: Path, **kwargs: object) -> Session:
    return Session(
        channel_socket=str(tmp_path / "c.sock"),
        ai_socket=str(tmp_path / "a.sock"),
        **kwargs,  # type: ignore[arg-type]
    )


def test_no_flags_activates_nothing(tmp_path: Path) -> None:
    """默认一条都不激活 —— 一条 schedule 必须恰好被一个 Session 触发。"""
    assert _session(tmp_path)._active_schedules() == set()


def test_scheduler_flag_expands_to_wildcard(tmp_path: Path) -> None:
    assert _session(tmp_path, scheduler=True)._active_schedules() == {ACTIVATE_ALL}


def test_schedules_names_are_split_and_trimmed(tmp_path: Path) -> None:
    assert _session(tmp_path, schedules=" daily , weekly ,")._active_schedules() == {"daily", "weekly"}


def test_scheduler_and_names_union(tmp_path: Path) -> None:
    got = _session(tmp_path, scheduler=True, schedules="daily")._active_schedules()
    assert got == {ACTIVATE_ALL, "daily"}

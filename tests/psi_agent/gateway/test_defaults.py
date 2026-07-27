from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from psi_agent.gateway._defaults import (
    appdata_history_path,
    resolve_appdata_root,
    resolve_default_agent,
    resolve_default_workspace,
    resolve_history_read_path,
)
from psi_agent.gateway._session_manager import SessionInfo


@pytest.mark.anyio
async def test_resolve_default_workspace_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "user-ws"
    await anyio.Path(ws).mkdir()
    assert await resolve_default_workspace(str(ws)) == str(await anyio.Path(ws).resolve())


@pytest.mark.anyio
async def test_resolve_default_workspace_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert await resolve_default_workspace("") == str(await anyio.Path.cwd())


@pytest.mark.anyio
async def test_resolve_default_agent_soft_haitun_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / "examples" / "haitun-workspace"
    await anyio.Path(agent).mkdir(parents=True)
    assert await resolve_default_agent("") == str(await anyio.Path(agent).resolve())


@pytest.mark.anyio
async def test_resolve_default_agent_empty_without_soft_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert await resolve_default_agent("") == ""


@pytest.mark.anyio
async def test_resolve_appdata_root_explicit(tmp_path: Path) -> None:
    root = tmp_path / "memory"
    await anyio.Path(root).mkdir()
    assert await resolve_appdata_root(str(root)) == str(await anyio.Path(root).resolve())


@pytest.mark.anyio
async def test_resolve_appdata_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "from-env"
    await anyio.Path(root).mkdir()
    monkeypatch.setenv("PSI_APPDATA", str(root))
    assert await resolve_appdata_root("") == str(await anyio.Path(root).resolve())


@pytest.mark.anyio
async def test_resolve_appdata_root_platformdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSI_APPDATA", raising=False)
    fake = tmp_path / "plat"
    await anyio.Path(fake).mkdir()
    monkeypatch.setattr(
        "psi_agent._appdata.platformdirs.user_data_dir",
        lambda **_kwargs: str(fake),
    )
    assert await resolve_appdata_root("") == str(await anyio.Path(fake).resolve())


@pytest.mark.anyio
async def test_resolve_history_read_path_prefers_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appdata = tmp_path / "appdata"
    ws = tmp_path / "ws"
    monkeypatch.setenv("PSI_APPDATA", str(appdata))
    primary = appdata_history_path(str(appdata), "s1")
    await primary.parent.mkdir(parents=True)
    await primary.write_text("{}\n", encoding="utf-8")
    legacy = anyio.Path(str(ws)) / "histories" / "s1.jsonl"
    await legacy.parent.mkdir(parents=True)
    await legacy.write_text("{}\n", encoding="utf-8")
    assert await resolve_history_read_path(appdata_root=str(appdata), workspace=str(ws), session_id="s1") == primary


@pytest.mark.anyio
async def test_resolve_history_read_path_falls_back_to_legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appdata = tmp_path / "appdata"
    ws = tmp_path / "ws"
    monkeypatch.setenv("PSI_APPDATA", str(appdata))
    await anyio.Path(str(appdata)).mkdir()
    legacy = anyio.Path(str(ws)) / "histories" / "s1.jsonl"
    await legacy.parent.mkdir(parents=True)
    await legacy.write_text("{}\n", encoding="utf-8")
    assert await resolve_history_read_path(appdata_root=str(appdata), workspace=str(ws), session_id="s1") == legacy


def test_session_info_includes_agent_field() -> None:
    info = SessionInfo(
        id="s1",
        backend_type="ai",
        backend_id="ai-1",
        workspace="/ws",
        channel_socket="sock",
        agent="/agent",
    )
    assert info.agent == "/agent"
    assert info.ai_id == "ai-1"

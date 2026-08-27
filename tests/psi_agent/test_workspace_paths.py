"""The neutral mechanism, exercised with non-brand names only.

Every name here is deliberately made up (``收货箱`` / ``pkgs/demo-agent``): if a
future edit sneaks a product default back into ``_workspace_paths``, these
assertions break instead of silently agreeing with ``gateway/_defaults.py``.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import psi_agent._workspace_paths as workspace_paths
from psi_agent._workspace_paths import (
    ensure_workspace_dir,
    resolve_agent_package,
    resolve_user_workspace,
)


@pytest.mark.anyio
async def test_resolve_user_workspace_explicit_ignores_default_name(tmp_path: Path) -> None:
    ws = tmp_path / "given-ws"
    await anyio.Path(ws).mkdir()
    got = await resolve_user_workspace(str(ws), default_name="收货箱")
    assert got == str(await anyio.Path(ws).resolve())


@pytest.mark.anyio
async def test_resolve_user_workspace_uses_caller_name_under_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop = tmp_path / "Desktop"
    await anyio.Path(desktop).mkdir()
    monkeypatch.setattr(
        "psi_agent._workspace_paths.platformdirs.user_desktop_dir",
        lambda: str(desktop),
    )
    expected = desktop / "收货箱"
    assert await resolve_user_workspace("", default_name="收货箱") == str(await anyio.Path(expected).resolve())
    # Announce only — the folder must not appear until Session create.
    assert not await anyio.Path(expected).exists()


@pytest.mark.anyio
async def test_ensure_workspace_dir_creates_parents(tmp_path: Path) -> None:
    ws = tmp_path / "a" / "b" / "收货箱"
    got = await ensure_workspace_dir(str(ws))
    assert got == str(await anyio.Path(ws).resolve())
    assert await anyio.Path(ws).is_dir()


@pytest.mark.anyio
async def test_resolve_agent_package_prefers_caller_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Candidate wins over the cwd tools/+skills/ layout (repo root may have both)."""
    monkeypatch.chdir(tmp_path)
    await anyio.Path(tmp_path / "tools").mkdir()
    await anyio.Path(tmp_path / "skills").mkdir()
    agent = tmp_path / "pkgs" / "demo-agent"
    await anyio.Path(agent).mkdir(parents=True)
    got = await resolve_agent_package("", repo_candidate="pkgs/demo-agent")
    assert got == str(await anyio.Path(agent).resolve())


@pytest.mark.anyio
async def test_resolve_agent_package_falls_back_to_cwd_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    await anyio.Path(tmp_path / "tools").mkdir()
    await anyio.Path(tmp_path / "skills").mkdir()
    got = await resolve_agent_package("", repo_candidate="pkgs/demo-agent")
    assert got == str(await anyio.Path(tmp_path).resolve())


@pytest.mark.anyio
async def test_resolve_agent_package_empty_without_candidate_or_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert await resolve_agent_package("", repo_candidate="pkgs/demo-agent") == ""
    # No candidate at all is legal: mechanism has no default of its own.
    assert await resolve_agent_package("") == ""


def test_module_carries_no_product_literals() -> None:
    """Guard for 3.2: the neutral layer must not learn a ToC concept."""
    # Resolve via the imported module, not cwd — other tests chdir into tmp_path.
    source = Path(str(workspace_paths.__file__)).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
    for banned in ("haitun", "交付", "examples/"):
        assert banned not in body, f"product literal {banned!r} leaked into the neutral module"

"""私密空间在各收口点的实际拦截 —— 路径解析 / bash / ``[SEND:]``。

守卫本身的判定逻辑见 ``test_private_space.py``; 这里验证它确实被接进了各工具。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "examples" / "haitun-workspace" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import _private_space as ps  # noqa: E402
import _runtime_paths as paths  # noqa: E402
import bash as bash_tool  # noqa: E402
import read as read_tool  # noqa: E402
import search_content as sc_tool  # noqa: E402
import write_excel as xl_tool  # noqa: E402

from psi_agent.channel.feishu import _private_space as chan_ps  # noqa: E402

OWNER = "ou_private_owner"
OTHER = "ou_other_person"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    (root / "public").mkdir(parents=True)
    (root / "public" / "shared.md").write_text("hello public", encoding="utf-8")
    priv = root / ps.PRIVATE_DIRNAME / OWNER
    priv.mkdir(parents=True)
    (priv / "secret.md").write_text("TOP SECRET payroll", encoding="utf-8")
    (root / OTHER).mkdir()
    monkeypatch.setenv("PSI_PRIVATE_OPEN_IDS", OWNER)
    return root


def _session_as(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    """让 ``_private_space`` 与 ``_runtime_paths`` 都看到同一个 session workspace。"""
    monkeypatch.setattr(ps, "_runtime_workspace", lambda: str(ws))
    monkeypatch.setattr(paths, "_runtime_workspace", lambda: str(ws))


# --- resolve_under: 22 个路径工具的公共收口点 ---


def test_resolve_under_blocks_other_users_private(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    with pytest.raises(PermissionError):
        paths.resolve_user_path(str(secret))


def test_resolve_under_allows_owner(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    priv = workspace / ps.PRIVATE_DIRNAME / OWNER
    _session_as(monkeypatch, priv)
    assert str(paths.resolve_user_path("secret.md")).endswith("secret.md")


def test_resolve_under_allows_public_for_everyone(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    assert paths.resolve_user_path(str(workspace / "public" / "shared.md")) is not None
    _session_as(monkeypatch, workspace / ps.PRIVATE_DIRNAME / OWNER)
    assert paths.resolve_user_path(str(workspace / "public" / "shared.md")) is not None


# --- read: 走 resolve_user_path, 应拿不到内容 ---


@pytest.mark.anyio
async def test_read_tool_denied_for_other(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    with pytest.raises(PermissionError):
        await read_tool.read(str(secret))


@pytest.mark.anyio
async def test_read_tool_owner_gets_content(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / ps.PRIVATE_DIRNAME / OWNER)
    out = await read_tool.read("secret.md")
    assert "TOP SECRET payroll" in out


# --- bash: 命令层前置检查 ---


@pytest.mark.anyio
async def test_bash_blocks_cat_of_private(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    out = await bash_tool.bash(f"cat {secret}")
    assert "[Error]" in out
    assert "TOP SECRET" not in out


@pytest.mark.anyio
async def test_bash_allows_public(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # cwd 是 session workspace, 用相对路径 —— Windows 反斜杠在 bash 里会被当转义吃掉。
    _session_as(monkeypatch, workspace / "public")
    out = await bash_tool.bash("cat shared.md")
    assert "hello public" in out


# --- search_content: 遍历时剔掉别人的私密子树 ---


@pytest.mark.anyio
async def test_search_content_skips_private_subtree(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    out = await sc_tool.search_content("SECRET", path=str(workspace), is_regex=False)
    assert "secret.md" not in out


@pytest.mark.anyio
async def test_search_content_direct_private_path_denied(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    out = await sc_tool.search_content("SECRET", path=str(workspace / ps.PRIVATE_DIRNAME / OWNER), is_regex=False)
    assert "[Error]" in out


@pytest.mark.anyio
async def test_search_content_owner_finds_own(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    priv = workspace / ps.PRIVATE_DIRNAME / OWNER
    _session_as(monkeypatch, priv)
    out = await sc_tool.search_content("SECRET", path=str(priv), is_regex=False)
    assert "secret.md" in out


# --- write_excel: 不经 _runtime_paths, 单独接的守卫 ---


@pytest.mark.anyio
async def test_write_excel_denied_into_private(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _session_as(monkeypatch, workspace / OTHER)
    target = workspace / ps.PRIVATE_DIRNAME / OWNER / "planted.xlsx"
    out = await xl_tool.write_excel(str(target), '[["a"]]')
    assert "[Error]" in out
    assert not target.exists()


# --- channel 侧 [SEND:] ---


def test_send_blocked_for_other_sender(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert chan_ps.blocks_send(secret, OTHER) is True


def test_send_allowed_for_owner(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert chan_ps.blocks_send(secret, OWNER) is False


def test_send_public_file_always_allowed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shared = workspace / "public" / "shared.md"
    assert chan_ps.blocks_send(shared, OTHER) is False
    assert chan_ps.blocks_send(shared, OWNER) is False


def test_send_noop_without_config(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSI_PRIVATE_OPEN_IDS", raising=False)
    secret = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert chan_ps.blocks_send(secret, OTHER) is False

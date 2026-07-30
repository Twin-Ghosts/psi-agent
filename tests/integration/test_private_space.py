"""私密文件空间守卫测试 —— ``examples/haitun-workspace/tools/_private_space.py``。

工具目录是扁平模块 (没有 ``__init__.py``, 工具间互相 ``import _xxx``), 所以这里
把它塞进 ``sys.path`` 后按模块名导入, 与运行时的加载方式一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "examples" / "haitun-workspace" / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import _private_space as ps  # noqa: E402

OWNER = "ou_private_owner"
OTHER = "ou_other_person"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``<tmp>/workspace`` 里放好公共区与 OWNER 的私密区, 并登记白名单。"""
    root = tmp_path / "workspace"
    (root / "public").mkdir(parents=True)
    (root / "public" / "shared.md").write_text("public", encoding="utf-8")
    priv = root / ps.PRIVATE_DIRNAME / OWNER
    priv.mkdir(parents=True)
    (priv / "secret.md").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("PSI_PRIVATE_OPEN_IDS", OWNER)
    return root


def _as_owner(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """把当前 session 的 workspace 设成 OWNER 的私密区。"""
    monkeypatch.setattr(ps, "_runtime_workspace", lambda: str(workspace / ps.PRIVATE_DIRNAME / OWNER))


def _as_other(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> None:
    """当前 session 是普通用户 (workspace 在公共根下的自己目录)。"""
    monkeypatch.setattr(ps, "_runtime_workspace", lambda: str(workspace / OTHER))


# --- 未配置白名单 → 完全空操作 (不改变任何现有行为) ---


def test_no_private_ids_allows_everything(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSI_PRIVATE_OPEN_IDS", raising=False)
    _as_other(monkeypatch, workspace)
    target = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert ps.denial_reason(target) is None
    assert ps.owner_of(target) is None


def test_private_open_ids_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSI_PRIVATE_OPEN_IDS", " ou_a , ou_b ;ou_a,, ")
    assert ps.private_open_ids() == ["ou_a", "ou_b"]


# --- 主人自己 ---


def test_owner_reads_own_private(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_owner(monkeypatch, workspace)
    assert ps.denial_reason(workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md") is None


def test_owner_may_use_public_root_one_way(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单向: 私密用户照常读写公共区。"""
    _as_owner(monkeypatch, workspace)
    assert ps.denial_reason(workspace / "public" / "shared.md") is None
    assert ps.denial_reason(workspace / "public" / "new-file.md") is None


def test_current_owner_detects_session(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_owner(monkeypatch, workspace)
    assert ps.current_owner() == OWNER
    _as_other(monkeypatch, workspace)
    assert ps.current_owner() is None


# --- 其他用户被拦 ---


def test_other_denied_absolute_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    reason = ps.denial_reason(workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md")
    assert reason is not None
    assert "私密" in reason


def test_other_denied_dotdot_escape(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``public/../.private/<owner>/x`` 规范化后仍落在私密区 → 拦。"""
    _as_other(monkeypatch, workspace)
    sneaky = workspace / "public" / ".." / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert ps.denial_reason(sneaky) is not None


def test_other_denied_private_dir_itself(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    assert ps.denial_reason(workspace / ps.PRIVATE_DIRNAME / OWNER) is not None


def test_other_may_use_public(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    assert ps.denial_reason(workspace / "public" / "shared.md") is None


def test_check_access_raises_permission_error(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    with pytest.raises(PermissionError):
        ps.check_access(workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md")
    ps.check_access(workspace / "public" / "shared.md")  # 不抛


@pytest.mark.skipif(sys.platform == "win32", reason="symlink 在 Windows 需要额外权限")
def test_other_denied_symlink_escape(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """公共区里指向私密区的 symlink 也拦得住 (realpath 展开)。"""
    link = workspace / "public" / "shortcut"
    link.symlink_to(workspace / ps.PRIVATE_DIRNAME / OWNER)
    _as_other(monkeypatch, workspace)
    assert ps.denial_reason(link / "secret.md") is not None


# --- shell 命令层 ---


def test_scan_command_blocks_absolute_private_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    target = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert ps.scan_command(f"cat {target}") is not None


def test_scan_command_blocks_bare_private_dir(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``ls .private`` 也拦 —— 免得靠列目录反推出谁有私密区。"""
    _as_other(monkeypatch, workspace)
    monkeypatch.setattr(ps, "_runtime_workspace", lambda: str(workspace))
    assert ps.scan_command(f"ls {workspace / ps.PRIVATE_DIRNAME}") is not None


def test_scan_command_allows_public(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_other(monkeypatch, workspace)
    assert ps.scan_command("cat public/shared.md") is None
    assert ps.scan_command("ls -la") is None


def test_scan_command_owner_reads_own(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _as_owner(monkeypatch, workspace)
    target = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert ps.scan_command(f"cat {target}") is None


def test_scan_command_noop_without_config(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PSI_PRIVATE_OPEN_IDS", raising=False)
    _as_other(monkeypatch, workspace)
    target = workspace / ps.PRIVATE_DIRNAME / OWNER / "secret.md"
    assert ps.scan_command(f"cat {target}") is None

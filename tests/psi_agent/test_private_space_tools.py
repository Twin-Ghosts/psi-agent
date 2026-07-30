"""工具层隔离测试 —— 验证守卫在真实工具调用上确实拒绝。

守卫本体的判定在 ``test_private_space.py``; 这里测的是**接线**: 每个收口点是否真的
调了守卫、拒绝时是否返回可读错误串而不是抛穿。

``examples/haitun-workspace/tools/`` 是扁平模块包(无 ``__init__.py``, 模块间互相
``import _xxx``), 由 pyproject 的 ``pytest.pythonpath`` 加进 ``sys.path``, 故可直接
按模块名导入。
"""

from __future__ import annotations

import bash as bash_tool
import edit as edit_tool
import find_files as find_files_tool
import list_dir as list_dir_tool
import pytest
import read as read_tool
import search_content as sc
import write as write_tool

from psi_agent.session.runtime_context import runtime_scope

_ME = "ou_me"
_OTHER = "ou_other"


@pytest.fixture
def space(tmp_path, monkeypatch):
    """``<root>/{ou_me,ou_other,public}``, 各放一个文件, 启用守卫。"""
    for name in (_ME, _OTHER, "public"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "secret.md").write_text(f"{name} 的内容", encoding="utf-8")
    monkeypatch.setenv("PSI_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def _as_me(space):
    """以 ou_me 的身份进入一轮 turn (绑定 session_id + workspace)。"""
    return runtime_scope(session_id=f"feishu-{_ME}", workspace=str(space / _ME))


@pytest.mark.anyio
async def test_read_denies_other_absolute_path(space):
    """绝对路径此前**原样放行**, 是最直接的越权读法。"""
    with _as_me(space):
        out = await read_tool.read(str(space / _OTHER / "secret.md"))
    assert "拒绝访问" in out
    assert "ou_other 的内容" not in out


@pytest.mark.anyio
async def test_read_allows_own_and_public(space):
    with _as_me(space):
        mine = await read_tool.read("secret.md")
        pub = await read_tool.read(str(space / "public" / "secret.md"))
    assert "ou_me 的内容" in mine
    assert "public 的内容" in pub


@pytest.mark.anyio
async def test_write_denies_other_space(space):
    with _as_me(space):
        out = await write_tool.write(str(space / _OTHER / "planted.md"), "x")
    # 读检查先于写检查触发(resolve 阶段就拒了), 故这里是"拒绝访问"而非"拒绝写入" ——
    # 两者都是拒绝, 断言只认"拒绝"以免绑死在实现顺序上。
    assert "拒绝" in out
    assert not (space / _OTHER / "planted.md").exists()


@pytest.mark.anyio
async def test_write_denies_public_but_allows_own(space):
    """公共区只读: 防一个人覆盖公共材料影响所有人。"""
    with _as_me(space):
        denied = await write_tool.write(str(space / "public" / "x.md"), "x")
        ok = await write_tool.write("mine.md", "x")
    assert "拒绝写入" in denied
    assert "[OK]" in ok
    assert (space / _ME / "mine.md").exists()


@pytest.mark.anyio
async def test_edit_denies_other_space(space):
    with _as_me(space):
        out = await edit_tool.edit(str(space / _OTHER / "secret.md"), "ou_other", "hacked")
    assert "拒绝" in out
    assert "hacked" not in (space / _OTHER / "secret.md").read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_list_dir_hides_other_owners(space):
    """列隔离父目录时, 别人的目录名本身就是信息泄露(有哪些人)。"""
    with _as_me(space):
        out = await list_dir_tool.list_dir(str(space))
    assert _OTHER not in out
    assert "public/" in out


@pytest.mark.anyio
async def test_find_files_skips_other_subtree(space):
    with _as_me(space):
        out = await find_files_tool.find_files("**/*.md", str(space))
    assert _OTHER not in out
    assert _ME in out


@pytest.mark.anyio
async def test_search_content_skips_other_subtree(space):
    """rg 不认隔离边界, 命中结果必须再过一遍前缀过滤。"""
    with _as_me(space):
        out = await sc.search_content("的内容", str(space), is_regex=False)
    assert "ou_other" not in out


@pytest.mark.anyio
async def test_search_content_python_fallback_skips_other(space, monkeypatch):
    """部署环境(214 容器)没装 rg, 走纯 Python walk —— 那条分支必须同样隔离。"""
    monkeypatch.setattr(sc.shutil, "which", lambda _name: None)
    with _as_me(space):
        out = await sc.search_content("的内容", str(space), is_regex=False)
    assert "ou_other" not in out
    assert "ou_me" in out


@pytest.mark.anyio
async def test_bash_blocks_reference_to_other_dir(space):
    """shell 层是启发式扫描(非沙箱), 挡直白形态。"""
    with _as_me(space):
        out = await bash_tool.bash(f"cat {space / _OTHER / 'secret.md'}")
    assert "拒绝执行" in out
    assert "ou_other 的内容" not in out


@pytest.mark.anyio
async def test_bash_allows_own_command(space):
    with _as_me(space):
        out = await bash_tool.bash("cat secret.md")
    assert "ou_me 的内容" in out


@pytest.mark.anyio
async def test_guard_disabled_restores_old_behavior(space, monkeypatch):
    """未配 PSI_WORKSPACE_ROOT 时越权读照旧放行 —— 钉住"零行为变化"。"""
    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    with _as_me(space):
        out = await read_tool.read(str(space / _OTHER / "secret.md"))
    assert "ou_other 的内容" in out


@pytest.mark.anyio
async def test_unowned_session_unrestricted(space):
    """SPA 手建 session (无 feishu- 前缀) 不受隔离约束。"""
    with runtime_scope(session_id="local-1", workspace=str(space / _ME)):
        out = await read_tool.read(str(space / _OTHER / "secret.md"))
    assert "ou_other 的内容" in out

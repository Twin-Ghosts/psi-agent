"""对称隔离守卫的单元测试。

覆盖两条线:
1. 守卫本体的判定 (owner 推导 / 读写权 / symlink 与 ``..`` 展开 / 命令扫描)。
2. **未启用时零行为变化** —— 这是能"先部署再开关"的前提, 必须钉住。
"""

from __future__ import annotations

import os

import anyio
import pytest

from psi_agent import _private_space as ps

_ME = "ou_me"
_OTHER = "ou_other"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """建 ``<root>/{ou_me,ou_other,public}`` 并启用守卫。"""
    for name in (_ME, _OTHER, ps.PUBLIC_DIRNAME):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("PSI_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def _sid(open_id: str) -> str:
    return f"feishu-{open_id}"


@pytest.mark.anyio
async def test_disabled_by_default(monkeypatch, tmp_path):
    """未配 PSI_WORKSPACE_ROOT 时全部放行 —— 零行为变化。"""
    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    assert not ps.enabled()
    assert await ps.check_read(str(tmp_path / "anything"), session_id=_sid(_ME)) is None
    assert await ps.check_write(str(tmp_path / "anything"), session_id=_sid(_ME)) is None
    assert await ps.forbidden_dirs(_sid(_ME)) == []
    assert await ps.scan_command("cat /etc/passwd", session_id=_sid(_ME)) is None
    assert ps.owns_session("feishu-anyone", session_id=_sid(_ME)) is True
    assert await ps.blocks_send(str(tmp_path / "x"), open_id=_ME) is False


def test_owner_from_session_id():
    assert ps.owner_from_session_id("feishu-ou_abc") == "ou_abc"
    assert ps.owner_from_session_id("feishu-chat-oc_x") == "chat-oc_x"
    # SPA 手建 / 本机 session 无主 → 不受约束。
    assert ps.owner_from_session_id("my-local-session") == ""
    assert ps.owner_from_session_id("") == ""


@pytest.mark.anyio
async def test_owner_of_and_public(root):
    assert await ps.owner_of(str(root / _ME / "f.md")) == _ME
    assert await ps.owner_of(str(root / _OTHER / "f.md")) == _OTHER
    # 公共区与 root 本身无主。
    assert await ps.owner_of(str(root / ps.PUBLIC_DIRNAME / "f.md")) == ""
    assert await ps.owner_of(str(root)) == ""
    # root 之外(agent 包 / 系统路径)不属任何人。
    assert await ps.owner_of(os.path.join(os.sep, "etc", "passwd")) == ""


@pytest.mark.anyio
async def test_read_own_and_public_allowed(root):
    me = _sid(_ME)
    assert await ps.check_read(str(root / _ME / "f.md"), session_id=me) is None
    assert await ps.check_read(str(root / ps.PUBLIC_DIRNAME / "f.md"), session_id=me) is None


@pytest.mark.anyio
async def test_read_other_denied(root):
    reason = await ps.check_read(str(root / _OTHER / "f.md"), session_id=_sid(_ME))
    assert reason is not None
    assert "拒绝访问" in reason


@pytest.mark.anyio
async def test_symmetric_both_directions_denied(root):
    """对称隔离: 两两互不可见, 不存在"某人能读所有人"的特权方向。"""
    assert await ps.check_read(str(root / _OTHER / "f.md"), session_id=_sid(_ME)) is not None
    assert await ps.check_read(str(root / _ME / "f.md"), session_id=_sid(_OTHER)) is not None


@pytest.mark.anyio
async def test_write_public_denied_read_allowed(root):
    """公共区可读不可写 —— 否则一个人能覆盖公共材料影响所有人。"""
    target = str(root / ps.PUBLIC_DIRNAME / "f.md")
    me = _sid(_ME)
    assert await ps.check_read(target, session_id=me) is None
    assert await ps.check_write(target, session_id=me) is not None


@pytest.mark.anyio
async def test_write_own_allowed(root):
    assert await ps.check_write(str(root / _ME / "new.md"), session_id=_sid(_ME)) is None


@pytest.mark.anyio
async def test_dotdot_traversal_denied(root):
    """``..`` 必须先被 realpath 展开, 否则前缀判定形同虚设。"""
    sneaky = str(root / _ME / ".." / _OTHER / "f.md")
    assert await ps.check_read(sneaky, session_id=_sid(_ME)) is not None


@pytest.mark.skipif(os.name == "nt", reason="Windows 建 symlink 需要特权")
async def test_symlink_escape_denied(root):
    link = root / _ME / "link"
    link.symlink_to(root / _OTHER)
    assert await ps.check_read(str(link / "f.md"), session_id=_sid(_ME)) is not None


@pytest.mark.anyio
async def test_unowned_session_unrestricted(root):
    """SPA 手建 session 不受隔离约束(本机用户自己的会话)。"""
    assert await ps.check_read(str(root / _OTHER / "f.md"), session_id="local-abc") is None


@pytest.mark.anyio
async def test_forbidden_dirs_excludes_self_and_public(root):
    dirs = await ps.forbidden_dirs(_sid(_ME))
    assert dirs == [str(await anyio.Path(root / _OTHER).resolve())]


@pytest.mark.anyio
async def test_scan_command_blocks_other_dir(root):
    denied = await ps.scan_command(f"cat {root / _OTHER / 'f.md'}", session_id=_sid(_ME))
    assert denied is not None and _OTHER in denied


@pytest.mark.anyio
async def test_scan_command_allows_own_and_lookalike(root):
    """自己空间里名字里含别人 id 的文件不该误伤(独立路径段才算命中)。"""
    me = _sid(_ME)
    assert await ps.scan_command(f"cat {root / _ME / 'f.md'}", session_id=me) is None
    assert await ps.scan_command(f"cat {root / _ME}/{_OTHER}_notes.md", session_id=me) is None


def test_owns_session(root):
    me = _sid(_ME)
    assert ps.owns_session(_sid(_ME), session_id=me) is True
    assert ps.owns_session(_sid(_OTHER), session_id=me) is False
    assert ps.owns_session("feishu-chat-oc_x", session_id=me) is False


@pytest.mark.anyio
async def test_blocks_send(root):
    assert await ps.blocks_send(str(root / _ME / "f.md"), open_id=_ME) is False
    assert await ps.blocks_send(str(root / _OTHER / "f.md"), open_id=_ME) is True
    # 公共区产物可发。
    assert await ps.blocks_send(str(root / ps.PUBLIC_DIRNAME / "f.md"), open_id=_ME) is False
    # 认不出收件人 → 保守拒发。
    assert await ps.blocks_send(str(root / _ME / "f.md"), open_id="") is True


@pytest.mark.anyio
async def test_blocks_send_group(root):
    """群聊按 chat-<chat_id> 判权, 与群 Session 共用一块空间一致。"""
    (root / "chat-oc_g").mkdir()
    target = str(root / "chat-oc_g" / "f.md")
    assert await ps.blocks_send(target, open_id=_ME, chat_id="oc_g", chat_type="group") is False
    # 同一个文件发到私聊则越界。
    assert await ps.blocks_send(target, open_id=_ME) is True

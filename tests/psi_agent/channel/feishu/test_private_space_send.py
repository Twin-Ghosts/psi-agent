"""``[SEND:]`` 侧的隔离测试。

``[SEND:]`` 能把**任意本地路径**上传到飞书, 是最直接的外泄口: 就算路径工具全堵上,
只要 agent 猜到别人的文件名就能一句 marker 把文件发给自己。channel 是独立进程没有
``runtime_context``, 故按会话事实(open_id / chat)判权。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from psi_agent.channel.feishu.client import _send_file

_ME = "ou_me"
_OTHER = "ou_other"


@dataclass
class _Result:
    success: bool = True


@dataclass
class _FakeChannel:
    """记下每次 send 的调用, 用来断言"到底有没有发出去"。"""

    sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def send(self, chat_id: str, payload: dict[str, Any]) -> _Result:
        self.sent.append((chat_id, payload))
        return _Result(success=True)


@pytest.fixture
def space(tmp_path, monkeypatch):
    for name in (_ME, _OTHER, "public"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.png").write_bytes(b"x")
    monkeypatch.setenv("PSI_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.anyio
async def test_send_blocks_other_users_file(space):
    channel = _FakeChannel()
    await _send_file(channel, "oc_dm", str(space / _OTHER / "f.png"), open_id=_ME)
    assert channel.sent == []  # 一次都没发


@pytest.mark.anyio
async def test_send_allows_own_file(space):
    channel = _FakeChannel()
    await _send_file(channel, "oc_dm", str(space / _ME / "f.png"), open_id=_ME)
    assert len(channel.sent) == 1


@pytest.mark.anyio
async def test_send_allows_public_file(space):
    channel = _FakeChannel()
    await _send_file(channel, "oc_dm", str(space / "public" / "f.png"), open_id=_ME)
    assert len(channel.sent) == 1


@pytest.mark.anyio
async def test_send_group_scope(space):
    """群产物发到本群可以; 同一文件发到私聊则越界。"""
    (space / "chat-oc_g").mkdir()
    target = str(space / "chat-oc_g" / "f.png")

    ok = _FakeChannel()
    await _send_file(ok, "oc_g", target, open_id=_ME, chat_type="group")
    assert len(ok.sent) == 1

    blocked = _FakeChannel()
    await _send_file(blocked, "oc_dm", target, open_id=_ME)
    assert blocked.sent == []


@pytest.mark.anyio
async def test_send_unknown_recipient_blocked(space):
    """认不出收件人 → 保守拒发, 不赌。"""
    channel = _FakeChannel()
    await _send_file(channel, "oc_dm", str(space / _ME / "f.png"), open_id="")
    assert channel.sent == []


@pytest.mark.anyio
async def test_send_unrestricted_when_disabled(space, monkeypatch):
    """未启用守卫时行为与改动前一致。"""
    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    channel = _FakeChannel()
    await _send_file(channel, "oc_dm", str(space / _OTHER / "f.png"), open_id=_ME)
    assert len(channel.sent) == 1

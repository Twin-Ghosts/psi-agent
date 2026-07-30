"""会话历史隔离测试。

AppData 的 ``histories/`` 是**全局**的(不按 workspace 分), 所以 ``sessions_list`` /
``session_keyword_search`` / ``sessions_history`` 原本能列出并全文搜索**所有人的对话
原文** —— 这比生成的文件更敏感。这里钉住三条路径都只见自己。

工具模块只能在 ``sys.path`` 插入之后才导得到, 故函数内 import 是必需的。
"""

# ruff: noqa: PLC0415

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[2] / "examples" / "haitun-workspace" / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from psi_agent.session.runtime_context import runtime_scope  # noqa: E402

_ME = "ou_me"
_OTHER = "ou_other"


def _write_history(histories: Path, session_id: str, text: str) -> None:
    line = json.dumps({"role": "user", "content": text}, ensure_ascii=False)
    (histories / f"{session_id}.jsonl").write_text(line + "\n", encoding="utf-8")


@pytest.fixture
def space(tmp_path, monkeypatch):
    """隔离 root + 全局 AppData histories, 两人各一条历史。"""
    for name in (_ME, _OTHER):
        (tmp_path / name).mkdir()
    appdata = tmp_path / "appdata"
    histories = appdata / "histories"
    histories.mkdir(parents=True)
    _write_history(histories, f"feishu-{_ME}", "我的薪酬方案 机密A")
    _write_history(histories, f"feishu-{_OTHER}", "他的薪酬方案 机密B")
    monkeypatch.setenv("PSI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("PSI_APPDATA", str(appdata))
    return tmp_path


def _as_me(space):
    return runtime_scope(session_id=f"feishu-{_ME}", workspace=str(space / _ME))


@pytest.mark.anyio
async def test_sessions_list_only_shows_own(space):
    import _session_helpers as _h

    with _as_me(space):
        result = await _h.list_sessions(workspace_raw=str(space / _ME), include_gateway=False)
    ids = [row["session_id"] for row in result["sessions"]]
    assert f"feishu-{_ME}" in ids
    assert f"feishu-{_OTHER}" not in ids


@pytest.mark.anyio
async def test_keyword_search_cannot_reach_other_history(space):
    """全文搜索必须搜不到别人的对话原文。"""
    import _session_helpers as _h

    with _as_me(space):
        result = await _h.keyword_search_sessions(query="薪酬方案", workspace_raw=str(space / _ME))
    blob = json.dumps(result, ensure_ascii=False)
    assert "机密A" in blob  # 自己的搜得到
    assert "机密B" not in blob  # 别人的搜不到


@pytest.mark.anyio
async def test_explicit_session_id_denied(space):
    """显式传别人的 session_id 是绕过列表过滤的直接方式, 必须拒。"""
    import _session_helpers as _h

    with _as_me(space):
        result = await _h.get_session_history(
            session_id=f"feishu-{_OTHER}",
            workspace_raw=str(space / _ME),
            include_gateway=False,
        )
    assert result["ok"] is False
    assert "拒绝访问" in result["message"]
    assert "机密B" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.anyio
async def test_own_history_still_readable(space):
    import _session_helpers as _h

    with _as_me(space):
        result = await _h.get_session_history(
            session_id=f"feishu-{_ME}",
            workspace_raw=str(space / _ME),
            include_gateway=False,
        )
    assert "机密A" in json.dumps(result, ensure_ascii=False)


@pytest.mark.anyio
async def test_disabled_guard_keeps_old_behavior(space, monkeypatch):
    """未启用时仍能跨会话读 —— 零行为变化。"""
    import _session_helpers as _h

    monkeypatch.delenv("PSI_WORKSPACE_ROOT", raising=False)
    with _as_me(space):
        result = await _h.get_session_history(
            session_id=f"feishu-{_OTHER}",
            workspace_raw=str(space / _ME),
            include_gateway=False,
        )
    assert "机密B" in json.dumps(result, ensure_ascii=False)

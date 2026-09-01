"""adopt 已存在 session 时, workspace 与派生值不符要**看得见**; 且这类错状态会自我延续。

实测背景(生产 63 个飞书会话): 15 个的 workspace 指向 ``/workspace`` **根目录**而非各自的
``ou_*`` 子目录, 其中 14 个是 ``feishu-ou_*`` 形状 —— 本该有自己的目录, 抽查 7 个那些目录
**一个都不存在**。后果是这 14 个人的 agent 产出全写进全公司可见的公共区, 根目录已散着约
290 个混放文件。

**延续机制**已由对照实验坐实, 就在 ``route()`` 的 adopt 分支: ``if self._sm.has(sid)`` 在
``ws = workspace or self._workspace_for(key)`` **之前**就 return 了。喂一个 workspace 指向
根目录的已存在 session, adopt 直接继承根目录, spawn 不发生, ``workspace_for`` 压根不被调用;
同一份代码在干净状态下走 spawn 则正确派生到 ``ou_*`` 子目录。于是错状态永不自愈。

本模块只钉「可见」: adopt **仍然照旧发生**(不抛错、不改 workspace —— 纠正数据是另一个独立
决定, 这里一行数据都不动), 但错位时必须留下一条 WARNING。阴性用例同等重要: 健康 session
误报会淹掉真告警, 生产 63 个会话里 48 个是健康的。
"""

from __future__ import annotations

import os
import re
from typing import Any

import anyio
import pytest
from loguru import logger

from psi_agent.gateway.feishu._feishu_manager import FeishuManager
from psi_agent.runtime._ai_manager import AIManager
from psi_agent.runtime._session_manager import SessionManager


class _Captured:
    """收 loguru 的 WARNING 文本。

    用 ``logger.add`` 而不是 pytest 的 ``caplog``: 本仓库用 loguru, 它默认不走 stdlib
    logging, ``caplog`` 会**一条都收不到**, 而断言「没有告警」的阴性用例照样绿 —— 假阴性,
    正是这里最不能出的错。与 ``test_feishu_dev_bypass_startup.py`` 同一写法。
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._handle = logger.add(lambda m: self.messages.append(m.record["message"]), level="WARNING")

    def stop(self) -> str:
        logger.remove(self._handle)
        return "\n".join(self.messages)


async def _make_managers(tg: object) -> tuple[AIManager, SessionManager]:
    am = AIManager(_prefix="gw-test", _tg=tg)
    sm = SessionManager(_aim=am, _prefix="gw-test", _tg=tg)
    await am.create(provider="o", model="m", api_key="k", base_url="b", id="ai1")
    return am, sm


async def _drain(sm: SessionManager, am: AIManager) -> None:
    for info in await sm.list_all():
        await sm.delete(info.id)
    for info in await am.list_all():
        await am.delete(info.id)


async def _adopt_with_existing_workspace(
    tmp_path: str,
    *,
    key: str,
    existing_workspace: str,
    chat_id: str = "",
    chat_type: str = "",
) -> tuple[str, str, str]:
    """先按 *existing_workspace* 建好 session, 再让 ``route`` 去 adopt 它。

    返回 ``(告警文本, adopt 后的 workspace, session_id)``。刻意**不 mock**
    ``SessionManager``: 「adopt 继承了旧 workspace」这件事只有真 session 才说得清, 而
    ``get_workspace`` 的返回正是判据本身。

    路由表故意留空(新建 ``FeishuManager``), 复现的正是生产那条路径 —— 进程重启后
    ``_routes`` 是空的, state 恢复出 session, 于是每次 route 都走 adopt 分支。
    """
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make_managers(tg)
        fm = FeishuManager(_sm=sm, _ai_id="ai1", _workspace_root=str(tmp_path))
        sid = fm.session_id_for(key)
        # 先用「错的」(或对的) workspace 把 session 建出来 —— 模拟 state 恢复的结果。
        await sm.create(ai_id="ai1", id=sid, workspace=existing_workspace)
        assert sm.has(sid)

        cap = _Captured()
        try:
            open_id = "" if key.startswith("chat:") else key
            _, routed_sid = await fm.route(open_id, chat_id=chat_id, chat_type=chat_type)
        finally:
            blob = cap.stop()
        assert routed_sid == sid, "adopt 应当返回同一个 session_id"
        return blob, sm.get_workspace(sid), sid
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_adopt_warns_when_workspace_points_at_root(tmp_path: Any) -> None:
    """阳性: 已存在 session 的 workspace 是**根目录** → 一条 WARNING, 四个字段齐全。

    四个字段缺一不可: 少了路由键就不知道是谁, 少了 session_id 就没法去 ``/sessions`` 核对,
    少了实际 workspace 就看不出错在哪, 少了应有 workspace 就不知道该改成什么。
    """
    root = str(tmp_path)
    key = "ou_6c30c11b76b15e42a7870e0686733c0f"
    blob, workspace_after, sid = await _adopt_with_existing_workspace(root, key=key, existing_workspace=root)

    assert blob, "workspace 指向根目录却一声不响 —— 这正是生产 14 个人查不出来的原因"

    # 四个字段**逐个按标签**断言, 不用裸子串。变异复核实测: 裸子串写法里有两条根本不吃劲 ——
    # ``key in blob`` 被 session_id 满足 (session_id 恰好含 open_id), ``root in blob`` 被
    # expected_workspace 满足 (它以 root 开头)。于是把 ``key=`` 和 ``actual_workspace=``
    # 整段从告警里删掉, 用例照样全绿。按标签取值之后, 删任何一个字段本条都红。
    expected = os.path.join(root, key)
    fields = dict(re.findall(r"(\w+)='([^']*)'", blob))
    assert fields.get("key") == key, f"告警没带路由键, 看不出是谁: {blob!r}"
    assert fields.get("session") == sid, f"告警没带 session_id, 没法去核对: {blob!r}"
    # 实际值与应有值都要在, 且必须是两个**不同**的路径 —— 只印一个的话读日志的人无法判断
    # 到底哪个是错的, 也不知道该改成什么。
    assert fields.get("actual_workspace") == root, f"告警没带实际 workspace: {blob!r}"
    assert fields.get("expected_workspace") == expected, f"告警没带本应的 workspace: {blob!r}"
    assert fields["actual_workspace"] != fields["expected_workspace"]

    # 路径不得被 ``!r`` 转义: Windows 上 repr 会把 ``\`` 变成 ``\\``, 印出来的路径没法直接
    # 复制去 ls, 而这条告警的唯一用途就是让人拿着这两个路径去核对。
    assert "\\\\" not in blob, f"路径被 repr 转义了, 没法复制粘贴: {blob!r}"

    # 本卡一行数据都不改: adopt 仍然成功, workspace 保持原样(纠正是后续独立决定)。
    assert workspace_after == root, "本卡不该改 workspace, 只该让它可见"


@pytest.mark.anyio
async def test_adopt_is_silent_when_workspace_matches(tmp_path: Any) -> None:
    """阴性(重要): workspace 正是派生值 → **一条 WARNING 都没有**。

    误报会淹掉真告警: 生产 63 个会话里 48 个是健康的, 每次 route 都报一条的话, 那 14 条真
    告警就找不着了。参照真实健康样本 —— ``ou_6c30c11b76b15e42a7870e0686733c0f`` 的
    workspace 是 ``/workspace/ou_6c30c11b76b15e42a7870e0686733c0f``, 与派生值一致。
    """
    root = str(tmp_path)
    key = "ou_6c30c11b76b15e42a7870e0686733c0f"
    healthy = os.path.join(root, key)
    blob, workspace_after, _ = await _adopt_with_existing_workspace(root, key=key, existing_workspace=healthy)

    assert blob == "", f"健康 session 被误报, 真告警会被淹掉: {blob!r}"
    assert workspace_after == healthy


@pytest.mark.anyio
async def test_adopt_is_silent_for_group_chat_workspace(tmp_path: Any) -> None:
    """阴性: 群聊合法形状 ``chat-<chat_id>`` 不误报。

    群聊派生的是 ``<root>/chat-<chat_id>``, 与私聊的 ``<root>/<open_id>`` 是两条规则。判据
    若拿私聊规则去套群聊, 全部群会话都会报错位。
    """
    root = str(tmp_path)
    chat_id = "oc_team_alpha"
    healthy = os.path.join(root, f"chat-{chat_id}")
    blob, workspace_after, sid = await _adopt_with_existing_workspace(
        root,
        key=f"chat:{chat_id}",
        existing_workspace=healthy,
        chat_id=chat_id,
        chat_type="group",
    )

    assert sid == f"feishu-chat-{chat_id}"
    assert blob == "", f"群聊合法 workspace 被误报: {blob!r}"
    assert workspace_after == healthy


@pytest.mark.anyio
async def test_adopt_is_silent_for_private_space_workspace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """阴性: 私密区合法形状 ``.private/<open_id>`` 不误报。

    白名单用户派生到 ``<root>/.private/<open_id>``, 是第三条规则。判据必须走
    ``workspace_for`` 本身而不是重拼路径, 否则这一支必被漏掉(重实现总会漏掉某一支)。
    """
    root = str(tmp_path)
    key = "ou_secret_person"
    monkeypatch.setenv("PSI_PRIVATE_OPEN_IDS", key)
    healthy = os.path.join(root, ".private", key)
    blob, workspace_after, _ = await _adopt_with_existing_workspace(root, key=key, existing_workspace=healthy)

    assert blob == "", f"私密区合法 workspace 被误报: {blob!r}"
    assert workspace_after == healthy


@pytest.mark.anyio
async def test_adopt_warning_survives_path_spelling_differences(tmp_path: Any) -> None:
    """同一个目录的不同写法(尾斜杠/``.`` 段)不算错位。

    ``<root>/ou_x/`` 与 ``<root>/./ou_x`` 和派生值是同一个目录。按裸字符串比会把它们判成
    错位 —— 那是纯噪音, 且会让阴性判据在 Windows(大小写、分隔符)上随环境变红。
    """
    root = str(tmp_path)
    key = "ou_spelling"
    odd = os.path.join(root, ".", key) + os.sep
    blob, _, _ = await _adopt_with_existing_workspace(root, key=key, existing_workspace=odd)

    assert blob == "", f"同一目录的不同写法被判成错位: {blob!r}"

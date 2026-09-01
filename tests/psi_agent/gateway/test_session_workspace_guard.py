"""某前缀的 session id 必须显式带一个 root 之下的 workspace, 否则**拒绝创建**。

治的是那 14 个会话的**初始成因**(成因本身仍未定, 见 ``test_feishu_workspace_drift.py``):
一定有某条路径拿 ``feishu-ou_*`` 形状的 id 建 session 却没给 workspace, 于是
``SessionManager.create`` 里 ``workspace.strip() or self._default_workspace`` 静默吃下
``--default-workspace``(生产上就是 ``/workspace`` 根目录), 那个人的 agent 产出从此全写进
全公司可见的公共区。``route()`` 这条路吃不到那个兜底(它的 ``ws`` 永远非空), 所以元凶是别处。

**不去猜是谁写的, 直接让它过不去。** 判据放在 ``SessionManager.create`` —— 所有建 session
的路径都必经此处, 这是「不管那条未知路径是谁」这个要求唯一能落实的位置。

**内核不认识「飞书」**: 前缀与 root 是两个注入字段 (``_guarded_id_prefix`` /
``_guarded_workspace_root``), 产品名由 ``Gateway.run`` 填。仓库已有「微内核反向依赖产品层
硬编码」的账, 不在这里再记一笔。两个字段留空 → 判据完全不存在, 与改动前逐字节等价。
"""

from __future__ import annotations

import os
from typing import Any

import anyio
import pytest

from psi_agent.runtime._ai_manager import AIManager
from psi_agent.runtime._session_manager import SessionManager

_PREFIX = "feishu-"


async def _make(tg: object, *, root: str, default_workspace: str) -> tuple[AIManager, SessionManager]:
    am = AIManager(_prefix="gw-test", _tg=tg)
    sm = SessionManager(
        _aim=am,
        _prefix="gw-test",
        _tg=tg,
        _default_workspace=default_workspace,
        _guarded_id_prefix=_PREFIX,
        _guarded_workspace_root=root,
    )
    await am.create(provider="o", model="m", api_key="k", base_url="b", id="ai1")
    return am, sm


async def _drain(sm: SessionManager, am: AIManager) -> None:
    for info in await sm.list_all():
        await sm.delete(info.id)
    for info in await am.list_all():
        await am.delete(info.id)


@pytest.mark.anyio
async def test_explicit_workspace_under_root_is_accepted(tmp_path: Any) -> None:
    """显式 workspace 在 root 之下 → 通过 (这是健康路径, 生产 48 个会话走的就是它)。"""
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        good = os.path.join(root, "ou_alice")
        info = await sm.create(ai_id="ai1", id="feishu-ou_alice", workspace=good)
        assert info.workspace == good
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_missing_workspace_is_rejected_instead_of_eating_the_default(tmp_path: Any) -> None:
    """不给 workspace → 拒绝, **不**静默落到 ``--default-workspace``。

    这条正是那 14 个会话的成因形状。断言里同时钉住「没建出来」: 只报错却仍留一个跑在根目录
    的 session, 与改动前没有区别。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        # 断言的是**这一支**的报错文案, 不是笼统的 "workspace"。变异复核实测: 把「空 workspace
        # 就拒绝」这个分支整个去掉, 空串会掉到下一条判据 (``is_strictly_under("", root)`` 恒
        # 假) 并抛出另一句话 —— 于是 ``match="workspace"`` 照样绿, 这支分支等于没被测到。
        # 两条判据一前一后确实是有意的纵深, 但纵深不该让判据本身失去分辨力: 空 workspace 与
        # 「给了但给错了」是两个不同的调用方错误, 报错必须能分开, 否则排查那条未知路径时读到
        # 的是错的线索。
        with pytest.raises(ValueError, match="refusing to fall back to the default workspace"):
            await sm.create(ai_id="ai1", id="feishu-ou_alice")
        assert not sm.has("feishu-ou_alice"), "拒绝了却还是把 session 建了出来"
        assert await sm.list_all() == []
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_workspace_equal_to_root_is_rejected(tmp_path: Any) -> None:
    """workspace 恰好**等于** root → 拒绝。

    这是生产实际错状态的形状: 那 15 个指的就是 ``/workspace`` 本身。「在 root 之下」不含
    root 自己 —— 含了的话这条判据对真正发生过的那个错法完全无效。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        with pytest.raises(ValueError, match="workspace"):
            await sm.create(ai_id="ai1", id="feishu-ou_alice", workspace=root)
        assert not sm.has("feishu-ou_alice")
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_workspace_outside_root_is_rejected(tmp_path: Any) -> None:
    """workspace 指向 root **之外** → 拒绝, 且 ``..`` 穿越回不来。

    第二个断言防的是「按字符串前缀判断」这种写法: ``<root>/../elsewhere`` 的字面量以 root
    开头, 但归一化之后并不在 root 下。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        outside = os.path.join(str(tmp_path), "elsewhere")
        with pytest.raises(ValueError, match="workspace"):
            await sm.create(ai_id="ai1", id="feishu-ou_alice", workspace=outside)

        traversal = os.path.join(root, "..", "elsewhere", "ou_alice")
        with pytest.raises(ValueError, match="workspace"):
            await sm.create(ai_id="ai1", id="feishu-ou_bob", workspace=traversal)
        assert await sm.list_all() == []
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_sibling_directory_sharing_the_root_prefix_is_rejected(tmp_path: Any) -> None:
    """``<root>-evil`` 这种同前缀兄弟目录 → 拒绝。

    裸 ``startswith(root)`` 会放它过去, 而它压根不在 root 里。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        with pytest.raises(ValueError, match="workspace"):
            await sm.create(ai_id="ai1", id="feishu-ou_alice", workspace=root + "-evil")
        assert await sm.list_all() == []
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_group_chat_and_private_space_shapes_are_accepted(tmp_path: Any) -> None:
    """两种合法形状都不被挡掉: 群聊 ``chat-<chat_id>``、私密区 ``.private/<open_id>``。

    它们与私聊的 ``<root>/<open_id>`` 是三条不同的派生规则 (见
    ``FeishuManager.workspace_for``)。判据若只认「root 的直接子目录里长得像 open_id 的那种」,
    群聊和私密区就全被拒 —— 那是把功能挡死, 比原 bug 更糟。私密区还多一层子目录, 顺带钉住
    判据认的是「在 root 之下」而不是「root 的直接子目录」。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        group = os.path.join(root, "chat-oc_team")
        private = os.path.join(root, ".private", "ou_secret")
        assert (await sm.create(ai_id="ai1", id="feishu-chat-oc_team", workspace=group)).workspace == group
        assert (await sm.create(ai_id="ai1", id="feishu-ou_secret", workspace=private)).workspace == private
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_unguarded_ids_keep_eating_the_default_workspace(tmp_path: Any) -> None:
    """不带该前缀的 id **一律不受影响** —— 兜底照旧。

    ToC 装机版与 SPA 手建的会话就靠这个兜底 (桌面端只有一个 workspace, 每个会话都该落它)。
    判据若漏掉前缀这个条件, 表现是装机版建会话直接失败。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        info = await sm.create(ai_id="ai1", id="some-spa-session")
        assert info.workspace == root, "非受管 id 的 --default-workspace 兜底被误伤"
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_guard_is_inert_when_not_configured(tmp_path: Any) -> None:
    """两个字段留空 → 判据不存在, 与改动前逐字节等价。

    「默认关」是判据的**缺席**而不是一个配置值 —— 这样本条才断言得硬 (同 ``PSI_DEBUG_MODULES``
    那条设计)。开发时不配 ``--feishu-workspace-root`` 的进程走的正是这条。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am = AIManager(_prefix="gw-test", _tg=tg)
        sm = SessionManager(_aim=am, _prefix="gw-test", _tg=tg, _default_workspace=root)
        await am.create(provider="o", model="m", api_key="k", base_url="b", id="ai1")
        info = await sm.create(ai_id="ai1", id="feishu-ou_alice")
        assert info.workspace == root
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_restore_is_exempt_so_existing_sessions_are_not_migrated(tmp_path: Any) -> None:
    """``skip_workspace_guard=True`` 放行 —— **state 恢复专用**, 且只有它用。

    恢复是「把已经存在的东西重新拉起来」, 不是「创建」。生产那 14 个会话的 workspace 正是
    根目录, 拿判据去挡恢复的后果是它们起不来, 下一条消息触发 spawn 时按正确规则派生 ——
    那等于**迁移了这 14 个人**: 历史虽然按 session_id 存在 appdata 里不会丢, 但他们过去的
    产出都留在根目录那约 290 个混放文件里, 而 agent 从此看不见自己的旧文件。是否迁移是一个
    独立决定, 本卡明确不做, 所以恢复必须放行, 由 adopt 那条 WARNING 负责让它们持续可见。
    """
    root = os.path.join(str(tmp_path), "workspace")
    tg = anyio.create_task_group()
    await tg.__aenter__()
    try:
        am, sm = await _make(tg, root=root, default_workspace=root)
        # 复现生产错状态: workspace 就是根目录本身。
        info = await sm.create(ai_id="ai1", id="feishu-ou_alice", workspace=root, skip_workspace_guard=True)
        assert info.workspace == root, "恢复出来的 workspace 被悄悄改掉了 —— 本卡不迁移任何 session"
    finally:
        await _drain(sm, am)
        await tg.__aexit__(None, None, None)

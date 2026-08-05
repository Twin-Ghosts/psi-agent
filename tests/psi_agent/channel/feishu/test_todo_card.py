"""Multi-use (TODO list) card callbacks: per-row consumption instead of per-card."""

from __future__ import annotations

import json

import anyio
import anyio.lowlevel
import pytest

from psi_agent.channel.feishu import _card_store
from psi_agent.channel.feishu._card_action import (
    CardActionBatcher,
    _batched_card_context,
    _consumed_card_content,
)
from psi_agent.channel.feishu._card_store import (
    card_claim_guard,
    peek_card_multi_use,
    pop_card_snapshot,
    rejected_claim_count,
    rewrite_card_snapshot,
    save_card_snapshot,
)


def _row(index: int, title: str) -> list[dict]:
    return [
        {"tag": "markdown", "content": f"○ **{title}**"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"○ 标记完成 {title}"},
                    "value": {"action": f"todo_tick_{index}", "todo_title": title},
                }
            ],
        },
    ]


def _todo_card() -> dict:
    elements: list[dict] = [{"tag": "markdown", "content": "进度: 0/2 已完成"}]
    elements.extend(_row(0, "写周报"))
    elements.extend(_row(1, "改文档"))
    return {"config": {"wide_screen_mode": True}, "elements": elements}


def _button_values(card: dict) -> list[dict]:
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            value = node.get("value")
            if node.get("tag") == "button" and isinstance(value, dict):
                found.append(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(card)
    return found


def test_multi_use_keeps_other_rows_interactive() -> None:
    card = _todo_card()
    consumed = _consumed_card_content(card, {"action": "todo_tick_0", "todo_title": "写周报"}, multi_use=True)
    assert consumed is not None
    remaining = [value["action"] for value in _button_values(consumed)]
    assert remaining == ["todo_tick_1"], "只有被点的那行该失去按钮"
    rendered = str(consumed)
    assert "~~" in rendered and "●" in rendered, "已完成项应为实心+删除线"


def test_single_use_still_strips_every_action() -> None:
    card = _todo_card()
    consumed = _consumed_card_content(card, {"action": "todo_tick_0", "todo_title": "写周报"})
    assert consumed is not None
    assert _button_values(consumed) == [], "默认单次卡必须清空所有交互元素"


@pytest.mark.anyio
async def test_two_rows_tick_independently(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_multi", _todo_card(), appdata, action_handlers={"todo_tick_0": "h"}, multi_use=True)
    assert await peek_card_multi_use("om_multi", appdata) is True

    first = await pop_card_snapshot("om_multi", appdata, action_id="todo_tick_0")
    assert first.status == "claimed"
    second = await pop_card_snapshot("om_multi", appdata, action_id="todo_tick_1")
    assert second.status == "claimed", "另一行不该被第一行的点击retire"
    assert second.snapshot is not None and second.snapshot.multi_use is True


@pytest.mark.anyio
async def test_repeat_tick_on_same_row_is_rejected(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_repeat", _todo_card(), appdata, multi_use=True)
    assert (await pop_card_snapshot("om_repeat", appdata, action_id="todo_tick_0")).status == "claimed"
    again = await pop_card_snapshot("om_repeat", appdata, action_id="todo_tick_0")
    assert again.status == "already_consumed", "同一行重复点必须恰好被拒一次"


@pytest.mark.anyio
async def test_concurrent_ticks_on_same_row_admit_exactly_one(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_race", _todo_card(), appdata, multi_use=True)
    statuses: list[str] = []

    async def tick() -> None:
        claim = await pop_card_snapshot("om_race", appdata, action_id="todo_tick_0")
        statuses.append(claim.status)

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(tick)

    assert statuses.count("claimed") == 1, f"并发点同一行只能有一个成功, got {statuses}"


@pytest.mark.anyio
async def test_rewrite_persists_ticked_state_for_next_tick(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_rw", _todo_card(), appdata, multi_use=True)
    ticked = _consumed_card_content(_todo_card(), {"action": "todo_tick_0", "todo_title": "写周报"}, multi_use=True)
    assert ticked is not None
    assert await rewrite_card_snapshot("om_rw", ticked, appdata) is True

    claim = await pop_card_snapshot("om_rw", appdata, action_id="todo_tick_1")
    assert claim.status == "claimed" and claim.snapshot is not None
    actions = [value["action"] for value in _button_values(claim.snapshot.card)]
    assert actions == ["todo_tick_1"], "回写后第二次点击必须看到第一行已完成"


@pytest.mark.anyio
async def test_single_use_snapshot_rejects_rewrite_and_retires_whole_card(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_single", _todo_card(), appdata)
    assert await peek_card_multi_use("om_single", appdata) is False
    assert await rewrite_card_snapshot("om_single", _todo_card(), appdata) is False, "单次卡不允许回写"
    # action_id 传了也不该走 per-action 分支: 快照非 multi_use。
    assert (await pop_card_snapshot("om_single", appdata, action_id="todo_tick_0")).status == "claimed"
    assert (await pop_card_snapshot("om_single", appdata, action_id="todo_tick_1")).status == "already_consumed"


@pytest.mark.anyio
async def test_mode_peek_is_safe_for_bad_and_missing_ids(tmp_path) -> None:
    appdata = str(tmp_path)
    # 模式探测在真正声明之前跑, 不能因为一个畸形 message_id 就把整个回调打挂。
    assert await peek_card_multi_use("om/../etc", appdata) is False
    assert await peek_card_multi_use("om_never_sent", appdata) is False
    # 但 pop 仍必须拒绝畸形 id (路径穿越防线不能因为这次改动松掉)。
    with pytest.raises(ValueError):
        await pop_card_snapshot("om/../etc", appdata)


@pytest.mark.anyio
async def test_row_without_canonical_action_falls_back_to_whole_card(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_noaction", _todo_card(), appdata, multi_use=True)
    # action_id=None 表示这次回调没有可用的规范 action, 只能退回整卡去重。
    assert (await pop_card_snapshot("om_noaction", appdata, action_id=None)).status == "claimed"
    assert (await pop_card_snapshot("om_noaction", appdata, action_id=None)).status == "already_consumed"


@pytest.mark.anyio
async def test_weird_action_id_gets_a_safe_distinct_tombstone(tmp_path) -> None:
    appdata = str(tmp_path)
    await save_card_snapshot("om_weird", _todo_card(), appdata, multi_use=True)
    # 带路径分隔符/空格的 action 不能污染文件名, 也不能和别的 action 撞成同一个墓碑。
    first = await pop_card_snapshot("om_weird", appdata, action_id="../evil tick")
    second = await pop_card_snapshot("om_weird", appdata, action_id="../evil tick")
    third = await pop_card_snapshot("om_weird", appdata, action_id="another/../one")
    assert (first.status, second.status, third.status) == ("claimed", "already_consumed", "claimed")


# -- 连点合并 ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_ticks_during_inflight_turn_collapse_into_one_reply() -> None:
    """连点 5 次不该换来 5 条回复: 在途回合把后续点击吸收成一个批。"""
    batcher = CardActionBatcher()
    started = anyio.Event()
    release = anyio.Event()
    batches: list[list[str]] = []

    async def run(batch: list[str]) -> None:
        batches.append(batch)
        started.set()
        await release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(batcher.submit, "om_1:user_a", "click_0", run)
        await started.wait()  # 第一个回合已在跑, 后面几次都算"在途中点的"
        for i in range(1, 5):
            tg.start_soon(batcher.submit, "om_1:user_a", f"click_{i}", run)
        await anyio.sleep(0.05)
        release.set()

    # 第一批只有 click_0, 其余 4 次合并进第二批 —— 共 2 个回合而不是 5 个。
    assert len(batches) == 2
    assert batches[0] == ["click_0"]
    assert sorted(batches[1]) == ["click_1", "click_2", "click_3", "click_4"]


@pytest.mark.anyio
async def test_batched_context_keeps_every_click_payload() -> None:
    """合并不等于丢弃: 每一次点击的 payload 都要进同一个回合。"""
    assert _batched_card_context(["only"]) == "only"
    merged = _batched_card_context(["a", "b", "c"])
    assert 'count="3"' in merged
    assert all(part in merged for part in ("a", "b", "c"))


@pytest.mark.anyio
async def test_different_clickers_do_not_share_a_batch() -> None:
    """群卡两个人各点各的, 必须各自回合、各自回复。"""
    batcher = CardActionBatcher()
    keys: list[str] = []

    async def run(batch: list[str]) -> None:
        keys.extend(batch)
        await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        tg.start_soon(batcher.submit, "om_1:user_a", "from_a", run)
        tg.start_soon(batcher.submit, "om_1:user_b", "from_b", run)

    assert sorted(keys) == ["from_a", "from_b"]


@pytest.mark.anyio
async def test_batcher_recovers_after_a_failed_turn() -> None:
    """一个回合炸了不能把这张卡的后续点击永久锁死。"""
    batcher = CardActionBatcher()

    async def boom(batch: list[str]) -> None:
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        await batcher.submit("om_1:user_a", "click_0", boom)

    seen: list[str] = []

    async def ok(batch: list[str]) -> None:
        seen.extend(batch)

    await batcher.submit("om_1:user_a", "click_1", ok)
    assert seen == ["click_1"]  # 没有重放上一轮失败的 click_0


# -- 回写竞态 -------------------------------------------------------------------


@pytest.mark.anyio
async def test_interleaved_rewrites_do_not_undo_each_other(tmp_path) -> None:
    """两行同时勾: 交错的读-改-写不能让后写的把先写的完成状态覆盖回未完成。

    临界区必须同时罩住读和写 —— 只锁 rewrite 时两边都会读到未勾过的原卡, 第二次
    回写就抹掉第一次的成果。
    """
    appdata = str(tmp_path)
    await save_card_snapshot("om_race", _todo_card(), appdata=appdata, multi_use=True)

    async def tick(index: int, title: str) -> None:
        action_id = f"todo_tick_{index}"
        async with card_claim_guard("om_race"):
            claim = await pop_card_snapshot("om_race", appdata, action_id=action_id)
            assert claim.status == "claimed"
            assert claim.snapshot is not None
            # 读到之后让出控制权 —— 没有锁的话另一条正好挤进来读同一份快照。
            await anyio.lowlevel.checkpoint()
            ticked = _consumed_card_content(
                claim.snapshot.card, {"action": action_id, "todo_title": title}, multi_use=True
            )
            assert ticked is not None
            assert await rewrite_card_snapshot("om_race", ticked, appdata)

    async with anyio.create_task_group() as tg:
        tg.start_soon(tick, 0, "写周报")
        tg.start_soon(tick, 1, "改文档")

    final = await pop_card_snapshot("om_race", appdata, action_id="todo_tick_probe")
    assert final.status == "claimed" and final.snapshot is not None
    rendered = json.dumps(final.snapshot.card, ensure_ascii=False)
    # 两条都得留在"已完成"态 (实心 + 删除线), 并且都不再带按钮。
    assert rendered.count("● ~~") == 2, "两行都必须保住已完成态, 后写的不能覆盖先写的"
    assert "写周报~~" in rendered
    assert "改文档~~" in rendered
    assert _button_values(final.snapshot.card) == []


@pytest.mark.anyio
async def test_rejected_claims_are_counted_for_diagnostics(tmp_path) -> None:
    """被墓碑拒掉要留下可查的上下文, 否则连点和跨进程重投在日志里没法区分。"""
    appdata = str(tmp_path)
    await save_card_snapshot("om_count", _todo_card(), appdata=appdata, multi_use=True)

    first = await pop_card_snapshot("om_count", appdata, action_id="todo_tick_0")
    assert first.status == "claimed"
    assert first.rejected_count == 0

    second = await pop_card_snapshot("om_count", appdata, action_id="todo_tick_0")
    assert second.status == "already_consumed"
    assert second.rejected_action_id == "todo_tick_0"
    assert second.rejected_count >= 1
    assert rejected_claim_count("om_count") >= 1


def test_rejection_counts_stay_bounded(monkeypatch) -> None:
    """诊断计数不能变成内存泄漏: 每张卡一个条目, 长跑进程会无界增长。"""
    monkeypatch.setattr(_card_store, "_REJECTED_CLAIMS", {})
    monkeypatch.setattr(_card_store, "_MAX_TRACKED_REJECTIONS", 4)
    for index in range(20):
        _card_store._record_rejection(f"om_{index}")

    assert len(_card_store._REJECTED_CLAIMS) == 4
    # 淘汰的是最老的卡, 最近几张仍可查。
    assert rejected_claim_count("om_19") == 1
    assert rejected_claim_count("om_0") == 0
    # 同一张卡重复被拒仍然累加, 不会因为限长而丢掉计数。
    assert _card_store._record_rejection("om_19") == 2

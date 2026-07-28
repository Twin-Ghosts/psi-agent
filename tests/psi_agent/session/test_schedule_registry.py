from __future__ import annotations

import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from croniter import croniter

from psi_agent._yaml import parse_yaml_header
from psi_agent.session import schedule_registry as schedule_registry_module
from psi_agent.session.conversation import Conversation
from psi_agent.session.schedule_registry import ACTIVATE_ALL, Schedule, ScheduleEntry, ScheduleRegistry
from psi_agent.session.tool_registry import FileEntry, ToolFunction, ToolRegistry

# ── helpers ───────────────────────────────────────────────────────────────────


class _MockAgent:
    _lock = anyio.Lock()

    async def run(self, msg: object, **_kwargs: object) -> Any:  # type: ignore[return]
        if False:
            yield

    def set_pending_schedule_chunks(self, chunks: object) -> None:
        pass


class _RaisingAgent:
    _lock = anyio.Lock()

    async def run(self, msg: object, **_kwargs: object) -> Any:  # type: ignore[return]
        if False:
            yield
        raise RuntimeError("test error")

    def set_pending_schedule_chunks(self, chunks: object) -> None:
        pass


# ── Schedule dataclass ────────────────────────────────────────────────────────


def test_schedule_dataclass_fields() -> None:
    s = Schedule(name="test", cron="* * * * *", task_content="Run")
    assert s.name == "test"
    assert s.cron == "* * * * *"
    assert s.task_content == "Run"
    assert s.visibility == "display"


# ── ScheduleEntry ─────────────────────────────────────────────────────────────


def test_schedule_entry_defaults() -> None:
    s = Schedule(name="t", cron="* * * * *", task_content="x")
    entry = ScheduleEntry(file_hash="abc", schedule=s)
    assert entry.file_hash == "abc"
    assert entry.schedule is s
    assert entry.fresh is False


def test_schedule_entry_fresh_flag() -> None:
    s = Schedule(name="t", cron="* * * * *", task_content="x")
    entry = ScheduleEntry(file_hash="abc", schedule=s, fresh=True)
    assert entry.fresh is True


# ── _load_from_dir ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_load_schedule_with_yaml_header(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "daily-report"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        textwrap.dedent("""\
        ---
        name: daily-report
        cron: "0 12 * * *"
        ---
        请生成项目进展日报。
    """),
        encoding="utf-8",
    )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    assert len(files) == 1
    entry = next(iter(files.values()))
    assert entry.fresh is True
    assert entry.schedule.name == "daily-report"
    assert entry.schedule.cron == "0 12 * * *"
    assert entry.schedule.visibility == "display"
    assert "请生成项目进展日报" in entry.schedule.task_content


@pytest.mark.anyio
async def test_load_schedule_visibility_silent(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "heartbeat"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        textwrap.dedent("""\
        ---
        name: heartbeat
        cron: "*/30 * * * *"
        visibility: silent
        ---
        Respond with HEARTBEAT_OK
    """),
        encoding="utf-8",
    )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    entry = next(iter(files.values()))
    assert entry.schedule.visibility == "silent"


@pytest.mark.anyio
async def test_load_schedule_missing_yaml_header(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "no-header"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text("Just a task without header.", encoding="utf-8")

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    assert len(files) == 0


@pytest.mark.anyio
async def test_load_multiple_schedules(tmp_path: Path) -> None:
    for name in ["daily", "weekly"]:
        d = tmp_path / "schedules" / name
        await anyio.Path(d).mkdir(parents=True)
        await anyio.Path(d / "TASK.md").write_text(
            f'---\nname: {name}\ncron: "0 12 * * *"\n---\nTask: {name}', encoding="utf-8"
        )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    assert len(files) == 2
    names = {entry.schedule.name for entry in files.values()}
    assert names == {"daily", "weekly"}


@pytest.mark.anyio
async def test_load_schedules_missing_dir(tmp_path: Path) -> None:
    files = await ScheduleRegistry._load_from_dir(tmp_path / "nonexistent")
    assert len(files) == 0


@pytest.mark.anyio
async def test_load_schedule_missing_name(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "bad"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text('---\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8")

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    assert len(files) == 0


@pytest.mark.anyio
async def test_load_schedule_invalid_cron_skipped(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "bad"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        '---\nname: bad\ncron: "not a cron"\n---\nTask', encoding="utf-8"
    )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    assert len(files) == 0


@pytest.mark.anyio
async def test_load_from_dir_skip_unchanged(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    old_files = files

    result = await ScheduleRegistry._load_from_dir(tmp_path / "schedules", old_files)
    assert len(result) == 1
    entry = next(iter(result.values()))
    assert entry.fresh is False
    assert entry.schedule.name == "daily"


@pytest.mark.anyio
async def test_load_from_dir_imports_changed(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    old_files = files

    await anyio.Path(schedules_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 6 * * *"\n---\nUpdated task', encoding="utf-8"
    )

    result = await ScheduleRegistry._load_from_dir(tmp_path / "schedules", old_files)
    entry = next(iter(result.values()))
    assert entry.fresh is True
    assert entry.schedule.cron == "0 6 * * *"


# ── ScheduleRegistry factory ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_registry_load(tmp_path: Path) -> None:
    sched_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(tmp_path / "schedules")
    assert len(sr.schedules) == 1
    assert sr.schedules[0].name == "daily"
    assert sr._work_dir == tmp_path / "schedules"


@pytest.mark.anyio
async def test_registry_load_missing_dir(tmp_path: Path) -> None:
    sr = await ScheduleRegistry.load(tmp_path / "nonexistent")
    assert sr.schedules == []
    assert sr._work_dir == tmp_path / "nonexistent"


# ── ScheduleRegistry.refresh ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_refresh_no_work_dir() -> None:
    sr = ScheduleRegistry()
    assert await sr.refresh() == {}


@pytest.mark.anyio
async def test_refresh_no_task_group() -> None:
    sched_dir = Path("/tmp")
    sr = ScheduleRegistry(work_dir=sched_dir)
    assert await sr.refresh() == {}


@pytest.mark.anyio
async def test_refresh_adds_new_schedule(tmp_path: Path) -> None:
    sr = await ScheduleRegistry.load(tmp_path / "nonexistent")
    sched_dir = tmp_path / "schedules" / "extra"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: extra\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )
    sr._work_dir = tmp_path / "schedules"

    agent = _MockAgent()
    sr._agent = cast(Any, agent)
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        added = await sr.refresh()
        assert added == {"extra": "added"}
        tg.cancel_scope.cancel()
    assert len(sr.schedules) == 1
    assert sr.schedules[0].name == "extra"


@pytest.mark.anyio
async def test_refresh_skips_existing(tmp_path: Path) -> None:
    sched_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(tmp_path / "schedules")
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        result = await sr.refresh()
        assert result == {"daily": "skipped"}
        tg.cancel_scope.cancel()
    assert len(sr.schedules) == 1


@pytest.mark.anyio
async def test_refresh_updates_modified_schedule(tmp_path: Path) -> None:
    sched_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(tmp_path / "schedules")
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        # initial refresh: skip (unchanged)
        result = await sr.refresh()
        assert result == {"daily": "skipped"}

        # modify
        await anyio.Path(sched_dir / "TASK.md").write_text(
            '---\nname: daily\ncron: "0 6 * * *"\n---\nUpdated', encoding="utf-8"
        )

        result = await sr.refresh()
        assert result == {"daily": "updated"}
        assert sr.schedules[0].cron == "0 6 * * *"
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_refresh_removes_deleted_schedule(tmp_path: Path) -> None:
    sched_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(tmp_path / "schedules")
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        # delete the schedule dir
        await anyio.Path(sched_dir / "TASK.md").unlink()
        await anyio.Path(sched_dir).rmdir()

        result = await sr.refresh()
        assert result == {"daily": "removed"}
        assert sr.schedules == []
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_refresh_mixed_changes(tmp_path: Path) -> None:
    """Add, modify, delete, and skip all in one refresh."""
    sched_dir = tmp_path / "schedules"
    await anyio.Path(sched_dir).mkdir()

    for name in ["keep", "modify", "delete"]:
        d = sched_dir / name
        await anyio.Path(d).mkdir()
        await anyio.Path(d / "TASK.md").write_text(
            f'---\nname: {name}\ncron: "0 12 * * *"\n---\nTask: {name}', encoding="utf-8"
        )

    sr = await ScheduleRegistry.load(tmp_path / "schedules")
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg

        # modify
        await anyio.Path(sched_dir / "modify" / "TASK.md").write_text(
            '---\nname: modify\ncron: "0 6 * * *"\n---\nChanged', encoding="utf-8"
        )
        # delete
        await anyio.Path(sched_dir / "delete" / "TASK.md").unlink()
        await anyio.Path(sched_dir / "delete").rmdir()
        # add
        d = sched_dir / "newone"
        await anyio.Path(d).mkdir()
        await anyio.Path(d / "TASK.md").write_text(
            '---\nname: newone\ncron: "0 12 * * *"\n---\nFresh', encoding="utf-8"
        )

        result = await sr.refresh()
        assert result == {"keep": "skipped", "modify": "updated", "delete": "removed", "newone": "added"}
        names = {s.name for s in sr.schedules}
        assert names == {"keep", "modify", "newone"}
        tg.cancel_scope.cancel()


# ── _run_one error handling ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_one_handles_agent_error() -> None:
    s = Schedule(name="test", cron="* * * * * *", task_content="ping")
    agent = _RaisingAgent()
    cancel_scope = anyio.CancelScope()
    registry = ScheduleRegistry()
    with anyio.move_on_after(3):
        await ScheduleRegistry._run_one(s, cast(Any, agent), cancel_scope, registry)


def test_seconds_until_next_uses_local_wall_clock() -> None:
    """once_at cron fields are local; must not treat Unix epoch as UTC base.

    On a UTC+N machine, croniter(cron, time.time()) would sleep ~N hours past
    a same-day local minute; datetime.now() base keeps wait under one minute.
    """
    now = datetime.now().replace(second=0, microsecond=0)
    target = now + timedelta(minutes=1)
    cron = f"{target.minute} {target.hour} {target.day} {target.month} *"
    wait = ScheduleRegistry._seconds_until_next(cron, now=now)
    assert 0.0 <= wait <= 60.0, f"expected local next-minute wait, got {wait}"


def test_schedule_tz_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    assert ScheduleRegistry._schedule_tz() is None


def test_schedule_tz_invalid_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Not/AZone")
    assert ScheduleRegistry._schedule_tz() is None


def test_schedule_tz_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    tz = ScheduleRegistry._schedule_tz()
    assert tz is not None
    assert str(tz) == "Asia/Shanghai"


def test_cron_anchored_to_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """With TZ set, cron fields mean wall time in that zone, not UTC.

    ``0 9 * * *`` must resolve to 09:00 in Asia/Shanghai regardless of the
    host clock's zone — the next fire's local hour is 9.
    """
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    tz = ScheduleRegistry._schedule_tz()
    assert tz is not None
    base = datetime.now(tz)
    nxt = croniter("0 9 * * *", base).get_next(datetime)
    assert nxt.hour == 9, f"expected 09:00 local, got {nxt.hour}"


@pytest.mark.anyio
async def test_load_fire_tool_header(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "ping"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    await anyio.Path(schedules_dir / "TASK.md").write_text(
        textwrap.dedent("""\
        ---
        name: ping
        cron: "0 12 * * *"
        fire: tool
        tool: feishu_message_send
        tool_args:
          receive_id: oc_abc
          text: hello
          receive_id_type: chat_id
        run_once: true
        visibility: silent
        ---
        notes ignored for tool fire
    """),
        encoding="utf-8",
    )
    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    entry = next(iter(files.values()))
    assert entry.schedule.fire == "tool"
    assert entry.schedule.tool_name == "feishu_message_send"
    assert entry.schedule.tool_args["receive_id"] == "oc_abc"
    assert entry.schedule.run_once is True


@pytest.mark.anyio
async def test_fire_tool_calls_registry_directly() -> None:
    called: dict[str, object] = {}

    async def feishu_message_send(receive_id: str, text: str, receive_id_type: str = "chat_id") -> str:
        called["receive_id"] = receive_id
        called["text"] = text
        called["receive_id_type"] = receive_id_type
        return '{"ok": true}'

    class _ToolAgent:
        _lock = anyio.Lock()

        def __init__(self) -> None:
            self._conversation = Conversation()
            self._tool_registry = ToolRegistry()
            tf = ToolFunction.from_callable(feishu_message_send)
            self._tool_registry._files["x"] = FileEntry(
                file_hash="h",
                tools={tf.name: tf},
                funcs={tf.name: feishu_message_send},
                fresh=True,
            )

        async def reload_tools(self) -> dict[str, str]:
            return {}

        def set_pending_schedule_chunks(self, chunks: object) -> None:
            pass

    agent = _ToolAgent()
    s = Schedule(
        name="r1",
        cron="0 12 * * *",
        task_content="",
        fire="tool",
        tool_name="feishu_message_send",
        tool_args={"receive_id": "oc_1", "text": "hi", "receive_id_type": "chat_id"},
        visibility="silent",
    )
    chunks = await ScheduleRegistry._fire_tool(s, cast(Any, agent), "schedule.silent")
    assert called["receive_id"] == "oc_1"
    assert called["text"] == "hi"
    assert any(c.reasoning and "Tool Call" in c.reasoning for c in chunks)


@pytest.mark.anyio
async def test_load_run_once_and_task_path(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "once-job"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    task = schedules_dir / "TASK.md"
    await anyio.Path(task).write_text(
        textwrap.dedent("""\
        ---
        name: once-job
        cron: "0 12 1 1 *"
        run_once: true
        visibility: display
        ---
        Remind once.
    """),
        encoding="utf-8",
    )
    files = await ScheduleRegistry._load_from_dir(tmp_path / "schedules")
    entry = next(iter(files.values()))
    assert entry.schedule.run_once is True
    assert entry.schedule.task_path.endswith("TASK.md")


@pytest.mark.anyio
async def test_consume_run_once_deletes_task(tmp_path: Path) -> None:
    schedules_dir = tmp_path / "schedules" / "once-job"
    await anyio.Path(schedules_dir).mkdir(parents=True)
    task = schedules_dir / "TASK.md"
    await anyio.Path(task).write_text("x", encoding="utf-8")
    s = Schedule(
        name="once-job",
        cron="0 12 1 1 *",
        task_content="hi",
        run_once=True,
        task_path=str(task),
    )
    registry = ScheduleRegistry(
        files={str(task): ScheduleEntry(file_hash="a", schedule=s)},
        work_dir=tmp_path / "schedules",
    )
    await ScheduleRegistry._consume_run_once(s, registry)
    assert not await anyio.Path(task).exists()
    assert str(task) not in registry._files


# ── watcher — 调度 Session 感知 TASK.md 变化的唯一途径 ────────────────────────


@pytest.mark.anyio
async def test_watcher_picks_up_schedule_created_after_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """调度 Session 没有 channel, 回合内的两个 refresh 时机都不会发生。

    少了 watcher, 用户经 ``schedule_manage`` 新建的定时任务永远不会被加载。
    """
    monkeypatch.setattr(schedule_registry_module, "_WATCH_INTERVAL_SECONDS", 0.05)
    sched_root = tmp_path / "schedules"
    await anyio.Path(sched_root / "first").mkdir(parents=True)
    await anyio.Path(sched_root / "first" / "TASK.md").write_text(
        '---\nname: first\ncron: "0 12 * * *"\n---\nT', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(sched_root, active_names={ACTIVATE_ALL})
    seen_second = anyio.Event()
    real_refresh = sr.refresh

    async def _signalling_refresh() -> dict[str, str]:
        result = await real_refresh()
        if "second" in {s.name for s in sr.schedules}:
            seen_second.set()
        return result

    monkeypatch.setattr(sr, "refresh", _signalling_refresh)
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert {s.name for s in sr.schedules} == {"first"}

        # 启动后才出现的第二个任务 —— 只有 watcher 能发现它。
        await anyio.Path(sched_root / "second").mkdir(parents=True)
        await anyio.Path(sched_root / "second" / "TASK.md").write_text(
            '---\nname: second\ncron: "5 12 * * *"\n---\nT2', encoding="utf-8"
        )
        with anyio.fail_after(5):
            await seen_second.wait()
        assert {s.name for s in sr.schedules} == {"first", "second"}
        assert "second" in sr._runner_scopes
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_empty_registry_starts_no_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非调度 Session 不该周期性扫盘 —— 空 registry 连 watcher 都不起。"""
    monkeypatch.setattr(schedule_registry_module, "_WATCH_INTERVAL_SECONDS", 0.05)
    refreshed = 0
    sr = ScheduleRegistry()

    async def _counting_refresh() -> dict[str, str]:
        nonlocal refreshed
        refreshed += 1
        return {}

    monkeypatch.setattr(sr, "refresh", _counting_refresh)
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        await anyio.sleep(0.3)
        assert refreshed == 0
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_watcher_survives_refresh_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """watcher 经 start_soon 挂在 Session 的 task group 上 —— 逸出的异常会连坐整个
    调度 Session。单次刷新失败必须只记 ERROR 并在下一周期重试。"""
    monkeypatch.setattr(schedule_registry_module, "_WATCH_INTERVAL_SECONDS", 0.02)
    await anyio.Path(tmp_path / "schedules").mkdir(parents=True)
    sr = await ScheduleRegistry.load(tmp_path / "schedules", active_names={ACTIVATE_ALL})

    calls = 0
    third_call = anyio.Event()

    async def _boom() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls >= 3:
            third_call.set()
        raise RuntimeError("refresh 之外的意外异常")

    monkeypatch.setattr(sr, "refresh", _boom)
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        # 连续失败 3 次仍未崩溃 → 异常没有逸出到 task group。
        with anyio.fail_after(5):
            await third_call.wait()
        assert calls >= 3
        tg.cancel_scope.cancel()


# ── 激活是 (session x schedule) 的属性 — 每条各自决定归哪个 Session ───────────


async def _load_three(
    tmp_path: Path,
    active: set[str] | None,
    *,
    inactive: set[str] | None = None,
) -> ScheduleRegistry:
    sched_root = tmp_path / "schedules"
    for name in ("alpha", "beta", "gamma"):
        d = sched_root / name
        await anyio.Path(d).mkdir(parents=True)
        await anyio.Path(d / "TASK.md").write_text(
            f'---\nname: {name}\ncron: "0 12 * * *"\n---\nTask {name}', encoding="utf-8"
        )
    return await ScheduleRegistry.load(sched_root, active_names=active, inactive_names=inactive)


@pytest.mark.anyio
async def test_start_all_starts_one_runner_per_active_schedule(tmp_path: Path) -> None:
    sched_dir = tmp_path / "schedules" / "daily"
    await anyio.Path(sched_dir).mkdir(parents=True)
    await anyio.Path(sched_dir / "TASK.md").write_text(
        '---\nname: daily\ncron: "0 12 * * *"\n---\nTask', encoding="utf-8"
    )

    sr = await ScheduleRegistry.load(tmp_path / "schedules", active_names={ACTIVATE_ALL})
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert len(sr._runner_scopes) == 1
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_named_subset_starts_only_those_runners(tmp_path: Path) -> None:
    """核心契约: 同一 workspace 的两个 Session 可各激活不同子集。

    整个 Session 一个布尔只能表达「全触发 / 全不触发」, 表达不了这种划分。
    """
    sr = await _load_three(tmp_path, {"alpha", "gamma"})
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert set(sr._runner_scopes) == {"alpha", "gamma"}
        # 未激活的条目照样在 registry 里可读 —— 只是不触发。
        assert {s.name for s in sr.schedules} == {"alpha", "beta", "gamma"}
        assert {s.name for s in sr.active_schedules} == {"alpha", "gamma"}
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_disjoint_subsets_fire_each_schedule_exactly_once(tmp_path: Path) -> None:
    """两个 Session 的名单不相交时, 每条 schedule 恰好被一个 Session 触发。"""
    a = await _load_three(tmp_path, {"alpha"})
    b = await ScheduleRegistry.load(tmp_path / "schedules", active_names={"beta", "gamma"})
    async with anyio.create_task_group() as tg:
        a.start_all(tg, cast(Any, _MockAgent()))
        b.start_all(tg, cast(Any, _MockAgent()))
        assert set(a._runner_scopes) == {"alpha"}
        assert set(b._runner_scopes) == {"beta", "gamma"}
        assert set(a._runner_scopes) & set(b._runner_scopes) == set()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_no_active_names_starts_nothing_but_still_loads(tmp_path: Path) -> None:
    """普通用户 Session: 读得到全部条目, 但一条都不触发 (刻意为之)。"""
    sr = await _load_three(tmp_path, None)
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert sr._runner_scopes == {}
        assert sr.active_schedules == []
        assert len(sr.schedules) == 3
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_refresh_only_starts_runners_for_active_names(tmp_path: Path) -> None:
    """refresh 的 add/update 统计不受激活影响, 但只为激活条目起 runner。"""
    sr = await ScheduleRegistry.load(tmp_path / "schedules", active_names={"wanted"})
    sched_root = tmp_path / "schedules"
    for name in ("wanted", "unwanted"):
        d = sched_root / name
        await anyio.Path(d).mkdir(parents=True)
        await anyio.Path(d / "TASK.md").write_text(f'---\nname: {name}\ncron: "0 12 * * *"\n---\nT', encoding="utf-8")
    sr._work_dir = sched_root
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        result = await sr.refresh()
        # 两条都被登记 (统计与展示不受激活影响)
        assert result == {"wanted": "added", "unwanted": "added"}
        # 但只有激活的那条起了 runner
        assert set(sr._runner_scopes) == {"wanted"}
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_inactive_registry_starts_no_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一条都不激活的 Session 不该周期性扫盘。"""
    monkeypatch.setattr(schedule_registry_module, "_WATCH_INTERVAL_SECONDS", 0.05)
    refreshed = 0
    sr = await _load_three(tmp_path, None)

    async def _counting_refresh() -> dict[str, str]:
        nonlocal refreshed
        refreshed += 1
        return {}

    monkeypatch.setattr(sr, "refresh", _counting_refresh)
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        await anyio.sleep(0.3)
        assert refreshed == 0
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_is_active_wildcard_and_names() -> None:
    assert ScheduleRegistry(active_names={ACTIVATE_ALL}).is_active("anything") is True
    assert ScheduleRegistry(active_names={"a"}).is_active("a") is True
    assert ScheduleRegistry(active_names={"a"}).is_active("b") is False
    assert ScheduleRegistry().is_active("a") is False


@pytest.mark.anyio
async def test_inactive_names_win_over_active() -> None:
    """黑名单优先: 通配符白名单也要给黑名单让路。"""
    sr = ScheduleRegistry(active_names={ACTIVATE_ALL}, inactive_names={"skip"})
    assert sr.is_active("skip") is False
    assert sr.is_active("other") is True
    # 同一条同时进两个名单 → 不触发。
    assert ScheduleRegistry(active_names={"x"}, inactive_names={"x"}).is_active("x") is False
    # 黑名单通配符 = 一条都不触发。
    assert ScheduleRegistry(active_names={ACTIVATE_ALL}, inactive_names={ACTIVATE_ALL}).is_active("x") is False


@pytest.mark.anyio
async def test_blacklist_excludes_named_entry_only(tmp_path: Path) -> None:
    """「除 beta 以外全归我」—— 白名单枚举做不到的划分。"""
    sr = await _load_three(tmp_path, {ACTIVATE_ALL}, inactive={"beta"})
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert set(sr._runner_scopes) == {"alpha", "gamma"}
        assert len(sr.schedules) == 3
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_wildcard_picks_up_schedule_created_after_start(tmp_path: Path) -> None:
    """通配符白名单会自动触发启动后新建的条目 —— 纯枚举白名单不会。

    这就是黑名单存在的理由: 「除某几条以外全归我」只能写成 ``*`` + 黑名单。
    """
    wild = await _load_three(tmp_path, {ACTIVATE_ALL}, inactive={"beta"})
    enumerated = await ScheduleRegistry.load(tmp_path / "schedules", active_names={"alpha", "gamma"})
    sched_root = tmp_path / "schedules"
    d = sched_root / "delta"
    await anyio.Path(d).mkdir(parents=True)
    await anyio.Path(d / "TASK.md").write_text('---\nname: delta\ncron: "0 12 * * *"\n---\nT', encoding="utf-8")

    async with anyio.create_task_group() as tg:
        for sr in (wild, enumerated):
            sr._agent = cast(Any, _MockAgent())
            sr._task_group = tg
            await sr.refresh()
        assert "delta" in wild._runner_scopes
        assert "delta" not in enumerated._runner_scopes
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_empty_registry_start_all_is_noop(tmp_path: Path) -> None:
    """空 registry (无 work_dir): 不加载、不触发。"""
    sr = ScheduleRegistry()
    async with anyio.create_task_group() as tg:
        sr.start_all(tg, cast(Any, _MockAgent()))
        assert sr.schedules == []
        assert sr._runner_scopes == {}
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_empty_registry_refresh_is_noop() -> None:
    """空 registry 的 refresh 不扫盘 —— 非调度 Session 每回合都会调它。"""
    sr = ScheduleRegistry()
    sr._agent = cast(Any, _MockAgent())
    async with anyio.create_task_group() as tg:
        sr._task_group = tg
        assert await sr.refresh() == {}
        assert sr.schedules == []
        tg.cancel_scope.cancel()


# ── YAML parse helper ─────────────────────────────────────────────────────────


def test_parse_yaml_header_error() -> None:
    content = "---\n: invalid yaml: :\n---\nbody"
    header, body = parse_yaml_header(content)
    assert header is None
    assert body == content


def test_parse_yaml_header_success() -> None:
    content = "---\nname: daily-report\ncron: '0 12 * * *'\n---\n请生成日报。"
    header, body = parse_yaml_header(content)
    assert header == {"name": "daily-report", "cron": "0 12 * * *"}
    assert body == "请生成日报。"

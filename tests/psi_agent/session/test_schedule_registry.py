from __future__ import annotations

import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from croniter import croniter

from psi_agent._yaml import parse_yaml_header
from psi_agent.session.conversation import Conversation
from psi_agent.session.schedule_registry import Schedule, ScheduleEntry, ScheduleRegistry
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

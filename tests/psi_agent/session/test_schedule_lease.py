from __future__ import annotations

from pathlib import Path

import pytest

from psi_agent.session import schedule_lease


@pytest.fixture(autouse=True)
def _clean_leases() -> None:
    schedule_lease.reset()


def test_acquire_first_holder_wins(tmp_path: Path) -> None:
    sched = tmp_path / "schedules"
    sched.mkdir()
    assert schedule_lease.acquire(sched, "session-a") is True
    assert schedule_lease.holder_of(sched) == "session-a"


def test_acquire_second_holder_denied(tmp_path: Path) -> None:
    sched = tmp_path / "schedules"
    sched.mkdir()
    assert schedule_lease.acquire(sched, "session-a") is True
    assert schedule_lease.acquire(sched, "session-b") is False
    assert schedule_lease.holder_of(sched) == "session-a"


def test_acquire_is_idempotent_for_same_holder(tmp_path: Path) -> None:
    sched = tmp_path / "schedules"
    sched.mkdir()
    assert schedule_lease.acquire(sched, "session-a") is True
    assert schedule_lease.acquire(sched, "session-a") is True


def test_different_workspaces_do_not_contend(tmp_path: Path) -> None:
    a = tmp_path / "ws-a" / "schedules"
    b = tmp_path / "ws-b" / "schedules"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert schedule_lease.acquire(a, "session-a") is True
    assert schedule_lease.acquire(b, "session-b") is True


def test_release_lets_next_holder_take_over(tmp_path: Path) -> None:
    sched = tmp_path / "schedules"
    sched.mkdir()
    schedule_lease.acquire(sched, "session-a")
    schedule_lease.release(sched, "session-a")
    assert schedule_lease.holder_of(sched) == ""
    assert schedule_lease.acquire(sched, "session-b") is True


def test_release_by_non_holder_is_noop(tmp_path: Path) -> None:
    sched = tmp_path / "schedules"
    sched.mkdir()
    schedule_lease.acquire(sched, "session-a")
    schedule_lease.release(sched, "session-b")
    assert schedule_lease.holder_of(sched) == "session-a"


def test_path_variants_hit_the_same_lease(tmp_path: Path) -> None:
    """大小写 / 斜杠 / 尾随 '.' 不同的同一目录必须撞同一个租约。"""
    sched = tmp_path / "schedules"
    sched.mkdir()
    assert schedule_lease.acquire(sched, "session-a") is True
    variant = str(sched).replace("\\", "/")
    assert schedule_lease.acquire(variant, "session-b") is False
    assert schedule_lease.acquire(sched / ".", "session-c") is False

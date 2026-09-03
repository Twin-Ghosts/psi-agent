"""Unit-test the pure org-tree builder behind ``feishu_sync_org_tree``.

The org tree is built from a member list (open_id / name / leader_user_id) by the
pure ``_build_org_tree`` function, so the tree shape, root detection, unresolved
leaders and cycle detection can be tested with no Feishu IO. These tests pin the
edge cases whose removal would silently produce a wrong tree: a member whose leader
is missing from the roster, an empty-leader roster, and leader cycles.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _module():
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    return importlib.import_module("feishu_sync_org_tree")


def _build(members: list[dict]) -> dict:
    return _module()._build_org_tree(members)


def test_normal_tree_builds_reports_and_roots() -> None:
    tree = _build(
        [
            {"open_id": "a", "name": "A", "leader_user_id": ""},
            {"open_id": "b", "name": "B", "leader_user_id": "a"},
            {"open_id": "c", "name": "C", "leader_user_id": "b"},
        ]
    )
    assert tree["roots"] == ["a"]
    assert tree["by_open_id"]["a"]["reports"] == ["b"]
    assert tree["by_open_id"]["b"]["reports"] == ["c"]
    assert tree["by_open_id"]["c"]["reports"] == []
    assert tree["unresolved_leaders"] == []
    assert tree["cycles_detected"] == []


def test_all_members_are_roots_when_no_leaders() -> None:
    tree = _build(
        [
            {"open_id": "a", "name": "A", "leader_user_id": ""},
            {"open_id": "b", "name": "B", "leader_user_id": ""},
        ]
    )
    assert tree["roots"] == ["a", "b"]
    assert tree["cycles_detected"] == []


def test_unresolved_leader_is_reported_not_silenced() -> None:
    tree = _build([{"open_id": "a", "name": "A", "leader_user_id": "missing"}])
    assert tree["roots"] == ["a"]
    assert tree["unresolved_leaders"] == ["missing"]


def test_cycle_is_detected() -> None:
    tree = _build(
        [
            {"open_id": "a", "name": "A", "leader_user_id": "b"},
            {"open_id": "b", "name": "B", "leader_user_id": "a"},
        ]
    )
    assert tree["roots"] == []
    assert ["a", "b", "a"] in tree["cycles_detected"]


def test_self_loop_is_a_cycle() -> None:
    tree = _build([{"open_id": "a", "name": "A", "leader_user_id": "a"}])
    assert tree["roots"] == []
    assert tree["cycles_detected"] == [["a", "a"]]


def test_duplicate_members_collapse_by_id() -> None:
    tree = _build(
        [
            {"open_id": "a", "name": "A", "leader_user_id": ""},
            {"open_id": "a", "name": "A2", "leader_user_id": ""},
        ]
    )
    assert tree["roots"] == ["a"]
    assert len(tree["by_open_id"]) == 1

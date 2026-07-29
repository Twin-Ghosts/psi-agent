"""Tests for agent-package channel_events loader + feishu member_added map."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from psi_agent.channel._event_defs import load_channel_event_defs

HAITUN = Path(__file__).resolve().parents[3] / "examples" / "haitun-workspace"


@pytest.mark.anyio
async def test_load_feishu_member_added_def() -> None:
    defs = await load_channel_event_defs(HAITUN, "feishu")
    names = {d.name for d in defs}
    assert "feishu.chat.member_added" in names
    hit = next(d for d in defs if d.name == "feishu.chat.member_added")
    assert hit.platform_event == "im.chat.member.user.added_v1"
    assert hit.map_fn is not None


def test_member_added_map_event() -> None:
    map_path = HAITUN / "channel_events" / "feishu" / "member_added" / "map.py"
    spec = importlib.util.spec_from_file_location("member_map", map_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    envs = mod.map_event(
        {
            "event": {
                "chat_id": "oc_1",
                "operator_id": {"open_id": "ou_op"},
                "users": [{"name": "A", "user_id": {"open_id": "ou_m"}}],
            }
        }
    )
    assert len(envs) == 1
    assert envs[0]["event"] == "feishu.chat.member_added"
    assert envs[0]["payload"]["member_open_id"] == "ou_m"
    assert envs[0]["routing"]["open_id"] == "ou_op"

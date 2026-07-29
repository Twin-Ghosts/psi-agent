"""Tests for agent-package channel_events loader + synthetic runner + maps."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, cast

import anyio
import pytest

from psi_agent.channel._core import ChannelCore
from psi_agent.channel._event_defs import ChannelEventDef, load_channel_event_defs
from psi_agent.channel._synthetic import SyntheticContext, start_synthetic_producers

HAITUN = Path(__file__).resolve().parents[3] / "examples" / "haitun-workspace"


@pytest.mark.anyio
async def test_load_feishu_member_added_def() -> None:
    defs = await load_channel_event_defs(HAITUN, "feishu")
    names = {d.name for d in defs}
    assert "feishu.chat.member_added" in names
    hit = next(d for d in defs if d.name == "feishu.chat.member_added")
    assert hit.platform_event == "im.chat.member.user.added_v1"
    assert hit.map_fn is not None
    assert hit.produce_fn is None


@pytest.mark.anyio
async def test_load_feishu_demo_tick_synthetic() -> None:
    defs = await load_channel_event_defs(HAITUN, "feishu")
    hit = next(d for d in defs if d.name == "feishu.synthetic.demo_tick")
    assert hit.kind == "synthetic"
    assert hit.produce_fn is not None
    assert hit.map_fn is None
    assert hit.platform_event == ""


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


@pytest.mark.anyio
async def test_synthetic_emit_posts_event(tmp_path: Path) -> None:
    """produce.py emit goes through SyntheticContext → resolve_core.post_event."""
    slug = tmp_path / "channel_events" / "feishu" / "once"
    await anyio.Path(str(slug)).mkdir(parents=True)
    await anyio.Path(str(slug / "EVENT.yaml")).write_text(
        "name: feishu.synthetic.once\nsource: feishu\nkind: synthetic\n",
        encoding="utf-8",
    )
    await anyio.Path(str(slug / "produce.py")).write_text(
        "async def produce(ctx):\n    await ctx.emit({'payload': {'n': 1}, 'routing': {'open_id': 'ou_x'}})\n",
        encoding="utf-8",
    )
    defs = await load_channel_event_defs(tmp_path, "feishu")
    assert len(defs) == 1 and defs[0].produce_fn is not None

    posted: list[dict[str, Any]] = []
    done = anyio.Event()

    class _FakeCore:
        async def post_event(self, envelope: dict[str, object]) -> dict[str, object]:
            posted.append(dict(envelope))
            done.set()
            return {"ok": True, "matched": [], "fired": []}

    async def resolve_core(open_id: str | None) -> ChannelCore:
        assert open_id == "ou_x"
        return cast(ChannelCore, _FakeCore())

    async with anyio.create_task_group() as tg:
        n = start_synthetic_producers(defs, resolve_core=resolve_core, task_group=tg)
        assert n == 1
        with anyio.fail_after(2):
            await done.wait()
        tg.cancel_scope.cancel()

    assert posted[0]["event"] == "feishu.synthetic.once"
    assert posted[0]["source"] == "feishu"
    assert posted[0]["payload"] == {"n": 1}


@pytest.mark.anyio
async def test_synthetic_context_emit_defaults() -> None:
    posted: list[dict[str, Any]] = []

    class _FakeCore:
        async def post_event(self, envelope: dict[str, object]) -> dict[str, object]:
            posted.append(dict(envelope))
            return {"ok": True}

    async def resolve_core(_open_id: str | None) -> ChannelCore:
        return cast(ChannelCore, _FakeCore())

    edef = ChannelEventDef(
        dir_name="x",
        name="feishu.synthetic.x",
        source="feishu",
        kind="synthetic",
        platform_event="",
        description="",
        map_fn=None,
        produce_fn=None,
        path=Path("."),
    )
    ctx = SyntheticContext(
        event_name=edef.name,
        source=edef.source,
        _resolve_core=resolve_core,
        _edef=edef,
    )
    await ctx.emit({"payload": {}})
    assert posted[0]["schema_version"] == 1
    assert posted[0]["raw_event"] == "synthetic:feishu.synthetic.x"

"""Wire agent-package ``channel_events/feishu`` into Feishu WS → Session ``/events``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from psi_agent.channel._core import ChannelCore
from psi_agent.channel._event_defs import ChannelEventDef, load_channel_event_defs

try:
    from lark_channel.event.custom import CustomizedEventProcessor
except ImportError:  # pragma: no cover
    CustomizedEventProcessor = None  # type: ignore[misc, assignment]


def _raw_to_dict(raw: Any) -> dict[str, Any]:
    """Best-effort normalize SDK event objects to a plain dict for map_event."""
    if isinstance(raw, dict):
        return dict(raw)
    for attr in ("dict", "model_dump", "to_dict"):
        fn = getattr(raw, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    # lark events often nest under .event
    nested = getattr(raw, "event", None)
    if isinstance(nested, dict):
        return {"event": nested, "header": getattr(raw, "header", None)}
    if nested is not None:
        inner = _raw_to_dict(nested)
        return {"event": inner, "type": getattr(raw, "type", None)}
    return {"raw": repr(raw)}


async def register_feishu_agent_events(
    *,
    channel: Any,
    agent_root: Path,
    resolve_core: Callable[[str | None], Awaitable[ChannelCore]],
    portal_start: Callable[..., Any],
) -> int:
    """Load ``channel_events/feishu`` and register platform_map processors.

    Must run **after** ``start_background()`` (dispatcher rebuild). Returns
    how many platform_event processors were registered.
    """
    if CustomizedEventProcessor is None:
        logger.warning("lark_channel CustomizedEventProcessor missing — agent events off")
        return 0

    defs = await load_channel_event_defs(agent_root, "feishu")
    platform_defs = [d for d in defs if d.kind == "platform_map" and d.map_fn and d.platform_event]
    if not platform_defs:
        logger.info(f"No feishu platform_map events under {agent_root / 'channel_events' / 'feishu'}")
        return 0

    dispatcher = getattr(channel, "dispatcher", None)
    proc_map = getattr(dispatcher, "_processorMap", None)
    if not isinstance(proc_map, dict):
        logger.warning("agent events unavailable — dispatcher has no _processorMap")
        return 0

    registered = 0
    for edef in platform_defs:
        for schema in ("p1", "p2"):
            key = f"{schema}.{edef.platform_event}"
            if key in proc_map:
                logger.debug(f"processor already present for {key}; skipping")
                continue

            def _on_event(raw: Any, _edef: ChannelEventDef = edef) -> None:
                try:
                    portal_start(_forward_one, _edef, raw, resolve_core)
                except Exception as e:
                    logger.warning(f"schedule agent event {_edef.name!r} failed — {e!r}")

            try:
                proc_map[key] = CustomizedEventProcessor(_on_event)
                registered += 1
                logger.info(f"Registered channel event {edef.name!r} → {key}")
            except Exception as e:
                logger.warning(f"register {key} failed — {e!r}")
    return registered


async def _forward_one(
    edef: ChannelEventDef,
    raw: Any,
    resolve_core: Callable[[str | None], Awaitable[ChannelCore]],
) -> None:
    """Map platform payload → envelope(s) → ``ChannelCore.post_event``."""
    try:
        if edef.map_fn is None:
            return
        raw_dict = _raw_to_dict(raw)
        envelopes = edef.map_fn(raw_dict)
        if not isinstance(envelopes, list):
            logger.error(f"{edef.name}: map_event must return list[dict], got {type(envelopes)!r}")
            return
        for env in envelopes:
            if not isinstance(env, dict):
                logger.error(f"{edef.name}: envelope is not a dict")
                continue
            # Fill defaults from EVENT.yaml if mapper omitted them.
            env.setdefault("schema_version", 1)
            env.setdefault("source", edef.source)
            env.setdefault("event", edef.name)
            env.setdefault("raw_event", edef.platform_event)
            routing = env.get("routing") if isinstance(env.get("routing"), dict) else {}
            open_id = None
            if isinstance(routing, dict):
                oid = routing.get("open_id")
                if isinstance(oid, str) and oid.strip():
                    open_id = oid.strip()
            core = await resolve_core(open_id)
            await core.post_event(env)
    except Exception as e:
        logger.error(f"Unhandled error forwarding channel event {edef.name!r}: {e!r}")

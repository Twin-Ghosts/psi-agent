"""Load Channel event definitions from the agent package.

Layout (per channel name, e.g. ``feishu``)::

    {agent}/channel_events/<channel>/
        <event_dir>/
            EVENT.yaml   # name, source, platform_event?, kind, …
            map.py       # required for kind=platform_map: map_event(raw) -> list[dict]

Session only receives envelopes via ``POST /events``. Business event
registry lives here (agent package), not in ``session/event_protocol``.

Adding a new event ≈ adding a tool: drop a directory under
``channel_events/<channel>/``, implement ``map.py``, restart Channel.
"""

from __future__ import annotations

import hashlib
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import yaml
from loguru import logger

MapEventFn = Callable[[dict[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ChannelEventDef:
    """One agent-package channel event definition."""

    dir_name: str
    name: str
    source: str
    kind: str  # platform_map | synthetic (synthetic = declare only for now)
    platform_event: str
    description: str
    map_fn: MapEventFn | None
    path: Path


async def load_channel_event_defs(agent_root: Path, channel: str) -> list[ChannelEventDef]:
    """Load ``channel_events/<channel>/*/EVENT.yaml`` (+ optional ``map.py``)."""
    root = anyio.Path(str(agent_root / "channel_events" / channel))
    try:
        if not await root.is_dir():
            logger.debug(f"No channel_events for {channel!r} under {agent_root}")
            return []
    except Exception as e:
        logger.warning(f"Cannot access channel_events/{channel}: {e!r}")
        return []

    defs: list[ChannelEventDef] = []
    async for entry in root.iterdir():
        try:
            if not await entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            yaml_path = entry / "EVENT.yaml"
            if not await yaml_path.is_file():
                # also accept EVENT.yml
                yaml_path = entry / "EVENT.yml"
            if not await yaml_path.is_file():
                logger.warning(f"Skip {entry}: no EVENT.yaml")
                continue
            text = await yaml_path.read_text(encoding="utf-8")
            header = yaml.safe_load(text) or {}
            if not isinstance(header, dict):
                logger.warning(f"Skip {yaml_path}: YAML root must be a mapping")
                continue
            name = str(header.get("name") or entry.name).strip()
            source = str(header.get("source") or channel).strip().casefold()
            kind = str(header.get("kind") or "platform_map").strip().casefold()
            platform_event = str(header.get("platform_event") or "").strip()
            description = str(header.get("description") or "").strip()
            map_fn: MapEventFn | None = None
            map_file = entry / "map.py"
            if kind == "platform_map":
                if not platform_event:
                    logger.error(f"{entry}: platform_map requires platform_event")
                    continue
                if not await map_file.is_file():
                    logger.error(f"{entry}: platform_map requires map.py")
                    continue
                map_fn = _load_map_fn(Path(str(map_file)), name)
                if map_fn is None:
                    continue
            elif kind == "synthetic":
                # Interface reserved: producer conditions live beside EVENT.yaml later.
                logger.info(f"Loaded synthetic event stub {name!r} (no runtime producer yet)")
            else:
                logger.warning(f"{entry}: unknown kind {kind!r}; skipping")
                continue
            defs.append(
                ChannelEventDef(
                    dir_name=entry.name,
                    name=name,
                    source=source,
                    kind=kind,
                    platform_event=platform_event,
                    description=description,
                    map_fn=map_fn,
                    path=Path(str(entry)),
                )
            )
            logger.info(
                f"channel_events/{channel}/{entry.name}: name={name!r} "
                f"kind={kind!r} platform_event={platform_event!r}"
            )
        except Exception as e:
            logger.error(f"Failed to load channel event from {entry!r}: {e!r}")
    defs.sort(key=lambda d: d.name)
    return defs


def _load_map_fn(map_path: Path, event_name: str) -> MapEventFn | None:
    """``compile``+``exec`` map.py → ``map_event`` callable (same idea as tools)."""
    try:
        source = map_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"Cannot read {map_path}: {e!r}")
        return None
    file_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    mod_name = f"psi_channel_event_{event_name}_{file_hash}"
    try:
        compiled = compile(source, str(map_path), "exec")
        module = types.ModuleType(mod_name)
        module.__file__ = str(map_path)
        sys.modules[mod_name] = module
        exec(compiled, module.__dict__)
    except Exception as e:
        logger.error(f"Failed to exec {map_path}: {e!r}")
        return None
    fn = getattr(module, "map_event", None)
    if not callable(fn):
        logger.error(f"{map_path}: must define map_event(raw) -> list[dict]")
        return None
    return cast(MapEventFn, fn)

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import anyio
from loguru import logger

_INTERVAL_SECONDS = 60


async def produce(ctx: Any) -> None:
    while True:
        tick = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
        try:
            open_ids = await _registered_open_ids()
        except Exception as exc:
            logger.warning(f"Assignment delivery token map read failed: {exc}")
            open_ids = []
        for open_id in open_ids:
            try:
                await ctx.emit(
                    {
                        "payload": {"tick": tick},
                        "routing": {"open_id": open_id},
                        "idempotency_key": (f"haitun.assignment.delivery_check:{open_id}:{tick}"),
                    }
                )
            except Exception as exc:
                logger.warning(f"Assignment delivery event emit failed for {open_id}: {exc}")
        await anyio.sleep(_INTERVAL_SECONDS)


async def _registered_open_ids() -> list[str]:
    raw_path = os.environ.get("FUSION_MEMORY_TOKEN_MAP_FILE", "").strip()
    if not raw_path:
        return []
    path = anyio.Path(raw_path)
    try:
        raw = await path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except OSError, json.JSONDecodeError:
        return []
    if not isinstance(value, dict):
        return []
    return sorted(
        open_id.strip()
        for open_id, entry in value.items()
        if isinstance(open_id, str)
        and open_id.strip().startswith("ou_")
        and isinstance(entry, dict)
        and isinstance(entry.get("token"), str)
        and entry["token"].strip()
    )

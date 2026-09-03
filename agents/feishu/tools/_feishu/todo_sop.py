"""Load the user-editable company TODO SOP config (``config/todo-sop.yaml``).

The company-specific TODO judgment requirements (three-level schema, priority quadrants,
quota, mentor-check deadline, leave approval codes, closure elements, ledger field schema,
completion / truthfulness verdict words) are the one thing that changes when this product
is sold to another company. They live in ``config/todo-sop.yaml`` and are read on demand;
a missing or malformed file returns ``{}`` so every caller falls back to its own built-in
default, keeping behaviour unchanged rather than silently misjudging.
"""

from __future__ import annotations

from typing import Any

import _runtime_paths as _paths
import yaml
from loguru import logger

_CONFIG_REL = "config/todo-sop.yaml"


async def load_todo_sop() -> dict[str, Any]:
    """Return the parsed ``config/todo-sop.yaml``, or ``{}`` when unreadable / invalid.

    Callers must fall back to their built-in default on ``{}``; ``mentor_ledger.py`` keeps
    ``_LEDGER_SCHEMA_FIELDS`` as that fallback, so a broken config never changes the ledger
    schema silently.
    """
    path = _paths.resolve_agent() / _CONFIG_REL
    try:
        text = await path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        logger.warning(f"todo-sop config unreadable ({exc}); callers fall back to defaults")
        return {}
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.warning(f"todo-sop config is not valid YAML ({exc}); callers fall back to defaults")
        return {}
    if not isinstance(loaded, dict) or not _valid_ledger(loaded):
        logger.warning("todo-sop config lacks a valid ledger_schema; callers fall back to defaults")
        return {}
    return loaded


def _valid_ledger(cfg: dict[str, Any]) -> bool:
    """The ledger schema is the tool-critical part; require it to be present and well-typed."""
    ledger = cfg.get("ledger_schema")
    if not isinstance(ledger, dict):
        return False
    fields = ledger.get("fields")
    if not isinstance(fields, list) or not fields:
        return False
    return all(isinstance(f, dict) and f.get("field_name") and "type" in f for f in fields)

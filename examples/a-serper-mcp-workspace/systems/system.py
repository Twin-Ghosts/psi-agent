"""Build the system prompt for the Serper MCP workspace."""

from __future__ import annotations

import inspect
from typing import Any

import anyio


async def system_prompt_builder() -> str:
    current_file = anyio.Path(inspect.getfile(system_prompt_builder))
    workspace_root = current_file.parent.parent

    return f"""You are a helpful AI assistant with web search capabilities.

You have access to a `serper` tool that searches Google via the Serper API.
Use it to look up current information, facts, and web content.

## Workspace
Location: {workspace_root}

## Tools
- serper: search the web via Google Serper API"""


async def compact_history(history: list[dict[str, Any]], complete_fn) -> str:
    """Summarize older conversation turns, keeping the last 2 turns verbatim.

    Returns the summary string; the framework merges it into the system prompt.
    """
    if len(history) <= 6:
        return ""

    older = history[:-4]

    parts: list[str] = []
    for msg in older:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
            parts.append(f"[{role}]: {content}")

    if not parts:
        return ""

    summary_prompt = [
        {"role": "system", "content": (
            "Summarize the following conversation concisely. "
            "Preserve all key facts, decisions, task context, file paths, "
            "and information the user or assistant explicitly mentioned. "
            "Do not omit anything that could be needed later."
        )},
        {"role": "user", "content": "Summarize:\n\n" + "\n".join(parts)},
    ]

    try:
        summary = await complete_fn(summary_prompt)
        return summary
    except Exception:
        return "\n".join(parts)

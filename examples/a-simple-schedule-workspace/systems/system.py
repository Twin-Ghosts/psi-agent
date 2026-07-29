"""Build the system prompt for the bash-only agent workspace."""

from __future__ import annotations

import inspect
from typing import Any

import anyio

from psi_agent._yaml import parse_yaml_header


async def system_prompt_builder() -> str:
    current_file = anyio.Path(inspect.getfile(system_prompt_builder))
    workspace_root = current_file.parent.parent
    skills_dir = workspace_root / "skills"

    skills: list[str] = []
    if await skills_dir.is_dir():
        skill_dirs = sorted([p async for p in skills_dir.iterdir()], key=lambda p: p.name)
        for skill_dir in skill_dirs:
            if not await skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not await skill_md.exists():
                continue
            header, _ = parse_yaml_header(await skill_md.read_text(encoding="utf-8"))
            if header and header.get("name") and header.get("description"):
                skills.append(f"- {header['name']}: {header['description']}")

    skills_text = "\n".join(skills) if skills else "(None)"

    return f"""You are a helpful AI assistant.

## Workspace
Location: {workspace_root}

## Skills
Location: {skills_dir}

Available:
{skills_text}"""


RECENT_TURNS_KEPT_VERBATIM = 20
"""How many trailing history messages ``compact_history`` keeps verbatim.

Raised from 4 to 20: with 4, a compaction triggered near the token threshold
left so little verbatim tail that the model lost the thread of the current
task and re-compacted almost every other turn.  20 messages is roughly 10
exchanges (~1% of the default 100K threshold for chat-only traffic).
"""


SUMMARY_MAX_CHARS = 8000
"""Hard cap on the carried-forward summary.

Chained summaries grow monotonically, and the result is merged into the system
prompt — left unbounded it would shrink the per-turn budget it exists to protect
and make compaction fire *more* often.  Truncation keeps the head, which is
where the running summary states the task and decisions.
"""


def _cap_summary(text: str) -> str:
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[:SUMMARY_MAX_CHARS] + f"\n[... running summary truncated at {SUMMARY_MAX_CHARS} characters]"


async def compact_history(history: list[dict[str, Any]], complete_fn) -> str:
    """Summarize older conversation turns via LLM, keeping recent turns verbatim.

    Returns the summary string with recent turns appended; the framework
    merges the whole result into the system prompt.

    Compactions chain: the summary produced by an earlier compaction is fed back
    in so the model *updates* it instead of describing only the newest slice.
    Without this the previous summary is silently dropped (its ``compacted`` row
    is not a ``user``/``assistant`` message), so every compaction forgot one more
    layer of the conversation.
    """
    if len(history) <= RECENT_TURNS_KEPT_VERBATIM + 2:
        return ""

    recent_count = RECENT_TURNS_KEPT_VERBATIM
    older = history[:-recent_count]
    recent = history[-recent_count:]

    # Only the LAST compaction's summary is current; earlier ones are already
    # folded into it and would re-introduce stale context if replayed.
    previous_summary = ""
    for msg in reversed(older):
        if msg.get("role") == "compacted":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                previous_summary = content
            break

    parts: list[str] = []
    for msg in older:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
            parts.append(f"[{role}]: {content}")

    recent_text = ""
    recent_parts: list[str] = []
    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
            recent_parts.append(f"[{role}]: {content}")
    if recent_parts:
        recent_text = "\n[Recent turns]\n" + "\n".join(recent_parts)

    if not parts:
        # Nothing new to summarize, but an existing summary must still be carried
        # forward — dropping it here would lose everything before this compaction.
        if previous_summary:
            return _cap_summary(previous_summary) + "\n" + recent_text
        return recent_text

    if previous_summary:
        instruction = (
            "You are maintaining a running summary of a long conversation. "
            "Update the existing summary below so it also covers the new messages. "
            "Preserve all key facts, decisions, task context, file paths, and "
            "information either party explicitly mentioned — including everything "
            "already captured in the existing summary. Do not drop earlier context, "
            "and do not omit anything that could be needed later. "
            f"Keep the result under roughly {SUMMARY_MAX_CHARS // 2} characters."
        )
        user_content = f"<existing-summary>\n{previous_summary}\n</existing-summary>\n\nNew messages:\n\n" + "\n".join(
            parts
        )
    else:
        instruction = (
            "Summarize the following conversation concisely. "
            "Preserve all key facts, decisions, task context, file paths, "
            "and information the user or assistant explicitly mentioned. "
            "Do not omit anything that could be needed later."
        )
        user_content = "Summarize:\n\n" + "\n".join(parts)

    summary_prompt = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_content},
    ]

    try:
        summary = await complete_fn(summary_prompt)
    except Exception:
        # Fall back to the raw older text, still keeping any existing summary.
        fallback = ("\n".join(parts)) if not previous_summary else previous_summary + "\n" + "\n".join(parts)
        return _cap_summary(fallback) + "\n" + recent_text
    return _cap_summary(summary) + "\n" + recent_text

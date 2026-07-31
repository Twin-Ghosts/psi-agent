from __future__ import annotations

import json

from loguru import logger

from psi_agent._appdata import (
    appdata_history_path,
    legacy_history_path,
    resolve_appdata_root,
    resolve_history_read_path,
)
from psi_agent.session.history_display import (
    KIND_CHAT,
    extract_send_paths,
    is_displayable_chat_message,
    message_kind,
    strip_transfer_markers,
    wire_role,
)


def _merge_reasoning(existing: object, extra: str) -> str:
    parts: list[str] = []
    if isinstance(existing, str) and existing.strip():
        parts.append(existing.strip())
    if extra.strip():
        parts.append(extra.strip())
    return "\n".join(parts)


class HistoryManager:
    async def get(self, workspace: str, session_id: str, *, appdata: str = "") -> list[dict[str, object]]:
        appdata_root = appdata.strip() or await resolve_appdata_root()
        path = await resolve_history_read_path(
            appdata_root=appdata_root,
            workspace=workspace,
            session_id=session_id,
        )
        messages: list[dict[str, object]] = []
        # Reasoning from tool-call assistant rows (often empty ``content``) held until
        # the next displayable assistant bubble — so SPA「已思考」survives refresh.
        pending_reasoning = ""
        try:
            content = await path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.debug(f"No history file for session {session_id!r} at {path!r}")
            return messages
        except OSError as e:
            logger.warning(f"Failed to read history for session {session_id!r}: {e!r}")
            return messages
        for line in content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue

            role = wire_role(msg.get("role"))
            kind = message_kind(msg)
            reasoning_raw = msg.get("reasoning")
            reasoning = (
                reasoning_raw.strip()
                if isinstance(reasoning_raw, str) and reasoning_raw.strip()
                else ""
            )

            if not is_displayable_chat_message(msg):
                # Tool rounds: assistant + tool_calls + reasoning, no chat content.
                if role == "assistant" and kind == KIND_CHAT and reasoning:
                    if messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        prev["reasoning"] = _merge_reasoning(prev.get("reasoning"), reasoning)
                    else:
                        pending_reasoning = _merge_reasoning(pending_reasoning, reasoning)
                continue

            text = msg.get("content", "")
            if role not in ("user", "assistant") or not isinstance(text, str):
                continue
            sends = extract_send_paths(text) if role == "assistant" else []
            cleaned = strip_transfer_markers(text)

            if role == "user" and pending_reasoning:
                # Orphan tool-round thinking before a new user turn: attach to last
                # assistant if any, else drop (no bubble to hang on).
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    prev["reasoning"] = _merge_reasoning(
                        prev.get("reasoning"),
                        pending_reasoning,
                    )
                pending_reasoning = ""

            # SEND-only assistant turns: fold paths into the previous assistant
            # bubble so spa v1 does not render an empty message.
            if not cleaned and sends:
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    prev_raw = prev.get("sends")
                    prev_sends = list(prev_raw) if isinstance(prev_raw, list) else []
                    prev_sends.extend(sends)
                    prev["sends"] = prev_sends
                    if reasoning or pending_reasoning:
                        prev["reasoning"] = _merge_reasoning(
                            prev.get("reasoning"),
                            _merge_reasoning(pending_reasoning, reasoning),
                        )
                        pending_reasoning = ""
                else:
                    row: dict[str, object] = {"role": role, "text": "", "sends": sends}
                    merged = _merge_reasoning(pending_reasoning, reasoning)
                    pending_reasoning = ""
                    if merged:
                        row["reasoning"] = merged
                    messages.append(row)
                continue
            if not cleaned:
                if reasoning:
                    if messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        prev["reasoning"] = _merge_reasoning(prev.get("reasoning"), reasoning)
                    else:
                        pending_reasoning = _merge_reasoning(pending_reasoning, reasoning)
                continue

            row = {"role": role, "text": cleaned}
            if kind != "chat":
                row["kind"] = kind
            if sends:
                row["sends"] = sends
            if role == "assistant":
                merged = _merge_reasoning(pending_reasoning, reasoning)
                pending_reasoning = ""
                if merged:
                    row["reasoning"] = merged
            messages.append(row)

        if pending_reasoning and messages and messages[-1].get("role") == "assistant":
            prev = messages[-1]
            prev["reasoning"] = _merge_reasoning(prev.get("reasoning"), pending_reasoning)

        logger.debug(f"History for session {session_id!r}: {len(messages)} displayable message(s)")
        return messages

    async def delete(self, workspace: str, session_id: str, *, appdata: str = "") -> None:
        """Remove AppData and legacy history files if present (best-effort)."""
        appdata_root = appdata.strip() or await resolve_appdata_root()
        for path in (
            appdata_history_path(appdata_root, session_id),
            legacy_history_path(workspace, session_id),
        ):
            try:
                await path.unlink()
                logger.info(f"Deleted history file for session {session_id!r} at {path!r}")
            except FileNotFoundError:
                logger.debug(f"No history file to delete for session {session_id!r} at {path!r}")
            except OSError as e:
                logger.warning(f"Failed to delete history for session {session_id!r}: {e!r}")

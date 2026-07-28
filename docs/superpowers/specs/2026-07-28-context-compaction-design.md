# Context Compaction Design Spec

## Overview

当对话 token 数超过 `max_context_tokens` 时，AI 层检测 upstream 用量并通知 Session
触发 context compaction：调用 `system.py` 的 `compact_history()` 生成 LLM 摘要 + 近期
对话原样，以独立 `compacted` 消息存入 conversation。`messages_for_ai()` 发送前自动裁剪。

## Flow

```
upstream LLM → AI Layer
                  │ stream_options={"include_usage": true}
                  │ chunk.usage.prompt_tokens > max_context_tokens
                  ↓ post-stream SSE: psi_compaction signal
             Session
                  │ AiClient.stream() → AiDelta.compaction_needed
                  │ Agent._maybe_compact()
                  ↓ compact_history() from system.py
             compacted message inserted → commit()

Next turn:
             messages_for_ai()
                  │ find system[0] + last compacted
                  │ delete messages in between
                  │ system.content += "[Compacted History]\n" + compacted.content
                  ↓ [system+summary, ...recent msgs...]
```

## Data Visibility

```
JSONL:  system, u1, a1, u2, a2, compacted(summary), u3, a3, ...
Gateway: system, u1, a1, u2, a2, u3, a3, ...
        (compacted filtered by is_displayable_chat_message)
AI:     [system+summary, u3, a3, ...]
```

## Parameters

| Parameter | CLI | Env | Default | Description |
|---|---|---|---|---|
| `max_context_tokens` | `--max-context-tokens` | `PSI_MAX_CONTEXT_TOKENS` | 100K | -1 = use env/default; 0 = disabled |

## Protocol Extension

psi-agent internal extension (not exposed to external clients):

```json
{"choices": [{"delta": {}, "finish_reason": "compaction_needed"}],
 "psi_compaction": {"needed": true, "prompt_tokens": N, "threshold": M}}
```

## AI Layer Changes

### `Ai` dataclass (`__init__.py`)
- New field: `max_context_tokens: int = -1` (sentinel: -1 = use env or 100K)
- `run()` resolves: CLI > env `PSI_MAX_CONTEXT_TOKENS` > 100K

### `handle_chat_completions()` (`server.py`)
- Forces `stream_options={"include_usage": true}` (merges existing keys)
- Tracks `chunk.usage.prompt_tokens` during upstream streaming
- After upstream stream ends, if `prompt_tokens > max_context_tokens`, sends extra SSE event with `psi_compaction` signal
- `isinstance` guard on `stream_opts` before setting `include_usage`

## Session Layer Changes

### Protocol (`protocol.py`)
- `AiDelta.compaction_needed: bool = False` — new field

### AiClient (`ai_client.py`)
- Parses top-level `psi_compaction` field from SSE data (with `isinstance` guard)
- Sets `AiDelta.compaction_needed` on appropriate deltas

### Agent Loop (`agent.py`)
- Stop handler moved out of inner `for delta in stream` loop to allow compaction signal to arrive before processing
- `_compaction_needed` flag tracked per-turn
- `_maybe_compact()`:
  1. Gets `compact_history` from `SystemPrompt`
  2. Builds `complete_fn` closure (streaming via `AiClient` with `aclosing()`)
  3. `summary = await compact_history(conversation.messages, complete_fn)`
  4. If `summary` empty → skip
  5. Inserts compaction message: `{"role": "compacted", "content": summary, "kind": "compacted"}`
  6. `commit()` — all history preserved in JSONL

### Conversation (`conversation.py`)
- `trim_after(index)` — mutation method with snapshot support (kept for future use, not called by compaction)

### SystemPrompt (`system_prompt.py`)
- `compaction_fn: Callable | None` — property exposing `compact_history` from system.py
- `_load_module()` extracts `compact_history` alongside builder/checker using `_extract_async_func`

### messages_for_ai() (`history_display.py`)
- New compaction path: when a `compacted` message exists:
  1. Find first system message (usually index 0)
  2. Find last `compacted` message (search backwards)
  3. Delete messages between system and compacted (exclusive)
  4. Merge compacted content into system message: `system.content += "\n\n[Compacted History]\n" + compacted.content`
  5. Keep all messages after compacted as-is
  6. Drop the `compacted` message itself
- No compaction → same behavior as before (strip display keys, fix roles)

## system.py Contract

```python
async def compact_history(
    history: list[dict[str, Any]],
    complete_fn: Callable[[list[dict[str, Any]]], Awaitable[str]],
) -> str:
    """Summarize conversation history.

    Args:
        history: Full conversation messages list.
        complete_fn: Async callable that sends messages to AI and returns
            the response string. Call this to make LLM summarization calls.

    Returns:
        Compaction summary string with recent turns appended verbatim.
        Return "" to skip compaction.
    """
```

Default strategy (all 11 example workspaces): ≤6 messages → skip; summaries older
messages via LLM; keeps last 4 messages (2 turns) verbatim; appends them as
`[Recent turns]` section to the returned string.

## Error Handling

| Scenario | Behavior |
|---|---|
| `max_context_tokens == 0` | Compaction disabled |
| No `compact_history` in system.py | Log WARNING, skip |
| `compact_history` raises | Log ERROR, skip, history unchanged |
| `compact_history` returns `""` | Skip (too few messages to compact) |
| Provider doesn't support usage streaming | `chunk.usage` None, never triggers |
| Non-dict `stream_options` in body | Guarded by `isinstance`, fallback to `{"include_usage": true}` |

## Multiple Compactions

Each compaction inserts a new `compacted` message. `messages_for_ai()` finds the
**last** one. Earlier compacted messages (and all messages between system and the
last compacted) are trimmed before sending to AI. This means each compaction
replaces ALL prior compactions — only the most recent summary is active.

## Concurrency

Compaction runs within the agent's `_lock` (same as the stop handler). No new
requests are processed until the compaction AI call completes and commits.
The compaction is a fast, single-turn LLM call for summarization.

## Cancellation Safety

- `complete_fn` uses `aclosing()` around `AiClient.stream()`
- `_maybe_compact()` catches `Exception` (not `CancelledError`)
- `CancelledError` propagates to `async with self._conversation.__aexit__` → rollback
- Compaction changes are rolled back; stop handler's assistant message is persisted

## Edge Cases

- **No system prompt**: `messages_for_ai()` finds no system_idx, falls through to normal projection (no compaction trimming)
- **Compaction signal without prior stop**: `finish_reason` tracked separately from `_compaction_needed` flag; handled gracefully by `"compaction_needed"` in exclusion set
- **Empty conversation**: ≤6 messages → compaction skipped (returns `""`)
- **System prompt at non-zero index**: `messages_for_ai()` searches for *first* system message, handles correctly

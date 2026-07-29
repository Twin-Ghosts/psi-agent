# Context Compaction Design Spec

## Overview

当对话 token 数超过 `max_context_tokens` 时，AI 层检测 upstream 用量并通知 Session
触发 context compaction：调用 `system.py` 的 `compact_history()` 生成 LLM 摘要 + 近期
对话原样，以独立 `compacted` 消息存入 conversation。`messages_for_ai()` 发送前自动裁剪。

阈值可按 AI 后端经 Gateway `POST /ais` 配置。摘要**链式累积**（新摘要在上一份之上
更新，不丢更早上下文，并有 `SUMMARY_MAX_CHARS` 上限），且 `_maybe_compact()` 带
**冷却门槛**——因为压缩改不了 system prompt 的体积，当提示词本身占阈值很大比例时
信号会每回合复发。

## Flow

```
upstream LLM → AI Layer
                  │ stream_options={"include_usage": true}
                  │ chunk.usage.prompt_tokens > max_context_tokens
                  ↓ post-stream SSE: psi_compaction signal
             Session
                  │ AiClient.stream() → AiDelta.compaction_needed (+ prompt_tokens/threshold)
                  │ Agent._maybe_compact() → cooldown gate
                  ↓ compact_history() from system.py (chains previous summary)
             compacted message inserted → commit() → watermark recorded

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

Also settable per AI backend through the Gateway: `POST /ais` body field
`max_context_tokens` → `AIManager.create(max_context_tokens=…)` → `Ai(...)`.
Omitted / `-1` keeps `Ai`'s own resolution, so existing deployments are
unaffected; state snapshots written before the field existed restore via
`cfg.get("max_context_tokens", -1)` and need no migration.

**Pick the threshold well below the model's real context window.** Compaction
cannot shrink the system prompt, and the compaction call itself needs headroom —
too high a threshold turns "summarizes too often" into "upstream rejects the
request". Reference measurement: the `haitun-workspace` system prompt is ~45.4K
tokens (tiktoken `o200k_base`), i.e. 45% of the 100K default.

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
- `AiDelta.prompt_tokens: int = 0` / `AiDelta.compaction_threshold: int = 0` — the
  numbers carried by the signal, needed by the cooldown gate (0 = unknown)

### AiClient (`ai_client.py`)
- Parses top-level `psi_compaction` field from SSE data (with `isinstance` guard)
- Sets `AiDelta.compaction_needed` on appropriate deltas
- Extracts `prompt_tokens` / `threshold` via `AiClient._as_int`, which returns 0
  for absent or malformed values and rejects `bool` explicitly (a JSON `true`
  would otherwise become `1` because `bool` subclasses `int`)

### Agent Loop (`agent.py`)
- Stop handler moved out of inner `for delta in stream` loop to allow compaction signal to arrive before processing
- `_compaction_needed` flag tracked per-turn, alongside the signal's
  `prompt_tokens` / `threshold`
- `_maybe_compact(prompt_tokens=0, threshold=0)`:
  1. Gets `compact_history` from `SystemPrompt`
  2. Cooldown gate — see "Compaction Cooldown" below; blocked → return
  3. Builds `complete_fn` closure (streaming via `AiClient` with `aclosing()`)
  4. `summary = await compact_history(conversation.messages, complete_fn)`
  5. If `summary` empty → skip
  6. Inserts compaction message: `{"role": "compacted", "content": summary, "kind": "compacted"}`
  7. `commit()` — all history preserved in JSONL
  8. Records `_tokens_at_last_compaction = prompt_tokens or None` **only on
     success**: a failed compaction shrank nothing, so the next signal must
     still be allowed through

### Compaction Cooldown

`COMPACTION_COOLDOWN_FRACTION = 0.1` (module constant in `agent.py`).

The signal only reports that `prompt_tokens` exceeded the threshold, and
compaction cannot shrink the system prompt. When the system prompt is a large
share of the threshold the signal re-fires every turn, so without a gate the
session re-summarizes back to back — each pass costing an LLM call and eroding
older context. `_compaction_cooldown_elapsed()` requires `prompt_tokens` to have
grown by at least `threshold * COMPACTION_COOLDOWN_FRACTION` since the last
successful compaction.

Measured in upstream-reported `prompt_tokens`, **not** message count: a single
tool result can be tens of thousands of tokens while two chat messages are a few
hundred, so a count-based gate is meaningless for tool-heavy turns.

Fails open — when the signal carries no usable numbers (older AI layer, malformed
field) compaction proceeds as before. State lives in a `SessionAgent` instance
attribute; the object is built once per session process and is therefore valid
across turns, and resets on process restart (acceptable: at most one extra
compaction after a restart).

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

Default strategy (all 11 example workspaces), driven by two module constants:

| Constant | Value | Meaning |
|---|---|---|
| `RECENT_TURNS_KEPT_VERBATIM` | 20 | trailing messages kept verbatim (~10 exchanges) |
| `SUMMARY_MAX_CHARS` | 8000 | hard cap on the carried-forward summary |

- `len(history) <= RECENT_TURNS_KEPT_VERBATIM + 2` → return `""` (skip).
  **The guard must track the keep count.** Otherwise `history[:-20]` on a short
  history is empty and the function returns a verbatim-tail-only, *non-empty*
  string; the agent writes a `compacted` row on any non-empty return, and
  `messages_for_ai()` then deletes every real message — trading a structurally
  intact conversation for flattened text.
- Older messages are summarized via LLM; the last `RECENT_TURNS_KEPT_VERBATIM`
  are appended verbatim as a `[Recent turns]` section.
- **Chaining**: the most recent `compacted` row is fed back as
  `<existing-summary>` so the model *updates* it instead of describing only the
  newest slice. Only the last one is used — earlier summaries are already folded
  into it, and replaying them would re-introduce stale context.
- When there is nothing new to summarize but a previous summary exists, it is
  carried forward without an LLM call.
- On `complete_fn` failure the fallback text still preserves the previous summary.
- The result is capped by `_cap_summary` (head-kept truncation). Chained summaries
  grow monotonically and land in the system prompt, so an uncapped summary would
  shrink the very budget it protects and make compaction fire *more* often.

Note the same-named `System.compact_history` **method** in these files is unused
prototype code — `SystemPrompt._extract_async_func` picks up the *module-level*
function. Only the module-level one is live.

## Error Handling

| Scenario | Behavior |
|---|---|
| `max_context_tokens == 0` | Compaction disabled |
| No `compact_history` in system.py | Log WARNING, skip |
| `compact_history` raises | Log ERROR, skip, history unchanged |
| `compact_history` returns `""` | Skip (too few messages to compact) |
| Provider doesn't support usage streaming | `chunk.usage` None, never triggers |
| Non-dict `stream_options` in body | Guarded by `isinstance`, fallback to `{"include_usage": true}` |
| Signal repeats before enough growth | Log INFO, skip (cooldown); watermark unchanged |
| Signal carries no / malformed numbers | Cooldown fails open, compaction proceeds |
| `compact_history` raises after a prior compaction | Watermark not updated, next signal still allowed |

## Multiple Compactions

Each compaction inserts a new `compacted` message. `messages_for_ai()` finds the
**last** one. Earlier compacted messages (and all messages between system and the
last compacted) are trimmed before sending to AI, so only the most recent summary
is on the wire.

That is safe because the default `compact_history` **chains**: the latest summary
is produced by updating the previous one, so it subsumes rather than discards
earlier context. A `compact_history` that ignores the incoming `compacted` row
would instead lose one more layer of history on every pass — which is exactly the
"forgets earlier conversation" failure mode chaining was added to fix.

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
- **Empty / short conversation**: `≤ RECENT_TURNS_KEPT_VERBATIM + 2` messages →
  compaction skipped (returns `""`)
- **System prompt at non-zero index**: `messages_for_ai()` searches for *first* system message, handles correctly

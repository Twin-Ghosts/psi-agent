# Assignment Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an append-only feedback thread for work assignments so agents can record clarification, waiting states, and recipient confirmation without overwriting history or auto-resuming execution.

**Architecture:** Memory owns the feedback data model, versioning, state, and field-level visibility. The MCP server exposes one high-cohesion `assignment_feedback` tool that fronts that service. The Agent workspace adds one feedback tool module plus skill/prompt updates so the model can create, append, and render a single active feedback card without touching `src/`.

**Tech Stack:** Python 3.14, `anyio`, `pytest`, `psycopg`-backed repository tests, existing Feishu card/message tools, `ruff`, `ty`.

## Global Constraints

- All Agent-side changes stay under `examples/haitun-workspace/`; do not edit `src/`.
- Feedback records are append-only; `raw_content` is immutable.
- `arrangement_id` is the thread anchor; `task_id` may be backfilled later.
- One active card per feedback thread; updates must reuse the existing card.
- After assigner reply, the default state is `updated_waiting_recipient_confirmation`; do not auto-resume execution.
- Memory and Agent should each keep a single high-cohesion feedback entrypoint; do not split this into many tiny tools.

## File Map

| Area | Files | Responsibility |
|---|---|---|
| Memory model | `fusion_memory/assignment_feedbacks.py` | Dataclasses and service for feedback threads/entries/state transitions |
| Memory persistence | `fusion_memory/assignment_feedbacks_postgres.py` | Postgres repository for append-only feedback storage |
| Memory schema | `fusion_memory/storage/migrations/postgres/009_assignment_feedbacks.sql` | Create feedback thread/entry tables and indexes |
| Memory wiring | `fusion_memory/mcp_server.py`, `fusion_memory/storage/postgres_store.py` | Expose the new MCP tool and ensure schema validation knows the tables |
| Memory tests | `tests/test_assignment_feedback_service.py`, `tests/test_assignment_feedback_postgres_repository.py`, `tests/test_mcp_server.py` | Service, repository, and MCP contract coverage |
| Agent tool | `examples/haitun-workspace/tools/assignment_feedback.py` | Single workspace tool that manages feedback and card lifecycle |
| Agent guidance | `examples/haitun-workspace/skills/work-assignment-delegation/SKILL.md`, `examples/haitun-workspace/systems/prompt_sections.py`, `examples/haitun-workspace/systems/system.py` | Teach the model when to use feedback and how to keep facts vs. analysis separate |
| Agent tests | `examples/haitun-workspace/tests/test_assignment_feedback.py`, `examples/haitun-workspace/tests/test_tool_discovery.py`, `examples/haitun-workspace/tests/test_prompt_sections.py` | Tool behavior, discovery, and prompt guidance |

### Task 1: Memory feedback model, repository, and migration

**Files:**
- Create: `fusion_memory/assignment_feedbacks.py`
- Create: `fusion_memory/assignment_feedbacks_postgres.py`
- Create: `fusion_memory/storage/migrations/postgres/009_assignment_feedbacks.sql`
- Modify: `fusion_memory/storage/postgres_store.py`
- Test: `tests/test_assignment_feedback_service.py`
- Test: `tests/test_assignment_feedback_postgres_repository.py`

**Interfaces:**
- Produces `AssignmentFeedbackThread` and `AssignmentFeedbackEntry` dataclasses.
- Produces `AssignmentFeedbackService.manage_feedback(...)` as the in-process API.
- Produces `PostgresAssignmentFeedbackRepository` with append-only thread/entry operations.

- [ ] **Step 1: Write the failing service tests**

Add tests that prove the following behaviors:

1. Creating a thread under one `arrangement_id` returns `state="open"` and stores the first entry as version 1.
2. Appending a second entry preserves the first entry’s `raw_content` and increments the thread version.
3. A `private_note` entry is readable only through the appropriate role-filtered view.
4. Replying from the assigner moves the thread to `updated_waiting_recipient_confirmation`.
5. A recipient confirmation moves the thread to `ready_to_execute`.

Run:

```bash
pytest tests/test_assignment_feedback_service.py -v
```

Expected: fail on missing module / missing service behavior.

- [ ] **Step 2: Write the failing repository tests**

Add tests that prove the repository round-trips:

1. Thread and entry rows persist separately.
2. Entries are append-only.
3. A thread lookup by `arrangement_id` returns the latest state and card id.
4. A list query ordered by `updated_at desc` returns the newest threads first.

Run:

```bash
pytest tests/test_assignment_feedback_postgres_repository.py -v
```

Expected: fail on missing tables / missing repository implementation.

- [ ] **Step 3: Implement the memory model and SQL**

Implement the dataclasses, service, repository, and migration so the tests pass. Keep the schema append-only: one table for threads, one for entries, one or more indexes for `arrangement_id`, `state`, and `updated_at`.

Update `fusion_memory/storage/postgres_store.py` if schema validation needs to know the new table names.

- [ ] **Step 4: Run the memory tests again**

Run:

```bash
pytest tests/test_assignment_feedback_service.py tests/test_assignment_feedback_postgres_repository.py -v
```

Expected: pass.

- [ ] **Step 5: Commit the memory slice**

Commit only the memory model, repository, migration, and tests.

### Task 2: Memory MCP exposure and contract tests

**Files:**
- Modify: `fusion_memory/mcp_server.py`
- Modify: `fusion_memory/storage/postgres_store.py` if the MCP startup path checks table names or schema completeness
- Modify: `tests/test_mcp_server.py`

**Interfaces:**
- Produces one new MCP tool:

- `assignment_feedback(arrangement_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]`

- The tool forwards organization scope from the token, never from the payload.
- The tool dispatches into `AssignmentFeedbackService.manage_feedback(...)`.

- [ ] **Step 1: Write the failing MCP contract tests**

Add coverage that proves:

1. `assignment_feedback` is registered only when the feedback service is present.
2. The tool does not expose `organization_id` in the schema.
3. The tool forwards the token-derived organization claim and the actor identity.
4. Missing schema errors mention the new migration file for feedback tables.

Run:

```bash
pytest tests/test_mcp_server.py -k feedback -v
```

Expected: fail until the new tool and schema hint exist.

- [ ] **Step 2: Implement the MCP tool wiring**

Add the new tool to `create_mcp_server(...)`, thread it through `create_mcp_app(...)`, and hook it up in `run_mcp_server(...)` beside the existing assignment and publication services.

Keep the tool payload minimal. The service owns the real state machine; the MCP layer only forwards `arrangement_id`, `action`, and an optional structured payload.

- [ ] **Step 3: Run the MCP tests again**

Run:

```bash
pytest tests/test_mcp_server.py -k feedback -v
```

Expected: pass.

- [ ] **Step 4: Commit the memory MCP slice**

Commit the MCP surface and its tests separately from the data-model slice if the repo history is still being kept tight.

### Task 3: Agent feedback tool and guidance

**Files:**
- Create: `examples/haitun-workspace/tools/assignment_feedback.py`
- Modify: `examples/haitun-workspace/skills/work-assignment-delegation/SKILL.md`
- Modify: `examples/haitun-workspace/systems/prompt_sections.py`
- Modify: `examples/haitun-workspace/systems/system.py`
- Modify: `examples/haitun-workspace/tests/test_tool_discovery.py`
- Modify: `examples/haitun-workspace/tests/test_prompt_sections.py`
- Create: `examples/haitun-workspace/tests/test_assignment_feedback.py`

**Interfaces:**
- Produces one public coroutine:

- `assignment_feedback(receive_id: str, arrangement_id: str, action: str, payload_json: str = "{}", receive_id_type: str = "open_id", user_key: str = "") -> str`

- The tool is the only new Agent-side entrypoint for this feature.
- It uses the existing Feishu card/message helpers for one live card per feedback thread.
- It sends `assignment_feedback` MCP payloads and keeps raw-vs-shared content separate.

- [ ] **Step 1: Write the failing tool tests**

Add focused tests that prove:

1. The tool rejects malformed JSON payloads.
2. The tool creates or updates a feedback thread through Memory.
3. The tool updates the same active card instead of sending a second one.
4. The tool renders shared content without leaking private notes.
5. The tool leaves assigner replies in `updated_waiting_recipient_confirmation` instead of auto-resuming execution.

Run:

```bash
pytest examples/haitun-workspace/tests/test_assignment_feedback.py -v
```

Expected: fail until the tool exists.

- [ ] **Step 2: Update discovery and guidance tests**

Add the new tool to the workspace discovery assertions and update prompt-section tests so the model is explicitly steered toward:

1. using the feedback tool for blocking / non-blocking clarification,
2. preserving raw content,
3. not treating assigner replies as execution authorization,
4. keeping one active card per thread.

Run:

```bash
pytest examples/haitun-workspace/tests/test_tool_discovery.py -k feedback -v
pytest examples/haitun-workspace/tests/test_prompt_sections.py -v
```

Expected: fail until guidance mentions the new workflow.

- [ ] **Step 3: Implement the Agent tool and prompt updates**

Implement the tool module using the existing `CLIENT`, `assignment_get`, `assignment_list`, `feishu_message_send_card`, and `feishu_message_edit_card` helpers as needed.

Keep the module small:

- parse `payload_json`
- call the Memory MCP `assignment_feedback`
- render or update the single live card
- return a compact JSON result

Update the skill so the model knows when to invoke feedback instead of stuffing clarification into `assignment_transition`.

- [ ] **Step 4: Run the Agent tests again**

Run:

```bash
pytest examples/haitun-workspace/tests/test_assignment_feedback.py examples/haitun-workspace/tests/test_tool_discovery.py examples/haitun-workspace/tests/test_prompt_sections.py -k feedback -v
```

Expected: pass.

- [ ] **Step 5: Commit the Agent slice**

Commit the new tool, skill, prompt, and test updates as a separate Agent-side change set.

### Task 4: Cross-workspace verification

**Files:**
- No new files expected

**Interfaces:**
- Confirms the Memory service and Agent tool agree on the same action names and payload shape.

- [ ] **Step 1: Run the focused memory suite**

Run:

```bash
pytest tests/test_assignment_feedback_service.py tests/test_assignment_feedback_postgres_repository.py tests/test_mcp_server.py -k feedback -v
```

Expected: all pass.

- [ ] **Step 2: Run the focused Agent suite**

Run:

```bash
pytest examples/haitun-workspace/tests/test_assignment_feedback.py examples/haitun-workspace/tests/test_tool_discovery.py examples/haitun-workspace/tests/test_prompt_sections.py -k feedback -v
```

Expected: all pass.

- [ ] **Step 3: Run lint on touched files**

Run:

```bash
ruff check /public/home/wwb/memory/fusion_memory/assignment_feedbacks.py /public/home/wwb/memory/fusion_memory/assignment_feedbacks_postgres.py /public/home/wwb/memory/fusion_memory/mcp_server.py /public/home/wwb/Dolphin-Agent/examples/haitun-workspace/tools/assignment_feedback.py /public/home/wwb/Dolphin-Agent/examples/haitun-workspace/skills/work-assignment-delegation/SKILL.md /public/home/wwb/Dolphin-Agent/examples/haitun-workspace/systems/prompt_sections.py /public/home/wwb/Dolphin-Agent/examples/haitun-workspace/systems/system.py
```

Expected: no lint errors.

- [ ] **Step 4: Do one manual gateway smoke test**

Start the local gateway against the latest main plus the two feature slices, then simulate one assigner → recipient clarification round and confirm the thread stays on a single card and does not auto-resume after the assigner reply.

---

If a task exposes a mismatch between the Memory payload shape and the Agent tool wrapper, fix it in the Memory/MCP slice first and only then re-run the Agent tests.

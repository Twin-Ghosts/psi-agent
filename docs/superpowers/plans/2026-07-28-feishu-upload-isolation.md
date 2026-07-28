# Feishu Upload Isolation Implementation Plan

> **For agentic workers:** Follow TDD and review each task against `docs/superpowers/specs/2026-07-28-feishu-upload-isolation-design.md`.

**Goal:** Bind Feishu attachment storage and file delivery to the sender's Gateway-selected Session workspace without changing the shared Agent package.

**Architecture:** Gateway returns the authoritative Session workspace in the Feishu route. The channel caches the complete route, rejects routing failures when Gateway mode is enabled, downloads only after routing into `<workspace>/uploads`, and prevents delivery from outside that workspace.

## Task 1: Gateway route contract

- Add a failing integration assertion for the actual Session workspace in `POST /feishu/route`.
- Return that workspace from the handler and require it in the OpenAPI schema.
- Run the focused Gateway test.

## Task 2: Complete route and fail-closed resolution

- Add focused tests for complete-route caching and malformed/failed Gateway responses.
- Add a small internal route value object and cache it by `open_id`.
- Resolve message targets as `(ChannelCore, route)`; reject missing IDs or route failures in Gateway mode.
- Keep the shared core path only when no Gateway URL is configured.

## Task 3: Attachment placement and file output boundary

- Add failing tests for routed upload placement and unsafe path components.
- Store routed downloads under `<workspace>/uploads/<date>/<message_id>/` while preserving the legacy directory without Gateway.
- Add failing tests for in-workspace output, cross-workspace paths, and symlink escape.
- Resolve routed `FileChunk` paths and reject delivery outside the Session workspace.

## Task 4: Documentation and verification

- Update Channel/Gateway `AGENTS.md`, Chinese/English README, and OpenAPI descriptions.
- Run focused Feishu/Gateway tests, Ruff, formatting checks, Ty, and the full suite with isolated AppData.
- Review the diff for scope, commit, push, and open a PR against `main`.

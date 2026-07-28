# Feishu Upload Isolation Design

## Goal

Keep Feishu attachments and agent-returned files inside the user data workspace selected by Gateway for the sender's Session. Preserve the existing one-Session-per-user routing, the shared `examples/haitun-workspace` Agent package, and the existing single-session behavior when the channel is started without `--gateway-url`.

## Routing Contract

`POST /feishu/route` remains the only authority for `open_id` routing and returns the Session's actual user data `workspace` together with `session_id` and `channel_socket`. The Feishu channel caches that complete route; it does not create a workspace or change the shared Agent package.

With Gateway routing enabled, a message without a usable sender `open_id`, a failed route request, or a malformed route response is rejected. It never falls back to the shared `session_socket`. Routing occurs before attachment download.

Without Gateway routing, the channel keeps the legacy shared socket and download directory behavior.

## Attachment Layout

For a routed message, downloaded resources are stored under:

```text
<session-workspace>/uploads/<YYYY-MM-DD>/<message_id>/<sanitized-file-name>
```

The message ID and generated file names are reduced to path-safe components before use. This gives each message a separate directory and prevents path separators or traversal components supplied by Feishu metadata from changing the destination. With `--feishu-workspace-root users`, the complete multi-user layout is `users/<open_id>/uploads/...`; all Sessions can still share the same `examples/haitun-workspace` Agent package.

## Agent File Output

For routed messages, every `FileChunk` returned by the Session is resolved before it is sent to Feishu. The real path must be inside the resolved Session workspace. Resolving both paths also rejects traversal and symlink escapes into another user's workspace. The channel reports a generic processing error and does not disclose the rejected host path.

The check is scoped to Feishu attachment placement and direct file delivery. General workspace tools and absolute-path behavior remain unchanged; the runtime is still trusted and this is not an OS or container sandbox. An agent with arbitrary host tool execution can still escape a workspace, so stronger confinement requires a future tool or process sandbox and is deliberately outside this A- change.

## Scope

The change is limited to the Gateway route response/OpenAPI contract, Feishu message routing/download/send handling, focused tests, and operator documentation. It does not change `FileChunk`, Session/history protocols, SPA behavior, workspace tools, document-comment handling, approval handling, or the agent's channel-awareness prompt.

## Verification

Tests cover the route workspace contract, complete-route caching, fail-closed routing, per-user upload placement, path-component sanitization, in-workspace file delivery, cross-workspace rejection, and symlink escape rejection. Existing single-session behavior remains covered.

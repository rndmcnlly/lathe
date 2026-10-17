# Interpreter identity lifecycle

`interpret()` keeps one Daytona context per non-empty OWUI conversation ID in
one loaded `Tools` instance. A per-conversation asyncio lock serializes identity lookup, probing,
and creation, not Python execution. Concurrent code can still race on variables.

The registry is separate from the bounded chat-notice LRU. Evicting or
reconstructing notices does not lose an interpreter association. Records are
not evicted: memory grows with conversations that use `interpret()` until the
toolkit instance is unloaded. Each record holds a lock, sandbox ID, and context
ID, not Python values or output.

## Recovery contract

- First use creates a context. Missing conversation IDs are rejected rather
  than sharing a default namespace.
- A successful, structurally valid context list containing the remembered ID
  preserves identity. A valid list without it authorizes replacement and the
  existing state-loss warning. A changed sandbox ID also authorizes replacement.
- HTTP errors (including a list-route 404), transport failures, and malformed
  list responses fail the call without creating a replacement. Retry after
  recovery. Failed creation leaves the previously remembered identity intact.
- Context disappearance between the list probe and execution remains possible.
  Lathe does not automatically replay Python code: replay can duplicate effects.

## Persistence boundary

This is process-local identity, not durable conversation storage. OWUI restart,
toolkit reload/reconstruction, and requests routed to a different worker are
outside the continuity guarantee, even if the remote interpreter remains alive.
Use a single loaded toolkit instance for stateful conversations. Save important
results to files; sandbox stop/restart loses Python memory, regardless of volumes.

[Daytona's documented creation API](https://www.daytona.io/docs/en/python-sdk/sync/code-interpreter/)
accepts a working directory and returns a server-assigned ID. Its list API
exposes contexts, not OWUI conversation associations. Lathe therefore cannot
safely rediscover an association by choosing an arbitrary live context. This
change intentionally adds no database, sandbox mapping file, or distributed
lock. A create request that succeeds remotely but loses its response can leave
an unassociated context; no server-side idempotency contract is assumed.

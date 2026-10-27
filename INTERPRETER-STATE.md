# Sandbox-owned Python sessions

`interpret()` uses a stdlib Python supervisor and a persistent Python subprocess
inside the user's sandbox, not Daytona's interpreter API. OWUI workers only
submit and observe runs through short Daytona execution calls. No Lathe Redis
service or OWUI database association is required.

## Identity and authority

The session key is SHA-256 of a JSON array containing the deployment label,
trusted caller email (the same identity used for sandbox ownership), conversation
ID, and sandbox ID. JSON tuple encoding avoids delimiter collisions. Missing
conversation, user, or deployment identities are rejected.

The supervisor holds an OS `flock` for its entire lifetime. Bootstrap callers
coordinate startup separately; a delayed initializer cannot acquire a second
live supervisor's lifetime lock, unlink its socket, or overwrite its journal.
Python is initialized before a submission is admitted. Worker-local caches do
not choose interpreters. Overlapping submissions return busy without executing;
different sessions execute independently. This is exclusion, not a FIFO queue.

The sandbox is user-controlled. Its owner and their agents can inspect, modify,
replace, or destroy the service and its records. Namespaces are organizational,
not a security boundary against those agents. Symlink substitution and deliberate
journal replay are not defended against. This service receives no Daytona or
preview-wrapper credentials. Caller ownership, provisioning, destruction
authorization, and preview access policy remain outside sandbox authority.

## Execution and failure

Before dispatching Python, the supervisor atomically publishes a run record and
an active claim. Each submission has a random execution ID and a fingerprint of
its code/timeout. Repeating the same ID retrieves its existing record; changing
its code is refused. Toolkit polling only observes that ID, never resubmits code.
Lost HTTP/RPC responses and malformed responses report an uncertain outcome and
the record path, not permission to replay Python. Cancellation of an OWUI request
does not cancel the sandbox run.

Each session has a separate Python process with persistent globals. stdout and
stderr share a 50 KiB UTF-8 preview budget; excess output is discarded, with an
explicit notice. Output is delivered after completion, not streamed live. Python
exceptions retain ordinary Python memory. A timeout kills the Python process;
its memory is lost and partial external effects may remain. Timeout/crash results
do not preserve partial stdout; save important evidence directly to files.
Inline submissions are bounded by the remote command's encoded size; larger
programs should live in a file and be executed from a short snippet.

A durable generation marker distinguishes first use from replacement of Python
memory. A subsequent run in a replacement process reports a reset, including
after sandbox stop/restart or archive/resume. Ordinary workspace files survive
those lifecycle changes separately. Sandbox deletion destroys both files and
session history; a newly provisioned sandbox starts fresh, with advisory lifecycle
warnings. Existing Daytona interpreter contexts are not adopted: upgrading this
implementation starts new sessions without migrating Python variables.
Do not mix Daytona-backed and sandbox-supervisor toolkit versions during a
rolling migration: update all OWUI workers together and tell users to rerun setup.

If a supervisor dies while idle, a new supervisor can replace its Python process
and report memory loss. If it dies with an unresolved run, startup blocks. A
timeout or an old PID is not permission to steal that claim. Completed run
publication can be reconciled if only clearing the active claim was interrupted.
Malformed journals and orphaned unresolved run records also block recovery.
Arbitrary filesystem tampering can invalidate these guarantees.

## Inspection and manual recovery

- Durable journal: `/home/daytona/.local/share/lathe/python/<key>/state.json`.
- Run evidence: the adjacent `runs/<execution-id>.json`, including status,
  generation, reset flag, fingerprint, and bounded output. Completed records are
  retained for at least 24 hours, with expiry on a later submission. These files
  may contain sensitive user output and are in the user's ordinary filesystem.
- Runtime socket, locks, diagnostic log, and process IDs:
  `/dev/shm/lathe/python/<key>/`. Stop/restart clears runtime files, not journals.
- `process.json` is inspection evidence, not authorization to kill a PID: verify
  the actual process identity before sending a signal.

For an unresolved run, inspect its record and any external effects first. Stop
the exact session supervisor and its Python process after verifying their
identities; an orphan Python process may still be executing. Only with user
approval, archive the session's durable directory and remove that session's
runtime directory after both processes have stopped. The next call starts a new
session. This deliberately discards the generation history, so tell the agent
that Python memory was reset. Do not delete all sessions or automatically replay
the interrupted submission. There is no automatic claim-stealing/reset endpoint.

Supervisors retain the code they started with until they exit; a toolkit update
does not hot-patch a running service. Coordinate service restarts for runtime
updates. Unknown journal versions fail closed rather than silently migrating.

## Other multi-worker state

Orientation suppression and deferred harness messages remain disposable local
state. Orientation may repeat. Background bash completion notices are registered
sandbox-side (`CMD/notify`, naming the chat) and claimed by an atomic rename, so
each is reported at most once, by whichever worker claims it: the launching
worker's poller on any tool call, or else any worker's next bash call in that
chat. A claim whose script result is lost in transit is not retried, and nothing
wakes an idle chat. Bash logs and exit evidence remain sandbox-side. Preview
registration is external; sandbox association/lifecycle authority stays in Daytona.
Consistent toolkit source and valves across workers remain an OWUI deployment
responsibility, not a Lathe configuration-distribution mechanism.

## Qualification

`test_interpreter.py` executes the production toolkit resolver in independently
loaded OS processes against a shared local filesystem with real sockets, locks,
supervisors, and Python subprocesses. These synthetic coordination tests do not
qualify Daytona daemon lifecycle behavior or real OWUI dispatch/events.
`test_integration.py` and `test_deployment.py` qualify those boundaries separately.

### Recorded qualification: 2026-10-21

Qualified `lathe.py` SHA-256 (before the release-only version bump to 1.1.0):
`8ef685177e46769fd7e1a48bf6feaf8db5bd132bbbdd3cd67ef38258e28dd4d4`.

- **Synthetic coordination:** `uv run pytest`, **510 passed**, covers independent toolkit
  processes/reloads, simultaneous admission, namespace/delimiter separation,
  requester death, lost-response deduplication, supervisor/Python process loss,
  corrupt journals, interrupted publication, bounded output, timeout resets,
  result retention, and a stale initializer's refusal to replace a live owner.
  Disposable in-memory mutations demonstrate detection of per-call namespaces
  and suppressed reset warnings. These are real local processes and filesystem
  locks, not simulations of the resolver.
- **Real Daytona:** `uv run python test_integration.py`, **9/9 passed**. Fresh
  toolkit OS processes shared Python memory, simultaneous first-use requests
  converged on exclusive admission, and stop/restart preserved ordinary files
  while a reloaded toolkit reported Python-memory loss. The isolated test
  sandbox was deleted on exit.
- **Real OWUI:** `uv run python test_deployment.py`, **5/5 passed**. Exact staged
  source/schema and real-model bash, write/read, interpreter, and image dispatch
  passed. The staging toolkit and its isolated sandbox were deleted on exit;
  production Lathe was not updated.

Those October 21 live checks are separate from synthetic coordination evidence.
They did not qualify requests load-balanced across multiple live uvicorn workers
or replicas, archive/resume, or every cold-provisioning/lifecycle race on Daytona.
Automatic background completion delivery remains deliberately best-effort, not
an unimplemented reliable-delivery guarantee. Adversarial record substitution by
the sandbox owner is outside the agreed threat model, not a qualification target.

### Additional 1.3.1 qualification: 2026-10-28 UTC

Qualified runtime SHA-256:
`a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`.

**Real Daytona lifecycle:** a disposable lab exercised the production resolver
and tools, with independent file downloads. Two concurrent resolver tasks sent
real competing creates: one platform 409 and authoritative name lookup converged
on the winner. This was not an independent-OWUI-worker test.

- Stop/archive/resume preserved ordinary file bytes, the prior generation marker,
  and a byte-identical completed run record. A fresh toolkit then reported Python
  memory loss, absent old globals, and a new generation.
- Delete/recreate produced a new sandbox ID, a fresh-environment warning, and a
  404 for the old ordinary file.
- A deliberately conflicting suite-owned name/owner-label association produced
  a real 409 and ownership refusal, without a subsequent toolbox request.
- All three owned sandbox IDs were deleted and per-ID 404 verified, followed by
  exact-label cleanup. No volumes; daytona-small (1 vCPU, 1 GiB, 3 GiB disk),
  30-minute TTL, 39.63-second run, no inference. Evidence and exact scope are
  recorded in [#110's lifecycle comment](https://github.com/rndmcnlly/lathe/issues/110#issuecomment-6457832043).

**Live OWUI routing, partial lab result:**
[Actions run 40614126169](https://github.com/rndmcnlly/lathe/actions/runs/40614126169)
tested lab commit `773bdb4801f681b5453da7e5b01a383f51d6a9ea` on released OWUI
0.12.4, image digest
`sha256:41812075aede8ada8497cb2b71c76412978633eee72775bb77796f593d03b677`.
Two processes shared one disposable container filesystem, PostgreSQL/pgvector,
Redis, source, valves and keys; this is not an arbitrary-replica qualification.

Actual handler observations established A (PID 7) → B (23) → A (7), followed by
continuity on replaced B (64). Four completed `interpret` calls and independent
file/state downloads showed counts 1–4, an unchanged sandbox-generated nonce,
Python PID 153 and generation `2e3f8b7c88e944a1add626b0c77f57a6`. The expected
nonce was not supplied in model prompts. Requested and observed model was
GLM-5.3-Flash. Sandbox deletion and container/network absence were verified.

That run's full scenario list did not pass: an extra final A request timed out
before another provider request was recorded (a relay body-size limit in the lab
harness is the likely cause, not established). The routing and replacement
observations above are the accepted #110 result.

**Not qualified:** overlapping first use of one interpreter from two workers;
arbitrary replicas across hosts; Lathe-managed background completion waking an
idle chat (#67, no supported OWUI dispatch boundary). OWUI's native
`delegate_task(background=true)` is an alternative outside Lathe: in
[run 40624797681](https://github.com/rndmcnlly/lathe/actions/runs/40624797681)
a real child ran and OWUI resumed the idle parent with its model, toolkit and
system prompt, and independent downloads matched. That run's evidence collector
then failed, and the busy-parent case was never reached.

The lab (two-worker OWUI with PostgreSQL/pgvector and Redis, pinned images,
credential-free preflight) is archived unmaintained at tag
[`readiness-lab-archive-20261028`](https://github.com/rndmcnlly/lathe/tree/readiness-lab-archive-20261028).
Nearly all of its failed live attempts were harness defects, not Lathe defects.

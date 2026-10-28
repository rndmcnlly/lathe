# Disposable OWUI infrastructure preflight

This qualifies local infrastructure, without `.env`, platform credentials, cloud
resources, or inference. It does **not** qualify real-model dispatch, native
subagents, Daytona wake-up, or multi-host/shared-filesystem production deployment.

```sh
uv run python readiness-infra/lab.py --preflight
uv run python readiness-infra/lab.py --cleanup readiness-infra/runs/<run>
uv run pytest readiness-infra/test_infra.py
uv run python readiness_lab.py --preflight  # also exercises the live controller, without keys/inference
```

`--artifacts NEW_DIRECTORY` chooses evidence storage. `--source REPO_DIRECTORY`
chooses the exact `lathe.py` and independent `testing_support.py` contract to stage.
There is no retained-running mode. A normal run, failed run, interrupt, or
termination attempts cleanup and verifies absence. After a host crash or SIGKILL,
use `--cleanup` with that run's `owner.json`. It refuses mismatched resource
labels/names and networks containing foreign endpoints. It never prunes Docker.
Disposable image tags/build cache remain available; no container or network does.

## Small, explicit topology

* Digest-pinned OWUI 0.12.4, PostgreSQL 16 with pgvector, and Redis 7.4.6.
  The controller rejects disagreement between the recorded OWUI reference and
  the Dockerfile's single actual `FROM` before building.
  `images.json` records immutable multi-platform image/index digests resolved
  October 27, 2026. Native Docker architecture is used, not forced emulation.
* A Dockerfile with a five-file allowlisted context. No repo root build context,
  dotenv reads, secret build arguments, bind mounts, or credential forwarding.
  Socket/HTTP helpers already exist in the pinned OWUI image. Lathe additionally
  needs cachetools: its hash-pinned 6.2.1 wheel is installed at build time with
  `--no-deps`, without upgrading OWUI packages. Build-time imports, version
  assertions, and `pip check` validate compatibility. Frontmatter runtime pip
  installation is explicitly disabled.
* One migration subprocess completes successfully before either of two real
  uvicorn processes starts. Workers have migrations disabled. PostgreSQL and
  Redis health gates precede app startup. pgvector's extension is created before
  migration. Both processes share DB, Redis, signing keys, and the same ephemeral
  filesystem. There are no named or anonymous volumes: database/cache/app data
  use tmpfs. No service publishes a host port.
* One `--internal` Docker network blocks internet egress. A Python socket guard
  records external DNS/connect attempts through the patched Python socket APIs,
  including swallowed errors. Only loopback and this run's DB/cache addresses
  are allowed. It does not observe native-library, subprocess, or UDP attempts.
  This guard is instrumentation, not comprehensive traffic auditing or a
  security sandbox for malicious tool code.
* The outer ASGI observer handles `/_readiness/*` before the SPA catch-all and
  attaches the actual process PID to HTTP responses. It is lab-only code, not a
  Lathe resolver or an OWUI production feature.

## Observed boundaries

The preflight verifies health and version per process; actual uvicorn PIDs;
one completed migration process; a shared filesystem marker; separate synthetic
admin and ordinary-user accounts; cross-worker token/DB compatibility; exact
tool source and the independent public schema through OWUI's real HTTP loader;
snapshot-only staged overrides (no CPU/memory/disk); independent valve readback
from the other worker; and authenticated Socket.IO delivery from process A to
an ordinary-user socket on B through OWUI's production Redis manager.

The event injection endpoint authenticates the ordinary user and emits only to
that user's room. It does not simulate a model completion or Lathe event emitter.
No providers or models are configured. No sandbox-backed tool is invoked. The
schema-loading test does not establish native model dispatch, background
completion behavior, tool authorization at completion, or absence of stale
worker-local valve caches during a real completion. Valve readback verifies DB
configuration, not Daytona's interpretation of a snapshot name.

OWUI 0.12.4 attempts automatic Ollama host probing even with that provider
disabled. Explicit inert loopback URLs suppress that startup autodetection;
the preflight requires zero guard-observed Python external DNS/connect attempts,
not zero network attempts across all native code or subprocesses.

`result.json` records image/source/infrastructure hashes, including the exact
staged `testing_support.py` contract bytes, the built image ID,
boundary outcomes and failure phase. `cleanup.json` is written only after exact
owned container/network absence is verified against successful daemon listings.
Neither auth response bodies nor generated passwords/JWTs are retained.
Evidence-write failure cannot skip cleanup: the active controller retains its
validated ownership manifest in memory. Original, result-write, and cleanup or
receipt-write failures are preserved together in an exception group. Failure to
write a cleanup receipt is not reported as success, even after verified removal.
Wall time is bounded to 15 minutes, individual Docker operations have shorter
timeouts, and the app supervisor has its own 15-minute lifetime. SIGKILL/daemon
failure still requires operator cleanup. The controller deliberately does not
load provider-dependent `monitor_e2e.bootstrap` or `completion_account`.

The poka-yoke is the interface: a run either passes explicit infrastructure
boundaries and proves cleanup, or leaves a named failure and an exact cleanup
handle. No successful inference is needed to discover an import-path, migration,
SPA routing, dependency, schema, or Redis setup mistake.

## Shared live seam

`lab.topology()` owns the same pinned services, exact source staging, image ID,
health gates and exact-owner cleanup for both paths. `entrypoint.Workers` owns
the one-shot migration and actual worker start/stop/replacement. `worker:app`
owns ASGI identity and native-handler observation. The root scenario runner no
longer has its own image tags, Docker bootstrap, worker app or process launcher.

The live runner first completes the 22 infrastructure gates, then a second
key-free internal topology exercises its actual setup controller: real worker
replacement (B changes, A stays), persistent shared configuration, synthetic
ordinary-user tool loading, restart of both workers, and provider/model/tool/
normalized loaded-valve checks through each worker. Neither topology inherits
cloud keys or configures a usable external provider. They make zero inference
requests. A failed gate stops before cloud access. Fresh workers initialize
persistent configuration sequentially to avoid a shared singleton-insert race.

Only then may a third fresh topology receive explicit live credentials and the
exact previously built image ID. Source, helper and infrastructure hashes must
still match. Workers receive neither provider key via their process environment;
toolkit credentials arrive through authenticated OWUI configuration. Both live
workers restart and pass the same loaded-config gate after final valves are
installed and before provisioning. DB valve readback alone is not the gate.

The live network permits outbound routing, unlike the internal key-free network.
Its Python guard allows local services and Daytona API/toolbox hosts; only the
controller additionally allows OpenRouter. This is a Python DNS/TCP allowlist,
not a firewall against native libraries or user-controlled subprocesses. Guard
denials are observable. Credentials are never build inputs or evidence fields.
Request producers stop before exact-label cloud cleanup. The shared lifecycle
removes local resources even if the controller, cloud callback, or evidence
export fails. Successful runs require their trace and summary exports.

# Local qualification: October 27, 2026

No cloud credentials, dotenv files, Daytona resources, or inference were used.
Docker initially contained no containers. All changes for this task are new files
under `readiness-infra/`; no branch commits or GitHub operations were performed.

## Results

* Repository offline suite: **821 passed**, 53.80 seconds.
* Infrastructure ownership/staging/secret-safe-timeout tests:
  **9 passed**, 0.04 seconds (`uv run pytest readiness-infra/test_infra.py`).
  These are explicitly invoked: the repository's default pytest file selection
  does not collect this new test filename.
* Two consecutive complete real-container preflights:
  **22 boundary checks passed** each, 14.21 and 14.12 seconds.
  Both verified exact owned container/network absence after cleanup.
* Live SIGTERM injected after the owned app container entered running state:
  controller exited nonzero, retained the cancellation failure, and verified
  exact owned container/network absence.
* Standalone `--cleanup` on an already-clean completed run: passed, idempotent.

The complete runs used two actual uvicorn PIDs, one earlier migration PID,
matching shipped Alembic heads, live pgvector SQL, a separate ordinary user and
admin, exact staged source/schema, independent cross-worker valve readback,
cross-process authenticated Redis socket delivery, and zero guard-observed
Python external DNS/connect attempts. Native-library, subprocess, and UDP
attempts are not observed by that guard; the internal Docker network separately
blocks internet egress. No host ports or non-tmpfs mounts were present.

## Reproducibility receipts

Immutable OWUI/PG/Redis references are in `images.json`. The extra dependency is
cachetools 6.2.1, wheel SHA256
`09868944b6dde876dfd44e1d47e18484541eaf12f26f29b7af91b26cc892d701`.
Build-time `pip check` reported no broken requirements.

Qualified `lathe.py` SHA256:
`a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`

Qualified Dockerfile SHA256:
`aafce6bc47d587d0c7285920d456549afdc4cf65d8ddedaafd0b6cc3b7986cd2`

Second complete run's built image ID:
`sha256:5395df3cbf1958d2d1604c04e555e76245e3903e9cc3bab6748a9bf2f0be3150`

Its platform image manifest and config, from the retained build log:

* Manifest: `sha256:711eab4251aaadb4fbf3375fbbd2aec740f6c1d8e226de22b2b4418722e496d8`
* Config: `sha256:34da2f9fd04b8bf714e44e61bfebe44fc6f630aa409aa3599704f39a9aa9ccb1`

The built image ID includes Docker's generated provenance attestation and may
change between otherwise cached builds. This is pinned-input infrastructure
qualification, not a claim of byte-for-byte identical OCI output.

Ignored local evidence is retained in `runs/qualification-1` through
`runs/qualification-6` and `runs/cancellation-1`: full source/infrastructure hashes,
sanitized failures, successful boundary lists, build receipt, and exact cleanup
manifests. Every setup attempt verified cleanup, including the failed attempts.

## Setup faults caught and corrected

1. The slim OWUI image lacked Lathe's cachetools import. Real HTTP tool loading
   failed, then the explicit build import gate caught the missing dependency
   earlier. A hash-pinned build-time wheel with `--no-deps` corrected it.
2. Even with Ollama disabled, OWUI's default configuration attempted
   `host.docker.internal` discovery. The egress guard recorded and blocked these
   attempts; explicit inert loopback provider URLs eliminated autodetection.

The bounded failures are retained rather than silently retried or classified as
successful infrastructure. See `README.md` for the precise acceptance limits:
real-model/native execution, Daytona wake-up, completion authorization and
worker-local runtime valve cache refresh still require separate live acceptance.

## Independent-review corrections

The corrected controller and preflight passed a complete credential-free local
run in **16.18 seconds**, with all **22 checks passed** and exact owned resources
verified absent. Evidence: `runs/review-corrections-1/result.json` and
`runs/review-corrections-1/cleanup.json`. Image pins and CLI interfaces are unchanged.

Focused offline tests: **18 passed**, 0.05 seconds. Fault injection calls the
actual production `preflight()` and `cleanup()` paths. Disk-full, permission,
removed evidence directory, and receipt-only write failures still remove all
exact owned resources; grouped exceptions preserve the original setup failure
and each finalization failure. Cleanup retains validated ownership in memory,
not solely in the possibly unavailable evidence directory.

The controller now rejects disagreement between `images.json` and the actual
single Dockerfile `FROM` before building. Both qualification inputs are hashed
from staged bytes and independently checked in the app container:

* `lathe.py`: `a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`
* `testing_support.py`: `fec390fa753a630130b8259449331a759f4ef4de2be0173d8cc969c0278af4a6`

Corrected implementation SHA256:

* `lab.py`: `5ad356adc949ffdc434b3636be61b8e1acbfbedba9ef41d39b44a5d7b45af43f`
* `preflight.py`: `37035a364b1bdaa4a1472fe9c609eb9e3438f746bf1592f9ed3474ce8f0797ac`

Corrected run's built image ID:
`sha256:035a7c68eeb44f92b9c969a31ada7205d01b419e83b9e0062cd051b9d6282433`

The network result is explicitly **zero guard-observed Python external
DNS/connect attempts**, not an assertion about native-library, subprocess, or
UDP attempts. The internal Docker network separately blocks internet egress.

## Shared-path qualification

The shared lifecycle refactor passed **22 infrastructure offline tests**,
**177 readiness/monitor tests**, and **821 default offline tests**. These include
production-path adapters proving ordered key-free gates, identical live image
selection, no legacy Docker bootstrap, both-worker loaded-valve validation,
observable Python egress denial, and local cleanup after cloud/export failures.
The original evidence-write fault tests remain intact.

The final local shared-path trial (`runs/shared-final-preflight-1` and
`runs/shared-final-config-1`) passed all **22 real-container infrastructure gates**
and the actual controller's key-free configuration/replacement trial. It made
**zero inference requests**, reserved **$0**, and verified exact owned resource
absence for both fresh topologies. Actual PIDs/incarnations, one migration,
stable A across B replacement and normalized loaded-toolkit checks through both
restarted workers are retained in `runs/shared-final-config-evidence-1/trace.jsonl`.
Live native behavior remains a separate paid qualification, not established by
this key-free controller trial.

## Single native-only attempt: NOT REACHED

Source commit: `98e47fc8626a5f52fac087ea2d42584eb871c99b`.
Workflow: [40618676311](https://github.com/rndmcnlly/lathe/actions/runs/40618676311).
The run completed, including successful exact-run teardown and evidence upload.
No second attempt was dispatched.

Both runner-side key-free topologies passed: all 22 infrastructure gates, then
the actual controller/config trial. B changed PID 20 → 29 while A stayed PID 11;
migration count remained one. After configuration restart, A39 and B56 both
verified provider/model access and the actual loaded toolkit/normalized valves.
Preflight, config trial and live topology used the identical built image ID:
`sha256:88045ab90cf420f247ce858c1870d839553f47bfb1f74bf599c344318a4fe346`.
The final local CLI trial used
`sha256:a34c05b0dda36e0a9b02b9416398c8e53193ebaba8f0e30e4afc862d2cd5d8d5`.

The live controller failed at its first Daytona snapshot request, before sandbox
creation or native dispatch: `ConnectError: Readiness infrastructure forbids
external network access`. The inner cleanup callback encountered the same guard
failure; the outer host cleanup and subsequent independent cleanup step both
verified cloud absence. All three exact local topologies were verified absent.

An offline reproduction through actual HTTPX reproduced the same error before
any socket connection. A second direct production-guard probe isolated the cause:
`socket.getaddrinfo("app.daytona.io", 443)` is permitted, but the equivalent
`bytes` hostname is rejected because the guard compares it against string names.
The guard's denial evidence renders that bytes value as `non-hostname`. The
denied-host tests did not cover permitted hosts through HTTPX/AnyIO. This is a
known shared-seam blocker, not evidence that Daytona is unreachable. The guard
was not relaxed and no paid startup debugging or retry followed.

Requested model: `z-ai/glm-5.3-flash`. Actual serving model: none. Inference
requests: **0**. Inference cost/reservations: **$0/$0**, with the $2 ceiling intact.
Sandboxes provisioned: **0**. Idle and busy native continuation: **NOT REACHED**;
there are no child/parent native observations or independent nonce/file proofs.
Overlapping first use remains unqualified; direct-bash wakeup remains unsupported.

Runtime SHA256 is unchanged:
`a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`.
Controller SHA256:
`a4258b70bf7eda739055f5db6b38e81a6768f59ba2215daf08ed7b247d22ccc1`.
Dockerfile SHA256:
`92b7551270e7ddb4d81f06b9cbf59ad0b24a76e1a613ce9f76c2ad500d334b68`.
Full sanitized receipts are in the workflow artifact and the ignored local copy
`monitor-artifacts/readiness-results/run-40618676311/`.

## Bytes-host fix and deterministic transport gate (no live retry)

The guard now canonicalizes supported hostname representations before applying
the unchanged exact allowlist: ASCII bytes, string/IDNA names, DNS case, one root
dot and normalized IP spellings. Invalid bytes and malformed names are rejected
explicitly and journaled before the client receives an error.

`transport_preflight.py` runs the actual guard and default HTTPX transport against
a loopback HTTP server with strictly scoped DNS answers. It verifies real HTTPX
bytes-host lookups, case/root-dot requests, snapshot-shaped JSON and byte-exact
download reads. Forbidden suffixes, another IDNA name, an unrelated IP and
malformed representations produce observed denials; no timeout substitutes for
that evidence. Fault injection proves the gate detects missing bytes handling
and a suffix-policy bypass. Additional HTTPX tests preserve preflight blocking
and the worker's lack of controller-only provider access.

The controller runs this key-free subprocess before installing its own guard or
contacting any platform, in both its config-only and live modes. It inherits no
platform credentials; fixture DNS and socket patches are restored. Its separate
`transport-preflight.json` receipt distinguishes intentional synthetic denials
from the real preflight's zero-guard-observed-external-attempt requirement.
This is synthetic transport qualification, not real platform DNS/TLS/API or
native acceptance. **No live/cloud retry has been dispatched.**

Verification after the fix: **224 focused tests passed** (46 infrastructure plus
178 readiness/monitor), and **821 default tests passed**. The earlier default
count of 829 was incorrect: default `testpaths` at that point omitted `test_readiness.py`.
Counts were checked against actual pytest collection.
The readiness workflow explicitly ran both the infrastructure and controller
contracts in addition to that earlier default suite.

Credential-free shared-image evidence:
`runs/shared-5719707561204a58bdbf48edc40baf31/`. Both fresh topologies passed and
verified exact owned resource absence: 22 infrastructure gates in 16.17 seconds,
then the actual controller/config gate in 26.14 seconds. Their identical image ID:
`sha256:9a2558b6b1ae49ca63a2852b939bd1c22915d30ada337dad2ae8e97a664a51bf`.
The first controller trace event is `transport_preflight`, before worker startup
or platform resolution. Its exported receipt records four real loopback HTTP
requests, actual HTTPX `bytes` lookups, a byte-exact independent download and six
observed denials. Provider requests/reservations remain **0/$0**; the $2 monetary
ceiling and other live limits are unchanged. Full staged source, helper and
infrastructure hashes are retained in each `result.json`.

## Final review: cleanup persistence and default offline gates

Exact daemon absence is now independently verifiable without writing a receipt.
The root cleanup controller retains validated manifests, records local cleanup
errors, independently verifies absence, and only then permits the exact-owned
cloud callback. A receipt-write failure after removal cannot skip that callback;
an unresolved producer, failed daemon read or missing live manifest still blocks
it. Final local teardown attempts every known topology even after callback or
evidence errors. An unwritable final report raises all sanitized failures together.

Production-path regressions invoke both actual cleanup implementations, faulting
only Docker responses, cloud callbacks and persistence: receipt failure after
removal, unresolved producers, unavailable daemon, callback interruption and final
report failure (including combined failures). Default pytest now includes
`test_readiness.py` and `readiness-infra/test_infra.py`. Live suites and Docker
qualification remain manual. No runtime/version change or live/cloud retry.

After these fixes, **912 default offline tests passed** (now including both cheap
safeguard files), and **230 focused infrastructure/readiness/monitor tests passed**.
Credential-free evidence: `runs/shared-5ba8f84925644a5d80d9b6738de70074/`.
The 22 real-container infrastructure gates passed in 16.04 seconds; the controller
qualification passed in 26.32 seconds, including four synthetic HTTP requests and
six observed guard denials. Both fresh topologies used
`sha256:2de8657cb6e2c16494fdf0beceb8ca5a33f899ef5cd60063019322d9e6d73d02`
and verified exact owned resource absence. Provider requests/reservations: **0/$0**.

## Authorized native-only trial at 9780e19: infrastructure failure, NOT REACHED

Completed workflow [40622252912](https://github.com/rndmcnlly/lathe/actions/runs/40622252912)
used source commit `9780e199a9cb81882589f20e4648df79a5f4c3f5`. Runner offline gates
passed: **912 default tests** and **230 focused tests**. Exactly one attempt was
dispatched; there was no automatic retry or implementation change after failure.

The first key-free topology failed after **26.02 seconds**, in `private services`,
before source staging, OWUI migrations/workers or boundary checks. Its PG
`pg_isready` command succeeded via the default Unix socket, but the subsequent
`psql` call to create the vector extension found no socket at
`/var/run/postgresql/.s.PGSQL.5432`. Controller/HTTPX qualification and the fresh
live topology were therefore **NOT REACHED**, as were both native idle/busy cases.
There are no delegate calls, parent/child/message associations, server-native
model/tool/worker observations, or independent nonce/file proofs from this run.

The pinned PG entrypoint was inspected read-only with no network or database
startup. It starts a socket-only initialization server (`listen_addresses=''`),
then stops it before executing the final server. The existing socket-only health
probe can admit that temporary server. The retained error is consistent with
this shutdown/startup gap, but PG container logs were not retained at this phase,
so an independent crash cannot be excluded. This is an **infrastructure readiness
failure**, not model/tool iteration failure or evidence about native upstream
behavior. A candidate next fix is an authenticated TCP readiness/SQL boundary that
cannot admit the socket-only initializer, plus bounded PG failure logs. That fix
and any subsequent trial require separate review/authorization.

Requested model: GLM-5.3-Flash; no serving model or price-catalogue check was
reached. Provider requests, inference cost and reservations: **0/$0/$0**. No Daytona
API request or sandbox provisioning occurred. The $2/40-request/128000-byte/
4096-output-token caps, ZDR/data-collection/price policy, TTL30 and wall25 limits
were unchanged and were never exercised by inference.

The built (not fully qualified) preflight image ID was
`sha256:808cf8370238e1d546beaed6d244dd457e87f22481b0907fa9e5a6cdfab5c95b`.
No live image comparison was reached. Runtime SHA256 remains
`a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`;
controller SHA256 was
`2134e1e5d12c357bed9658f9b6ea312039c379277340f9c7b01ddae55ce93b22`.
Every input source/infrastructure hash and pinned image reference is retained in
the uploaded `preflight/result.json` and the ignored local
`monitor-artifacts/readiness-results/run-40622252912/handoff.json`.

Exact owned container/network absence was verified after failure and again by
host/workflow cleanup. Both cleanup reports passed with `cloud_cleanup_attempted`
false: no cloud deletion or cloud-absence API query was needed or performed.
The read-only diagnostic container was removed. Overlapping first use remains
unqualified, direct-bash wakeup unsupported, and no #110/#67 closure is claimed.

## Final TCP PostgreSQL gate and deterministic initializer trial

The authorized key-free fix uses the same shared `lab.postgres_ready()` in both
the root controller and standalone preflight. `pg_isready` explicitly targets
`127.0.0.1:5432`; authenticated `psql` targets the identical endpoint, creates
pgvector and requires the SQL result `127.0.0.1|5432|1`. SQL errors fail immediately,
not through blind retries. A monotonic deadline bounds TCP readiness polling.
Startup failure retains only allowlisted state and event counts from a bounded
log tail before teardown, never raw log lines, credentials or SQL. Injected
diagnostic/evidence failures preserve the existing cleanup guarantees.

`postgres_trial.py` copies a small 45-second-bounded barrier hook into the exact
owned container's init directory using `docker cp`. It uses the pinned PG image,
an internal network and tmpfs, with no host mounts, persistent volumes or ports.
During the controlled pause, actual Unix readiness succeeds; the actual production
TCP helper rejects the initializer and does not advance to SQL. Releasing the
barrier permits final TCP SQL/pgvector success. The pinned entrypoint explicitly
sets `listen_addresses=''` for its temporary server; its SHA256 is
`9c440299ae04a0a79d55b8bf03307036d890a40979d2fb698073c9050d4b20a5`.
This proves the mechanism, not the historical cause of run 40622252912: without
its PG logs, an independent crash remains possible.

Local evidence:

* `runs/postgres-302c3b0369434549b28887a0599d5599`: barrier, Unix-ready/TCP-not-ready,
  production helper held, final TCP SQL passed, exact cleanup verified.
* `runs/postgres-fault-37bdc926dc86421296f39e472701e030`: an **in-memory only**
  production-helper mutation restores the Unix probe. The same real trial
  detects premature advancement/failure, retains sanitized startup facts
  (one Unix listener, zero TCP listeners), and verifies cleanup. No deployed
  runtime/toolkit or working-tree implementation was faulted.
* `runs/shared-4cd04d0711a041f39eb2401ad8200ef2`: all 22 infrastructure gates,
  actual controller/config replacement and restart gates, and synthetic live-policy
  HTTPX passed. Both fresh topologies used image
  `sha256:f392f3435cee5c250518dc3f619870574fe5b39b2d11e5378cb32c39e0840705`
  and verified exact owned absence. Provider requests/reservations: **0/$0**.

Runtime and controller source hashes remain unchanged from the previous trial.
The PG helper SHA256 is
`f0ea757059555bb97632efdbbd08f144cc0d08c2c8b6ae435bc2d00d67194321`.
Immutable base image references remain in `images.json`; full staged source,
image and infrastructure hashes are retained per topology in `result.json`.
The new explicit manual workflow selector `lab=readiness-preflight` supplies no
provider credentials and cannot enter live provisioning/inference. Native/cloud
qualification is still a separate authorization gate.

Offline guards: **924 default tests**, **242 focused infrastructure/readiness/
monitor tests**, including SQL-result faults, TCP timeout, diagnostic failure
before teardown, sanitized evidence, and the credential-free workflow boundary.

### Completed credential-free runner qualification

Exactly one manual `readiness-preflight` dispatch completed successfully:
[40624089294](https://github.com/rndmcnlly/lathe/actions/runs/40624089294), source
`0a8ae7663ef258ded6ad361d5cf568c19663d137`. Job duration: **3m6s**.
Runner guards passed **924 default** and **242 focused** tests.

The real initializer trial independently reproduced Unix-ready/TCP-not-ready,
proved the unchanged production helper did not advance during the pause, then
passed final TCP SQL/pgvector. Its entrypoint and helper hashes match the local
receipts above. The runner's resolved PG image ID was
`sha256:f1447071e6c8449dcf4056aa0994c81d3566ce5ee232de36b04dc25696a116dd`.
All 22 infrastructure gates and the real controller/config replacement/restart
gates passed, including the synthetic HTTPX transport's four loopback requests
and six observed denials. Both fresh shared topologies used exactly
`sha256:3988c50dc06b433ce6fdb1fb491fb803c790f5669b2be7940655a2419d6b3ec1`.

Every one of the three owned topologies verified exact container/network absence.
The separate always-run key-free cleanup also passed; evidence upload succeeded.
The monitor job, live readiness step and credential-bearing cloud cleanup step
were **skipped**. No provider credentials were supplied to executed steps.
Provider requests, reservations and inference cost: **0/$0/$0**. No Daytona
provisioning or native dispatch occurred. Next gate remains separately authorized
real platform/native qualification, not an automatic paid retry or issue closure.

Uploaded artifact: `readiness-40624089294-1`. Its retained local copy, including
full per-topology source/image hashes, cleanup receipts and workflow logs, is
`runs/runner-40624089294/`. Runtime and root controller remain unchanged.

### Authorized native-only trial at dad4390: idle observed, observer qualification failed

Exactly one trial completed: [40624797681](https://github.com/rndmcnlly/lathe/actions/runs/40624797681),
source `dad43900af91dd9c65c3331a5844fbc72f62453a`. Runner offline gates passed
**924 default / 242 focused tests**. All 22 key-free infrastructure checks and
the actual controller/config/HTTPX gates passed before platform access. Preflight,
config preflight and fresh live topology all used the exact built image ID
`sha256:47f063a1f7cc80baaaecf4b754125dcdc8657e0d11301fcffc49847b98fd8f7a`,
with identical staged source/helper/infrastructure hashes. The live TCP SQL and
pgvector gate passed. Both restarted workers independently verified loaded tools,
valves and provider/model configuration before one small sandbox was provisioned.

**Native idle: FAILED qualification due to a harness observer bug. Native busy:
NOT REACHED.** This was not a model/tool-iteration or unsupported upstream-protocol
failure. Actual idle behavior was observed:

* Parent `8bbf9346-a7f6-4a08-8871-9faa1c2b3930`, initial assistant message
  `bdecf960-b9b3-4c1a-9760-1c4c510da3b1`, made one real `delegate_task` call with
  `background=true`; OWUI returned delegation `deleg_1cfa7e97` and child chat
  `d27041b4-606a-417a-bed4-ea3d7304cf0b`. Saved child metadata matched parent,
  initial message and delegation, with `internal=true`, type `subagent`.
* Child assistant `736a4a8a-2155-4ba7-a1b9-26bca13fca31` called `interpret` once
  with the exact supplied Python, including `secrets.token_hex(16)`. The controller
  released only the filesystem barrier. The child completed normally.
* Without a second human request, the parent resumed as assistant
  `7c3e9b0e-2d5b-4243-903c-1a0b98f5c937`, called `read`, then `bash` to copy the
  payload. The server observer recorded worker A, PID34, incarnation
  `efb83ed1a41b4d4d9fb4df232f68adbb`, model `lathe-ci-direct`, tool IDs
  `["lathe_test"]`. Its system hash
  `670396c6fc151496a1a1666f0ba3e1178076c8a2df3d5584907dd01fa893f8af`
  matches the original system message in a post-run check. The observed request
  `internal` flag was false for the resumed parent, true for the child.
* The real controller independently downloaded source/copy files, passed its
  32-byte equality assertion, and emitted `native_child_results`. It then tried
  to read both workers' event files unconditionally. B51 had served configuration
  but no completion/native-handler request in this A-only case, so
  `/lab/artifacts/worker-B.jsonl` did not exist. That `FileNotFoundError` was the
  first failure; there was no `scenario_pass`, `native_context_verified` or final
  `independent_native_reads` event. The native busy scenario did not start.

The retained read-tool payload is `8637e4c80491fdbc56d9335941c76a21`; it was absent
from the initial system/task prompts. Its SHA256, derived post-run from that read
payload, is `4b54b9c5875bdd800eb0fd605408157e4c3317d842cb2315ff2df1f61d299ab2`.
The independent download responses themselves were not retained: their successful
comparison is established by actual controller execution reaching the following
`native_child_results` event. This is stronger than assistant text, but is not a
standalone retained download receipt. The observer/context comparison can be
completed from A's existing events post-run; that does not upgrade the failed
qualification or supply the missing busy/result-message evidence.

Requested and served model: **z-ai/glm-5.3-flash**. All **7** provider requests
returned HTTP200, maximum wire body **48,963 bytes**, maximum completion **1,986
tokens**. Summed provider-reported inference cost: **$0.004868994**; nonrefundable
cumulative reservations: **$0.20299460 / $2**. Daytona compute charge was not
measured. No monetary/request/output/TTL/wall/privacy/price constraint was raised.

The one sandbox, `acf6c346-9854-4758-a96b-870966fc5b27`, was deleted after request
producers stopped. All three exact local topologies were verified absent. Both
host and always-run workflow cleanup passed, including independent cloud-label
absence checks. Evidence upload completed. No implementation patch or live retry
followed. Next review concerns the observer's assumption that every healthy worker
must already have an event file, and early persistence of the independent oracle.

Full actual events, per-topology source/image hashes and cleanup receipts are in
artifact `readiness-40624797681-1`, retained locally under
`monitor-artifacts/readiness-results/run-40624797681/`. Its ignored `handoff.json`
contains validated IDs/context, all input hashes, usage/costs, cleanup reports and
explicit proof limitations. Runtime SHA256 remains
`a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`;
root controller remains `2134e1e5d12c357bed9658f9b6ea312039c379277340f9c7b01ddae55ce93b22`.
Overlapping first use remains unqualified and direct-bash wakeup unsupported;
neither #110 nor #67 is closed or fully qualified by this run.

### Observer/oracle correction: offline and key-free only

Worker streams now start eagerly with identity/start receipts, even with zero
completion events. Both actual key-free OWUI/controller paths verify them against
HTTP identity and real PID. Replacement startup must find the prior stream and
supervisor-known PID, and links previous incarnations; streams cannot silently
restart from missing/truncated history. Consumers distinguish startup from actual
completion/native-handler events, and reject missing/unreadable/malformed streams
and wrong worker/incarnation/context/association or human-resubmitted continuation.

The shared production native-evidence pipeline now persists the paired synthetic
32-byte canary source/copy before observer collection. Small fixed-name receipts
contain sandbox ID/path, byte count, SHA256 and base64 of the actual downloaded
bytes. Paths, sandbox IDs, lengths and lowercase-hex content are constrained;
arbitrary sandbox files, headers and credentials are not retained. Receipt write
failures still reach exact-owner teardown. Artifact export requires both worker
streams even for successful config-only qualification.

Offline fixtures use selected retained events from run 40624797681, clearly
separating real native record shapes from synthetic startup test records. Replay
calls the actual writer/collector/context/oracle functions, not a model or native
dispatcher replacement. The historical run remains **FAILED qualification** and
its busy case **NOT REACHED**. No live/provider dispatch accompanies this fix.

### Review fixes after 7a4ef7c: offline only

Continuation selection now requires exactly one resumed parent reply for the
delegation, both in the saved chat (zero pending, two or more fail, re-checked
after evidence collection) and in worker handler observations (every
unsubmitted parent-chat entry must be that one message). Canary receipts persist
before continuation/child traces and model tool-use assertions; if the oracle
itself fails, the behavior diagnosis still runs and both are reported. Artifact
export attempts canary receipts first and every file independently, then reports
all controller/stop/export failures. Fault-injected replay and controller tests
fail against 7a4ef7c. No live, cloud or provider calls accompanied this change.

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

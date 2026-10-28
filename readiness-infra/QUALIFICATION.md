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
**177 readiness/monitor tests**, and **829 default offline tests**. These include
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

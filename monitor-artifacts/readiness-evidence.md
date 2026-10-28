# Readiness lab: partial live proof, cleaned up

## Sandbox discovery/cleanup fix after 40628835065 (offline + key-free only)

Read-only check (local `.env` Daytona key; same org as the repository secret is
unverified; that key sees 3 sandboxes total): exact `{label: email}` filter for
both lab identities, errored/deleted included, plus name-prefix, for all 9
`lathe-ci-next-*` labels in retained evidence: **0 remaining each**, no
`lathe-ci-` names. Nothing deleted.

Root cause class: Daytona `GET /sandbox` is eventually consistent and paginates
with `nextCursor`; the old readiness lister read `cursor`, listed unfiltered and
matched label keys locally (page 1 only; errored/deleted excluded). Fix: exact
server-side filter over every page with client re-check; bootstrap polls (bounded)
until exactly one, confirms by `GET /api/sandbox/{id}`, reports the observed
count and IDs only; cleanup deletes only exact-labelled lab sandboxes and requires
three consecutive empty reads. Against the old code the offline scenarios show:
lagging index and page-2 owned sandbox fail bootstrap (observed 0), two owned
falsely accepted (page 1 only), page-2 straggler falsely passes absence.
`test_deployment.cleanup_test_sandboxes` has the same `cursor` defect; the
readiness lab no longer uses it, and it was not changed here.

## Native-only trial 40628835065 at dc69ff0: FAILED in bootstrap, native NOT REACHED

Run: https://github.com/rndmcnlly/lathe/actions/runs/40628835065 (one dispatch,
no retry). Source `dc69ff0038c903ee748c8e93f65a26e1256aaa01`; `lathe.py`
`a9089a2e...58d` unchanged; controller `readiness_lab.py` `345cbd75...474`.
Local credential-free preflight on dc69ff0 passed first (22 gates, controller,
HTTPX transport, startup observers; resources removed). Runner offline gates:
971 default + 289 focused passed.

- Key-free gates on the runner passed on the exact built image
  `sha256:898e8ee5359c6bc4502bbeeac38a7ae487d82c47e57e2578fd868d991ccf02c6`,
  identical across preflight, config-preflight and live topologies. Config-only
  summary: exit 0, 0 provider requests.
- Live bootstrap passed: one migration (pid 10), startup observer streams for
  A14/B23 then restarted A34/B51 with linked previous incarnations and zero
  completions, loaded toolkit source/valves verified through both workers,
  `daytona-small` snapshot (1 CPU/1 GiB/3 GiB), installed valves read back.
- One sandbox provisioned at 44.80 s: `359902ac-14af-45df-94af-8ce78efaf1df`.
- **First failure at 45.47 s:** `RuntimeError: Native lab requires the single owned
  sandbox` (`readiness_lab.py:603`). `native_scenarios` calls `owned_sandboxes`,
  which lists `GET /api/sandbox` UNFILTERED and matches label keys locally,
  0.67 s after provisioning. The error does not record the count (0 vs >=2).
  Most likely cause (hypothesis, unconfirmed): the listing is an eventually
  consistent discovery index (documented as such at `lathe.py:119`) and had not
  yet surfaced the new sandbox; an alternative is unfiltered pagination missing
  it. The same code passed this point in run 40624797681 at dad4390. This is a
  harness discovery/timing defect, not model, tool-iteration or upstream native
  protocol behavior.
- Native idle: **NOT REACHED**. Native busy: **NOT REACHED**. No delegate_task
  calls, parent/child/continuation IDs, native worker handling or canary receipts
  exist for this run.
- Model GLM-5.3-Flash requested, none served: **0 provider requests, $0 inference
  cost, $0 reserved** of the $2 ceiling. Daytona compute: one small sandbox for
  about 2.5 s (unmeasured charge).
- Cleanup: in-container cleanup deleted `359902ac...` at 47.27 s. Host cleanup and
  the workflow's independent repeat both report OWUI stopped before cloud cleanup,
  cloud cleanup attempted, no remaining labelled sandboxes, success with no errors.
  All three local topologies verified absent. Caveat: the host absence check uses
  the same unfiltered `owned_sandboxes` listing, so it shares the weakness above;
  the deletion itself was performed by ID.
- Suggested fix (not applied, needs authorization): use the server-side labels
  filter with exact label/value match, poll it with a bound until exactly one
  candidate, confirm by `GET /api/sandbox/{id}`, record the observed count in the
  failure, and use the same filtered query for the absence check.

Retained artifact: `monitor-artifacts/readiness-results/run-40628835065/`.

## Offline native-scope handoff: 890597d (no new live run)

User steering halted new paid/cloud dispatch until deterministic local preflight
is integrated. No run was dispatched in this continuation. Actions listing at
handoff showed the latest readiness run, 40614126169, completed/failure, not in
flight. No new sandbox/container/network was created; no new cleanup is needed.
The successful cleanup evidence for earlier runs below remains unchanged.

Pushed `890597db36c96ef675c6a3d2bf2791953c24df38` to `readiness-labs`.
Only `readiness_lab.py`, `test_readiness.py` and the manual readiness workflow
extension were committed. `.openchamber/`, ignored historical evidence and the
other agent's untracked `readiness-infra/` were preserved, not committed.

- Relay middleware records admission BEFORE body reads, then status/error class,
  declared length and completed-read byte count. Correlation IDs connect admission,
  reservation and status. No body/header/exception text is retained by these new
  diagnostics. Chunked oversized requests have unknown declared/read lengths,
  explicitly null, but retain admission, byte ceiling and status 413.
- Real aiohttp offline HTTP regressions cover fixed/chunked oversize, malformed
  JSON, budget rejection, accepted forwarding and upstream 503. Rejected requests
  make no reservation and never reach the forwarder. Focused readiness/monitor:
  **169 passed** (two aiohttp request-key deprecation warnings). Default repository
  tests: **821 passed**. `git diff --check` passed.
- Text ceiling raised from 64,000 to **128,000 bytes**. Conservative reservation
  remains **4 x wire bytes + 16,384 prompt tokens**, maximum **528,384** prompt
  tokens plus at most 4,096 completion tokens, **$0.0813056** at the maximum size.
  The real **$2** cumulative nonrefundable ceiling is unchanged, as are ZDR,
  data_collection=deny, max_price, request-count and TTL gates. A regression proves
  the larger per-request ceiling exhausts money before the request-count cap.
- `--scope native` runs only native idle/busy with separate fresh saved parent
  chats. Workflow manual input `readiness_scope` defaults to `native`; CLI default
  stays `full`. Full scope no longer repeats the redundant extra final A turn.
  Native assertions retain actual delegate arguments/results/associations,
  child outputs, independent downloads, worker PID/incarnation and original
  model/tool/system context checks. These assertions have NOT been live qualified.
- Source SHA-256 `d11a014c028e4b679b1697792d5576d9578e964986dd56757e8652a9fb69e740`.
  Runtime remains `a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`.
  No new image qualification: previous OWUI 0.12.4 digest remains
  `sha256:41812075aede8ada8497cb2b71c76412978633eee72775bb77796f593d03b677`.

New provider requests/reservations/spend: **0 / $0 / $0**. Serving model, native
chat/delegation IDs, idle automatic resumption, busy pending-then-resumption and
native file oracles: **NOT REACHED**. Prior continuity evidence remains as below;
the historical 413 explanation is still a hypothesis, not a live diagnosis.
Next blocker: integrate and pass the separate credential-free deterministic
Docker preflight before any newly authorized bounded native-only live run.
Overlapping first use remains unimplemented/inconclusive; direct bash wake-up
remains unsupported. No runtime/production change, merge or issue closure.

## Latest approved targeted retry: 40614126169

https://github.com/rndmcnlly/lathe/actions/runs/40614126169

Commit `773bdb4801f681b5453da7e5b01a383f51d6a9ea`, pushed to `readiness-labs`.
Only `readiness_lab.py` and `test_readiness.py` changed in this repair. Named
`daytona-small` preflight succeeded (1 vCPU, 1 GiB, 3 GiB disk, GPU=0). Full
staged valves were read, updated and independently read back before completions:
exact snapshot+ttlMinutes=30 only, credentials retained, deployment label exact,
auto-create disabled, stop/archive=5 minutes, delete-on-stop=0. Explicit manual
provisioning used no CPU/memory/disk override. This fixed the prior setup error.
Offline regression executes the production provisioning function with MockTransport
and rejects oversized snapshot, resource-override/read-back drift, credential loss
and lifecycle-policy drift before provisioning. Focused readiness/monitor: 161 passed.

### Verified live scope

One saved ordinary-user chat `f01dde38-7cee-4e34-bc50-b1aab8aa6314`:

| Step | Actual OWUI handler | Result |
| --- | --- | --- |
| A first use | PID 7, incarnation afa43c36d5db4b10a83531df339ed813 | interpret called; independent file count=1 |
| B | PID 23, incarnation 4ffc6491683f4e3cbd1ecb5897907e35 | interpret called; independent file count=2 |
| A again | PID 7, same incarnation | interpret called; independent file count=3 |
| B replacement | PID 64, incarnation dd1bc68026d041d49f0e9453c96fcb6d | interpret called; independent file count=4 |
| Extra final A | PID 7, accepted HTTP 200 | 240-second timeout, no further provider reservation/request recorded; inconclusive |

All FOUR independent downloads and separate interpreter state.json reads matched:
nonce `fe3ac081d7034a1ac786f19021566ce7`, Python PID `153`, generation
`2e3f8b7c88e944a1add626b0c77f57a6`, active=None. The nonce was generated inside
the sandbox, never supplied as expected model output. Request IDs/worker assignment,
actual completed interpret calls and file values are retained in trace.jsonl.
Thus A→B→A and continuity onto a replaced B are observed, but the full lab did NOT
pass. Native idle/busy were NOT REACHED, not failed. No delegate_task evidence.
Overlapping first use remains inconclusive/unimplemented; direct-bash wake-up unsupported.

### Bounded timeout diagnosis, no further live retry

Final HTTP request was accepted at 128.454 seconds; fatal TimeoutError recorded at
368.395 seconds. No request eight, tool result or independent read was recorded.
The seven forwarded body sizes, recovered exactly from reservations, were:
32316, 38238, 41142, 44891, 52012, 56876, 63427 bytes. The last is within 573 bytes
of aiohttp's 64000-byte request cap. Offline reproduction using aiohttp's real
Request.json/read path accepts a 63427-byte JSON body and raises HTTP 413 for
64427 bytes, before reservation/forwarding. This makes uninstrumented byte-cap
rejection a strong harness hypothesis, NOT proof of the live timeout cause.
Do not attribute this timeout to a Lathe interpreter-state reset or provisioning.
Next bounded diagnostic: record rejected relay status/body size and serialize the
actual next request; preserve the $2 cumulative reservation guard if increasing
the byte cap, or run native scenarios in their already-fresh saved chats. No
additional live run has been dispatched after this approved retry.

### Source, cost and cleanup

- Lab SHA-256: `594d98dc9cc2da77bc7071280c0e0f51bf27c9383e56d839fe6edd2c1332f420`
- Lathe remains `a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`.
- OWUI image digest remains `sha256:41812075aede8ada8497cb2b71c76412978633eee72775bb77796f593d03b677`, verified version 0.12.4.
- Actual serving model `z-ai/glm-5.3-flash`; 7 provider requests with enforced
  ZDR/data_collection=deny/price ceilings. Usage cost sum `$0.0040860842`;
  nonrefundable reserved cost `$0.22888040` of `$2.00`.
- One `daytona-small`; 30-minute compute bound <=$0.034, not an actual billing claim.
- Exact label `lathe-ci-next-2927eb67fa40d294`; sandbox
  `b1fd7247-400b-49ce-9af5-c21b723a126b` was deleted and per-ID deletion verified.
  OWUI stopped before deletion; all three owned containers and the network were
  removed and verified absent. Host cleanup AND workflow always-run verification
  both succeeded. No raw logs exported, no persistent volumes, no production change.

Evidence:
`monitor-artifacts/readiness-run-40614126169/readiness-40614126169-1/lathe-ci-next-2927eb67fa40d294/`
contains trace, summary, worker-A/B routing observations, pricing/environment and
both successful cleanup records.

## Earlier setup attempts (historical; superseded by the latest run)

Branch `readiness-labs`, pushed, not merged. Commits: `a2986a9` (lab),
`1a1936c` (image import path and key redaction), `eab4d01` (identity before
SPA catch-all plus regression test), `12bf439` (sanitized HTTP diagnosis).
Only tracked changes: `readiness_lab.py`, `test_readiness.py`,
`.github/workflows/monitor.yml` (manual-only readiness job/input).
No Lathe runtime, production deployment, issue or lifecycle qualification changes.

## Completed attempts

| Actions run | Exact label suffix after `lathe-ci-next-` | Result |
| --- | --- | --- |
| https://github.com/rndmcnlly/lathe/actions/runs/40612973368 | cce41f0121c3e672 | Worker could not import `open_webui`: /tmp script did not inherit /app/backend module path. Targeted import-path repair. |
| https://github.com/rndmcnlly/lathe/actions/runs/40613135529 | 98e3c6e09f32b135 | Identity route returned SPA HTML, causing JSONDecodeError. Targeted middleware repair and regression test. |
| https://github.com/rndmcnlly/lathe/actions/runs/40613351959 | a6a101354139f3aa | A started, verified 0.12.4, bootstrap HTTP 400. A PID 7, incarnation bf351f4333c7443b9e34236c0ff870df. One final diagnostic retry retained response path/body. |
| https://github.com/rndmcnlly/lathe/actions/runs/40613572935 | f8c9a81dec804059 | Definitive blocker: POST /api/sandbox returned 400, `Cannot specify Sandbox resources when using a snapshot`. Stopped, no further retry. |

Latest run verified A PID `7`, incarnation `aee84ca352f94c5bb3b182a197372945`,
OWUI `0.12.4`. Synthetic admin/user setup, configured GLM policy and toolkit
deployment completed. B is started only after sandbox provisioning, so B and
replacement were NOT reached. No A→B→A routing claim.

Saved-chat continuity, replacement, native idle and native busy are implemented
but NOT exercised. No actual delegate_task, independent sandbox file reads or
generation proof was obtained. Overlapping first use remains unimplemented and
inconclusive. Direct-bash native wake-up remains an unsupported upstream gap.
Parent's separate lifecycle results were not duplicated or claimed here.

## Exact latest source/environment

- Checkout: `12bf43998f3abe08732578506f9548c8a0e93faa`
- Lab SHA-256: `2fae456d9377792a5a1af29a7304024d61261495bee6006f7b94357fda7112ab`
- Lathe 1.3.1 SHA-256: `a9089a2e4b93621a450ee5c6f889c73191782adb06ad6d26ce2fe1bf8216a58d`
- OWUI image: `ghcr.io/open-webui/open-webui:v0.12.4-slim`
- Image digest: `sha256:41812075aede8ada8497cb2b71c76412978633eee72775bb77796f593d03b677`
- Disposable PostgreSQL/pgvector and Redis readiness succeeded; shared keys and
  filesystem, no host bind mounts or reusable volumes. Two-worker qualification
  has NOT been established merely by that configuration.

## Cost/safety and verification

All four runs: zero provider requests, zero reservations, no serving model
observed, no Daytona sandbox created. Configured model is
`z-ai/glm-5.3-flash`, not evidence of actual model serving.

Configured inference limits: $2 cumulative nonrefundable reservations, 40
requests, 64,000-byte text request cap, 4,096 output tokens; upstream ZDR,
data_collection=deny and max_price prompt=$0.15/M, completion=$0.50/M,
request/image=0 enforced before forwarding. Published model pricing was checked
per run. Token reservation is 4×wire bytes + 16,384 framing tokens. These gates
passed offline tests but have not yet been exercised with live inference.

Intended compute: one sandbox, 30-minute TTL, 25-minute lab; actual resources
must be <=2 vCPU/4GiB/10GiB, estimated <=$0.084 for 30 minutes at supplied prices.
Current creation incorrectly mixes snapshot defaults and resource overrides.
Next repair must preflight a named small snapshot's resources, create by snapshot
without cpu/memory/disk overrides, and retain resource checks, TTL, exact label,
no volumes, and disabled Lathe auto-provisioning. Then run the scenarios once.

821 default repository tests passed before first dispatch. Latest focused run:
156 readiness/monitor tests passed, including identity catch-all regression.

All four runs have TWO successful cleanup records: host teardown and the
workflow's always-run independent repeat. OWUI stopped before cloud cleanup;
exact-label sandbox listing was empty; owned containers and network were removed
and independently checked absent. No raw logs exported. Latest teardown Actions
step succeeded. There are no remaining owned resources reported by these runs.

## Evidence paths

Each run is retained at `monitor-artifacts/readiness-run-<run-id>/`.
Latest detailed directory:
`monitor-artifacts/readiness-run-40613572935/readiness-40613572935-1/lathe-ci-next-f8c9a81dec804059/`
contains environment/pricing/summary JSON, trace.jsonl, and both cleanup records.
Bounded gh watch output is retained in `monitor-artifacts/readiness-watch-<run-id>.log`.

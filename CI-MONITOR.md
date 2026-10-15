# Isolated live monitor

The **Isolated live monitor** Actions workflow is a synthetic monitor for Lathe's
agent experience and hosted dependencies. It runs the authentic path:

```text
real OpenRouter model → disposable Open WebUI → checked-out lathe.py → real Daytona
```

It runs weekly, on relevant pushes to `main`, and through **Run workflow** on a
selected trusted ref. It never connects to the personal Open WebUI deployment.
No pull-request trigger exists. Manual dispatch runs code from the selected ref:
only select code you trust with the repository's provider secrets.

## Setup and execution

Repository Actions secrets:

- `DAYTONA_API_KEY`
- `OPENROUTER_API_KEY`

These are intentionally repository-level, so the disposable demo-video
workflow can use the same credentials. The isolated demo uses these existing
keys under the `lathe-demo` GitHub environment (same-name environment overrides
are optional). Neither key belongs in workflow source,
artifacts, or a branch-specific configuration file.

For a local run, export those two variables and start Docker, then run:

```sh
uv run --locked python monitor_e2e.py
```

The monitor ignores `.env` and existing `OWUI_*` configuration. It pulls and
launches the moving `ghcr.io/open-webui/open-webui:slim` tag so upstream drift is
part of the contract under test. After pulling, it records the exact resolved
digest, image ID, and platform as forensic evidence; it does not use those values
to pin a version. Docker publishes port 8080 only on a randomly assigned loopback
port. SQLite and other OWUI state live in the disposable container's storage.
There is no reusable data volume.

`bootstrap()` creates the first admin headlessly and configures a native-tool
workspace model. Its base model is `openrouter/pareto-code`. OWUI's workspace
model `custom_params` inject this request-level configuration:

```json
{
  "plugins": [{"id": "pareto-router", "min_coding_score": 0.5}],
  "provider": {"data_collection": "deny"}
}
```

No account-level Pareto setting is required. A loopback-only recording relay
inside the container forwards request bodies unchanged to OpenRouter. It
rejects a missing or changed routing policy, and records the concrete serving
model from actual streaming and non-streaming responses. It does not emulate
inference or tool execution. If a future OWUI release drops custom parameters,
the monitor fails visibly rather than silently testing another tier.

### Explicit model selection

The regular monitor still defaults to Pareto Medium. To qualify a checkout with
the demo's direct model, export `LATHE_MONITOR_MODEL=z-ai/glm-5.3-flash` before
running `monitor_e2e.py`. Both bootstrap and the real-provider relay use that
selection. Direct models get **no Pareto plugin**.
The relay rejects mismatched model/plugin requests instead of rewriting them.
Data collection remains denied. `environment.json`, per-request trace entries,
and summaries distinguish requested and actually served models; a direct model
may legitimately serve under its own requested ID, unlike the Pareto alias.

The monitor workflow also accepts a `model` manual-dispatch input. With this
workflow and helper revision present on a trusted target branch:

```sh
gh workflow run monitor.yml --repo rndmcnlly/lathe --ref TRUSTED_BRANCH \
  -f model=z-ai/glm-5.3-flash
```

The selected ref is checked out and its exact Git SHA and Lathe source hash are
recorded. For another PR checkout, apply this helper/workflow revision there
first if it predates model selection. Never run untrusted PR code with secrets.
Existing repository secrets permit GitHub runs, not local credential recovery.

## Checks and budgets

The monitor reuses `test_deployment.py`: byte-identical source, the complete
independent schema contract, bash, write/read, interpreter, and image middleware.
It then runs a first-time journey with a separate read-back check:

1. Consult the built-in overview.
2. Create a uniquely named file containing a fresh canary.
3. Make an exact copy.
4. Inspect the copied contents.

The oracle checks actual tool calls and returned outputs, then independently
downloads both files through Daytona and compares their bytes with the canary.
The journey permits 12 tool calls and four minutes of wall time for the chat.
OWUI also caps tool-call iterations at 12. The container execution has a
25-minute limit, the live Actions step 35
minutes (including image pull), and the job 45 minutes to leave cleanup time.

This is one nondeterministic smoke test, not a model benchmark. A red run can
mean an AX regression, an OWUI/Daytona API change, a routing change, or a transient
provider failure. Inspect the first failing scenario and its evidence before
assigning a cause. Rerunning is an explicit diagnostic decision, not an automatic
retry that hides flapping.

## Shared Daytona account: ownership and expiry

Each run gets a unique deployment-label key:

- Actions: `lathe-ci-<run-id>-<attempt>`
- Local: `lathe-ci-<random-96-bit-id>`

The label is recorded in `environment.json`. Cleanup follows pagination and
deletes only sandboxes with that exact key, then polls their per-ID endpoints
until deletion is confirmed. The synthetic user's email is stable, but different
label keys keep simultaneous test instances from discovering each other's VMs.
Lathe provisions no extra volumes; monitor qualification checks the actual sandbox.

Three teardown layers serve different failure modes:

1. The deployment suite deletes its staging toolkit and sandboxes in `finally`.
2. The host stops OWUI before a second exact-label cleanup, so an interrupted
   tool loop cannot create another VM after cleanup. Docker removal includes
   anonymous volumes. An `always()` Actions step repeats this after interruption.
3. Daytona gets `autoStopInterval=5`, `autoDeleteInterval=0` (delete on stop),
   and `ttlMinutes=30`. The hard wall-clock deadline applies even to an active
   sandbox if the runner disappears. Provider cleanup scheduling is not an
   exact-second guarantee.

For an interrupted local run:

```sh
uv run --locked python monitor_e2e.py --cleanup
```

Keep its artifact directory: that manifest identifies the owned label. A cleanup
error fails the monitor. The live workflow is serialized without cancellation,
and each runner uses a fixed local container name to reject overlapping local
invocations. Labels still differ across runs and future workflows.

## Evidence

Actions uploads `monitor-artifacts/` on success or failure for 14 days:

| File | Evidence |
|---|---|
| `environment.json` | Moving OWUI image tag plus resolved digest/ID/platform, Lathe SHA-256, checkout SHA, router policy, owned label |
| `trace.jsonl` | Scenarios, prompts, tool calls/outputs, statuses, provider errors, serving models and usage, independent reads |
| `summary.json` | Result, first failing scenario, concrete models, inference count |
| `suite.log`, `owui.log` | Deployment-suite output and container logs |
| `cleanup.json` | Last-resort cleanup result |
| `host-error.log`, `cleanup-error.log` | Present when those phases fail |

Secrets are redacted before persistence. All HTTP URLs in logs and trace are
redacted, including unfamiliar signed-preview URL formats; JWTs and provider
key patterns are also removed. No database, container filesystem, raw provider
headers, or raw logs are uploaded. `environment.json` intentionally preserves
non-secret image identifiers. Startup failures may occur before a suite summary
exists; use the host error and OWUI log in that case.

## Local private-editor demo video

With the same two provider keys exported and Docker running:

```sh
npm ci --prefix demo-video
npm exec --prefix demo-video -- playwright install chromium
uv run python demo_local.py
```

This records the shared-editor scenario against a fresh local deployment. It
starts three containers: Open WebUI, the actual preview Worker under local
Wrangler, and an Nginx HTTPS entry point. The entry point binds only
`127.0.0.1:443`, so that port must be available. Chromium resolves the generated
`*.lathe-preview.test` names to loopback. OWUI reaches the registration endpoint
over the private Docker network and trusts a temporary certificate bundle.
The capture browser accepts that temporary certificate only in its disposable
browser contexts; nothing is installed in the laptop's trust store.

The demo explicitly defaults to `z-ai/glm-5.3-flash`, overridable with
`LATHE_MONITOR_MODEL`. It uses the same no-plugin direct-model relay policy as
isolated deployment qualification. The Docker-exec process runs with host UID/GID
so routing and teardown evidence remain host-writable on Linux runners.
The Worker also drops to host UID/GID after its root-only CA installation,
before npm/Wrangler creates files beside the bind-mounted configuration. This
keeps temporary-directory removal host-owned, including `.wrangler/tmp`.

The same scenario always asks for a **private code-server** editor, launched
through the pinned optional recipe described by `lathe(manpage="services")`.
The oracle binds the checkout to the launcher's actual ready result, then
requires `expose(target="http:8080", access="private")` plus the owner-only
access result before opening it. No public workaround is permitted. A disposable identity
fixture runs inside OWUI on port 9100, reachable only over the Docker network
and the loopback TLS listener's `/demo-idp/` route. Its sign-in button explicitly
identifies the fixed synthetic user `monitor@lathe.invalid`. This is not Pocket
ID, real user authentication, or a full OIDC implementation. The actual Worker
uses its unchanged discovery, authorization-code + S256 PKCE, userinfo,
verified-email matching, and host-scoped session path. Single-use codes expire
after 60 seconds; the fixture checks the client secret and redirect binding.
Backchannel discovery/token/userinfo use HTTP inside the disposable Docker
network; browser authorization and callbacks use the temporary HTTPS listener.
Provider inference and Daytona remain hosted. The browser-facing wrapper is
local; the signed upstream Daytona URL remains a remote bearer capability held
by the wrapper.

`DEMO_LOCAL_TOKEN` is handed directly from bootstrap to the browser process;
capture accepts this mode only with a loopback OWUI URL and skips `.env`.
The token changes OWUI login only, never the narrative or preview policy.
Output is `demo-video/out/demo.webm`, with screenshots and a qualification report.
Infrastructure evidence goes into a unique `monitor-artifacts/demo-*` directory.
Normal completion and exceptions clean up containers, local KV state, temporary
TLS material, the Docker network, and the uniquely labelled Daytona sandbox.
Use `--artifacts PATH` for a known fresh evidence directory, and
`demo_local.py --cleanup --artifacts PATH` for repeated last-resort teardown.
Cleanup attempts removal directly and ignores only explicit missing-resource
responses, not Docker daemon or inspection failures. `qualification.json` is
written only after the browser take, model-serving evidence, inner staging
teardown, and outer cleanup all succeed. It identifies the tested Git SHA.
The small infrastructure artifact also contains that qualification manifest,
so requested/served-model and checkout evidence do not require downloading the
large video/Playwright archive. Repeated cleanup preserves original startup logs.

Local live runs shared with other review sessions must hold the review lock for
the full subprocess lifetime. After explicitly supplying both provider keys,
run this from the checkout being qualified:

```sh
LATHE_MONITOR_MODEL=z-ai/glm-5.3-flash uv run --locked python - <<'PY'
import fcntl, subprocess
with open('/private/var/folders/qj/gyqbn5y57956rrqd_g6yfqwr0000gn/T/opencode/lathe-review-live.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    subprocess.run(['uv', 'run', '--locked', 'python', 'monitor_e2e.py'], check=True)
PY
```

Use `demo_local.py` instead of `monitor_e2e.py` for a video take. Do not load
production OWUI settings or obtain a general OpenRouter key as a shortcut.

## Reusing the host for branch demo videos

`launch()`, `bootstrap()`, and `cleanup()` in `monitor_e2e.py` are the lifecycle
seam for a future video driver. A video workflow should create its own run
identity (include a job suffix if several jobs share one Actions run ID), use
the same two repository secrets, capture the loopback-published OWUI instance,
and upload video artifacts labeled with the tested ref and commit. Keep its
unique Daytona label, no-extra-volume contract, and expiry policy. Its browser and
capture code must never fall back to a configured daily-use OWUI URL.

`render-demo.yml` now runs this same disposable lifecycle every Wednesday at
14:17 UTC and on manual dispatch, with the chosen GLM model. No hosted OWUI or
cloud Worker credentials are used. Jobs run under `lathe-demo`: configure any
desired trusted-ref restrictions or approvals without blocking scheduled runs.
Provider secrets remain `OPENROUTER_API_KEY` and `DAYTONA_API_KEY`, already
present at repository level. No `DEMO_OWUI_URL`, `DEMO_PASSKEY`, or cloud token
is required. Runs serialize without cancellation and always repeat cleanup and
upload sanitized infrastructure/capture evidence.

Manual dispatch defaults to capture/check only. `publish=true` enables
publication **only on the default branch**, after accepted capture, routing,
cleanup, and exact-SHA gates. It retains the existing `demo-video-latest`
release, `demo.webm`, and screenshot filenames. Scheduled or branch runs never
refresh public assets. To qualify this branch without publication:

```sh
gh workflow run render-demo.yml --repo rndmcnlly/lathe \
  --ref review/demo-alignment -f publish=false
```

Linux live qualification is still required; offline tests do not qualify
Docker/Wrangler/TLS/editor behavior. Pocket ID/passkeys remain outside the
synthetic identity fixture's qualification boundary.

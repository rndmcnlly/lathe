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

These are intentionally repository-level, so a future disposable demo-video
workflow can use the same credentials. Neither key belongs in workflow source,
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
model `custom_params` inject this request-level configuration, including on
the delegate's in-process calls back into OWUI:

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

## Checks and budgets

The monitor reuses `test_deployment.py`: byte-identical source, the complete
independent schema contract, bash, write/read, interpreter, image middleware,
and delegation with a separately verified file effect. It then runs a first-time
journey whose prompt describes the goal without naming tool functions:

1. Consult the built-in overview.
2. Create a uniquely named file containing a fresh canary.
3. Ask a subagent to make an exact copy.
4. Inspect the copied contents.

The oracle checks actual tool calls and returned outputs, then independently
downloads both files through Daytona and compares their bytes with the canary.
The journey permits 12 outer tool calls and four minutes of wall time for the chat.
Delegates retain Lathe's own step limits. OWUI also caps tool-call iterations at
12. The container execution has a 25-minute limit, the live Actions step 35
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
Persistent volumes are always disabled.

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

## Local public-preview demo video

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

The local variant explicitly asks for **public** previews and requires the
tool's `Public wrapped preview` result before opening the editor. It demonstrates
the real registration, proxy, and WebSocket paths without an OIDC provider.
Provider inference and Daytona remain hosted. The browser-facing wrapper is
local; the signed upstream Daytona URL remains a remote bearer capability held
by the wrapper.

`DEMO_LOCAL_TOKEN` is handed directly from bootstrap to the browser process;
capture accepts this mode only with a loopback OWUI URL and skips `.env`.
The default release scenario and its passkey authentication remain separate.
Output is `demo-video/out/demo.webm`, with screenshots and a qualification report.
Infrastructure evidence goes into a unique `monitor-artifacts/demo-*` directory.
Normal completion and exceptions clean up containers, local KV state, temporary
TLS material, the Docker network, and the uniquely labelled Daytona sandbox.

## Reusing the host for branch demo videos

`launch()`, `bootstrap()`, and `cleanup()` in `monitor_e2e.py` are the lifecycle
seam for a future video driver. A video workflow should create its own run
identity (include a job suffix if several jobs share one Actions run ID), use
the same two repository secrets, capture the loopback-published OWUI instance,
and upload video artifacts labeled with the tested ref and commit. Keep its
unique Daytona label, disabled volumes, and expiry policy. Its browser and
capture code must never fall back to a configured daily-use OWUI URL.

`demo_local.py` now exercises this lifecycle with the existing browser capture.
The GitHub demo-video workflow has not yet been migrated to launch this stack.

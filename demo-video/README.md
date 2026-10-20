# Everyday visualization demo

A real GLM 5.3 Flash conversation in Open WebUI: raw repair-café data and three
short requests, not a scripted coding-agent capabilities showcase.

1. Enable Lathe visibly through **Integrations → Tools → Lathe**.
2. Ask for a quick analysis: repairs, volunteer time, and average item time
   by session, plus what blocked unsuccessful repairs. Include `repair-log.csv` as pasted individual records, keeping advice
   separate from repairs. The take must show a real `interpret` result without
   naming that tool in the user's prompt. Hold the short answer for reading.
3. Ask for a phone-readable HTML infographic based on those findings, with
   evidence-grounded claims and suggestions labeled as suggestions.
4. “Serve it up, private.” Open the resulting private preview and scroll through
   the infographic.

The model chooses its calculations, conclusions, implementation, file paths and
service port. There are no subagents, version question, exact-answer phrases,
prescribed tool sequence, output schema, installer recipes or editor choreography.
The demo system context states that sandbox Internet access is blocked and
instructs the model to prefer `interpret` for Python data analysis, keeping state
for follow-up turns, while using `bash` for shell work and service processes.
This is demo-only tool-selection guidance, not a change to Lathe's shipped tools
or a prescribed analysis implementation.

## Run

With Docker, OpenSSL, Node/npm, and exported `DAYTONA_API_KEY` and
`OPENROUTER_API_KEY`:

```sh
npm ci --prefix demo-video
npm exec --prefix demo-video -- playwright install chromium
uv run python demo_local.py
```

If another trial owns loopback port 443, use `DEMO_TLS_PORT=18443`. Chromium maps
only demo hostnames to that separate loopback port; HTTPS/OIDC URLs and
Docker-side connections retain port 443. No existing identity fixture, server or
sandbox is reused. Temporary TLS trust is confined to disposable browser contexts
and OWUI's temporary CA bundle, never the OS or keychain.

The runner creates disposable OWUI, local Wrangler running the actual preview
Worker, and loopback HTTPS infrastructure. A fixed synthetic identity exercises
the Worker's PKCE/session flow, not Pocket ID or real passkeys. OWUI still reaches
the model provider and Daytona control plane; the sandbox has actual
`networkBlockAll=true` and no extra volumes. No production OWUI configuration,
hosted login, or `.env` is imported by the runner. The upstream relay enforces ZDR,
data-collection denial, price ceilings, a conservative $4.50 reservation cap, and
request/output-token limits. See [CI-MONITOR.md](../CI-MONITOR.md).
The demo permits 24 tool iterations per user turn, independently of the shorter
monitor's 12. The existing whole-run inference, time, and cost budgets still apply.

## Evidence and checks

`scenario.json` is a linear take. The adapter preserves real multiline editor
input and waits for visible chat readiness instead of `networkidle`. Failures
reject the take, without narrative retries or alternate routes.
If the private service opens a directory listing, the browser follows only its
exact same-origin HTML link matching the artifact identified in tool records.
It never guesses a URL or substitutes a different artifact.
Each completed turn is checked for OWUI's persisted errors. Tool-call exhaustion
rejects the take even if it produced an otherwise usable artifact and preview.

Persisted tool records identify the model's HTML path and prove an actual private
`expose` result, not an assistant promise. The runner independently downloads that
HTML through Daytona, checks basic computed counts against the raw CSV, rejects
external assets, and verifies the actual sandbox egress policy and absence of
extra volumes. This is not an automatic judgment of every visual or editorial
claim: inspect the screenshots and infographic as part of reviewing a take.

`out/` contains the video, screenshots, full-page final image, redacted chat,
sanitized trace, artifact path and capture/qualification reports. Infrastructure
evidence includes exact deployed-source hashes, downloaded HTML with independently
computed reference totals, model usage and cleanup records. A later take replaces
`out/`; copy accepted captures into their run's artifact directory before rerunning.

```sh
npm test --prefix demo-video
uv run pytest test_demo_local.py
```

Offline checks do not qualify real inference, private previews or Docker/Daytona
boundaries. `render-demo.yml` uses GLM 5.3 Flash on Wednesdays at 14:17 UTC and
manual dispatch. Publication defaults off; only explicit default-branch publication
after capture, model/source and cleanup gates may update `demo-video-latest`.
Local captures and source edits never replace the existing public video.

## Current qualification status

On 2026-10-20, [scenario 14 passed live qualification](https://github.com/rndmcnlly/lathe/actions/runs/39460102515)
at checkout `c5577a420874f400e018ee51264c9b6990f64eb0`. The reviewed analytics-first
take is published at `demo-video-latest` and embedded at lathe.tools, with its
qualification and capture reports attached to the release.

The first answer arrives about 40 seconds into the take, with an actual `interpret`
result and a reading pause before the infographic request. All three turns
completed without persisted errors; private preview, independent artifact/source
checks, serving-model evidence, and cleanup passed. The exact downloaded artifact
was promoted after default-branch SHA and qualification checks, not regenerated
as an unreviewed take.

The previously published scenario 11 had exhausted the monitor's inherited
12-iteration limit despite producing a usable artifact. The demo now uses 24
iterations per turn and rejects persisted OWUI errors. Natural analytics and
Python-specific requests subsequently chose no tools and shell Python respectively;
demo-only system guidance now prefers `interpret` for analysis. These earlier
takes remain in their Actions runs.

Generated conclusions still need human review. In particular, the analytics
answer's projected 90%+ repair rate is speculation, not a measured finding from
the supplied log. The infographic labels its suggestions and small-sample caveat.

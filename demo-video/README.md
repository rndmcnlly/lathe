# Everyday visualization demo

A real GLM 5.3 Flash conversation in Open WebUI: raw repair-café data and two
short requests, not a scripted coding-agent capabilities showcase.

1. Enable Lathe visibly through **Integrations → Tools → Lathe**.
2. “Here's our repair-café log. Make an HTML infographic showing the volunteers
   what we accomplished and where we could improve.” The message includes
   `repair-log.csv` as a pasted CSV block of individual repair records.
3. “Serve it up, private.” Open the resulting private preview and scroll through
   the infographic.

The model chooses its calculations, conclusions, implementation, file paths and
service port. There are no subagents, version question, exact-answer phrases,
prescribed tool sequence, output schema, installer recipes or editor choreography.
The model gets only an ordinary deployment fact as additional system context:
sandbox Internet access is blocked, so use available tools and self-contained assets.

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

## Evidence and checks

`scenario.json` is a linear take. The adapter preserves real multiline editor
input and waits for visible chat readiness instead of `networkidle`. Failures
reject the take, without narrative retries or alternate routes.

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

As of 2026-10-15, the everyday-use storyboard has no accepted live capture.
Iteration was paused after capture/relay fixes and provider rate limiting;
trial cleanup succeeded. Offline checks pass, but a future accepted end-to-end
take is still required before publishing a replacement video.

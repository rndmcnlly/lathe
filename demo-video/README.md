# Demo capture

The demo is a live synthetic monitor of Open WebUI, Lathe, Daytona, the preview wrapper, and the configured model. `scenario.json` owns the narrative, prompts, required outcomes, and bounded presentation fallbacks. `interpreter.mjs` validates and executes that data through the finite operation set implemented by `capture.mjs`.

Run the offline tests (including a local browser UI fixture) with:

```sh
npx playwright install --only-shell chromium
npm test
```

The supported capture entry point uses disposable local infrastructure and only
exported `DAYTONA_API_KEY` and `OPENROUTER_API_KEY`. It never imports production
OWUI configuration. See [CI-MONITOR.md](../CI-MONITOR.md) for prerequisites:

```sh
uv run python demo_local.py
```

For local iteration after the sandbox is already awake, skip the throwaway
prewarm model turn with `DEMO_SKIP_PREWARM=1 uv run python demo_local.py`.
The browser adapter still supports explicit hosted passkey credentials for
maintainer diagnostics, but no workflow runs that mode or publishes it.

Every run starts with a clean `out/` directory and writes available evidence:
`capture-report.json`, `demo.webm`, `last-page.png`, `playwright-trace.zip`, and
a redacted `sanitized-chat.json`. Scenario-marked main-sequence frames go in
`out/screenshots/` with stable narrative names; failed fallback attempts go in
`out/diagnostics/`. Login and prewarm occur before the recorded context exists
and are never captured as release screenshots. A failed or rejected take remains
diagnostic evidence but cannot replace the `demo-video-latest` video or screenshots.

Chat navigation waits for the visible composer, not `networkidle`: OWUI's
realtime traffic can keep the network busy indefinitely. Release notes are
acknowledged before recording, with browser storage carried into the take.
The disposable demo disables prompt suggestions, follow-up generation, and the
built-in code interpreter. Model and toolkit display names omit staging labels;
hidden staging IDs still protect cleanup isolation.

The recorded activation beat deliberately animates a tutorial cursor through
`Integrations → Tools → Lathe`, with recognition and confirmation pauses. Keep
that choreography when changing selectors: activation is instructional content,
not incidental browser mechanics.

The first recorded question asks Lathe for its installed version. The capture
requires a matching `lathe(manpage="version")` result and saves a version frame,
so the video identifies the deployed toolkit rather than the checkout version.
Conversational prompts keep test instructions out of the visible dialogue.
The overview precedes the relay-writing turn, and the editor's rooted expose
call supplies the actual checkout path rather than a hardcoded clone destination.
Cold IDE setup gets a six-minute turn budget, covering Lathe's four-minute
installation window and the model's reply.

The relay edit targets the file editor by URI, selects text with cursor movement,
and verifies the rendered document before saving with the platform's shortcut.
It replaces the entire second logical line, including any model-written joke,
instead of assuming the model left the answer blank.
It waits for the editor's dirty indicator to clear before leaving. The later Lathe read proves
persistence independently. Editor failures reject the take as `ui_drift`;
terminal editing cannot substitute for the demonstrated editor interaction.
Prewarm sets code-server user preferences, not settings in a parent folder:
all VS Code AI features are disabled and the secondary Chat sidebar starts hidden.

## Capture policy and automation

Local and hosted authentication use the same unmodified scenario. Managed
code-server must be private: the oracle requires an actual `expose` call with
private access and its owner-only result, not an assistant promise or a public
HTTP-port substitute. The local identity fixture auto-approves its one fixed
synthetic user without a visible sign-in page, while
the real Worker performs PKCE exchange, owner-email authorization, and host-only
session creation. It does not qualify Pocket ID or real passkey authentication.

`render-demo.yml` runs this disposable stack every Wednesday at 14:17 UTC and
on manual dispatch, using `z-ai/glm-5.3-flash` and existing repository provider
secrets under the `lathe-demo` environment. Scheduled and branch runs check and
retain evidence only. Manual dispatch with `publish=true` on the default branch
can refresh the existing `demo-video-latest` release after exact-checkout,
accepted-capture, requested/served-model, and cleanup gates. Publication defaults
off. The regular weekly isolated monitor remains separate and does not record
this video. No hosted personal OWUI or cloud Worker is used.

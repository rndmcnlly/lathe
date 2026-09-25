# Demo capture

The demo is a live synthetic monitor of Open WebUI, Lathe, Daytona, the preview wrapper, and the configured model. `scenario.json` owns the narrative, prompts, required outcomes, and bounded presentation fallbacks. `interpreter.mjs` validates and executes that data through the finite operation set implemented by `capture.mjs`.

Run the offline interpreter tests with:

```sh
npm test
```

Run a live capture with `DEMO_OWUI_URL`, `DEMO_PASSKEY`, and `DEMO_MODEL` configured:

```sh
npm run capture
```

For local iteration after the sandbox is already awake, skip the throwaway
prewarm model turn with `DEMO_SKIP_PREWARM=1 npm run capture`. CI and scheduled
monitoring always use the full production path.

Every run starts with a clean `out/` directory and writes available evidence:
`capture-report.json`, `demo.webm`, `last-page.png`, `playwright-trace.zip`, and
a redacted `sanitized-chat.json`. Scenario-marked main-sequence frames go in
`out/screenshots/` with stable narrative names; failed fallback attempts go in
`out/diagnostics/`. Login and prewarm occur before the recorded context exists
and are never captured as release screenshots. A failed or rejected take remains
diagnostic evidence but cannot replace the `demo-video-latest` video or screenshots.

The recorded activation beat deliberately animates a tutorial cursor through
`Integrations → Tools → Lathe`, with recognition and confirmation pauses. Keep
that choreography when changing selectors: activation is instructional content,
not incidental browser mechanics.

The first recorded question asks Lathe for its installed version. The capture
requires a matching `lathe(manpage="version")` result and saves a version frame,
so the video identifies the deployed toolkit rather than the checkout version.

## Weekly monitor

GitHub Actions runs the production scenario every Wednesday at 14:17 UTC. Scheduled runs retain evidence and report health, but never publish the canonical demo. A required contract failure fails the workflow and identifies its category and first failing step in the job summary.

GitHub sends Actions notifications to the schedule's actor only when that person has enabled web or email Actions notifications. Scheduled workflows run from the default branch, may be delayed or dropped during high load, and GitHub automatically disables schedules in public repositories after 60 days without repository activity. The monitor cannot report its own automatic disablement.

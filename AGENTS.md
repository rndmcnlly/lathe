# Agent Instructions — lathe

This repo is a single-file Open WebUI toolkit (`lathe.py`) with a three-tier test suite. Read `lathe.py` **in full** into your context before making any changes or discussing the code, plus whichever test file is relevant. The codebase is small enough that this is always feasible — do not delegate exploration to a subagent that only reports a summary. You need the actual code in context to reason about interactions, not a lossy precis of it.

## Three audiences, three homes

This project has three distinct audiences. Each has a designated place for its documentation:

1. **Users** ("What is Lathe? What can it do?") — the docs site at [lathe.tools](https://lathe.tools) (`docs/`). The README should not try to be their entry point.
2. **OWUI admins** ("Should I install this? How?") — the `README.md`. This is the most important README audience. They need: what it does to their instance, security/trust model, requirements, installation, valve reference. Keep it focused and operational.
3. **Agents maintaining the project** — this file, `AGENTS.md`. Architecture, credentials, test procedures, contribution rules.

When writing or reorganizing documentation, route content to the right home. Implementation internals (how the onboard script works, output truncation design, browser-side JS mechanics) belong in `docs/` or in code comments — not in the README.

## Credentials and deployment

Deployment credentials live in `.env` (gitignored). See `.env.example` for the full list. Key variables:

- `DAYTONA_API_KEY` — used by integration tests against the live sandbox API.
- `OWUI_URL` / `OWUI_TOKEN` / `OWUI_MODEL` — target OWUI instance for deployment tests. The token is an admin JWT; the model must be a valid model ID on that instance (including connection prefix).
- `DEMO_OWUI_URL` / `DEMO_EMAIL` / `DEMO_PASS` — login credentials used by the demo video capture script. These are also set as GitHub Actions secrets (`DEMO_OWUI_URL`, `DEMO_EMAIL`, `DEMO_PASS`) for the CI workflow.

Dependencies are managed as a uv virtual project (`pyproject.toml` with no
build system). `uv run` resolves everything automatically — no manual venv
activation needed.

To run unit tests (no sandbox needed):

```
uv run python test_unit.py
```

To run integration tests (requires `DAYTONA_API_KEY` in `.env`):

```
uv run python test_integration.py
```

To run deployment tests against the live OWUI instance (requires `OWUI_URL`, `OWUI_TOKEN`, `OWUI_MODEL`):

```
uv run python test_deployment.py              # all deployment tests
uv run python test_deployment.py --verbose    # show all socket.io events
```

## Keep tests in sync with the implementation

`test_unit.py` tests internal helpers directly (imports them by name from `lathe`). When you:

- **Remove or rename a module-level name** — search the test files for it and update accordingly.
- **Change a function signature** — find all call sites in the test files and update them.
- **Delete a code path** — remove the tests that exercised it; dead tests are misleading.
- **Add a new helper or behavior** — add corresponding tests.

Run `uv run python test_unit.py` before committing any change to `lathe.py`. All tests must pass.

## Closing issues

Do not close an issue (via commit message or `gh issue close`) without:

1. **Running the unit tests** and confirming they pass.
2. **Getting admin feedback from a real deployment** — install the updated `lathe.py` on the OWUI instance specified by `OWUI_URL` using the admin token from `.env` and verify the behavior works end-to-end — unless the change clearly has no runtime impact (e.g. pure documentation, comment-only edits, test-only changes).

The unit tests catch regressions in pure-Python helpers but cannot catch broken HTTP paths, OWUI integration issues, or indentation bugs that only surface at runtime. Real deployment is the final gate.

## Design principles

**Poka-yoke over convenience.** The model operating the tools is the primary interface consumer, not a human typing at a REPL. Every tool call should re-assert correct knowledge of the system rather than silently accommodating ambiguity. Prefer designs that make the wrong thing impossible over designs that make the right thing easy.

Example: all file-path parameters (`read`, `write`, `edit`) require absolute paths. The Daytona toolbox API resolves relative paths against `/home/daytona`, but bash's default cwd is `/home/daytona/workspace` — so a relative path like `workspace/file.txt` means different things depending on which tool processes it. Requiring absolute paths eliminates the class of bug entirely. A model that doesn't know where its files are should fail loudly, not succeed quietly in the wrong place.

When adding new tool parameters or options, apply the same lens: can a plausible misuse silently produce wrong results? If so, add validation that rejects it with a clear error.

## Cold-start bootstrap

The Lathe-using agent (the model operating the tools at runtime, not the agent maintaining this repo) must be able to bootstrap a fully functional environment from a blank Daytona sandbox — e.g. immediately after `destroy()`. No tool or utility should be assumed pre-installed beyond what the base Daytona image provides.

The `lathe(manpage="recipes")` manpage is the authoritative source for tested bootstrap scripts. When adding a new recipe, keep these constraints in mind:

- **Egress filtering.** The sandbox can only reach an allowlisted set of hosts. GitHub API (`api.github.com`) and GitHub release downloads (`github.com`, `objects.githubusercontent.com`) are reachable, as are major package registries and CDNs. Scripts that depend on non-allowlisted hosts will silently fail — always verify the download path works from inside the sandbox.
- **No hardcoded versions in download URLs.** GitHub release asset filenames contain the version tag. Use the GitHub API to resolve the latest tag dynamically, then construct the URL. A hardcoded URL rots the moment upstream ships a new release.
- **Install to /tmp.** Binaries in `/tmp` survive sandbox stop/restart but not `destroy()`. This is the right tradeoff — the recipes page tells the model to re-run the install script if the binary is missing.
- **Architecture.** Daytona sandboxes are x86_64 Linux. Use `x86_64-unknown-linux-musl` (static) builds where available.

## Video pipelines

The repo contains two CI-rendered video pipelines, both triggered on push or manual dispatch and uploaded to GitHub Releases:

| Directory | Workflow | Output | Release tag |
|-----------|----------|--------|-------------|
| `explainer-video/` | `render-explainer.yml` | Narrated Remotion explainer (MP4 + VTT) | `explainer-video-latest` |
| `demo-video/` | `render-demo.yml` | Playwright capture of a live session (WebM) | `demo-video-latest` |

Both are embedded on the docs site (`docs/index.html`) via release asset URLs.

**Explainer video** is a Remotion project with TTS narration (requires `DEEPINFRA_TOKEN` secret). Edit `explainer-video/src/data/script.tsx` to change content.

**Demo video** is a headless Playwright script (`demo-video/capture.mjs`) that logs into an OWUI instance, enables Lathe, and drives a real multi-turn conversation. Requires `DEMO_OWUI_URL`, `DEMO_EMAIL`, and `DEMO_PASS` secrets. The capture is non-deterministic — model responses vary between runs. The script asks the model to use markdown links for exposed URLs so that raw proxy URLs never appear as visible text in the video.

Changes to `lathe.py` can break the demo video if they affect user-visible behavior (e.g. tool output format, expose URL structure, sandbox lifecycle). The demo workflow is a reasonable smoke test for the live deployment but should not block merges — it depends on external services (OWUI, Daytona, model inference) that can fail independently.

## Debugging techniques for OWUI integration

Lathe runs inside the OWUI process. Some bugs only manifest at runtime — in the OWUI request context, with real model routing, real auth, and real pipe/manifold behavior. Two techniques that proved essential during delegate() development:

**Temporary diagnostic tools.** When you need to inspect runtime state that only exists inside OWUI (e.g. `__model__` dict contents, `__request__` attributes, ASGI transport behavior), add a temporary tool method to the `Tools` class. Keep it minimal — dump the specific values you need, make one inference call to gather data, then remove it before committing. The `diagnose()` pattern: accept the OWUI dunder params, format them into a string, return it. The model calls it, you read the output, you know what OWUI is actually providing. This is faster and more reliable than guessing from source code, because OWUI's model routing involves caches, pipe functions, workspace model resolution, and access control that interact in ways the source doesn't make obvious.

**Local scripts hitting OWUI endpoints directly.** For testing sub-agent infrastructure (pydantic-ai agent loops, tool-calling response format, usage accounting), write a `uv run --script` file that talks to the live OWUI instance over HTTP using credentials from `.env`. This isolates the pydantic-ai ↔ OWUI interaction from the Lathe toolkit context, making failures easier to attribute. The script can test raw httpx requests (to see exact response JSON), then pydantic-ai agent runs (to see what the SDK makes of it). During delegate() development, this caught that the Anthropic pipe's non-streaming `_complete()` method returned incomplete ChatCompletion dicts — a bug invisible to normal OWUI chat (which uses streaming) but fatal for pydantic-ai's response parser.

Both artifacts are disposable — remove them before committing. They exist to close the feedback loop between "what does the code say" and "what actually happens at runtime."

## Architecture notes

- **Single file** — everything lives in `lathe.py`. Resist splitting it.
- **`_tool_context(emitter, fn)`** — the execution wrapper for all public tool methods except `destroy`. It opens a shared `httpx.AsyncClient`, calls `fn(client)`, and catches standard exceptions. Each tool defines `async def _run(client)` and passes it to `_tool_context`.
- **`destroy`** — intentional exception to the above: it manages its own client and error handling because it is destructive and does not use `_ensure_sandbox`.
- **`_ensure_sandbox(valves, email, client, emitter)`** — called at the top of every `_run(client)`. Transparent to the model; handles create/start/recover/poll.
- **No OWUI storage dependency.** Lathe previously had an `attach()` tool that embedded rich media (images, download cards, syntax-highlighted previews) directly in chat via OWUI's file storage. It was cut because it coupled Lathe to OWUI's storage subsystems, bloated the chat database, and invited relentless scope creep (filetype sniffing, inline rendering, download cards). The replacement is `expose()`: binary outputs go to the sandbox filesystem, dufs or a custom server makes them accessible, and the user gets a URL. This keeps Lathe independent of OWUI internals. Do not re-introduce OWUI storage integration without a clear principle for halting scope creep.


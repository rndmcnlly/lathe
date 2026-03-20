# Agent Instructions — lathe

This repo is a single-file Open WebUI toolkit (`lathe.py`) with a test harness (`test_harness.py`). Read both files **in full** into your context before making any changes or discussing the code. The codebase is small enough that this is always feasible — do not delegate exploration to a subagent that only reports a summary. You need the actual code in context to reason about interactions, not a lossy precis of it.

## Three audiences, three homes

This project has three distinct audiences. Each has a designated place for its documentation:

1. **Users** ("What is Lathe? What can it do?") — the docs site at [lathe.tools](https://lathe.tools) (`docs/`). The README should not try to be their entry point.
2. **OWUI admins** ("Should I install this? How?") — the `README.md`. This is the most important README audience. They need: what it does to their instance, security/trust model, requirements, installation, valve reference. Keep it focused and operational.
3. **Agents maintaining the project** — this file, `AGENTS.md`. Architecture, credentials, test procedures, contribution rules.

When writing or reorganizing documentation, route content to the right home. Implementation internals (how the onboard script works, output truncation design, browser-side JS mechanics) belong in `docs/` or in code comments — not in the README.

## Credentials and deployment

Deployment credentials live in `.env` (gitignored). It contains:

- `DAYTONA_API_KEY` — used by the test harness for integration tests against the live sandbox API.
- `CHAT_ADAMSMITH_AS_OWUI_TOKEN` — admin JWT for `https://chat.adamsmith.as`, the primary deployment. Use this to install or update the tool via the OWUI admin API.

To run unit tests (no sandbox needed):

```
uv run --script test_harness.py unit
```

To run all integration tests (requires `DAYTONA_API_KEY` in `.env`):

```
uv run --script test_harness.py
```

## Keep tests in sync with the implementation

`test_harness.py` tests internal helpers directly (imports them by name from `lathe`). When you:

- **Remove or rename a module-level name** — search the test harness for it and update accordingly.
- **Change a function signature** — find all call sites in the test harness and update them.
- **Delete a code path** — remove the tests that exercised it; dead tests are misleading.
- **Add a new helper or behavior** — add corresponding tests.

Run `uv run --script test_harness.py unit` before committing any change to `lathe.py`. All tests must pass.

## Closing issues

Do not close an issue (via commit message or `gh issue close`) without:

1. **Running the unit tests** and confirming they pass.
2. **Getting admin feedback from a real deployment** — install the updated `lathe.py` on `https://chat.adamsmith.as` using the admin token from `.env` and verify the behavior works end-to-end — unless the change clearly has no runtime impact (e.g. pure documentation, comment-only edits, test-only changes).

The unit tests catch regressions in pure-Python helpers but cannot catch broken HTTP paths, OWUI integration issues, or indentation bugs that only surface at runtime. Real deployment is the final gate.

## Design principles

**Poka-yoke over convenience.** The model operating the tools is the primary interface consumer, not a human typing at a REPL. Every tool call should re-assert correct knowledge of the system rather than silently accommodating ambiguity. Prefer designs that make the wrong thing impossible over designs that make the right thing easy.

Example: all file-path parameters (`read`, `write`, `edit`, fetch `body=@...` / `output=@...`) require absolute paths. The Daytona toolbox API resolves relative paths against `/home/daytona`, but bash's default cwd is `/home/daytona/workspace` — so a relative path like `workspace/file.txt` means different things depending on which tool processes it. Requiring absolute paths eliminates the class of bug entirely. A model that doesn't know where its files are should fail loudly, not succeed quietly in the wrong place.

When adding new tool parameters or options, apply the same lens: can a plausible misuse silently produce wrong results? If so, add validation that rejects it with a clear error.

## Cold-start bootstrap

The Lathe-using agent (the model operating the tools at runtime, not the agent maintaining this repo) must be able to bootstrap a fully functional environment from a blank Daytona sandbox — e.g. immediately after `destroy()`. No tool or utility should be assumed pre-installed beyond what the base Daytona image provides.

The `lathe(manpage="recipes")` manpage is the authoritative source for tested bootstrap scripts. When adding a new recipe, keep these constraints in mind:

- **Egress filtering.** The sandbox can only reach an allowlisted set of hosts. GitHub API (`api.github.com`) and GitHub release downloads (`github.com`, `objects.githubusercontent.com`) are reachable, as are major package registries and CDNs. Scripts that depend on non-allowlisted hosts will silently fail — always verify the download path works from inside the sandbox.
- **No hardcoded versions in download URLs.** GitHub release asset filenames contain the version tag. Use the GitHub API to resolve the latest tag dynamically, then construct the URL. A hardcoded URL rots the moment upstream ships a new release.
- **Install to /tmp.** Binaries in `/tmp` survive sandbox stop/restart but not `destroy()`. This is the right tradeoff — the recipes page tells the model to re-run the install script if the binary is missing.
- **Architecture.** Daytona sandboxes are x86_64 Linux. Use `x86_64-unknown-linux-musl` (static) builds where available.

## Architecture notes

- **Single file** — everything lives in `lathe.py`. Resist splitting it.
- **`_tool_context(emitter, fn)`** — the execution wrapper for all public tool methods except `destroy`. It opens a shared `httpx.AsyncClient`, calls `fn(client)`, and catches standard exceptions. Each tool defines `async def _run(client)` and passes it to `_tool_context`.
- **`destroy`** — intentional exception to the above: it manages its own client and error handling because it is destructive and does not use `_ensure_sandbox`.
- **`_ensure_sandbox(valves, email, client, emitter)`** — called at the top of every `_run(client)`. Transparent to the model; handles create/start/recover/poll.
- **No OWUI storage dependency.** Lathe previously had an `attach()` tool that embedded rich media (images, download cards, syntax-highlighted previews) directly in chat via OWUI's file storage. It was cut because it coupled Lathe to OWUI's storage subsystems, bloated the chat database, and invited relentless scope creep (filetype sniffing, inline rendering, download cards). The replacement is `expose()`: binary outputs go to the sandbox filesystem, dufs or a custom server makes them accessible, and the user gets a URL. This keeps Lathe independent of OWUI internals. Do not re-introduce OWUI storage integration without a clear principle for halting scope creep.


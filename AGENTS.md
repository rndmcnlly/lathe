# Agent Instructions — lathe

Single-file Open WebUI toolkit (`lathe.py`) with a three-tier test suite. Read `lathe.py` **in full** before making changes — do not delegate exploration to a subagent that returns a summary. You need the actual code in context.

## Documentation routing

- **Users** → public landing page at [lathe.tools](https://lathe.tools) (`docs/`), for users considering the toolkit and asking their admins to install it
- **OWUI admins and maintainers** → repository-root Markdown: `README.md` (install, valves, security), `CI-MONITOR.md` (live CI operation)
- **Agents** → this file

`docs/` is exclusively the public, user-facing landing page. Admin-facing and
implementation documentation belongs in repository-root Markdown or code
comments; keep detailed internals out of the installation README.

## Credentials and tests

Credentials in `.env` (gitignored); see `.env.example`. Dependencies via `uv run`.

```
uv run pytest                                  # offline, no credentials
uv run pytest -k delegate                       # focused selection
uv run python test_integration.py               # needs DAYTONA_API_KEY
uv run python test_deployment.py [--verbose]     # also needs OWUI_URL, OWUI_TOKEN, OWUI_MODEL
```

`test_deployment.py` owns the separate OWUI toolkit ID `lathe_test` and never
updates or invokes production `lathe`. It also uses a separate Daytona label
with persistent volumes disabled. The suite deletes its staging toolkit and
sandboxes on exit, including after failures and `--no-deploy` runs. Override
the staging ID with `LATHE_TEST_TOOL_ID`; use `--no-deploy` only when
intentionally testing already-staged source that may be deleted afterward.

Run `uv run pytest` before committing any change to `lathe.py`.
`uv run python test_unit.py` remains an equivalent entry point; it accepts pytest
options. The old named-test registry and `--extended` diagnostics are gone.

### Testing policy

Protect observable contracts and known failure modes, rather than inventorying
functions, constants, prompt wording, or implementation choices. A regression
test must execute the production path it claims to protect. Do not copy the
implementation into a test and test the copy.

- **Offline (`test_unit.py`)**: real temporary files and shipped scripts;
  real module loading with and without future annotations; public wrapper and
  delegate schemas; HTTP boundary failures; lifecycle and message delivery;
  preview credential handling; byte-exact image returns. Delegate tests run the
  actual Pydantic AI loop against a scripted in-process ASGI model responder.
  Tool cores execute their shipped scripts against temporary files; completion
  is observed through subsequent tool calls, not private task names. Real HTTP
  is blocked. Unexpected requests and responder assertions are recorded and
  fail fixture teardown even when production catches the exception. Polling
  tests use a Lathe-scoped clock that advances and yields, not global no-op
  sleeps. Use pytest fixtures and parametrization, not custom runners or
  handwritten HTTP response classes.
- **Daytona (`test_integration.py`)**: real remote execution, interpreter state,
  background notices, previews, and persistent-volume recreation. Type checking
  and capability-policy matrices belong offline. Phases share a sandbox and
  stop after the first failure; cleanup failure makes the run fail.
- **OWUI (`test_deployment.py`)**: actual loader/schema/middleware and real-model
  dispatch. The delegate must produce a file verified by a separate read; an
  echoed canary in its answer is insufficient. This remains a nondeterministic
  smoke test, not an offline model simulation.

`testing_support.py` holds one independently specified public schema contract
shared by local and deployed checks. Do not derive expected values from Lathe
itself. Delegate-specific differences are explicit in the local test.

Run each live suite only once at a time: its fixed staging identity is shared
across invocations. Integration retains one named test volume for reuse;
deployment disables volumes. Neither belongs in default pytest collection.

`monitor_e2e.py` runs deployment checks plus one naturalistic AX journey in a
fresh Docker Open WebUI instance, using real OpenRouter and Daytona. It ignores
`.env`, uses only environment-supplied provider keys, and never targets an
existing OWUI host. CI assigns a unique `lathe-ci-*` deployment label per run;
cleanup must match that exact label. Sandboxes have no persistent volume,
delete on idle stop after five minutes, and have a 30-minute wall-clock TTL.
`test_monitor.py` protects isolation and sanitized evidence offline. See
`CI-MONITOR.md` for workflow operation and future demo-video reuse.

When replacing coverage, verify a few plausible injected faults are detected.
Use in-memory module copies or disposable workspaces, never mutate a deployed
toolkit to evaluate tests. Assertion counts and line coverage are not goals.

## Closing issues

1. Unit tests pass.
2. Run `test_integration.py` for Daytona-facing changes.
3. Run `test_deployment.py` for loader, schema, wrapper, interpreter, or delegate changes. It deploys and verifies the isolated `lathe_test` copy end-to-end.

Unit tests can't catch broken HTTP paths or OWUI integration bugs. Real deployment is the final gate.

## Design principles

**Poka-yoke over convenience.** The model is the interface consumer. Prefer designs that make the wrong thing impossible over designs that make the right thing easy. Example: all file-path params require absolute paths because the Daytona API and bash resolve relative paths differently. When adding parameters, ask: can a plausible misuse silently produce wrong results? If so, reject it with a clear error.

**No OWUI storage dependency.** Use `expose()` for user-facing file access, not OWUI's file storage. Do not re-introduce storage coupling without a clear principle for halting scope creep.

## OWUI parameter type enforcement (#57)

OWUI performs **zero type coercion at dispatch**: after `json.loads`, the parsed dict is splatted directly into the tool function with no validation against the schema. If a model sends `{"offset": "80"}` instead of `{"offset": 80}`, the string passes through unchanged.

Rather than coercing bad types leniently (which silently masks upstream bugs), lathe enforces types **strictly at the wrapper boundary**. `_check_tool_params()` validates that each non-string param has the correct runtime type before the core function ever sees it. Wrong types get a clear error message returned to the model.

- **`_standard_tool` methods**: type check is automatic (built into the factory).
- **Hand-written methods** (`bash`, `delegate`): call `_check_tool_params()` explicitly before entering `_run()`.
- **`_core_*` functions**: trust their type signatures. No coercion code.

`test_bad_types_rejected_before_io` invokes the actual wrappers with wrong types
(string for int, string for bool, etc.) in both loading modes.

### Annotations arrive stringized (PEP 563)

OWUI's tool loader (`open_webui/utils/plugin.py`) has `from __future__ import annotations` and execs tool source with a bare `exec()`, which **inherits the caller's `__future__` flags**. So lathe's module compiles under PEP 563: `inspect.signature(core_fn).parameters[...].annotation` returns the **string** `'int'`, not the class `int`. Feeding that to `isinstance()` raises `isinstance() arg 2 must be a type, a tuple of types, or a union` (the prod bug fixed in 0.23.2: every typed-param tool crashed; `bash` survived only because it passes a literal `int`).

Rule: **never trust raw signature annotations at runtime.** Resolve via `typing.get_type_hints(fn)` (which resolves the strings back to real classes against module globals), as `_standard_tool` now does for both `tool_annotations` and `__annotations__`. `_check_tool_params` also guards: a non-`type` `base_type` is skipped, never passed to `isinstance()`. This only reproduces under OWUI's loader, not a normal file import or top-level `exec`: verify fixes inside the OWUI process or load the real source with future annotations enabled. The `module` fixture does the latter for interface and wrapper-dispatch tests.


## Cold-start bootstrap

The runtime agent must bootstrap from a blank Daytona sandbox. Advanced service details live in `lathe(manpage="services")`. Constraints: egress-allowlisted hosts only, no hardcoded version URLs (resolve via GitHub API), install to `/tmp`, x86_64 Linux (`*-musl` static builds preferred).

## Demo video

The Playwright capture in `demo-video/` is uploaded to GitHub Releases and embedded on the docs site. It captures a live OWUI session (non-deterministic). Changes to user-visible behavior can break it, but it should not block merges.

## Debugging OWUI integration

Two techniques for bugs that only manifest at runtime:

- **Temporary diagnostic tool** — add a throwaway method to `Tools` that dumps OWUI dunder params (`__model__`, `__request__`, etc.), make one call, read the output, remove before committing.
- **Local scripts** — `uv run --script` files that hit the OWUI API directly using `.env` credentials, isolating pydantic-ai ↔ OWUI from the toolkit context.

## Architecture

- **Single file** — everything in `lathe.py`. Resist splitting.
- **Optional preview wrapper** — `preview-wrapper/` is separately deployed
  Cloudflare Worker infrastructure, not imported by the OWUI toolkit. Keep the
  toolkit single-file while maintaining the wrapper contract and deployment
  guide beside it.

### Preview wrapper and private authentication

`expose(target, access, tag)` first asks Daytona for a signed upstream URL, then may
register that credential with the separately deployed Cloudflare Worker in
`preview-wrapper/`. The Worker maps a configurable, DNS-safed hostname prefix
plus a mandatory random nonce to the upstream; Lathe returns only the wrapped
hostname. The optional model-supplied tag is an untrusted display hint. Public requests may fall back to the
direct Daytona bearer URL, but private requests always fail closed.

The private-preview identity chain crosses three systems and each has one
authority:

- OWUI's injected `__user__` is the authority for the preview owner's email.
  Lathe sends that trusted identity and the explicit required access policy over
  the registration control plane, authenticated by `preview_wrapper_key`.
- Pocket ID at `auth.adamsmith.as` is the authority for the browser user's
  identity. The Worker uses OIDC authorization code + PKCE and authorizes only
  an exact normalized match between Pocket ID's verified email claim and the
  registered owner email. Model input, URL parameters, and browser-submitted
  identity are never authorities.
- The Worker is the authority for the browser session. OIDC state and opaque
  sessions are short-lived KV records scoped to one random preview
  hostname;
  the browser receives a Secure, HttpOnly, SameSite=Lax, host-only `__Host-`
  cookie. Never use a parent-domain cookie across preview hosts.

Private OIDC callbacks return to the same random preview hostname (Pocket ID
supports a single-label wildcard callback). Validate state against that exact
host before exchanging the code. Clamp auth state and session expiry to the
preview registration's remaining lifetime. The proxy must remove its auth
cookie before forwarding requests upstream and must discard upstream attempts
to set that cookie, since sandbox applications are untrusted relative to the
wrapper's authentication boundary.

Registration records contain the target, access policy, owner identity, and
absolute expiry. The sensitive upstream URL is never echoed by the Worker or
included in auth redirects/errors. KV registration expiry remains the ultimate
lease boundary; authentication does not keep a sandbox or service alive.
- **`_tool_context(emitter, fn)`** — execution wrapper for all tools except `destroy`. Opens `httpx.AsyncClient`, calls `fn(client)`, catches exceptions.
- **`_ensure_sandbox(valves, email, client, emitter)`** — called at top of every `_run`. Transparent create/start/recover/poll.
- **`destroy`** — manages its own client; does not use `_ensure_sandbox`.

### Tool core layer

Seven tool cores (bash, read, write, edit, glob, grep, view) back the **Tools class** (outer model, via OWUI); all except `view` also back **delegate closures** (sub-agent, via pydantic-ai). Both surfaces call shared `_core_*` functions containing all I/O logic:

```
_core_read(valves, sandbox_id, client, *, path, offset, limit) -> str
_core_write(valves, sandbox_id, client, *, path, content) -> str
_core_edit(valves, sandbox_id, client, *, path, old_string, new_string, replace_all) -> str
_core_glob(valves, sandbox_id, client, *, pattern, max_lines) -> str
_core_grep(valves, sandbox_id, client, *, pattern, files, max_lines) -> str
_core_view(valves, sandbox_id, client, *, path) -> str
_core_bash(valves, sandbox_id, client, *, command, workdir, user_pairs, foreground_seconds, emit) -> str
```

Signature: `(valves, sandbox_id, client, **tool_params) -> str`. No OWUI dunders, no sandbox lifecycle. Bash also has two pure helpers: `_build_bash_script` and `_format_bash_result`. The cores call shared Daytona I/O helpers (`_upload_file`, `_download_file`, `_run_sandbox_script`) so HTTP patterns and auth are defined once.

`view` is Tools-only because its return convention (a `data:image/...;base64,` string, which OWUI ≥ 0.11.0 lifts into a model-visible image part) only fires through OWUI's tool middleware; the delegate's pydantic-ai result channel is plain text, so the base64 would flood the sub-agent context. Its hand-written Tools wrapper (a) refuses up front when the current model cannot accept image input, via `_model_supports_vision` on the `__metadata__`/`__model__` dicts (`architecture.input_modalities` is authoritative; `info.meta.capabilities.vision: false` is the fallback; unknown allows), and (b) returns the core result byte-identical, since OWUI detects the image with `startswith("data:image/")` and prepended harness text would silently break that detection. Harness messages stay queued for the next non-deferring tool call.

**Two thin wrapper layers call the cores:**

1. **Tools methods** — most are generated by `_standard_tool(core_fn, ...)`, which produces a method with the correct OWUI-visible signature, docstring, and all boilerplate (ensure_sandbox, ensure_chat_init, emit, drain harness messages). `bash` stays hand-written because it has unique pre-call logic (user env var resolution from UserValves, foreground timeout fallback from Valves, background polling callback). `view` is hand-written for its vision-capability gate and byte-exact image return. `onboard`, `delegate`, `expose`, `destroy`, `lathe`, and `handoff` are also hand-written (each has unique logic that doesn't fit the standard pattern).
2. **Delegate closures** (`_build_delegate_tools`) — generated by `_build_delegate_tool(core_fn, ...)`, which uses `exec()` to produce a function with the correct Python signature (pydantic-ai introspects `get_type_hints()`, not `__signature__`). Each tool is a one-line declaration.

To change tool behavior, edit the `_core_*` function. Both surfaces pick it up.

### `_standard_tool` factory

`_standard_tool(core_fn, emit_start, emit_done, extra_core_kwargs)` builds a Tools class method from a `_core_*` function. It introspects the core function's signature to extract tool-visible parameters (skipping infrastructure params in `_CORE_INFRA_PARAMS`), constructs a synthetic `inspect.Signature` combining tool params + OWUI dunder params, and sets both `__signature__` and `__annotations__` explicitly on the generated method.

**Both are critical.** OWUI's `convert_function_to_pydantic_model` uses `inspect.signature()` for parameter names and defaults, but `get_type_hints()` (which reads `__annotations__`) for **types**. Without `__annotations__`, all params silently fall back to `Any`, which Pydantic renders as `"type": "string"` in the JSON schema after OWUI's `clean_properties` fallback. `functools.wraps` copies `__annotations__` but does NOT copy `__signature__`, so both must be set manually on dynamically generated methods.

`test_tools_interface` checks known-good parameter names, types, defaults,
injected context parameters, and `get_type_hints()` results in both loading modes.

### Docstring single source of truth

`_core_*` docstrings (`:param:` format) are the single source of truth for tool descriptions and parameter docs. `_standard_tool` and `_build_delegate_tool` copy the docstring onto Tools methods and delegate closures respectively. Both OWUI and pydantic-ai parse `:param:` natively; extra `:param:` lines for infrastructure params are silently ignored. `test_delegate_interface` verifies delegate schema parity; `test_tools_interface` verifies the Tools class surface.

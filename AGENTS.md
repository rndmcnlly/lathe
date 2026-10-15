# Agent Instructions: Lathe

Single-file Open WebUI toolkit (`lathe.py`). Keep the OWUI runtime implementation
in that file. Optional `recipes/` programs run inside the sandbox via `bash`, not
as imported runtime modules; the optional `preview-wrapper/` Cloudflare Worker
is separate infrastructure. Read [DESIGN.md](DESIGN.md) for the human-facing tool
story and responsibility boundaries before changing the tool surface.

## Documentation routing

- Users: [lathe.tools](https://lathe.tools), `docs/` is exclusively the public landing page.
- Admins: root `README.md` for installation, valves, and security.
- Maintainers: this file for invariants/tests; `CI-MONITOR.md` for live CI;
  `preview-wrapper/README.md` for the authoritative wrapper protocol/deployment.

Keep implementation detail out of the installation README. Runtime orientation
should be concise, with task-specific manuals for details. Preserve safeguards
and AGENTS.md/skill discovery; don't inventory wording in tests.

## Credentials and tests

Credentials: gitignored `.env`, documented in `.env.example`. Use `uv run`.

```sh
uv run pytest                                  # offline, no credentials
uv run pytest -k interpreter                    # focused selection
uv run python test_integration.py               # DAYTONA_API_KEY
uv run python test_deployment.py [--verbose]     # also OWUI_URL, OWUI_TOKEN, OWUI_MODEL
```

Run offline tests before committing any `lathe.py` change. `uv run python
test_unit.py` is an equivalent pytest entry point and accepts pytest options.

### Testing policy

Protect observable contracts and known failure modes, not function inventories,
constants, prompt wording, or implementation choices. Execute the production path
under test, never a copied implementation. Use pytest fixtures/parametrization,
not custom runners or handwritten HTTP response classes.

- Offline (`test_unit.py`): real temporary files and shipped scripts; loading
  with/without future annotations; wrapper schemas, HTTP failures,
  lifecycle/message delivery, preview credentials, and byte-exact images.
  Block real HTTP; record unexpected requests/responder assertions and
  fail teardown even if production catches them. Observe background completion
  through subsequent tool calls, not private task names. Use a Lathe-scoped
  advancing/yielding clock, never global no-op sleeps.
- Daytona (`test_integration.py`): remote execution, interpreter state, background
  notices, previews, and persistent-volume recreation. Phases share a sandbox and
  stop after the first failure; cleanup failure fails the run. Type/capability
  policy matrices belong offline.
- OWUI (`test_deployment.py`): actual loader/schema/middleware and real-model
  dispatch. Verify created files by a separate read, not an echoed
  canary. This is a nondeterministic smoke test, not a model simulation or a
  minimum-version matrix: it qualifies local source against `OWUI_URL`.

`testing_support.py` specifies the public schema contract independently of Lathe,
shared by local/deployed checks.
When replacing coverage, demonstrate detection of plausible injected faults in
in-memory copies or disposable workspaces, never a deployed toolkit. Assertion
counts and line coverage are not goals.

### Live isolation and qualification

Run only one instance of each live suite at a time: staging identities are shared.
Neither live suite belongs in default pytest collection.

- Integration provisions no extra volumes; cleanup deletes sandboxes.
- Deployment owns `lathe_test`, never production `lathe`, with a separate Daytona
  label and no extra volumes. It deletes its toolkit/sandboxes on exit, including
  failures and `--no-deploy`. Override with `LATHE_TEST_TOOL_ID`; use `--no-deploy`
  only for intentionally staged source that may be deleted afterward.
- `monitor_e2e.py` uses fresh Docker OWUI, real OpenRouter/Daytona, environment
  keys only (ignores `.env`), never an existing OWUI host. Cleanup must match the
  exact run's `lathe-ci-*` label. No volumes; delete on idle stop after five
  minutes; 30-minute wall-clock TTL. `test_monitor.py` protects isolation and
  sanitized evidence. Operation: `CI-MONITOR.md`.

For closing issues: offline tests pass; run integration for Daytona-facing
changes and deployment for loader/schema/wrapper/interpreter behavior
changes. Unit tests do not qualify live HTTP/OWUI boundaries. Pure prose/comment
edits need offline tests, not paid model qualification solely for wording.

## Design and compatibility invariants

- **Poka-yoke over convenience**: reject plausible misuse that silently produces
  wrong results. File tools require absolute paths (Daytona and bash resolve
  relative paths differently); documented exceptions such as onboard("") are explicit.
- **No OWUI storage dependency**: use expose for user-facing files, not OWUI file
  storage; do not reintroduce coupling without a scope-limiting principle.
- **Strict wrapper types**: OWUI splats parsed JSON without schema validation or
  coercion. `_check_tool_params` must run before parameter use/I/O, automatically
  in `_standard_tool` and explicitly in hand-written parameterized methods.
  Reject bool-for-int and invalid container members; errors name the parameter
  and expected type without echoing values. Cores trust signatures; no coercion.
- **Resolve annotations** with `typing.get_type_hints()`, never raw signature
  annotations for runtime checks. OWUI exec inherits future-annotations flags,
  making annotations strings. Test both loading modes.
- **Schema factories**: `_standard_tool` must set both `__signature__` (names,
  defaults) and resolved `__annotations__` (types). OWUI reads both; missing
  annotations silently degrade schema types.
- **Docstrings**: `_core_*` `:param:` docs supply the public tool descriptions.
  Change tool behavior in cores.
- **OWUI floor**: 0.12.0 is the supported minimum. Raise the
  frontmatter floor only for concrete required behavior/verified incompatibility,
  backed by upstream evidence, not a speculative version matrix.
- **Cold start**: bootstrap from a blank x86_64 Linux Daytona sandbox. Egress
  allowlist only; resolve versions via GitHub API rather than hardcoded URLs;
  install to `/tmp`, prefer static `*-musl` builds. Details: services manpage.

## Runtime boundaries

Shared `_core_*` functions own tool I/O, without OWUI dunders or sandbox lifecycle.
Tools wrappers call them. Shared
Daytona helpers centralize upload/download/script execution. Read actual function
signatures rather than maintaining a parallel signature catalog here.

`_tool_context` owns HTTP client/error handling for sandbox operations except
destroy. Manual calls need no sandbox. Sandbox-backed wrappers call `_ensure_sandbox` for
create/start/recover/poll and `_ensure_chat_init` for chat context. Destroy manages
its own client without creating/recovering a sandbox.

View must return the core `data:image/...;base64,` result byte-identically: OWUI
uses startswith detection, so prepended text breaks image lifting. Keep harness
messages queued for a later text result. Reject known non-vision models before
I/O: architecture input modalities are authoritative, capabilities.vision=false
is fallback, unknown allows.

## Preview security

The complete protocol and private-authentication implementation live in
`preview-wrapper/README.md`. Preserve these cross-system invariants:

- Private unless the user explicitly requests public/world access. Public
  headerless requests may fall back to direct Daytona bearer URLs; private and
  header-bearing requests fail closed. Wrapper registration does not keep the
  sandbox/service alive.
- OWUI injected user context owns registrar identity; verified OIDC email owns
  browser identity; the wrapper owns browser sessions. Model tags/URL parameters
  are never identity authorities. Match normalized verified email exactly.
- Random per-preview hostnames, host-bound OIDC state, PKCE, short-lived opaque
  sessions, Secure/HttpOnly/SameSite=Lax host-only `__Host-` cookies. No parent-domain
  auth cookies. Clamp state/session expiry to registration lifetime; KV expiry
  is the final lease boundary. Validate callback host before token exchange.
- Strip wrapper auth cookies before forwarding; discard upstream attempts to
  set them. Never echo upstream bearer URLs in wrapper output/auth errors/redirects.
  The wrapper belongs on a separate registrable domain from OWUI.

## Debugging and demo

OWUI-only bugs: use a temporary diagnostic Tools method to inspect injected
context (remove before committing), or a local `uv run --script` API probe using
`.env`. Do not expose credentials in published evidence.

`demo-video/` captures a nondeterministic live OWUI session for Releases/the docs
site. User-visible changes can break it, but it should not block merges.

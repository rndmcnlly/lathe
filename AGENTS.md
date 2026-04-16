# Agent Instructions — lathe

Single-file Open WebUI toolkit (`lathe.py`) with a three-tier test suite. Read `lathe.py` **in full** before making changes — do not delegate exploration to a subagent that returns a summary. You need the actual code in context.

## Documentation routing

- **Users** → docs site at [lathe.tools](https://lathe.tools) (`docs/`)
- **OWUI admins** → `README.md` (install, valves, security)
- **Agents** → this file

Implementation internals belong in `docs/` or code comments, not the README.

## Credentials and tests

Credentials in `.env` (gitignored); see `.env.example`. Dependencies via `uv run`.

```
uv run python test_unit.py                      # no sandbox needed
uv run python test_integration.py               # needs DAYTONA_API_KEY
uv run python test_deployment.py [--verbose]     # needs OWUI_URL, OWUI_TOKEN, OWUI_MODEL
```

Run `uv run python test_unit.py` before committing any change to `lathe.py`. Keep tests in sync: rename/remove/add tests when you rename/remove/add code they cover.

## Closing issues

1. Unit tests pass.
2. Deploy to the OWUI instance (`OWUI_URL` / `OWUI_TOKEN` in `.env`) and verify end-to-end — unless the change has no runtime impact.

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

The `string_typed_params` unit test verifies that `_check_tool_params` rejects wrong types (string for int, string for bool, etc.) and accepts correct types.

## Cold-start bootstrap

The runtime agent must bootstrap from a blank Daytona sandbox. Recipes live in `lathe(manpage="recipes")`. Constraints: egress-allowlisted hosts only, no hardcoded version URLs (resolve via GitHub API), install to `/tmp`, x86_64 Linux (`*-musl` static builds preferred).

## Video pipelines

Two CI-rendered videos in `explainer-video/` and `demo-video/`, uploaded to GitHub Releases, embedded on the docs site. The demo captures a live OWUI session (non-deterministic). Changes to user-visible behavior can break it, but it should not block merges.

## Debugging OWUI integration

Two techniques for bugs that only manifest at runtime:

- **Temporary diagnostic tool** — add a throwaway method to `Tools` that dumps OWUI dunder params (`__model__`, `__request__`, etc.), make one call, read the output, remove before committing.
- **Local scripts** — `uv run --script` files that hit the OWUI API directly using `.env` credentials, isolating pydantic-ai ↔ OWUI from the toolkit context.

## Architecture

- **Single file** — everything in `lathe.py`. Resist splitting.
- **`_tool_context(emitter, fn)`** — execution wrapper for all tools except `destroy`. Opens `httpx.AsyncClient`, calls `fn(client)`, catches exceptions.
- **`_ensure_sandbox(valves, email, client, emitter)`** — called at top of every `_run`. Transparent create/start/recover/poll.
- **`destroy`** — manages its own client; does not use `_ensure_sandbox`.

### Tool core layer

Six tools (bash, read, write, edit, glob, grep) are exposed through two surfaces: the **Tools class** (outer model, via OWUI) and **delegate closures** (sub-agent, via pydantic-ai). Both call shared `_core_*` functions containing all I/O logic:

```
_core_read(valves, sandbox_id, client, *, path, offset, limit) -> str
_core_write(valves, sandbox_id, client, *, path, content) -> str
_core_edit(valves, sandbox_id, client, *, path, old_string, new_string, replace_all) -> str
_core_glob(valves, sandbox_id, client, *, pattern, max_lines) -> str
_core_grep(valves, sandbox_id, client, *, pattern, files, max_lines) -> str
_core_bash(valves, sandbox_id, client, *, command, workdir, user_pairs, foreground_seconds, emit) -> str
```

Signature: `(valves, sandbox_id, client, **tool_params) -> str`. No OWUI dunders, no sandbox lifecycle. Bash also has two pure helpers: `_build_bash_script` and `_format_bash_result`. The cores call shared Daytona I/O helpers (`_upload_file`, `_download_file`, `_run_sandbox_script`) so HTTP patterns and auth are defined once.

**Two thin wrapper layers call the cores:**

1. **Tools methods** — most are generated by `_standard_tool(core_fn, ...)`, which produces a method with the correct OWUI-visible signature, docstring, and all boilerplate (ensure_sandbox, ensure_chat_init, emit, drain harness messages). `bash` stays hand-written because it has unique pre-call logic (user env var resolution from UserValves, foreground timeout fallback from Valves, background polling callback). `onboard`, `delegate`, `expose`, `destroy`, `lathe`, and `handoff` are also hand-written (each has unique logic that doesn't fit the standard pattern).
2. **Delegate closures** (`_build_delegate_tools`) — generated by `_build_delegate_tool(core_fn, ...)`, which uses `exec()` to produce a function with the correct Python signature (pydantic-ai introspects `get_type_hints()`, not `__signature__`). Each tool is a one-line declaration.

To change tool behavior, edit the `_core_*` function. Both surfaces pick it up.

### `_standard_tool` factory

`_standard_tool(core_fn, emit_start, emit_done, extra_core_kwargs)` builds a Tools class method from a `_core_*` function. It introspects the core function's signature to extract tool-visible parameters (skipping infrastructure params in `_CORE_INFRA_PARAMS`), constructs a synthetic `inspect.Signature` combining tool params + OWUI dunder params, and sets both `__signature__` and `__annotations__` explicitly on the generated method.

**Both are critical.** OWUI's `convert_function_to_pydantic_model` uses `inspect.signature()` for parameter names and defaults, but `get_type_hints()` (which reads `__annotations__`) for **types**. Without `__annotations__`, all params silently fall back to `Any`, which Pydantic renders as `"type": "string"` in the JSON schema after OWUI's `clean_properties` fallback. `functools.wraps` copies `__annotations__` but does NOT copy `__signature__`, so both must be set manually on dynamically generated methods.

The `tools_schema_parity` unit test locks in the known-good parameter names, types, defaults, dunder params, and `get_type_hints()` results for every Tools method, catching silent schema breakage.

### Docstring single source of truth

`_core_*` docstrings (`:param:` format) are the single source of truth for tool descriptions and parameter docs. `_standard_tool` and `@_doc_from_core(_core_fn)` copy the docstring onto Tools methods and delegate closures respectively. Both OWUI and pydantic-ai parse `:param:` natively; extra `:param:` lines for infrastructure params are silently ignored. The `delegate_tools_build` test verifies delegate schema parity; `tools_schema_parity` verifies the Tools class surface.

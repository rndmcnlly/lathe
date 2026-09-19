# Lathe

A single-file Open WebUI toolkit that gives any model a coding agent's tool surface — `bash`, `read`, `write`, `edit`, `glob`, `grep`, `view`, `delegate`, `onboard`, `expose`, `destroy` — executing against per-user cloud sandboxes via [Daytona](https://www.daytona.io/).

**For users**: See [lathe.tools](https://lathe.tools) for what Lathe can do, how to use it, and example workflows.

## What it does to your instance

Lathe registers fourteen tools that models can call in [Native function calling mode](https://docs.openwebui.com/features/extensibility/plugin/tools/). When a user's model calls a tool, Lathe creates, starts, or resumes a cloud sandbox VM via the Daytona control plane and toolbox APIs. All sandbox operations go outbound from your OWUI server.

No OWUI internals are touched. The toolkit does not import `open_webui.*`, does not use OWUI file storage, and does not modify models, prompts, users, or other configuration. Its runtime dependencies are `httpx` and `pydantic-ai-slim[openai]`.

## Security and trust model

- **Per-user sandbox isolation** — Each OWUI user gets exactly one sandbox, identified by their email address. Users cannot access each other's sandboxes.
- **Deployment label scoping** — Sandboxes are tagged with a `deployment_label` (e.g. `chat.example.com`), so multiple OWUI instances sharing a Daytona account do not collide.
- **User secrets** — The `env_vars` UserValve is a password field (masked in UI), but its values are deliberately entrusted to the model through every `bash` command it can invoke. The model can read command environments and should be treated as able to disclose or misuse these credentials. Use narrowly scoped, revocable credentials only.
- **Destroy confirmation guard** — The `destroy` tool requires confirmation through Open WebUI's interactive confirmation dialog.
- **No model prompt modification** — Lathe does not inject system prompts or alter model behavior. It only exposes tools.

## Requirements

1. **Daytona account** with an API key ([daytona.io](https://www.daytona.io/))
2. **Open WebUI** with Native function calling mode enabled (≥ 0.11.0 for `view()`; image returns to the model rely on the history-replay fix shipped in that release)
3. Models that support tool/function calling (`view()` additionally needs a vision-capable model)

## Installation

Deploy `lathe.py` as a tool via the OWUI admin API:

```bash
# Upload the toolkit
curl -X POST "https://your-owui.example.com/api/v1/tools/create" \
  -H "Authorization: Bearer $OWUI_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg content "$(cat lathe.py)" '{
    id: "lathe",
    name: "Lathe",
    content: $content
  }')"
```

Then configure the admin Valves (below) through the OWUI UI or API.

To update an existing installation, use the `/api/v1/tools/id/lathe/update` endpoint with the same payload shape.

## Valves (admin configuration)

| Valve | Default | Purpose |
|-------|---------|---------|
| `daytona_api_key` | *(empty, password field)* | Daytona API key |
| `daytona_api_url` | `https://app.daytona.io/api` | Control plane URL |
| `daytona_proxy_url` | `https://proxy.app.daytona.io/toolbox` | Toolbox proxy URL |
| `preview_wrapper_url` | *(empty)* | HTTPS registration endpoint for a public or owner-authenticated preview wrapper |
| `preview_wrapper_key` | *(empty, password field)* | Installation bearer credential for that wrapper |
| `preview_expiry_seconds` | `86400` | Upstream HTTP signed-URL lifetime (60–86400 seconds); independent of wrapper registration expiry |
| `deployment_label` | *(empty, must configure)* | Label key for sandbox tagging (e.g. `chat.example.com`) |
| `auto_stop_minutes` | `15` | Idle timeout before sandbox stops |
| `auto_archive_minutes` | `60` | Minutes after stop before sandbox archives |
| `auto_delete_minutes` | `-1` | Minutes after archive before permanent deletion (`-1` = never) |
| `persistent_volume` | `true` | Mount a persistent S3/FUSE volume at `/home/daytona/volume` |
| `auto_create_sandbox` | `true` | Auto-create a sandbox when none exists. Disable when sandboxes are provisioned externally |
| `sandbox_missing_message` | *(empty)* | Message shown to the agent when no sandbox exists and `auto_create_sandbox` is off |
| `sandbox_create_overrides` | `{}` | JSON of extra Daytona create args (see below) |
| `foreground_timeout_seconds` | `30` | Seconds to wait for a bash command before auto-backgrounding (1–300) |

### Sandbox shape (`sandbox_create_overrides`)

`sandbox_create_overrides` is a JSON object merged into the Daytona sandbox
create request, letting you control sandbox shape without a valve per field:

```json
{"cpu": 2, "memory": 4, "disk": 20, "snapshot": "my-snapshot", "target": "us"}
```

Resource fields (`cpu`, `memory`, `disk`, `gpu`) are integers in cores / GB /
units; `snapshot` selects the base image; `target` picks a region. Any field
the Daytona [`POST /sandbox`](https://www.daytona.io/docs/en/tools/api/) body
accepts works here. The keys `name`, `labels`, and `volumes` are managed by
lathe (per-user lookup invariant and the `persistent_volume` valve) and are
rejected with a clear error if you try to set them.

### HTTP preview access and wrapping

> [!WARNING]
> Without a preview wrapper, every HTTP URL returned by `expose()` is a bearer
> credential: anyone who sees or copies it can access the service until it
> expires or stops working. A raw URL exposed in chat, a screen share, or a
> recording can disclose a terminal-capable code-server session or a writable
> file browser. After several months of personal and institutional use, this has
> been Lathe's most significant recurring security sharp edge.

Every `expose()` call requires the agent to choose `access="public"` or
`access="private"`. Public requests try the configured wrapper first, then fall
back to Daytona's direct signed bearer URL if wrapping is unavailable, refused,
or malformed. Private requests require a validated `owner-authenticated`
wrapper response and never downgrade to public. The model-facing contract tells
the agent to choose private unless the user specifically requests public/world
access; convenience or wrapper failure is not permission to disclose a service.

Direct signed URLs can be appropriate for intentionally public, personal, or
otherwise low-risk services. Configure `preview_wrapper_url` and
`preview_wrapper_key` to add a distinct branded origin, cookie isolation, and,
when the wrapper supports it, browser authentication. Wrapping applies to
arbitrary HTTP services, dufs, and code-server before a URL reaches the model or
user. Lathe does not provide SSH access.

#### What a wrapper does

A wrapper is a reverse proxy deployed outside the OWUI process. It keeps the
Daytona-provided bearer URL on the server side and gives the browser a distinct
public origin. A public wrapper provides branding and cookie isolation but URL
possession still grants access. A private wrapper additionally authenticates the
browser and authorizes access under the deployment's policy. A personal
deployment might admit only the registered owner email; an institutional
deployment might admit an appropriate class, lab, or campus community.

Run the wrapper on a domain distinct from Open WebUI. This provides cookie and
origin isolation between OWUI and arbitrary user-controlled services running in
the sandbox. This repository includes an optional Cloudflare Worker under
[`preview-wrapper/`](preview-wrapper/) but deliberately deploys it as separate
infrastructure. Adding proxy routes inside OWUI could expose OWUI-origin cookies
or other same-origin authority to sandbox applications whose content is
controlled by users and models. An external origin makes that class of
ambient-authority failure structurally unavailable.

Without a wrapper, Daytona's opaque preview URL is effectively the access token.
This capability-URL model is reasonable when the operator understands that
constraint and will not share, stream, or record the screen while an exposed
service or its URL is visible. It is a poor default when demonstrations,
institutional support, or routine screen sharing are part of the workflow.

The wrapper receives the upstream URL and ownership from OWUI's injected
`__user__` context. The model can select an exposure target, port, and desired
access level, but it cannot supply the owner identity or substitute an arbitrary
upstream URL. For private access, the generic identity invariant is email-based:
the wrapper authenticates the browser through OAuth/OIDC and matches the
provider-verified email claim against the owner email supplied at registration.
The wrapper is trusted infrastructure: its operator must explicitly authorize
the Lathe installation and define its identity namespace, destination policy,
public naming, replacement behavior, revocation, retention, and browser-login
policy.

#### Registration contract

Lathe sends an HTTPS POST to `preview_wrapper_url` with
`Authorization: Bearer <preview_wrapper_key>` and JSON:

```json
{"owner":{"subject":"injected-owui-user-id","email":"owner@example.edu"},"slot":"5000","upstream_url":"https://temporary-upstream.example/"}
```

`subject` and `email` come exclusively from trusted request context. `slot` is
the resolved service port. Lathe sends no sandbox-management credential to the
wrapper; the installation credential establishes only the registrar's
authority.

A successful response contains either public wrapping:

```json
{"url":"https://lathe-nonce.previews.example/","access_mode":"public-wrapped","expires_at":"2026-09-18T19:00:00Z"}
```

or owner-authenticated wrapping:

```json
{"url":"https://owner-5000.previews.example/","access_mode":"owner-authenticated","expires_at":"2026-09-18T19:00:00Z"}
```

Lathe verifies a distinct HTTPS destination, an access mode matching the agent's
request, and a future timezone-qualified expiry. It rejects responses that
reflect the upstream hostname or installation credential in the returned URL.
It never follows registration redirects or exposes raw response/error text.
For public requests, any wrapper failure or mode mismatch falls back to the
direct signed URL. For private requests, missing configuration or identity,
registration failure, mode mismatch, and malformed responses all fail closed.
The wrapper remains trusted to implement its claims; valid JSON alone cannot
prove that it enforces browser ownership.

#### Lifetime and browser behavior

`preview_expiry_seconds` controls the upstream signed URL (default and maximum:
24 hours). The wrapper chooses its own registration lifetime; Lathe reports the
two clocks separately. Sandbox sleep, service failure, or explicit shutdown can
end availability sooner. Calling `expose()` again prepares the service and
replaces or renews the registration. The wrapper may invalidate existing
browser sessions and WebSocket connections when replacing it.

A wrapper may assign a fresh browser origin on every registration. Do not
promise bookmark stability: changing origins can intentionally isolate service
workers and browser storage from previous exposures. Browser compatibility,
including uploads, redirects, cookies, workers, WebSockets, and open-connection
expiry, belongs to the wrapper. Lathe embeds no deployment domains,
identity-provider protocol, or proprietary proxy protocol in this interface.

An owner-authenticated URL appearing in a screen recording does not itself
grant access to viewers. Wrapping does not hide application content already
visible in the recording; collaborator access depends on the wrapper's policy,
not possession of the URL. Opening a wrapped URL may require a separate login
even when the owner is already signed into Open WebUI.

### Externally provisioned sandboxes (`auto_create_sandbox`)

If another system shapes and provisions sandboxes (e.g. giving different users
different sandboxes), set `auto_create_sandbox` to `false`. When the agent then
finds no sandbox for the user, it receives `sandbox_missing_message` instead of
a freshly created one. Use that message to point users at your provisioning
flow, e.g. *"Visit https://example.com/setup to create your sandbox first."*

## UserValves (per-user configuration)

| Valve | Default | Purpose |
|-------|---------|---------|
| `env_vars` | `{}` *(password field)* | JSON object of environment variables injected into every `bash` command. e.g. `{"GITHUB_TOKEN":"ghp_...","OPENAI_API_KEY":"sk-..."}` |

## Tools reference

| Tool | Purpose |
|------|---------|
| `lathe(manpage)` | Agent-facing manual system — orientation, recipes, troubleshooting |
| `onboard(path)` | Load project context (AGENTS.md + skill catalog) |
| `bash(command, workdir, foreground_seconds)` | Execute shell commands (auto-backgrounds after ~30s, output truncated to last 2000 lines / 50 KB) |
| `read(path, start, stop)` | Read file with line numbers; supports positive and negative half-open ranges |
| `write(path, content)` | Write/create file (auto-creates parent dirs) |
| `edit(path, old_string, new_string, replace_all)` | Exact string replacement |
| `glob(pattern, max_lines)` | Search for files by glob pattern (hierarchical output, collapsed directories) |
| `grep(pattern, files, max_lines)` | Search file contents by regex (grouped by file with line numbers) |
| `view(path)` | Load an image into the model's visual context (PNG/JPEG/GIF/WebP, ≤ 4 MB, content-sniffed) |
| `interpret(code, timeout)` | Run Python in a conversation-scoped persistent interpreter |
| `delegate(task, context_files, max_steps, foreground_seconds)` | Dispatch a sub-agent to perform a multi-step task autonomously |
| `expose(target, access)` | Expose an HTTP service, file browser, or browser IDE with required `public` or `private` access |
| `handoff()` | Prepare instructions for continuing work in a fresh conversation |
| `destroy()` | Permanently delete the sandbox after interactive confirmation |

## Testing

```bash
uv run pytest                               # offline, no credentials (~2s)
uv run python test_integration.py            # needs DAYTONA_API_KEY in .env
uv run python test_deployment.py [--verbose] # also needs OWUI_URL, OWUI_TOKEN, OWUI_MODEL
```

The tiers cover different boundaries:

- The offline suite checks local behavior without provisioning resources. Use `uv run pytest -k delegate` for a focused selection; `uv run python test_unit.py` is also supported.
- `test_integration.py` verifies the live Daytona API using an isolated test identity. It retains one named test volume for reuse and deletes test sandboxes on exit. Cleanup failure makes the run fail.
- `test_deployment.py` temporarily deploys local `lathe.py` to the isolated OWUI toolkit ID `lathe_test`, never `lathe`. It configures a separate Daytona label with persistent volumes disabled, checks exact source and complete loaded schema parity, then exercises model-mediated `bash`, `write`, `read`, `interpret`, `view`, and `delegate` dispatch. It deletes the staging toolkit and sandboxes on exit, including after failures and `--no-deploy` runs. Use `--no-deploy` to test an already staged copy that may be deleted afterward, or `LATHE_TEST_TOOL_ID` to choose another staging ID.

Run only one instance of each live suite at a time: staging identities are shared.
See [AGENTS.md](AGENTS.md#testing-policy) for contributor testing policy.

For a focused protected-preview test, use `test_deployment.py --preview-only`
with `LATHE_PREVIEW_WRAPPER_URL`, `LATHE_PREVIEW_WRAPPER_KEY`,
`LATHE_PREVIEW_EXPECTED_URL`, `LATHE_PREVIEW_REVOKE_URL`, and optionally
`LATHE_PREVIEW_ACCESS` (`private` by default) in the environment.
For transient hostnames, use `LATHE_PREVIEW_EXPECTED_PATTERN` instead of the
exact URL and `{label}` in the revoke URL; cleanup uses the validated returned
hostname. The disposable suite refuses the production toolkit ID `lathe`.
This checks staged source/schema and model-mediated exposure, then revokes the
test registration. It removes only the dependency-install frontmatter from the
staging copy, so testing `expose` beside an older toolkit does not upgrade that
instance's shared Pydantic AI dependency. It does not qualify the other tools or
a full dependency migration.

## Files

| File | Purpose |
|------|---------|
| `lathe.py` | The OWUI toolkit (single file, deployed via OWUI admin API) |
| `preview-wrapper/` | Optional Cloudflare Worker for branded, cookie-isolated preview origins |
| `test_unit.py` | Offline behavioral contracts (pytest, no sandbox) |
| `testing_support.py` | Shared public schema contract |
| `test_integration.py` | Integration tests (live sandbox API) |
| `test_deployment.py` | Deployment tests (live OWUI instance via Socket.IO) |
| `AGENTS.md` | Agent/contributor working instructions |
| `docs/` | User-facing docs site ([lathe.tools](https://lathe.tools)) |

## Further reading

- **Users**: [lathe.tools](https://lathe.tools) — what Lathe is, what it can do, recipes
- **Contributors**: [AGENTS.md](AGENTS.md) — architecture, credentials, test procedures

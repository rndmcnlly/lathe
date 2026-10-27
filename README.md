# Lathe

A single-file Open WebUI toolkit that gives any model a coding agent's tool surface: `bash`, `read`, `write`, `edit`, `glob`, `grep`, `view`, `peek`, `poke`, `onboard`, `expose`, `destroy`, executing against per-user cloud sandboxes via [Daytona](https://www.daytona.io/).

**For users**: See [lathe.tools](https://lathe.tools) for what Lathe can do, how to use it, and example workflows.

**Design**: [A legible sandbox interface](DESIGN.md) explains the human-facing tool story and the boundary between Lathe, Open WebUI, and optional sandbox recipes.

## What it does to your instance

Lathe registers fourteen tools that models can call in [Native function calling mode](https://docs.openwebui.com/features/extensibility/plugin/tools/). When a user's model calls a tool, Lathe creates, starts, or resumes a cloud sandbox VM via the Daytona control plane and toolbox APIs. All sandbox operations go outbound from your OWUI server.

Lathe does not import `open_webui.*`, use OWUI file storage, or change stored model, prompt, user, or instance configuration. Its tool descriptions and results include operational instructions, project context, and background notices that influence the calling model.

## Security and trust model

- **Per-user sandbox isolation** — Each OWUI user gets exactly one sandbox, identified by their email address. Users cannot access each other's sandboxes.
- **Deployment label scoping** — Sandboxes are tagged with a `deployment_label` (e.g. `chat.example.com`), so multiple OWUI instances sharing a Daytona account do not collide.
- **User secrets** — The `env_vars` UserValve is a password field (masked in UI), but its values are deliberately entrusted to the model through every `bash` command it can invoke. The model can read command environments and should be treated as able to disclose or misuse these credentials. Use narrowly scoped, revocable credentials only.
- **Destroy confirmation guard** — The `destroy` tool requires confirmation through Open WebUI's interactive confirmation dialog.
- **Model-facing guidance, not configuration changes**: Tool outputs contain prescriptive guidance and discovered AGENTS.md/skill context. Treat these as model-visible instructions, not merely data.

## Requirements

1. **Daytona account** with an API key ([daytona.io](https://www.daytona.io/))
2. **Open WebUI ≥ 0.12.0** with Native function calling mode enabled
3. Models that support tool/function calling (`view()` and `peek` screenshots additionally need a vision-capable model)

The toolkit requires Open WebUI 0.12.0 or newer, including its native conversation orchestration and image-history support for `view()`.

This floor identifies the oldest release with the required known behavior, not a promise that every OWUI patch release is tested. Compatibility is checked against the OWUI instance configured for the deployment suite.

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

Open WebUI installs the source's declared Python requirements on its host,
including Pydantic 2 and PyYAML. Skill discovery needs no sandbox package
installation or public network access.

## Updating

Open WebUI stores the uploaded copy of `lathe.py`; it does not track this repository or update the toolkit automatically. Check the installed version with `lathe(manpage="version")`, or inspect the `version:` field in the installed tool source's frontmatter.

To apply updates, review and download the current `lathe.py` from the `main` branch, then replace the tool source in Open WebUI. API installations can use `/api/v1/tools/id/lathe/update` with the same payload shape as installation. Lathe has no built-in update or notification mechanism. See the [security policy](SECURITY.md#supported-versions) for support and security-fix details.

## Valves (admin configuration)

`onboarding_instructions` is an optional, empty-by-default string for local
resources, conventions, and short recipes. It is labeled as administrator
guidance in the first sandbox-tool orientation per chat and is also available
in the overview and services manuals. Image results remain images; pending
orientation is delivered with the next text result. This is agent-visible
guidance, not a place for secrets or an enforced policy.

`handbook_url` is an optional reference, also empty by default. For deployments
whose sandbox egress permits public application downloads, point it to the
Lathe Handbook at <https://github.com/rndmcnlly/lathe/blob/main/handbook/README.md>. An intranet reference or test
Markdown page works too. Lathe only displays the URL: the agent chooses how to
read it, and handles unavailable resources using local knowledge and skills.
Short customizations belong in `onboarding_instructions`; a separate handbook
is useful for substantial guidance. Neither valve causes automatic fetching.

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
accepts works here except reserved identity/storage fields. The keys `name`,
`labels`, and `volumes` are rejected: Lathe owns per-user lookup and does not
provision extra volumes. Ordinary workspace files survive stop/archive, but not
sandbox destruction. Export important work or commit it to a remote repository.

### HTTP preview access and wrapping

> [!WARNING]
> Without a preview wrapper, every HTTP URL returned by `expose()` is a bearer
> credential: anyone who sees or copies it can access the service until it
> expires or stops working. A raw URL exposed in chat, a screen share, or a
> recording can disclose an intentionally public custom terminal service or a writable
> file browser. After several months of personal and institutional use, this has
> been Lathe's most significant recurring security sharp edge.

Every `expose()` call requires the agent to choose `access="public"` or
`access="private"`. Public requests without upstream headers try the configured wrapper first, then fall
back to Daytona's direct signed bearer URL if wrapping is unavailable, refused,
or malformed. Private requests require successful owner-authenticated wrapper
registration and never downgrade to public. The model-facing contract tells
the agent to choose private unless the user specifically requests public/world
access; convenience or wrapper failure is not permission to disclose a service.

Direct signed URLs can be appropriate for intentionally public, personal, or
otherwise low-risk services. Configure `preview_wrapper_url` and
`preview_wrapper_key` to add a distinct branded origin, cookie isolation, and,
when the wrapper supports it, browser authentication. Wrapping applies to
running HTTP services before a URL reaches the model or user. Start the service
through `bash`, then call `expose(target="http:8080", access="private")` (substitute
its port). `expose(target="vnc", access=...)` opens the shared desktop
described in [Shared desktop](#shared-desktop-computer-use). Lathe does not
install applications through `expose` or provide SSH access.

The optional [agent handbook](handbook/README.md) describes static sites, dufs
file browsers, ttyd terminals, and code-server editors. These are reference
guides, not managed application tools or core requirements. On first setup, ask
the agent to consult the handbook when available, install through `bash`, verify
the process and HTTP readiness, and expose privately. After a successful setup,
the agent can offer to save the working procedure as a skill for later chats.
Lathe-only skills live at `~/.local/share/lathe/skills/<name>/SKILL.md` and are
discovered by `onboard`; portable knowledge can use `~/.agents/skills/` or a
project's `.agents/skills/`. Saving requires user approval, not automatic
overwriting or regeneration. Consult `lathe(manpage="services")` for generic
service operations and skill cultivation. Where public web access is restricted,
the core can use dependencies and applications from intranet sources the sandbox
can reach. Daytona's hosted control plane still requires connectivity.
A browser editor's initial folder is not confinement:
its terminal can access the full sandbox. Keep editors, terminals, and writable
file browsers private unless the user explicitly authorizes public/world access.

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
`__user__` context. The model can select an exposure target, port, desired
access level, optional application headers, and untrusted display tag, but it cannot supply the owner
identity or substitute an arbitrary upstream URL. For private access, the generic identity invariant is email-based:
the wrapper authenticates the browser through OAuth/OIDC and matches the
provider-verified email claim against the owner email supplied at registration.
This integration can cross two administrative authorities. The Lathe/OWUI admin
chooses a wrapper endpoint and installation credential. The preview-wrapper
admin authorizes that Lathe installation and independently controls identity
mapping, destination policy, public hostname templates, replacement behavior,
revocation, retention, and browser-login policy. These may be the same person,
but the protocol does not assume that they are. The model can suggest `tag`; it
cannot choose the hostname template or which trusted identity fields it exposes.

#### Registration contract

The authoritative wire protocol, client validation, and header forwarding
contract are in [preview-wrapper/CONTRACT.md](preview-wrapper/CONTRACT.md);
identity requirements for the reference Worker are in
[preview-wrapper/README.md](preview-wrapper/README.md#identity-compatibility).
Independent wrappers must implement that contract, using their own deployment
and storage mechanisms. Valid registration JSON does not prove that a wrapper
enforces its browser-authentication claims: the configured wrapper is trusted.

`upstream_headers` can attach fixed application credentials to HTTP/WebSocket
requests, independently of public/private access. They require a compatible
wrapper and never fall back to a direct URL. Values remain in OWUI tool
arguments and are entrusted to the application; public visitors can exercise
them. They neither establish visitor identity nor prevent direct application
bypass. Browser headers already pass through except reserved ones, so injection is
not needed for forwarding. See the [contract](preview-wrapper/CONTRACT.md#4-upstream_headers-injection)
for limits, acknowledgement, storage, and rotation requirements.

#### Lifetime and browser behavior

`preview_expiry_seconds` controls the upstream signed URL (default and maximum:
24 hours). The wrapper chooses its own registration lifetime; Lathe reports the
two clocks separately. Sandbox sleep, service failure, or explicit shutdown can
end availability sooner. Calling `expose()` again registers the running service and
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

### Shared desktop (computer use)

Daytona sandboxes include an Xfce desktop on a virtual display, with a noVNC
viewer on port 6080. Lathe gives the model and the user two ways into that one
screen:

- The model calls `peek` to read it (screenshots, window lists, pointer position,
  accessibility tree, process status) and `poke` to change it (start/stop the
  desktop, mouse and keyboard input, accessibility actions). Each tool takes a
  `command` name and an `args` object. Lathe checks both against a fixed command
  list before contacting the sandbox, so `peek` cannot send input and neither
  tool is a general proxy to Daytona's API.
- The user opens the desktop in a browser tab after the model calls
  `expose(target="vnc", access="private")`. Lathe picks the port and returns the
  noVNC page URL. `expose` does not start the desktop; the model starts it with
  `poke(command="start")` first.

The intended use is a browser task that needs a human login partway through.
The model starts the desktop and browser, navigates to the login page, sends
the user the private desktop link, and stops sending input or taking
screenshots. The user signs in, then replies in chat; the model takes a fresh
screenshot and continues. The password never needs to enter the chat.

What administrators should know:

- **No new valves or wrapper changes.** Upgrading from 1.1 adds `peek` and
  `poke` to the existing toolkit. Desktop links use the same preview wrapper
  and `preview_wrapper_*` valves as other `expose` targets.
- **Private is advice, not enforcement.** As for shells and editors, the model is
  told to use private access, but `access="public"` still works, including the
  direct Daytona bearer URL fallback for deployments without the wrapper. A
  public desktop URL lets anyone who has it control the desktop and every
  account logged in there. Failed private requests never fall back to public.
- **A login is shared with the agent.** Keeping the password out of chat does
  not keep the session out of reach: anything running in the sandbox, including
  the model's `bash` commands, can use an account the user logs into there.
- **Screenshots are stored in chat history.** `peek` screenshots are returned as
  images to the model, so OWUI keeps them with the conversation. They require a
  vision-capable model; text-only models can still use the other commands.
- **Handoff is a convention, not a lock.** Nothing stops the model, another chat,
  or a subagent from sending input while the user is working. Lathe also does
  not retry input, and a successful `poke` means the input was delivered, not
  that the application did what was intended.
- **Snapshots and resources.** Daytona's default snapshot provides the desktop
  packages; a custom `sandbox_create_overrides` snapshot must include them.
  BayLeaf measured the desktop stack idling near 450 MB of memory, so very
  small sandboxes may struggle. Browsers are ordinary sandbox software the
  model installs or launches with `bash`.

The model-facing command reference is `lathe(manpage="computer")`.
[COMPUTER-USE.md](COMPUTER-USE.md) records the design and live qualification.

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
| `glob(pattern, max_lines)` | List workspace files using one native ripgrep glob |
| `grep(pattern, files, max_lines)` | Search workspace text using ripgrep regex and one native file glob |
| `view(path)` | Load an image into the model's visual context (PNG/JPEG/GIF/WebP, ≤ 4 MB, content-sniffed) |
| `peek(command, args)` | Observe Daytona's shared desktop: screenshots, display, accessibility and diagnostics |
| `poke(command, args)` | Send desktop input or change its lifecycle; coordinate human login explicitly |
| `interpret(code, timeout)` | Run Python in a conversation-scoped persistent interpreter |
| `expose(target, access)` | Publish a running `http:<port>` service or the `vnc` desktop with explicit `public` or `private` access |
| `destroy()` | Permanently delete the sandbox after interactive confirmation |

Search scopes are workspace-relative, with ripgrep's brace, `!`, ignore, and
hidden-file rules. There is no comma-separated glob language or exact-total
guarantee. Use `bash` for absolute search scopes or additional ripgrep flags.
Open WebUI owns conversation continuation and compaction; Lathe does not provide
a separate handoff tool.

## Testing

For hands-on design iteration in a retained Docker OWUI installation, use
[the interactive trial playbook](INTERACTIVE-TRIAL.md): development handbook
serving, first-use and fresh-chat skill journeys, preview TLS, and cleanup.

For handbook-dependent agent trials, serve the **current development checkout**,
not the public deployment's handbook. See the development-handbook playbook in
`CI-MONITOR.md`. A plain Markdown HTTP response suffices; GitHub need not be mocked.

```bash
uv run pytest                               # offline, no credentials
uv run python test_integration.py            # needs DAYTONA_API_KEY in .env
uv run python test_deployment.py [--verbose] # also needs OWUI_URL, OWUI_TOKEN, OWUI_MODEL
```

The tiers cover different boundaries:

- The offline suite checks local behavior without provisioning resources. Use `uv run pytest -k interpreter` for a focused selection; `uv run python test_unit.py` is also supported.
- `test_integration.py` verifies the live Daytona API using an isolated test identity and deletes test sandboxes on exit. Cleanup failure makes the run fail.
- `test_deployment.py` is a disposable end-to-end OWUI smoke test for local source, loaded schema, and model-mediated dispatch, including `view()`. It uses isolated staging identities and cleans them up on exit.

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
test registration. Both focused and full runs stage the exact local source,
including dependency requirements. The focused run does not qualify the other tools.

## Files

| File | Purpose |
|------|---------|
| `lathe.py` | The OWUI toolkit (single file, deployed via OWUI admin API) |
| `handbook/` | Mutable agent reference guides for optional applications |
| `DESIGN.md` | Human-facing tool story and responsibility boundaries |
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

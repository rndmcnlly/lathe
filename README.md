# Lathe

A single-file Open WebUI toolkit that gives any model a coding agent's tool surface — `bash`, `read`, `write`, `edit`, `attach`, `ingest`, `onboard`, `ssh`, `preview`, `destroy` — executing against per-user cloud sandboxes via [Daytona](https://www.daytona.io/).

**For users**: See [lathe.tools](https://lathe.tools) for what Lathe can do, how to use it, and example workflows.

## What it does to your instance

Lathe registers ten tools that models can call in [Native function calling mode](https://docs.openwebui.com/features/extensibility/plugin/tools/). When a user's model calls a tool:

1. **Outbound API calls to Daytona** — Lathe creates, starts, or resumes a cloud sandbox VM via the Daytona control plane and toolbox APIs. All sandbox operations go outbound from your OWUI server.
2. **`__event_call__` JS injection** — The `ingest` tool uses OWUI's `__event_call__` mechanism to render a file-upload modal in the user's browser tab. This is the same pattern used by other OWUI toolkits (e.g. [picker-agent](https://github.com/rndmcnlly/picker-agent)).
3. **OWUI file storage** — Two tools use OWUI's file storage layer:
   - `ingest` briefly relays uploaded files through the Files API (to work around `__event_call__` size limits), then deletes them after transfer to the sandbox.
   - `attach` permanently stores media and binary files (audio, video, archives, etc.) in OWUI file storage so large payloads don't bloat the chat database. These files are owned by the user and persist until manually removed. Text and image files are inlined directly and do not touch file storage.

No other OWUI internals are touched. The toolkit does not modify models, prompts, users, or other configuration.

## Security and trust model

- **Per-user sandbox isolation** — Each OWUI user gets exactly one sandbox, identified by their email address. Users cannot access each other's sandboxes.
- **Deployment label scoping** — Sandboxes are tagged with a `deployment_label` (e.g. `chat.example.com`), so multiple OWUI instances sharing a Daytona account do not collide.
- **User secrets** — The `env_vars` UserValve is a password field (masked in UI). Values are injected into shell commands but never shown to the model. Pair with system prompts that reference variable names without values.
- **Destroy confirmation guard** — The `destroy` tool requires an explicit `confirm=true` parameter to prevent accidental deletion.
- **No model prompt modification** — Lathe does not inject system prompts or alter model behavior. It only exposes tools.

## Requirements

1. **Daytona account** with an API key ([daytona.io](https://www.daytona.io/))
2. **Open WebUI** with Native function calling mode enabled
3. Models that support tool/function calling

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
| `deployment_label` | *(empty, must configure)* | Label key for sandbox tagging (e.g. `chat.example.com`) |
| `auto_stop_minutes` | `15` | Idle timeout before sandbox stops |
| `auto_archive_minutes` | `60` | Minutes after stop before sandbox archives |
| `sandbox_language` | `python` | Default sandbox runtime |

## UserValves (per-user configuration)

| Valve | Default | Purpose |
|-------|---------|---------|
| `env_vars` | `{}` *(password field)* | JSON object of environment variables injected into every `bash` command. e.g. `{"GITHUB_TOKEN":"ghp_...","OPENAI_API_KEY":"sk-..."}` |

## Tools reference

| Tool | Purpose |
|------|---------|
| `onboard(path)` | Load project context (AGENTS.md + skill catalog) |
| `bash(command, workdir)` | Execute shell commands (2-min timeout, output truncated to last 2000 lines / 50 KB) |
| `read(path, offset, limit)` | Read file with line numbers |
| `write(path, content)` | Write/create file (auto-creates parent dirs) |
| `edit(path, old_string, new_string, replace_all)` | Exact string replacement |
| `attach(path)` | Show file to user without consuming model context |
| `ingest(prompt)` | Get a file or pasted text from the user into the sandbox |
| `ssh(expires_in_minutes)` | Generate a time-limited SSH access command |
| `preview(port)` | Get a live URL for a running web server in the sandbox |
| `destroy(confirm)` | Permanently delete the sandbox (requires `confirm=true`) |

## Testing

```bash
uv run --script test_harness.py              # run everything
uv run --script test_harness.py unit         # unit tests only (no sandbox, ~0.1s)
uv run --script test_harness.py bash env_vars  # specific groups only
uv run --script test_harness.py --list       # list available groups
```

Requires `DAYTONA_API_KEY` in a `.env` file (not needed for `unit`).

## Files

| File | Purpose |
|------|---------|
| `lathe.py` | The OWUI toolkit (single file, deployed via OWUI admin API) |
| `test_harness.py` | Test suite (`uv run --script test_harness.py`) |
| `AGENTS.md` | Agent/contributor working instructions |
| `docs/` | User-facing docs site ([lathe.tools](https://lathe.tools)) |

## Further reading

- **Users**: [lathe.tools](https://lathe.tools) — what Lathe is, what it can do, recipes
- **Contributors**: [AGENTS.md](AGENTS.md) — architecture, credentials, test procedures

# Lathe

A single-file Open WebUI toolkit that gives any model a coding agent's tool surface — `bash`, `read`, `write`, `edit`, `glob`, `grep`, `delegate`, `onboard`, `expose`, `destroy` — executing against per-user cloud sandboxes via [Daytona](https://www.daytona.io/).

**For users**: See [lathe.tools](https://lathe.tools) for what Lathe can do, how to use it, and example workflows.

## What it does to your instance

Lathe registers eleven tools that models can call in [Native function calling mode](https://docs.openwebui.com/features/extensibility/plugin/tools/). When a user's model calls a tool, Lathe creates, starts, or resumes a cloud sandbox VM via the Daytona control plane and toolbox APIs. All sandbox operations go outbound from your OWUI server.

No OWUI internals are touched. The toolkit does not import `open_webui.*`, does not use OWUI file storage, and does not modify models, prompts, users, or other configuration. Its only runtime dependency is `httpx`.

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
| `foreground_timeout_seconds` | `30` | Seconds to wait for a bash command before auto-backgrounding (1–300) |

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
| `read(path, offset, limit)` | Read file with line numbers |
| `write(path, content)` | Write/create file (auto-creates parent dirs) |
| `edit(path, old_string, new_string, replace_all)` | Exact string replacement |
| `glob(pattern, max_lines)` | Search for files by glob pattern (hierarchical output, collapsed directories) |
| `grep(pattern, files, max_lines)` | Search file contents by regex (grouped by file with line numbers) |
| `delegate(task, context_files, max_steps)` | Dispatch a sub-agent to perform a multi-step task autonomously |
| `expose(target)` | Expose a sandbox service — `"http:5000"` for a public HTTPS URL, `"ssh"` for a time-limited SSH command |
| `destroy(confirm)` | Permanently delete the sandbox (requires `confirm=true`) |

## Testing

```bash
uv run python test_unit.py                   # no sandbox needed (~0.7s)
uv run python test_integration.py            # needs DAYTONA_API_KEY in .env
uv run python test_deployment.py [--verbose] # needs OWUI_URL, OWUI_TOKEN, OWUI_MODEL in .env
```

## Files

| File | Purpose |
|------|---------|
| `lathe.py` | The OWUI toolkit (single file, deployed via OWUI admin API) |
| `test_unit.py` | Unit tests (pure-Python helpers, no sandbox) |
| `test_integration.py` | Integration tests (live sandbox API) |
| `test_deployment.py` | Deployment tests (live OWUI instance via Socket.IO) |
| `AGENTS.md` | Agent/contributor working instructions |
| `docs/` | User-facing docs site ([lathe.tools](https://lathe.tools)) |

## Further reading

- **Users**: [lathe.tools](https://lathe.tools) — what Lathe is, what it can do, recipes
- **Contributors**: [AGENTS.md](AGENTS.md) — architecture, credentials, test procedures

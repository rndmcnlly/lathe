# daytona-owui-agent-harness

An Open WebUI toolkit that gives any OWUI-compatible model a coding agent's tool surface — `bash`, `read`, `write`, `edit`, `onboard` — executing against per-user [Daytona](https://daytona.io/) sandboxes with transparent lifecycle management.

## Design

### Core idea

The model calls tools that look like a local coding agent (inspired by Pi / Claude Code). Under the hood, every call executes against a Daytona sandbox VM. The sandbox lifecycle is fully hidden from the model: the first tool call lazily creates, unarchives, or restarts the sandbox as needed. Nothing about the VM is scoped to the chat session — files persist across conversations for the same user.

### Sandbox identity

- **One sandbox per OWUI user**, identified by email address.
- **Label**: `{deployment_label}:{email}` (e.g. `chat.example.com:user@example.com`) for API lookup via `GET /sandbox?label=...`.
- **Name**: `{deployment_label}/{email}` for human readability on the Daytona dashboard.
- Sandboxes are never deleted (`autoDeleteInterval: -1`). They stop after idle (default 15 min) and archive after stop (default 60 min).

### Lifecycle: `_ensure_sandbox()`

Called at the top of every tool method. Transparent to the model.

```
GET /sandbox?label={deployment_label}:{email}
  |
  +-- not found --> POST /sandbox (create, wait for started)
  +-- started   --> readiness probe, return
  +-- stopped   --> POST /start, poll until started
  +-- archived  --> POST /start, poll until started (slower)
  +-- error     --> POST /recover + /start (if recoverable)
```

After the control plane reports `state=started`, a readiness probe (`echo ready`) polls the toolbox API until the daemon is responsive. On first start, also creates `/home/daytona/workspace` via `mkdir -p`.

### Tools

| Tool | Purpose | Implementation |
|------|---------|----------------|
| `onboard(path)` | Load project context (AGENTS.md + skill catalog) | Uploads+executes a shell script that reads AGENTS.md and parses SKILL.md YAML frontmatter from `.agents/skills/` |
| `bash(command, workdir)` | Execute shell commands | Uploads command as a temp script and runs `bash /tmp/_cmd.sh` with `set -e -o pipefail` and non-interactive env vars. 2-min timeout. |
| `read(path, offset, limit)` | Read file with line numbers | `GET /files/download`, client-side line slicing |
| `write(path, content)` | Write/create file | `mkdir -p` parent via exec, then `POST /files/upload` |
| `edit(path, old_string, new_string, replace_all)` | Exact string replacement | Download, string replace, re-upload. Rejects ambiguous matches unless `replace_all=True`. |

### Key implementation details

- **Command execution**: Daytona's `/process/execute` does argv splitting, not shell invocation. To avoid quoting/escaping issues, the `bash` tool writes the command verbatim to a temp script (`/tmp/_cmd.sh`) and executes `bash /tmp/_cmd.sh`. The model's command reaches bash with zero transformations.
- **Error handling**: Scripts run with `set -e -o pipefail` so failures abort immediately and pipe errors propagate. Models can override with `|| true` when intentional.
- **Non-interactive environment**: Every bash command runs with `DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0 PIP_NO_INPUT=1 NPM_CONFIG_YES=true CI=true` exported to prevent blocking on interactive prompts.
- **Helper visibility**: OWUI exposes all non-`__dunder__` methods on `class Tools` as callable tools. All helpers are module-level functions to stay invisible to tool discovery.
- **Onboard script**: The `onboard` tool uploads a shell script to `/tmp/_onboard.sh` and executes it, using the same tempfile pattern as `bash`. The script parses YAML frontmatter line-by-line in bash.

### Agent Skills support

The `onboard` tool follows the [Agent Skills](https://agentskills.io/) progressive disclosure pattern:

1. **Tier 1 (catalog)**: `onboard()` returns skill `name` + `description` from YAML frontmatter (~50-100 tokens each)
2. **Tier 2 (instructions)**: Model calls `read()` on the `SKILL.md` path to load full instructions
3. **Tier 3 (resources)**: Model uses `read()` or `bash()` to access `scripts/`, `references/`, etc.

Skills are discovered at `{project_path}/.agents/skills/*/SKILL.md`.

### Valves (admin configuration)

| Valve | Default | Purpose |
|-------|---------|---------|
| `daytona_api_key` | (empty, password field) | Daytona API key |
| `daytona_api_url` | `https://app.daytona.io/api` | Control plane URL |
| `daytona_proxy_url` | `https://proxy.app.daytona.io/toolbox` | Toolbox proxy URL |
| `deployment_label` | (empty, must configure) | Label key for sandbox tagging (e.g. `chat.example.com`) |
| `auto_stop_minutes` | `15` | Idle timeout |
| `auto_archive_minutes` | `60` | Minutes after stop before archive |
| `sandbox_language` | `python` | Default runtime |

### Daytona API surface used

- **Control plane** (`app.daytona.io/api`): `GET /sandbox`, `POST /sandbox`, `POST /sandbox/{id}/start`, `POST /sandbox/{id}/stop`, `POST /sandbox/{id}/recover`
- **Toolbox** (`proxy.app.daytona.io/toolbox/{id}`): `POST /process/execute`, `GET /files/download`, `POST /files/upload`

## Files

- `daytona_sandbox.py` — The OWUI toolkit (single file, deployed via OWUI admin API)
- `test_harness.py` — Integration test suite against live Daytona API (`uv run --script test_harness.py`)

## Deployment

Deploy as tool ID `daytona_sandbox` on any Open WebUI instance:

```python
# Read source, POST to /api/v1/tools/id/daytona_sandbox/update
# Then POST valves to /api/v1/tools/id/daytona_sandbox/valves/update
```

## Testing

```bash
uv run --script test_harness.py
```

Requires `DAYTONA_API_KEY` in a `.env` file or exported as an environment variable. Creates a sandbox labeled `test-harness`, runs 40 tests across all 5 tools, stops the sandbox on completion.

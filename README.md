# Lathe

An Open WebUI toolkit that gives any OWUI-compatible model a coding agent's tool surface — `bash`, `read`, `write`, `edit`, `attach`, `ingest`, `onboard` — executing against per-user sandbox VMs with transparent lifecycle management.

## Design

### Core idea

The model calls tools that look like a local coding agent (inspired by [Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) by Mario Zechner, aka [shittycodingagent.ai](https://shittycodingagent.ai)). Under the hood, every call executes against a sandbox VM. The sandbox lifecycle is fully hidden from the model: the first tool call lazily creates, unarchives, or restarts the sandbox as needed. Nothing about the VM is scoped to the chat session — files persist across conversations for the same user.

### Sandbox identity

- **One sandbox per OWUI user**, identified by email address.
- **Label**: `{deployment_label}:{email}` (e.g. `chat.example.com:user@example.com`) for API lookup via `GET /sandbox?label=...`.
- **Name**: `{deployment_label}/{email}` for human readability on the provider dashboard.
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
| `bash(command, workdir)` | Execute shell commands | Uploads command as a temp script and runs `bash /tmp/_cmd.sh` with `set -e -o pipefail` and non-interactive env vars. 2-min timeout. Output truncated to last 2000 lines / 50 KB; full output spilled to sandbox temp file if truncated. |
| `read(path, offset, limit)` | Read file with line numbers | `GET /files/download`, client-side line slicing |
| `write(path, content)` | Write/create file | `mkdir -p` parent via exec, then `POST /files/upload` |
| `edit(path, old_string, new_string, replace_all)` | Exact string replacement | Download, string replace, re-upload. Rejects ambiguous matches unless `replace_all=True`. |
| `attach(path)` | Show file to user without consuming model context | Classifies file as text/image/binary, renders appropriate viewer as inline iframe. See [Rich attachments](#rich-attachments) below. |
| `ingest(prompt)` | Get a file from the user into the sandbox | Pops a file picker modal via `__event_call__`, user selects a local file, bytes go directly to sandbox. See [Ingest](#ingest) below. |

### Key implementation details

- **Command execution**: The sandbox provider's `/process/execute` does argv splitting, not shell invocation. To avoid quoting/escaping issues, the `bash` tool writes the command verbatim to a temp script (`/tmp/_cmd.sh`) and executes `bash /tmp/_cmd.sh`. The model's command reaches bash with zero transformations.
- **Output truncation**: Inspired by [Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent)'s bash tool design. Output is truncated to the **last** 2000 lines or 50 KB (whichever limit is hit first), keeping the tail where errors and final results live. If truncated, the full output is uploaded to a temp file in the sandbox (`/tmp/_bash_output_{hash}.log`) and the model sees a notice like `[Showing lines 1842-2000 of 5400. Full output: /tmp/_bash_output_abc12345.log]`. The model can then use `read()` or `bash("head -n 100 /tmp/...")` to inspect earlier parts without re-running the command. This prevents verbose commands like `pip install` or `find /` from flooding the context window.
- **Error handling**: Scripts run with `set -e -o pipefail` so failures abort immediately and pipe errors propagate. Models can override with `|| true` when intentional.
- **Non-interactive environment**: Every bash command runs with `DEBIAN_FRONTEND=noninteractive GIT_TERMINAL_PROMPT=0 PIP_NO_INPUT=1 NPM_CONFIG_YES=true CI=true` exported to prevent blocking on interactive prompts.
- **Helper visibility**: OWUI exposes all non-`__dunder__` methods on `class Tools` as callable tools. All helpers are module-level functions to stay invisible to tool discovery.
- **Onboard script**: The `onboard` tool uploads a shell script to `/tmp/_onboard.sh` and executes it, using the same tempfile pattern as `bash`. The script parses YAML frontmatter line-by-line in bash.
- **Browser-side execution**: The `ingest` tool uses OWUI's `__event_call__` mechanism to inject and run JavaScript in the user's browser tab. This is the same pattern used by the [picker-agent](https://github.com/rndmcnlly/picker-agent) toolkits. The JS renders a modal, the user interacts with it, and structured data flows back to Python.

### Agent Skills support

The `onboard` tool follows the [Agent Skills](https://agentskills.io/) progressive disclosure pattern:

1. **Tier 1 (catalog)**: `onboard()` returns skill `name` + `description` from YAML frontmatter (~50-100 tokens each)
2. **Tier 2 (instructions)**: Model calls `read()` on the `SKILL.md` path to load full instructions
3. **Tier 3 (resources)**: Model uses `read()` or `bash()` to access `scripts/`, `references/`, etc.

Skills are discovered at `{project_path}/.agents/skills/*/SKILL.md`.

### Rich attachments

The `attach` tool solves a fundamental problem with chat-based coding agents: the model shouldn't have to burn context tokens just to show the user a file. When the model calls `attach(path)`, the tool:

1. Fetches the file as raw bytes from the sandbox
2. Classifies it as **text**, **image**, or **binary** based on extension (with a UTF-8 decode fallback heuristic)
3. Renders the appropriate viewer as a self-contained HTML page
4. Returns an OWUI `HTMLResponse` with `Content-Disposition: inline`, which OWUI renders as an inline iframe at the tool call site

**What the model sees**: A short JSON confirmation (`"status": "success", "code": "ui_component"`). The file content never enters the context window. The model can use `read()` separately if it needs to inspect the content itself.

**Three rendering modes**:

| Mode | Detected by | What the user sees |
|------|-------------|--------------------|
| **Text** (default) | UTF-8 decodable, not an image/binary extension | Dark-themed code viewer (Catppuccin Mocha) with Pygments syntax highlighting, line numbers, Copy + Save buttons |
| **Image** | Extension in `{png, jpg, jpeg, gif, svg, webp, bmp, ico, avif}` | Inline `<img>` rendered from a base64 data URI, with Save button |
| **Binary** | Known binary extension (`zip`, `tar`, `pdf`, `whl`, `exe`, etc.) or UTF-8 decode failure | Download card showing filename, size, and file type. **Save** button included for files under 10 MB; larger files show metadata only |

This uses OWUI's [Rich UI Embedding](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui) feature and works in Native function calling mode.

### Ingest

The `ingest` tool is the inverse of `attach`: instead of pushing a file from the sandbox to the user, it pulls a file from the user's local machine into the sandbox. The file bytes never enter the model's context window. They transit through OWUI's file storage for a few seconds (as a relay) before being pushed to the sandbox and deleted from OWUI.

When the model calls `ingest(prompt)`, the tool:

1. Ensures the sandbox is running (same `_ensure_sandbox()` lifecycle as all other tools)
2. Injects JavaScript via `__event_call__` that renders a Catppuccin Mocha-themed file picker modal with a progress bar
3. The user picks a local file via `<input type="file">` (works in all browsers, no File System Access API dependency)
4. JavaScript uploads the file to OWUI's Files API (`POST /api/v1/files/`) using XHR (for upload progress), then returns just the file ID through `__event_call__`
5. Python reads the file from OWUI's storage layer (direct in-process import, no HTTP self-call), uploads to the sandbox at `/home/daytona/workspace/{filename}`, and deletes the transient file from OWUI

**Why the OWUI relay?** OWUI's `__event_call__` bridge has a message size limit that prevents returning large payloads (like base64-encoded files) from JavaScript to Python. The workaround routes the bytes through a normal HTTP upload to OWUI's Files API (which has no such limit), then reads them from disk in-process. The file is deleted immediately after transfer.

**What the model sees**: A short confirmation string like `"Uploaded data.csv (42.0 KB) to /home/daytona/workspace/data.csv"`. The model can then use `read()`, `bash()`, etc. to work with the file.

**What the user sees**: A modal overlay with the prompt text (e.g. "Upload your CSV dataset"), a file chooser button, file size validation, a progress bar during upload, and Upload/Cancel buttons.

**Design notes**:
- The `prompt` parameter is optional — a generic message is shown if omitted. The model uses it to tell the user *what* file is needed.
- The destination path is derived from the picked filename, not specified by the model. This keeps the tool surface minimal.
- Files are capped at 25 MB. The modal shows a clear error if the user picks something too large.
- Requires `__event_call__` (OWUI's browser-side JavaScript execution), which means the toolkit must be used in **Native function calling mode**.
- Uses `open_webui.models.files.Files` and `open_webui.storage.provider.Storage` to read the file in-process rather than making HTTP calls to localhost.

### Valves (admin configuration)

| Valve | Default | Purpose |
|-------|---------|---------|
| `daytona_api_key` | (empty, password field) | Sandbox provider API key |
| `daytona_api_url` | `https://app.daytona.io/api` | Control plane URL |
| `daytona_proxy_url` | `https://proxy.app.daytona.io/toolbox` | Toolbox proxy URL |
| `deployment_label` | (empty, must configure) | Label key for sandbox tagging (e.g. `chat.example.com`) |
| `auto_stop_minutes` | `15` | Idle timeout |
| `auto_archive_minutes` | `60` | Minutes after stop before archive |
| `sandbox_language` | `python` | Default runtime |

### Sandbox provider API surface used

- **Control plane** (`app.daytona.io/api`): `GET /sandbox`, `POST /sandbox`, `POST /sandbox/{id}/start`, `POST /sandbox/{id}/stop`, `POST /sandbox/{id}/recover`
- **Toolbox** (`proxy.app.daytona.io/toolbox/{id}`): `POST /process/execute`, `GET /files/download`, `POST /files/upload`

## Files

- `lathe.py` — The OWUI toolkit (single file, deployed via OWUI admin API)
- `test_harness.py` — Integration test suite against live sandbox provider API (`uv run --script test_harness.py`)

## Deployment

Deploy as tool ID `lathe` on any Open WebUI instance:

```python
# Read source, POST to /api/v1/tools/id/lathe/update
# Then POST valves to /api/v1/tools/id/lathe/valves/update
```

## Testing

```bash
uv run --script test_harness.py
```

Requires `DAYTONA_API_KEY` in a `.env` file or exported as an environment variable. Creates a sandbox labeled `test-harness`, runs tests across all server-side tools (including attach modes for text, images, and binary files), stops the sandbox on completion.

### Manual testing: `ingest`

The `ingest` tool requires a live browser with `__event_call__` support and cannot be tested headlessly. Use Native function calling mode and verify:

1. **Modal appears**: Model calls `ingest("Upload a test file")` → Catppuccin-themed modal with prompt text
2. **File selection**: Click "Choose File...", pick a small text file → filename and size shown in green, Upload button appears
3. **Size rejection**: Pick a file > 25 MB → red error text, no Upload button
4. **Cancel**: Click Cancel → model gets "User cancelled"
5. **Upload progress**: Click Upload on a ~1 MB file → progress bar fills, percentage updates, buttons hide during upload
6. **Processing phase**: After progress hits 100%, bar turns green, text says "Processing..."
7. **Sandbox delivery**: Model gets `"Uploaded foo.txt (1.2 KB) to /home/daytona/workspace/foo.txt"` → verify with `bash("ls -la workspace/foo.txt")` or `read("workspace/foo.txt")`
8. **OWUI cleanup**: Check OWUI Files (admin panel or API) → transient file should be deleted
9. **Binary files**: Repeat with a PDF or image → verify `bash("file workspace/test.pdf")` shows correct type

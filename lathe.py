"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (bash, read, write, edit, attach, ingest, onboard, preview) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx
version: 0.5.0
licence: MIT
"""

import asyncio
import base64
import hashlib
import html as html_mod
import io
import json
import textwrap
import time

import httpx
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


# ── module-level helpers (invisible to OWUI tool discovery) ──────────


def _headers(valves) -> dict:
    return {
        "Authorization": f"Bearer {valves.daytona_api_key}",
        "Content-Type": "application/json",
    }


def _api(valves, path: str) -> str:
    return f"{valves.daytona_api_url.rstrip('/')}{path}"


def _toolbox(valves, sandbox_id: str, path: str) -> str:
    return f"{valves.daytona_proxy_url.rstrip('/')}/{sandbox_id}{path}"


async def _emit(emitter, description: str, done: bool = False):
    if emitter:
        await emitter(
            {
                "type": "status",
                "data": {"description": description, "done": done},
            }
        )


async def _tool_guard(emitter, coro):
    """Run *coro*, catch standard tool exceptions, and return an error string."""
    try:
        return await coro
    except RuntimeError as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"
    except httpx.HTTPStatusError as e:
        await _emit(emitter, f"API error: HTTP {e.response.status_code}", done=True)
        return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
    except Exception as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe use in shell scripts."""
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_env_vars(env_vars: str) -> list[tuple[str, str]]:
    """Parse a JSON object string into (key, value) pairs.

    Expects a JSON object mapping string keys to string values,
    e.g. '{"MY_TOKEN":"abc123","FOO":"bar"}'.
    Keys must match [A-Za-z_][A-Za-z0-9_]*; invalid keys are skipped with a warning.
    Returns [] on empty input (not an error).
    Raises ValueError on malformed input so the caller can surface it to the agent.
    """
    import re
    s = env_vars.strip()
    if not s or s == "{}":
        return []
    try:
        mapping = json.loads(s)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"UserValves env_vars is not valid JSON: {exc}. "
            f"Fix the env_vars field in your tool settings (it should look like "
            f'{{\"MY_TOKEN\":\"abc123\"}}) and retry.'
        ) from exc
    if not isinstance(mapping, dict):
        raise ValueError(
            f"UserValves env_vars must be a JSON object, got {type(mapping).__name__}. "
            f'Expected something like {{\"MY_TOKEN\":\"abc123\"}}.'
        )
    pairs: list[tuple[str, str]] = []
    skipped: list[str] = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            skipped.append(repr(key))
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            skipped.append(repr(key))
            continue
        pairs.append((key, value))
    if skipped:
        raise ValueError(
            f"UserValves env_vars contains invalid keys or non-string values: "
            f"{', '.join(skipped)}. Keys must match [A-Za-z_][A-Za-z0-9_]*."
        )
    return pairs


def _get_email(user: dict) -> str:
    email = user.get("email", "")
    if not email:
        raise RuntimeError("No email found for user. Cannot provision sandbox.")
    return email


# ── output truncation (tail-biased, mirrors Pi's design) ────────────

_MAX_LINES = 2000
_MAX_BYTES = 50 * 1024  # 50 KB


def _truncate_tail(text: str) -> tuple[str, bool, dict]:
    """Truncate output keeping the *tail* (where errors and results live).

    Returns (output, was_truncated, metadata).
    Metadata keys when truncated:
      total_lines, total_bytes, shown_start_line, shown_end_line, truncated_by
    """
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)

    if total_lines <= _MAX_LINES and total_bytes <= _MAX_BYTES:
        return text, False, {}

    # Walk backwards, collecting complete lines within both limits
    kept: list[str] = []
    kept_bytes = 0
    truncated_by = "lines"

    for i in range(total_lines - 1, -1, -1):
        line = lines[i]
        line_bytes = len(line.encode("utf-8")) + (1 if kept else 0)  # +1 for \n joiner
        if kept_bytes + line_bytes > _MAX_BYTES:
            truncated_by = "bytes"
            break
        kept.append(line)
        kept_bytes += line_bytes
        if len(kept) >= _MAX_LINES:
            truncated_by = "lines"
            break

    kept.reverse()
    output = "\n".join(kept)
    shown_lines = len(kept)
    start_line = total_lines - shown_lines + 1

    meta = {
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "shown_start_line": start_line,
        "shown_end_line": total_lines,
        "truncated_by": truncated_by,
    }
    return output, True, meta


def _human_size(n: int) -> str:
    """Format byte count as human-readable string."""
    b = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:,.0f} {unit}" if unit == "B" else f"{b:,.1f} {unit}"
        b /= 1024
    return f"{b:,.1f} TB"


def _highlight_code(code: str, path: str) -> str:
    """Syntax-highlight code to HTML spans using Pygments. Falls back to escaped plaintext."""
    try:
        from pygments import highlight
        from pygments.lexers import get_lexer_for_filename, TextLexer
        from pygments.formatters import HtmlFormatter

        try:
            lexer = get_lexer_for_filename(path, stripall=False)
        except Exception:
            lexer = TextLexer()

        # Catppuccin Mocha-inspired color scheme via Pygments style overrides
        formatter = HtmlFormatter(
            nowrap=True,       # no <div>/<pre> wrapper, just inline spans
            noclasses=True,    # use inline styles so no external CSS needed
            style="monokai",   # dark base theme
        )
        return highlight(code, lexer, formatter)
    except ImportError:
        # Pygments not available -- plain escaped text
        return html_mod.escape(code)


# ── shared HTML/CSS/JS constants ─────────────────────────────────────

_BASE_CSS = """\
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
    font-size: 13px;
    background: #1e1e2e;
    color: #cdd6f4;
  }
  .header {
    background: #313244;
    padding: 8px 12px;
    font-size: 12px;
    color: #a6adc8;
    border-bottom: 1px solid #45475a;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .header .filename { color: #89b4fa; font-weight: 600; }
  .header .meta { color: #585b70; margin-left: auto; }
  .header .actions { display: flex; gap: 4px; }
  .header button {
    background: transparent;
    border: 1px solid #45475a;
    color: #a6adc8;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    cursor: pointer;
    font-family: inherit;
  }
  .header button:hover { background: #45475a; color: #cdd6f4; }"""

_REPORT_HEIGHT_JS = """\
    function reportHeight() {
      parent.postMessage({ type: 'iframe:height', height: document.documentElement.scrollHeight }, '*');
    }
    window.addEventListener('load', reportHeight);
    new ResizeObserver(reportHeight).observe(document.body);"""

# ── file classification for attach ───────────────────────────────────

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico", ".avif"}
_BINARY_EXTS = {
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".pdf", ".whl", ".egg",
    ".exe", ".dll", ".so", ".dylib", ".a",
    ".pyc", ".pyo", ".class",
    ".sqlite", ".db",
    ".wasm",
    ".o", ".obj",
    ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mkv", ".mov", ".webm",
}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif",
}
# Max bytes to embed in HTML for binary download (10 MB)
_EMBED_SIZE_CAP = 10 * 1024 * 1024


def _classify_file(path: str, raw: bytes) -> str:
    """Classify a file as 'image', 'binary', or 'text' based on extension and content."""
    ext = ("." + path.rsplit(".", 1)[-1]).lower() if "." in path.rsplit("/", 1)[-1] else ""
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _BINARY_EXTS:
        return "binary"
    # Heuristic: try UTF-8 decode; if it fails, it's binary
    try:
        raw.decode("utf-8")
        return "text"
    except (UnicodeDecodeError, ValueError):
        return "binary"


def _render_image_html(raw: bytes, filename: str, path: str) -> str:
    """Render an image file as an inline <img> with Save button."""
    ext = ("." + path.rsplit(".", 1)[-1]).lower() if "." in path.rsplit("/", 1)[-1] else ""
    mime = _IMAGE_MIME.get(ext, "application/octet-stream")
    n_bytes = len(raw)
    raw_b64 = base64.b64encode(raw).decode("ascii")

    # SVGs can also be rendered directly, but data URI works fine
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{_BASE_CSS}
  .img-wrap {{
    padding: 16px;
    display: flex;
    justify-content: center;
    background: #181825;
  }}
  .img-wrap img {{
    max-width: 100%;
    height: auto;
    border-radius: 4px;
  }}
</style>
</head>
<body>
  <div class="header">
    <span class="filename">{html_mod.escape(filename)}</span>
    <span class="meta">{n_bytes:,} bytes</span>
    <span class="actions">
      <button onclick="saveFile()">Save</button>
    </span>
  </div>
  <div class="img-wrap">
    <img src="data:{mime};base64,{raw_b64}" alt="{html_mod.escape(filename)}">
  </div>
  <script>
    var _b64 = "{raw_b64}";
    var _mime = "{mime}";
    var _fname = "{html_mod.escape(filename)}";
    function saveFile() {{
      var bin = atob(_b64);
      var arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      var blob = new Blob([arr], {{type: _mime}});
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = _fname;
      a.click();
      URL.revokeObjectURL(a.href);
    }}
{_REPORT_HEIGHT_JS}
  </script>
</body>
</html>"""


def _render_binary_html(raw: bytes, filename: str, path: str) -> str:
    """Render a binary file as a download card. Embeds content for Save if under size cap."""
    ext = ("." + path.rsplit(".", 1)[-1]).lower() if "." in path.rsplit("/", 1)[-1] else ""
    n_bytes = len(raw)
    can_embed = n_bytes <= _EMBED_SIZE_CAP

    save_button = ""
    save_script = ""
    if can_embed:
        raw_b64 = base64.b64encode(raw).decode("ascii")
        save_button = '<button onclick="saveFile()">Save</button>'
        save_script = f"""
    var _b64 = "{raw_b64}";
    var _fname = "{html_mod.escape(filename)}";
    function saveFile() {{
      var bin = atob(_b64);
      var arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      var blob = new Blob([arr], {{type: 'application/octet-stream'}});
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = _fname;
      a.click();
      URL.revokeObjectURL(a.href);
    }}"""
    else:
        save_button = '<span class="too-large">Too large to download from viewer</span>'

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
    font-size: 13px;
    background: #1e1e2e;
    color: #cdd6f4;
  }}
  .card {{
    background: #313244;
    margin: 16px;
    border-radius: 8px;
    border: 1px solid #45475a;
    overflow: hidden;
  }}
  .card-body {{
    padding: 20px 16px;
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .icon {{
    font-size: 32px;
    flex-shrink: 0;
    width: 48px;
    height: 48px;
    background: #45475a;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .info {{ flex: 1; }}
  .info .filename {{ color: #89b4fa; font-weight: 600; font-size: 14px; }}
  .info .meta {{ color: #585b70; font-size: 12px; margin-top: 4px; }}
  .actions {{ display: flex; gap: 8px; align-items: center; }}
  .actions button {{
    background: #89b4fa;
    border: none;
    color: #1e1e2e;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
    font-weight: 600;
  }}
  .actions button:hover {{ background: #b4d0fb; }}
  .too-large {{ color: #585b70; font-size: 11px; font-style: italic; }}
</style>
</head>
<body>
  <div class="card">
    <div class="card-body">
      <div class="icon">&#128230;</div>
      <div class="info">
        <div class="filename">{html_mod.escape(filename)}</div>
        <div class="meta">{_human_size(n_bytes)} &middot; {ext.lstrip('.').upper() or 'BIN'} file</div>
      </div>
      <div class="actions">
        {save_button}
      </div>
    </div>
  </div>
  <script>
    {save_script}
{_REPORT_HEIGHT_JS}
  </script>
</body>
</html>"""


async def _wait_for_toolbox(valves, sandbox_id: str, emitter=None):
    """Poll the toolbox API until it responds, then ensure workspace dir exists."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(30):
            try:
                resp = await client.post(
                    _toolbox(valves, sandbox_id, "/process/execute"),
                    headers=_headers(valves),
                    json={"command": "echo ready", "timeout": 5000},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("exitCode") == 0 and "ready" in data.get("result", ""):
                        await client.post(
                            _toolbox(valves, sandbox_id, "/process/execute"),
                            headers=_headers(valves),
                            json={
                                "command": "bash -c 'mkdir -p /home/daytona/workspace'",
                                "timeout": 5000,
                            },
                        )
                        return
            except (httpx.HTTPError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)
            if attempt == 2:
                await _emit(emitter, "Waiting for sandbox to become ready...")
    raise RuntimeError("Sandbox started but toolbox daemon did not become responsive (30s)")


async def _ensure_sandbox(valves, email: str, emitter=None) -> str:
    """Find or create a running sandbox for this user. Returns sandbox_id."""
    if not valves.daytona_api_key:
        raise RuntimeError(
            "Daytona API key not configured. Ask an admin to set it in Tool settings."
        )

    if not valves.deployment_label:
        raise RuntimeError(
            "Deployment label not configured. Ask an admin to set it in Tool settings."
        )

    label_key = valves.deployment_label
    labels_filter = json.dumps({label_key: email})

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Look up existing sandbox by label
        resp = await client.get(
            _api(valves, "/sandbox"),
            params={"labels": labels_filter},
            headers=_headers(valves),
        )
        resp.raise_for_status()
        sandboxes = resp.json() or []

        matches = [s for s in sandboxes if s.get("labels", {}).get(label_key) == email]

        if len(matches) > 1:
            ids = ", ".join(s["id"] for s in matches)
            raise RuntimeError(
                f"Found {len(matches)} sandboxes labelled {label_key}={email} ({ids}). "
                f"Expected at most 1. Please delete the extras in the Daytona dashboard "
                f"and try again."
            )

        sandbox = matches[0] if matches else None

        if sandbox is None:
            # 2. Create new sandbox
            await _emit(emitter, "Creating sandbox...")
            resp = await client.post(
                _api(valves, "/sandbox"),
                headers=_headers(valves),
                json={
                    "language": valves.sandbox_language,
                    "name": f"{label_key}/{email}",
                    "labels": {label_key: email},
                    "autoStopInterval": valves.auto_stop_minutes,
                    "autoArchiveInterval": valves.auto_archive_minutes,
                    "autoDeleteInterval": -1,
                },
            )
            resp.raise_for_status()
            sandbox = resp.json()

        sandbox_id = sandbox["id"]
        state = sandbox.get("state", "unknown")

        # 3. Ensure it's running
        if state == "started":
            await _wait_for_toolbox(valves, sandbox_id, emitter)
            await _emit(emitter, "Sandbox ready", done=True)
            return sandbox_id

        if state in ("stopped", "archived"):
            label = "Restoring sandbox..." if state == "archived" else "Starting sandbox..."
            await _emit(emitter, label)
            resp = await client.post(
                _api(valves, f"/sandbox/{sandbox_id}/start"),
                headers=_headers(valves),
            )
            resp.raise_for_status()

        elif state == "error" and sandbox.get("recoverable"):
            await _emit(emitter, "Recovering sandbox...")
            resp = await client.post(
                _api(valves, f"/sandbox/{sandbox_id}/recover"),
                headers=_headers(valves),
            )
            resp.raise_for_status()
            resp = await client.post(
                _api(valves, f"/sandbox/{sandbox_id}/start"),
                headers=_headers(valves),
            )
            resp.raise_for_status()

        elif state in ("starting", "stopping", "archiving"):
            await _emit(emitter, f"Sandbox is {state}, waiting...")
        else:
            if state == "error":
                raise RuntimeError(
                    f"Sandbox is in non-recoverable error state: {sandbox.get('errorReason', 'unknown')}"
                )

        # 4. Poll until started
        deadline = time.time() + 120
        poll_interval = 1.0
        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            resp = await client.get(
                _api(valves, f"/sandbox/{sandbox_id}"),
                headers=_headers(valves),
            )
            resp.raise_for_status()
            info = resp.json()
            state = info.get("state", "unknown")

            if state == "started":
                await _wait_for_toolbox(valves, sandbox_id, emitter)
                await _emit(emitter, "Sandbox ready", done=True)
                return sandbox_id

            if state == "error":
                raise RuntimeError(
                    f"Sandbox entered error state: {info.get('errorReason', 'unknown')}"
                )

            poll_interval = min(poll_interval * 1.2, 5.0)

        raise RuntimeError("Timed out waiting for sandbox to start (120s)")


# ── Tools class (only public methods are visible to OWUI) ───────────


class Tools:
    class Valves(BaseModel):
        daytona_api_key: str = Field(
            "",
            description="Daytona API key",
            json_schema_extra={"input": {"type": "password"}},
        )
        daytona_api_url: str = Field(
            "https://app.daytona.io/api",
            description="Daytona control plane API URL",
        )
        daytona_proxy_url: str = Field(
            "https://proxy.app.daytona.io/toolbox",
            description="Daytona toolbox proxy URL",
        )
        deployment_label: str = Field(
            "",
            description="Label key used to tag sandboxes for this OWUI deployment (e.g. 'chat.example.com')",
        )
        auto_stop_minutes: int = Field(
            15,
            description="Minutes of idle before sandbox stops (0 = never)",
        )
        auto_archive_minutes: int = Field(
            60,
            description="Minutes after stop before sandbox archives",
        )
        sandbox_language: str = Field(
            "python",
            description="Default language runtime (python, typescript, javascript)",
        )

    class UserValves(BaseModel):
        env_vars: str = Field(
            "{}",
            description=(
                'Environment variables injected into every bash command. '
                'JSON object mapping variable names to values, e.g. {"MY_TOKEN":"abc123","FOO":"bar"}. '
                "Values are shell-quoted before injection and never shown to the model."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def destroy(
        self,
        confirm: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Permanently destroy your sandbox. ALL files and state are lost forever.
        A fresh sandbox will be created automatically on your next tool call.
        Only use this if your sandbox is in an unrecoverable state.
        You must set confirm=true to proceed. Without it, no action is taken.
        :param confirm: Must be set to true to confirm destruction. Defaults to false as a safety measure.
        """
        if not confirm:
            return (
                "Destroy aborted: confirm was not set to true. "
                "Set confirm=true to permanently destroy the sandbox and all its contents."
            )
        try:
            email = _get_email(__user__)
            valves = self.valves

            if not valves.daytona_api_key:
                return "Error: Daytona API key not configured."
            if not valves.deployment_label:
                return "Error: Deployment label not configured."

            label_key = valves.deployment_label
            labels_filter = json.dumps({label_key: email})

            await _emit(__event_emitter__, "Looking up sandbox...")

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _api(valves, "/sandbox"),
                    params={"labels": labels_filter},
                    headers=_headers(valves),
                )
                resp.raise_for_status()
                sandboxes = resp.json() or []
                matches = [
                    s for s in sandboxes
                    if s.get("labels", {}).get(label_key) == email
                ]

                if not matches:
                    await _emit(__event_emitter__, "No sandbox found", done=True)
                    return "No sandbox found. One will be created on your next tool call."

                deleted = []
                for s in matches:
                    sid = s["id"]
                    await _emit(__event_emitter__, f"Destroying sandbox {sid[:12]}...")
                    resp = await client.delete(
                        _api(valves, f"/sandbox/{sid}"),
                        headers=_headers(valves),
                        params={"force": "true"},
                    )
                    resp.raise_for_status()
                    deleted.append(sid)

                # Poll until deletion propagates
                for _ in range(30):
                    await asyncio.sleep(1)
                    resp = await client.get(
                        _api(valves, "/sandbox"),
                        params={"labels": labels_filter},
                        headers=_headers(valves),
                    )
                    remaining = [
                        s for s in (resp.json() or [])
                        if s.get("labels", {}).get(label_key) == email
                    ]
                    if not remaining:
                        break

                await _emit(__event_emitter__, "Sandbox destroyed", done=True)
                ids = ", ".join(d[:12] for d in deleted)
                return (
                    f"Destroyed {len(deleted)} sandbox(es) ({ids}). "
                    f"A fresh sandbox will be created on the next tool call."
                )

        except Exception as exc:
            await _emit(__event_emitter__, "Destroy failed", done=True)
            return f"Error: {exc}"

    async def onboard(
        self,
        path: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Load project context at the start of a conversation.
        Call this tool first when beginning work on a project. It returns the project's
        AGENTS.md (instructions, persona, conventions) and a catalog of available skills
        (name + description only). To load a skill's full instructions later, use read()
        on the SKILL.md path shown in the catalog.
        Fails if the path contains neither an AGENTS.md file nor a .agents/skills/ directory.
        :param path: Absolute path to the project root (e.g. /home/daytona/workspace/myproject).
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, "Loading project context...")

            p = path.rstrip("/")
            script = textwrap.dedent("""\
                P=__PATH__
                found=0
                if [ -f "$P/AGENTS.md" ]; then
                  echo "# AGENTS.md"
                  echo ""
                  cat "$P/AGENTS.md"
                  found=1
                fi
                if [ -d "$P/.agents/skills" ]; then
                  skills=""
                  for d in "$P"/.agents/skills/*/SKILL.md; do
                    [ -f "$d" ] || continue
                    found=1
                    dir_name=$(basename "$(dirname "$d")")
                    fm_name="$dir_name"
                    fm_desc=""
                    in_fm=0
                    while IFS= read -r line; do
                      case "$in_fm" in
                        0) [ "$line" = "---" ] && in_fm=1 ;;
                        1) [ "$line" = "---" ] && break
                           case "$line" in
                             name:*) fm_name="${line#name:}"; fm_name="${fm_name# }" ;;
                             description:*) fm_desc="${line#description:}"; fm_desc="${fm_desc# }" ;;
                           esac ;;
                      esac
                    done < "$d"
                    skills="${skills}- **${fm_name}**: ${fm_desc}\\n  \\`${d}\\`\\n"
                  done
                  if [ -n "$skills" ]; then
                    [ -f "$P/AGENTS.md" ] && echo -e "\\n---\\n"
                    echo "# Available Skills"
                    echo ""
                    echo "Load a skill's full instructions with read(path) when the task matches its description."
                    echo ""
                    echo -e "$skills"
                  fi
                fi
                [ "$found" -eq 0 ] && echo "ERROR_NO_CONTEXT" && exit 1
                exit 0
            """).replace("__PATH__", _shell_quote(p))

            # Write script to temp file and execute it (avoids all quoting issues)
            script_tag = hashlib.sha1(
                (p + str(time.time())).encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            script_path = f"/tmp/_onboard_{script_tag}.sh"
            content_bytes = script.encode("utf-8")
            async with httpx.AsyncClient(timeout=60.0) as client:
                await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": script_path},
                    headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                    files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                )

                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/process/execute"),
                    headers=_headers(self.valves),
                    json={
                        "command": f"bash {script_path}",
                        "cwd": p,
                        "timeout": 30000,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            result = data.get("result", "")
            exit_code = data.get("exitCode", -1)

            if "ERROR_NO_CONTEXT" in result:
                await _emit(__event_emitter__, "No project context found", done=True)
                return (
                    f"Error: No AGENTS.md or .agents/skills/ directory found at {path}. "
                    f"This path must contain at least one of these to use onboard."
                )

            if exit_code != 0:
                await _emit(__event_emitter__, "Error loading context", done=True)
                return f"Error: onboard script failed (exit {exit_code}): {result[:500]}"

            await _emit(__event_emitter__, "Project context loaded", done=True)
            return result if result else "(empty project context)"

        return await _tool_guard(__event_emitter__, _run())

    async def bash(
        self,
        command: str,
        workdir: str = "/home/daytona/workspace",
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Execute a bash command in a persistent Linux sandbox.
        The sandbox and its filesystem persist across conversations for this user.
        Supports pipes, redirects, &&, ||, and all standard bash syntax.
        Commands must be non-interactive (no prompts for input). Use -y flags where needed.
        For long-running servers, background them: nohup cmd > /tmp/out.log 2>&1 & echo $!
        Default working directory is /home/daytona/workspace.
        Output is truncated to the last 2000 lines or 50 KB (whichever limit is hit first).
        If truncated, the full output is saved to a file in /tmp/ and the path is shown.
        Use read() or another bash command to inspect specific parts of that file.
        User-configured environment variables (set via the UserValves env_vars JSON field) are
        automatically injected into every command — reference them by name without exposing values.
        :param command: The bash command to execute.
        :param workdir: Working directory for the command (default: /home/daytona/workspace).
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, "Running command...")

            # Build a script that sets non-interactive env vars then runs the
            # command exactly as provided.  Writing to a temp file avoids all
            # quoting/escaping issues with the Daytona execute API's argv
            # splitting — the command reaches bash with zero transformations.

            # Collect user-supplied env vars from UserValves (never logged).
            # Raises ValueError (caught below) if env_vars is malformed.
            user_valves = __user__.get("valves")
            user_env_lines = ""
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                pairs = _parse_env_vars(raw_env)
                if pairs:
                    user_env_lines = (
                        "# user env vars\n"
                        + "".join(
                            f"export {k}={_shell_quote(v)}\n"
                            for k, v in pairs
                        )
                    )

            script = (
                "#!/usr/bin/env bash\n"
                "set -e -o pipefail\n"
                "export DEBIAN_FRONTEND=noninteractive "
                "GIT_TERMINAL_PROMPT=0 "
                "PIP_NO_INPUT=1 "
                "NPM_CONFIG_YES=true "
                "CI=true\n"
                + user_env_lines
                + command
                + "\n"
            )

            script_tag = hashlib.sha1(
                (command + str(time.time())).encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            script_path = f"/tmp/_cmd_{script_tag}.sh"
            async with httpx.AsyncClient(timeout=300.0) as client:
                # Upload the script
                await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": script_path},
                    headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                    files={"file": ("file", io.BytesIO(script.encode("utf-8")), "application/octet-stream")},
                )

                # Execute it
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/process/execute"),
                    headers=_headers(self.valves),
                    json={
                        "command": f"bash {script_path}",
                        "cwd": workdir,
                        "timeout": 120000,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            exit_code = data.get("exitCode", -1)
            result = data.get("result", "")

            # ── Truncation + spill-to-file ──────────────────────────
            output, was_truncated, meta = _truncate_tail(result)
            spill_path = None

            if was_truncated:
                # Write the full output to a unique temp file in the sandbox
                # so the model can retrieve slices without re-running.
                tag = hashlib.sha1(result[:256].encode("utf-8", errors="replace")).hexdigest()[:8]
                spill_path = f"/tmp/_bash_output_{tag}.log"
                async with httpx.AsyncClient(timeout=120.0) as client:
                    await client.post(
                        _toolbox(self.valves, sandbox_id, "/files/upload"),
                        params={"path": spill_path},
                        headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                        files={
                            "file": (
                                "file",
                                io.BytesIO(result.encode("utf-8")),
                                "application/octet-stream",
                            )
                        },
                    )

            await _emit(__event_emitter__, "Command complete", done=True)

            # ── Format the return value ─────────────────────────────
            if exit_code != 0:
                output = f"Exit code: {exit_code}\n{output}"

            if not output.strip():
                output = "(no output)"

            if was_truncated and spill_path:
                start = meta["shown_start_line"]
                end = meta["shown_end_line"]
                total = meta["total_lines"]
                total_size = _human_size(meta["total_bytes"])
                if meta["truncated_by"] == "lines":
                    notice = (
                        f"\n\n[Showing lines {start}-{end} of {total}. "
                        f"Full output ({total_size}): {spill_path}]"
                    )
                else:
                    notice = (
                        f"\n\n[Showing lines {start}-{end} of {total} "
                        f"({_human_size(_MAX_BYTES)} limit, full output is {total_size}). "
                        f"Full output: {spill_path}]"
                    )
                output += notice

            return output

        return await _tool_guard(__event_emitter__, _run())

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 2000,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Read a file from the persistent Linux sandbox.
        Returns numbered lines. The sandbox filesystem persists across conversations for this user.
        :param path: Absolute path or relative to /home/daytona (e.g. workspace/main.py).
        :param offset: Line number to start from (1-indexed, default 1).
        :param limit: Maximum number of lines to return (default 2000).
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, f"Reading {path}...")

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    _toolbox(self.valves, sandbox_id, "/files/download"),
                    params={"path": path},
                    headers=_headers(self.valves),
                )

                if resp.status_code == 404:
                    await _emit(__event_emitter__, "File not found", done=True)
                    return f"Error: File not found: {path}"

                resp.raise_for_status()
                content = resp.text

            lines = content.split("\n")
            if lines and lines[-1] == "":
                lines = lines[:-1]

            total_lines = len(lines)
            start_idx = max(0, offset - 1)
            end_idx = start_idx + limit
            selected = lines[start_idx:end_idx]

            numbered = "\n".join(
                f"{start_idx + i + 1}: {line}"
                for i, line in enumerate(selected)
            )

            await _emit(__event_emitter__, "Read complete", done=True)

            header = f"File: {path} ({total_lines} lines total)"
            if start_idx > 0 or end_idx < total_lines:
                header += f", showing lines {start_idx + 1}-{min(end_idx, total_lines)}"
            return f"{header}\n{numbered}"

        return await _tool_guard(__event_emitter__, _run())

    async def write(
        self,
        path: str,
        content: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Write a file to the persistent Linux sandbox, creating it if it doesn't exist.
        Parent directories are created automatically. The sandbox filesystem persists across conversations for this user.
        :param path: Absolute path or relative to /home/daytona (e.g. workspace/main.py).
        :param content: The full file content to write.
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, f"Writing {path}...")

            parent = "/".join(path.rstrip("/").split("/")[:-1])
            if parent:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    await client.post(
                        _toolbox(self.valves, sandbox_id, "/process/execute"),
                        headers=_headers(self.valves),
                        json={
                            "command": f'bash -c "mkdir -p {parent}"',
                            "timeout": 5000,
                        },
                    )

            content_bytes = content.encode("utf-8")
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": path},
                    headers={
                        "Authorization": f"Bearer {self.valves.daytona_api_key}",
                    },
                    files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                )
                resp.raise_for_status()

            n_bytes = len(content_bytes)
            n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            await _emit(__event_emitter__, "Write complete", done=True)
            return f"Wrote {n_bytes} bytes ({n_lines} lines) to {path}"

        return await _tool_guard(__event_emitter__, _run())

    async def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Edit a file in the persistent Linux sandbox by replacing exact string matches.
        The sandbox filesystem persists across conversations for this user.
        :param path: Absolute path or relative to /home/daytona (e.g. workspace/main.py).
        :param old_string: The exact text to find and replace. Must match exactly including whitespace and indentation.
        :param new_string: The replacement text.
        :param replace_all: If true, replace all occurrences. If false (default), fail if multiple matches found.
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, f"Editing {path}...")

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    _toolbox(self.valves, sandbox_id, "/files/download"),
                    params={"path": path},
                    headers=_headers(self.valves),
                )

                if resp.status_code == 404:
                    await _emit(__event_emitter__, "File not found", done=True)
                    return f"Error: File not found: {path}"

                resp.raise_for_status()
                content = resp.text

            count = content.count(old_string)

            if count == 0:
                await _emit(__event_emitter__, "No match found", done=True)
                return f"Error: old_string not found in {path}"

            if count > 1 and not replace_all:
                await _emit(__event_emitter__, "Multiple matches", done=True)
                return (
                    f"Error: Found {count} matches for old_string in {path}. "
                    f"Provide more surrounding context to identify a unique match, "
                    f"or set replace_all=true to replace all occurrences."
                )

            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)

            content_bytes = new_content.encode("utf-8")
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": path},
                    headers={
                        "Authorization": f"Bearer {self.valves.daytona_api_key}",
                    },
                    files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                )
                resp.raise_for_status()

            replaced = count if replace_all else 1
            await _emit(__event_emitter__, "Edit complete", done=True)
            return f"Replaced {replaced} occurrence(s) in {path}"

        return await _tool_guard(__event_emitter__, _run())

    async def attach(
        self,
        path: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> HTMLResponse:
        """
        Attach a file from the sandbox for the user to view inline.
        The file content is rendered visually for the human but is NOT returned
        to the model. Use read() if you need to see file contents yourself.
        Works with text files (syntax-highlighted), images (rendered inline),
        and binary files (download card with Save button for files under 10 MB).
        :param path: Absolute path or relative to /home/daytona (e.g. workspace/main.py).
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, f"Attaching {path}...")

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    _toolbox(self.valves, sandbox_id, "/files/download"),
                    params={"path": path},
                    headers=_headers(self.valves),
                )

                if resp.status_code == 404:
                    await _emit(__event_emitter__, "File not found", done=True)
                    return f"Error: File not found: {path}"

                resp.raise_for_status()
                raw = resp.content  # bytes, not text — safe for binary

            n_bytes = len(raw)
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            file_type = _classify_file(path, raw)

            if file_type == "image":
                html_content = _render_image_html(raw, filename, path)
            elif file_type == "binary":
                html_content = _render_binary_html(raw, filename, path)
            else:
                # Text path: decode, highlight, render code viewer
                content = raw.decode("utf-8")
                n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
                highlighted = _highlight_code(content, path)
                raw_b64 = base64.b64encode(raw).decode("ascii")

                html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
{_BASE_CSS}
  .header button.ok {{ color: #a6e3a1; border-color: #a6e3a1; }}
  .code-wrap {{
    display: flex;
    overflow-x: auto;
  }}
  .gutter {{
    padding: 12px 0;
    text-align: right;
    color: #585b70;
    user-select: none;
    flex-shrink: 0;
    border-right: 1px solid #313244;
  }}
  .gutter div {{
    padding: 0 12px;
    line-height: 1.45;
  }}
  pre {{
    margin: 0;
    flex: 1;
    overflow-x: auto;
  }}
  pre code {{
    display: block;
    padding: 12px;
    line-height: 1.45;
    tab-size: 4;
  }}
</style>
</head>
<body>
  <div class="header">
    <span class="filename">{html_mod.escape(filename)}</span>
    <span class="meta">{n_lines} lines &middot; {n_bytes:,} bytes</span>
    <span class="actions">
      <button id="copy-btn" onclick="copyFile()">Copy</button>
      <button onclick="saveFile()">Save</button>
    </span>
  </div>
  <div class="code-wrap">
    <div class="gutter">{"".join(f"<div>{i}</div>" for i in range(1, n_lines + 1))}</div>
    <pre><code>{highlighted}</code></pre>
  </div>
  <script>
    var _raw = atob("{raw_b64}");
    var _fname = "{html_mod.escape(filename)}";
    function copyFile() {{
      var btn = document.getElementById('copy-btn');
      var ta = document.createElement('textarea');
      ta.value = _raw;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try {{ ok = document.execCommand('copy'); }} catch(e) {{}}
      document.body.removeChild(ta);
      if (ok) {{
        btn.textContent = 'Copied!';
        btn.classList.add('ok');
        setTimeout(function() {{ btn.textContent = 'Copy'; btn.classList.remove('ok'); }}, 1500);
      }} else {{
        btn.textContent = 'Failed';
        setTimeout(function() {{ btn.textContent = 'Copy'; }}, 1500);
      }}
    }}
    function saveFile() {{
      var blob = new Blob([_raw], {{type: 'text/plain'}});
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = _fname;
      a.click();
      URL.revokeObjectURL(a.href);
    }}
{_REPORT_HEIGHT_JS}
  </script>
</body>
</html>"""

            await _emit(__event_emitter__, f"Attached {filename} ({n_bytes:,} bytes)", done=True)
            return HTMLResponse(content=html_content, headers={"Content-Disposition": "inline"})

        return await _tool_guard(__event_emitter__, _run())

    async def ingest(
        self,
        prompt: str = "",
        __user__: dict = {},
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Ask the user to upload a file from their local machine into the sandbox.
        The file goes directly to the sandbox filesystem — it does not enter the
        conversation context or the OWUI database. Use read() afterward if you
        need to inspect the file contents yourself.
        :param prompt: Optional message shown to the user explaining what file is needed, e.g. "Upload your CSV dataset".
        """
        async def _run():
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            if not __event_call__:
                return "Error: ingest requires browser-side execution (__event_call__). Ensure the toolkit is used in Native function calling mode."

            # Build the prompt text for the modal
            prompt_text = prompt if prompt else "The assistant is asking for a file."
            prompt_js = json.dumps(prompt_text)

            # Max file size (25 MB)
            max_bytes = 25 * 1024 * 1024

            # The JS picks a file, uploads it to OWUI's Files API from the
            # browser (normal HTTP POST with XHR for progress), and returns
            # only the small file ID + metadata through __event_call__.
            js = f"""
const promptText = {prompt_js};
const maxBytes = {max_bytes};

return await new Promise((resolve) => {{
    const container = document.createElement("div");
    container.style.cssText =
        "position:fixed;top:0;left:0;width:100%;height:100%;" +
        "display:flex;align-items:center;justify-content:center;" +
        "background:rgba(0,0,0,0.45);z-index:99999";

    const card = document.createElement("div");
    card.style.cssText =
        "background:#1e1e2e;border-radius:12px;padding:32px 40px;" +
        "text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.5);" +
        "font-family:system-ui,sans-serif;color:#cdd6f4;max-width:480px";

    const title = document.createElement("h3");
    title.textContent = "Upload File to Sandbox";
    title.style.cssText = "margin:0 0 8px;font-size:18px;color:#f5e0dc";

    const desc = document.createElement("p");
    desc.textContent = promptText;
    desc.style.cssText = "margin:0 0 20px;font-size:14px;opacity:0.8";

    const fileInfo = document.createElement("p");
    fileInfo.style.cssText = "margin:0 0 16px;font-size:13px;color:#89b4fa;min-height:20px";

    // Progress bar (hidden until upload starts)
    const progressWrap = document.createElement("div");
    progressWrap.style.cssText =
        "display:none;margin:0 0 16px;background:#45475a;border-radius:4px;" +
        "height:6px;overflow:hidden";
    const progressBar = document.createElement("div");
    progressBar.style.cssText =
        "height:100%;width:0%;background:#89b4fa;border-radius:4px;" +
        "transition:width 0.2s ease";
    progressWrap.appendChild(progressBar);

    const input = document.createElement("input");
    input.type = "file";
    input.style.display = "none";

    const chooseBtn = document.createElement("button");
    chooseBtn.textContent = "Choose File\\u2026";
    chooseBtn.style.cssText =
        "padding:10px 28px;font-size:15px;border:none;border-radius:8px;" +
        "background:#89b4fa;color:#1e1e2e;cursor:pointer;font-weight:600;" +
        "white-space:nowrap";

    const uploadBtn = document.createElement("button");
    uploadBtn.textContent = "Upload";
    uploadBtn.style.cssText =
        "padding:10px 28px;font-size:15px;border:none;border-radius:8px;" +
        "background:#a6e3a1;color:#1e1e2e;cursor:pointer;font-weight:600;" +
        "white-space:nowrap;display:none";

    const cancel = document.createElement("button");
    cancel.textContent = "Cancel";
    cancel.style.cssText =
        "padding:10px 28px;font-size:14px;border:1px solid #585b70;" +
        "border-radius:8px;background:transparent;color:#a6adc8;" +
        "cursor:pointer;white-space:nowrap";

    let selectedFile = null;

    function humanSize(n) {{
        if (n < 1024) return n + " B";
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
        return (n / 1024 / 1024).toFixed(1) + " MB";
    }}

    function selectFile(file) {{
        selectedFile = file;
        if (selectedFile.size > maxBytes) {{
            fileInfo.textContent = selectedFile.name + " (" + humanSize(selectedFile.size) + ") \\u2014 too large (max 25 MB)";
            fileInfo.style.color = "#f38ba8";
            uploadBtn.style.display = "none";
        }} else {{
            fileInfo.textContent = selectedFile.name + " (" + humanSize(selectedFile.size) + ")";
            fileInfo.style.color = "#a6e3a1";
            uploadBtn.style.display = "inline-block";
        }}
    }}

    input.onchange = () => {{
        if (input.files && input.files.length > 0) selectFile(input.files[0]);
    }};

    // Drag-and-drop: make the whole card a drop target
    let dragCounter = 0;
    const defaultBorder = "none";
    const activeBorder = "2px dashed #89b4fa";
    card.style.border = defaultBorder;
    card.addEventListener("dragenter", (e) => {{
        e.preventDefault();
        dragCounter++;
        card.style.border = activeBorder;
    }});
    card.addEventListener("dragleave", (e) => {{
        e.preventDefault();
        dragCounter--;
        if (dragCounter <= 0) {{ dragCounter = 0; card.style.border = defaultBorder; }}
    }});
    card.addEventListener("dragover", (e) => {{
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
    }});
    card.addEventListener("drop", (e) => {{
        e.preventDefault();
        dragCounter = 0;
        card.style.border = defaultBorder;
        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {{
            selectFile(e.dataTransfer.files[0]);
        }}
    }});

    const dropHint = document.createElement("p");
    dropHint.textContent = "or drop a file onto this dialog";
    dropHint.style.cssText = "margin:14px 0 0;font-size:12px;color:#585b70;font-style:italic";

    chooseBtn.onclick = () => input.click();

    uploadBtn.onclick = () => {{
        if (!selectedFile) return;
        uploadBtn.style.display = "none";
        chooseBtn.style.display = "none";
        cancel.style.display = "none";
        dropHint.style.display = "none";
        progressWrap.style.display = "block";
        fileInfo.textContent = "Uploading\\u2026 0%";
        fileInfo.style.color = "#89b4fa";

        const token = localStorage.getItem("token");
        const formData = new FormData();
        formData.append("file", selectedFile);

        const xhr = new XMLHttpRequest();

        xhr.upload.onprogress = (e) => {{
            if (e.lengthComputable) {{
                const pct = Math.round(e.loaded / e.total * 100);
                progressBar.style.width = pct + "%";
                fileInfo.textContent = "Uploading\\u2026 " + pct + "% (" + humanSize(e.loaded) + " / " + humanSize(e.total) + ")";
            }}
        }};

        xhr.upload.onload = () => {{
            progressBar.style.width = "100%";
            progressBar.style.background = "#a6e3a1";
            fileInfo.textContent = "Processing\\u2026";
            fileInfo.style.color = "#a6e3a1";
        }};

        xhr.onload = () => {{
            if (xhr.status >= 200 && xhr.status < 300) {{
                try {{
                    const result = JSON.parse(xhr.responseText);
                    container.remove();
                    resolve(JSON.stringify({{
                        ok: true,
                        name: selectedFile.name,
                        size: selectedFile.size,
                        file_id: result.id,
                    }}));
                }} catch (e) {{
                    container.remove();
                    resolve(JSON.stringify({{ ok: false, error: "Bad response: " + xhr.responseText.slice(0, 200) }}));
                }}
            }} else {{
                container.remove();
                resolve(JSON.stringify({{ ok: false, error: "Upload failed: HTTP " + xhr.status + " " + xhr.responseText.slice(0, 200) }}));
            }}
        }};

        xhr.onerror = () => {{
            container.remove();
            resolve(JSON.stringify({{ ok: false, error: "Network error during upload" }}));
        }};

        xhr.open("POST", "/api/v1/files/");
        xhr.setRequestHeader("Authorization", "Bearer " + token);
        xhr.send(formData);
    }};

    cancel.onclick = () => {{
        container.remove();
        resolve(JSON.stringify({{ ok: false, error: "User cancelled" }}));
    }};

    const btnRow = document.createElement("div");
    btnRow.style.cssText = "display:flex;align-items:center;justify-content:center;gap:12px";
    btnRow.appendChild(chooseBtn);
    btnRow.appendChild(uploadBtn);
    btnRow.appendChild(cancel);

    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(fileInfo);
    card.appendChild(progressWrap);
    card.appendChild(input);
    card.appendChild(btnRow);
    card.appendChild(dropHint);
    container.appendChild(card);
    document.body.appendChild(container);
}});
"""

            await _emit(__event_emitter__, "Waiting for file selection...")

            raw = await __event_call__({"type": "execute", "data": {"code": js}})

            # Normalise response
            if isinstance(raw, str):
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    result = {"ok": False, "error": f"Unexpected response: {raw[:200]}"}
            elif isinstance(raw, dict):
                result = raw
            else:
                result = {"ok": False, "error": f"Unexpected response type: {type(raw)}"}

            if not result.get("ok"):
                err = result.get("error", "Unknown error")
                await _emit(__event_emitter__, f"File not uploaded: {err}", done=True)
                return f"File not uploaded: {err}"

            filename = result.get("name", "unknown")
            file_size = result.get("size", 0)
            file_id = result.get("file_id", "")

            if not file_id:
                await _emit(__event_emitter__, "No file ID received", done=True)
                return "Error: Browser upload succeeded but no file ID returned."

            # Read file directly from OWUI's storage layer. The toolkit runs
            # in-process, so we import the models and storage provider rather
            # than making an HTTP call to ourselves.
            await _emit(__event_emitter__, f"Transferring {filename} to sandbox...")

            try:
                from open_webui.models.files import Files as OWUIFiles
                from open_webui.storage.provider import Storage

                file_record = OWUIFiles.get_file_by_id(file_id)
                if not file_record:
                    await _emit(__event_emitter__, "File not found in OWUI", done=True)
                    return f"Error: File {file_id} not found in OWUI database."

                local_path = Storage.get_file(file_record.path)
                with open(local_path, "rb") as f:
                    file_bytes = f.read()
            except ImportError as e:
                await _emit(__event_emitter__, "Internal error accessing OWUI storage", done=True)
                return f"Error: Could not import OWUI storage layer: {e}"

            dest_path = f"/home/daytona/workspace/{filename}"

            # Ensure workspace directory exists
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    _toolbox(self.valves, sandbox_id, "/process/execute"),
                    headers=_headers(self.valves),
                    json={
                        "command": 'bash -c "mkdir -p /home/daytona/workspace"',
                        "timeout": 5000,
                    },
                )

            # Upload to Daytona sandbox
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": dest_path},
                    headers={
                        "Authorization": f"Bearer {self.valves.daytona_api_key}",
                    },
                    files={"file": ("file", io.BytesIO(file_bytes), "application/octet-stream")},
                )
                resp.raise_for_status()

            # Delete the transient file from OWUI storage
            try:
                from open_webui.models.files import Files as OWUIFiles
                OWUIFiles.delete_file_by_id(file_id)
            except Exception:
                pass  # cleanup failure is non-fatal

            size_str = _human_size(file_size)
            await _emit(__event_emitter__, f"Uploaded {filename} ({size_str})", done=True)
            return f"Uploaded {filename} ({size_str}) to {dest_path}"

        return await _tool_guard(__event_emitter__, _run())

    async def preview(
        self,
        port: int = 3000,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Generate a preview URL for a service running in the sandbox.
        The server must already be running in the background (see bash docs).
        Returns a signed URL the user can open in a new browser tab.
        :param port: The port the sandbox service is listening on (3000–9999). Defaults to 3000.
        """
        async def _run():
            if not isinstance(port, int) or port < 3000 or port > 9999:
                return "Error: port must be an integer between 3000 and 9999."

            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, f"Generating preview URL for port {port}...")

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ports/{port}/signed-preview-url"),
                    params={"expiresInSeconds": 3600},
                    headers=_headers(self.valves),
                )
                resp.raise_for_status()
                data = resp.json()

            url = data.get("url", "")
            if not url:
                return "Error: Daytona returned an empty preview URL."

            await _emit(__event_emitter__, f"Preview URL ready (port {port})", done=True)
            return (
                f"Preview URL (valid ~1 hour): {url}\n\n"
                f"The user can open this in a new browser tab. "
                f"They may see a Daytona security warning on first visit — they can click through it.\n\n"
                f"Note: the sandbox auto-stops after ~15 min of inactivity regardless of "
                f"running background processes, killing the server. If the user reports "
                f"the preview stopped working, restart the server and call preview() again."
            )

        return await _tool_guard(__event_emitter__, _run())

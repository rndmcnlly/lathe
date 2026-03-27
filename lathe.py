"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (lathe, bash, read, write, edit, onboard, expose, destroy) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx
version: 0.10.0
licence: MIT
"""

import asyncio
import inspect
import io
import json
import textwrap
import time
import urllib.parse
import uuid

import httpx
from pydantic import BaseModel, Field


# ── module-level helpers (invisible to OWUI tool discovery) ──────────
#
# OWUI discovers tools by calling dir() on the Tools instance and
# keeping every callable whose name doesn't start with "_".  The
# underscore filter was only added in Mar 2026 (PR #22408), so older
# deployments expose *all* methods as tools.  To stay safe across
# versions, keep helpers at module scope — OWUI never introspects the
# module, only the class.


def _build_tool_catalog(tools_instance) -> str:
    """Introspect a Tools instance to produce a tool summary table.

    Skips private methods and the lathe() tool itself so the catalog
    describes only the "real" tools the model can call.
    """
    lines = []
    for name, method in inspect.getmembers(tools_instance, predicate=inspect.ismethod):
        if name.startswith("_") or name == "lathe":
            continue
        sig = inspect.signature(method)
        params = [
            p.name for p in sig.parameters.values()
            if not p.name.startswith("__")
        ]
        doc = inspect.getdoc(method) or ""
        # First sentence of the docstring as the summary
        summary = doc.split("\n")[0].rstrip(".") if doc else "(no description)"
        param_str = ", ".join(params) if params else ""
        lines.append(f"  {name}({param_str}) — {summary}")
    return "\n".join(sorted(lines))


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


async def _tool_context(emitter, fn):
    """Open a shared HTTP client, call fn(client), catch standard tool exceptions."""
    try:
        async with httpx.AsyncClient() as client:
            return await fn(client)
    except RuntimeError as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"
    except httpx.HTTPStatusError as e:
        await _emit(emitter, f"API error: HTTP {e.response.status_code}", done=True)
        return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
    except Exception as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"


def _prepend_warning(result: str, warning: str | None) -> str:
    """Prepend a sandbox lifecycle warning to a tool result if present."""
    return f"{warning}\n{result}" if warning else result


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


def _require_abs_path(path: str, param_name: str = "path") -> str | None:
    """Return an error string if *path* is not absolute, else None."""
    if not path.startswith("/"):
        return (
            f"Error: {param_name} must be an absolute path "
            f"(e.g. /home/daytona/workspace/file.txt). Got: {path}"
        )
    return None


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


def _build_onboard_script(project_path: str) -> str:
    """Build a Python script that collects agent context from a sandbox.

    Searches two locations:
      1. ~/.agents/          — global agent instructions and skills
      2. <project_path>/     — project-local instructions and skills

    Skills are merged into a single catalog.  On name collision, the
    project-level entry wins (more specific scope takes precedence).
    """
    # The script is a self-contained Python program executed on the sandbox.
    # project_path is injected via repr() so it's safely quoted as a
    # Python string literal.  The rest of the script uses no interpolation.
    return "import os, glob\n\nPROJECT = " + repr(project_path) + "\n" + textwrap.dedent("""\
        GLOBAL  = os.path.expanduser("~/.agents")

        sections = []
        found = False

        # ── Collect AGENTS.md files ──────────────────────────────────

        def read_agents_md(base, heading):
            p = os.path.join(base, "AGENTS.md")
            if not os.path.isfile(p):
                return None
            with open(p) as f:
                return f"# {heading} ({p})\\n\\n{f.read()}"

        global_md = read_agents_md(GLOBAL, "Global Agent Instructions")
        if global_md:
            sections.append(global_md)
            found = True

        project_md = read_agents_md(PROJECT, "Project Agent Instructions")
        if project_md:
            sections.append(project_md)
            found = True

        # ── Collect and merge skills ─────────────────────────────────

        def collect_skills(base):
            skills_dir = os.path.join(base, "skills")
            if not os.path.isdir(skills_dir):
                return
            for skill_md in sorted(glob.glob(os.path.join(skills_dir, "*/SKILL.md"))):
                dir_name = os.path.basename(os.path.dirname(skill_md))
                name = dir_name
                desc = ""
                try:
                    with open(skill_md) as f:
                        lines = f.readlines()
                except OSError:
                    continue
                # Parse YAML frontmatter (minimal, no deps)
                if lines and lines[0].strip() == "---":
                    for line in lines[1:]:
                        if line.strip() == "---":
                            break
                        if line.startswith("name:"):
                            name = line[len("name:"):].strip()
                        elif line.startswith("description:"):
                            desc = line[len("description:"):].strip()
                yield name, desc, skill_md

        # Global first, then project overrides on collision
        skills = {}   # name -> (desc, path)
        order  = []   # first-seen order
        for name, desc, path in collect_skills(GLOBAL):
            if name not in skills:
                order.append(name)
            skills[name] = (desc, path)
        for name, desc, path in collect_skills(os.path.join(PROJECT, ".agents")):
            if name not in skills:
                order.append(name)
            skills[name] = (desc, path)

        if order:
            found = True
            lines = [
                "# Available Skills",
                "",
                "Load a skill's full instructions with read(path) when the task matches its description.",
                "",
            ]
            for name in order:
                desc, path = skills[name]
                lines.append(f"- **{name}**: {desc}")
                lines.append("  `" + path + "`")
            sections.append("\\n".join(lines))

        # ── Output ───────────────────────────────────────────────────

        if not found:
            print("ERROR_NO_CONTEXT")
            raise SystemExit(1)

        print("\\n\\n---\\n\\n".join(sections))
    """)




VOLUME_MOUNT_PATH = "/home/daytona/volume"


async def _ensure_volume(valves, volume_name: str, client: httpx.AsyncClient) -> str:
    """Get or create a Daytona volume by name. Polls until ready. Returns the volume ID."""
    encoded_name = urllib.parse.quote(volume_name, safe="")
    get_url = _api(valves, f"/volumes/by-name/{encoded_name}")

    # Try to fetch existing volume (treat deleting volumes as absent)
    resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
    need_create = (
        resp.status_code != 200
        or resp.json().get("state") in ("pending_delete", "deleting")
    )
    if need_create:
        # Create the volume.  Retry loop handles the race where a
        # recently-deleted volume name hasn't fully freed up yet.
        for attempt in range(30):
            resp = await client.post(
                _api(valves, "/volumes"),
                headers=_headers(valves),
                json={"name": volume_name},
                timeout=30.0,
            )
            if resp.status_code == 400 and "already exists" in resp.text:
                # Deletion still propagating — wait and retry.
                await asyncio.sleep(2)
                resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
                if resp.status_code == 200:
                    state = resp.json().get("state")
                    if state not in ("pending_delete", "deleting"):
                        break
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(
                f"Could not create volume '{volume_name}' — "
                f"name still reserved by a deleting volume after 60s of retries"
            )

    vol = resp.json()
    vol_id = vol["id"]

    # Poll until the volume is ready (creation involves S3 provisioning).
    # Tolerate transient 404s — the by-name index may lag behind creation.
    if vol.get("state") == "ready":
        return vol_id

    deadline = time.time() + 60
    poll_interval = 1.0
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
        if resp.status_code == 404:
            poll_interval = min(poll_interval * 1.2, 5.0)
            continue
        resp.raise_for_status()
        vol = resp.json()
        if vol.get("state") == "ready":
            return vol_id
        poll_interval = min(poll_interval * 1.2, 5.0)

    raise RuntimeError(f"Volume '{volume_name}' did not reach ready state within 60s (state: {vol.get('state')})")


async def _wait_for_toolbox(valves, sandbox_id: str, client: httpx.AsyncClient, emitter=None):
    """Poll the toolbox API until it responds, then ensure workspace dir exists."""
    for attempt in range(30):
        try:
            resp = await client.post(
                _toolbox(valves, sandbox_id, "/process/execute"),
                headers=_headers(valves),
                json={"command": "echo ready", "timeout": 5000},
                timeout=10.0,
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
                        timeout=10.0,
                    )
                    return
        except (httpx.HTTPError, httpx.TimeoutException):
            pass
        await asyncio.sleep(1)
        if attempt == 2:
            await _emit(emitter, "Waiting for sandbox to become ready...")
    raise RuntimeError("Sandbox started but toolbox daemon did not become responsive (30s)")


async def _ensure_sandbox(valves, email: str, client: httpx.AsyncClient, emitter=None) -> tuple[str, str | None]:
    """Find or create a running sandbox for this user.

    Returns (sandbox_id, warning) where warning is None if the sandbox was
    already running, or a short message describing what recovery was needed.
    """
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

    # 1. Look up existing sandbox by label
    resp = await client.get(
        _api(valves, "/sandbox"),
        params={"labels": labels_filter},
        headers=_headers(valves),
        timeout=30.0,
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
    warning: str | None = None

    if sandbox is None:
        # 2. Get or create a persistent volume for this user
        volume_name = f"{label_key}/{email}"
        volume_id = await _ensure_volume(valves, volume_name, client)

        # 3. Create new sandbox with volume mounted
        await _emit(emitter, "Preparing sandbox...")
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
                "volumes": [
                    {
                        "volumeId": volume_id,
                        "mountPath": VOLUME_MOUNT_PATH,
                    }
                ],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        sandbox = resp.json()
        warning = "[Sandbox was created — this is a fresh environment with no prior files]"

    sandbox_id = sandbox["id"]
    state = sandbox.get("state", "unknown")

    # 3. Ensure it's running
    if state == "started":
        await _wait_for_toolbox(valves, sandbox_id, client, emitter)
        await _emit(emitter, "Sandbox ready", done=True)
        return sandbox_id, warning

    if state in ("stopped", "archived"):
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/start"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        if not warning:
            warning = (
                "[Sandbox was restarted from archived state — running processes were lost]"
                if state == "archived" else
                "[Sandbox was restarted — running processes were lost]"
            )

    elif state == "error" and sandbox.get("recoverable"):
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/recover"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/start"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        if not warning:
            warning = "[Sandbox was recovered from error — check that expected files and processes still exist]"

    elif state in ("starting", "stopping", "archiving"):
        await _emit(emitter, "Preparing sandbox...")
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
            timeout=30.0,
        )
        resp.raise_for_status()
        info = resp.json()
        state = info.get("state", "unknown")

        if state == "started":
            await _wait_for_toolbox(valves, sandbox_id, client, emitter)
            await _emit(emitter, "Sandbox ready", done=True)
            return sandbox_id, warning

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
        foreground_timeout_seconds: int = Field(
            30,
            description="Seconds to wait for a bash command before auto-backgrounding it (1-300)",
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

    # ── agent-facing manual ──────────────────────────────────────────
    #
    # The manpage system is the model's primary orientation surface.
    # Design principles:
    #   - Tool docstrings stay minimal; details are deferred here so
    #     context budget is spent only when the model actually needs help.
    #   - The tool catalog is introspected dynamically so it can never
    #     drift from the actual method list.
    #   - Manpage content is currently static strings, but the
    #     architecture is designed to evolve:
    #
    # TODO(future): Valve-driven behavioral policy injection.
    #   Valves already control mechanism (timeouts, sandbox language,
    #   auto-stop). The next step is for Valve values to also inject
    #   *policy guidance* into manpage content. Mechanism is fixed at
    #   class scan time; policy is runtime-configurable through the manual.
    #
    # TODO(future): Information architecture expansion.
    #   Add per-tool deep dives (manpage="bash"), workflow recipes
    #   (manpage="expose-recipes"), and troubleshooting guides. Existing
    #   tool docstrings can then be further scrunched by adding
    #   breadcrumbs like 'see lathe(manpage="bash") for details'.

    # WARNING: Manpage strings are NOT passed through str.format()
    # unconditionally.  Only pages containing the literal placeholder
    # "{tool_catalog}" are formatted (see the lathe() method).  This
    # means shell snippets with curly braces (${VAR}, {sh,pid,log,exit},
    # {"key":"value"}, etc.) are safe in all other pages.  If you add a
    # new dynamic placeholder, gate the .format() call on its presence
    # rather than calling .format() on every page — otherwise any page
    # with literal braces will blow up with a KeyError at runtime.
    _MANPAGES: dict[str, str] = {
        "egress": textwrap.dedent("""\
            # Lathe — Egress Restrictions

            ## What can the sandbox reach?

            The sandbox can directly reach a broad allowlist of hosts:
            package registries (PyPI, npm, apt), git hosts (GitHub, GitLab,
            Bitbucket), container registries (Docker Hub, ghcr.io), AI APIs
            (OpenAI, Anthropic, OpenRouter, Groq, etc.), CDNs (Cloudflare,
            jsDelivr, unpkg), select cloud storage (S3, GCS), and common
            dev platforms (Vercel, Supabase, Sentry).

            bash("curl ...") and bash("wget ...") work for all allowlisted
            hosts. Most tasks never hit the limit.

            ## When curl fails: egress workarounds

            If a request fails because the sandbox cannot reach a host
            (connection timeout, connection refused on a host you know is
            up), the host is not on the egress allowlist. There is no clean
            way for the agent to independently bypass this — the
            workarounds involve the user.

            **Common — user downloads and uploads via dufs:**
            Ask the user to download the file on their own machine, then
            upload it to the sandbox through the dufs file browser. See
            lathe(manpage="recipes") for dufs setup. This handles any file
            type and any host with no size constraints.

            **Rare — custom browser-side fetch service:**
            For repeated fetch needs (e.g. crawling an API the sandbox
            can't reach), build a small web service in the sandbox that
            presents a UI where the user clicks to initiate fetches from
            their browser. The browser has unrestricted egress but is
            subject to CORS — this only works automatically for targets
            that set Access-Control-Allow-Origin headers. The service
            POSTs results back to itself for the agent to read.

            **Clean solution — Daytona Tier 3:**
            Daytona Tier 3 accounts have unrestricted egress. If the
            admin's Daytona account is Tier 3, none of these workarounds
            are needed — bash("curl ...") reaches any host. The admin can
            check their tier at https://app.daytona.io.

            ## What does NOT work

            - pip install / git clone / npm install to non-allowlisted
              hosts fail even with dufs. Download the artifact first, then
              install from the local file (e.g. pip install ./package.whl).
            - The browser's fetch() API is subject to CORS. A custom fetch
              service cannot silently proxy arbitrary URLs — only
              CORS-friendly ones auto-complete; others require the user to
              download and upload manually.
            """),
        "background": textwrap.dedent("""\
            # Lathe — Background Jobs

            When bash() auto-backgrounds a command, it returns a descriptor with
            two paths:

              CMD=/tmp/cmd/<id>   — the job's sidecar directory
              PID=/tmp/cmd/<id>/pid — process ID file

            ## Sidecar files

            Where CMD=/tmp/cmd/<id>:

              CMD/sh    — the full wrapper script that was executed
              CMD/pid   — PID of the bash process (written before exec)
              CMD/log   — stdout+stderr, written live via tee
              CMD/exit  — exit code; present only when the process ends

            Absence of CMD/exit means the process is still running *or* the
            sandbox was restarted (in which case the PID is stale). To
            distinguish the two, check whether the PID is still alive.

            ## Recipes

            **Peek at live output:**
            ```
            tail $CMD/log
            ```

            **Poll until done (bounded):**
            ```
            for i in 1 2 3 4 5; do
              test -f $CMD/exit && {{ cat $CMD/exit; break; }} || sleep 2
            done
            test -f $CMD/exit || echo STILL_RUNNING
            ```

            **Check if process is alive:**
            ```
            kill -0 $(cat $CMD/pid) 2>/dev/null && echo ALIVE || echo DEAD_OR_GONE
            ```

            **Kill the job:**
            ```
            kill $(cat $CMD/pid)
            ```
            This stops the wrapper process. Note: child processes the job spawned
            will have already reparented to init and will keep running. If the job
            launched named services (e.g. a dev server), kill them by name:
            ```
            pkill -f my_server_name
            ```

            **Kill and confirm wrapper is gone:**
            ```
            kill $(cat $CMD/pid); sleep 1; kill -0 $(cat $CMD/pid) 2>/dev/null && echo STILL_UP || echo GONE
            ```

            ## Caution

            After a sandbox restart, CMD/pid contains a stale PID. A new process
            may have been assigned that ID. Do not kill without first verifying
            the process is the one you launched (e.g. check CMD/log for expected
            output before killing).
            """),
        "recipes": textwrap.dedent("""\
            # Lathe — Recipes

            Tested scripts for bootstrapping common tools from a cold sandbox.
            These tools live in /tmp and survive sandbox stop/restart but not
            destroy(). If /tmp/dufs or /tmp/code-server is missing, re-run the
            install script.

            ## File browser — dufs

            When the user asks to upload files, download files, browse files,
            or transfer files, the answer is dufs + expose(). Do NOT attempt
            to relay file contents through the conversation — give the user a
            URL they can use directly in their browser.

            **Install dufs (run once per sandbox):**
            ```
            TAG=$(curl -s https://api.github.com/repos/sigoden/dufs/releases/latest | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
            curl -sL "https://github.com/sigoden/dufs/releases/download/${TAG}/dufs-${TAG}-x86_64-unknown-linux-musl.tar.gz" \
              | tar xz -C /tmp && chmod +x /tmp/dufs && /tmp/dufs --version
            ```

            **Start and expose:**
            ```
            nohup /tmp/dufs /home/daytona --port 3000 --allow-all &
            ```
            Then call expose(port=3000). Give the user the URL and tell them:
            - Drag and drop to upload
            - Click files to download
            - Navigate folders to browse

            **Serve a specific directory:**
            ```
            nohup /tmp/dufs /home/daytona/workspace/output --port 3000 --allow-all &
            ```

            **Read-only (download only, no upload):**
            ```
            nohup /tmp/dufs /home/daytona/workspace --port 3000 &
            ```

            ## Full IDE — code-server

            When the user asks for an IDE, editor, or VS Code in the browser,
            use code-server.

            **Install code-server (run once per sandbox):**
            ```
            curl -fsSL https://code-server.dev/install.sh | sh -s -- --method=standalone --prefix=/tmp/code-server
            ```

            **Start and expose:**
            ```
            nohup /tmp/code-server/bin/code-server --bind-addr 0.0.0.0:8080 --auth none /home/daytona/workspace &
            ```
            Then call expose(port=8080). The user gets VS Code in their browser
            with full terminal, extensions, and file editing.
            """),
        "overview": textwrap.dedent("""\
            # Lathe Toolkit — Overview

            Lathe is a coding-agent toolkit running inside Open WebUI. It gives
            you a persistent Linux sandbox backed by a Daytona VM with a
            cross-conversation filesystem. The sandbox is created transparently
            on first tool use and survives across conversations for the same user.

            Read this page fully before your first tool call. It covers the
            sandbox model, available workflows, and common mistakes.

            ## Sandbox model

            - One sandbox per user, identified by email. The sandbox starts,
              stops, and recovers automatically — you never manage lifecycle.
            - The default working directory is /home/daytona/workspace.
            - /home/daytona/volume is S3/FUSE-backed persistent storage that
              survives sandbox destruction.
            - The sandbox auto-stops after a configurable idle timeout and
              auto-archives after a further interval. Any tool call transparently
              restarts it. The filesystem (including installed packages and user
              files) survives both stop and archive — only running processes are
              lost.

            ## Tool catalog

            {tool_catalog}

            ## Key workflows

            **Running services and exposing them:**
            The sandbox is a server. Background a web server with nohup, then
            call expose(port=N) to get a public HTTPS URL the user can open.
            The sandbox auto-stops on idle, which kills background processes —
            restart the server and call expose() again if needed.

            **File upload/download/browsing:**
            When the user wants to upload, download, or browse files, run dufs
            (a file server) in the sandbox and expose it. The user gets
            drag-and-drop upload/download in their browser with no size limit.
            See lathe(manpage="recipes") for install and usage scripts.

            **Interactive shell:**
            For interactive work, call expose(ssh=true) to give the user a
            time-limited SSH command they can paste into their terminal, VS Code
            Remote SSH, or JetBrains Gateway.

            **Project context:**
            Call onboard() at the start of a conversation to load AGENTS.md and
            discover available skills. Searches both the project directory and
            ~/.agents/ for global agent instructions and skills.

            **Network requests:**
            Use bash("curl ...") or bash("wget ...") for HTTP requests. The
            sandbox can reach a broad allowlist of hosts directly (package
            registries, git hosts, CDNs, AI APIs, etc.), and bash gives you
            streaming, piping, and natural access to env-var credentials.
            If a request fails due to egress filtering, see
            lathe(manpage="egress") for workarounds.

            ## Gotchas

            - Commands are non-interactive. No stdin prompts, no curses UIs. Use
              -y or equivalent flags. For interactive work, give the user an
              expose(ssh=true) token.
            - bash() auto-backgrounds commands that exceed ~30 seconds. When this
              happens, it returns a background descriptor with CMD and PID paths.
              Use foreground_seconds= to extend the wait (e.g. foreground_seconds=120
              for known-slow commands or when waiting for a backgrounded command to
              finish). The command keeps running even after backgrounding.
              See lathe(manpage="background") for peek/poll/kill recipes.
            - bash() output is truncated to the last 2000 lines / 50 KB. If
              truncated, the full output is available in the log file at
              /tmp/cmd/<id>/log — use read() to inspect specific sections.
            - edit() requires an exact string match (including whitespace). If
              the match is ambiguous, provide more surrounding context or use
              replace_all=true.
            - expose() URLs expire after ~1 hour. The sandbox itself stops on
              idle (~15 min default), killing servers.
            - destroy() is irreversible. The volume is preserved.
            - **Network egress may be restricted.** Depending on the admin's
              Daytona tier, the sandbox may only reach a curated allowlist of
              hosts (package registries, git hosts, CDNs, AI APIs, etc.).
              Requests to non-allowlisted hosts silently fail or time out.
              If curl fails on a host you know is up, see
              lathe(manpage="egress") for workarounds.
            """),
    }

    # One-line descriptions for the page index (shown on unknown page
    # lookups and useful for the model to decide which page to request).
    _MANPAGE_INDEX: dict[str, str] = {
        "overview": "Big-picture orientation: sandbox model, tool catalog, key workflows, gotchas.",
        "recipes": "Bootstrap scripts for common tools: dufs (file browser), code-server (IDE).",
        "background": "Background job sidecar files, and peek/poll/kill recipes.",
        "egress": "Egress restrictions, workarounds (dufs upload, browser-side fetch), Tier 3.",
        "version": "Show the installed Lathe toolkit version.",
    }

    async def lathe(
        self,
        manpage: str = "overview",
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Manual for the lathe toolkit. Call lathe(manpage="overview") before your first tool use in a new conversation to learn the sandbox model, available workflows, and gotchas. Costs one tool call, saves many.
        :param manpage: Which manual page to return. Use "overview" for big-picture orientation, "version" for the installed version.
        """
        tool_catalog = _build_tool_catalog(self)

        if manpage == "version":
            # Extract version from the module docstring (single source of truth).
            import re as _re
            mod_doc = globals().get("__doc__", "") or ""
            match = _re.search(r"^version:\s*(.+)$", mod_doc, _re.MULTILINE)
            ver = match.group(1).strip() if match else "unknown"
            await _emit(__event_emitter__, f"Lathe v{ver}", done=True)
            return f"Lathe toolkit version: {ver}"

        if manpage in self._MANPAGES:
            content = self._MANPAGES[manpage]
            if "{tool_catalog}" in content:
                content = content.format(tool_catalog=tool_catalog)
            await _emit(__event_emitter__, f"Manual page: {manpage}", done=True)
            return content

        # Unknown page — return the index so the model can discover what exists
        index_lines = "\n".join(
            f"  {name} — {desc}"
            for name, desc in sorted(self._MANPAGE_INDEX.items())
        )
        await _emit(__event_emitter__, f"Unknown manpage: {manpage}", done=True)
        return (
            f"Unknown manpage \"{manpage}\". Available pages:\n\n"
            f"{index_lines}\n\n"
            f"Call lathe(manpage=\"overview\") for big-picture orientation."
        )

    async def destroy(
        self,
        confirm: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Permanently destroy the sandbox VM. Irreversible. Set confirm=true to proceed.
        Persistent volume data is preserved and will reappear in the next sandbox.
        :param confirm: Must be true to confirm destruction.
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

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    _api(valves, "/sandbox"),
                    params={"labels": labels_filter},
                    headers=_headers(valves),
                    timeout=30.0,
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
                        timeout=30.0,
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
                        timeout=30.0,
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
                    f"Destroyed {len(deleted)} sandbox(es) ({ids})."
                    f" Your persistent files in {VOLUME_MOUNT_PATH} are intact"
                    f" and will reappear in your next sandbox."
                    f" A fresh sandbox will be created on the next tool call."
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
        Load project context (AGENTS.md + skill catalog) at the start of a conversation.
        Searches both the project directory and ~/.agents/ for global context.
        Use read() on a skill's SKILL.md path to load its full instructions later.
        :param path: Absolute path to the project root (e.g. /home/daytona/workspace/myproject).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, "Loading project context...")

            p = path.rstrip("/")
            script = _build_onboard_script(p)

            # Write script to temp file and execute it
            script_path = f"/tmp/_onboard_{uuid.uuid4()}.py"
            content_bytes = script.encode("utf-8")
            await client.post(
                _toolbox(self.valves, sandbox_id, "/files/upload"),
                params={"path": script_path},
                headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                timeout=60.0,
            )

            resp = await client.post(
                _toolbox(self.valves, sandbox_id, "/process/execute"),
                headers=_headers(self.valves),
                json={
                    "command": f"python3 {script_path}",
                    "timeout": 30000,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

            result = data.get("result", "")
            exit_code = data.get("exitCode", -1)

            if "ERROR_NO_CONTEXT" in result:
                await _emit(__event_emitter__, "No project context found", done=True)
                return (
                    f"Error: No agent context found. Searched:\n"
                    f"  - {path}/AGENTS.md\n"
                    f"  - {path}/.agents/skills/\n"
                    f"  - ~/.agents/AGENTS.md\n"
                    f"  - ~/.agents/skills/\n"
                    f"At least one of these must exist to use onboard."
                )

            if exit_code != 0:
                await _emit(__event_emitter__, "Error loading context", done=True)
                return f"Error: onboard script failed (exit {exit_code}): {result[:500]}"

            await _emit(__event_emitter__, "Project context loaded", done=True)
            return _prepend_warning(result if result else "(empty project context)", _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def bash(
        self,
        command: str,
        workdir: str = "/home/daytona/workspace",
        foreground_seconds: int = 0,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Execute a bash command in the persistent Linux sandbox. Non-interactive only.
        Commands that finish within the foreground window return output directly.
        Long-running commands auto-background and return a descriptor with log
        file paths for monitoring.
        If you have not read the manual yet, call lathe(manpage="overview") first.
        :param command: The bash command to execute.
        :param workdir: Working directory (default: /home/daytona/workspace).
        :param foreground_seconds: Seconds to wait before auto-backgrounding (default: per admin setting, usually 30). Use higher values when waiting for a known-slow command to finish.
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, "Running command...")

            # Collect user-supplied env vars from UserValves (never logged).
            # Raises ValueError (caught below) if env_vars is malformed.
            user_valves = __user__.get("valves")
            user_pairs: list[tuple[str, str]] = []
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                user_pairs = _parse_env_vars(raw_env)

            # ── Build wrapper script ────────────────────────────────
            #
            # Each command gets a /proc-style directory under /tmp/cmd/:
            #   /tmp/cmd/<uuid>/sh    — the wrapper script
            #   /tmp/cmd/<uuid>/pid   — wrapper process ID (written before exec; kill to stop the job)
            #   /tmp/cmd/<uuid>/log   — stdout+stderr (tee'd live)
            #   /tmp/cmd/<uuid>/exit  — exit code (written on completion)
            cmd_id = str(uuid.uuid4())
            cmd_dir = f"/tmp/cmd/{cmd_id}"
            log_path = f"{cmd_dir}/log"
            pid_path = f"{cmd_dir}/pid"
            exit_path = f"{cmd_dir}/exit"
            script_path = f"{cmd_dir}/sh"

            user_env_lines = "".join(
                f"export {k}={_shell_quote(v)}\n" for k, v in user_pairs
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
                + f"echo $BASHPID > {_shell_quote(pid_path)}\n"
                + f"exec > >(tee {_shell_quote(log_path)}) 2>&1\n"
                + command
                + "\n"
            )

            # Upload the script (creates parent dirs automatically)
            await client.post(
                _toolbox(self.valves, sandbox_id, "/files/upload"),
                params={"path": script_path},
                headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                files={"file": ("file", io.BytesIO(script.encode("utf-8")), "application/octet-stream")},
                timeout=60.0,
            )

            # ── Create a per-command session ─────────────────────────
            # Each bash() call gets its own session so commands never
            # queue behind each other.  This is critical: a shared
            # session serialises commands, so monitoring a backgrounded
            # build via tail/cat would block until the build finishes.
            session_id = f"lathe-cmd-{cmd_id}"
            resp = await client.post(
                _toolbox(self.valves, sandbox_id, f"/process/session"),
                headers=_headers(self.valves),
                json={"sessionId": session_id},
                timeout=30.0,
            )
            if resp.status_code not in (200, 409):
                resp.raise_for_status()

            # ── Execute asynchronously in the session ────────────────
            # The actual command writes exit code to a sidecar file so
            # the agent can check completion even after backgrounding.
            # Session exec has no cwd parameter, so we cd explicitly.
            exec_command = (
                f"cd {_shell_quote(workdir)} && "
                f"bash {script_path}; EC=$?; "
                f"echo $EC > {_shell_quote(exit_path)}; "
                f"(exit $EC)"
            )
            resp = await client.post(
                _toolbox(self.valves, sandbox_id, f"/process/session/{session_id}/exec"),
                headers=_headers(self.valves),
                json={
                    "command": exec_command,
                    "runAsync": True,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            session_cmd_id = resp.json().get("cmdId", "")

            # ── Foreground polling window ────────────────────────────
            # Per-call override wins; 0 (default) falls back to Valve.
            fg_timeout = max(1, min(300,
                foreground_seconds if foreground_seconds > 0
                else self.valves.foreground_timeout_seconds))
            deadline = time.time() + fg_timeout
            poll_interval = 0.25
            last_status_at = time.time()
            finished = False
            exit_code = None

            while time.time() < deadline:
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 2.0)

                # Check command status via session info
                resp = await client.get(
                    _toolbox(self.valves, sandbox_id, f"/process/session/{session_id}"),
                    headers=_headers(self.valves),
                    timeout=15.0,
                )
                if resp.status_code != 200:
                    continue
                session_info = resp.json()
                commands = session_info.get("commands", [])

                # Find our command by id
                for cmd in commands:
                    if cmd.get("id") == session_cmd_id:
                        ec = cmd.get("exitCode")
                        if ec is not None:
                            exit_code = ec
                            finished = True
                        break

                if finished:
                    break

                # Emit progress every ~5s
                now = time.time()
                if now - last_status_at >= 5.0:
                    elapsed = int(now - (deadline - fg_timeout))
                    await _emit(__event_emitter__, f"Running... ({elapsed}s)")
                    last_status_at = now

            # ── Fetch logs and clean up session ─────────────────────
            logs_resp = await client.get(
                _toolbox(self.valves, sandbox_id, f"/process/session/{session_id}/command/{session_cmd_id}/logs"),
                headers=_headers(self.valves),
                timeout=30.0,
            )
            result = logs_resp.text if logs_resp.status_code == 200 else ""

            if finished:
                # Session served its purpose — clean up to avoid accumulation.
                await client.delete(
                    _toolbox(self.valves, sandbox_id, f"/process/session/{session_id}"),
                    headers=_headers(self.valves),
                    timeout=10.0,
                )

            # ── Auto-backgrounded: command still running ────────────
            if not finished:
                elapsed = int(time.time() - (deadline - fg_timeout))
                await _emit(__event_emitter__, f"Command backgrounded ({elapsed}s)", done=True)

                # Return partial output + background descriptor
                output, was_truncated, meta = _truncate_tail(result)
                if not output.strip():
                    output = "(no output yet)"

                bg_notice = (
                    f"\n\n[Backgrounded after {elapsed}s — command is still running]\n"
                    f"CMD={cmd_id}\n"
                    f"Ref /tmp/cmd/$CMD/{{sh,pid,log,exit}}\n"
                    f"See lathe(manpage=\"background\") for peek/poll/kill recipes.\n"
                    f"Tell the user the command is running. Don't poll until they ask or "
                    f"you have a concrete reason to expect completion."
                )
                return _prepend_warning(output + bg_notice, _sb_warning)

            # ── Command finished within foreground window ────────────
            output, was_truncated, meta = _truncate_tail(result)
            spill_path = None

            if was_truncated:
                # The log file already exists on disk (tee'd by the
                # wrapper script), so just point at it — no upload needed.
                spill_path = log_path

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

            return _prepend_warning(output, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 2000,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Read a file from the sandbox. Returns numbered lines.
        :param path: Absolute path (e.g. /home/daytona/workspace/main.py).
        :param offset: Starting line number (1-indexed, default: 1).
        :param limit: Max lines to return (default: 2000).
        """
        async def _run(client):
            err = _require_abs_path(path)
            if err:
                return err
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, f"Reading {path}...")

            resp = await client.get(
                _toolbox(self.valves, sandbox_id, "/files/download"),
                params={"path": path},
                headers=_headers(self.valves),
                timeout=60.0,
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
            return _prepend_warning(f"{header}\n{numbered}", _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def write(
        self,
        path: str,
        content: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Write a file to the sandbox (created if it doesn't exist, parents auto-created).
        :param path: Absolute path (e.g. /home/daytona/workspace/main.py).
        :param content: The full file content to write.
        """
        async def _run(client):
            err = _require_abs_path(path)
            if err:
                return err
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, f"Writing {path}...")

            parent = "/".join(path.rstrip("/").split("/")[:-1])
            if parent:
                await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/folder"),
                    headers=_headers(self.valves),
                    json={"path": parent, "mode": "755"},
                    timeout=30.0,
                )

            content_bytes = content.encode("utf-8")
            resp = await client.post(
                _toolbox(self.valves, sandbox_id, "/files/upload"),
                params={"path": path},
                headers={
                    "Authorization": f"Bearer {self.valves.daytona_api_key}",
                },
                files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                timeout=60.0,
            )
            resp.raise_for_status()

            n_bytes = len(content_bytes)
            n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
            await _emit(__event_emitter__, "Write complete", done=True)
            return _prepend_warning(f"Wrote {n_bytes} bytes ({n_lines} lines) to {path}", _sb_warning)

        return await _tool_context(__event_emitter__, _run)

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
        Edit a file by exact string replacement. Fails on ambiguous matches unless replace_all=true.
        :param path: Absolute path (e.g. /home/daytona/workspace/main.py).
        :param old_string: Exact text to find (must match including whitespace).
        :param new_string: The replacement text.
        :param replace_all: Replace all occurrences (default: false).
        """
        async def _run(client):
            err = _require_abs_path(path)
            if err:
                return err
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, f"Editing {path}...")

            resp = await client.get(
                _toolbox(self.valves, sandbox_id, "/files/download"),
                params={"path": path},
                headers=_headers(self.valves),
                timeout=60.0,
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
            resp = await client.post(
                _toolbox(self.valves, sandbox_id, "/files/upload"),
                params={"path": path},
                headers={
                    "Authorization": f"Bearer {self.valves.daytona_api_key}",
                },
                files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                timeout=60.0,
            )
            resp.raise_for_status()

            replaced = count if replace_all else 1
            await _emit(__event_emitter__, "Edit complete", done=True)
            return _prepend_warning(f"Replaced {replaced} occurrence(s) in {path}", _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def expose(
        self,
        port: int = 0,
        ssh: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Expose a sandbox service to the user. Use port= for web servers,
        ssh=true for interactive shell access.
        Common patterns: dufs for file upload/download, code-server for a full
        IDE, or any web app. See lathe(manpage="recipes") for install scripts.
        :param port: Port the service listens on (3000–9999). Returns a public HTTPS URL valid ~1 hour.
        :param ssh: If true, returns a time-limited SSH command (ignores port).
        """
        async def _run(client):
            if not ssh and (not isinstance(port, int) or port < 3000 or port > 9999):
                return "Error: port must be an integer between 3000 and 9999, or set ssh=true for shell access."

            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            if ssh:
                await _emit(__event_emitter__, "Creating SSH access token...")
                resp = await client.post(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ssh-access"),
                    params={"expiresInMinutes": 60},
                    headers=_headers(self.valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()

                ssh_command = data.get("sshCommand", "")
                if not ssh_command:
                    token = data.get("token", "")
                    if not token:
                        return "Error: Daytona returned neither sshCommand nor token."
                    ssh_command = f"ssh {token}@ssh.app.daytona.io"

                await _emit(__event_emitter__, "SSH access ready", done=True)
                return _prepend_warning(
                    f"SSH command (valid 60 min):\n\n"
                    f"```\n{ssh_command}\n```\n\n"
                    f"The user can paste this into their terminal, VS Code Remote SSH, "
                    f"or JetBrains Gateway.\n\n"
                    f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity. "
                    f"Active SSH sessions keep the sandbox alive.",
                    _sb_warning,
                )

            # Port exposure path
            await _emit(__event_emitter__, f"Generating URL for port {port}...")

            resp = await client.get(
                _api(self.valves, f"/sandbox/{sandbox_id}/ports/{port}/signed-preview-url"),
                params={"expiresInSeconds": 3600},
                headers=_headers(self.valves),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

            url = data.get("url", "")
            if not url:
                return "Error: Daytona returned an empty URL."

            await _emit(__event_emitter__, f"URL ready (port {port})", done=True)
            return _prepend_warning(
                f"Public URL (valid ~1 hour): {url}\n\n"
                f"The user can open this in a new browser tab. "
                f"They may see a Daytona security warning on first visit — they can click through it.\n\n"
                f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity regardless of "
                f"running background processes, killing the server. If the user reports "
                f"the URL stopped working, restart the server and call expose() again.",
                _sb_warning,
            )

        return await _tool_context(__event_emitter__, _run)

"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (lathe, bash, read, write, edit, onboard, expose, fetch, destroy) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx, beautifulsoup4, markdownify
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
        fetch_max_response_bytes: int = Field(
            100 * 1024 * 1024,
            description="Maximum response body size for fetch() in bytes (default: 100 MB)",
        )
        fetch_timeout_seconds: int = Field(
            120,
            description="HTTP timeout for fetch() requests in seconds (default: 120)",
        )
        fetch_inline_max_bytes: int = Field(
            50 * 1024,
            description="Maximum response body size for inline fetch() output in bytes (default: 50 KB). Larger responses auto-spill to a temp file.",
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
        "fetch": textwrap.dedent("""\
            # Lathe — fetch() egress bypass

            ## When to use fetch() vs bash(curl/wget)

            **Default: use bash().** The sandbox can reach a broad allowlist
            of hosts directly — package registries, git hosts, CDNs, AI APIs,
            cloud storage, and common dev platforms. For these, bash("curl ...")
            or bash("wget ...") is simpler, faster, supports streaming, and
            keeps credentials inside the sandbox where env vars work naturally.

            **Use fetch() when:**
            - A request fails because the sandbox cannot reach the host
              (egress filtering). fetch() runs the request from the OWUI
              server, which has unrestricted egress.
            - You need server-side HTML processing (filter="markdown",
              "links", or "meta") without installing anything in the sandbox.

            **Do NOT use fetch() just because it exists.** If curl would work,
            curl is better. fetch() relays through the OWUI server, adding
            latency, a 100 MB body cap, and no streaming.

            ## Parameter reference

              body="@/home/daytona/workspace/req.json"   — read request body from sandbox file
              body={"query":"test"}                      — send literal JSON as body

              output="@/home/daytona/workspace/resp.json" — write response body to sandbox file
              output="inline"                             — return body in context
              output="inline:1024"                        — return body truncated to 1024 bytes
              output=""                                   — discard body (default)

              include_response_headers="important"  — content-type, content-length, location, rate-limit (default)
              include_response_headers="all"        — every response header
              include_response_headers="none"       — suppress response headers entirely

              filter="markdown"  — convert HTML to markdown (strips scripts/styles)
              filter="links"     — extract all links as text + href pairs
              filter="meta"      — extract title, description, og/twitter tags

              verify_ssl=false   — skip TLS certificate verification (self-signed certs, internal CAs)

            ## Patterns

            **Egress bypass — download a blocked artifact:**
            ```
            fetch(url="https://blocked-host.example.com/model.tar.gz",
                  output="@/home/daytona/workspace/model.tar.gz")
            bash("tar xzf model.tar.gz", workdir="/home/daytona/workspace")
            ```

            **Egress bypass — API call to a blocked host:**
            ```
            fetch(url="https://blocked-api.example.com/search",
                  method="POST",
                  headers={"Content-Type": "application/json"},
                  body={"query": "test"},
                  output="inline")
            ```

            **Crawl documentation (server-side HTML filtering):**
            ```
            fetch(url="https://docs.example.com/api/reference",
                  filter="markdown",
                  output="inline")
            ```
            The filter converts HTML to clean markdown server-side —
            no sandbox round-trip needed. Use filter="links" to
            extract a link index, or filter="meta" for page metadata.

            **Save response to file:**
            ```
            fetch(url="https://example.com/api/data",
                  output="@/home/daytona/workspace/data.json")
            read("/home/daytona/workspace/data.json")
            ```

            **POST with a file body:**
            ```
            write("/home/daytona/workspace/payload.json", {"big": "data..."})
            fetch(url="https://blocked-api.example.com/upload",
                  method="POST",
                  headers={"Content-Type": "application/json"},
                  body="@/home/daytona/workspace/payload.json",
                  output="@/home/daytona/workspace/results.json")
            ```

            **Check availability (HEAD):**
            ```
            fetch(url="https://example.com/file.zip", method="HEAD")
            ```

            ## Inline with auto-spill

            If output="inline" but the response exceeds the inline size
            limit (~50 KB default), the body is automatically written to a
            temp file and the tool result tells you where. Use read() to
            inspect specific sections.

            ## Working with APIs that require auth

            The model cannot see UserValves secrets directly. To use a
            secret as a request header, the user must set it in UserValves
            env_vars, then:
            ```
            bash("echo -n $MY_API_KEY > /tmp/key.txt")
            ```
            Read the key from the sandbox and construct the headers param.
            If the API host is on the egress allowlist, skip fetch() and just use
            bash("curl -H \"Authorization: Bearer $MY_API_KEY\" ...") directly.

            ## Limitations

            - One request per call. No connection pooling or cookie jars
              across calls.
            - Redirects are followed automatically (up to 20 hops). The
              metadata shows the final URL if it differs from the requested
              one.
            - Response body must fit in server memory during relay. Default
              limit is 100 MB (admin-configurable via fetch_max_response_bytes).
            - Inline responses are capped at ~50 KB (admin-configurable via
              fetch_inline_max_bytes). Larger responses auto-spill to a temp file.
            - fetch() does NOT provide ambient network access to the sandbox.
              Commands like `pip install` or `git clone` to non-allowlisted
              hosts still fail. Workaround: fetch() the artifact, then
              install from the local file.
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
            discover available skills for the project.

            **Network requests:**
            Use bash("curl ...") or bash("wget ...") for HTTP requests. The
            sandbox can reach a broad allowlist of hosts directly (package
            registries, git hosts, CDNs, AI APIs, etc.), and bash gives you
            streaming, piping, and natural access to env-var credentials.
            Only use fetch() when a request fails due to egress filtering —
            it relays through the OWUI server to bypass sandbox restrictions.
            See lathe(manpage="fetch") for the full decision rule.

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
            - **Network egress is restricted.** The sandbox can only reach a
              curated allowlist of hosts: package registries (PyPI, npm, apt),
              git hosts (GitHub, GitLab, Bitbucket), container registries
              (Docker Hub, ghcr.io), AI APIs (OpenAI, Anthropic, OpenRouter,
              Groq, etc.), CDNs (Cloudflare, jsDelivr, unpkg), select cloud
              storage (S3, GCS), and common dev platforms (Vercel, Supabase,
              Sentry). Requests to other hosts will silently fail or time out.
              Use fetch() to retrieve URLs the sandbox cannot reach directly —
              the request is made from the server, bypassing egress restrictions.
              Request and response bodies are read/written as sandbox files;
              only HTTP metadata enters the conversation. Note: fetch() is
              single-shot and does not provide ambient proxy access, so commands
              like `pip install` or `git clone` to non-allowlisted hosts still
              fail — use fetch() to download the artifact, then install from
              the local file. See lathe(manpage="fetch") for patterns.
            """),
    }

    # One-line descriptions for the page index (shown on unknown page
    # lookups and useful for the model to decide which page to request).
    _MANPAGE_INDEX: dict[str, str] = {
        "overview": "Big-picture orientation: sandbox model, tool catalog, key workflows, gotchas.",
        "recipes": "Bootstrap scripts for common tools: dufs (file browser), code-server (IDE).",
        "background": "Background job sidecar files, and peek/poll/kill recipes.",
        "fetch": "When to use fetch() vs bash(curl), egress bypass patterns, HTML filtering, API auth.",
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
        Use read() on a skill's SKILL.md path to load its full instructions later.
        :param path: Absolute path to the project root (e.g. /home/daytona/workspace/myproject).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

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
            script_path = f"/tmp/_onboard_{uuid.uuid4()}.sh"
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
                    "command": f"bash {script_path}",
                    "cwd": p,
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
                    f"Error: No AGENTS.md or .agents/skills/ directory found at {path}. "
                    f"This path must contain at least one of these to use onboard."
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

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: str = "{}",
        body: str = "",
        output: str = "",
        filter: str = "",
        include_response_headers: str = "important",
        verify_ssl: bool = True,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Egress bypass: fetch a URL from the OWUI server when the sandbox cannot reach the host directly.
        For hosts the sandbox CAN reach (allowlisted package registries, git hosts, CDNs, AI APIs, etc.),
        prefer bash("curl ...") or bash("wget ...") instead — they are faster, support streaming, and
        keep credentials in the sandbox. Use fetch() only when a request fails due to egress filtering,
        or when you need server-side HTML filtering (filter="markdown"|"links"|"meta").
        See lathe(manpage="fetch") for when-to-use guidance and patterns.
        :param url: The URL to fetch (http or https).
        :param method: HTTP method: GET, POST, PUT, DELETE, PATCH, HEAD (default: GET).
        :param headers: JSON object of extra request headers, e.g. {"Accept": "text/html"}.
        :param body: Request body. "@/absolute/path" reads from sandbox file; bare string is sent literally. Empty = no body.
        :param output: Response body handling. "@/absolute/path" writes to sandbox file; "inline" returns body in context; "inline:N" truncates to N bytes; empty = discard (metadata only).
        :param filter: Post-process HTML responses: "markdown" (convert to markdown), "links" (extract all links), "meta" (title + description + og tags). Empty = no filtering.
        :param include_response_headers: How many response headers to show: "none", "important" (default — content-type, content-length, location, etc.), or "all".
        :param verify_ssl: Verify TLS certificates. Set false for self-signed certs (default: true).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            # ── validate inputs ──────────────────────────────────────
            allowed_methods = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")
            norm_method = method.strip().upper()
            if norm_method not in allowed_methods:
                return f"Error: method must be one of {', '.join(allowed_methods)}. Got: {method}"

            if not url or not (url.startswith("http://") or url.startswith("https://")):
                return "Error: url must start with http:// or https://"

            try:
                req_headers = json.loads(headers) if headers.strip() else {}
                if not isinstance(req_headers, dict):
                    return "Error: headers must be a JSON object, e.g. {\"Accept\": \"text/html\"}"
            except (json.JSONDecodeError, ValueError) as exc:
                return f"Error: headers is not valid JSON: {exc}"

            # ── resolve request body ─────────────────────────────────
            req_body: bytes | None = None
            if body:
                if body.startswith("@"):
                    # Read from sandbox file
                    body_path = body[1:]
                    err = _require_abs_path(body_path, "body (after @)")
                    if err:
                        return err
                    await _emit(__event_emitter__, f"Reading request body from {body_path}...")
                    resp = await client.get(
                        _toolbox(self.valves, sandbox_id, "/files/download"),
                        params={"path": body_path},
                        headers=_headers(self.valves),
                        timeout=60.0,
                    )
                    if resp.status_code == 404:
                        return f"Error: body file not found: {body_path}"
                    resp.raise_for_status()
                    req_body = resp.content
                else:
                    # Literal inline body
                    req_body = body.encode("utf-8")

            # ── resolve output mode ──────────────────────────────────
            output_mode = "discard"  # "discard" | "file" | "inline"
            output_path = ""
            inline_caller_limit = 0  # 0 = no caller override
            if output:
                out_stripped = output.strip().lower()
                if output.startswith("@"):
                    output_mode = "file"
                    output_path = output[1:]
                    err = _require_abs_path(output_path, "output (after @)")
                    if err:
                        return err
                elif out_stripped == "inline" or out_stripped.startswith("inline:"):
                    output_mode = "inline"
                    if ":" in out_stripped:
                        try:
                            inline_caller_limit = int(out_stripped.split(":", 1)[1])
                            if inline_caller_limit <= 0:
                                return "Error: inline byte limit must be a positive integer, e.g. output=\"inline:1024\""
                        except ValueError:
                            return "Error: invalid inline limit — use output=\"inline:1024\" (bytes)"
                else:
                    return (
                        f"Error: unrecognized output mode \"{output}\". "
                        f"Use \"@/absolute/path\" to write to file, "
                        f"\"inline\" or \"inline:N\" to return body in context, "
                        f"or omit for metadata only."
                    )

            # ── make the HTTP request from a dedicated client ────────
            # Separate from the Daytona API client: different trust
            # boundary, different timeout, optional TLS bypass.
            await _emit(__event_emitter__, f"{norm_method} {url}...")

            max_bytes = self.valves.fetch_max_response_bytes
            timeout_s = float(max(1, self.valves.fetch_timeout_seconds))

            try:
                async with httpx.AsyncClient(verify=verify_ssl) as fetch_client:
                    fetch_resp = await fetch_client.request(
                        norm_method,
                        url,
                        headers=req_headers,
                        content=req_body,
                        timeout=timeout_s,
                        follow_redirects=True,
                    )
            except httpx.TimeoutException:
                await _emit(__event_emitter__, "Request timed out", done=True)
                return f"Error: Request timed out after {int(timeout_s)}s"
            except httpx.RequestError as exc:
                await _emit(__event_emitter__, "Request failed", done=True)
                return f"Error: Request failed: {exc}"

            status_code = fetch_resp.status_code
            reason = fetch_resp.reason_phrase or ""
            resp_headers = dict(fetch_resp.headers)
            resp_body = fetch_resp.content

            # ── enforce size limit ───────────────────────────────────
            if len(resp_body) > max_bytes:
                await _emit(__event_emitter__, "Response too large", done=True)
                return (
                    f"Error: Response body is {_human_size(len(resp_body))}, "
                    f"exceeding the {_human_size(max_bytes)} limit. "
                    f"Ask an admin to increase fetch_max_response_bytes if needed."
                )

            # ── apply filter (HTML post-processing) ───────────────────
            filter_warning = ""
            filter_mode = filter.strip().lower() if filter else ""
            if filter_mode:
                if filter_mode not in ("markdown", "links", "meta"):
                    return f"Error: filter must be \"markdown\", \"links\", or \"meta\". Got: {filter}"

                content_type = resp_headers.get("content-type", "")
                is_html = "html" in content_type or "xhtml" in content_type
                if not is_html and resp_body:
                    filter_warning = (
                        f"Warning: filter=\"{filter_mode}\" is intended for HTML, "
                        f"but response content-type is \"{content_type}\". "
                        f"Applying anyway.\n"
                    )

                if resp_body:
                    try:
                        from bs4 import BeautifulSoup
                        html_text = resp_body.decode("utf-8", errors="replace")
                        soup = BeautifulSoup(html_text, "html.parser")
                        filtered = ""

                        if filter_mode == "markdown":
                            from markdownify import markdownify as md
                            # Remove script/style noise before converting
                            for tag in soup(["script", "style", "noscript"]):
                                tag.decompose()
                            filtered = md(str(soup), heading_style="ATX", strip=["img"])
                            # Collapse excessive blank lines
                            import re
                            filtered = re.sub(r"\n{3,}", "\n\n", filtered).strip()

                        elif filter_mode == "links":
                            links = []
                            for a in soup.find_all("a", href=True):
                                href = a["href"]
                                text = a.get_text(strip=True)
                                if text:
                                    links.append(f"  {text} — {href}")
                                else:
                                    links.append(f"  {href}")
                            filtered = f"{len(links)} links found:\n" + "\n".join(links) if links else "No links found."

                        elif filter_mode == "meta":
                            parts = []
                            title_tag = soup.find("title")
                            if title_tag:
                                parts.append(f"Title: {title_tag.get_text(strip=True)}")
                            for meta in soup.find_all("meta"):
                                name = meta.get("name", meta.get("property", "")).lower()
                                content = meta.get("content", "")
                                if name in ("description", "og:title", "og:description",
                                            "og:image", "og:url", "og:type", "og:site_name",
                                            "twitter:title", "twitter:description",
                                            "twitter:image", "twitter:card"):
                                    parts.append(f"{name}: {content}")
                            filtered = "\n".join(parts) if parts else "No metadata found."

                        resp_body = filtered.encode("utf-8")
                    except Exception as exc:
                        filter_warning += f"Warning: filter=\"{filter_mode}\" failed: {exc}. Returning unfiltered body.\n"

            # ── handle response body disposition ─────────────────────
            body_disposition = ""
            inline_body = ""

            if norm_method == "HEAD":
                body_disposition = "No body (HEAD request)"

            elif output_mode == "file" and resp_body:
                await _emit(__event_emitter__, f"Writing response to {output_path}...")
                parent = "/".join(output_path.rstrip("/").split("/")[:-1])
                if parent:
                    await client.post(
                        _toolbox(self.valves, sandbox_id, "/files/folder"),
                        headers=_headers(self.valves),
                        json={"path": parent, "mode": "755"},
                        timeout=30.0,
                    )
                upload_resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/files/upload"),
                    params={"path": output_path},
                    headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                    files={"file": ("file", io.BytesIO(resp_body), "application/octet-stream")},
                    timeout=120.0,
                )
                upload_resp.raise_for_status()
                body_disposition = f"Written to {output_path} ({_human_size(len(resp_body))})"

            elif output_mode == "inline" and resp_body:
                system_limit = self.valves.fetch_inline_max_bytes
                effective_limit = min(inline_caller_limit, system_limit) if inline_caller_limit else system_limit

                if len(resp_body) <= effective_limit:
                    # Small enough to return directly
                    try:
                        inline_body = resp_body.decode("utf-8")
                    except UnicodeDecodeError:
                        inline_body = resp_body.decode("latin-1")
                    body_disposition = f"Inline ({_human_size(len(resp_body))})"
                elif inline_caller_limit and len(resp_body) <= system_limit:
                    # Fits in system limit but exceeds caller limit — truncate
                    truncated = resp_body[:effective_limit]
                    try:
                        inline_body = truncated.decode("utf-8", errors="replace")
                    except UnicodeDecodeError:
                        inline_body = truncated.decode("latin-1")
                    body_disposition = (
                        f"Inline, truncated ({_human_size(effective_limit)} of "
                        f"{_human_size(len(resp_body))} shown)"
                    )
                else:
                    # Too large for inline — auto-spill to temp file
                    spill_path = f"/tmp/fetch_{uuid.uuid4().hex[:12]}"
                    await _emit(__event_emitter__, f"Response too large for inline ({_human_size(len(resp_body))}), spilling to {spill_path}...")
                    upload_resp = await client.post(
                        _toolbox(self.valves, sandbox_id, "/files/upload"),
                        params={"path": spill_path},
                        headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                        files={"file": ("file", io.BytesIO(resp_body), "application/octet-stream")},
                        timeout=120.0,
                    )
                    upload_resp.raise_for_status()
                    body_disposition = (
                        f"Too large for inline ({_human_size(len(resp_body))} > "
                        f"{_human_size(system_limit)} limit). "
                        f"Written to {spill_path} — use read() to inspect."
                    )

            elif output_mode == "discard" and resp_body:
                body_disposition = f"Discarded ({_human_size(len(resp_body))}) — use output=\"inline\" to see or output=\"@path\" to save"

            else:
                body_disposition = "Empty response body"

            # ── detect redirects ─────────────────────────────────────
            redirect_note = ""
            final_url = str(fetch_resp.url)
            if final_url != url:
                redirect_note = f"Redirected-To: {final_url}\n"

            # ── format response metadata ─────────────────────────────
            rh_mode = include_response_headers.strip().lower() if include_response_headers else "important"
            if rh_mode not in ("none", "important", "all"):
                rh_mode = "important"

            IMPORTANT_HEADERS = {
                "content-type", "content-length", "content-disposition",
                "content-encoding", "location", "retry-after",
                "www-authenticate", "x-ratelimit-remaining",
                "x-ratelimit-limit", "x-ratelimit-reset",
            }

            headers_block = ""
            if rh_mode != "none":
                header_lines = []
                for k, v in resp_headers.items():
                    kl = k.lower()
                    if kl in ("set-cookie",):
                        if rh_mode == "all":
                            header_lines.append(f"  {k}: (omitted)")
                        continue
                    if rh_mode == "important" and kl not in IMPORTANT_HEADERS:
                        continue
                    display_v = v if len(v) <= 512 else v[:512] + "..."
                    header_lines.append(f"  {k}: {display_v}")
                if header_lines:
                    headers_block = "\nResponse headers:\n" + "\n".join(header_lines)

            await _emit(__event_emitter__, f"HTTP {status_code}", done=True)

            result = (
                f"HTTP {status_code} {reason}\n"
                f"{redirect_note}"
                f"Response-Body: {body_disposition}"
                f"{headers_block}"
            )

            # Append inline body after metadata if present
            if inline_body:
                result += f"\n\n--- response body ---\n{inline_body}"

            if filter_warning:
                result = filter_warning + result

            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

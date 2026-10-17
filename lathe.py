"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (lathe, bash, read, write, edit, glob, grep, view, interpret, onboard, expose, destroy) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.12.0
requirements: httpx, httpx-ws, pydantic>=2, cachetools, PyYAML
version: 1.0.0
licence: MIT
"""

import asyncio
import base64
import inspect
import io
import json
import logging
import re
import textwrap
import time
import typing
import urllib.parse
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import httpx
from cachetools import LRUCache
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
import yaml


logger = logging.getLogger("lathe")

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


def _extract_sandbox_list(payload) -> list:
    """Tolerate both legacy and post-2026-05-24 shapes for GET /api/sandbox.

    Legacy (≤ May 24, 2026): a flat JSON array of sandbox objects.
    New (cursor pagination): {"items": [...], "cursor": "..."} (or similar).
    Lathe filters by label client-side and expects 0–1 matches per user, so
    the default page size is plenty; we don't paginate further. If a future
    deployment ever has >page_size sandboxes sharing the label prefix, this
    would silently return only the first page — acceptable given the design
    invariant of one sandbox per user.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("items") or []
    return []


def _multiple_sandboxes_error(label_key: str, email: str, sandboxes: list[dict]) -> str:
    """Explain why an ambiguous per-user sandbox association is refused."""
    ids = ", ".join(str(s.get("id", "unknown"))[:12] for s in sandboxes)
    return (
        "Multiple sandboxes appear to be associated with your account. Lathe will not "
        "choose, start, or destroy any of them because doing so could affect the wrong "
        "environment. Please contact your system administrator and include this message. "
        f"They should inspect the Daytona deployment label {label_key}={email} and "
        f"reconcile these sandbox IDs: {ids}."
    )


async def _get_live_sandbox(valves, sandbox_id: str, client: httpx.AsyncClient) -> dict | None:
    """Return authoritative sandbox data, or None for a deleted/deleting ghost.

    GET /sandbox?labels=... is an eventually consistent discovery index. Once its
    cardinality gate admits one candidate, this per-ID endpoint is authoritative.
    """
    resp = await client.get(
        _api(valves, f"/sandbox/{sandbox_id}"),
        headers=_headers(valves),
        timeout=30.0,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    sandbox = resp.json()
    if sandbox.get("state") in ("deleted", "destroyed"):
        return None
    return sandbox


async def _emit(emitter, description: str, done: bool = False):
    if emitter:
        await emitter(
            {
                "type": "status",
                "data": {"description": description, "done": done},
            }
        )


async def _tool_context(emitter, fn, harness=None):
    """Open a shared HTTP client, call fn(client), catch standard tool exceptions."""
    try:
        async with httpx.AsyncClient() as client:
            result = await fn(client)
    except RuntimeError as e:
        await _emit(emitter, f"Error: {e}", done=True)
        result = f"Error: {e}"
    except httpx.HTTPStatusError as e:
        await _emit(emitter, f"API error: HTTP {e.response.status_code}", done=True)
        try:
            detail = e.response.text[:500]
        except httpx.ResponseNotRead:
            detail = "(streamed error body not buffered)"
        result = f"API error: HTTP {e.response.status_code}: {detail}"
    except Exception as e:
        await _emit(emitter, f"Error: {e}", done=True)
        result = f"Error: {e}"
    return harness.deliver(result) if harness is not None else result


class _HarnessDelivery:
    """Per-call lifecycle context, delivered alongside the final text response."""

    def __init__(self, chat_state, chat_id, *, image_result=False):
        self.chat_state = chat_state
        self.chat_id = chat_id
        self.warning = None
        self.image_result = image_result

    def deliver(self, result):
        if self.image_result and result.startswith("data:image/"):
            # OWUI lifts only byte-exact image results. Defer context to text.
            if self.warning:
                _push_bg_notice(self.chat_state, self.chat_id, self.warning)
            return result
        return _prepend_harness_messages(result, _drain_harness_messages(
            self.chat_state, self.chat_id, self.warning,
        ))


def _prepend_harness_messages(result: str, messages: list[str]) -> str:
    """Prepend harness-injected context to a tool result.

    Harness messages are things the infrastructure decides to push to the
    agent alongside the tool's own output: sandbox lifecycle warnings,
    auto-init snapshots, and background job completion notices.
    Each message is separated by a blank line for readability.
    """
    if not messages:
        return result
    prefix = "\n\n".join(messages)
    return f"{prefix}\n\n{result}"


def _drain_harness_messages(chat_state: LRUCache, chat_id: str | None,
                            warning: str | None) -> list[str]:
    """Collect pending harness messages and the sandbox lifecycle warning."""
    messages: list[str] = []
    if warning:
        messages.append(warning)
    if chat_id and chat_id in chat_state:
        state = chat_state[chat_id]
        pending = state.get("pending", [])
        if pending:
            messages.extend(pending)
            state["pending"] = []
    return messages


def _push_bg_notice(chat_state: LRUCache, chat_id: str | None, notice: str):
    """Push a background job completion notice into the chat's pending queue.

    Safe to call even if the chat_id has been evicted from the LRU cache
    (silently dropped — if the chat is that old, nobody is waiting).
    """
    if not chat_id or chat_id not in chat_state:
        return
    state = chat_state[chat_id]
    pending = state.get("pending")
    if pending is None:
        state["pending"] = [notice]
    else:
        pending.append(notice)


def _format_bg_bash_notice(cmd_id: str, exit_code: int | None,
                           elapsed: int, tail: str) -> str:
    ec_str = str(exit_code) if exit_code is not None else "unknown"
    lines = [
        f"[Background job completed: CMD-{cmd_id}]",
        f"Exit code: {ec_str} (took {elapsed}s)",
    ]
    tail = tail.encode("utf-8")[-2048:].decode("utf-8", errors="ignore").strip()
    if tail:
        lines.append("Last lines of output:")
        lines.extend(f"  {tl}" for tl in tail.splitlines()[-5:])
    else:
        lines.append("(no output)")
    lines.append(f"Full output: {_bash_sidecar_dir(cmd_id)}/log; exit: {_bash_sidecar_dir(cmd_id)}/exit")
    lines.append("Retained for 24 hours after completion (sandbox stop/restart clears it).")
    return "\n".join(lines)


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


_EPHEMERAL_ROOT = "/dev/shm/lathe"


def _bash_sidecar_dir(cmd_id: str) -> str:
    """Return the tmpfs directory for one bash invocation."""
    return f"{_EPHEMERAL_ROOT}/cmd/{cmd_id}"


def _onboard_script_path(script_id: str) -> str:
    """Return the tmpfs path for one generated onboarding script."""
    return f"{_EPHEMERAL_ROOT}/onboard/{script_id}.py"


def _parse_env_vars(env_vars: str) -> list[tuple[str, str]]:
    """Parse a JSON object string into (key, value) pairs.

    Expects a JSON object mapping string keys to string values,
    e.g. '{"MY_TOKEN":"abc123","FOO":"bar"}'.
    Keys must match [A-Za-z_][A-Za-z0-9_]*; invalid keys are skipped with a warning.
    Returns [] on empty input (not an error).
    Raises ValueError on malformed input so the caller can surface it to the agent.
    """
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


# Keys lathe manages itself; admin create-overrides may not clobber them.
# - name/labels: the per-user lookup invariant (one sandbox per email).
# - volumes: forbidden; Lathe never provisions or mounts volumes.
_PROTECTED_CREATE_KEYS = frozenset({"name", "labels", "volumes"})


def _parse_create_overrides(raw: str) -> dict:
    """Parse the sandbox_create_overrides valve into a dict of create args.

    Expects a JSON object, e.g. '{"cpu":2,"memory":4,"snapshot":"my-snap"}'.
    Returns {} on empty input (not an error). Raises ValueError on malformed
    input or on any attempt to override a lathe-managed key, so the caller
    can surface it to the agent.
    """
    s = raw.strip()
    if not s or s == "{}":
        return {}
    try:
        overrides = json.loads(s)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Valves sandbox_create_overrides is not valid JSON: {exc}. "
            f"Fix the sandbox_create_overrides field in Tool settings (it should "
            f'look like {{"cpu":2,"memory":4}}) and retry.'
        ) from exc
    if not isinstance(overrides, dict):
        raise ValueError(
            f"Valves sandbox_create_overrides must be a JSON object, got "
            f"{type(overrides).__name__}. Expected something like "
            f'{{"cpu":2,"memory":4,"snapshot":"my-snapshot"}}.'
        )
    clobbered = _PROTECTED_CREATE_KEYS & overrides.keys()
    if clobbered:
        raise ValueError(
            f"Valves sandbox_create_overrides may not set lathe-managed keys: "
            f"{', '.join(sorted(clobbered))}. These are derived per user "
            f"(name/labels) or forbidden (volumes)."
        )
    return overrides


def _get_email(user: dict) -> str:
    email = user.get("email", "")
    if not email:
        raise RuntimeError("No email found for user. Cannot provision sandbox.")
    return email


_UPSTREAM_HEADER_BLOCKED = frozenset({
    "host", "cookie", "origin", "referer", "forwarded", "x-real-ip", "true-client-ip",
    "connection", "upgrade", "keep-alive", "te", "trailer", "transfer-encoding",
    "content-length", "expect", "http2-settings", "proxy-authorization", "proxy-authenticate",
})
_UPSTREAM_HEADER_PREFIXES = ("x-forwarded-", "sec-", "cf-", "daytona-", "x-daytona-", "x-lathe-")


def _validate_upstream_headers(headers: dict[str, str]) -> str | None:
    """Bound application headers without disclosing names or credential values.

    Keep this wire contract aligned with preview-wrapper/src/index.js. Access
    policy is independent: these are model-controlled app configuration, not
    identity claims vouched for by Lathe.
    """
    if len(headers) > 16:
        return "Error: upstream_headers permits at most 16 headers."
    seen = set()
    total = 0
    for name, value in headers.items():
        lower = name.lower()
        if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}", name)
                or lower in _UPSTREAM_HEADER_BLOCKED
                or lower.startswith(_UPSTREAM_HEADER_PREFIXES)):
            return "Error: upstream_headers contains an invalid or reserved header name."
        if lower in seen:
            return "Error: upstream_headers contains duplicate names (case-insensitive)."
        seen.add(lower)
        if len(value) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            return "Error: upstream_headers values must be printable ASCII, at most 4096 bytes each."
        total += len(name) + len(value)
    if total > 8192:
        return "Error: upstream_headers names and values exceed the 8192-byte total limit."
    return None


async def _http_preview(valves, sandbox_id: str, port: int, user: dict,
                         access: str, tag: str,
                         client: httpx.AsyncClient, *,
                         upstream_headers: dict[str, str] = {}) -> tuple[str, str]:
    """Obtain a signed URL and enforce the caller-selected disclosure level.

    Headerless public requests fail open to the direct signed bearer URL when
    wrapping is unavailable or refused. Private requests fail closed unless the trusted
    wrapper successfully registers an owner-authenticated URL. Header-bearing
    requests always require acknowledged injection, with no direct fallback. Wrapper failures
    stay inside this credential boundary so raw response bodies never reach the model.
    """
    endpoint = valves.preview_wrapper_url.strip()
    credential = valves.preview_wrapper_key
    wrapper_configured = bool(endpoint or credential)
    lifetime = f"{valves.preview_expiry_seconds / 3600:g} hour(s)"

    # Private or header-bearing requests fail before minting a bearer URL when
    # the wrapper configuration or trusted caller identity is unusable.
    if access == "private" or upstream_headers:
        endpoint_parts = urllib.parse.urlsplit(endpoint)
        if (not endpoint or not credential or endpoint_parts.scheme != "https"
                or not endpoint_parts.hostname or endpoint_parts.username
                or endpoint_parts.password or endpoint_parts.fragment
                or any(c.isspace() for c in endpoint)
                or not isinstance(user.get("id"), str) or not user["id"]
                or not isinstance(user.get("email"), str) or not user["email"]):
            raise RuntimeError(
                "HTTP preview unavailable. This request requires a correctly configured "
                "wrapping service and trusted caller identity; no direct URL was returned."
            )

    try:
        response = await client.get(
            _api(valves, f"/sandbox/{sandbox_id}/ports/{port}/signed-preview-url"),
            params={"expiresInSeconds": valves.preview_expiry_seconds},
            headers=_headers(valves), timeout=30.0, follow_redirects=False,
        )
        response.raise_for_status()
        upstream = response.json()["url"]
        upstream_parts = urllib.parse.urlsplit(upstream)
        if (upstream_parts.scheme != "https" or not upstream_parts.hostname
                or upstream_parts.username or upstream_parts.password
                or any(c.isspace() for c in upstream)):
            raise ValueError()
    except Exception:
        raise RuntimeError(
            "HTTP preview unavailable. Could not obtain a signed preview URL. Try expose again."
        ) from None

    def direct_public(reason: str) -> tuple[str, str]:
        return upstream, (
            f"Public direct preview ({reason}): this URL is a bearer credential, valid for up to {lifetime}. "
            "Anyone who copies it can access the service. You may see a Daytona warning on first visit. "
            "Sandbox sleep or service failure can end access sooner; call expose again to renew."
        )

    if not wrapper_configured:
        if access == "public":
            return direct_public("no wrapping service is configured")
        raise RuntimeError(
            "Private HTTP preview unavailable. No wrapping service is configured; no public URL was returned."
        )

    try:
        endpoint_parts = urllib.parse.urlsplit(endpoint)
        if (not endpoint or not credential or endpoint_parts.scheme != "https"
                or not endpoint_parts.hostname or endpoint_parts.username
                or endpoint_parts.password or endpoint_parts.fragment
                or any(c.isspace() for c in endpoint)
                or not isinstance(user.get("id"), str) or not user["id"]
                or not isinstance(user.get("email"), str) or not user["email"]):
            raise ValueError()
        registration = {
            "owner": {"subject": user["id"], "email": user["email"]},
            "upstream_url": upstream,
            "access": access,
        }
        if tag:
            registration["tag"] = tag
        if upstream_headers:
            registration["upstream_headers"] = upstream_headers
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {credential}"},
            json=registration,
            timeout=30.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        result = response.json()
        if upstream_headers and result.get("upstream_headers_applied") is not True:
            raise ValueError()
        url = result["url"]
        parsed = urllib.parse.urlsplit(url)
        expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or parsed.hostname == upstream_parts.hostname
                or upstream_parts.hostname in urllib.parse.unquote(url)
                or credential in url or any(c.isspace() for c in url)
                or expiry.tzinfo is None or expiry <= datetime.now(timezone.utc)):
            raise ValueError()
    except Exception:
        if access == "public" and not upstream_headers:
            return direct_public("wrapping was unavailable or refused")
        raise RuntimeError(
            "HTTP preview unavailable. Required wrapper registration failed or requested "
            "header injection was not acknowledged; no direct URL was returned. "
            "Ask the administrator to check the wrapping service."
        ) from None

    if access == "public":
        access_note = (
            "Public wrapped preview: anyone who has this URL can access the service."
        )
    else:
        access_note = (
            "Owner-authenticated private preview: sign in as the owning user. "
            "Copying the URL does not grant access."
        )
    return url, (
        f"{access_note} Registration expires {expiry.astimezone(timezone.utc).isoformat()}; "
        f"upstream access lasts up to {lifetime}. "
        "Sandbox sleep or service failure can end availability sooner; call expose again to renew."
    )


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
            room = _MAX_BYTES - kept_bytes - (1 if kept else 0)
            if room > 0:
                kept.append(line.encode("utf-8")[-room:].decode("utf-8", errors="ignore"))
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


# ── ripgrep adapter (runs on sandbox) ────────────────────────────────
# Use standard rg matching, ignore rules and traversal, not a second language.
_GLOB_MAX_LINES = _GREP_MAX_LINES = 100
_SEARCH_SCRIPT = r'''
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def _ripgrep():
    installed = shutil.which("rg")
    if installed:
        return installed
    root = Path("/tmp/lathe")
    root.mkdir(parents=True, exist_ok=True)
    binary = root / "rg"
    with (root / "rg.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
        def fetch(url, limit):
            with urllib.request.urlopen(url, timeout=20) as response:
                data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("ripgrep download exceeds size limit")
            return data
        release = json.loads(fetch("https://api.github.com/repos/BurntSushi/ripgrep/releases/latest", 2_000_000))
        tag = release["tag_name"]
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tag):
            raise ValueError("invalid ripgrep release tag")
        name = f"ripgrep-{tag}-x86_64-unknown-linux-musl"
        asset, = (a for a in release["assets"] if a["name"] == name + ".tar.gz")
        url = f"https://github.com/BurntSushi/ripgrep/releases/download/{tag}/{name}.tar.gz"
        digest = asset.get("digest", "")
        if asset["browser_download_url"] != url or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("ripgrep asset lacks a verified URL/digest")
        archive = fetch(url, 32_000_000)
        if hashlib.sha256(archive).hexdigest() != digest[7:]:
            raise ValueError("ripgrep checksum mismatch")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = tar.getmember(name + "/rg")
            if not member.isfile() or member.size > 32_000_000:
                raise ValueError("invalid ripgrep archive member")
            data = tar.extractfile(member).read()
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as staged:
            staged.write(data)
        try:
            os.chmod(staged.name, 0o755)
            os.replace(staged.name, binary)
        finally:
            if os.path.exists(staged.name):
                os.unlink(staged.name)
    return str(binary)


def search(base_dir, pattern, files, max_lines):
    base = Path(base_dir).resolve()
    if not base.is_dir():
        return "Error: search base is not a directory"
    if files.startswith("/"):
        return "Error: use a workspace-relative ripgrep glob, or bash for other roots"
    args = [_ripgrep(), "--color=never", "--no-config", "--glob", files]
    if pattern is None:
        args += ["--files"]
    else:
        args += ["--line-number", "--with-filename", "--max-columns", "200",
                 "--max-columns-preview", "--regexp", pattern]
    args += ["--", str(base)]
    lines = []
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(args, cwd=base, stdout=subprocess.PIPE, stderr=errors) as proc:
            try:
                for line in proc.stdout:
                    lines.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))
                    if len(lines) > max_lines:
                        proc.terminate()
                        break
            finally:
                proc.stdout.close()
                proc.wait()
            limited = len(lines) > max_lines
            errors.seek(0)
            error = errors.read(2000).decode("utf-8", errors="replace").strip()
            if not limited and proc.returncode not in (0, 1):
                return "Error: ripgrep: " + (error or f"exit {proc.returncode}")
    header = f"{min(len(lines), max_lines)} results" + (" (limit reached; narrow search, total not counted)" if limited else "")
    if error:
        header += "\nWarning: " + error
    return header + ("\n" + "\n".join(lines[:max_lines]) if lines else "")
'''

_GLOB_SCRIPT = _GREP_SCRIPT = _SEARCH_SCRIPT

# ── read script (runs on sandbox) ───────────────────────────────────
#
# Executed on the sandbox via _run_sandbox_script so the file never
# leaves the sandbox: line selection, numbering, and header formatting
# all happen in-process next to the data.  Args are repr()-embedded at
# call time so arbitrary path strings are safe.

_READ_SCRIPT = r'''
import os
import sys


def read_file(path, start, stop):
    """Read a file and return numbered lines with a header.

    start/stop follow the same semantics as _core_read:
      - positive: 1-indexed, stop is exclusive
      - negative: counts from end
      - 0: start=1, stop=end
    Internal cap: 2000 lines.
    """
    if not os.path.exists(path):
        return f"Error: File not found: {path}"

    # Count in fixed-size chunks, including files consisting of one huge line.
    # A second bounded scan preserves negative indexing without loading the file.
    cap = 50 * 1024
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            total_lines = 0
            last = ""
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                total_lines += chunk.count("\n")
                last = chunk[-1:]
            total_lines += int(bool(last) and last != "\n")
        start_idx = max(0, total_lines + start) if start < 0 else max(0, start - 1)
        end_idx = total_lines + stop if stop < 0 else (stop - 1 if stop else total_lines)
        start_idx = min(start_idx, total_lines)
        end_idx = max(start_idx, min(end_idx, total_lines, start_idx + 2000))
        selected = bytearray()
        line_idx = 0
        clipped = False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            while line_idx < end_idx and not clipped:
                chunk = f.read(8192)
                if not chunk:
                    break
                parts = chunk.split("\n")
                for index, part in enumerate(parts):
                    if line_idx >= end_idx:
                        break
                    terminated = index < len(parts) - 1
                    if line_idx >= start_idx:
                        raw_part = (part + ("\n" if terminated else "")).encode("utf-8")
                        room = cap - len(selected)
                        selected.extend(raw_part[:room])
                        if len(raw_part) > room:
                            clipped = True
                            break
                    if terminated:
                        line_idx += 1
        lines = selected.decode("utf-8", errors="replace").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        numbered = "\n".join(f"{start_idx + i + 1}: {line}" for i, line in enumerate(lines))
        # Line prefixes also count toward the preview budget.
        raw = numbered.encode("utf-8")
        clipped = clipped or len(raw) > cap
        numbered = raw[:cap].decode("utf-8", errors="ignore")
    except OSError as e:
        return f"Error: Cannot read {path}: {e}"
    header = f"File: {path} ({total_lines} lines total)"
    if clipped:
        header += f", requested lines {start_idx + 1}-{end_idx} (preview clipped)"
    elif start_idx > 0 or end_idx < total_lines:
        header += f", showing lines {start_idx + 1}-{end_idx}"
    notice = f"\n[Text preview truncated at 50 KiB. Full file: {path}; use bash byte ranges or expose() for full access.]" if clipped else ""
    return f"{header}\n{numbered}{notice}"
'''


# ── write script (runs on sandbox) ───────────────────────────────────
#
# Receives path and content as repr()-embedded Python literals.
# Creates parent directories, writes the file, and reports byte/line
# counts.  Content is bounded by model output token limits so size is
# not a practical concern.

_WRITE_SCRIPT = r'''
import os


def write_file(path, content):
    """Write content to path, creating parent directories as needed.

    Returns a one-line result string on success or an error string
    starting with "Error:".
    """
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"Error: Cannot create parent directory {parent}: {e}"

    try:
        with open(path, "w") as f:
            f.write(content)
    except OSError as e:
        return f"Error: Cannot write {path}: {e}"

    n_bytes = len(content.encode("utf-8"))
    n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"Wrote {n_bytes} bytes ({n_lines} lines) to {path}"
'''


# ── edit script (runs on sandbox) ────────────────────────────────────
#
# Performs string replacement entirely on the sandbox, avoiding a full
# round-trip download+upload of the file contents.  Args are
# repr()-embedded at call time.

_EDIT_SCRIPT = r'''
import os


def edit_file(path, old_string, new_string, replace_all):
    """Edit a file by exact string replacement.

    Returns a one-line result string on success or an error string
    starting with "Error:".
    """
    if not os.path.exists(path):
        return f"Error: File not found: {path}"

    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"Error: Cannot read {path}: {e}"

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"Error: Found {count} matches for old_string in {path}. "
            f"Provide more surrounding context to identify a unique match, "
            f"or set replace_all=true to replace all occurrences."
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replaced = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replaced = 1

    try:
        with open(path, "w") as f:
            f.write(new_content)
    except OSError as e:
        return f"Error: Cannot write {path}: {e}"

    return f"Replaced {replaced} occurrence(s) in {path}"
'''


# ── shared Daytona I/O helpers ───────────────────────────────────────
#
# Thin wrappers over the Daytona toolbox API endpoints that multiple
# _core_* functions call.  Extracted to avoid duplicating HTTP call
# patterns and error handling across read/write/edit/glob/grep/onboard.


async def _upload_file(valves, sandbox_id: str, client: httpx.AsyncClient,
                       path: str, content: bytes) -> None:
    """Upload raw bytes to a sandbox path (creates parent dirs first)."""
    parent = "/".join(path.rstrip("/").split("/")[:-1])
    if parent:
        await client.post(
            _toolbox(valves, sandbox_id, "/files/folder"),
            headers=_headers(valves),
            json={"path": parent, "mode": "755"},
            timeout=30.0,
        )
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/files/upload"),
        params={"path": path},
        headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
        files={"file": ("file", io.BytesIO(content), "application/octet-stream")},
        timeout=60.0,
    )
    resp.raise_for_status()


class _ToolFailure(RuntimeError):
    """An operation failed, rather than returning successful text containing an error."""


def _checked_result(result: str) -> str:
    # Legacy sandbox scripts have a reserved error prefix. Normalize it once at
    # the internal boundary; user-facing wrappers render prose.
    if result.startswith("Error:"):
        raise _ToolFailure(result.removeprefix("Error:").strip())
    return result


async def _run_sandbox_script(valves, sandbox_id: str, client: httpx.AsyncClient,
                              script: str, *, error_prefix: str,
                              timeout_ms: int = 30000,
                              http_timeout: float = 60.0) -> str:
    """Execute a Python script on the sandbox and return its stdout.

    For short scripts, passes the script inline via ``python3 -c``.
    Returns the script's stdout on success. Non-zero exit and the reserved
    script error prefix raise _ToolFailure, rendered only at user boundaries.
    """
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/process/execute"),
        headers=_headers(valves),
        json={
            "command": f"python3 -c {_shell_quote(script)}",
            "timeout": timeout_ms,
        },
        timeout=http_timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", "")
    exit_code = data.get("exitCode", -1)
    if exit_code != 0:
        raise _ToolFailure(f"{error_prefix} (exit {exit_code}).\n{result[:500]}")
    return _checked_result(result)


# ── shared tool cores ───────────────────────────────────────────────
#
# Each _core_* function encapsulates the I/O logic for a tool, called
# by the Tools class methods. The dependency signature is explicit:
# (valves, sandbox_id, client, **tool_params) -> str.
#
# Docstrings on _core_* are the **single source of truth** for tool
# descriptions and parameter docs. Use :param: format, parsed natively
# by OWUI. _standard_tool copies docstrings onto generated functions;
# hand-written methods (bash) set __doc__ directly.



@lru_cache(maxsize=128)
def _param_adapter(annotated_fn, name):
    # OWUI may exec this module with future annotations enabled.
    hint = typing.get_type_hints(annotated_fn).get(name)
    return hint, TypeAdapter(hint) if hint is not None else None


def _check_tool_params(kwargs: dict, annotated_fn) -> str | None:
    """Strict type check for tool params at the wrapper boundary.

    Pydantic strict mode rejects JSON coercions, including bool-for-int, and
    validates container members. Unlike exact type identity it accepts Python
    subclasses of built-ins, which OWUI's parsed JSON cannot contain.
    Never render ValidationError: inputs AND dictionary-key locations can leak
    secrets. Report only the parameter and its declared type.
    """
    for name, value in kwargs.items():
        hint, adapter = _param_adapter(annotated_fn, name)
        if adapter is None:
            continue
        try:
            adapter.validate_python(value, strict=True)
        except ValidationError:
            expected = str(hint) if typing.get_origin(hint) else hint.__name__
            return f"Error: parameter '{name}' expected type {expected}"
    return None


# Infrastructure params in _core_* signatures that are NOT tool params.
# The _standard_tool factory skips these when building the OWUI-visible
# method signature.
_CORE_INFRA_PARAMS = frozenset({
    "valves", "sandbox_id", "client", "emit", "user_pairs",
    "on_background", "chat_state", "chat_id", "interpreter_state",
})


def _standard_tool(core_fn, *, emit_start: str, emit_done: str,
                   extra_core_kwargs=None):
    """Build a Tools class method that wraps a _core_* function.

    Generates a method with the correct OWUI-visible signature (tool params
    from core_fn + OWUI dunder params), docstring from core_fn, and all
    standard boilerplate (ensure_sandbox, ensure_chat_init, emit, drain
    harness messages).

    The generated method has an explicit ``__signature__`` so
    ``inspect.signature()`` returns the exact parameter list OWUI expects.
    ``functools.wraps`` does NOT copy ``__signature__``; we set it manually.

    Args:
        core_fn: The _core_* function to wrap.
        emit_start: Status message emitted before calling the core.
            May contain ``{kwargs}`` which is replaced with a dict of
            the tool kwargs at call time, useful for messages like
            "Reading {kwargs[path]}...".  Use a plain string without
            ``{kwargs}`` for static messages.
        emit_done: Status message emitted after the core returns.
        extra_core_kwargs: Optional callable(self, sandbox_id, __chat_id__)
            returning a dict of extra kwargs to pass to the core function
            (e.g. interpreter_state and chat_id for interpret).
    """
    # ── Extract tool-visible parameters from the core function ───────
    core_sig = inspect.signature(core_fn)
    tool_params = []
    for name, param in core_sig.parameters.items():
        if name in _CORE_INFRA_PARAMS:
            continue
        # Convert keyword-only params to positional-or-keyword for OWUI
        tool_params.append(
            param.replace(kind=inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )

    # ── Build the synthetic signature ────────────────────────────────
    # self + tool params + OWUI dunder params
    synth_params = [
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ] + tool_params + [
        inspect.Parameter("__user__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default={}),
        inspect.Parameter("__chat_id__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=""),
        inspect.Parameter("__event_emitter__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=None),
    ]
    synth_sig = inspect.Signature(synth_params)

    # Names of tool params for extracting kwargs at call time
    tool_param_names = [p.name for p in tool_params]
    # Resolve annotations for the generated OWUI schema. Runtime validation
    # independently resolves the same core function through _check_tool_params.
    # We resolve via get_type_hints(core_fn), NOT the raw
    # inspect.Parameter.annotation, because OWUI's tool loader
    # (open_webui.utils.plugin.load_tool_module_by_id) execs tool source
    # from within a module that has `from __future__ import annotations`.
    # exec() inherits the caller's __future__ flags, so PEP 563 stringized
    # annotations are in effect: raw signature annotations come back as the
    # STRING 'int', not the class int, which would make _check_tool_params'
    # isinstance() raise "arg 2 must be a type".  get_type_hints() resolves
    # those strings back to real classes against the module globals.
    _core_hints = typing.get_type_hints(core_fn)

    async def _method(self, *args, **kwargs):
        # Bind positional + keyword args to the synthetic signature.
        # Skip 'self' (already bound by Python's method machinery).
        bound = synth_sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        ba = bound.arguments

        # Extract OWUI dunder params
        __user__ = ba.pop("__user__")
        __chat_id__ = ba.pop("__chat_id__")
        __event_emitter__ = ba.pop("__event_emitter__")
        ba.pop("self", None)

        # Remaining args are tool kwargs
        tool_kwargs = {k: ba[k] for k in tool_param_names if k in ba}

        # Strict type check at the wrapper boundary.
        type_err = _check_tool_params(tool_kwargs, core_fn)
        if type_err:
            return type_err

        harness = _HarnessDelivery(self._chat_state, __chat_id__)

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, harness.warning = await _ensure_sandbox(
                self.valves, email, client, __event_emitter__,
            )
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            # Format emit_start with tool kwargs if it contains {kwargs}
            if "{kwargs" in emit_start:
                start_msg = emit_start.format(kwargs=tool_kwargs)
            else:
                start_msg = emit_start
            await _emit(__event_emitter__, start_msg)

            # Build core call kwargs
            call_kwargs = dict(tool_kwargs)
            if extra_core_kwargs:
                call_kwargs.update(
                    extra_core_kwargs(self, sandbox_id, __chat_id__)
                )

            result = await core_fn(
                self.valves, sandbox_id, client, **call_kwargs,
            )
            # Interpreter output is arbitrary user stdout, not error-protocol
            # text. Its failures already raise _ToolFailure.
            if core_fn is not _core_interpret:
                result = _checked_result(result)

            await _emit(__event_emitter__, emit_done, done=True)
            return result

        return await _tool_context(__event_emitter__, _run, harness)

    # ── Set the correct signature, annotations, and docstring ────────
    _method.__signature__ = synth_sig
    # OWUI uses get_type_hints() (which reads __annotations__) for the
    # JSON schema type mapping, NOT inspect.signature().annotation.
    # Without this, all _standard_tool params fall back to "string".
    # Use resolved hints (see tool_annotations above): under OWUI's loader
    # the synth_param annotations are stringized (PEP 563), so prefer the
    # get_type_hints()-resolved class where available.
    _method.__annotations__ = {
        p.name: _core_hints.get(p.name, p.annotation)
        for p in synth_params
        if p.annotation is not inspect.Parameter.empty and p.name != "self"
    }
    _method.__doc__ = inspect.getdoc(core_fn) or ""
    _method.__name__ = core_fn.__name__.replace("_core_", "")
    _method.__qualname__ = f"Tools.{_method.__name__}"

    return _method


async def _core_read(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str, start: int = 1, stop: int = 0) -> str:
    """Read a file from the sandbox. Returns numbered lines, capped at 2000 lines
    and 50 KiB of text. A clipped preview is explicitly labeled; the original
    file remains available via bash byte ranges or expose().
    Supports Python-style negative indexing: start=-10 reads from
    10 lines before the end, stop=-3 stops 3 lines before the end.
    All positive values are 1-indexed. stop is exclusive (half-open).
    stop=0 (default) means end of file.

    :param path: Absolute path to the file.
    :param start: First line to return (1-indexed). Negative counts from end. 0 is treated as 1.
    :param stop: Line to stop before (exclusive, 1-indexed). Negative counts from end. 0 means end of file.
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _READ_SCRIPT
        + f"\nprint(read_file({path!r}, {start!r}, {stop!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="read script failed",
    )


async def _core_write(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                      path: str, content: str) -> str:
    """Write a file to the sandbox (creates parents automatically).

    For large files, prefer a skeleton-then-edit workflow: write the file first
    with overall structure and ``# PLACEHOLDER: <section>`` markers, then use
    ``edit()`` to expand each placeholder incrementally. This makes progress
    visible to the user in real time and keeps partial work on disk if
    generation is interrupted.

    :param path: Absolute path to write to.
    :param content: The full file content.
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _WRITE_SCRIPT
        + f"\nprint(write_file({path!r}, {content!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="write script failed",
    )


async def _core_edit(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str, old_string: str, new_string: str,
                     replace_all: bool = False) -> str:
    """Edit a file by exact string replacement. Fails on ambiguous matches unless replace_all=true.

    :param path: Absolute path to the file.
    :param old_string: Exact text to find.
    :param new_string: Replacement text.
    :param replace_all: Replace all occurrences (default: false).
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _EDIT_SCRIPT
        + f"\nprint(edit_file({path!r}, {old_string!r}, {new_string!r}, {replace_all!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="edit script failed",
    )


async def _core_glob(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     pattern: str, max_lines: int = _GLOB_MAX_LINES) -> str:
    """Search for files in the sandbox workspace by glob pattern.
    Thin ripgrep --files adapter: returns bounded absolute paths, not exhaustive
    counts. Uses rg ignore/hidden-file rules and traversal order.

    :param pattern: One workspace-relative ripgrep glob, e.g. '**/*.py' or '*.{js,ts}'. No comma-separated pattern language. rg supports braces and ! exclusion. Use bash with rg for multiple globs, hidden/ignored files, or other roots.
    :param max_lines: Max concrete result lines, excluding the summary header (default: 100, clamped to 1–500).
    """
    clamped = max(1, min(500, max_lines))
    base_dir = "/home/daytona/workspace"
    script = (
        _GLOB_SCRIPT
        + f"\nprint(search({base_dir!r}, None, {pattern!r}, {clamped!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="glob script failed",
        timeout_ms=120000, http_timeout=150.0,
    )


async def _core_grep(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     pattern: str, files: str = "**/*",
                     max_lines: int = _GREP_MAX_LINES) -> str:
    """Search file contents in the sandbox workspace by regex.
    Thin ripgrep adapter: returns bounded absolute path:line:text hits with
    rg regex, ignore and binary-file semantics. Stops at the result limit,
    without exhaustive counts. Long matches use rg's 200-column preview.

    :param pattern: Ripgrep regex, e.g. 'import.*asyncio' or 'TODO|FIXME'. No Python lookaround/backreferences.
    :param files: One workspace-relative ripgrep glob (default: '**/*'); braces supported. Use bash with rg for multiple globs, hidden/ignored files, or other roots.
    :param max_lines: Max concrete result lines, excluding the summary header (default: 100, clamped to 1–500).
    """
    clamped = max(1, min(500, max_lines))
    base_dir = "/home/daytona/workspace"
    script = (
        _GREP_SCRIPT
        + f"\nprint(search({base_dir!r}, {pattern!r}, {files!r}, {clamped!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="grep script failed",
        timeout_ms=120000, http_timeout=150.0,
    )


# ── view core (model-facing image perception) ───────────────────────
#
# Relies on OWUI's image-return convention (shipped in 0.11.0): a tool
# result string starting with "data:image/<mime>;base64," is moved out of
# the text channel and re-injected as an input_image part, so the model
# sees the image on its next turn.  History replay flattens it back into
# a user image message (convert_output_to_messages with
# flatten_tool_images=True, the open-webui#27126-equivalent fix).
#
# Three hard requirements fall out of that convention:
#   1. The result must START with the data URI.  Any prepended text (e.g.
#      harness messages) breaks OWUI's startswith() detection, and the
#      base64 lands in the model's text context instead.  The Tools
#      wrapper leaves harness messages queued for a later text result.
#   2. On OWUI < 0.11.0 the current turn works but replayed history is
#      NOT flattened — strict providers reject the conversation on later
#      turns.  Only deploy with view() on OWUI >= 0.11.0.
#   3. Injection is NOT capability-gated upstream: a non-vision model
#      receives an image part it cannot consume, and strict providers may
#      then reject every later turn.  The Tools wrapper refuses view() up
#      front for such models (_model_supports_vision), before any sandbox
#      work happens.

_VIEW_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB, under Anthropic's 5 MB/image limit


def _model_supports_vision(model: dict) -> bool:
    """Best-effort vision-capability inference from an OWUI model dict.

    Provider-declared architecture metadata (present on connection models)
    is authoritative.  Otherwise fall back to the admin-declared
    info.meta.capabilities.vision flag.  When neither says otherwise,
    allow: a false refusal would silently disable view() for capable
    models, which is worse than the residual risk of an unmarked
    non-vision model.
    """
    if not isinstance(model, dict):
        return True
    arch = model.get("architecture") or {}
    input_mods = arch.get("input_modalities")
    if isinstance(input_mods, list) and input_mods:
        return "image" in input_mods
    modality = arch.get("modality")
    if isinstance(modality, str) and "->" in modality:
        return "image" in modality.split("->", 1)[0]
    caps = ((model.get("info") or {}).get("meta") or {}).get("capabilities") or {}
    if caps.get("vision") is False:
        return False
    return True


def _sniff_image_mime(header: bytes) -> str | None:
    """Return the MIME type for recognized raster image magic bytes, else None.

    Checks content, not extension: the model can't be trusted to name files
    accurately, and providers reject mislabeled image payloads.  SVG and
    other text formats return None (they are not viewable raster images).
    """
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _core_view(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str) -> str:
    """View an image file from the sandbox, loading it into your visual context.
    Use for screenshots, charts, rendered pages, or generated images.
    Requires a vision-capable model; if the current model cannot accept image
    input, the call is refused with a reminder instead of the image.
    Supported formats: PNG, JPEG, GIF, WebP (detected from file contents, not
    extension). Max 4 MB; downscale larger images first via bash (e.g. with
    ImageMagick or Pillow).

    :param path: Absolute path to the image file.
    """
    err = _require_abs_path(path)
    if err:
        return err
    resp = await client.get(
        _toolbox(valves, sandbox_id, "/files/download"),
        params={"path": path},
        headers=_headers(valves),
        timeout=60.0,
    )
    if resp.status_code == 404:
        return f"Error: File not found: {path}"
    resp.raise_for_status()
    data = resp.content

    mime = _sniff_image_mime(data[:16])
    if mime is None:
        return (
            f"Error: {path} is not a PNG, JPEG, GIF, or WebP image "
            f"(checked file contents, not extension). Convert it to PNG "
            f"first via bash (e.g. with ImageMagick or Pillow). SVG and "
            f"other text/vector formats cannot be viewed directly."
        )
    if len(data) > _VIEW_MAX_BYTES:
        return (
            f"Error: {path} is {_human_size(len(data))}, over the "
            f"{_human_size(_VIEW_MAX_BYTES)} limit for view(). Downscale or "
            f"recompress it via bash (e.g. ImageMagick or Pillow) and view "
            f"the smaller result."
        )
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ── bash core (session + sidecar protocol) ──────────────────────────


def _build_bash_script(command: str, user_pairs: list[tuple[str, str]],
                       pid_path: str, log_path: str, keep_path: str,
                       lease_path: str) -> str:
    """Build the bash wrapper script with sidecar file setup."""
    user_env_lines = "".join(
        f"export {k}={_shell_quote(v)}\n" for k, v in user_pairs
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -e -o pipefail\n"
        "rm -f -- \"${BASH_SOURCE[0]}\"\n"
        + f": > {_shell_quote(lease_path)}\n"
        "export DEBIAN_FRONTEND=noninteractive "
        "GIT_TERMINAL_PROMPT=0 "
        "PIP_NO_INPUT=1 "
        "NPM_CONFIG_YES=true "
        "CI=true\n"
        + user_env_lines
        + "_lathe_record_jobs() {\n"
        + "  _lathe_ec=$?\n"
        + f"  : > {_shell_quote(keep_path)}\n"
        + "  for _lathe_pid in $(jobs -pr); do\n"
        + "    if kill -0 \"$_lathe_pid\" 2>/dev/null && test -r \"/proc/$_lathe_pid/stat\"; then\n"
        + "      _lathe_stat=$(cat \"/proc/$_lathe_pid/stat\")\n"
        + "      read -ra _lathe_fields <<< \"${_lathe_stat##*) }\"\n"
        + f"      echo \"$_lathe_pid:${{_lathe_fields[19]}}\" >> {_shell_quote(keep_path)}\n"
        + "    fi\n"
        + "  done\n"
        + "  return $_lathe_ec\n"
        + "}\n"
        + "trap _lathe_record_jobs EXIT\n"
        + f"echo $BASHPID > {_shell_quote(pid_path)}\n"
        + f"exec > >(tee {_shell_quote(log_path)}) 2>&1\n"
        + command
        + "\n"
    )


_BASH_COMPLETION_LEASE_SECONDS = 960
_BASH_RESULT_RETENTION_SECONDS = 24 * 60 * 60


def _build_bash_reap_script(preserve_log_for: str | None = None,
                            release_lease_for: str | None = None,
                            proc_root: str = "/proc") -> str:
    """Release completed processes independently of the 24-hour result lease."""
    return textwrap.dedent(f"""\
        import os
        import time

        root = {_EPHEMERAL_ROOT + '/cmd'!r}
        proc_root = {proc_root!r}
        release = {release_lease_for!r}
        if os.path.isdir(root):
            for command_id in os.listdir(root):
                command_dir = os.path.join(root, command_id)
                lease_path = os.path.join(command_dir, "lease")
                if command_id == release:
                    try:
                        os.unlink(lease_path)
                    except OSError:
                        pass
                else:
                    try:
                        if time.time() - os.path.getmtime(lease_path) < {_BASH_COMPLETION_LEASE_SECONDS}:
                            continue
                    except OSError:
                        pass
                if not any(os.path.isfile(os.path.join(command_dir, name))
                           for name in ("exit", "keep", "spill", "reap")):
                    continue
                keep_path = os.path.join(command_dir, "keep")
                live = False
                try:
                    with open(keep_path) as f:
                        identities = [line.strip().split(":", 1) for line in f
                                      if ":" in line]
                except OSError:
                    identities = []
                for raw_pid, expected_start in identities:
                    try:
                        pid = int(raw_pid)
                        os.kill(pid, 0)
                        stat = open(os.path.join(proc_root, str(pid), "stat")).read()
                        fields = stat[stat.rfind(")") + 2:].split()
                        if len(fields) > 19 and fields[19] == expected_start:
                            live = True
                            break
                    except (OSError, ValueError, IndexError):
                        pass
                for name in ("sh", "pid"):
                    try:
                        os.unlink(os.path.join(command_dir, name))
                    except OSError:
                        pass
                if live:
                    print("retain " + command_id)
                else:
                    try:
                        os.unlink(keep_path)
                    except OSError:
                        pass
                    open(os.path.join(command_dir, "reap"), "a").close()
                    if not os.path.isfile(os.path.join(command_dir, "session_done")):
                        print("delete " + command_id)
                    try:
                        evidence = os.path.join(command_dir, "exit")
                        if not os.path.exists(evidence):
                            evidence = os.path.join(command_dir, "reap")
                        completed = os.path.getmtime(evidence)
                        if time.time() - completed >= {_BASH_RESULT_RETENTION_SECONDS}:
                            print("expire " + command_id)
                    except OSError:
                        pass
        """)


async def _reap_bash_commands(valves, sandbox_id: str, client: httpx.AsyncClient,
                              preserve_log_for: str | None = None,
                              release_lease_for: str | None = None) -> None:
    """Delete completed sessions; expire result files only after their lease."""
    try:
        result = await _run_sandbox_script(
            valves, sandbox_id, client,
            _build_bash_reap_script(preserve_log_for, release_lease_for),
            error_prefix="bash cleanup failed",
        )
        if result.startswith("Error:"):
            logger.warning("%s", result)
            return
        cleaned = []
        failed_cleanup = set()
        for line in result.splitlines():
            action, _, command_id = line.partition(" ")
            if action not in ("delete", "expire") or not command_id:
                continue
            if action == "delete":
                response = await client.delete(
                    _toolbox(valves, sandbox_id, f"/process/session/lathe-cmd-{command_id}"),
                    headers=_headers(valves), timeout=30.0,
                )
                if response.status_code not in (204, 404):
                    logger.warning("bash cleanup: session %s returned HTTP %s",
                                   command_id, response.status_code)
                    failed_cleanup.add(command_id)
                else:
                    cleaned.append(command_id)
            else:
                if command_id in failed_cleanup:
                    # Keep the retry record until session release succeeds.
                    continue
                response = await client.delete(
                    _toolbox(valves, sandbox_id, "/files/"),
                    params={"path": _bash_sidecar_dir(command_id), "recursive": "true"},
                    headers=_headers(valves), timeout=30.0,
                )
                if response.status_code not in (204, 404):
                    logger.warning("bash cleanup: sidecar %s returned HTTP %s",
                                   command_id, response.status_code)
        if cleaned:
            # A successful session release is recorded separately from evidence
            # expiry, avoiding repeated HTTP deletes for every retained result.
            script = f"import os\nroot={_EPHEMERAL_ROOT + '/cmd'!r}\n"
            script += f"for command_id in {cleaned!r}:\n"
            script += "    path = os.path.join(root, command_id, 'session_done')\n"
            script += "    if os.path.isdir(os.path.dirname(path)):\n        open(path, 'a').close()\n"
            await _run_sandbox_script(valves, sandbox_id, client, script,
                                      error_prefix="bash session cleanup marker failed")
    except Exception as exc:
        logger.warning("bash cleanup failed: %s", exc)


async def _discard_bash_setup(valves, sandbox_id: str, client: httpx.AsyncClient,
                              command_id: str, session_created: bool) -> None:
    """Best-effort cleanup when a command could not be launched."""
    try:
        await client.delete(
            _toolbox(valves, sandbox_id, "/files/"),
            params={"path": _bash_sidecar_dir(command_id), "recursive": "true"},
            headers=_headers(valves), timeout=30.0,
        )
        if session_created:
            await client.delete(
                _toolbox(valves, sandbox_id, f"/process/session/lathe-cmd-{command_id}"),
                headers=_headers(valves), timeout=30.0,
            )
    except Exception as exc:
        logger.warning("bash setup cleanup failed: %s", exc)


def _format_bash_result(output: str, exit_code: int | None,
                        was_truncated: bool, meta: dict,
                        spill_path: str | None = None,
                        background_info: dict | None = None) -> str:
    """Format bash output for return to the caller."""
    if background_info is not None:
        # Auto-backgrounded: command still running
        if not output.strip():
            output = "(no output yet)"
        elapsed = background_info["elapsed"]
        cmd_id = background_info["cmd_id"]
        bg_notice = (
            f"\n\n[Backgrounded after {elapsed}s — command is still running]\n"
            f"CMD={cmd_id}\n"
            f"Ref {_EPHEMERAL_ROOT}/cmd/$CMD/{{pid,log,exit}}\n"
            f"See lathe(manpage=\"background\") for peek/poll/kill recipes.\n"
            f"Tell the user the command is running. Don't poll until they ask or "
            f"you have a concrete reason to expect completion."
        )
        return output + bg_notice

    # Finished command
    if exit_code is not None and exit_code != 0:
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


async def _read_bash_log_tail(valves, sandbox_id, client, log_path, *,
                              max_bytes=_MAX_BYTES, allow_missing=False):
    """Read bounded raw log text and its full byte size, without presentation."""
    script = textwrap.dedent(f"""\
        import json, os
        path = {log_path!r}
        if {allow_missing!r} and not os.path.exists(path):
            print(json.dumps({{'text': '', 'size': 0}}))
            raise SystemExit(0)
        with open(path, 'rb') as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - {max_bytes}))
            data = f.read({max_bytes})
        print(json.dumps({{'text': data.decode('utf-8', errors='replace'), 'size': size}}))
        """)
    return json.loads(await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix=f"bash log preview failed; full log: {log_path}",
    ))


async def _bash_log_preview(valves, sandbox_id, client, log_path, *, allow_missing=False):
    """Format a bounded foreground preview with recovery information."""
    data = await _read_bash_log_tail(
        valves, sandbox_id, client, log_path, allow_missing=allow_missing,
    )
    # Replacement decoding can expand invalid UTF-8, so bound the decoded text too.
    decoded_bytes = data["text"].encode("utf-8")
    text = decoded_bytes[-_MAX_BYTES:].decode("utf-8", errors="ignore")
    output, truncated, meta = _truncate_tail(text)
    if data["size"] > _MAX_BYTES or len(decoded_bytes) > _MAX_BYTES:
        output += f"\n[Bounded tail preview, may begin mid-line. Full output ({_human_size(data['size'])}): {log_path}]"
        # The fetched tail does not know the full file's line count. Its byte
        # notice is authoritative; do not relabel tail-relative lines as global.
        return output, False, {}
    return output, truncated, meta


async def _core_bash(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     command: str, workdir: str = "/home/daytona/workspace",
                     user_pairs: list[tuple[str, str]],
                     foreground_seconds: int = 30,
                     emit=None,
                     on_background=None) -> str:
    """Execute a bash command in the sandbox. Non-interactive only.
    Each call uses a fresh non-login Bash shell; rc files are not sourced and
    shell state (such as exports and cd) does not persist between calls.
    Runs with set -e -o pipefail: an unhandled command or pipeline failure can
    abort the remaining script. Handle expected probe failures explicitly
    (for example, with if); use && for commands with dependent prerequisites.
    Nonzero command exits return an exit status and captured output, distinct
    from errors launching or observing execution.
    Commands that finish within the foreground window return output directly.
    Long-running commands auto-background and return a descriptor with log
    file paths for monitoring. Text previews are capped at 50 KiB. Full logs and
    exit status remain readable for 24 hours after completion, until a later
    bash call reaps expired evidence. Sandbox stop/restart clears these files.

    When creating git commits, add an Assisted-by: Lathe trailer to the
    commit message to acknowledge AI assistance.

    For first-time application setup, read lathe(manpage="services") before
    installing or launching; it routes to relevant skills and the configured
    handbook. Inspection commands may come first.

    :param command: The bash command to execute.
    :param workdir: Working directory (default: /home/daytona/workspace).
    :param foreground_seconds: Seconds to wait before auto-backgrounding. Omit or set -1 to use the server default. Set 0 for immediate background (fire-and-forget). Use higher positive values for known-slow commands.
    """
    cmd_id = str(uuid.uuid4())
    cmd_dir = _bash_sidecar_dir(cmd_id)
    log_path = f"{cmd_dir}/log"
    pid_path = f"{cmd_dir}/pid"
    exit_path = f"{cmd_dir}/exit"
    script_path = f"{cmd_dir}/sh"
    keep_path = f"{cmd_dir}/keep"
    lease_path = f"{cmd_dir}/lease"

    await _reap_bash_commands(valves, sandbox_id, client)
    script = _build_bash_script(
        command, user_pairs, pid_path, log_path, keep_path, lease_path,
    )

    session_id = f"lathe-cmd-{cmd_id}"
    session_created = False
    try:
        # Each command gets a session so concurrent calls never queue.
        resp = await client.post(
            _toolbox(valves, sandbox_id, "/files/upload"),
            params={"path": script_path},
            headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
            files={"file": ("file", io.BytesIO(script.encode("utf-8")), "application/octet-stream")},
            timeout=60.0,
        )
        resp.raise_for_status()
        resp = await client.post(
            _toolbox(valves, sandbox_id, "/process/session"),
            headers=_headers(valves), json={"sessionId": session_id}, timeout=30.0,
        )
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()
        session_created = True

        # Session exec has no cwd parameter, so change directory explicitly.
        exec_command = (
            f"cd {_shell_quote(workdir)} && "
            f"bash {script_path}; EC=$?; "
            f"echo $EC > {_shell_quote(exit_path)}; "
            f"(exit $EC)"
        )
        resp = await client.post(
            _toolbox(valves, sandbox_id, f"/process/session/{session_id}/exec"),
            headers=_headers(valves),
            json={"command": exec_command, "runAsync": True}, timeout=30.0,
        )
        resp.raise_for_status()
    except BaseException:
        await _discard_bash_setup(
            valves, sandbox_id, client, cmd_id, session_created,
        )
        raise
    session_cmd_id = resp.json().get("cmdId", "")

    # ── Foreground polling window ────────────────────────────────
    # 0 = immediate background (fire-and-forget).  Positive values
    # are clamped to [1, 300].
    if foreground_seconds <= 0:
        fg_timeout = 0.01  # single poll iteration, then background
    else:
        fg_timeout = max(1, min(300, foreground_seconds))
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
            _toolbox(valves, sandbox_id, f"/process/session/{session_id}"),
            headers=_headers(valves),
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
            await _emit(emit, f"Running... ({elapsed}s)")
            last_status_at = now

    # ── Fetch logs ──────────────────────────────────────────────
    try:
        output, was_truncated, meta = await _bash_log_preview(
            valves, sandbox_id, client, log_path, allow_missing=not finished,
        )
    except Exception as exc:
        logger.warning("bash preview unavailable: %s", exc)
        output = f"[Preview unavailable. Full output: {log_path}; exit: {exit_path}]"
        was_truncated, meta = False, {}

    if not finished:
        elapsed = int(time.time() - (deadline - fg_timeout))
        if on_background:
            on_background(sandbox_id=sandbox_id, session_id=session_id,
                          session_cmd_id=session_cmd_id, cmd_id=cmd_id,
                          start_time=deadline - fg_timeout)
        return _format_bash_result(
            output, exit_code, was_truncated, meta,
            background_info={"elapsed": elapsed, "cmd_id": cmd_id},
        )

    # Command finished within foreground window
    spill_path = log_path if was_truncated else None
    formatted = _format_bash_result(
        output, exit_code, was_truncated, meta,
        spill_path=spill_path,
    )
    await _reap_bash_commands(
        valves, sandbox_id, client,
        preserve_log_for=cmd_id if was_truncated else None,
        release_lease_for=cmd_id,
    )
    return formatted


# ── Background bash completion polling ───────────────────────────────
#
# When a bash command auto-backgrounds, the outer bash() Tool method
# spawns this coroutine to poll for completion and push a notice into
# the chat's harness message queue.  The next tool call in the same
# chat will see it prepended to the result.
#
# Max poll duration is 10 minutes.  If the command takes longer, the
# notice is silently dropped (the model can still poll sidecar files).

_BG_BASH_POLL_MAX_SECONDS = 600

async def _poll_bg_bash(valves, sandbox_id: str, session_id: str,
                        session_cmd_id: str, cmd_id: str, start_time: float,
                        chat_state: LRUCache, chat_id: str):
    """Poll a backgrounded bash command and push a completion notice."""
    poll_interval = 2.0
    deadline = time.time() + _BG_BASH_POLL_MAX_SECONDS
    exit_code = None

    async with httpx.AsyncClient() as poll_client:
        try:
            while time.time() < deadline:
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 10.0)
                try:
                    resp = await poll_client.get(
                        _toolbox(valves, sandbox_id, f"/process/session/{session_id}"),
                        headers=_headers(valves),
                        timeout=15.0,
                    )
                    if resp.status_code != 200:
                        continue
                    commands = resp.json().get("commands", [])
                    for cmd in commands:
                        if cmd.get("id") == session_cmd_id:
                            ec = cmd.get("exitCode")
                            if ec is not None:
                                exit_code = ec
                            break
                    else:
                        continue
                    if exit_code is not None:
                        break
                except Exception:
                    continue  # transient network error, keep polling
            else:
                return  # timed out, silently give up

            # Fetch tail of log for the notice
            elapsed = int(time.time() - start_time)
            tail = ""
            try:
                data = await _read_bash_log_tail(
                    valves, sandbox_id, poll_client, f"{_bash_sidecar_dir(cmd_id)}/log",
                    max_bytes=2048,
                )
                tail = data["text"]
            except Exception as e:
                logger.debug("bg-poll: failed to fetch log tail: %s", e)
                tail = "(Preview unavailable; use the full log path below.)"

            notice = _format_bg_bash_notice(cmd_id, exit_code, elapsed, tail)
            await _reap_bash_commands(
                valves, sandbox_id, poll_client, release_lease_for=cmd_id,
            )
            _push_bg_notice(chat_state, chat_id, notice)

        except Exception as e:
            logger.debug("bg-poll: poller failed: %s", e)


# ── Code interpreter ─────────────────────────────────────────────────
#
# The Daytona toolbox exposes a persistent Python REPL via a WebSocket
# endpoint.  Variables, imports, and definitions survive across calls
# within the same "context". Interpreter identity lives separately from
# disposable chat notices, for the lifetime of one loaded Tools instance.
# If the sandbox restarts (killing the interpreter process), we detect the stale
# context, create a fresh one, and warn the model that state was lost.
#
# The WS protocol:
#   1. Connect to /process/interpreter/execute (HTTP upgrade)
#   2. Send one JSON frame: {code, contextId, timeout}
#   3. Receive N JSON frames: {type: "stdout"|"stderr"|"error", text, ...}
#   4. Server sends WS close: code 1000 = normal, 4008 = timeout
#
# httpx-ws (aconnect_ws) handles the upgrade using the same
# httpx.AsyncClient we already have, keeping the HTTP stack cohesive.

_INTERPRET_DEFAULT_TIMEOUT = 120  # seconds; server default is 600


async def _ensure_interpreter_context(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    interpreter_state: dict, chat_id: str,
) -> tuple[str, bool]:
    """Resolve identity under a per-chat lock; only a valid list proves absence.

    Records are intentionally non-evicting, scoped to one Tools instance.
    A failed/ambiguous probe must never replace a healthy interpreter.
    The lock covers identity resolution, not execution of user Python code.
    """
    if not chat_id:
        raise ValueError("interpret() requires a conversation ID")
    state = interpreter_state.setdefault(chat_id, {"lock": asyncio.Lock()})
    async with state["lock"]:
        ctx_id = state.get("id")
        if ctx_id and state["sandbox_id"] == sandbox_id:
            resp = await client.get(
                _toolbox(valves, sandbox_id, "/process/interpreter/context"),
                headers=_headers(valves),
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            contexts = data.get("contexts") if isinstance(data, dict) else None
            if not isinstance(contexts, list) or any(
                not isinstance(c, dict) or not isinstance(c.get("id"), str)
                or not c["id"] for c in contexts
            ):
                raise RuntimeError("Daytona returned an invalid interpreter context list")
            if any(c["id"] == ctx_id for c in contexts):
                return ctx_id, False

        # Authoritative absence, a different sandbox, or first use.
        resp = await client.post(
            _toolbox(valves, sandbox_id, "/process/interpreter/context"),
            headers=_headers(valves),
            json={"language": "python", "cwd": "/home/daytona/workspace"},
            timeout=30.0,
        )
        resp.raise_for_status()
        new_id = resp.json().get("id")
        if not isinstance(new_id, str) or not new_id:
            raise RuntimeError("Daytona returned an interpreter context with no ID")
        state.update(id=new_id, sandbox_id=sandbox_id)
        return new_id, ctx_id is not None


def _format_interpret_result(stdout: str, stderr: str,
                             errors: list[dict], lost_state: bool) -> str:
    """Format the result of an interpret() call for the model."""
    parts = []

    if lost_state:
        parts.append(
            "[Warning: the interpreter session was reset (sandbox restarted). "
            "All previously defined variables, imports, and state are gone. "
            "Re-run any necessary setup.]"
        )

    if errors:
        for err in errors:
            name = err.get("name", "Error")
            value = err.get("value", "")
            tb = err.get("traceback", "")
            if tb:
                parts.append(tb.rstrip())
            else:
                parts.append(f"{name}: {value}")

    if stdout.strip():
        parts.append(stdout.rstrip())

    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip()}")

    if not parts:
        parts.append("(no output)")

    return "\n\n".join(parts)


async def _core_interpret(valves, sandbox_id: str, client: httpx.AsyncClient,
                          interpreter_state: dict, chat_id: str, *,
                          code: str, timeout: int = _INTERPRET_DEFAULT_TIMEOUT) -> str:
    """Run Python code in a persistent REPL session. Variables, imports, and
    definitions survive across calls within the same conversation.
    Use this for iterative data exploration, incremental computation, or
    any workflow where you need state to persist between executions.
    For one-shot shell commands or non-Python work, prefer bash() instead.
    Stream output is a 50 KiB preview; excess output is discarded explicitly.
    Write large results to a sandbox file for full access via read() or expose().

    :param code: Python code to execute. Top-level await is not supported.
    :param timeout: Max execution time in seconds (default: 120). Use 0 for no limit.
    """
    from httpx_ws import aconnect_ws, WebSocketDisconnect

    if not code.strip():
        raise _ToolFailure("empty code. Provide Python code to execute.")

    context_id, lost_state = await _ensure_interpreter_context(
        valves, sandbox_id, client, interpreter_state, chat_id,
    )

    # Build the WS URL from the proxy base (same host, /process/interpreter/execute).
    ws_url = _toolbox(valves, sandbox_id, "/process/interpreter/execute")

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    errors: list[dict] = []
    timed_out = False
    preview_bytes = 0
    preview_clipped = False
    execution_failed = False

    try:
        async with aconnect_ws(
            ws_url, client,
            keepalive_ping_interval_seconds=20,
            queue_size=1,
            headers=_headers(valves),
        ) as ws:
            # Send the execute request.
            await ws.send_json({
                "code": code,
                "contextId": context_id,
                "timeout": timeout if timeout > 0 else 0,
            })

            # Receive output frames until the server closes the connection.
            while True:
                try:
                    msg = await ws.receive_json()
                except WebSocketDisconnect as exc:
                    if exc.code == 4008:
                        timed_out = True
                    break

                msg_type = msg.get("type", "")
                if msg_type not in ("stdout", "stderr", "error"):
                    continue
                if msg_type == "error":
                    execution_failed = True
                    text = msg.get("traceback") or f"{msg.get('name', 'Error')}: {msg.get('value', '')}"
                else:
                    text = msg.get("text", "")
                raw = text.encode("utf-8")
                room = _MAX_BYTES - preview_bytes
                clipped = raw[:room].decode("utf-8", errors="ignore")
                preview_bytes += len(clipped.encode("utf-8"))
                preview_clipped = preview_clipped or len(raw) > room
                if clipped:
                    if msg_type == "stdout":
                        stdout_parts.append(clipped)
                    elif msg_type == "stderr":
                        stderr_parts.append(clipped)
                    else:
                        errors.append({"traceback": clipped})

    except Exception as e:
        raise _ToolFailure(f"Error connecting to interpreter: {e}") from e

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    result = _format_interpret_result(stdout, stderr, errors, lost_state)
    if timed_out:
        result = f"[Execution timed out after {timeout}s]\n\n{result}"
    if preview_clipped:
        result += "\n[Text preview truncated at 50 KiB; excess stream output was discarded. For full output, write to a sandbox file in your Python code and use read() or expose().]"
    if timed_out or execution_failed:
        raise _ToolFailure(result)

    return result


_SKILL_CATALOG_SCRIPT = textwrap.dedent("""\
    # Shared discovery for explicit onboarding and the first-call snapshot.
    LATHE = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "lathe")
    sections.append(
        "# Skill Locations\\n\\n"
        "Scanning global skills via " + os.path.join(GLOBAL, "skills", "<skill-name>", "SKILL.md") + "\\n"
        "Scanning Lathe skills via " + os.path.join(LATHE, "skills", "<skill-name>", "SKILL.md") + "\\n"
        "Scanning project skills via " + os.path.join(PROJECT, ".agents", "skills", "<skill-name>", "SKILL.md")
    )

    skills = []
    for base in (GLOBAL, LATHE, os.path.join(PROJECT, ".agents")):
        skills_dir = os.path.join(base, "skills")
        if not os.path.isdir(skills_dir):
            continue
        # Bounded records, no bodies, dependencies, installation, or networking.
        paths = sorted(glob.glob(os.path.join(skills_dir, "*/SKILL.md")))
        if len(paths) > 256:
            sections.append("Skill discovery limited to the first 256 files in " + skills_dir)
        for skill_md in paths[:256]:
            metadata = ""
            try:
                with open(skill_md, encoding="utf-8") as f:
                    lines = f.read(16385).splitlines()
            except (OSError, UnicodeError):
                continue
            # Only frame the YAML here. The OWUI host owns safe YAML parsing.
            if lines and lines[0].strip() == "---":
                for index, line in enumerate(lines[1:], 1):
                    if line.strip() == "---":
                        candidate = "\\n".join(lines[1:index])
                        if len(candidate) <= 16384:
                            metadata = candidate
                        break
            skills.append({"path": skill_md, "metadata": metadata})
""")


def _render_context(result: str) -> str:
    """Render sandbox records with host-only YAML; bad metadata stays readable."""
    context = json.loads(result)
    skills = {}
    # Records arrive in global, Lathe, project order: later names override.
    for record in context["skills"]:
        path = record["path"]
        name = path.rsplit("/", 2)[-2]
        desc = "Metadata unavailable; read the skill for instructions."
        try:
            metadata = yaml.safe_load(record["metadata"])
            if isinstance(metadata, dict):
                if isinstance(metadata.get("name"), str) and metadata["name"].strip():
                    name = " ".join(metadata["name"].split())
                if isinstance(metadata.get("description"), str):
                    desc = " ".join(metadata["description"].split())
        except (yaml.YAMLError, ValueError, OverflowError, RecursionError):
            pass
        skills[name] = (desc, path)
    sections = context["sections"]
    if skills:
        lines = ["# Available Skills", "",
                 "Load a skill's full instructions with read(path) when the task matches its description.", ""]
        for name, (desc, path) in skills.items():
            lines.extend([f"- **{name}**: {desc}", f"  `{path}`"])
        sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)


def _build_onboard_script(project_path: str) -> str:
    """Build a Python script that collects agent context from a sandbox.

    Always produces a flat directory listing of project_path (one level,
    absolute paths, dirs marked with trailing /).  Additionally searches
    two locations for agent context:
      1. ~/.agents/          – global agent instructions and skills
      2. <project_path>/     – project-local instructions and skills

    Existing Lathe-specific skills under XDG_DATA_HOME/lathe/skills override
    shared global skills; project skills override both. Discovery is read-only.
    """
    # The script is a self-contained Python program executed on the sandbox.
    # project_path is injected via repr() so it's safely quoted as a
    # Python string literal.  The rest of the script uses no interpolation.
    return (
        "import os, glob, json\n\nPROJECT = " + repr(project_path)
        + "\n" + textwrap.dedent("""\
        GLOBAL  = os.path.expanduser("~/.agents")

        sections = []

        # ── Directory listing (flat, one level) ──────────────────────

        if os.path.isdir(PROJECT):
            entries = sorted(os.listdir(PROJECT))
            lines = []
            for e in entries:
                full = os.path.join(PROJECT, e)
                lines.append(full + "/" if os.path.isdir(full) else full)
            if lines:
                sections.append("# Directory: " + PROJECT + "\\n\\n" + "\\n".join(lines))
            else:
                sections.append("# Directory: " + PROJECT + "\\n\\n(empty)")
        else:
            sections.append("# Directory: " + PROJECT + "\\n\\n(not found)")

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

        project_md = read_agents_md(PROJECT, "Project Agent Instructions")
        if project_md:
            sections.append(project_md)

    """)
        + _SKILL_CATALOG_SCRIPT
        + textwrap.dedent("""\
        # ── Output ───────────────────────────────────────────────────

        print(json.dumps({"sections": sections, "skills": skills}))
    """)
    )


# ── chat auto-init (first tool call per conversation) ───────────────
#
# On the first tool call in a new chat, run a lightweight snapshot of the
# workspace and sandbox environment.  The result is prepended to the tool
# output as a harness message, giving the model orientation context without
# a dedicated tool call.  Subsequent calls in the same chat skip the
# snapshot (tracked by chat_id in an LRU cache on the Tools instance).
#
# The snapshot is deliberately lighter than onboard(): no AGENTS.md or
# full skill bodies, no deep directory scan. Just enough to eliminate the
# 2-5 exploratory turns models typically spend probing "what's here?" and
# "what's installed?".

def _build_snapshot_script(project_path: str) -> str:
    """Build first-call orientation with the same skill catalog as onboard()."""
    return (
        "import subprocess, os, glob, json\n\nPROJECT = " + repr(project_path)
        + "\n" + textwrap.dedent("""\
    GLOBAL = os.path.expanduser("~/.agents")
    sections = []
    """)
        + _SKILL_CATALOG_SCRIPT
        + textwrap.dedent("""\
    # ── Directory listing (flat, one level) ──────────────────────
    for label, path in [("Workspace", PROJECT)]:
        if os.path.isdir(path):
            entries = sorted(os.listdir(path))
            lines = []
            for e in entries:
                full = os.path.join(path, e)
                lines.append(full + "/" if os.path.isdir(full) else full)
            if lines:
                sections.append(label + ":\\n" + "\\n".join(lines))

    # ── System info ──────────────────────────────────────────────
    info_lines = []
    for label, cmd in [
        ("OS", ["uname", "-a"]),
        ("Python", ["python3", "--version"]),
        ("Node", ["node", "--version"]),
    ]:
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, timeout=5
            ).decode().strip()
            info_lines.append(f"{label}: {out}")
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired):
            pass
    if info_lines:
        sections.append("\\n".join(info_lines))

    print(json.dumps({"sections": sections, "skills": skills}))
    """)
    )


_SNAPSHOT_SCRIPT = _build_snapshot_script("/home/daytona/workspace")


def _installation_guidance(valves) -> str:
    """Render admin-supplied references as text, without fetching them."""
    parts = []
    if valves.onboarding_instructions.strip():
        parts.append(
            "## Installation-specific guidance from your OWUI administrator\n\n"
            + valves.onboarding_instructions
        )
    if valves.handbook_url.strip():
        parts.append(
            "## Optional agent handbook\n\n"
            + valves.handbook_url.strip()
            + "\nFor first-time application setup without an applicable skill, read the "
            "index and relevant application page before installing or launching. Lathe does not "
            "fetch it for you. The URL does not guarantee network access; if unreachable, "
            "continue with available skills and local resources."
        )
    return "\n\n".join(parts)


async def _chat_auto_init(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    chat_state: LRUCache, chat_id: str,
    user: dict,
    emitter=None,
) -> None:
    """Run the first-call-per-chat environment snapshot.

    Deposits the snapshot as a pending harness message in the chat state.
    Non-fatal: silently swallows errors so it never blocks the actual
    tool call.  Also fetches sandbox resource metadata from the Daytona
    API (cpu, memory, disk, region) without an extra sandbox-side command.
    """
    parts: list[str] = ["[Auto-init: first tool call in this conversation]"]
    parts.append(
        'For first-time application setup, read lathe(manpage="services") '
        "before installing or launching."
    )
    guidance = _installation_guidance(valves)
    if guidance:
        parts.append(guidance)

    # ── Sandbox-side snapshot (workspace listing + versions) ─────
    try:
        resp = await client.post(
            _toolbox(valves, sandbox_id, "/process/execute"),
            headers=_headers(valves),
            json={"command": f"python3 -c {_shell_quote(_SNAPSHOT_SCRIPT)}", "timeout": 10000},
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("exitCode") == 0 and data.get("result", "").strip():
                parts.append(_render_context(data["result"]))
    except Exception as e:
        logger.debug("auto-init: snapshot failed: %s", e)

    # ── Sandbox metadata from Daytona API ────────────────────────
    try:
        resp = await client.get(
            _api(valves, f"/sandbox/{sandbox_id}"),
            headers=_headers(valves),
            timeout=15.0,
        )
        if resp.status_code == 200:
            sb = resp.json()
            cpu = sb.get("cpu")
            mem = sb.get("memory")
            disk = sb.get("disk")
            region = sb.get("region")
            meta_parts = []
            if cpu is not None:
                meta_parts.append(f"{cpu} vCPU")
            if mem is not None:
                meta_parts.append(f"{mem} GB RAM")
            if disk is not None:
                meta_parts.append(f"{disk} GB disk")
            if region:
                meta_parts.append(f"region: {region}")
            if meta_parts:
                parts.append("Sandbox: " + ", ".join(meta_parts))
    except Exception as e:
        logger.debug("auto-init: sandbox metadata fetch failed: %s", e)

    # ── User env var names for auto-init (values stay out of this message) ──
    try:
        user_valves = user.get("valves")
        if user_valves:
            raw_env = getattr(user_valves, "env_vars", "") or ""
            pairs = _parse_env_vars(raw_env)
            if pairs:
                names = ", ".join(k for k, _v in pairs)
                parts.append(f"User env vars: {names}")
    except Exception as e:
        logger.debug("auto-init: env var parsing failed: %s", e)

    if len(parts) > 1:
        state = chat_state[chat_id]
        state["pending"].append("\n".join(parts))


async def _ensure_chat_init(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    chat_state: LRUCache, chat_id: str | None,
    user: dict,
    emitter=None,
) -> None:
    """Ensure auto-init has run for this chat.  No-op if chat_id is None
    (older OWUI versions that don't pass it) or if already initialized."""
    if not chat_id:
        return
    if chat_id in chat_state and chat_state[chat_id].get("init"):
        return
    # First tool call in this chat: create state and run snapshot.
    chat_state[chat_id] = {"init": True, "pending": []}
    await _chat_auto_init(
        valves, sandbox_id, client, chat_state, chat_id, user, emitter,
    )


async def _wait_for_toolbox(valves, sandbox_id: str, client: httpx.AsyncClient, emitter=None, *, wait: bool = True):
    """Ensure the workspace exists, retrying toolbox startup only during lifecycle work."""
    attempts = 30 if wait else 1
    for attempt in range(attempts):
        try:
            resp = await client.post(
                _toolbox(valves, sandbox_id, "/process/execute"),
                headers=_headers(valves),
                json={"command": "bash -c 'mkdir -p /home/daytona/workspace'", "timeout": 5000},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("exitCode") == 0:
                    return
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.debug("wait-for-toolbox: attempt %d failed: %s", attempt, e)
        if not wait:
            raise RuntimeError(
                "Sandbox was started but its workspace could not be prepared. "
                "Retry this tool call; if it persists, ask the administrator to inspect the sandbox."
            )
        await asyncio.sleep(1)
        if attempt == 2:
            await _emit(emitter, "Waiting for sandbox to become ready...")
    raise RuntimeError("Sandbox started but toolbox could not prepare the workspace (30 attempts)")


_SANDBOX_START_STATES = frozenset({"stopped", "paused", "archived"})
_SANDBOX_WAIT_STATES = frozenset({
    # Provisioning: never interrupt creation with a start request.
    "unknown", "pending_build", "pulling_snapshot", "building_snapshot", "creating",
    # Start/resume and lifecycle operations converge without intervention.
    "starting", "resuming", "restoring", "stopping", "pausing", "archiving",
    "resizing", "snapshotting", "forking",
})
_SANDBOX_DELETION_STATES = frozenset({
    "deleting", "deleted", "destroying", "destroyed",
})


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
    sandboxes = _extract_sandbox_list(resp.json())

    matches = [s for s in sandboxes if s.get("labels", {}).get(label_key) == email]

    if len(matches) > 1:
        raise RuntimeError(_multiple_sandboxes_error(label_key, email, matches))

    # The label list is an eventually consistent discovery index. Its only
    # permitted live cardinality is zero or one, so this adds at most one
    # authoritative per-ID request per tool call.
    sandbox = (
        await _get_live_sandbox(valves, matches[0]["id"], client)
        if matches else None
    )
    warning: str | None = None
    # No readiness cache: only a sandbox found started by the authoritative
    # per-ID lookup takes the steady-state path. Creation (including a losing
    # concurrent create) and any observed transition still wait for toolbox.
    wait_for_startup = sandbox is None or sandbox.get("state") != "started"

    if sandbox is None:
        if not valves.auto_create_sandbox:
            msg = valves.sandbox_missing_message.strip()
            raise RuntimeError(
                msg or (
                    f"No sandbox exists for {email} and automatic creation is "
                    f"disabled on this deployment. A sandbox must be provisioned "
                    f"separately before lathe's tools can be used."
                )
            )

        # 2. Build the create request. Admin overrides layer over lathe's
        # tunable defaults (language, autoStop/Archive/Delete).
        # Identity keys are forced last; volume overrides are forbidden so a
        # misconfigured override can never break the per-user lookup
        # invariant. _parse_create_overrides already rejects those keys,
        # but forcing them here is belt-and-suspenders.
        overrides = _parse_create_overrides(valves.sandbox_create_overrides)
        sandbox_name = f"{label_key}/{email}"
        create_json: dict = {
            "autoStopInterval": valves.auto_stop_minutes,
            "autoArchiveInterval": valves.auto_archive_minutes,
            "autoDeleteInterval": valves.auto_delete_minutes,
            **overrides,
            "name": sandbox_name,
            "labels": {label_key: email},
        }
        # 3. Create new sandbox (workspace persists across stop/archive)
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, "/sandbox"),
            headers=_headers(valves),
            json=create_json,
            timeout=30.0,
        )
        if resp.status_code == 409:
            # Daytona's control-plane source makes (organizationId, name) unique
            # and translates duplicate inserts to 409; labels only have a
            # non-unique GIN index.
            # Evidence: https://github.com/daytonaio/daytona/blob/b5a5d9e78d76c8bcf351f2049620250e0f34eea4/apps/api/src/sandbox/entities/sandbox.entity.ts#L31-L55
            # and https://github.com/daytonaio/daytona/blob/b5a5d9e78d76c8bcf351f2049620250e0f34eea4/apps/api/src/sandbox/services/sandbox.service.ts#L711-L713
            # Resolve the winning cross-process create by that unique name, not
            # the eventually consistent label index that allowed the race.
            lookup = await client.get(
                _api(valves, f"/sandbox/{urllib.parse.quote(sandbox_name, safe='')}"),
                headers=_headers(valves),
                timeout=30.0,
            )
            if lookup.status_code == 404:
                raise RuntimeError(
                    "Sandbox creation conflicted, but Daytona did not return the "
                    "winning sandbox by its unique name. Retry this tool call; if "
                    "the conflict persists, ask the Daytona administrator to inspect "
                    f"sandbox name {sandbox_name}."
                )
            lookup.raise_for_status()
            sandbox = lookup.json()
            if (sandbox.get("name") != sandbox_name
                    or sandbox.get("labels", {}).get(label_key) != email):
                raise RuntimeError(
                    f"Daytona sandbox name {sandbox_name} is already in use without "
                    f"the expected {label_key}={email} association. Lathe will not "
                    "select it. Ask the Daytona administrator to reconcile that name."
                )
        else:
            resp.raise_for_status()
            sandbox = resp.json()
            warning = "[Sandbox was created — this is a fresh environment with no prior files]"

    sandbox_id = sandbox["id"]
    state = sandbox.get("state", "unknown")

    # 4. Drive every observed lifecycle state toward started. The same policy
    # runs before and during polling so stopping -> stopped and archiving ->
    # archived issue the start request that their terminal states require.
    deadline = time.time() + 120
    poll_interval = 1.0
    info = sandbox
    recovery_attempted = False
    start_requested = False
    preparing_emitted = False
    while time.time() < deadline:
        state = info.get("state", "unknown")

        if state == "started":
            await _wait_for_toolbox(valves, sandbox_id, client, emitter, wait=wait_for_startup)
            await _emit(emitter, "Sandbox ready", done=True)
            return sandbox_id, warning

        if not preparing_emitted:
            await _emit(emitter, "Preparing sandbox...")
            preparing_emitted = True

        if state in _SANDBOX_START_STATES and not start_requested:
            resp = await client.post(
                _api(valves, f"/sandbox/{sandbox_id}/start"),
                headers=_headers(valves),
                timeout=30.0,
            )
            resp.raise_for_status()
            start_requested = True
            if not warning:
                if state == "archived":
                    warning = "[Sandbox was restarted from archived state — running processes were lost]"
                elif state == "paused":
                    warning = "[Sandbox was resumed from paused state]"
                else:
                    warning = "[Sandbox was restarted — running processes were lost]"

        if state == "error":
            if info.get("recoverable") and not recovery_attempted:
                recovery_attempted = True
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
                start_requested = True
                if not warning:
                    warning = "[Sandbox was recovered from error — check that expected files and processes still exist]"
            else:
                reason = info.get("errorReason", "unknown")
                detail = " after recovery was attempted" if recovery_attempted else ""
                raise RuntimeError(
                    f"Sandbox is in a non-recoverable error state{detail}: {reason}. "
                    "Ask the Daytona administrator to inspect or replace this sandbox."
                )

        elif state == "build_failed":
            raise RuntimeError(
                "Sandbox snapshot build failed. Ask the Daytona administrator to "
                "inspect the failed build and replace or retry this sandbox."
            )

        elif state in _SANDBOX_DELETION_STATES:
            raise RuntimeError(
                f"Sandbox is being deleted (state: {state}). Retry after deletion completes "
                "so Lathe can provision a replacement."
            )

        elif state not in _SANDBOX_START_STATES and state not in _SANDBOX_WAIT_STATES:
            raise RuntimeError(
                f"Sandbox has unsupported Daytona lifecycle state '{state}'. "
                "Ask the administrator to inspect the sandbox and update Lathe if this state is expected."
            )

        await asyncio.sleep(poll_interval)
        resp = await client.get(
            _api(valves, f"/sandbox/{sandbox_id}"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        info = resp.json()

        poll_interval = min(poll_interval * 1.2, 5.0)

    raise RuntimeError("Timed out waiting for sandbox to start (120s)")


# ── Tools class (only public methods are visible to OWUI) ───────────


class Tools:
    class Valves(BaseModel):
        onboarding_instructions: str = Field(
            "",
            description="Optional admin guidance delivered with the first sandbox-tool orientation per chat and in the overview manual. Use for local resources, conventions, or short recipes. Visible to the agent; do not put secrets here.",
        )
        handbook_url: str = Field(
            "",
            description="Optional agent handbook reference URL. Empty by default for network-restricted deployments. The agent chooses how to read it; Lathe never fetches it. May point to a public, intranet, or test Markdown resource.",
        )
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
        preview_wrapper_url: str = Field(
            "", description="Optional HTTPS registration endpoint for public or owner-authenticated HTTP previews. Headerless public requests fall back to the direct signed URL; private or header-bearing requests fail closed.",
        )
        preview_wrapper_key: str = Field(
            "", description="Installation bearer credential for the preview wrapper (admin only).",
            json_schema_extra={"input": {"type": "password"}},
        )
        preview_expiry_seconds: int = Field(
            86400, ge=60, le=86400,
            description="Lifetime of upstream HTTP signed previews, in seconds (default/max: 24 hours). Independent of wrapper registration expiry.",
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
        auto_delete_minutes: int = Field(
            -1,
            description="Minutes after archive before sandbox is permanently deleted (-1 = never). Example: 129600 = 90 days.",
        )
        auto_create_sandbox: bool = Field(
            True,
            description=(
                "Automatically create a sandbox when none exists for the user. "
                "Disable for deployments where sandboxes are provisioned by another "
                "system; the agent then receives sandbox_missing_message instead."
            ),
        )
        sandbox_missing_message: str = Field(
            "",
            description=(
                "Message returned to the agent when no sandbox exists and "
                "auto_create_sandbox is disabled. Use it to give site-specific "
                "setup advice (e.g. a URL to provision a sandbox). Empty falls "
                "back to a generic message."
            ),
        )
        sandbox_create_overrides: str = Field(
            "{}",
            description=(
                "JSON object of extra arguments merged into the Daytona sandbox "
                "create request, e.g. "
                '{"cpu":2,"memory":4,"disk":20,"snapshot":"my-snapshot","target":"us"}. '
                "Resource fields (cpu/memory/disk) are integers in cores/GB. "
                "Cannot override name or labels, or attach volumes."
            ),
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
                "Values are shell-quoted before injection and entrusted to the model-controlled shell."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        self.valves = self.Valves()
        # Chat-scoped auto-init and background notices, drained into text results.
        # LRU eviction drops pending messages; a later call re-runs auto-init.
        self._chat_state: LRUCache = LRUCache(maxsize=1024)
        self._interpreter_state: dict = {}

    # ── agent-facing manual ──────────────────────────────────────────
    #
    # Keep initial orientation short; defer recipes to task-specific pages.
    # The tool catalog is introspected from the public methods.

    # Manpage strings are interpolated via plain str.replace() in the
    # lathe() method, NOT str.format().  Known placeholders today:
    # {tool_catalog}, {preview_access_note}. Unknown
    # placeholders pass through unchanged (no KeyError), and literal
    # braces in shell/JSON/regex snippets (${VAR}, {pid,log,exit},
    # {"key":"value"}, {n,m}) are always safe.  If you add a new dynamic
    # placeholder, register a corresponding .replace() call in lathe().
    _MANPAGES: dict[str, str] = {
        "egress": textwrap.dedent("""\
            # Lathe — Egress Restrictions

            ## What can the sandbox reach?

            Network access depends on the Daytona account and deployment.
            An allowlist may permit some package registries and GitHub endpoints
            without permitting every release asset, redirect, CDN, or dependency.
            Test the actual source and its redirects. Do not assume all public
            downloads work. Use preinstalled tools or reachable intranet mirrors
            when available; Lathe does not require a public app repository.

            ## When curl fails: egress workarounds

            Timeouts or connection failures on a known-live host may indicate
            egress filtering, but are not proof: check the URL and service too.
            For a blocked host, the workarounds below involve the user.

            **User-assisted transfer through an available file browser:**
            Ask the user to download the file on their own machine, then
            upload it through an installed file browser, such as dufs. If none
            exists, consult the services manual and configured handbook before
            installing from a reachable source; dufs is not preinstalled by Lathe.
            After starting and checking the browser, use
            expose(target="http:5000", access="private") to get the URL. Choose public
            only if the user explicitly requests public/world access; missing
            private wrapping is not permission to downgrade. This handles any
            file type and host, subject to the deployment's upload limits.

            **Rare — custom browser-side fetch service:**
            For repeated fetch needs (e.g. crawling an API the sandbox
            can't reach), build a small web service in the sandbox that
            presents a UI where the user clicks to initiate fetches from
            their browser. The browser uses the user's network and is
            subject to CORS: this only works automatically for targets
            that set Access-Control-Allow-Origin headers. The service
            POSTs results back to itself for the agent to read.

            **Administrator network policy:**
            Ask the administrator about account-tier restrictions, approved
            mirrors, or allowlist changes. Check the current policy at
            https://app.daytona.io rather than assuming a tier guarantees access.

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

              CMD=/dev/shm/lathe/cmd/<id>   — the job's sidecar directory
              PID=/dev/shm/lathe/cmd/<id>/pid — process ID file

            ## Sidecar files

            Where CMD=/dev/shm/lathe/cmd/<id>:

              CMD/sh    — the wrapper script, unlinked as soon as execution starts
              CMD/pid   — PID of the bash process (written before exec)
              CMD/log   — stdout+stderr, written live via tee
              CMD/exit  — exit code; present only when the process ends

            Absence of CMD/exit means the process is still running *or* the
            sandbox was restarted (in which case the PID is stale). To
            distinguish the two, check whether the PID is still alive.
            While a command runs, its sidecar remains available for monitoring.
            Completion releases the Daytona session unless a live shell-backgrounded
            job still depends on it. Cleanup removes the wrapper and PID, but keeps
            the full log and exit status for 24 hours after completion. Unrelated
            bash calls, including calls from other chats, do not shorten this lease.
            A later bash call removes expired evidence once no retained job is live.
            Previews are bounded to 50 KiB, and may begin mid-line. Use read() for
            numbered previews, bash byte ranges, or expose() for full file access.
            Sandbox stop or restart clears all sidecars and sessions.

            ## Recipes

            **Peek at live output:**
            ```
            tail $CMD/log
            ```

            **Poll until done (bounded):**
            ```
            for i in 1 2 3 4 5; do
              test -f $CMD/exit && { cat $CMD/exit; break; } || sleep 2
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
        "interpret": textwrap.dedent("""\
            # Lathe — Persistent Python Interpreter

            interpret() runs Python code in a persistent REPL backed by the
            Daytona code interpreter. Variables, imports, and definitions
            survive across calls within the same conversation.

            ## State model

            - One interpreter context per conversation (chat), created lazily
              on the first interpret() call.
            - State persists across calls: define a function in one call, use
              it in the next. Import a module once, use it everywhere.
            - Identity survives eviction/reconstruction of chat notices, but
              only within one loaded toolkit instance. OWUI restart, toolkit
              reload, or another worker does not reconnect to the old context.
              Save important results to files; this is not durable storage.
            - Transient context-list failures fail the call without replacing
              state. Retry once the service recovers.
            - State is lost when the sandbox stops or restarts (idle timeout,
              destroy, etc.). The tool detects this and warns you to re-run
              setup code.

            ## When to use interpret() vs bash()

            **Use interpret() when:**
            - You need state across calls (accumulating data, iterative
              exploration, building up a computation step by step).
            - You are doing data analysis, numerical computation, or
              prototyping where seeing intermediate results matters.
            - You want to define helper functions and reuse them.

            **Use bash() when:**
            - Running shell commands, installing packages, managing files.
            - Running non-Python programs.
            - Commands need shell features (pipes, redirection, env vars).
            - One-shot scripts with no state to preserve.

            ## Limitations

            - Python only. For other languages, use bash().
            - Top-level ``await`` is not supported. Use ``asyncio.run()``
              for async code.
            - No stdin interaction. The code runs non-interactively.
            - Default timeout is 120 seconds. Pass timeout=0 for no limit,
              or a custom value in seconds.
            - stdout/stderr previews are bounded to 50 KiB. Save large results
              to files and inspect selected ranges instead of printing them.

            """),
        "services": textwrap.dedent("""\
            # Lathe — Service Details

            Start applications with bash(), then expose(target="http:<port>",
            access="private"). expose never installs or starts applications.
            Shells and IDEs provide arbitrary execution: always keep them private.

            ## Install and operate ordinary programs

            Lathe has no app installer or managed application bundle. For a first
            setup, read this manual, relevant existing skills, and the configured
            agent handbook when needed. The handbook is an optional reference,
            not executable setup or a required public dependency. Use installed
            programs or sources the sandbox can reach, including intranet mirrors.
            Examples include code-server (IDE), dufs (file browser), ttyd (terminal),
            or a static HTTP server. Inspect the installed version and --help;
            confirm architecture, dependencies, and published checksums before
            installing. Never upgrade an existing installation merely to start it:
            ask about version, extensions, configuration, and upgrade preferences.

            Choose an existing absolute root and check the requested port before
            launch. Inspect listeners with ss/lsof when installed, or /proc on
            Linux. A PID file or open port alone does not identify the application.
            Verify executable, command line, process start identity, root, and
            authentication settings. Reuse a matching healthy instance without
            restarting it. If another root or application owns the port, explain
            the conflict and ask whether to reuse it, choose a different port, or
            stop it. Never blindly kill a listener or stale PID.

            Keep binaries, configuration, logs, and service records outside skill
            directories. Start via bash; use its background sidecars to inspect
            logs and exit status, or an explicitly detached process with recorded
            PID/start identity. Check HTTP readiness with a bounded timeout and
            inspect logs on failure before exposing. Verify browser behavior too:
            a listening socket does not prove IDE or terminal WebSockets work.
            Use the least reachable bind address the proxy can actually access.
            Disable app authentication only behind a verified private authenticated
            proxy with no unauthenticated direct path; otherwise retain app auth.
            Private registration does not secure other routes to the same app.

            For shutdown, recheck the recorded identity before sending TERM to
            that process (or a deliberately created, verified process group).
            Wait and check port release. Do not escalate to KILL or stop unrelated
            processes without permission. After sandbox sleep, verify or restart
            the intended application before renewing exposure.
            The ordinary sandbox filesystem survives stop/archive, not destroy.
            Preview registration neither keeps a sandbox awake nor restarts a process.

            {preview_access_note}

            ## Develop reusable skills with the user

            Transagency describes both the capacity to act that develops through human–agent collaboration and the expanded agency a participant experiences through it. Build skills that support both: agentic skills help you work independently in the Daytona sandbox, while transagentic skills help you and the user coordinate through Open WebUI. Offer to preserve successful practices, and develop them with the user’s preferences about what to remember, automate, share, revise, or forget.

            Choose a skill’s location according to who should discover it: ~/.local/share/lathe/skills/<name>/SKILL.md for Lathe-specific practices, ~/.agents/skills/<name>/SKILL.md for sandbox-wide practices shared with compatible agents, or <project>/.agents/skills/<name>/SKILL.md for project-local knowledge. onboard() catalogs available skills; read the relevant SKILL.md before applying one. Shared skills should describe portable procedures without assuming another agent has Lathe’s expose() tool or communicates through Open WebUI.

            XDG_DATA_HOME replaces ~/.local/share when configured. On a name
            collision, project skills override Lathe-specific skills, which
            override shared global skills. After a successful setup, OFFER to save
            a skill and wait for approval before writing it. Record the tested
            version, installation/source paths, start/stop/readiness checks,
            process identity, port-reuse choices, and upgrade preferences, not
            secrets or expiring preview URLs. Do not install prepackaged skills
            or grow skills automatically. Deletion means forget: an absent skill
            may be offered again after use, never silently regenerated.

            ## Worked example: a static site, then a reusable skill

            A simple static preview needs only the Python 3 already used by Lathe;
            no handbook, package installation, or outbound download is necessary.
            For example, use write() to create
            /home/daytona/workspace/site/index.html with the user's page. Serve a
            dedicated directory containing only intended public-facing content,
            not the entire workspace. Python's HTTP server lists directories and
            follows symlinks: check for links that could expose files outside this
            root. This is a development preview, not a hardened hosting service.

            Check port 8000 using the identity/reuse procedure above, then start:

            bash(command="python3 -m http.server 8000 --bind 127.0.0.1 --directory /home/daytona/workspace/site",
                 foreground_seconds=0)

            Keep the returned background descriptor. In a subsequent bash call,
            fetch http://127.0.0.1:8000/index.html using Python's urllib.request
            with a short timeout. Check the actual page content, not merely a 200
            status, and inspect the background log if startup fails. Then call:

            expose(target="http:8000", access="private")

            Verify that the preview reaches this server. If the proxy cannot reach
            loopback, diagnose its routing before changing the bind address; do not
            silently switch to public access. Public exposure requires the user's
            explicit request. Editing files under the served root needs only a
            browser refresh, not a server restart.

            After success, ask: "Would you like me to save our tested preview
            procedure as a skill for next time?" With approval, write a SKILL.md
            in the appropriate location above, with YAML name and description.
            Capture the root, chosen port and bind address, launch command,
            readiness/content check, verified process-identity and shutdown steps,
            and the user's port-reuse/access preferences. Teach rechecking identity
            rather than treating today's PID as permanent. Include how to restart
            after sandbox sleep and obtain a fresh preview URL. Keep credentials
            and temporary URLs out of the skill. A shared skill should separate
            portable server operation from Lathe-specific expose() instructions.

            In a fresh conversation, discover and read that skill, inspect whether
            the intended server is already running, then reuse it or restart it
            safely. The saved skill is the user's adapted knowledge, not a newly
            installed tool or a named expose target.

            ## Upstream application headers

            expose(..., upstream_headers={"Authorization": "Bearer app-token"})
            configures fixed headers on this registration's HTTP requests and
            WebSocket handshakes. Public/private access is independent: public
            visitors also use the injected credential. Headers do not prove
            visitor identity. The application decides what they mean.

            Configured names replace browser-supplied values case-insensitively.
            Unconfigured names remain ordinary browser headers; no application
            assertion namespace is reserved. Authorization (including Basic) is
            supported. Routing, framing, transport, browser security, and provider
            headers are forbidden. Limits: 16 headers, 64-byte names, printable
            ASCII values up to 4096 bytes, 8192 bytes total for names and values.

            Nonempty headers require a compatible wrapper and acknowledgement:
            no direct fallback or silent omission. Values are sent to the wrapper
            and application, not echoed in tool results. Model-supplied values
            remain in the chat's tool arguments. Apps may reflect them in replies.
            If the app trusts an assertion, separately verify its trusted-peer
            rules and direct/upstream bypass routes. Exposure does not secure
            alternate paths into the sandbox application.

            """),
        "overview": textwrap.dedent("""\
            # Lathe Toolkit — Overview

            Work in a per-user Linux sandbox, shared across conversations.
            Tools create/start/recover it automatically. Default working directory:
            /home/daytona/workspace. Use absolute sandbox paths for file tools.
            Files survive stop/archive; running processes and interpreter state do
            not. Idle stop defaults to 15 minutes. Previews do not keep it awake.

            ## Start with project context

            The first sandbox tool call attempts a workspace snapshot and skill
            catalog, not AGENTS.md contents. Use onboard(path) for the project's listing, AGENTS.md,
            and skill catalog (shared ~/.agents/skills, Lathe-specific
            ~/.local/share/lathe/skills, and project .agents/skills). Read a relevant SKILL.md
            before using its workflow. Do not repeat onboarding just for a listing
            already supplied. Use "" for the default workspace.

            ## Operating safeguards

            - Shell commands are non-interactive; use -y flags as needed.
              Parallelize with separate bash calls, not shell-level & + wait.
            - bash auto-backgrounds after its foreground window.
              Use the returned sidecar paths to inspect progress/results. Bash
              logs and exit status remain readable for 24 hours after completion;
              later bash calls reap expired results. Sandbox stop/restart clears them.
            - {preview_access_note}
            - Start services with bash, check readiness, then expose http:<port>.
              Never expose shells or IDEs publicly.
            - For a first app setup, consult the services manual and configured
              handbook when needed. After success, offer to save a skill; wait
              for user approval. Skills are never created automatically.
            - destroy is irreversible and requires user confirmation.

            ## Read details only when needed

            Call lathe(manpage="...") with:
            - "interpret": persistent Python state and limitations (vs. one-shot bash).
            - "background": shell-job sidecars and peek/poll/kill recipes.
            - "services": browser file transfer, sites, terminals, IDEs, headers, recovery.
            - "egress": network allowlist and user-assisted workarounds for blocked hosts.
            - "version": installed toolkit version.

            ## Available tools

            {tool_catalog}

            Users: https://lathe.tools · Source: https://github.com/rndmcnlly/lathe
            """),
    }

    # One-line descriptions for the page index (shown on unknown page
    # lookups and useful for the model to decide which page to request).
    _MANPAGE_INDEX: dict[str, str] = {
        "overview": "Big-picture orientation: sandbox model, tool catalog, key workflows, gotchas.",
        "interpret": "Persistent Python REPL: state model, when to use vs bash, limitations.",
        "services": "Advanced service details: lifecycle, ports, logs, manual configuration, and recovery.",
        "background": "Background job sidecar files, and peek/poll/kill recipes.",
        "egress": "Deployment-dependent network access, mirrors, and user-assisted transfers.",
        "version": "Show the installed Lathe toolkit version.",
    }

    async def lathe(
        self,
        manpage: str = "overview",
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Read operational orientation or a task-specific manual page.
        :param manpage: "overview" for safeguards and page index; "interpret", "background", "services", "egress" for details; "version" for installed version.
        """
        type_err = _check_tool_params({"manpage": manpage}, type(self).lathe)
        if type_err:
            return type_err

        tool_catalog = _build_tool_catalog(self)

        if manpage == "version":
            mod_doc = globals().get("__doc__", "") or ""
            match = re.search(r"^version:\s*(.+)$", mod_doc, re.MULTILINE)
            ver = match.group(1).strip() if match else "unknown"
            await _emit(__event_emitter__, f"Lathe v{ver}", done=True)
            return f"Lathe toolkit version: {ver}"

        if manpage in self._MANPAGES:
            content = self._MANPAGES[manpage]
            # Use .replace() rather than .format() so manpages with literal
            # braces (e.g. shell snippets like ${VAR}, regex {n,m}) don't blow
            # up with KeyError, and so missing placeholders are silently
            # tolerated rather than fatal.
            content = content.replace("{tool_catalog}", tool_catalog)
            preview_note = (
                "HTTP expose requires an explicit public/private access choice. Public requests try the wrapper, "
                "then fall back to a direct signed bearer URL if wrapping is unavailable or refused (unless upstream_headers are requested). "
                "Private requests require successful owner-authenticated wrapper registration and never downgrade to public. "
                "Choose private unless the user specifically requested public/world access."
                if self.valves.preview_wrapper_url or self.valves.preview_wrapper_key else
                "HTTP expose requires an explicit public/private access choice. Only public direct signed bearer URLs "
                "are available on this deployment; private requests fail closed. Choose private unless the user "
                "specifically requested public/world access."
            )
            preview_note += f" Upstream HTTP access lasts up to {self.valves.preview_expiry_seconds / 3600:g} hour(s); call expose again to renew."
            content = content.replace("{preview_access_note}", preview_note)
            if manpage in ("overview", "services"):
                guidance = _installation_guidance(self.valves)
                if guidance:
                    content += "\n\n" + guidance
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
        __user__: dict = {},
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Permanently destroy the sandbox VM. Irreversible.
        Prompts the user for confirmation before proceeding.
        """
        # Gate on real human consent via OWUI's confirmation dialog.
        if __event_call__:
            message = (
                "This will permanently destroy the sandbox VM and "
                "all its contents. This action cannot be undone."
            )
            confirmed = await __event_call__({
                "type": "confirmation",
                "data": {
                    "title": "Destroy sandbox?",
                    "message": message,
                },
            })
            if not confirmed:
                await _emit(__event_emitter__, "Destroy cancelled", done=True)
                return "Destroy cancelled by user."
        else:
            # Fallback: if __event_call__ is not available (old OWUI or
            # testing), refuse to proceed rather than silently destroying.
            return (
                "Error: cannot confirm destruction — interactive confirmation "
                "is not available. Update Open WebUI or try again from the chat UI."
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
                sandboxes = _extract_sandbox_list(resp.json())
                matches = [
                    s for s in sandboxes
                    if s.get("labels", {}).get(label_key) == email
                ]

                if len(matches) > 1:
                    return f"Error: {_multiple_sandboxes_error(label_key, email, matches)}"

                sandbox = (
                    await _get_live_sandbox(valves, matches[0]["id"], client)
                    if matches else None
                )

                if sandbox is None:
                    await _emit(__event_emitter__, "No sandbox found", done=True)
                    return "No sandbox found. One will be created on your next tool call."

                sid = sandbox["id"]
                await _emit(__event_emitter__, f"Destroying sandbox {sid[:12]}...")
                resp = await client.delete(
                    _api(valves, f"/sandbox/{sid}"),
                    headers=_headers(valves),
                    params={"force": "true"},
                    timeout=30.0,
                )
                resp.raise_for_status()

                # The label list may transiently disappear then reappear with a
                # stale pre-delete state. Only the per-ID 404 is completion.
                for _ in range(30):
                    await asyncio.sleep(1)
                    resp = await client.get(
                        _api(valves, f"/sandbox/{sid}"),
                        headers=_headers(valves),
                        timeout=30.0,
                    )
                    if resp.status_code == 404:
                        break
                    resp.raise_for_status()
                else:
                    raise RuntimeError(
                        f"Sandbox {sid[:12]} did not finish deletion within 30 seconds."
                    )

                await _emit(__event_emitter__, "Sandbox destroyed", done=True)
                ids = sid[:12]
                return (
                    f"Destroyed 1 sandbox(es) ({ids})."
                    f" A fresh sandbox will be created on the next tool call."
                )

        except Exception as exc:
            await _emit(__event_emitter__, "Destroy failed", done=True)
            return f"Error: {exc}"

    async def onboard(
        self,
        path: str,
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        """
        Load project listing, AGENTS.md, and shared, Lathe-specific, and project skills.
        Read relevant SKILL.md files for full instructions. Skip repeat onboarding
        when the supplied context already covers this project.
        :param path: Absolute project root; "" selects /home/daytona/workspace.
        """
        type_err = _check_tool_params({"path": path}, type(self).onboard)
        if type_err:
            return type_err

        harness = _HarnessDelivery(self._chat_state, __chat_id__)

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, harness.warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, "Loading project context...")

            # Models sometimes pass "" despite the docstring requiring an
            # absolute path.  Silently default to the workspace root rather
            # than error -- this is the most useful interpretation.
            p = path.strip()
            if not p:
                p = "/home/daytona/workspace"
            p = p.rstrip("/")
            script = _build_onboard_script(p)

            script_path = _onboard_script_path(str(uuid.uuid4()))
            await _upload_file(
                self.valves, sandbox_id, client,
                script_path, script.encode("utf-8"),
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

            if exit_code != 0:
                await _emit(__event_emitter__, "Error loading context", done=True)
                return f"Error: onboard script failed (exit {exit_code}): {result[:500]}"

            rendered = _render_context(result)
            await _emit(__event_emitter__, "Project context loaded", done=True)
            return rendered if rendered else "(empty project context)"

        return await _tool_context(__event_emitter__, _run, harness)

    async def bash(
        self,
        command: str,
        workdir: str = "/home/daytona/workspace",
        foreground_seconds: int = -1,
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        type_err = _check_tool_params(
            {"command": command, "workdir": workdir,
             "foreground_seconds": foreground_seconds},
            type(self).bash,
        )
        if type_err:
            return type_err

        harness = _HarnessDelivery(self._chat_state, __chat_id__)

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, harness.warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, "Running command...")

            # Collect user-supplied env vars from UserValves (never logged).
            # Raises ValueError (caught below) if env_vars is malformed.
            user_valves = __user__.get("valves")
            user_pairs: list[tuple[str, str]] = []
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                user_pairs = _parse_env_vars(raw_env)

            # Negative (default) falls back to the valve setting.
            # 0 means immediate background (fire-and-forget).
            # Positive values are the foreground window in seconds.
            if foreground_seconds < 0:
                fg_seconds = self.valves.foreground_timeout_seconds
            else:
                fg_seconds = foreground_seconds

            # When the command auto-backgrounds, spawn a polling coroutine
            # to push a completion notice into the chat's message queue.
            backgrounded = False
            def _on_bg(*, sandbox_id, session_id, session_cmd_id, cmd_id, start_time):
                nonlocal backgrounded
                backgrounded = True
                asyncio.ensure_future(_poll_bg_bash(
                    self.valves, sandbox_id, session_id, session_cmd_id,
                    cmd_id, start_time,
                    self._chat_state, __chat_id__,
                ))

            result = await _core_bash(
                self.valves, sandbox_id, client,
                command=command,
                workdir=workdir,
                user_pairs=user_pairs,
                foreground_seconds=fg_seconds,
                emit=__event_emitter__,
                on_background=_on_bg,
            )

            status = "Command running in background" if backgrounded else "Command complete"
            await _emit(__event_emitter__, status, done=True)
            return result

        return await _tool_context(__event_emitter__, _run, harness)
    bash.__doc__ = inspect.getdoc(_core_bash)

    read = _standard_tool(
        _core_read,
        emit_start="Reading {kwargs[path]}...",
        emit_done="Read complete",
    )

    glob = _standard_tool(
        _core_glob,
        emit_start="Searching for {kwargs[pattern]}...",
        emit_done="Search complete",
    )

    grep = _standard_tool(
        _core_grep,
        emit_start="Searching for {kwargs[pattern]!r}...",
        emit_done="Search complete",
    )

    write = _standard_tool(
        _core_write,
        emit_start="Writing {kwargs[path]}...",
        emit_done="Write complete",
    )

    edit = _standard_tool(
        _core_edit,
        emit_start="Editing {kwargs[path]}...",
        emit_done="Edit complete",
    )

    interpret = _standard_tool(
        _core_interpret,
        emit_start="Running Python...",
        emit_done="Execution complete",
        extra_core_kwargs=lambda self, sandbox_id, chat_id: {
            "interpreter_state": self._interpreter_state,
            "chat_id": chat_id,
        },
    )

    async def view(
        self,
        path: str,
        __user__: dict = {},
        __chat_id__: str = "",
        __model__: dict = {},
        __metadata__: dict = {},
        __event_emitter__=None,
    ) -> str:
        type_err = _check_tool_params({"path": path}, type(self).view)
        if type_err:
            return type_err

        # Capability gate: refuse cleanly when the current model cannot
        # accept image input.  Checked BEFORE _ensure_sandbox so a refusal
        # never spins up a VM.  __metadata__["model"] reflects the user's
        # actual selection more reliably than __model__ in multi-model
        # chats and some tool-invocation paths.
        model = __metadata__.get("model") if isinstance(__metadata__, dict) else None
        if not isinstance(model, dict) or not model:
            model = __model__ if isinstance(__model__, dict) else {}
        if not _model_supports_vision(model):
            modality = (model.get("architecture") or {}).get("modality", "unknown")
            return (
                f"Error: the current model ({model.get('id', 'unknown')}) does not "
                f"accept image input (modality: {modality}), so view() would deliver "
                f"an image you cannot perceive, and some providers then reject every "
                f"later turn of the conversation. Ask the user to switch to a "
                f"vision-capable model, or extract the information without vision "
                f"(e.g. OCR or metadata inspection via bash)."
            )

        harness = _HarnessDelivery(self._chat_state, __chat_id__, image_result=True)

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, harness.warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, f"Loading image {path}...")
            result = _checked_result(await _core_view(self.valves, sandbox_id, client, path=path))

            await _emit(__event_emitter__, "Image loaded", done=True)
            return result

        return await _tool_context(__event_emitter__, _run, harness)
    view.__doc__ = inspect.getdoc(_core_view)

    async def expose(
        self,
        target: str,
        access: str,
        tag: str = "",
        upstream_headers: dict[str, str] = {},
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        """Expose an already-running HTTP/WebSocket service. Does not install or start applications.
        For user file upload/download, interactive shells, browser VS Code IDEs,
        or static websites, first read lathe(manpage="services") for lifecycle
        guidance and optional handbook references, then launch with bash.
        :param target: "http:<port>" for a service listening on port 1024–65535. Start it with bash first. See lathe(manpage="services") for lifecycle and access safeguards.
        :param access: Required policy: "private" authenticates the owner and fails closed; "public" allows anyone with the URL and may fall back to a direct bearer URL. Choose private unless the user explicitly requests public/world access. Shells and IDEs should always be private.
        :param tag: Optional untrusted hostname hint: lowercase letters, digits, internal hyphens, max 32 characters. Hostname text never proves identity or purpose.
        :param upstream_headers: Fixed application headers for HTTP/WebSocket forwarding, independent of access. Replaces browser values case-insensitively; supports Authorization. Requires a compatible wrapper, never direct fallback. Max 16 headers, 64-byte names, printable ASCII values up to 4096 bytes, 8192 bytes total. Routing/transport headers forbidden. Values remain in tool arguments; see services manual for trust boundaries.
        """
        type_err = _check_tool_params(
            {"target": target, "access": access, "tag": tag,
             "upstream_headers": upstream_headers}, type(self).expose,
        )
        if type_err:
            return type_err
        header_error = _validate_upstream_headers(upstream_headers)
        if header_error:
            return header_error
        access = access.strip().lower()
        if access not in ("public", "private"):
            return 'Error: access must be "public" or "private".'
        tag = tag.strip().lower()
        if tag and not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", tag):
            return "Error: tag must be a lowercase DNS label (max 32 characters)."
        match = re.fullmatch(r"http:([0-9]{1,5})", target.strip().lower())
        if not match:
            return 'Error: target must be "http:<port>". Start applications using bash; see lathe(manpage="services") for lifecycle and access safeguards.'
        port = int(match[1])
        if not 1024 <= port <= 65535:
            return "Error: port must be between 1024 and 65535."
        harness = _HarnessDelivery(self._chat_state, __chat_id__)

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, harness.warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )
            await _emit(__event_emitter__, f"Generating URL for port {port}...")
            url, access_note = await _http_preview(
                self.valves, sandbox_id, port, __user__, access, tag, client,
                upstream_headers=upstream_headers,
            )
            await _emit(__event_emitter__, f"URL ready (port {port})", done=True)
            return (
                f"Service URL: {url}\n\n{access_note}\n\n"
                f"The sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity, "
                "even with background processes. Restart the service with bash after sleep, "
                "then call expose again. Registration does not keep the service alive."
            )

        return await _tool_context(__event_emitter__, _run, harness)

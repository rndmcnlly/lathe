"""
title: Daytona Sandbox
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (bash, read, write, edit) backed by per-user Daytona sandboxes with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx
version: 0.1.1
licence: MIT
"""

import asyncio
import io
import time

import httpx
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


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe use in shell scripts."""
    return "'" + s.replace("'", "'\\''") + "'"


def _get_email(user: dict) -> str:
    email = user.get("email", "")
    if not email:
        raise RuntimeError("No email found for user. Cannot provision sandbox.")
    return email


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

    label_key = valves.deployment_label
    label_filter = f"{label_key}:{email}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Look up existing sandbox by label
        resp = await client.get(
            _api(valves, "/sandbox"),
            params={"label": label_filter},
            headers=_headers(valves),
        )
        resp.raise_for_status()
        sandboxes = resp.json()

        sandbox = None
        if sandboxes:
            sandbox = sandboxes[0]

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

    def __init__(self):
        self.valves = self.Valves()

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
        try:
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, "Loading project context...")

            p = path.rstrip("/")
            script = (
                'P=' + _shell_quote(p) + '\n'
                'found=0\n'
                'if [ -f "$P/AGENTS.md" ]; then\n'
                '  echo "# AGENTS.md"\n'
                '  echo ""\n'
                '  cat "$P/AGENTS.md"\n'
                '  found=1\n'
                'fi\n'
                'if [ -d "$P/.agents/skills" ]; then\n'
                '  skills=""\n'
                '  for d in "$P"/.agents/skills/*/SKILL.md; do\n'
                '    [ -f "$d" ] || continue\n'
                '    found=1\n'
                '    dir_name=$(basename "$(dirname "$d")")\n'
                '    fm_name="$dir_name"\n'
                '    fm_desc=""\n'
                '    in_fm=0\n'
                '    while IFS= read -r line; do\n'
                '      case "$in_fm" in\n'
                '        0) [ "$line" = "---" ] && in_fm=1 ;;\n'
                '        1) [ "$line" = "---" ] && break\n'
                '           case "$line" in\n'
                '             name:*) fm_name="${line#name:}"; fm_name="${fm_name# }" ;;\n'
                '             description:*) fm_desc="${line#description:}"; fm_desc="${fm_desc# }" ;;\n'
                '           esac ;;\n'
                '      esac\n'
                '    done < "$d"\n'
                '    skills="${skills}- **${fm_name}**: ${fm_desc}\\n  \\`${d}\\`\\n"\n'
                '  done\n'
                '  if [ -n "$skills" ]; then\n'
                '    [ -f "$P/AGENTS.md" ] && echo -e "\\n---\\n"\n'
                '    echo "# Available Skills"\n'
                '    echo ""\n'
                '    echo "Load a skill'"'"'s full instructions with read(path) when the task matches its description."\n'
                '    echo ""\n'
                '    echo -e "$skills"\n'
                '  fi\n'
                'fi\n'
                '[ "$found" -eq 0 ] && echo "ERROR_NO_CONTEXT" && exit 1\n'
                'exit 0\n'
            )

            # Write script to temp file and execute it (avoids all quoting issues)
            script_path = "/tmp/_onboard.sh"
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

        except RuntimeError as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            await _emit(__event_emitter__, f"API error: HTTP {e.response.status_code}", done=True)
            return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
        except Exception as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"

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
        Default working directory is /home/daytona/workspace.
        :param command: The bash command to execute.
        :param workdir: Working directory for the command (default: /home/daytona/workspace).
        """
        try:
            email = _get_email(__user__)
            sandbox_id = await _ensure_sandbox(self.valves, email, __event_emitter__)

            await _emit(__event_emitter__, "Running command...")

            # Build a script that sets non-interactive env vars then runs the
            # command exactly as provided.  Writing to a temp file avoids all
            # quoting/escaping issues with the Daytona execute API's argv
            # splitting — the command reaches bash with zero transformations.
            script = (
                "#!/usr/bin/env bash\n"
                "set -e -o pipefail\n"
                "export DEBIAN_FRONTEND=noninteractive "
                "GIT_TERMINAL_PROMPT=0 "
                "PIP_NO_INPUT=1 "
                "NPM_CONFIG_YES=true "
                "CI=true\n"
                + command
                + "\n"
            )

            script_path = "/tmp/_cmd.sh"
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

            await _emit(__event_emitter__, "Command complete", done=True)

            if exit_code == 0:
                return result if result else "(no output)"
            else:
                return f"Exit code: {exit_code}\n{result}"

        except RuntimeError as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            await _emit(__event_emitter__, f"API error: HTTP {e.response.status_code}", done=True)
            return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
        except Exception as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"

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
        try:
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

        except RuntimeError as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            await _emit(__event_emitter__, f"API error: HTTP {e.response.status_code}", done=True)
            return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
        except Exception as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"

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
        try:
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

        except RuntimeError as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            await _emit(__event_emitter__, f"API error: HTTP {e.response.status_code}", done=True)
            return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
        except Exception as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"

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
        try:
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

        except RuntimeError as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            await _emit(__event_emitter__, f"API error: HTTP {e.response.status_code}", done=True)
            return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
        except Exception as e:
            await _emit(__event_emitter__, f"Error: {e}", done=True)
            return f"Error: {e}"

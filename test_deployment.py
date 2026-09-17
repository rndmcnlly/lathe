#!/usr/bin/env python3
"""Open WebUI loader and dispatch tests using an isolated toolkit ID.

By default this suite temporarily deploys the exact local ``lathe.py`` as
``lathe_test``. It never updates or invokes the production ``lathe`` toolkit.
The staging toolkit uses a distinct Daytona deployment label and persistent
volumes are disabled, so its sandboxes are also isolated and disposable. The
staging toolkit and sandboxes are deleted when the suite exits, including after
failures and ``--no-deploy`` runs.

Usage:
    uv run python test_deployment.py
    uv run python test_deployment.py --no-deploy
    uv run python test_deployment.py --verbose

Requires ``OWUI_URL``, ``OWUI_TOKEN``, ``OWUI_MODEL``, and
``DAYTONA_API_KEY`` in the environment or ``.env``.
"""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
import uuid

import httpx
import socketio
from dotenv import load_dotenv


load_dotenv()

OWUI_BASE = os.environ.get("OWUI_URL", "").rstrip("/")
OWUI_TOKEN = os.environ.get("OWUI_TOKEN", "")
MODEL = os.environ.get("OWUI_MODEL", "")
DAYTONA_API_KEY = os.environ.get("DAYTONA_API_KEY", "")
TOOL_ID = os.environ.get("LATHE_TEST_TOOL_ID", "lathe_test")
DEPLOYMENT_LABEL = "lathe-owui-deployment-test"
SOURCE_PATH = Path(__file__).with_name("lathe.py")
VERBOSE = False
PREVIEW_WRAPPER_URL = os.environ.get("LATHE_PREVIEW_WRAPPER_URL", "")
PREVIEW_WRAPPER_KEY = os.environ.get("LATHE_PREVIEW_WRAPPER_KEY", "")
PREVIEW_EXPECTED_URL = os.environ.get("LATHE_PREVIEW_EXPECTED_URL", "")
PREVIEW_EXPECTED_PATTERN = os.environ.get("LATHE_PREVIEW_EXPECTED_PATTERN", "")
PREVIEW_REVOKE_URL = os.environ.get("LATHE_PREVIEW_REVOKE_URL", "")


def staged_source():
    source = SOURCE_PATH.read_text()
    if "--preview-only" in sys.argv:
        # expose has no pydantic-ai dependency. Avoid changing the shared OWUI
        # environment while qualifying only this path beside an older toolkit.
        # All executable source remains identical; only install metadata differs.
        source = re.sub(r'^requirements:.*\n', '', source, count=1, flags=re.MULTILINE)
    return source

# 1x1 transparent PNG, used to exercise view() end-to-end.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


EXPECTED_SCHEMA = {
    "lathe": {"manpage": ("string", False, "overview")},
    "handoff": {},
    "destroy": {},
    "onboard": {"path": ("string", True, None)},
    "bash": {
        "command": ("string", True, None),
        "workdir": ("string", False, "/home/daytona/workspace"),
        "foreground_seconds": ("integer", False, -1),
    },
    "read": {
        "path": ("string", True, None),
        "start": ("integer", False, 1),
        "stop": ("integer", False, 0),
    },
    "write": {
        "path": ("string", True, None),
        "content": ("string", True, None),
    },
    "edit": {
        "path": ("string", True, None),
        "old_string": ("string", True, None),
        "new_string": ("string", True, None),
        "replace_all": ("boolean", False, False),
    },
    "glob": {
        "pattern": ("string", True, None),
        "max_lines": ("integer", False, 100),
    },
    "grep": {
        "pattern": ("string", True, None),
        "files": ("string", False, "**/*"),
        "max_lines": ("integer", False, 100),
    },
    "interpret": {
        "code": ("string", True, None),
        "timeout": ("integer", False, 120),
    },
    "view": {"path": ("string", True, None)},
    "delegate": {
        "task": ("string", True, None),
        "context_files": ("array", False, []),
        "max_steps": ("integer", False, 10),
        "foreground_seconds": ("integer", False, -1),
    },
    "expose": {"target": ("string", True, None)},
}


def auth_headers():
    return {"Authorization": f"Bearer {OWUI_TOKEN}", "Content-Type": "application/json"}


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


class Results:
    def __init__(self):
        self.scenarios = 0
        self.failed = 0

    async def run(self, name, fn):
        self.scenarios += 1
        print(f"\n-- {name} --")
        try:
            await fn()
            print(f"  PASS: {name}")
        except Exception as exc:
            self.failed += 1
            print(f"  FAIL: {name}: {type(exc).__name__}: {exc}")


async def deploy_staging_tool():
    source = staged_source()
    payload = {
        "id": TOOL_ID,
        "name": "Lathe Test",
        "content": source,
        "meta": {"description": "Ephemeral deployment-test copy of Lathe"},
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}",
            headers=auth_headers(),
            timeout=30,
        )
        if response.status_code == 404:
            response = await client.post(
                f"{OWUI_BASE}/api/v1/tools/create",
                headers=auth_headers(),
                json=payload,
                timeout=120,
            )
        else:
            response.raise_for_status()
            response = await client.post(
                f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/update",
                headers=auth_headers(),
                json=payload,
                timeout=120,
            )
        response.raise_for_status()

        valves = {
            "daytona_api_key": DAYTONA_API_KEY,
            "daytona_api_url": "https://app.daytona.io/api",
            "daytona_proxy_url": "https://proxy.app.daytona.io/toolbox",
            "deployment_label": DEPLOYMENT_LABEL,
            "auto_stop_minutes": 15,
            "auto_archive_minutes": 60,
            "auto_delete_minutes": 60,
            "persistent_volume": False,
            "auto_create_sandbox": True,
            "sandbox_missing_message": "",
            "sandbox_create_overrides": "{}",
            "foreground_timeout_seconds": 30,
            "preview_wrapper_url": PREVIEW_WRAPPER_URL,
            "preview_wrapper_key": PREVIEW_WRAPPER_KEY,
            "preview_expiry_seconds": 86400,
        }
        response = await client.post(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/valves/update",
            headers=auth_headers(),
            json=valves,
            timeout=30,
        )
        response.raise_for_status()


async def fetch_staging_tool():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}",
            headers=auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


async def delete_staging_tool():
    """Delete the suite-owned OWUI toolkit if it exists."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}",
            headers=auth_headers(),
            timeout=30,
        )
        if response.status_code == 404:
            return
        response.raise_for_status()
        response = await client.delete(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/delete",
            headers=auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        require(response.json() is True, f"toolkit deletion returned {response.text}")


async def cleanup_test_sandboxes():
    """Delete only sandboxes carrying the staging deployment-label key."""
    headers = {
        "Authorization": f"Bearer {DAYTONA_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://app.daytona.io/api/sandbox",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        sandboxes = payload if isinstance(payload, list) else payload.get("items", [])
        for sandbox in sandboxes:
            if DEPLOYMENT_LABEL not in sandbox.get("labels", {}):
                continue
            response = await client.delete(
                f"https://app.daytona.io/api/sandbox/{sandbox['id']}",
                params={"force": "true"},
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()


class OWUIClient:
    def __init__(self, tool_id=TOOL_ID):
        self.tool_id = tool_id
        self.sio = socketio.AsyncClient()
        self.session_id = None
        self.events = []
        self.done = asyncio.Event()

        @self.sio.event
        async def connect():
            self.session_id = self.sio.sid

        @self.sio.on("*")
        async def catch_all(event, *args):
            data = args[0] if args else None
            self.events.append({"event": event, "data": data})
            if VERBOSE:
                print(f"  socket {event}: {json.dumps(data, default=str)[:300]}")
            if event != "events" or not isinstance(data, dict):
                return
            inner = data.get("data", {})
            if inner.get("type") == "chat:completion" and inner.get("data", {}).get("done"):
                self.done.set()

    async def connect(self):
        await self.sio.connect(
            OWUI_BASE,
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            auth={"token": OWUI_TOKEN},
            wait_timeout=15,
        )
        await self.sio.emit("user-join", {"auth": {"token": OWUI_TOKEN}})
        await asyncio.sleep(0.5)

    async def close(self):
        if self.sio.connected:
            await self.sio.disconnect()

    async def send(self, prompt, *, timeout=180):
        self.events.clear()
        self.done.clear()
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "chat_id": f"local:{uuid.uuid4()}",
            "id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "tool_ids": [self.tool_id],
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{OWUI_BASE}/api/chat/completions",
                headers=auth_headers(),
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
        await asyncio.wait_for(self.done.wait(), timeout=timeout)

        output = []
        for event in self.events:
            if event["event"] != "events":
                continue
            inner = (event.get("data") or {}).get("data", {})
            if inner.get("type") != "chat:completion":
                continue
            data = inner.get("data", {})
            if "output" in data:
                output = data["output"]
        return output


def tool_calls(output):
    return [item for item in output if item.get("type") == "function_call"]


def tool_outputs(output):
    results = []
    for item in output:
        if item.get("type") != "function_call_output":
            continue
        value = item.get("output", "")
        if isinstance(value, list):
            value = "".join(part.get("text", "") for part in value if isinstance(part, dict))
        results.append(str(value))
    return results


async def main():
    global VERBOSE
    VERBOSE = "--verbose" in sys.argv
    deploy = "--no-deploy" not in sys.argv
    if TOOL_ID == 'lathe':
        print('Refusing to use the production toolkit ID for the disposable deployment suite.')
        return 2
    preview_only = "--preview-only" in sys.argv
    preview_enabled = all([PREVIEW_WRAPPER_URL, PREVIEW_WRAPPER_KEY, PREVIEW_EXPECTED_URL or PREVIEW_EXPECTED_PATTERN, PREVIEW_REVOKE_URL])
    if preview_only and not preview_enabled:
        print("Preview-only tests require wrapper URL/key, expected URL, and revoke URL in LATHE_PREVIEW_* variables.")
        return 2
    missing = [
        name
        for name, value in (
            ("OWUI_URL", OWUI_BASE),
            ("OWUI_TOKEN", OWUI_TOKEN),
            ("OWUI_MODEL", MODEL),
            ("DAYTONA_API_KEY", DAYTONA_API_KEY),
        )
        if not value
    ]
    if missing:
        print(f"Error: missing {', '.join(missing)}")
        return 2

    started = time.monotonic()
    results = Results()
    client = OWUIClient()
    cleanup_failed = False
    canary = f"OWUI_{uuid.uuid4().hex}"
    path = f"/home/daytona/workspace/{canary}.txt"
    preview_url = None

    async def exact_source_and_schema():
        remote = await fetch_staging_tool()
        local_source = staged_source()
        remote_source = remote.get("content", "")
        require(
            remote_source == local_source,
            "staging source differs: local "
            f"{hashlib.sha256(local_source.encode()).hexdigest()[:12]}, remote "
            f"{hashlib.sha256(remote_source.encode()).hexdigest()[:12]}",
        )

        specs = {spec["name"]: spec for spec in remote.get("specs", [])}
        require(set(specs) == set(EXPECTED_SCHEMA), f"tool names: {sorted(specs)}")
        for name, expected_params in EXPECTED_SCHEMA.items():
            schema = specs[name]["parameters"]
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            require(set(properties) == set(expected_params), f"{name} params: {properties}")
            for param, (expected_type, is_required, default) in expected_params.items():
                actual = properties[param]
                require(actual.get("type") == expected_type, f"{name}.{param}: {actual}")
                require((param in required) == is_required, f"{name}.{param} required={required}")
                if not is_required:
                    require(actual.get("default") == default, f"{name}.{param}: {actual}")

    async def bash_dispatch():
        output = await client.send(
            f"Call bash exactly once with command printf {canary}. Do not use another tool."
        )
        calls = tool_calls(output)
        require(calls and calls[0].get("name") == "bash", calls)
        require(any(canary in value for value in tool_outputs(output)), output)

    async def write_and_read_dispatch():
        output = await client.send(
            f"Call write exactly once to write the exact text {canary} to {path}."
        )
        require(any(call.get("name") == "write" for call in tool_calls(output)), output)
        require(any("Wrote" in value for value in tool_outputs(output)), output)

        output = await client.send(f"Call read exactly once for {path}.")
        require(any(call.get("name") == "read" for call in tool_calls(output)), output)
        require(any(canary in value for value in tool_outputs(output)), output)

    async def interpreter_dispatch():
        output = await client.send(
            f"Call interpret exactly once with Python code print('{canary}')."
        )
        require(any(call.get("name") == "interpret" for call in tool_calls(output)), output)
        require(any(canary in value for value in tool_outputs(output)), output)

    async def view_dispatch():
        png_path = f"/home/daytona/workspace/{canary}.png"
        output = await client.send(
            "Call bash exactly once with command: "
            f"printf '%s' '{PNG_B64}' | base64 -d > {png_path}"
        )
        require(any(call.get("name") == "bash" for call in tool_calls(output)), output)

        output = await client.send(f"Call view exactly once for {png_path}.")
        require(any(call.get("name") == "view" for call in tool_calls(output)), output)
        values = tool_outputs(output)
        # OWUI's image-return convention (0.11.0+): the data URI is lifted
        # out of the text channel, leaving this summary in its place.
        # Raw base64 in the text channel means the convention did not fire.
        require(any("Image file read successfully" in v for v in values), values)
        require(not any(PNG_B64[:40] in v for v in values),
                f"raw base64 leaked into text channel: {values[0][:120]}")

    async def delegate_dispatch():
        output = await client.send(
            "Call delegate exactly once with max_steps=3 and foreground_seconds=120. "
            f"The delegated task is: use bash to run printf {canary}, then report the output.",
            timeout=240,
        )
        require(any(call.get("name") == "delegate" for call in tool_calls(output)), output)
        values = tool_outputs(output)
        require(any(canary in value for value in values), values)

    async def protected_preview_dispatch():
        nonlocal preview_url
        output = await client.send(
            "Call bash to run this exact command: "
            "nohup python3 -m http.server 8765 >/tmp/lathe-preview-test.log 2>&1 &\n"
            "Then call expose with target http:8765. Return the resulting protected URL."
        )
        require(any(call.get('name') == 'expose' for call in tool_calls(output)), 'Model did not call expose')
        values = '\n'.join(tool_outputs(output))
        urls = re.findall(r'https://[^\s]+', values)
        preview_url = next((u for u in urls if (re.fullmatch(PREVIEW_EXPECTED_PATTERN, u) if PREVIEW_EXPECTED_PATTERN else u == PREVIEW_EXPECTED_URL)), None)
        require(preview_url and 'Owner-authenticated preview' in values,
                'Protected preview result missing expected URL/access mode')
        require(PREVIEW_WRAPPER_KEY not in values, 'Installation credential leaked')
        require('daytonaproxy' not in values and '.proxy.daytona.work' not in values,
                'Upstream hostname leaked')

    try:
        if deploy:
            print(f"Deploying local lathe.py to isolated toolkit {TOOL_ID!r}...")
            await deploy_staging_tool()
        await cleanup_test_sandboxes()

        await results.run("exact staged source and complete OWUI schema", exact_source_and_schema)
        await client.connect()
        if not preview_only:
            await results.run("model to OWUI to bash dispatch", bash_dispatch)
            await results.run("write and read dispatch", write_and_read_dispatch)
            await results.run("interpreter dispatch", interpreter_dispatch)
            await results.run("view dispatch", view_dispatch)
            await results.run("delegate dispatch", delegate_dispatch)
        if preview_enabled:
            await results.run("model to OWUI to owner-authenticated expose", protected_preview_dispatch)
    finally:
        await client.close()
        if preview_enabled:
            try:
                async with httpx.AsyncClient() as http:
                    revoke_url = PREVIEW_REVOKE_URL
                    if '{label}' in revoke_url:
                        from urllib.parse import urlsplit
                        require(preview_url, 'Cannot identify the preview registration for cleanup')
                        revoke_url = revoke_url.replace('{label}', urlsplit(preview_url).hostname.split('.')[0])
                    response = await http.delete(revoke_url,
                        headers={"Authorization": f"Bearer {PREVIEW_WRAPPER_KEY}"}, timeout=30)
                    response.raise_for_status()
            except Exception:
                cleanup_failed = True
                print("Preview registration cleanup failed")
        try:
            await cleanup_test_sandboxes()
        except Exception as exc:
            cleanup_failed = True
            print(f"Sandbox cleanup warning: {exc}")
        try:
            await delete_staging_tool()
        except Exception as exc:
            cleanup_failed = True
            print(f"Toolkit cleanup warning: {exc}")

    elapsed = time.monotonic() - started
    print(f"\n{results.scenarios - results.failed}/{results.scenarios} scenarios passed in {elapsed:.1f}s")
    return 1 if results.failed or cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

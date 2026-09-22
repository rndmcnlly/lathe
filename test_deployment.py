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
from testing_support import EXPECTED_SCHEMA, normalize_schema


if os.environ.get("LATHE_ISOLATED_CI") != "1":
    load_dotenv()

OWUI_BASE = os.environ.get("OWUI_URL", "").rstrip("/")
OWUI_TOKEN = os.environ.get("OWUI_TOKEN", "")
MODEL = os.environ.get("OWUI_MODEL", "")
DAYTONA_API_KEY = os.environ.get("DAYTONA_API_KEY", "")
TOOL_ID = os.environ.get("LATHE_TEST_TOOL_ID", "lathe_test")
DEPLOYMENT_LABEL = os.environ.get("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-owui-deployment-test")
if not (DEPLOYMENT_LABEL == "lathe-owui-deployment-test"
        or re.fullmatch(r"lathe-ci-[a-z0-9-]{1,48}", DEPLOYMENT_LABEL)):
    raise ValueError("Deployment tests require a suite-owned staging label")
SOURCE_PATH = Path(__file__).with_name("lathe.py")
VERBOSE = False
# Optional observer for the disposable monitor, without headers or credentials.
TRACE = lambda kind, **data: None
PREVIEW_WRAPPER_URL = os.environ.get("LATHE_PREVIEW_WRAPPER_URL", "")
PREVIEW_WRAPPER_KEY = os.environ.get("LATHE_PREVIEW_WRAPPER_KEY", "")
PREVIEW_EXPECTED_URL = os.environ.get("LATHE_PREVIEW_EXPECTED_URL", "")
PREVIEW_EXPECTED_PATTERN = os.environ.get("LATHE_PREVIEW_EXPECTED_PATTERN", "")
PREVIEW_REVOKE_URL = os.environ.get("LATHE_PREVIEW_REVOKE_URL", "")
PREVIEW_ACCESS = os.environ.get("LATHE_PREVIEW_ACCESS", "private")


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
        TRACE("scenario_start", name=name)
        print(f"\n-- {name} --")
        try:
            await fn()
            TRACE("scenario_pass", name=name)
            print(f"  PASS: {name}")
            return True
        except Exception as exc:
            self.failed += 1
            TRACE("scenario_fail", name=name, error=f"{type(exc).__name__}: {exc}")
            print(f"  FAIL: {name}: {type(exc).__name__}: {exc}")
            return False


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
            "auto_stop_minutes": 5,
            "auto_archive_minutes": 5,
            "auto_delete_minutes": 0,
            "persistent_volume": False,
            "auto_create_sandbox": True,
            "sandbox_missing_message": "",
            "sandbox_create_overrides": '{"ttlMinutes":30}',
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
        sandboxes = []
        cursor = None
        seen = set()
        while True:
            response = await client.get(
                "https://app.daytona.io/api/sandbox", headers=headers,
                params={"cursor": cursor} if cursor else {}, timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            sandboxes.extend(payload if isinstance(payload, list) else payload.get("items", []))
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor:
                break
            require(cursor not in seen, "Daytona repeated a pagination cursor")
            seen.add(cursor)
        for sandbox in sandboxes:
            if DEPLOYMENT_LABEL not in sandbox.get("labels", {}):
                continue
            response = await client.delete(
                f"https://app.daytona.io/api/sandbox/{sandbox['id']}",
                params={"force": "true"},
                headers=headers,
                timeout=30,
            )
            if response.status_code != 404:
                response.raise_for_status()
            for _ in range(60):
                response = await client.get(
                    f"https://app.daytona.io/api/sandbox/{sandbox['id']}",
                    headers=headers, timeout=30,
                )
                if response.status_code == 404:
                    break
                response.raise_for_status()
                await asyncio.sleep(1)
            else:
                raise RuntimeError(f"Staging sandbox {sandbox['id']} did not finish deletion")
            TRACE("sandbox_deleted", id=sandbox["id"])


class OWUIClient:
    def __init__(self, tool_id=TOOL_ID):
        self.tool_id = tool_id
        self.sio = socketio.AsyncClient()
        self.session_id = None
        self.events = []
        self.done = asyncio.Event()
        self.max_calls = None
        self.budget_error = None
        self.last_output = []
        self.traced_results = 0

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
            detail = inner.get("data", {})
            if inner.get("type") == "chat:completion" and "output" in detail:
                self.last_output = detail["output"]
                completed = [item for item in self.last_output
                             if item.get("type") == "function_call_output"
                             and item.get("status") == "completed"]
                if len(completed) > self.traced_results:
                    fresh = completed[self.traced_results:]
                    ids = {item.get("call_id") for item in fresh}
                    TRACE("tool_progress", output=[item for item in self.last_output
                          if item.get("call_id") in ids])
                    self.traced_results = len(completed)
                if self.max_calls and len(tool_calls(self.last_output)) > self.max_calls:
                    self.budget_error = "Outer tool-call budget exceeded"
                    self.done.set()
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

    async def send(self, prompt, *, timeout=180, max_calls=None):
        self.events.clear()
        self.done.clear()
        self.max_calls = max_calls
        self.budget_error = None
        self.last_output = []
        self.traced_results = 0
        TRACE("prompt", prompt=prompt, timeout=timeout, max_calls=max_calls)
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "chat_id": f"local:{uuid.uuid4()}",
            "id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "tool_ids": [self.tool_id],
        }
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{OWUI_BASE}/api/chat/completions",
                        headers=auth_headers(),
                        json=payload,
                        timeout=30,
                    )
                    if response.is_error:
                        raise RuntimeError(
                            f"OWUI completion HTTP {response.status_code}: {response.text[:1000]}"
                        )
                    response.raise_for_status()
                await self.done.wait()
                require(not self.budget_error, self.budget_error)
        finally:
            TRACE("chat_output", output=self.last_output,
                  statuses=[e for e in self.events if e.get("event") == "events"
                            and (e.get("data") or {}).get("data", {}).get("type")
                            in ("status", "chat:error")])

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


def verify_journey(output, destination, marker):
    """Require observed discovery, delegation, and a subsequent inspection.

    Filesystem effects are checked separately through Daytona. In particular,
    assistant prose is never evidence that the overview or file was inspected.
    """
    calls = tool_calls(output)
    require(len(calls) <= 12, "Agent exceeded 12 outer tool calls")
    results = {item.get("call_id"): item for item in output
               if item.get("type") == "function_call_output"}
    overview_seen = False
    delegated = False
    inspected = False
    for call in calls:
        args = call.get("arguments") or "{}"
        args = json.loads(args) if isinstance(args, str) else args
        values = tool_outputs([results[call["call_id"]]]) if call.get("call_id") in results else []
        if call.get("name") == "lathe" and args.get("manpage", "overview") == "overview":
            overview_seen = any(value.strip() and not value.startswith(("Error:", "Unknown manpage"))
                                for value in values)
        if call.get("name") == "delegate":
            require(overview_seen, "Delegation happened before overview discovery")
            delegated = True
        if delegated and call.get("name") in ("read", "bash", "interpret"):
            inspected |= destination in json.dumps(args) and any(marker in value for value in values)
    require(overview_seen, "Agent did not consult the overview")
    require(delegated, "Agent did not use delegation")
    require(inspected, "Agent did not inspect the copied result after delegation")


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
    path = f"/home/daytona/workspace/contract-{uuid.uuid4().hex}.txt"
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
            actual = normalize_schema(specs[name]["parameters"])
            require(actual == expected_params, f"{name}: expected {expected_params}, got {actual}")

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
        # The value is in the sandbox, not in the delegate's task. Verify a
        # disk effect separately so repeating the instructions cannot pass.
        destination = path + ".copy"
        output = await client.send(
            "Call delegate exactly once with max_steps=5 and foreground_seconds=120. "
            f"The delegated task is: read {path} and write an exact copy of its contents to {destination}. "
            "Do not do the task yourself or call any other tool.",
            timeout=240,
        )
        require([call.get("name") for call in tool_calls(output)] == ["delegate"], output)
        output = await client.send(f"Call read exactly once for {destination}.")
        require([call.get("name") for call in tool_calls(output)] == ["read"], output)
        require(any(canary in value for value in tool_outputs(output)), output)

    async def protected_preview_dispatch():
        nonlocal preview_url
        output = await client.send(
            "Call bash to run this exact command: "
            "nohup python3 -m http.server 8765 >/tmp/lathe-preview-test.log 2>&1 &\n"
            f"Then call expose with target http:8765, access {PREVIEW_ACCESS}, and tag deployment-test. "
            "Return the resulting URL."
        )
        require(any(call.get('name') == 'expose' for call in tool_calls(output)), 'Model did not call expose')
        values = '\n'.join(tool_outputs(output))
        urls = re.findall(r'https://[^\s]+', values)
        preview_url = next((u for u in urls if (re.fullmatch(PREVIEW_EXPECTED_PATTERN, u) if PREVIEW_EXPECTED_PATTERN else u == PREVIEW_EXPECTED_URL)), None)
        expected_note = ('Owner-authenticated private preview' if PREVIEW_ACCESS == 'private'
                         else 'Public wrapped preview')
        require(preview_url and expected_note in values,
                'Protected preview result missing expected URL/access note')
        require(PREVIEW_WRAPPER_KEY not in values, 'Installation credential leaked')
        require('daytonaproxy' not in values and '.proxy.daytona.work' not in values,
                'Upstream hostname leaked')

    async def agent_experience():
        source = f"/home/daytona/workspace/journey-{uuid.uuid4().hex}.txt"
        destination = source + ".copy"
        marker = f"JOURNEY_{uuid.uuid4().hex}"
        output = await client.send(
            "You are using this coding environment for the first time. Consult its built-in "
            "overview before starting. "
            f"Create {source} containing exactly {marker} with no extra whitespace. "
            f"Ask a subagent to make an exact copy at {destination}, then inspect the copied "
            "contents yourself to verify the result. Wait for the work to finish. "
            "Use at most 12 tool calls and finish within 4 minutes.",
            timeout=240, max_calls=12,
        )
        verify_journey(output, destination, marker)
        # Bypass both the model's claims and Lathe's output formatting.
        async with httpx.AsyncClient() as http:
            headers = {"Authorization": f"Bearer {DAYTONA_API_KEY}"}
            response = await http.get("https://app.daytona.io/api/sandbox",
                params={"labels": json.dumps({DEPLOYMENT_LABEL: "monitor@lathe.invalid"})},
                headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            matches = [s for s in (data if isinstance(data, list) else data.get("items", []))
                       if s.get("labels", {}).get(DEPLOYMENT_LABEL) == "monitor@lathe.invalid"]
            require(len(matches) == 1, "Expected one monitor sandbox")
            sandbox = matches[0]
            require(not sandbox.get("volumes"), "Monitor sandbox has persistent volumes")
            for filename in (source, destination):
                response = await http.get(
                    f"https://proxy.app.daytona.io/toolbox/{sandbox['id']}/files/download",
                    params={"path": filename}, headers=headers, timeout=30)
                response.raise_for_status()
                require(response.content == marker.encode(), f"Exact canary mismatch: {filename}")
                TRACE("independent_read", path=filename, content=response.text, sandbox_id=sandbox["id"])

    try:
        if deploy:
            print(f"Deploying local lathe.py to isolated toolkit {TOOL_ID!r}...")
            await deploy_staging_tool()
        await cleanup_test_sandboxes()

        if not await results.run("exact staged source and complete OWUI schema", exact_source_and_schema):
            return 1
        await client.connect()
        scenarios = []
        if not preview_only:
            scenarios.extend([
                ("model to OWUI to bash dispatch", bash_dispatch),
                ("write and read dispatch", write_and_read_dispatch),
                ("interpreter dispatch", interpreter_dispatch),
                ("view dispatch", view_dispatch),
                ("delegate dispatch", delegate_dispatch),
            ])
        if preview_enabled:
            scenarios.append((f"model to OWUI to {PREVIEW_ACCESS} wrapped expose", protected_preview_dispatch))
        if os.environ.get("LATHE_ISOLATED_CI") == "1":
            scenarios.append(("first-time agent experience", agent_experience))
        for name, scenario in scenarios:
            if not await results.run(name, scenario):
                break
    finally:
        try:
            await client.close()
        except Exception as exc:
            cleanup_failed = True
            TRACE("cleanup_error", error=f"Socket cleanup: {exc}")
            print(f"Socket cleanup failed: {exc}")
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
                TRACE("cleanup_error", error="Preview registration cleanup failed")
                print("Preview registration cleanup failed")
        try:
            await cleanup_test_sandboxes()
        except Exception as exc:
            cleanup_failed = True
            TRACE("cleanup_error", error=f"Sandbox cleanup: {exc}")
            print(f"Sandbox cleanup warning: {exc}")
        try:
            await delete_staging_tool()
        except Exception as exc:
            cleanup_failed = True
            TRACE("cleanup_error", error=f"Toolkit cleanup: {exc}")
            print(f"Toolkit cleanup warning: {exc}")

    elapsed = time.monotonic() - started
    print(f"\n{results.scenarios - results.failed}/{results.scenarios} scenarios passed in {elapsed:.1f}s")
    return 1 if results.failed or cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

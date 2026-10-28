#!/usr/bin/env python3
"""Open WebUI loader and dispatch tests using an isolated toolkit ID.

By default this suite temporarily deploys the exact local ``lathe.py`` as
``lathe_test``. It never updates or invokes the production ``lathe`` toolkit.
The staging toolkit uses a distinct Daytona deployment label and provisions no
extra volumes, so its sandboxes are also isolated and disposable. The
staging toolkit and sandboxes are deleted when the suite exits, including after
failures and ``--no-deploy`` runs.

Usage:
    uv run python test_deployment.py
    uv run python test_deployment.py --no-deploy
    uv run python test_deployment.py --verbose

Requires ``OWUI_URL``, admin ``OWUI_TOKEN``, dedicated ``OWUI_TEST_TOKEN`` and
``OWUI_TEST_EMAIL``, ``OWUI_MODEL``, and ``DAYTONA_API_KEY`` in the environment
or ``.env``. The test account must be a separate ordinary user, not an admin.
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
OWUI_TEST_TOKEN = os.environ.get("OWUI_TEST_TOKEN", "")
OWUI_TEST_EMAIL = os.environ.get("OWUI_TEST_EMAIL", "")
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


# 1x1 transparent PNG, used to exercise view() end-to-end.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def auth_headers(*, completion=False):
    token = OWUI_TEST_TOKEN if completion else OWUI_TOKEN
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


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


async def session_identity(*, completion=False):
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{OWUI_BASE}/api/v1/auths/",
            headers=auth_headers(completion=completion), timeout=30)
        response.raise_for_status()
        return response.json()


async def check_test_identity():
    """Verify both credentials before setup, sockets, model requests or cleanup."""
    require(OWUI_TEST_TOKEN and OWUI_TEST_EMAIL,
            "Dedicated OWUI_TEST_TOKEN and OWUI_TEST_EMAIL are required")
    admin = await session_identity()
    user = await session_identity(completion=True)
    require(admin.get("id") and admin.get("role") == "admin",
            "OWUI_TOKEN must identify the setup administrator")
    require(user.get("id") and isinstance(user.get("email"), str)
            and user["email"].strip().lower() == OWUI_TEST_EMAIL.strip().lower(),
            "OWUI_TEST_TOKEN does not identify OWUI_TEST_EMAIL")
    require(user.get("role") == "user" and user["id"] != admin["id"],
            "Completion identity must be a dedicated non-admin user, separate from setup")
    return admin, user


def staging_grants(user):
    return [{"principal_type": "user", "principal_id": user["id"], "permission": "read"}]


async def check_completion_tools(*, staged):
    """Fail if the synthetic account can reach another local toolkit."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{OWUI_BASE}/api/v1/tools/",
            headers=auth_headers(completion=True), timeout=30)
        response.raise_for_status()
        ids = {tool["id"] for tool in response.json()}
    require(ids == {TOOL_ID} if staged else ids <= {TOOL_ID},
            "Test identity must have access only to the suite-owned staging toolkit")


async def check_staging_access(admin, user):
    remote = await fetch_staging_tool()
    require(remote.get("user_id") == admin["id"], "Staging toolkit is not owned by setup identity")
    grants = remote.get("access_grants")
    expected = staging_grants(user)
    require(isinstance(grants, list) and len(grants) == 1
            and all(grants[0].get(k) == v for k, v in expected[0].items()),
            "Staging toolkit must grant read access only to the test identity")
    await check_completion_tools(staged=True)


async def deploy_staging_tool(admin, user, *, display_name="Lathe Test"):
    source = SOURCE_PATH.read_text()
    payload = {
        "id": TOOL_ID,
        "name": display_name,
        "content": source,
        "meta": {"description": "Ephemeral deployment-test copy of Lathe"},
        "access_grants": [],
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
            require(response.json().get("user_id") == admin["id"],
                    "Refusing to overwrite staging toolkit owned by another account")
            # Revoke old grants before replacing code or installing credentials.
            response = await client.post(f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/access/update",
                headers=auth_headers(), json={"access_grants": []}, timeout=30)
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

        response = await client.post(f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/access/update",
            headers=auth_headers(), json={"access_grants": staging_grants(user)}, timeout=30)
        response.raise_for_status()


async def fetch_staging_tool(*, missing_ok=False):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}",
            headers=auth_headers(),
            timeout=30,
        )
        if missing_ok and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def delete_staging_tool(admin):
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
        require(response.json().get("user_id") == admin["id"],
                "Refusing to delete staging toolkit owned by another account")
        response = await client.delete(
            f"{OWUI_BASE}/api/v1/tools/id/{TOOL_ID}/delete",
            headers=auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        require(response.json() is True, f"toolkit deletion returned {response.text}")


async def labelled_sandboxes(http, label, emails=None):
    """IDs/labels of sandboxes carrying `label`, following every `nextCursor` page.

    GET /sandbox is eventually consistent and cursor-paginated (Daytona's
    ListSandboxesResponseDto: `items`, `nextCursor`). With `emails`, use Lathe's
    exact server-side {label: email} filter, for known owners. Without, scan for
    the label KEY with any owner value: cleanup cannot assume who created a
    sandbox. Errored/deleting records are included so stragglers stay visible.
    `http` carries the Daytona authorization header.
    """
    found = {}
    for email in emails or [None]:
        params = {"includeErroredDeleted": "true", "limit": 100}
        if email is not None:
            params["labels"] = json.dumps({label: email})
        cursor, seen = None, set()
        while True:
            response = await http.get("https://app.daytona.io/api/sandbox", timeout=30,
                                      params={**params, **({"cursor": cursor} if cursor else {})})
            response.raise_for_status()
            payload = response.json()
            for sandbox in payload if isinstance(payload, list) else payload.get("items", []):
                labels = sandbox.get("labels") or {}
                if label in labels and (email is None or labels[label] == email):  # Re-check the server.
                    found[sandbox["id"]] = {"id": sandbox["id"], "labels": {label: labels[label]}}
            cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
            if not cursor:
                break
            require(cursor not in seen, "Daytona repeated a pagination cursor")
            seen.add(cursor)
    return list(found.values())


async def cleanup_test_sandboxes():
    """Delete only sandboxes carrying the staging deployment-label key."""
    headers = {
        "Authorization": f"Bearer {DAYTONA_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(headers=headers) as client:
        for sandbox in await labelled_sandboxes(client, DEPLOYMENT_LABEL):
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
        self.active_request = None

        @self.sio.event
        async def connect():
            self.session_id = self.sio.sid

        @self.sio.on("*")
        async def catch_all(event, *args):
            data = args[0] if args else None
            # OWUI emits to the entire account room, including native child-chat
            # and resumed-parent events. Filter before retaining or logging data.
            if (self.active_request is None or event != "events" or not isinstance(data, dict)
                    or (data.get("chat_id"), data.get("message_id")) != self.active_request):
                return
            inner = data.get("data")
            if not isinstance(inner, dict):
                return
            detail = inner.get("data", {})
            if not isinstance(detail, dict):
                return
            self.events.append({"event": event, "data": data})
            if VERBOSE:
                print(f"  socket {event}: {json.dumps(data, default=str)[:300]}")
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
            auth={"token": OWUI_TEST_TOKEN},
            wait_timeout=15,
        )
        await self.sio.emit("user-join", {"auth": {"token": OWUI_TEST_TOKEN}})
        await asyncio.sleep(0.5)

    async def close(self):
        if self.sio.connected:
            await self.sio.disconnect()

    async def send(self, prompt, *, timeout=180, max_calls=None):
        require(self.active_request is None, "OWUIClient supports only one active request")
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
        self.active_request = (payload["chat_id"], payload["id"])
        started = time.monotonic()
        acceptance = {}
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{OWUI_BASE}/api/chat/completions",
                        headers=auth_headers(completion=True),
                        json=payload,
                        timeout=30,
                    )
                    if response.is_error:
                        raise RuntimeError(
                            f"OWUI completion HTTP {response.status_code}: {response.text[:1000]}"
                        )
                    response.raise_for_status()
                    acceptance = {"http_status": response.status_code,
                                  "task_id": response.json().get("task_id")}
                    TRACE("completion_accepted", **acceptance)
                await self.done.wait()
                require(not self.budget_error, self.budget_error)
        finally:
            self.active_request = None
            statuses = [e for e in self.events
                        if e["data"]["data"].get("type") in ("status", "chat:error")]
            diagnostics = dict(elapsed=round(time.monotonic() - started, 3),
                               done=self.done.is_set(), **acceptance,
                               completed_calls=sum(item.get("type") == "function_call_output"
                                                   and item.get("status") == "completed"
                                                   for item in self.last_output),
                               last_status=statuses[-1]["data"]["data"] if statuses else None)
            TRACE("request_end", **diagnostics)
            if not self.done.is_set():
                print(f"  Incomplete OWUI request: {json.dumps(diagnostics, default=str)}")
            TRACE("chat_output", output=self.last_output,
                  statuses=statuses)

        return self.last_output


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
    """Require observed discovery, direct copying, and a separate read.

    Filesystem effects are checked separately through Daytona. In particular,
    assistant prose is never evidence that the overview or file was inspected.
    """
    calls = tool_calls(output)
    require(len(calls) <= 12, "Agent exceeded 12 outer tool calls")
    results = {item.get("call_id"): item for item in output
               if item.get("type") == "function_call_output"}
    overview_seen = False
    copied = False
    inspected = False
    for call in calls:
        args = call.get("arguments") or "{}"
        args = json.loads(args) if isinstance(args, str) else args
        values = tool_outputs([results[call["call_id"]]]) if call.get("call_id") in results else []
        if call.get("name") == "lathe" and args.get("manpage", "overview") == "overview":
            overview_seen = any(value.strip() and not value.startswith(("Error:", "Unknown manpage"))
                                for value in values)
        name = call.get("name")
        # File tools require absolute paths; shell/Python programs may use
        # relative paths. This only identifies a candidate mutation: the
        # separate read and independent Daytona downloads prove its effects.
        target = Path(destination).name if name in ("bash", "interpret") else destination
        if name in ("write", "bash", "interpret") and target in json.dumps(args):
            require(overview_seen, "Copying happened before overview discovery")
            copied |= any(value.strip() and not value.startswith(("Error:", "Exit code:")) for value in values)
        if copied and call.get("name") == "read":
            inspected |= args.get("path") == destination and any(marker in value for value in values)
    require(overview_seen, "Agent did not consult the overview")
    require(copied, "Agent did not directly copy the file")
    require(inspected, "Agent did not separately read the destination after copying")


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
            ("OWUI_TEST_TOKEN", OWUI_TEST_TOKEN),
            ("OWUI_TEST_EMAIL", OWUI_TEST_EMAIL),
            ("OWUI_MODEL", MODEL),
            ("DAYTONA_API_KEY", DAYTONA_API_KEY),
        )
        if not value
    ]
    if missing:
        print(f"Error: missing {', '.join(missing)}")
        return 2

    try:
        admin, user = await check_test_identity()
        remote = await fetch_staging_tool(missing_ok=True)
        require(remote is None or remote.get("user_id") == admin["id"],
                "Refusing to use staging toolkit owned by another account")
    except Exception as exc:
        print(f"Deployment-test preflight failed: {type(exc).__name__}: {exc}")
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
        local_source = SOURCE_PATH.read_text()
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
        # As in interpreter_dispatch, keep the expected output out of the prompt.
        output = await client.send(
            f"Call bash exactly once with command printf {canary[::-1]} | rev. Do not use another tool."
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
        # The prompt must not contain the expected output: a model can echo a
        # literal canary as text without calling the tool.
        output = await client.send(
            f"Call interpret exactly once with Python code print('{canary[::-1]}'[::-1])."
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

    async def computer_dispatch():
        output = await client.send('Call poke exactly once with command "start" and args {}.')
        require(any(call.get("name") == "poke" for call in tool_calls(output)), "Model did not dispatch poke")
        for _ in range(15):
            output = await client.send('Call peek exactly once with command "status" and args {}.')
            require(any(call.get("name") == "peek" for call in tool_calls(output)), "Model did not dispatch peek")
            if any("Desktop is active" in v for v in tool_outputs(output)):
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("Desktop did not become ready through OWUI")
        output = await client.send('Call peek exactly once with command "screenshot.compressed" and args {"format":"png","quality":60}.')
        require(any(call.get("name") == "peek" for call in tool_calls(output)), "Model did not request screenshot")
        values = tool_outputs(output)
        require(any("Image file read successfully" in v for v in values), "OWUI did not lift the desktop image")
        require(not any("data:image/" in v for v in values), "Desktop image leaked as text")

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
            f"Make an exact copy at {destination}, then use a separate read tool call on "
            "the copied file to verify its contents. "
            "Use at most 12 tool calls and finish within 4 minutes.",
            timeout=240, max_calls=12,
        )
        verify_journey(output, destination, marker)
        # Bypass both the model's claims and Lathe's output formatting.
        async with httpx.AsyncClient() as http:
            headers = {"Authorization": f"Bearer {DAYTONA_API_KEY}"}
            http.headers.update(headers)
            matches = await labelled_sandboxes(http, DEPLOYMENT_LABEL, [user["email"].strip().lower()])
            require(len(matches) == 1, f"Expected one test-identity sandbox, observed {len(matches)}")
            response = await http.get(f"https://app.daytona.io/api/sandbox/{matches[0]['id']}", timeout=30)
            response.raise_for_status()
            sandbox = response.json()  # Authoritative per-ID record, not the index.
            require(not sandbox.get("volumes"), "Monitor sandbox has persistent volumes")
            for filename in (source, destination):
                response = await http.get(
                    f"https://proxy.app.daytona.io/toolbox/{sandbox['id']}/files/download",
                    params={"path": filename}, headers=headers, timeout=30)
                response.raise_for_status()
                require(response.content == marker.encode(), f"Exact canary mismatch: {filename}")
                TRACE("independent_read", path=filename, content=response.text, sandbox_id=sandbox["id"])

    try:
        # Identity and ownership authorize cleanup, even when later access
        # checks reject an intentionally staged --no-deploy installation.
        await check_completion_tools(staged=False)
        if deploy:
            print(f"Deploying local lathe.py to isolated toolkit {TOOL_ID!r}...")
            await deploy_staging_tool(admin, user)
        await check_staging_access(admin, user)
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
                ("peek/poke dispatch and native desktop image", computer_dispatch),
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
            await delete_staging_tool(admin)
        except Exception as exc:
            cleanup_failed = True
            TRACE("cleanup_error", error=f"Toolkit cleanup: {exc}")
            print(f"Toolkit cleanup warning: {exc}")

    elapsed = time.monotonic() - started
    print(f"\n{results.scenarios - results.failed}/{results.scenarios} scenarios passed in {elapsed:.1f}s")
    return 1 if results.failed or cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

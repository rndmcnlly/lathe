#!/usr/bin/env python3
"""Live Daytona contract tests for Lathe.

These tests call ``Tools`` directly. They verify the boundary between Lathe and
Daytona, but deliberately do not claim to cover Open WebUI loading or dispatch.

Usage:
    uv run python test_integration.py

Requires ``DAYTONA_API_KEY`` in the environment or ``.env``.
"""

import asyncio
import json
import os
import sys
import time
import traceback
import uuid

import httpx
from dotenv import load_dotenv

from lathe import Tools, _api, _extract_sandbox_list, _headers, _toolbox


load_dotenv()

API_KEY = os.environ.get("DAYTONA_API_KEY", "")
DEPLOYMENT_LABEL = "lathe-integration-test"
TEST_EMAIL = "runner@lathe-integration.test"
WORKSPACE = "/home/daytona/workspace"

# 1x1 transparent PNG, used to exercise view().
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


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
            return True
        except Exception as exc:
            self.failed += 1
            print(f"  FAIL: {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            return False


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


async def emitter(event):
    data = event.get("data", {})
    description = data.get("description")
    if description:
        print(f"  status: {description}")


async def confirmed(_event):
    return True


async def _delete_test_sandboxes(tools: Tools):
    """Best-effort cleanup for the fixed, isolated test identity."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{tools.valves.daytona_api_url}/sandbox",
            params={"labels": json.dumps({DEPLOYMENT_LABEL: TEST_EMAIL})},
            headers=_headers(tools.valves),
            timeout=30,
        )
        response.raise_for_status()
        matches = [
            sandbox
            for sandbox in _extract_sandbox_list(response.json())
            if sandbox.get("labels", {}).get(DEPLOYMENT_LABEL) == TEST_EMAIL
        ]
        for sandbox in matches:
            sid = sandbox["id"]
            response = await client.delete(
                f"{tools.valves.daytona_api_url}/sandbox/{sid}",
                params={"force": "true"},
                headers=_headers(tools.valves),
                timeout=30,
            )
            if response.status_code == 409:
                # Only a deletion already in progress may be treated as success.
                current = await client.get(
                    f"{tools.valves.daytona_api_url}/sandbox/{sid}",
                    headers=_headers(tools.valves), timeout=30,
                )
                if current.status_code != 404:
                    current.raise_for_status()
                    require(current.json().get("state") in ("deleting", "destroying"),
                            f"Sandbox {sid} delete conflicted outside deletion")
            elif response.status_code != 404:
                response.raise_for_status()
            for _ in range(60):
                current = await client.get(
                    f"{tools.valves.daytona_api_url}/sandbox/{sid}",
                    headers=_headers(tools.valves), timeout=30,
                )
                if current.status_code == 404:
                    break
                current.raise_for_status()
                await asyncio.sleep(1)
            else:
                raise AssertionError(f"Sandbox {sid} did not finish deletion")


async def main():
    if not API_KEY:
        print("Error: DAYTONA_API_KEY is not set.")
        return 2

    tools = Tools()
    tools.valves.daytona_api_key = API_KEY
    tools.valves.deployment_label = DEPLOYMENT_LABEL
    tools.valves.auto_stop_minutes = 15
    tools.valves.auto_archive_minutes = 60
    tools.valves.auto_delete_minutes = -1

    user = {"email": TEST_EMAIL, "id": "lathe-integration", "name": "Lathe Integration"}
    chat_id = f"integration-{uuid.uuid4()}"
    ctx = {
        "__user__": user,
        "__chat_id__": chat_id,
        "__event_emitter__": emitter,
    }
    canary = f"LATHE_{uuid.uuid4().hex}"
    test_file = f"{WORKSPACE}/contract-{uuid.uuid4().hex}.txt"
    results = Results()
    started = time.monotonic()
    cleanup_failed = False

    await _delete_test_sandboxes(tools)

    async def core_tool_roundtrip():
        output = await tools.bash("printf 'sandbox-ready'", **ctx)
        require("sandbox-ready" in output, output)

        output = await tools.write(test_file, f"alpha\n{canary}\nomega\n", **ctx)
        require("Wrote" in output, output)
        output = await tools.read(test_file, start=2, stop=3, **ctx)
        require(canary in output and "alpha" not in output, output)
        output = await tools.edit(test_file, "omega", "OMEGA", **ctx)
        require("Replaced 1" in output, output)

        glob_output, grep_output = await asyncio.gather(
            tools.glob("contract-*.txt", **ctx),
            tools.grep(canary, files="contract-*.txt", **ctx),
        )
        require(test_file in glob_output, glob_output)
        require(canary in grep_output and test_file in grep_output, grep_output)

    async def view_tool_roundtrip():
        import base64 as _b64

        png_file = f"{WORKSPACE}/contract-{uuid.uuid4().hex}.png"
        await tools.bash(f"printf '%s' '{PNG_B64}' | base64 -d > {png_file}", **ctx)

        output = await tools.view(png_file, **ctx)
        require(output.startswith("data:image/png;base64,"), output[:100])
        payload = _b64.b64decode(output.removeprefix("data:image/png;base64,"))
        require(payload.startswith(b"\x89PNG\r\n\x1a\n"), payload[:16])

        # Non-image content is rejected by content sniffing, not extension
        output = await tools.view(test_file, **ctx)
        require(output.startswith("Error:") and "not a PNG" in output, output[:200])

        # Missing file
        output = await tools.view(f"{WORKSPACE}/no-such-image.png", **ctx)
        require("not found" in output.lower(), output[:200])

        # First call in a fresh chat queues the auto-init snapshot, but the
        # image channel must stay byte-exact (defer_harness_messages).
        fresh = {**ctx, "__chat_id__": f"integration-{uuid.uuid4()}"}
        output = await tools.view(png_file, **fresh)
        require(output.startswith("data:image/png;base64,"), output[:120])

    async def onboarding_and_interpreter():
        project = f"{WORKSPACE}/onboard-contract"
        await tools.write(
            f"{project}/AGENTS.md",
            "# Contract instructions\nKeep the canary visible.\n",
            **ctx,
        )
        output = await tools.onboard(project, **ctx)
        require("Keep the canary visible" in output, output)

        output = await tools.interpret("contract_value = 40; print(contract_value)", **ctx)
        require("40" in output, output)
        output = await tools.interpret("print(contract_value + 2)", **ctx)
        require("42" in output, output)

    async def multiworker_interpreter():
        # Fresh OS processes load the actual toolkit, with no shared Python
        # registry. Secrets travel over stdin, never command-line arguments.
        worker_source = """
import asyncio, json, sys
from lathe import Tools
options = json.load(sys.stdin)
tools = Tools()
tools.valves = tools.Valves(**options['valves'])
async def main():
    result = await tools.interpret(options['code'], __user__=options['user'], __chat_id__=options['chat'])
    print(json.dumps(result))
asyncio.run(main())
"""
        async def worker(code, chat=chat_id):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", worker_source,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            options = {"valves": tools.valves.model_dump(), "user": user, "chat": chat, "code": code}
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(json.dumps(options).encode()), timeout=180)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            require(process.returncode == 0, "Independent toolkit process failed")
            return json.loads(stdout)

        output = await worker("crossworker_value = 35")
        require(not output.startswith("Error:"), output)
        output = await worker("crossworker_value += 1; print(crossworker_value)")
        require(output.rstrip().endswith("36"), output)
        output = await tools.interpret("print(crossworker_value)", **ctx)
        require(output.rstrip().endswith("36"), output)

        fresh_chat = f"race-{uuid.uuid4()}"
        outputs = await asyncio.gather(*[
            worker("race_value = globals().get('race_value', 0) + 1; print(race_value)", fresh_chat)
            for _ in range(2)
        ])
        # Orientation may precede either success or an Error result on each
        # fresh worker. Classify the actual numeric output, not its prefix.
        admitted = [o for o in outputs if o.rstrip().splitlines()[-1] in {"1", "2"}]
        require(bool(admitted), outputs)
        require(all(o in admitted or "Session is executing run" in o for o in outputs), outputs)
        output = await worker("print(race_value)", fresh_chat)
        require(output.rstrip().endswith(str(len(admitted))), output)
        print("  independent processes: continuity, reload, simultaneous first-use admission")

    async def background_completion_notice():
        output = await tools.bash(
            "sleep 2; printf 'background-finished'",
            foreground_seconds=0,
            **ctx,
        )
        require("Backgrounded" in output, output)

        # Observe through ordinary non-bash calls on the launching worker: its
        # poller marks the job ready, and the next tool call claims the notice.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            output = await tools.read(test_file, **ctx)
            if "Background job completed" in output:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("background completion notice never arrived on a read")
        require(output.count("Background job completed") == 1, output)
        require("background-finished" in output, output)
        output = await tools.read(test_file, **ctx)
        require("Background job completed" not in output, output)

    async def background_notice_claimed_once_across_workers():
        # A second Tools instance stands in for another OWUI worker. Whichever
        # claims first (this worker's poller or the other's bash), one report.
        other = Tools()
        other.valves = tools.valves
        marker = f"claimed-once-{uuid.uuid4().hex}"
        output = await tools.bash(f"sleep 3; printf '{marker}'", foreground_seconds=0, **ctx)
        require("Backgrounded" in output, output)
        await asyncio.sleep(6)
        reports = (await other.bash("true", **ctx)).count(marker)
        await asyncio.sleep(15)  # outlast the launching worker's next poll
        reports += (await tools.read(test_file, **ctx)).count(marker)
        require(reports == 1, f"expected one completion report, saw {reports}")

    async def bounded_command_retention():
        import re
        output = await tools.bash("python3 -c 'print(\"x\" * 1000000)'", **ctx)
        require(len(output.encode()) < 53 * 1024, "bash preview exceeded byte budget")
        match = re.search(r"Full output \([^)]+\): ([^\s\]]+)", output)
        require(match is not None, output[-2000:])
        full_log = match[1]
        await tools.bash("echo unrelated-chat", **{**ctx, "__chat_id__": "retention-other"})
        output = await tools.read(full_log, **ctx)
        require("Text preview truncated" in output and len(output.encode()) < 53 * 1024, output[-2000:])
        output = await tools.bash(f"wc -c < {full_log}; cat {full_log.rsplit('/', 1)[0]}/exit", **ctx)
        require("1000001" in output, output)

        output = await tools.bash(
            "nohup python3 -m http.server 8766 >/tmp/lathe-retention-http.log 2>&1 &",
            **ctx,
        )
        require("Exit code:" not in output, output)
        for index in range(20):
            output = await tools.bash(f"printf 'retention-{index}'", **ctx)
            require(f"retention-{index}" in output, output)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                _api(tools.valves, "/sandbox"),
                params={"labels": json.dumps({DEPLOYMENT_LABEL: TEST_EMAIL})},
                headers=_headers(tools.valves), timeout=30,
            )
            response.raise_for_status()
            sandbox = next(
                item for item in _extract_sandbox_list(response.json())
                if item.get("labels", {}).get(DEPLOYMENT_LABEL) == TEST_EMAIL
            )
            sandbox_id = sandbox["id"]

            response = await client.get(
                _toolbox(tools.valves, sandbox_id, "/process/session"),
                headers=_headers(tools.valves), timeout=30,
            )
            response.raise_for_status()
            sessions = [
                session for session in response.json()
                if str(session.get("sessionId", session.get("id", ""))).startswith("lathe-cmd-")
            ]
            inspect_script = (
                "import json,os; root='/dev/shm/lathe/cmd'; "
                "print(json.dumps({d: sorted(os.listdir(root+'/'+d)) for d in os.listdir(root)} "
                "if os.path.isdir(root) else {}))"
            )
            response = await client.post(
                _toolbox(tools.valves, sandbox_id, "/process/execute"),
                headers=_headers(tools.valves),
                json={"command": f"python3 -c {json.dumps(inspect_script)}", "timeout": 5000},
                timeout=15,
            )
            response.raise_for_status()
            sidecars = json.loads(response.json()["result"].strip())
            require(len(sessions) == 1, f"expected one service session, got {len(sessions)}")
            require(len(sidecars) >= 21, sidecars)
            require(all("log" in names and "exit" in names for names in sidecars.values()), sidecars)
            require(sum("keep" in names for names in sidecars.values()) == 1, sidecars)
            print(f"  measured: {len(sessions)} service session, {len(sidecars)} retained results")

            response = await client.post(
                _toolbox(tools.valves, sandbox_id, "/process/execute"),
                headers=_headers(tools.valves),
                json={"command": "curl -fsS http://127.0.0.1:8766 >/dev/null", "timeout": 5000},
                timeout=15,
            )
            response.raise_for_status()
            require(response.json().get("exitCode") == 0, "retained HTTP service was killed")
            await client.post(
                _toolbox(tools.valves, sandbox_id, "/process/execute"),
                headers=_headers(tools.valves),
                json={"command": "pkill -f '[h]ttp.server 8766'; sleep 1", "timeout": 5000},
                timeout=15,
            )

        await tools.bash("true", **ctx)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _toolbox(tools.valves, sandbox_id, "/process/session"),
                headers=_headers(tools.valves), timeout=30,
            )
            response.raise_for_status()
            sessions = [
                session for session in response.json()
                if session.get("sessionId", "").startswith("lathe-cmd-")
            ]
            require(not sessions, f"exited service session was not reaped: {sessions}")
            expire_script = (
                "import os,time; root='/dev/shm/lathe/cmd'; "
                "[(os.utime(root+'/'+d+'/exit', (time.time()-86401, time.time()-86401))) "
                "for d in os.listdir(root) if os.path.isfile(root+'/'+d+'/exit')]"
            )
            response = await client.post(
                _toolbox(tools.valves, sandbox_id, "/process/execute"),
                headers=_headers(tools.valves),
                json={"command": f"python3 -c {json.dumps(expire_script)}", "timeout": 5000}, timeout=15,
            )
            response.raise_for_status()
        await tools.bash("echo expire-old-results", **ctx)
        output = await tools.read(full_log, **ctx)
        require(output.startswith("Error:") and "File not found" in output, output)

    async def expose_contract():
        import re
        await tools.bash(
            "nohup python3 -m http.server 8765 >/tmp/lathe-http.log 2>&1 &",
            **ctx,
        )
        output = await tools.expose("http:8765", "public", **ctx)
        require("Service URL" in output and "bearer credential" in output, "Direct preview result missing expected access description")
        match = re.search(r'https://\S+', output)
        require(match is not None, "Direct preview URL missing")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(match.group(), headers={"X-Daytona-Skip-Preview-Warning":"true"}, timeout=20)
                require(response.status_code == 200, "Signed preview did not reach the test service")
        except Exception:
            raise AssertionError("Direct signed-preview reachability failed") from None

    async def computer_roundtrip():
        import base64
        import re
        import shlex

        output = await tools.poke("start", **ctx)
        require(not output.startswith("Error:"), output)
        for _ in range(30):
            output = await tools.peek("status", **ctx)
            if "Desktop is active." in output:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError(f"Desktop did not become active: {output}")

        for command in ("display.info", "display.windows", "mouse.position"):
            output = await tools.peek(command, **ctx)
            require(not output.startswith("Error:"), output)
        for command, args in [
            ("screenshot", {}),
            ("screenshot", {"region": {"x": 0, "y": 0, "width": 100, "height": 80}}),
            ("screenshot.compressed", {"format": "jpeg", "quality": 50}),
            ("screenshot.compressed", {"region": {"x": 0, "y": 0, "width": 100, "height": 80}, "format": "png"}),
        ]:
            output = await tools.peek(command, args, **ctx)
            require(output.startswith("data:image/"), output[:200])
            require(len(base64.b64decode(output.split(",", 1)[1], validate=True)) > 100, "Empty screenshot")

        # A real terminal consumes keyboard events; verify its file separately,
        # not the input acknowledgement or a canary echoed in a tool argument.
        script = f"{WORKSPACE}/desktop-input.py"
        saved = f"{WORKSPACE}/desktop-input.txt"
        await tools.write(script, f"from pathlib import Path\nPath({saved!r}).write_text(input())\n", **ctx)
        child = shlex.quote(f"python3 {script}")
        output = await tools.bash(
            f"export DISPLAY=:0; if command -v xfce4-terminal >/dev/null; then "
            f"exec xfce4-terminal --disable-server --title=LatheInput --command={child}; "
            f"else exec xterm -title LatheInput -e python3 {script}; fi",
            foreground_seconds=0, **ctx)
        require("Error:" not in output, output)
        for _ in range(20):
            windows = await tools.peek("display.windows", **ctx)
            if "LatheInput" in windows:
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError(f"Test terminal did not open: {windows}")
        output = await tools.poke("keyboard.type", {"text": canary}, **ctx)
        require("Input delivered" in output, output)
        output = await tools.poke("keyboard.press", {"key": "enter"}, **ctx)
        require("Input delivered" in output, output)
        for _ in range(20):
            output = await tools.read(saved, **ctx)
            if canary in output:
                break
            await asyncio.sleep(0.5)
        require(canary in output, output)

        output = await tools.peek("accessibility.tree", {"maxDepth": 1}, **ctx)
        require(not output.startswith("Error:"), output)

        # Explicitly public only on this disposable test desktop, with no login.
        output = await tools.expose("vnc", "public", **ctx)
        match = re.search(r'https://\S+', output)
        require(match is not None, "VNC preview URL missing")
        async with httpx.AsyncClient() as client:
            response = await client.get(match.group(), headers={"X-Daytona-Skip-Preview-Warning": "true"}, timeout=20)
            require(response.status_code == 200 and "noVNC" in response.text, "VNC landing page unreachable")
            # A real WebSocket handshake qualifies the signed-preview transport;
            # cookie authentication for private wrappers needs browser testing.
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(match.group())
            socket_url = urlunsplit(parts._replace(path="/websockify"))
            async with client.stream("GET", socket_url, headers={
                "Origin": f"{parts.scheme}://{parts.netloc}",
                "Connection": "Upgrade", "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
                "Sec-WebSocket-Protocol": "binary", "X-Daytona-Skip-Preview-Warning": "true",
            }, timeout=20) as response:
                require(response.status_code == 101, "VNC WebSocket upgrade failed")
        output = await tools.poke("stop", **ctx)
        require(not output.startswith("Error:"), output)

    async def browser_roundtrip():
        import re
        from urllib.parse import urlsplit

        # After the sandbox restart above every display process is down (Daytona
        # still reports "partial"). Input after an in-plugin stop/start cycle
        # crashes Daytona's computer-use service, so this follows a sandbox wake.
        # peek must not start the desktop; poke must.
        output = await tools.peek("status", **ctx)
        require("Desktop is down:" in output, output)
        output = await tools.peek("mouse.position", **ctx)
        require(output.startswith("Error:") and "desktop is down" in output, output)
        output = await tools.poke("page.open", {"url": "https://example.org/"}, **ctx)
        require("started" in output and "Example Domain" in output, output)
        output = await tools.peek("status", **ctx)
        require("Desktop is active." in output, output)
        output = await tools.peek("page.text", **ctx)
        require("untrusted page text" in output and "documentation" in output, output)
        def hosts(text):
            return {urlsplit(u).hostname for u in re.findall(r"https?://\S+", text)}

        output = await tools.poke("page.click", {"text": "Learn more"}, **ctx)
        require(hosts(output) & {"iana.org", "www.iana.org"}, output)
        output = await tools.peek("page.list", **ctx)
        require(hosts(output) & {"iana.org", "www.iana.org"}, output)

        # Local pages: the form proves page.type reaches the page as trusted
        # input; localhost vs 127.0.0.1 makes the fetch cross-origin (no CORS).
        site = f"{WORKSPACE}/browser-site"
        await tools.write(f"{site}/form.html", (
            "<title>Lathe form</title><form><input id=q value=old><input id=ro value=fixed readonly>"
            "<button>Go</button></form>"
            "<p id=out></p><script>document.forms[0].onsubmit = e => { e.preventDefault(); "
            "document.getElementById('out').textContent = 'submitted:' + "
            "document.getElementById('q').value.length + ':' + e.isTrusted; };</script>"), **ctx)
        output = await tools.bash(f"cd {site} && nohup python3 -m http.server 8767 >/tmp/lathe-browser-http.log 2>&1 &", **ctx)
        require("Exit code:" not in output, output)
        output = await tools.poke("page.open", {"url": "http://127.0.0.1:8767/form.html", "newTab": True}, **ctx)
        require("Lathe form" in output, output)
        tab = re.search(r"Tab ([0-9A-F]{8})", output)[1]
        # A read-only field takes focus but no text: rejected before any input.
        output = await tools.poke("page.type", {"selector": "#ro", "text": "x", "submit": True, "tab": tab}, **ctx)
        require(output.startswith("Error:") and "read-only" in output, output)
        output = await tools.poke("page.eval", {"js": "document.getElementById('out').textContent === ''", "tab": tab}, **ctx)
        require("true" in output, output)
        output = await tools.poke("page.type", {"selector": "#q", "text": "typed value", "submit": True, "tab": tab}, **ctx)
        require("pressed Enter" in output and "typed value" not in output, output)
        output = await tools.peek("page.text", {"selector": "#out", "tab": tab}, **ctx)
        require("submitted:11:true" in output, output)
        output = await tools.poke("page.eval", {"js": "document.querySelectorAll('input').length + 40"}, **ctx)
        require("42" in output, output)

        # Ambiguity must not change app state. An exact title inside a clickable
        # ancestor must beat the unrelated "Hi" substring in Archived/History.
        await tools.write(f"{site}/clicks.html", (
            "<title>Click safety</title><button id=archive>Archived</button>"
            "<button id=history>History</button>"
            "<button id=session style='display:none'><span>Hi</span><span> online</span></button>"
            "<p id=out>untouched</p><script>document.addEventListener('click', e => {"
            "document.getElementById('out').textContent = (e.target.closest('button')?.id || 'other') + ':' + e.isTrusted;"
            "});</script>"), **ctx)
        output = await tools.poke("page.open", {"url": "http://127.0.0.1:8767/clicks.html", "newTab": True}, **ctx)
        require("Click safety" in output, output)
        safety_tab = re.search(r"Tab ([0-9A-F]{8})", output)[1]
        output = await tools.poke("page.click", {"text": "Hi", "tab": safety_tab}, **ctx)
        require(output.startswith("Error:") and "ambiguous click" in output and "no input sent" in output, output)
        output = await tools.peek("page.text", {"selector": "#out", "tab": safety_tab}, **ctx)
        require("untouched" in output, output)
        output = await tools.poke("page.eval", {"js": "document.getElementById('session').style.display = 'inline-block'",
                                              "tab": safety_tab}, **ctx)
        require(not output.startswith("Error:"), output)
        output = await tools.poke("page.click", {"text": "Hi", "tab": safety_tab}, **ctx)
        require("Clicked" in output, output)
        output = await tools.peek("page.text", {"selector": "#out", "tab": safety_tab}, **ctx)
        require("session:true" in output, output)
        output = await tools.poke("page.eval", {"js": (
            "document.body.insertAdjacentHTML('beforeend', "
            "'<div id=overlay style=\"position:fixed;inset:0;z-index:9999\">overlay</div>')"), "tab": safety_tab}, **ctx)
        require(not output.startswith("Error:"), output)
        output = await tools.poke("page.click", {"selector": "#session", "tab": safety_tab}, **ctx)
        require(output.startswith("Error:") and "no input sent" in output, output)
        output = await tools.peek("page.text", {"selector": "#out", "tab": safety_tab}, **ctx)
        require("session:true" in output, output)
        output = await tools.poke("page.eval", {"js": (
            "document.getElementById('overlay').remove(); document.body.insertAdjacentHTML('beforeend', "
            "'<div role=button style=\"position:relative;width:200px;height:100px\">"
            "<span>Nested title</span><button id=delete style=\"position:absolute;left:50%;top:50%;"
            "transform:translate(-50%,-50%)\">Delete</button></div>')"), "tab": safety_tab}, **ctx)
        require(not output.startswith("Error:"), output)
        output = await tools.poke("page.click", {"text": "Nested title", "tab": safety_tab}, **ctx)
        require(output.startswith("Error:") and "no input sent" in output, output)
        output = await tools.peek("page.text", {"selector": "#out", "tab": safety_tab}, **ctx)
        require("session:true" in output, output)

        rendered = f"{site}/doc.pdf"
        # No url or format: an HTML page is rendered by the .pdf extension.
        output = await tools.poke("page.save", {"path": rendered, "tab": tab}, **ctx)
        require("Saved" in output and "application/pdf" in output, output)
        fetched = f"{WORKSPACE}/fetched.pdf"
        output = await tools.poke("page.save", {"path": fetched, "url": "http://localhost:8767/doc.pdf"}, **ctx)
        require("Saved" in output, output)
        output = await tools.bash(f"head -c 5 {fetched}; echo; cmp {rendered} {fetched} && echo identical", **ctx)
        require("%PDF-" in output and "identical" in output, output)
        # Same origin: the streamed in-page fetch, not the cross-origin fallback.
        same = f"{WORKSPACE}/same-origin.pdf"
        output = await tools.poke("page.save", {"path": same, "url": "http://127.0.0.1:8767/doc.pdf"}, **ctx)
        require("Saved" in output, output)
        output = await tools.bash(f"cmp {rendered} {same} && echo identical", **ctx)
        require("identical" in output, output)
        # A tab showing a PDF saves the served bytes, not a print of the viewer.
        output = await tools.poke("page.open", {"url": "http://127.0.0.1:8767/doc.pdf", "newTab": True}, **ctx)
        require("Error:" not in output, output)
        viewed = f"{WORKSPACE}/viewed.pdf"
        output = await tools.poke("page.save", {"path": viewed}, **ctx)
        require("original bytes" in output, output)
        output = await tools.bash(f"cmp {rendered} {viewed} && echo identical", **ctx)
        require("identical" in output, output)

        # page.wait polls for delayed content and times out as a normal result.
        # The cross-origin frame's reported SCREEN bounds must hit it via
        # desktop input; it reports trusted clicks to the parent by message.
        await tools.write(f"{site}/frame.html", (
            "<body style='margin:0;background:red'><script>document.body.onclick = e => "
            "parent.postMessage('frame-clicked:' + e.isTrusted, '*');</script></body>"), **ctx)
        await tools.write(f"{site}/host.html", (
            "<title>Frame host</title><body style='margin:0'><p id=out>waiting</p>"
            "<iframe src='http://localhost:8767/frame.html' style='position:absolute;left:100px;"
            "top:150px;width:300px;height:65px;border:0'></iframe><script>"
            "addEventListener('message', e => document.getElementById('out').textContent = e.data);"
            "setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<p>delayed-ready</p>'), 3000);"
            "</script></body>"), **ctx)
        output = await tools.poke("page.open", {"url": "http://127.0.0.1:8767/host.html"}, **ctx)
        require("Frame host" in output, output)
        output = await tools.peek("page.wait", {"text": "delayed-ready", "timeout": 15}, **ctx)
        require("Condition met" in output, output)
        output = await tools.peek("page.wait", {"selector": "#never", "timeout": 2}, **ctx)
        require("Timed out after 2 s" in output and not output.startswith("Error:"), output)
        output = await tools.peek("page.text", **ctx)
        center = re.search(r"http://localhost:8767 .* center=\((\d+),(\d+)\)", output)
        require(center is not None, output)
        output = await tools.poke("mouse.click", {"x": int(center[1]), "y": int(center[2])}, **ctx)
        require("Input delivered" in output, output)
        output = await tools.peek("page.wait", {"text": "frame-clicked:true", "timeout": 5}, **ctx)
        require("Condition met" in output, output)
        await tools.bash("pkill -f '[h]ttp.server 8767'", **ctx)

    async def workspace_survives_stop():
        # No extra volume: restart the same VM and independently read its file.
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _api(tools.valves, "/sandbox"),
                params={"labels": json.dumps({DEPLOYMENT_LABEL: TEST_EMAIL})},
                headers=_headers(tools.valves), timeout=30,
            )
            response.raise_for_status()
            sandbox, = [s for s in _extract_sandbox_list(response.json())
                        if s.get("labels", {}).get(DEPLOYMENT_LABEL) == TEST_EMAIL]
            require(not sandbox.get("volumes"), "Test sandbox unexpectedly has an extra volume")
            sid = sandbox["id"]
            response = await client.post(_api(tools.valves, f"/sandbox/{sid}/stop"),
                                         headers=_headers(tools.valves), timeout=30)
            response.raise_for_status()
            for _ in range(60):
                response = await client.get(_api(tools.valves, f"/sandbox/{sid}"),
                                            headers=_headers(tools.valves), timeout=30)
                response.raise_for_status()
                if response.json().get("state") == "stopped":
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("Test sandbox did not stop")
        output = await tools.read(test_file, **ctx)
        require(canary in output and "OMEGA" in output, output)
        # Files and durable session history survive, but Python memory does not.
        reloaded = Tools()
        reloaded.valves = tools.Valves(**tools.valves.model_dump())
        output = await reloaded.interpret("print('contract_value' in globals())", **ctx)
        require("session was reset" in output and output.rstrip().endswith("False"), output)

    async def disabled_auto_create_is_respected():
        output = await tools.destroy(
            __user__=user,
            __event_emitter__=emitter,
            __event_call__=confirmed,
        )
        require("Destroyed 1 sandbox" in output, output)
        tools.valves.auto_create_sandbox = False
        tools.valves.sandbox_missing_message = "CONTRACT_PROVISION_EXTERNALLY"
        try:
            output = await tools.bash("true", **ctx)
            require("CONTRACT_PROVISION_EXTERNALLY" in output, output)
        finally:
            tools.valves.auto_create_sandbox = True
            tools.valves.sandbox_missing_message = ""

    try:
        # These phases share one VM. Stop after a failed prerequisite rather
        # than reporting a cascade of failures against a broken fixture.
        for name, scenario in [
            ("core tool roundtrip", core_tool_roundtrip),
            ("view tool roundtrip", view_tool_roundtrip),
            ("onboarding and persistent interpreter", onboarding_and_interpreter),
            ("independent toolkit workers share interpreter identity", multiworker_interpreter),
            ("bounded command retention", bounded_command_retention),
            ("background completion notice", background_completion_notice),
            ("background notice claimed once across workers", background_notice_claimed_once_across_workers),
            ("signed preview URL", expose_contract),
            ("desktop input, screenshots and VNC transport", computer_roundtrip),
            ("ordinary workspace survives sandbox stop", workspace_survives_stop),
            ("browser page commands with desktop auto-start", browser_roundtrip),
            ("disabled auto-create policy", disabled_auto_create_is_respected),
        ]:
            if not await results.run(name, scenario):
                break
    finally:
        # Cleanup deletes only isolated test sandboxes, never existing volumes.
        try:
            await _delete_test_sandboxes(tools)
        except Exception as exc:
            cleanup_failed = True
            print(f"Cleanup failed: {exc}")

    elapsed = time.monotonic() - started
    print(f"\n{results.scenarios - results.failed}/{results.scenarios} scenarios passed in {elapsed:.1f}s")
    return 1 if results.failed or cleanup_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

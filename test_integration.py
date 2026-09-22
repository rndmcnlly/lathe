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
from httpx_ws import aconnect_ws
from dotenv import load_dotenv

from lathe import (
    Tools, _api, _extract_sandbox_list, _headers, _site_port,
    _CS_BIN, _CS_ENSURE_SCRIPT, _DUFS_BIN, _DUFS_ENSURE_SCRIPT,
    _TTYD_ENSURE_SCRIPT, _TTYD_PORT,
)


load_dotenv()

API_KEY = os.environ.get("DAYTONA_API_KEY", "")
DEPLOYMENT_LABEL = "lathe-integration-test"
TEST_EMAIL = "runner@lathe-integration.test"
VOLUME_NAME = f"{DEPLOYMENT_LABEL}/{TEST_EMAIL}"
WORKSPACE = "/home/daytona/workspace"
VOLUME = "/home/daytona/volume"

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
            response = await client.delete(
                f"{tools.valves.daytona_api_url}/sandbox/{sandbox['id']}",
                params={"force": "true"},
                headers=_headers(tools.valves),
                timeout=30,
            )
            if response.status_code != 404:
                response.raise_for_status()


async def main():
    if not API_KEY:
        print("Error: DAYTONA_API_KEY is not set.")
        return 2

    tools = Tools()
    tools.valves.daytona_api_key = API_KEY
    tools.valves.deployment_label = DEPLOYMENT_LABEL
    tools.valves.persistent_volume = True
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
    volume_file = f"{VOLUME}/contract-volume-canary.txt"
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
            tools.glob(f"{WORKSPACE}/contract-*.txt", **ctx),
            tools.grep(canary, files=f"{WORKSPACE}/contract-*.txt", **ctx),
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

    async def background_completion_notice():
        output = await tools.bash(
            "sleep 2; printf 'background-finished'",
            foreground_seconds=0,
            **ctx,
        )
        require("Backgrounded" in output, output)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = tools._chat_state.get(chat_id, {})
            if any("Background job completed" in msg for msg in state.get("pending", [])):
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("background completion notice was not queued")

        output = await tools.read(test_file, **ctx)
        require("Background job completed" in output, output)
        require("background-finished" in output, output)

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

    async def ttyd_terminal_roundtrip():
        output = await tools.bash(_TTYD_ENSURE_SCRIPT, foreground_seconds=90, **ctx)
        require("READY PID=" in output, output)

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
            response = await client.get(
                _api(tools.valves, f"/sandbox/{sandbox['id']}/ports/{_TTYD_PORT}/signed-preview-url"),
                params={"expiresInSeconds": 300}, headers=_headers(tools.valves), timeout=30,
            )
            response.raise_for_status()
            preview = response.json()["url"]

            page = await client.get(
                preview, headers={"X-Daytona-Skip-Preview-Warning": "true"}, timeout=20,
            )
            require(page.status_code == 200 and "ttyd" in page.text.lower(),
                    "ttyd page did not load through the signed preview")

            parsed = httpx.URL(preview)
            ws_url = parsed.copy_with(
                scheme="wss", path="/ws",
            )
            canary_command = f"printf '{canary}\\n'\n"
            async with aconnect_ws(
                str(ws_url), client, subprotocols=["tty"],
                headers={"X-Daytona-Skip-Preview-Warning": "true"},
            ) as ws:
                await ws.send_text('{"columns":80,"rows":24}')
                await ws.send_bytes(b"0" + canary_command.encode())
                deadline = time.monotonic() + 15
                received = b""
                while time.monotonic() < deadline and canary.encode() not in received:
                    received += await asyncio.wait_for(ws.receive_bytes(), timeout=5)
                require(canary.encode() in received, received[-500:])

    async def verified_service_bootstrap():
        for name, script, binary, timeout in [
            ("dufs", _DUFS_ENSURE_SCRIPT, _DUFS_BIN, 90),
            ("code-server", _CS_ENSURE_SCRIPT, _CS_BIN, 300),
        ]:
            output = await tools.bash(script, foreground_seconds=timeout, **ctx)
            require("READY PID=" in output, output)
            output = await tools.bash(f"test -x {binary} && {binary} --version", **ctx)
            require("Exit code:" not in output, f"{name} bootstrap did not install a working executable: {output}")

    async def parallel_static_sites():
        import re

        roots = [f"{WORKSPACE}/site-a", f"{WORKSPACE}/site-b"]
        markers = [f"SITE_A_{canary}", f"SITE_B_{canary}"]
        for root, marker in zip(roots, markers):
            await tools.write(f"{root}/index.html", marker, **ctx)

        outputs = await asyncio.gather(*(
            tools.expose(f"site:{root}", "public", **ctx) for root in roots
        ))
        urls = []
        for output, root in zip(outputs, roots):
            require(f"port {_site_port(root)}" in output, output)
            match = re.search(r"https://\S+", output)
            require(match is not None, output)
            urls.append(match.group())

        async with httpx.AsyncClient() as client:
            pages = await asyncio.gather(*(
                client.get(url, headers={"X-Daytona-Skip-Preview-Warning": "true"}, timeout=20)
                for url in urls
            ))
        for page, marker in zip(pages, markers):
            require(page.status_code == 200 and marker in page.text, page.text[:200])

        repeated = await tools.expose(f"site:{roots[0]}", "public", **ctx)
        require(f"port {_site_port(roots[0])}" in repeated, repeated)

    async def volume_survives_recreation():
        await tools.write(volume_file, canary + "\n", **ctx)
        output = await tools.destroy(
            __user__=user,
            __event_emitter__=emitter,
            __event_call__=confirmed,
        )
        require("Destroyed 1 sandbox" in output, output)

        # The next call creates a fresh VM and remounts the same named volume.
        output = await tools.read(volume_file, **ctx)
        require(canary in output, output)
        await tools.bash(f"rm -f {volume_file}", **ctx)

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
            ("background completion notice", background_completion_notice),
            ("signed preview URL", expose_contract),
            ("parallel static sites", parallel_static_sites),
            ("verified dufs and code-server cold-start bootstrap", verified_service_bootstrap),
            ("ttyd page and websocket command", ttyd_terminal_roundtrip),
            ("persistent volume survives VM recreation", volume_survives_recreation),
            ("disabled auto-create policy", disabled_auto_create_is_respected),
        ]:
            if not await results.run(name, scenario):
                break
    finally:
        # Cleanup runs even when an HTTP exception aborts a scenario. The named
        # volume is intentionally retained and reused, so runs do not leak one
        # Daytona volume per invocation.
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

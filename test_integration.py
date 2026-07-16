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
import uuid

import httpx
from dotenv import load_dotenv

from lathe import Tools, _extract_sandbox_list, _headers


load_dotenv()

API_KEY = os.environ.get("DAYTONA_API_KEY", "")
DEPLOYMENT_LABEL = "lathe-integration-test"
TEST_EMAIL = "runner@lathe-integration.test"
VOLUME_NAME = f"{DEPLOYMENT_LABEL}/{TEST_EMAIL}"
WORKSPACE = "/home/daytona/workspace"
VOLUME = "/home/daytona/volume"


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
            await client.delete(
                f"{tools.valves.daytona_api_url}/sandbox/{sandbox['id']}",
                params={"force": "true"},
                headers=_headers(tools.valves),
                timeout=30,
            )


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

    async def strict_wrapper_types():
        output = await tools.read(test_file, start="2", **ctx)
        require("expected type int" in output, output)
        output = await tools.edit(test_file, "alpha", "ALPHA", replace_all="false", **ctx)
        require("expected type bool" in output, output)

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
        await tools.bash(
            "nohup python3 -m http.server 8765 >/tmp/lathe-http.log 2>&1 &",
            **ctx,
        )
        output = await tools.expose("http:8765", **ctx)
        require("Public URL" in output and "https://" in output, output)

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
        await results.run("core tool roundtrip", core_tool_roundtrip)
        await results.run("onboarding and persistent interpreter", onboarding_and_interpreter)
        await results.run("strict wrapper types", strict_wrapper_types)
        await results.run("background completion notice", background_completion_notice)
        await results.run("signed preview URL", expose_contract)
        await results.run("persistent volume survives VM recreation", volume_survives_recreation)
        await results.run("disabled auto-create policy", disabled_auto_create_is_respected)
    finally:
        # Cleanup runs even when an HTTP exception aborts a scenario. The named
        # volume is intentionally retained and reused, so runs do not leak one
        # Daytona volume per invocation.
        try:
            await _delete_test_sandboxes(tools)
        except Exception as exc:
            print(f"Cleanup warning: {exc}")

    elapsed = time.monotonic() - started
    print(f"\n{results.scenarios - results.failed}/{results.scenarios} scenarios passed in {elapsed:.1f}s")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

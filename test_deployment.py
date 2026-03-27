#!/usr/bin/env python3
"""
Deployment tests for lathe.py — exercises the full tool execution pipeline
against a live Open WebUI instance via Socket.IO.

Connects to the OWUI Socket.IO endpoint, sends chat completions with
tool_ids=["lathe"], and verifies that tool calls are executed server-side
and produce expected results.  Each test gets a fresh chat_id that is
deleted on completion — no persistent state is left in the OWUI database.

Usage:
    uv run --script test_deployment.py                  # run all tests
    uv run --script test_deployment.py --list           # list available tests
    uv run --script test_deployment.py bash_execution   # specific test
    uv run --script test_deployment.py --verbose        # show all socket.io events

Requires CHAT_ADAMSMITH_AS_OWUI_TOKEN in .env (or environment).
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "python-socketio[asyncio_client]", "aiohttp", "python-dotenv"]
# ///

import asyncio
import json
import os
import sys
import time
import uuid

from dotenv import load_dotenv
import httpx
import socketio

load_dotenv()

OWUI_BASE = "https://chat.adamsmith.as"
OWUI_TOKEN = os.environ.get("CHAT_ADAMSMITH_AS_OWUI_TOKEN", "")
# Cheap, fast model for deployment tests
MODEL = "anthropic_via_openrouter.anthropic/claude-haiku-4.5"
VERBOSE = False


# ── Test result tracking ─────────────────────────────────────────────

class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self._failure_diagnostics: list[dict] = []

    def check(self, name, condition, detail=""):
        if condition:
            print(f"  PASS: {name}")
            self.passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            self.failed += 1

    def dump_on_failure(self, label: str, events: list[dict]):
        """Store events to dump if any checks in this test fail."""
        self._failure_diagnostics.append({"label": label, "events": events})

    def flush_diagnostics(self, had_failures_before: int):
        """If new failures occurred, dump stored diagnostics."""
        if self.failed > had_failures_before:
            for diag in self._failure_diagnostics:
                print(f"\n  ── diagnostic dump: {diag['label']} ({len(diag['events'])} events) ──")
                for ev in diag["events"]:
                    print(f"    {ev['event']}: {str(ev.get('data', ''))[:200]}")
        self._failure_diagnostics.clear()


# ── OWUI Socket.IO client ───────────────────────────────────────────

class OWUIClient:
    """Headless Open WebUI client that drives conversations with tool execution.

    Connects via Socket.IO to get a session_id, then POSTs to
    /api/chat/completions with the session_id to trigger the server-side
    tool execution loop.  Collects results via Socket.IO events.

    OWUI wraps all events in a top-level "events" socket.io event with
    structure: {chat_id, message_id, data: {type, data: {...}}}.  The
    tool execution pipeline emits:
      - chat:active (active=true)    — generation started
      - chat:completion (streaming)  — tool calls building, results, text
      - status                       — tool status ("Running command...", etc.)
      - chat:completion (done=true)  — final output with complete output array
      - chat:active (active=false)   — generation finished
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.sio = socketio.AsyncClient()
        self.session_id: str | None = None
        self._events: list[dict] = []
        self._done = asyncio.Event()

        @self.sio.event
        async def connect():
            self.session_id = self.sio.sid

        @self.sio.on("*")
        async def catch_all(event, *args):
            data = args[0] if args else None
            self._events.append({"event": event, "data": data})
            if VERBOSE:
                preview = json.dumps(data, default=str)[:200] if data else "(none)"
                print(f"    [ws] {event}: {preview}")
            # Detect end of generation: events → data.type=="chat:completion" → data.data.done==True
            if event == "events" and isinstance(data, dict):
                inner = data.get("data", {})
                if isinstance(inner, dict) and inner.get("type") == "chat:completion":
                    inner_data = inner.get("data", {})
                    if isinstance(inner_data, dict) and inner_data.get("done"):
                        self._done.set()

    async def connect(self):
        """Connect to OWUI Socket.IO and authenticate."""
        # OWUI mounts socket.io at /ws/socket.io, not the default /socket.io/
        await self.sio.connect(
            self.base_url,
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            auth={"token": self.token},
            wait_timeout=15,
        )
        # Register in the OWUI session pool (mirrors frontend behavior)
        await self.sio.emit("user-join", {"auth": {"token": self.token}})
        await asyncio.sleep(0.5)  # let registration propagate

    async def disconnect(self):
        if self.sio.connected:
            try:
                await asyncio.wait_for(self.sio.disconnect(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

    async def send_message(
        self,
        messages: list[dict],
        tool_ids: list[str] | None = None,
        model: str = MODEL,
        timeout_seconds: int = 90,
    ) -> dict:
        """Send a chat completion request and wait for the full response.

        Returns a dict with:
          content  — the assistant's final HTML content string
          output   — the structured output array (function_call, function_call_output, message)
          events   — all raw Socket.IO events received during this turn
          chat_id  — the chat ID (for cleanup via delete_chat)
        """
        if not self.session_id:
            raise RuntimeError("Not connected — call connect() first")

        chat_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        self._events.clear()
        self._done.clear()

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "chat_id": chat_id,
            "id": message_id,
            "session_id": self.session_id,
        }
        if tool_ids:
            payload["tool_ids"] = tool_ids

        # POST triggers async processing; server returns {status, task_id}
        # immediately, then streams results over Socket.IO
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            if VERBOSE:
                print(f"    [http] {resp.status_code}: {resp.text[:200]}")

        # Wait for the done event
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            if VERBOSE:
                print(f"    [timeout] {len(self._events)} events received before timeout")

        # Extract final results from the done event
        content = ""
        output = []
        for ev in self._events:
            if ev["event"] != "events":
                continue
            inner = (ev.get("data") or {}).get("data", {})
            if not isinstance(inner, dict) or inner.get("type") != "chat:completion":
                continue
            inner_data = inner.get("data", {})
            if not isinstance(inner_data, dict):
                continue
            if "content" in inner_data:
                content = inner_data["content"]
            if "output" in inner_data:
                output = inner_data["output"]

        return {
            "content": content,
            "output": output,
            "events": list(self._events),
            "chat_id": chat_id,
        }

    async def delete_chat(self, chat_id: str):
        """Delete a chat to clean up after tests."""
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"{self.base_url}/api/v1/chats/{chat_id}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=10.0,
                )
        except Exception:
            pass  # best-effort cleanup


# ── Helpers for inspecting output arrays ─────────────────────────────

def get_tool_calls(output: list[dict]) -> list[dict]:
    """Extract function_call items from an output array."""
    return [o for o in output if o.get("type") == "function_call"]


def get_tool_results(output: list[dict]) -> list[dict]:
    """Extract function_call_output items from an output array."""
    return [o for o in output if o.get("type") == "function_call_output"]


def get_tool_result_text(result: dict) -> str:
    """Extract the text content from a function_call_output item.

    OWUI stores tool output as either a plain string or an array of
    {type: "input_text", text: "..."} objects.
    """
    out = result.get("output", "")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        return "".join(
            item.get("text", "") for item in out
            if isinstance(item, dict) and item.get("type") == "input_text"
        )
    return str(out)


def get_messages(output: list[dict]) -> list[dict]:
    """Extract message items from an output array."""
    return [o for o in output if o.get("type") == "message"]


# ── Tests ────────────────────────────────────────────────────────────

async def test_tool_schema(R: Results):
    """Verify Lathe is installed and has expected tools in its schema."""
    print("\n── deployment: tool installation ──")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{OWUI_BASE}/api/v1/tools/",
            headers={"Authorization": f"Bearer {OWUI_TOKEN}"},
        )
        resp.raise_for_status()
        tools = resp.json()
        lathe_tools = [t for t in tools if t["id"] == "lathe"]
        R.check("Lathe is installed", len(lathe_tools) == 1, f"found {len(lathe_tools)}")

        if lathe_tools:
            lathe = lathe_tools[0]
            R.check("Lathe has name", lathe.get("name") == "Lathe", lathe.get("name"))
            specs = lathe.get("specs", [])
            spec_names = {s.get("name") for s in specs}
            for expected in ("bash", "read", "write", "edit", "expose", "destroy", "onboard", "lathe"):
                R.check(f"spec includes {expected}", expected in spec_names, str(spec_names))


async def test_bash_execution(R: Results, owui: OWUIClient):
    """Full tool execution: model calls bash, OWUI executes it, canary appears in output."""
    print("\n── deployment: bash tool execution ──")
    before = R.failed

    canary = f"CANARY_{uuid.uuid4().hex[:8]}"
    result = await owui.send_message(
        messages=[{"role": "user", "content": f"Use the bash tool to run exactly this command: echo {canary}"}],
        tool_ids=["lathe"],
    )
    R.dump_on_failure("bash_execution", result["events"])

    output = result["output"]
    R.check("got output items", len(output) > 0, f"output has {len(output)} items")

    calls = get_tool_calls(output)
    R.check("model made a tool call", len(calls) > 0, f"got {len(calls)}")
    if calls:
        R.check("tool call is bash", calls[0].get("name") == "bash", calls[0].get("name"))

    results = get_tool_results(output)
    R.check("got tool result", len(results) > 0, f"got {len(results)}")
    if results:
        text = get_tool_result_text(results[0])
        R.check("canary in tool output", canary in text, text[:200])

    msgs = get_messages(output)
    R.check("assistant responded after tool call", len(msgs) > 0, f"got {len(msgs)} messages")

    R.flush_diagnostics(before)
    await owui.delete_chat(result["chat_id"])


async def test_lathe_manual(R: Results, owui: OWUIClient):
    """The lathe(manpage=...) tool returns version info."""
    print("\n── deployment: lathe manual ──")
    before = R.failed

    result = await owui.send_message(
        messages=[{"role": "user", "content": 'Call lathe(manpage="version") and tell me the version.'}],
        tool_ids=["lathe"],
    )
    R.dump_on_failure("lathe_manual", result["events"])

    calls = get_tool_calls(result["output"])
    R.check("model called lathe tool", len(calls) > 0, f"got {len(calls)}")
    if calls:
        R.check("called lathe function", calls[0].get("name") == "lathe", calls[0].get("name"))

    results = get_tool_results(result["output"])
    R.check("got lathe result", len(results) > 0, f"got {len(results)}")
    if results:
        text = get_tool_result_text(results[0])
        R.check("version in output", "version" in text.lower(), text[:200])

    R.flush_diagnostics(before)
    await owui.delete_chat(result["chat_id"])


async def test_write_read_roundtrip(R: Results, owui: OWUIClient):
    """Write a file via the write tool, then read it back via bash."""
    print("\n── deployment: write + read roundtrip ──")
    before = R.failed

    marker = f"MARKER_{uuid.uuid4().hex[:8]}"
    file_path = f"/tmp/deployment_test_{uuid.uuid4().hex[:8]}.txt"

    # Turn 1: write the file
    result1 = await owui.send_message(
        messages=[{"role": "user", "content": f"Use the write tool to write exactly this text to {file_path}: {marker}"}],
        tool_ids=["lathe"],
    )
    R.dump_on_failure("write_read_roundtrip (write)", result1["events"])

    write_results = get_tool_results(result1["output"])
    R.check("write produced output", len(write_results) > 0, f"got {len(write_results)}")
    if write_results:
        text = get_tool_result_text(write_results[0])
        R.check("write succeeded", "Wrote" in text, text[:200])

    # Turn 2: read it back via bash (avoids multi-turn message threading
    # complexity — just a fresh single-turn with an explicit path)
    result2 = await owui.send_message(
        messages=[{"role": "user", "content": f"Use the bash tool to run: cat {file_path}"}],
        tool_ids=["lathe"],
    )
    R.dump_on_failure("write_read_roundtrip (read)", result2["events"])

    read_results = get_tool_results(result2["output"])
    R.check("read produced output", len(read_results) > 0, f"got {len(read_results)}")
    if read_results:
        text = get_tool_result_text(read_results[0])
        R.check("marker in read output", marker in text, text[:200])

    R.flush_diagnostics(before)
    await owui.delete_chat(result1["chat_id"])
    await owui.delete_chat(result2["chat_id"])


# ── Test registry and runner ─────────────────────────────────────────

API_TESTS = {
    "tool_schema": test_tool_schema,
}

LIVE_TESTS = {
    "bash_execution": test_bash_execution,
    "lathe_manual": test_lathe_manual,
    "write_read": test_write_read_roundtrip,
}

ALL_TESTS = {**API_TESTS, **LIVE_TESTS}


async def main():
    global VERBOSE
    args = [a for a in sys.argv[1:] if a != "--verbose"]
    VERBOSE = "--verbose" in sys.argv[1:]

    if "--list" in args:
        print("API tests (httpx only):")
        for name in API_TESTS:
            print(f"  {name}")
        print("\nLive tests (Socket.IO, full tool execution):")
        for name in LIVE_TESTS:
            print(f"  {name}")
        return

    selected = [a for a in args if a != "--list"] or list(ALL_TESTS.keys())
    for name in selected:
        if name not in ALL_TESTS:
            print(f"Unknown test: {name}. Use --list to see available tests.")
            sys.exit(1)

    if not OWUI_TOKEN:
        print("Error: CHAT_ADAMSMITH_AS_OWUI_TOKEN not set. Add it to .env or export it.")
        sys.exit(1)

    R = Results()
    t0 = time.time()

    print(f"{'='*60}")
    print(f"DEPLOYMENT TESTS against {OWUI_BASE}")
    if VERBOSE:
        print("(verbose mode — all socket.io events will be printed)")
    print(f"{'='*60}")

    api_selected = [g for g in selected if g in API_TESTS]
    live_selected = [g for g in selected if g in LIVE_TESTS]

    for name in api_selected:
        await API_TESTS[name](R)

    if live_selected:
        print("\nConnecting to OWUI via Socket.IO...")
        owui = OWUIClient(OWUI_BASE, OWUI_TOKEN)
        try:
            await owui.connect()
            print(f"Connected (session_id: {owui.session_id})")
            for name in live_selected:
                await LIVE_TESTS[name](R, owui)
        finally:
            await owui.disconnect()

    elapsed = time.time() - t0
    total = R.passed + R.failed
    print(f"\n{'='*60}")
    print(f"Results: {R.passed} passed, {R.failed} failed out of {total}  ({elapsed:.1f}s)")
    if R.failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

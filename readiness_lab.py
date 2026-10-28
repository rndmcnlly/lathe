#!/usr/bin/env python3
"""Disposable readiness lab. Offline: --plan. Explicit live work: --run.

Run from a trusted checkout AFTER 1.3.1 lands, preferably a manual GitHub job
with repository DAYTONA_API_KEY / OPENROUTER_API_KEY secrets. Never loads .env.
No host mounts, published ports, production accounts, or persistent data volumes.
This first lab qualifies two OWUI processes, not arbitrary HA deployments.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

IMAGE = "ghcr.io/open-webui/open-webui:v0.12.4-slim"
MODEL = "z-ai/glm-5.3-flash"
MAX_REQUESTS = 40
MAX_PROMPT_BYTES = 64_000
PRICE = {"prompt": Decimal("0.00000015"), "completion": Decimal("0.0000005")}
PROVIDER = {"data_collection": "deny", "zdr": True,
            "max_price": {"prompt": 0.15, "completion": 0.5, "request": 0, "image": 0}}
BUDGET = Decimal("2.00")
PORTS = {"A": 8080, "B": 8081}
SNAPSHOT = "daytona-small"
UNIMPLEMENTED = {
    "overlapping_first_use": "NOT IMPLEMENTED: requires an observed admission barrier",
    "direct_bash_wakeup": "UNSUPPORTED: no generic supported native dispatch boundary",
}


class Reservation:
    """Never refund reservations, even on upstream failure; no automatic retry.

    Four tokens per wire byte plus 16K framing tokens conservatively covers
    UTF-8 text, function schemas and chat-template overhead. No media, search
    plugin or per-request charges are permitted. Upstream max_price is mandatory.
    Prices checked against the public model catalogue before any inference.
    """
    def __init__(self):
        self.reserved = Decimal(0)
        self.calls = 0

    def reserve(self, payload, wire_bytes):
        require(payload.get("model") == MODEL and payload.get("provider") == PROVIDER,
                "Model/ZDR/price-cap policy changed")
        require(payload.get("plugins", []) == [], "Paid plugins prohibited")
        require(type(payload.get("max_tokens")) is int and 0 < payload["max_tokens"] <= 4096,
                "Missing/bad output ceiling")
        require(0 < wire_bytes <= MAX_PROMPT_BYTES, "Provider input too large")
        require(all(isinstance(m.get("content", ""), (str, type(None))) for m in payload.get("messages", [])),
                "Only text messages are budgeted")
        require(not any(k in payload for k in ("web_search_options", "modalities", "audio", "images")),
                "Unbudgeted modalities or search")
        require(payload.get("n", 1) == 1, "Only one completion per request is budgeted")
        cost = Decimal(wire_bytes * 4 + 16384) * PRICE["prompt"] + Decimal(payload["max_tokens"]) * PRICE["completion"]
        require(self.calls < MAX_REQUESTS and self.reserved + cost <= BUDGET,
                "Hard provider reservation budget exhausted")
        self.reserved += cost
        self.calls += 1
        return cost


def root():
    for directory in Path(__file__).resolve().parents:
        if (directory / "monitor_e2e.py").is_file():
            return directory
    raise RuntimeError("Place draft under a Lathe checkout, or copy it to the root")


def setup_imports():
    # Set BEFORE importing test_deployment: explicitly disable dotenv loading.
    os.environ["LATHE_ISOLATED_CI"] = "1"
    sys.path.insert(0, str(root()))
    import monitor_e2e as monitor
    return monitor


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def owned_sandboxes(key, label):
    import httpx
    matches, cursor, seen = [], None, set()
    async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {key}"}) as http:
        while True:
            response = await http.get("https://app.daytona.io/api/sandbox",
                                      params={"cursor": cursor} if cursor else {})
            response.raise_for_status()
            payload = response.json()
            matches.extend(s for s in (payload if isinstance(payload, list) else payload.get("items", []))
                           if label in s.get("labels", {}))
            cursor = payload.get("cursor") if isinstance(payload, dict) else None
            if not cursor:
                return matches
            require(cursor not in seen, "Repeated pagination cursor")
            seen.add(cursor)


async def verify_prices():
    import httpx
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
        model = next(m for m in response.json()["data"] if m["id"] == MODEL)
    for kind, ceiling in PRICE.items():
        require(Decimal(model["pricing"][kind]) <= ceiling, "Current model pricing exceeds reservation ceiling")
    require(Decimal(model["pricing"].get("request", "0")) == 0, "Per-request billing not budgeted")
    return {"pricing": model["pricing"], "provider_price_filter": PROVIDER,
            "reservation_ceiling_usd": str(BUDGET), "checked_at": time.time(),
            "source": "https://openrouter.ai/api/v1/models"}


async def provision_small(http, base, admin_headers, key, label, email, trace):
    """Preflight the snapshot, install/read back complete valves, create one VM.

    OWUI valve updates are replacements, not JSON merge patches. Preserve the
    deployed credentials rather than accidentally replacing them with defaults.
    Snapshot resources are selected by Daytona, never by resource overrides.
    """
    cloud_headers = {"Authorization": f"Bearer {key}"}
    response = await http.get(f"https://app.daytona.io/api/snapshots/{SNAPSHOT}", headers=cloud_headers)
    response.raise_for_status()
    snapshot = response.json()
    require(snapshot.get("name") == SNAPSHOT and snapshot.get("state") == "active",
            "Named small snapshot is not active")
    require(all(type(snapshot.get(k)) in (int, float) and 0 < snapshot[k] <= maximum
                for k, maximum in (("cpu", 1), ("mem", 1), ("disk", 3)))
            and snapshot.get("gpu") == 0, "Snapshot exceeds declared small CPU budget")
    trace("snapshot_preflight", name=SNAPSHOT, id=snapshot.get("id"),
          resources={k: snapshot[k] for k in ("cpu", "mem", "disk", "gpu")})
    endpoint = base + "/api/v1/tools/id/lathe_test/valves"
    response = await http.get(endpoint, headers=admin_headers)
    response.raise_for_status()
    valves = response.json()
    require(valves.get("daytona_api_key") == key and valves.get("deployment_label") == label,
            "Staging credentials/ownership missing before valve update")
    overrides = {"snapshot": SNAPSHOT, "ttlMinutes": 30}
    expected = {"deployment_label": label, "auto_create_sandbox": False,
                "auto_stop_minutes": 5, "auto_archive_minutes": 5, "auto_delete_minutes": 0}
    valves.update(expected, sandbox_create_overrides=json.dumps(overrides))
    response = await http.post(endpoint + "/update", headers=admin_headers, json=valves)
    response.raise_for_status()
    response = await http.get(endpoint, headers=admin_headers)
    response.raise_for_status()
    installed = response.json()
    require(all(installed.get(k) == v for k, v in expected.items())
            and installed.get("daytona_api_key") == key
            and json.loads(installed.get("sandbox_create_overrides", "null")) == overrides,
            "Installed staging valves do not match snapshot/lifetime/ownership policy")
    trace("installed_valves_verified", **expected, sandbox_create_overrides=overrides)
    response = await http.post("https://app.daytona.io/api/sandbox", headers=cloud_headers, json={
        **overrides, "name": label, "labels": {label: email.strip().lower()},
        "autoStopInterval": 5, "autoArchiveInterval": 5, "autoDeleteInterval": 0})
    response.raise_for_status()
    info = response.json()
    require(all(type(info.get(k)) in (int, float) and 0 < info[k] <= limit
                for k, limit in (("cpu", 1), ("memory", 1), ("disk", 3))),
            "Created sandbox exceeds preflighted small CPU resources")
    require(info.get("labels", {}).get(label) == email.strip().lower()
            and not info.get("volumes"), "Created sandbox ownership/volume mismatch")
    trace("provisioned", id=info["id"], resources={k: info[k] for k in ("cpu", "memory", "disk")})
    return info


def run_command(*args, timeout=120, capture=True):
    result = subprocess.run(args, check=True, timeout=timeout,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    return result.stdout.decode().strip() if capture else ""


def worker(name):
    """Lab-only HTTP observation; no replacement of toolkit/runtime functions."""
    # The image normally launches `python -m uvicorn` from /app/backend.
    # A /tmp script gets /tmp, not that working directory, on sys.path.
    sys.path.insert(0, "/app/backend")
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from open_webui.main import app

    identity = {"worker": name, "pid": os.getpid(), "incarnation": uuid.uuid4().hex}

    def note(kind, **data):
        with open(f"/lab/artifacts/worker-{name}.jsonl", "a") as out:
            out.write(json.dumps({"kind": kind, "time": time.time(), **identity, **data}) + "\n")

    # Observe OWUI's own invocation, never dispatch internal messages ourselves.
    original = app.state.CHAT_COMPLETION_HANDLER

    async def observe_handler(request, form_data, *args, **kwargs):
        note("native_handler_entry", chat_id=form_data.get("chat_id"),
             message_id=form_data.get("id"), internal=getattr(request.state, "internal", False),
             model=form_data.get("model"), tool_ids=form_data.get("tool_ids"),
             system_hash=hashlib.sha256(json.dumps([m for m in form_data.get("messages", [])
                 if m.get("role") == "system"], sort_keys=True).encode()).hexdigest())
        return await original(request, form_data, *args, **kwargs)

    app.state.CHAT_COMPLETION_HANDLER = observe_handler

    @app.middleware("http")
    async def observed(request: Request, call_next):
        # OWUI's SPA catch-all mount is already registered at import. A route
        # appended here would return the frontend HTML instead of JSON.
        if request.url.path == "/__lathe_lab_identity":
            return JSONResponse(identity)
        response = await call_next(request)
        if request.url.path == "/api/chat/completions":
            note("http_completion", status=response.status_code)
        response.headers["X-Lathe-Lab-Pid"] = str(identity["pid"])
        response.headers["X-Lathe-Lab-Incarnation"] = identity["incarnation"]
        return response

    # Only the controller in this container can access either worker or relay.
    uvicorn.run(app, host="127.0.0.1", port=PORTS[name], access_log=False)


async def inside():
    import httpx
    from aiohttp import web

    monitor = setup_imports()
    monitor.SECRETS.extend(os.environ.get(k, "") for k in
                          ("WEBUI_SECRET_KEY", "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY"))
    directory = Path("/lab/artifacts")
    directory.mkdir(parents=True, exist_ok=True)
    trace = monitor.Trace(directory)
    os.environ["LATHE_MONITOR_MODEL"] = MODEL
    # Do not inherit an operator's existing OWUI configuration or preview keys.
    for key in list(os.environ):
        if key.startswith(("OWUI_", "LATHE_PREVIEW_")):
            del os.environ[key]
    os.environ.update(OWUI_URL="http://127.0.0.1:8080",
                      LATHE_TEST_TOOL_ID="lathe_test")
    import test_deployment as suite
    suite.TRACE = trace
    relay = monitor.RecordingRelay(trace, os.environ["OPENROUTER_API_KEY"])
    relay_app = web.Application(client_max_size=MAX_PROMPT_BYTES)
    reservation = Reservation()

    async def bounded_relay(request):
        payload = await request.json()
        cost = reservation.reserve(payload, len(await request.read()))
        trace("reservation", cost=str(cost), total=str(reservation.reserved), ceiling=str(BUDGET))
        return await relay.handle(request)

    relay_app.router.add_post("/v1/chat/completions", bounded_relay)
    runner = web.AppRunner(relay_app, access_log=None)
    processes = {}
    handles = []
    client = None
    exit_code = 1

    def start(name, migrations=False):
        env = dict(os.environ, ENABLE_DB_MIGRATIONS=str(migrations).lower())
        # Raw logs stay inside disposable storage and are never exported.
        handle = open(f"/lab/{name}-{uuid.uuid4().hex}.raw.log", "wb")
        handles.append(handle)
        processes[name] = subprocess.Popen(
            [sys.executable, __file__, "--worker", name], env=env,
            stdout=handle, stderr=subprocess.STDOUT)

    async def identity(name):
        async with httpx.AsyncClient(timeout=3) as http:
            for _ in range(120):
                if processes[name].poll() is not None:
                    # Extract only a sanitized bounded tail, never export raw logs.
                    handle = handles[-1]
                    trace("worker_failure", worker=name, tail=Path(handle.name).read_text(errors="replace")[-5000:])
                    raise RuntimeError(f"Worker {name} exited")
                try:
                    response = await http.get(f"http://127.0.0.1:{PORTS[name]}/__lathe_lab_identity")
                    response.raise_for_status()
                    value = response.json()
                    require(value["pid"] == processes[name].pid, "Backend PID mismatch")
                    trace("backend_identity", **value)
                    return value
                except httpx.HTTPError:
                    await asyncio.sleep(1)
        raise RuntimeError(f"Worker {name} readiness deadline")

    def stop(name):
        process = processes.get(name)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        require(not process or process.poll() is not None, "Worker did not stop")

    try:
        async with asyncio.timeout(22 * 60):
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 9099).start()
            start("A", migrations=True)
            await identity("A")
            async with httpx.AsyncClient(timeout=10) as http:
                version = await http.get(suite.OWUI_BASE + "/api/version")
                version.raise_for_status()
                require(version.json().get("version", "").lstrip("v") == "0.12.4",
                        "Expected released OWUI 0.12.4")
                trace("owui_version", version=version.json()["version"])
            trace("bootstrap_step", name="monitor_bootstrap")
            token = await monitor.bootstrap(suite.OWUI_BASE, relay.key)
            trace("bootstrap_step", name="workspace_provider_policy")
            async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}"}) as http:
                response = await http.post(suite.OWUI_BASE + "/api/v1/models/model/update", json={
                    "id": monitor.workspace_model(), "base_model_id": MODEL, "name": "NEXT GLM Flash",
                    "params": {"function_calling": "native", "max_tokens": 4096,
                               "custom_params": {"provider": PROVIDER}},
                    "meta": {"capabilities": {"vision": False, "builtin_tools": True},
                             "builtinTools": {"subagents": True}},
                })
                response.raise_for_status()
                trace("bootstrap_step", name="subagents_config")
                response = await http.post(suite.OWUI_BASE + "/api/v1/configs/subagents", json={
                    "ENABLE_SUBAGENTS": True, "SUBAGENTS_BACKGROUND_ENABLED": True,
                    "SUBAGENTS_MAX_CONCURRENT": 2, "SUBAGENTS_MAX_ASYNC": 2,
                    "SUBAGENTS_MAX_ITERATIONS": 8, "SUBAGENTS_MAX_OUTPUT": 6000,
                    "SUBAGENTS_SYSTEM_PROMPT": ""})
                response.raise_for_status()
            trace("bootstrap_step", name="ordinary_completion_account")
            account = await monitor.completion_account(suite.OWUI_BASE, token)
            suite.OWUI_TOKEN = token
            suite.OWUI_TEST_TOKEN = account["token"]
            suite.OWUI_TEST_EMAIL = account["email"]
            suite.MODEL = monitor.workspace_model()
            admin, user = await suite.check_test_identity()
            trace("bootstrap_step", name="toolkit_deployment")
            await suite.deploy_staging_tool(admin, user, display_name="Disposable NEXT lab")
            # Provision exactly one small CPU sandbox ourselves, with authoritative
            # ownership label. Lathe only discovers/starts it; it cannot provision more.
            trace("bootstrap_step", name="single_small_sandbox")
            async with httpx.AsyncClient(timeout=180) as http:
                await provision_small(http, suite.OWUI_BASE, suite.auth_headers(), suite.DAYTONA_API_KEY,
                                      suite.DEPLOYMENT_LABEL, user["email"], trace)
            await suite.check_staging_access(admin, user)
            # B starts after DB migrations and shared configuration are committed.
            start("B")
            await identity("B")
            client = suite.OWUIClient()
            await client.connect()  # Keep the authenticated socket on A throughout.
            async with httpx.AsyncClient(timeout=30, headers=suite.auth_headers(completion=True)) as http:
                response = await http.post(suite.OWUI_BASE + "/api/v1/chats/new", json={
                    "chat": {"title": "NEXT continuity", "models": [suite.MODEL],
                             "history": {"messages": {}, "currentId": None}, "messages": []}})
                response.raise_for_status()
                chat_id = response.json()["id"]
                filename = f"/home/daytona/lathe-next-{uuid.uuid4().hex}.json"
                previous = None
                oracle = None

                async def turn(name, number):
                    nonlocal previous, oracle
                    trace("scenario_start", name=f"continuity-{number}-{name}")
                    who = await identity(name)
                    if number == 1:
                        code = "import os, secrets, json; lathe_next_nonce = secrets.token_hex(16); lathe_next_count = 1"
                    else:
                        code = "lathe_next_count += 1"
                    code += (f"; open({filename!r}, 'w').write(json.dumps(dict("
                             "nonce=lathe_next_nonce, count=lathe_next_count, python_pid=os.getpid())))"
                             "; print(lathe_next_count)")
                    prompt = ("Call Lathe interpret exactly once with this Python code. Do not recreate "
                              "missing globals, retry, or use bash/write to simulate success. Then stop.\n" + code)
                    aid, uid = str(uuid.uuid4()), str(uuid.uuid4())
                    client.active_request = (chat_id, aid)
                    client.events.clear()
                    client.done.clear()
                    client.last_output = []
                    client.traced_results = 0
                    client.max_calls = 2
                    client.budget_error = None
                    # Chat branch is authoritative; never submit a stale history snapshot.
                    saved = await http.get(suite.OWUI_BASE + f"/api/v1/chats/{chat_id}")
                    saved.raise_for_status()
                    messages = saved.json()["chat"]["history"]["messages"]
                    branch = []
                    cursor = previous
                    while cursor:
                        message = messages[cursor]
                        branch.append({"role": message["role"], "content": message.get("content", "")})
                        cursor = message.get("parentId")
                    payload = {
                        "model": suite.MODEL, "chat_id": chat_id, "id": aid,
                        "parent_id": previous,
                        "user_message": {"id": uid, "role": "user", "content": prompt,
                                         "parentId": previous, "childrenIds": [], "timestamp": int(time.time())},
                        "messages": [*reversed(branch), {"role": "user", "content": prompt}],
                        "session_id": client.session_id, "stream": True,
                        "tool_ids": [suite.TOOL_ID], "background_tasks": {},
                    }
                    try:
                        async with asyncio.timeout(240):
                            accepted = await http.post(
                                f"http://127.0.0.1:{PORTS[name]}/api/chat/completions", json=payload)
                            accepted.raise_for_status()
                            require(accepted.headers.get("X-Lathe-Lab-Pid") == str(who["pid"])
                                    and accepted.headers.get("X-Lathe-Lab-Incarnation") == who["incarnation"],
                                    "Completion backend assignment mismatch")
                            trace("request_assignment", chat_id=chat_id, message_id=aid, **who)
                            await client.done.wait()
                        require(not client.budget_error, "Tool-call budget exceeded")
                        calls = suite.tool_calls(client.last_output)
                        require(len(calls) == 1 and calls[0].get("name", "").endswith("interpret"),
                                "Expected one actual interpret call")
                        # Independent filesystem oracle, not assistant text or tool stdout.
                        headers = {"Authorization": f"Bearer {suite.DAYTONA_API_KEY}"}
                        response = await http.get("https://app.daytona.io/api/sandbox", headers=headers,
                            params={"labels": json.dumps({suite.DEPLOYMENT_LABEL: user["email"].lower()})})
                        response.raise_for_status()
                        data = response.json()
                        matches = [s for s in (data if isinstance(data, list) else data.get("items", []))
                                   if s.get("labels", {}).get(suite.DEPLOYMENT_LABEL) == user["email"].lower()]
                        require(len(matches) == 1 and not matches[0].get("volumes"), "Sandbox ownership/volume mismatch")
                        sandbox = matches[0]
                        response = await http.get(
                            f"https://proxy.app.daytona.io/toolbox/{sandbox['id']}/files/download",
                            params={"path": filename}, headers=headers)
                        response.raise_for_status()
                        value = response.json()
                        require(value["count"] == number and len(value["nonce"]) == 32, "File oracle mismatch")
                        if oracle:
                            require((value["nonce"], value["python_pid"]) ==
                                    (oracle["nonce"], oracle["python_pid"]), "Python memory/process changed")
                        import lathe
                        durable, _ = lathe._interpreter_paths(
                            SimpleNamespace(deployment_label=suite.DEPLOYMENT_LABEL),
                            sandbox["id"], user["email"].lower(), chat_id)
                        journal = await http.get(
                            f"https://proxy.app.daytona.io/toolbox/{sandbox['id']}/files/download",
                            params={"path": durable + "/state.json"}, headers=headers)
                        journal.raise_for_status()
                        state = journal.json()
                        require(state.get("generation") and state.get("active") is None,
                                "Interpreter journal is not idle/identified")
                        if oracle:
                            require(state["generation"] == oracle["generation"], "Interpreter generation changed")
                        value["generation"] = state["generation"]
                        oracle = value
                        previous = aid
                        trace("independent_read", sandbox_id=sandbox["id"], path=filename, value=value)
                        trace("scenario_pass", name=f"continuity-{number}-{name}")
                    finally:
                        client.active_request = None

                await turn("A", 1)
                await turn("B", 2)
                await turn("A", 3)
                before = await identity("B")
                stop("B")
                start("B")
                after = await identity("B")
                require(before["incarnation"] != after["incarnation"], "Worker was not replaced")
                trace("worker_replaced", before=before, after=after)
                await turn("B", 4)
                await turn("A", 5)
            await native_scenarios(suite, client.session_id, user, trace)
            relay.verify_evidence()
            exit_code = 0
    except BaseException as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            trace("http_failure", method=exc.request.method, path=exc.request.url.path,
                  status=exc.response.status_code, detail=exc.response.text[:3000])
        trace("fatal", error=f"{type(exc).__name__}: {exc}")
    finally:
        # Stop request producers BEFORE deleting their cloud resources.
        for name in ("B", "A"):
            try:
                stop(name)
            except Exception as exc:
                exit_code = 1
                trace("cleanup_error", error=str(exc))
        try:
            require(all(p.poll() is not None for p in processes.values()), "Live worker blocks cloud cleanup")
            await suite.cleanup_test_sandboxes()
        except Exception as exc:
            exit_code = 1
            trace("cleanup_error", error=str(exc))
        try:
            if client:
                await client.close()
        except Exception as exc:
            exit_code = 1
            trace("cleanup_error", error=str(exc))
        try:
            await runner.cleanup()
        except Exception as exc:
            exit_code = 1
            trace("cleanup_error", error=str(exc))
        for handle in handles:
            handle.close()
        (directory / "summary.json").write_text(monitor.sanitize(json.dumps({
            "exit_code": exit_code, "first_failure": trace.first_failure,
            "requested_model": MODEL, "served_models": sorted(trace.models),
            "provider_requests": relay.requests, "reserved_usd": str(reservation.reserved),
            "reservation_ceiling_usd": str(BUDGET), "unimplemented": UNIMPLEMENTED})))
    return exit_code


async def native_scenarios(suite, session_id, user, trace):
    """Real model delegation and server-owned continuation, with file barriers.

    Poll authoritative saved chats instead of admitting unrelated account-wide
    socket events. Harness process calls ONLY release filesystem barriers; they
    never insert internal messages, invoke private dispatch or generate results.
    """
    import httpx
    import shlex

    sandboxes = await owned_sandboxes(suite.DAYTONA_API_KEY, suite.DEPLOYMENT_LABEL)
    require(len(sandboxes) == 1, "Native lab requires the single owned sandbox")
    sandbox_id = sandboxes[0]["id"]
    toolbox = f"https://proxy.app.daytona.io/toolbox/{sandbox_id}"
    cloud_headers = {"Authorization": f"Bearer {suite.DAYTONA_API_KEY}"}
    async with httpx.AsyncClient(timeout=35, headers=suite.auth_headers(completion=True)) as http:
        async def saved(chat_id):
            response = await http.get(suite.OWUI_BASE + f"/api/v1/chats/{chat_id}")
            response.raise_for_status()
            return response.json()

        async def download(path, optional=False):
            response = await http.get(toolbox + "/files/download", params={"path": path}, headers=cloud_headers)
            if optional and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.content

        async def release(path):
            response = await http.post(toolbox + "/process/execute", headers=cloud_headers,
                json={"command": "touch " + shlex.quote(path), "timeout": 5000})
            response.raise_for_status()
            require(response.json().get("exitCode") == 0, "Barrier release failed")
            trace("barrier_release", path=path)

        async def wait_for(fn, detail, timeout=150):
            async with asyncio.timeout(timeout):
                while True:
                    result = await fn()
                    if result:
                        return result
                    await asyncio.sleep(0.5)

        async def wait_message(chat_id, message_id):
            async def done():
                data = await saved(chat_id)
                message = data["chat"]["history"]["messages"].get(message_id, {})
                require(not message.get("error"), "Native completion failed")
                return message if message.get("done") is True else None
            return await wait_for(done, "message completion")

        async def send(chat_id, prompt, system, name="A", previous=None):
            aid, uid = str(uuid.uuid4()), str(uuid.uuid4())
            data = await saved(chat_id)
            messages = data["chat"]["history"]["messages"]
            branch, cursor = [], previous
            while cursor:
                message = messages[cursor]
                branch.append({"role": message["role"], "content": message.get("content", "")})
                cursor = message.get("parentId")
            payload = {"model": suite.MODEL, "chat_id": chat_id, "id": aid,
                "parent_id": previous, "user_message": {"id": uid, "role": "user", "content": prompt,
                    "parentId": previous, "childrenIds": [], "timestamp": int(time.time())},
                "messages": [{"role": "system", "content": system}, *reversed(branch),
                             {"role": "user", "content": prompt}],
                "session_id": session_id, "stream": True, "tool_ids": [suite.TOOL_ID], "background_tasks": {}}
            response = await http.post(f"http://127.0.0.1:{PORTS[name]}/api/chat/completions", json=payload)
            response.raise_for_status()
            trace("native_request", chat_id=chat_id, message_id=aid, worker=name,
                  pid=response.headers.get("X-Lathe-Lab-Pid"),
                  incarnation=response.headers.get("X-Lathe-Lab-Incarnation"))
            return aid

        def waiting_code(started, gate, final):
            return ("import pathlib,time,secrets\n"
                f"pathlib.Path({started!r}).write_text('started')\n"
                "deadline=time.monotonic()+120\n"
                f"while not pathlib.Path({gate!r}).exists():\n"
                "    if time.monotonic()>deadline: raise TimeoutError('lab gate deadline')\n"
                "    time.sleep(0.2)\n"
                f"pathlib.Path({final!r}).write_text(secrets.token_hex(16))\n"
                "print('bounded work finished')")

        for busy in (False, True):
            scenario = "native_busy" if busy else "native_idle"
            trace("scenario_start", name=scenario)
            prefix = "/home/daytona/next-" + uuid.uuid4().hex
            source, copied = prefix + ".child", prefix + ".continued"
            child_started, child_gate = prefix + ".child-started", prefix + ".child-release"
            busy_started, busy_gate = prefix + ".busy-started", prefix + ".busy-release"
            response = await http.post(suite.OWUI_BASE + "/api/v1/chats/new", json={"chat": {
                "title": scenario, "models": [suite.MODEL], "messages": [],
                "history": {"messages": {}, "currentId": None}}})
            response.raise_for_status()
            parent_id = response.json()["id"]
            system = ("This is a bounded native continuation lab. Never delegate more than once. "
                "On receiving an ASYNC SUBAGENT COMPLETE internal message, use Lathe read to read "
                f"{source}, then Lathe bash to execute exactly: cp {source} {copied}. "
                "Only do that in the resumed turn, never during initial dispatch or the busy turn. "
                "Use the same tools; then finish. Do not schedule timers or invent internal metadata.")
            child_code = waiting_code(child_started, child_gate, source)
            task = ("Call Lathe interpret exactly once with timeout=150 and this exact code, "
                    "wait for it to finish, then give a short final summary. Do not delegate, "
                    "do not return before the code finishes, do not recreate the barrier.\n" + child_code)
            first = await send(parent_id,
                "Call native delegate_task once with background=true, context='', and task=" + json.dumps(task)
                + ". Then immediately finish with a short dispatch acknowledgment. Do not wait for the child.", system)
            initial = await wait_message(parent_id, first)
            trace("native_initial", chat_id=parent_id, message_id=first, output=initial.get("output"))
            dispatches = []
            for output in suite.tool_outputs(initial.get("output", [])):
                try:
                    item = json.loads(output)
                    if item.get("status") == "dispatched":
                        dispatches.append(item)
                except (ValueError, AttributeError):
                    pass
            calls = suite.tool_calls(initial.get("output", []))
            require(len(dispatches) == 1 and any(c.get("name") == "delegate_task" for c in calls),
                    "No actual native background delegation observed")
            dispatched = dispatches[0]
            child_id = dispatched["subagent_chat_id"]
            child = await saved(child_id)
            meta = child.get("meta") or {}
            require(meta.get("parent_chat_id") == parent_id and meta.get("delegation_id") == dispatched["delegation_id"],
                    "Native child association mismatch")
            await wait_for(lambda: download(child_started, True), "child entered interpreter")
            busy_aid = None
            if busy:
                busy_code = waiting_code(busy_started, busy_gate, prefix + ".busy-done")
                busy_aid = await send(parent_id,
                    "Call interpret once, timeout=150, with this code and wait for it to finish. "
                    "Do not inspect/copy the child result in this turn. Then finish.\n" + busy_code,
                    system, name="B", previous=first)
                await wait_for(lambda: download(busy_started, True), "parent entered busy interpreter")
            await release(child_gate)

            async def result_message():
                data = await saved(parent_id)
                values = data["chat"]["history"]["messages"].values()
                return next((m for m in values if (m.get("meta") or {}).get("delegation_id")
                             == dispatched["delegation_id"] and m.get("role") == "user"), None)

            result = await wait_for(result_message, "native result insertion")
            require(result["meta"].get("subagent_chat_id") == child_id, "Result child identity mismatch")
            if busy:
                require(result["meta"].get("status") == "pending", "Busy parent result was not pending")
                tasks = await http.get(suite.OWUI_BASE + f"/api/tasks/chat/{parent_id}")
                tasks.raise_for_status()
                require(tasks.json().get("task_ids"), "Busy parent was not active")
                require(await download(copied, True) is None, "Premature competing continuation")
                trace("native_pending", chat_id=parent_id, message_id=result["id"], child_id=child_id,
                      active_tasks=len(tasks.json()["task_ids"]))
                await release(busy_gate)
                await wait_message(parent_id, busy_aid)

            async def continued():
                data = await saved(parent_id)
                messages = data["chat"]["history"]["messages"]
                return next((m for m in messages.values() if m.get("role") == "assistant"
                    and m.get("done") is True and (messages.get(m.get("parentId")) or {}).get("meta", {})
                    .get("delegation_id") == dispatched["delegation_id"]), None)

            continuation = await wait_for(continued, "server-owned continuation")
            output = continuation.get("output", [])
            trace("native_continuation", chat_id=parent_id, child_id=child_id,
                  message_id=continuation["id"], output=output)
            require(any(c.get("name", "").endswith("read") for c in suite.tool_calls(output)),
                    "Resumed parent did not use Lathe read")
            original_bytes, copied_bytes = await download(source), await download(copied)
            require(len(original_bytes) == 32 and copied_bytes == original_bytes, "Independent native file oracle mismatch")
            child = await saved(child_id)
            child_messages = child["chat"]["history"]["messages"].values()
            require(any(c.get("name", "").endswith("interpret") for m in child_messages
                        for c in suite.tool_calls(m.get("output", []))), "Child did not use interpret")
            trace("independent_native_reads", chat_id=parent_id, child_id=child_id,
                  sha256=hashlib.sha256(original_bytes).hexdigest(), paths=[source, copied])
            trace("scenario_pass", name=scenario)


def cleanup(label, output):
    """Repeatable last-resort teardown; never acts on names without ownership labels."""
    monitor = setup_imports()
    require(re.fullmatch(r"lathe-ci-next-[a-f0-9]{16}", label), "Invalid owned lab label")
    expected = [label + suffix for suffix in ("-app", "-redis", "-pg")]
    errors, deleted, removed = [], [], []
    stopped = False
    try:
        names = run_command("docker", "ps", "-a", "--filter", f"label=lathe.readiness={label}",
                            "--format", "{{.Names}}").splitlines()
        require(set(names) <= set(expected), "Unexpected labelled container")
        if expected[0] in names:
            run_command("docker", "stop", "--time", "20", expected[0], timeout=40)
            require(run_command("docker", "inspect", "--format", "{{.State.Running}}", expected[0]) == "false",
                    "OWUI still running")
        stopped = True
        import test_deployment as suite
        suite.DEPLOYMENT_LABEL = label
        suite.DAYTONA_API_KEY = os.environ["DAYTONA_API_KEY"]
        suite.TRACE = lambda kind, **data: deleted.append(data["id"]) if kind == "sandbox_deleted" else None
        asyncio.run(suite.cleanup_test_sandboxes())
        require(not asyncio.run(owned_sandboxes(suite.DAYTONA_API_KEY, label)), "Owned sandboxes remain")
    except BaseException as exc:
        errors.append(monitor.sanitize(f"{type(exc).__name__}: {exc}"))
    # Even after cloud cleanup failure, kill producers and remove local resources.
    try:
        names = run_command("docker", "ps", "-a", "--filter", f"label=lathe.readiness={label}",
                            "--format", "{{.Names}}").splitlines()
        require(set(names) <= set(expected), "Unexpected labelled container")
        for name in expected:
            if name in names:
                run_command("docker", "rm", "--force", "--volumes", name)
                removed.append(name)
        networks = run_command("docker", "network", "ls", "--filter", f"label=lathe.readiness={label}",
                               "--format", "{{.Name}}").splitlines()
        require(set(networks) <= {label + "-net"}, "Unexpected labelled network")
        for name in networks:
            run_command("docker", "network", "rm", name)
            removed.append(name)
        all_names = run_command("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()
        all_networks = run_command("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
        require(not set(expected) & set(all_names) and label + "-net" not in all_networks,
                "Exact container/network removal not verified")
    except BaseException as exc:
        errors.append(monitor.sanitize(f"{type(exc).__name__}: {exc}"))
    result = {"label": label, "owui_stopped_before_cloud_cleanup": stopped,
              "sandbox_deletions_verified": deleted, "local_removed": removed,
              "success": not errors, "errors": errors}
    output.mkdir(parents=True, exist_ok=True)
    (output / f"cleanup-{time.time_ns()}.json").write_text(json.dumps(result, indent=2))
    return not errors


def host():
    monitor = setup_imports()
    require(all(os.environ.get(k) for k in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY")),
            "Export repository provider secrets through the trusted runner environment")
    label = "lathe-ci-next-" + secrets.token_hex(8)
    os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] = label
    network, database, redis, app = [label + suffix for suffix in ("-net", "-pg", "-redis", "-app")]
    owned = []
    output = root() / "monitor-artifacts" / "readiness-results" / label
    output.mkdir(parents=True, exist_ok=False)
    (root() / "monitor-artifacts" / "readiness-state.json").write_text(json.dumps({"label": label}))
    status = 1
    try:
        pricing = asyncio.run(verify_prices())
        (output / "pricing.json").write_text(json.dumps(pricing, indent=2))
        run_command("docker", "network", "create", "--label", f"lathe.readiness={label}", network)
        for name, image, options in [
            (database, "pgvector/pgvector:pg16", ["--tmpfs", "/var/lib/postgresql/data",
                "-e", "POSTGRES_USER=lathe", "-e", "POSTGRES_PASSWORD=disposable", "-e", "POSTGRES_DB=lathe"]),
            (redis, "redis:7-alpine", ["--tmpfs", "/data"]),
        ]:
            run_command("docker", "create", "--name", name, "--network", network,
                        "--label", f"lathe.readiness={label}", *options, image)
            owned.append(name)
            run_command("docker", "start", name)
        for _ in range(60):
            try:
                run_command("docker", "exec", database, "pg_isready", "-U", "lathe", timeout=5)
                break
            except subprocess.CalledProcessError:
                time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL readiness deadline")
        for _ in range(60):
            try:
                require(run_command("docker", "exec", redis, "redis-cli", "ping", timeout=5) == "PONG",
                        "Redis not ready")
                break
            except (subprocess.CalledProcessError, RuntimeError):
                time.sleep(1)
        else:
            raise RuntimeError("Redis readiness deadline")
        run_command("docker", "exec", database, "psql", "-U", "lathe", "-d", "lathe", "-c",
                    "CREATE EXTENSION IF NOT EXISTS vector;")
        shared_key = secrets.token_urlsafe(32)
        monitor.SECRETS.append(shared_key)
        env = {
            "DATABASE_URL": f"postgresql://lathe:disposable@{database}:5432/lathe",
            "VECTOR_DB": "pgvector", "PGVECTOR_DB_URL": f"postgresql://lathe:disposable@{database}:5432/lathe",
            "REDIS_URL": f"redis://{redis}:6379/0", "WEBSOCKET_REDIS_URL": f"redis://{redis}:6379/0",
            "WEBSOCKET_MANAGER": "redis", "ENABLE_WEBSOCKET_SUPPORT": "true",
            "REDIS_KEY_PREFIX": label, "WEBSOCKET_REDIS_ROOM_CHANNELS": "true",
            "WEBUI_SECRET_KEY": shared_key, "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY": shared_key,
            "DATA_DIR": "/lab/data", "CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS": "8",
            "RAG_EMBEDDING_ENGINE": "openai", "ENABLE_OLLAMA_API": "false",
            "LATHE_TEST_DEPLOYMENT_LABEL": label, "LATHE_ISOLATED_CI": "1",
        }
        options = [item for k, v in env.items() for item in ("-e", f"{k}={v}")]
        run_command("docker", "create", "--name", app, "--network", network,
                    "--label", f"lathe.readiness={label}",
                    "--tmpfs", "/app/backend/data",
                    "--entrypoint", "python", "-e", "DAYTONA_API_KEY", "-e", "OPENROUTER_API_KEY",
                    *options, IMAGE, "/tmp/readiness_lab.py", "--inside")
        owned.append(app)
        # docker cp creates no host bind mount and copies no .env or other secrets.
        run_command("docker", "cp", str(Path(__file__).resolve()), app + ":/tmp/readiness_lab.py")
        for filename in ("monitor_e2e.py", "test_deployment.py", "testing_support.py", "lathe.py"):
            run_command("docker", "cp", str(root() / filename), app + ":/tmp/" + filename)
        metadata = {"label": label, "owui_image": IMAGE, "model": MODEL,
                    "lathe_sha256": hashlib.sha256((root() / "lathe.py").read_bytes()).hexdigest(),
                    "checkout_sha": run_command("git", "-C", str(root()), "rev-parse", "HEAD"),
                    "max_provider_requests": MAX_REQUESTS, "max_request_bytes": MAX_PROMPT_BYTES,
                    "max_output_tokens_per_request": 4096, "sandbox_ttl_minutes": 30,
                    "lab_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "provider_reservation_usd": str(BUDGET),
                    "snapshot": SNAPSHOT,
                    "compute_estimate_usd": "<=0.034 at 1 vCPU/1GiB/3GiB for 30min; one sandbox only",
                    "unimplemented": UNIMPLEMENTED}
        metadata["image"] = json.loads(run_command("docker", "image", "inspect", IMAGE,
                                                    "--format", "{{json .RepoDigests}}"))
        (output / "environment.json").write_text(json.dumps(metadata, indent=2))
        run_command("docker", "start", app)
        status = int(run_command("docker", "wait", app, timeout=25 * 60))
    except BaseException as exc:
        (output / "host-error.log").write_text(monitor.sanitize(f"{type(exc).__name__}: {exc}"))
    finally:
        if app in owned:
            try:
                run_command("docker", "stop", "--time", "20", app, timeout=40)
            except Exception as exc:
                (output / "stop-error.log").write_text(monitor.sanitize(str(exc)))
            for filename in ("trace.jsonl", "summary.json", "worker-A.jsonl", "worker-B.jsonl"):
                try:
                    run_command("docker", "cp", f"{app}:/lab/artifacts/{filename}", str(output / filename))
                except Exception:
                    pass  # Startup failures legitimately have no inner evidence.
        if not cleanup(label, output):
            status = 1
    print(f"Readiness lab exit={status}; sanitized evidence: {output}")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--cleanup", action="store_true")
    mode.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--worker", choices=PORTS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.cleanup:
        state = root() / "monitor-artifacts" / "readiness-state.json"
        if not state.exists():
            print("No readiness run manifest: nothing owned")
            return 0
        label = json.loads(state.read_text())["label"]
        return 0 if cleanup(label, root() / "monitor-artifacts" / "readiness-results" / label) else 1
    if args.worker:
        require(Path("/tmp/monitor_e2e.py").is_file(), "Internal worker mode requires disposable helper copy")
        worker(args.worker)
        return 0
    if args.inside:
        require(Path("/tmp/monitor_e2e.py").is_file(), "Internal controller requires disposable helper copy")
        return asyncio.run(inside())
    if not args.run:
        require(all((root() / p).is_file() for p in
                    ("lathe.py", "monitor_e2e.py", "test_deployment.py", "testing_support.py")),
                "Required checkout helper missing")
        print(json.dumps({"network_or_cloud_work": False,
            "topology": "two loopback OWUI processes, shared container filesystem, PostgreSQL/pgvector + Redis",
            "owui": IMAGE, "model": MODEL, "routing": ["A", "B", "A", "replace B", "B", "A"],
            "saved_chat_endpoints": ["POST /api/v1/chats/new", "POST /api/chat/completions",
                                     "GET /api/v1/chats/{id}"],
            "limits": {"requests": MAX_REQUESTS, "input_bytes": MAX_PROMPT_BYTES,
                       "output_tokens_per_request": 4096, "wall_minutes": 25},
            "unimplemented": UNIMPLEMENTED,
            "provider_reservation_usd": str(BUDGET), "provider_policy": PROVIDER,
            "warning": "Requires explicit --run and trusted provider secrets. No production config is read."
        }, indent=2))
        return 0
    # Convert runner termination into normal unwinding so finally cleanup runs.
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    return host()


if __name__ == "__main__":
    raise SystemExit(main())

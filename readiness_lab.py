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

MODEL = "z-ai/glm-5.3-flash"
MAX_REQUESTS = 40
# Allow valid text schemas/history; the nonrefundable $2 reservation stays fixed.
MAX_PROMPT_BYTES = 128_000
PRICE = {"prompt": Decimal("0.00000015"), "completion": Decimal("0.0000005")}
PROVIDER = {"data_collection": "deny", "zdr": True,
            "max_price": {"prompt": 0.15, "completion": 0.5, "request": 0, "image": 0}}
BUDGET = Decimal("2.00")
PORTS = {"A": 8081, "B": 8082}
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


def relay_application(relay, reservation, trace):
    """Observe admission before aiohttp reads; never retain bodies or headers."""
    from aiohttp import web

    @web.middleware
    async def admission(request, handler):
        observation = uuid.uuid4().hex
        request["observation"] = observation
        trace("relay_admission", observation=observation, declared_bytes=request.content_length,
              ceiling_bytes=MAX_PROMPT_BYTES)
        status, error = 500, None
        try:
            if request.content_length is not None and request.content_length > MAX_PROMPT_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_PROMPT_BYTES,
                                                   actual_size=request.content_length)
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status, error = exc.status, type(exc).__name__
            raise
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            trace("relay_status", observation=observation, status=status, error=error,
                  declared_bytes=request.content_length, read_bytes=request.get("read_bytes"))

    async def bounded(request):
        body = await request.read()
        request["read_bytes"] = len(body)
        try:
            payload = json.loads(body)
            cost = reservation.reserve(payload, len(body))
        except (ValueError, TypeError, AttributeError, RuntimeError):
            raise web.HTTPBadRequest(text="Invalid or unbudgeted inference request") from None
        trace("reservation", cost=str(cost), total=str(reservation.reserved), ceiling=str(BUDGET),
              observation=request["observation"],
              wire_bytes=len(body), prompt_tokens_reserved=len(body) * 4 + 16384)
        return await relay.handle(request)

    app = web.Application(client_max_size=MAX_PROMPT_BYTES, middlewares=[admission])
    app.router.add_post("/v1/chat/completions", bounded)
    return app


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


async def provision_small(http, base, admin_headers, key, label, email, trace, before_provision=None):
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
    if before_provision:
        await before_provision(installed)
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


async def verify_workers(http, suite, relay_key, installed, trace):
    """Read both processes after restart, including their actual loaded toolkit."""
    import lathe
    normalized = lathe.Tools.Valves(**installed).model_dump()
    expected_valves = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    for name, port in PORTS.items():
        base = f"http://127.0.0.1:{port}"
        async def get(path, completion=False):
            response = await http.get(base + path, headers=suite.auth_headers(completion=completion))
            response.raise_for_status()
            return response.json()
        provider = await get("/openai/config")
        require(provider.get("ENABLE_OPENAI_API") is True
                and provider.get("OPENAI_API_BASE_URLS") == ["http://127.0.0.1:9099/v1"]
                and provider.get("OPENAI_API_KEYS") == [relay_key]
                and provider.get("OPENAI_API_CONFIGS", {}).get("0", {}).get("model_ids") == [MODEL],
                f"Worker {name} provider policy mismatch")
        model = await get("/api/v1/models/model?id=" + suite.MODEL)
        require(model.get("base_model_id") == MODEL
                and model.get("params", {}).get("function_calling") == "native"
                and model.get("params", {}).get("max_tokens") == 4096
                and model.get("params", {}).get("custom_params", {}).get("provider") == PROVIDER,
                f"Worker {name} model policy mismatch")
        require(suite.MODEL in {m["id"] for m in (await get("/api/models", True))["data"]},
                f"Worker {name} ordinary-user model access missing")
        valves = await get("/api/v1/tools/id/lathe_test/valves")
        require(valves == installed, f"Worker {name} valve readback mismatch")
        loaded = await get("/_readiness/loaded", True)
        trace("worker_loaded_observation", expected_valves_sha256=expected_valves, **loaded)
        require(loaded.get("valves_sha256") == expected_valves
                and loaded.get("source_sha256") == hashlib.sha256(Path("/opt/staged/lathe.py").read_bytes()).hexdigest()
                and {"interpret", "read", "bash"} <= set(loaded.get("tools", [])),
                f"Worker {name} loaded toolkit mismatch")
        trace("worker_config_verified", worker=name, pid=loaded["pid"],
              incarnation=loaded["incarnation"], valves_sha256=expected_valves,
              source_sha256=loaded["source_sha256"], model=suite.MODEL)


def run_command(*args, timeout=120, capture=True):
    result = subprocess.run(args, check=True, timeout=timeout,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    return result.stdout.decode().strip() if capture else ""


async def inside(scope="full", config_only=False):
    import httpx
    from aiohttp import web

    monitor = setup_imports()
    monitor.SECRETS.extend(os.environ.get(k, "") for k in
                          ("WEBUI_SECRET_KEY", "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY"))
    directory = Path("/lab/artifacts")
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("lathe.py", "testing_support.py"):
        require(Path("/tmp", name).read_bytes() == Path("/opt/staged", name).read_bytes(),
                "Controller source differs from qualified staged source")
    from guard import install_egress_guard
    from entrypoint import Workers
    workers = Workers()
    trace = monitor.Trace(directory)
    os.environ["LATHE_MONITOR_MODEL"] = MODEL
    # Do not inherit an operator's existing OWUI configuration or preview keys.
    for key in list(os.environ):
        if key.startswith(("OWUI_", "LATHE_PREVIEW_")):
            del os.environ[key]
    os.environ.update(OWUI_URL="http://127.0.0.1:8081",
                      LATHE_TEST_TOOL_ID="lathe_test")
    import test_deployment as suite
    suite.TRACE = trace
    relay = monitor.RecordingRelay(trace, os.environ["OPENROUTER_API_KEY"])
    reservation = Reservation()
    relay_app = relay_application(relay, reservation, trace)
    runner = web.AppRunner(relay_app, access_log=None)
    processes = workers.processes
    client = None
    exit_code = 1
    platform_ready = False

    class NativeComplete(Exception):
        """Leave the full-scope path while retaining common teardown/return."""

    async def identity(name):
        async with httpx.AsyncClient(timeout=3) as http:
            for _ in range(120):
                if processes[name].poll() is not None:
                    # Extract only a sanitized bounded tail, never export raw logs.
                    handle = workers.handles[-1]
                    trace("worker_failure", worker=name, tail=Path(handle.name).read_text(errors="replace")[-1800:])
                    raise RuntimeError(f"Worker {name} exited")
                try:
                    response = await http.get(f"http://127.0.0.1:{PORTS[name]}/_readiness/identity")
                    response.raise_for_status()
                    value = response.json()
                    require(value["pid"] == processes[name].pid, "Backend PID mismatch")
                    trace("backend_identity", **value)
                    return value
                except httpx.HTTPError:
                    await asyncio.sleep(1)
        raise RuntimeError(f"Worker {name} readiness deadline")

    try:
        async with asyncio.timeout(22 * 60):
            transport = check_transport()
            (directory / "transport-preflight.json").write_text(json.dumps(transport, indent=2))
            trace("transport_preflight", **transport)
            # Only after the key-free live-policy adapter gate may the controller
            # resolve real platform hosts or start any request producer.
            install_egress_guard(controller=True)
            platform_ready = True
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 9099).start()
            workers.migrate()
            workers.start("A")
            initial_a = await identity("A")
            workers.start("B")
            initial_b = await identity("B")
            if config_only:
                workers.replace("B")
                new_b = await identity("B")
                stable_a = await identity("A")
                require(initial_a == stable_a and initial_b["incarnation"] != new_b["incarnation"]
                        and new_b["migration"]["count"] == 1, "credential-free worker replacement failed")
                trace("replacement_verified", before=initial_b, after=new_b, stable=stable_a)
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
            async def refresh(installed):
                for name in PORTS:
                    workers.replace(name)
                    await identity(name)
                async with httpx.AsyncClient(timeout=30) as http:
                    await verify_workers(http, suite, relay.key, installed, trace)

            async with httpx.AsyncClient(timeout=180) as http:
                if config_only:
                    endpoint = suite.OWUI_BASE + "/api/v1/tools/id/lathe_test/valves"
                    response = await http.get(endpoint, headers=suite.auth_headers())
                    response.raise_for_status()
                    installed = response.json()
                    installed.update(auto_create_sandbox=False,
                        sandbox_create_overrides=json.dumps({"snapshot": SNAPSHOT, "ttlMinutes": 30}))
                    response = await http.post(endpoint + "/update", headers=suite.auth_headers(), json=installed)
                    response.raise_for_status()
                    await refresh(installed)
                    require(relay.requests == 0 and reservation.calls == 0, "key-free adapter attempted inference")
                    denied = Path("/run/readiness/egress-denied")
                    require(not denied.exists() or not denied.read_text(), "key-free adapter attempted external egress")
                    trace("config_preflight_pass", provider_requests=0)
                    raise NativeComplete
                await provision_small(http, suite.OWUI_BASE, suite.auth_headers(), suite.DAYTONA_API_KEY,
                                      suite.DEPLOYMENT_LABEL, user["email"], trace, before_provision=refresh)
            await suite.check_staging_access(admin, user)
            client = suite.OWUIClient()
            await client.connect()  # Keep the authenticated socket on A throughout.
            if scope == "native":
                await native_scenarios(suite, client.session_id, user, trace)
                relay.verify_evidence()
                raise NativeComplete
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
                workers.replace("B")
                after = await identity("B")
                require(before["incarnation"] != after["incarnation"], "Worker was not replaced")
                trace("worker_replaced", before=before, after=after)
                await turn("B", 4)
            await native_scenarios(suite, client.session_id, user, trace)
            relay.verify_evidence()
            exit_code = 0
    except NativeComplete:
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
                workers.stop(name)
            except Exception as exc:
                exit_code = 1
                trace("cleanup_error", error=str(exc))
        try:
            require(all(p.poll() is not None for p in processes.values()), "Live worker blocks cloud cleanup")
            if not config_only and platform_ready:
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
        workers.close()
        (directory / "summary.json").write_text(monitor.sanitize(json.dumps({
            "exit_code": exit_code, "first_failure": trace.first_failure, "scope": scope, "config_only": config_only,
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
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        result = await fn()
                        if result:
                            return result
                        await asyncio.sleep(0.5)
            except TimeoutError:
                raise TimeoutError(f"Native deadline: {detail}") from None

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
            delegations = [c for c in calls if c.get("name") == "delegate_task"]
            require(len(dispatches) == 1 and len(delegations) == 1,
                    "No actual native background delegation observed")
            args = delegations[0].get("arguments", {})
            args = json.loads(args) if isinstance(args, str) else args
            require(args.get("background") is True and args.get("task") == task
                    and args.get("context", "") == "", "Native delegation arguments changed")
            dispatched = dispatches[0]
            trace("native_dispatch", chat_id=parent_id, calls=calls, result=dispatched)
            child_id = dispatched["subagent_chat_id"]
            child = await saved(child_id)
            meta = child.get("meta") or {}
            require(meta.get("parent_chat_id") == parent_id and meta.get("delegation_id") == dispatched["delegation_id"],
                    "Native child association mismatch")
            trace("native_association", chat_id=parent_id, child_id=child_id, meta=meta)
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
            child_messages = list(child["chat"]["history"]["messages"].values())
            trace("native_child_results", chat_id=child_id, messages=child_messages)
            require(any(c.get("name", "").endswith("interpret") for m in child_messages
                        for c in suite.tool_calls(m.get("output", []))), "Child did not use interpret")
            entries = [json.loads(line) for name in PORTS
                       for line in Path(f"/lab/artifacts/worker-{name}.jsonl").read_text().splitlines()]
            entry = next((e for e in entries if e.get("kind") == "native_handler_entry"
                          and e.get("message_id") == continuation["id"]), None)
            expected_system = hashlib.sha256(json.dumps(
                [{"role": "system", "content": system}], sort_keys=True).encode()).hexdigest()
            require(entry and entry.get("model") == suite.MODEL
                    and entry.get("tool_ids") == [suite.TOOL_ID]
                    and entry.get("system_hash") == expected_system,
                    "Continuation lost actual model/tool/system context")
            trace("native_context_verified", **{k: v for k, v in entry.items() if k != "kind"})
            trace("independent_native_reads", chat_id=parent_id, child_id=child_id,
                  sha256=hashlib.sha256(original_bytes).hexdigest(), paths=[source, copied])
            trace("scenario_pass", name=scenario)


def cleanup(label, output, *, cloud=True):
    """Stop the shared topology before exact-label cloud cleanup, even on failure."""
    monitor = setup_imports()
    require(re.fullmatch(r"lathe-ci-next-[a-f0-9]{16}", label), "Invalid owned lab label")
    infra = infrastructure()
    directories = [output / name for name in ("preflight", "config-preflight", "live")]
    errors, deleted = [], []
    owned = []
    stopped = True
    cloud_attempted = False

    def record(exc):
        errors.append(monitor.sanitize(f"{type(exc).__name__}: {exc}"))

    # Retain validated ownership even if receipt storage disappears afterward.
    for directory in directories:
        try:
            if directory.exists():
                owned.append((directory, infra.load_manifest(directory)))
        except BaseException as exc:
            stopped = False
            record(exc)
    if cloud and not any(directory.name == "live" for directory, _ in owned):
        stopped = False
        record(RuntimeError("live ownership manifest unavailable; producer state unknown"))

    for directory, manifest in owned:
        try:
            infra.cleanup(directory, manifest=manifest)
        except BaseException as exc:
            record(exc)
        # A cleanup receipt failure is not a producer-state verdict. Only an
        # independent successful daemon read can authorize the cloud callback.
        try:
            infra.verify_absent(manifest)
        except BaseException as exc:
            stopped = False
            record(exc)

    try:
        if stopped and cloud:
            cloud_attempted = True
            import test_deployment as suite
            suite.DEPLOYMENT_LABEL = label
            suite.DAYTONA_API_KEY = os.environ["DAYTONA_API_KEY"]
            suite.TRACE = lambda kind, **data: deleted.append(data["id"]) if kind == "sandbox_deleted" else None
            asyncio.run(suite.cleanup_test_sandboxes())
            require(not asyncio.run(owned_sandboxes(suite.DAYTONA_API_KEY, label)), "Owned sandboxes remain")
    except BaseException as exc:
        record(exc)
    finally:
        # Try every known topology even after cloud, teardown or receipt errors.
        for directory, manifest in owned:
            try:
                infra.cleanup(directory, manifest=manifest)
            except BaseException as exc:
                record(exc)
    result = {"label": label, "owui_stopped_before_cloud_cleanup": stopped,
              "cloud_cleanup_attempted": cloud_attempted,
              "sandbox_deletions_verified": deleted,
              "success": not errors, "errors": errors}
    try:
        output.mkdir(parents=True, exist_ok=True)
        (output / f"cleanup-{time.time_ns()}.json").write_text(json.dumps(result, indent=2))
    except BaseException as exc:
        record(exc)
        # Preserve every sanitized failure if even the final report cannot be
        # stored. A receipt error must never turn an attempted cleanup into PASS.
        raise ExceptionGroup("readiness cleanup/evidence failures",
                             [RuntimeError(error) for error in errors]) from None
    return not errors


def infrastructure():
    import importlib.util
    spec = importlib.util.spec_from_file_location("readiness_infrastructure", root() / "readiness-infra/lab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_transport():
    """Isolate fixture DNS/guard hooks and never pass platform credentials."""
    result = subprocess.run([sys.executable, "/opt/readiness/transport_preflight.py"],
                            env={"PYTHONUNBUFFERED": "1"}, capture_output=True, timeout=30)
    require(result.returncode == 0, "key-free live transport gate failed: " + result.stderr.decode(errors="replace")[-2000:])
    receipt = json.loads(result.stdout)
    require(receipt.get("status") == "passed" and receipt.get("synthetic_loopback_only") is True
            and receipt.get("platform_acceptance") is False, "invalid transport qualification receipt")
    return receipt


def controller_action(infra, directory, *, config_only=False):
    """Wait/export only. The shared lifecycle owns local resources, even on failure."""
    directory.mkdir(parents=True, exist_ok=True)
    def action(app):
        status = None
        try:
            status = int(infra.docker("wait", app, timeout=25 * 60).strip())
            return status
        finally:
            infra.docker("stop", "--time", "20", app, timeout=40)
            for filename in ("trace.jsonl", "summary.json", "transport-preflight.json", "worker-A.jsonl", "worker-B.jsonl"):
                try:
                    infra.docker("cp", f"{app}:/lab/artifacts/{filename}", str(directory / filename))
                except RuntimeError:
                    if filename in ("trace.jsonl", "summary.json") or (status == 0 and
                            (filename == "transport-preflight.json" or not config_only)):
                        raise
    return action


def local_preflight(infra, output, label):
    qualified = infra.preflight(output / "preflight", root())
    for name, digest in qualified["staged_sha256"].items():
        require(hashlib.sha256((root() / name).read_bytes()).hexdigest() == digest,
                "Source changed after key-free qualification")
    infra.topology(output / "config-preflight", root(), image_id=qualified["built_image_id"],
                   live_action=controller_action(infra, output / "config-evidence", config_only=True),
                   config_only=True, qualified=qualified, deployment_label=label)
    return qualified


def host(scope="full"):
    monitor = setup_imports()
    require(all(os.environ.get(k) for k in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY")),
            "Export repository provider secrets through the trusted runner environment")
    label = "lathe-ci-next-" + secrets.token_hex(8)
    os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] = label
    output = root() / "monitor-artifacts" / "readiness-results" / label
    output.mkdir(parents=True, exist_ok=False)
    state = root() / "monitor-artifacts" / "readiness-state.json"
    state.write_text(json.dumps({"label": label, "cloud": False}))
    status = 1
    live_started = False
    infra = infrastructure()
    try:
        # No cloud calls, keys, or model configuration in the first topology.
        qualified = local_preflight(infra, output, label)
        pricing = asyncio.run(verify_prices())
        (output / "pricing.json").write_text(json.dumps(pricing, indent=2))
        metadata = {"label": label, "images": infra.IMAGES, "built_image_id": qualified["built_image_id"],
                    "model": MODEL, "scope": scope,
                    "lathe_sha256": hashlib.sha256((root() / "lathe.py").read_bytes()).hexdigest(),
                    "checkout_sha": run_command("git", "-C", str(root()), "rev-parse", "HEAD"),
                    "max_provider_requests": MAX_REQUESTS, "max_request_bytes": MAX_PROMPT_BYTES,
                    "max_prompt_token_reservation": MAX_PROMPT_BYTES * 4 + 16384,
                    "max_output_tokens_per_request": 4096, "sandbox_ttl_minutes": 30,
                    "lab_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "provider_reservation_usd": str(BUDGET),
                    "snapshot": SNAPSHOT,
                    "compute_estimate_usd": "<=0.034 at 1 vCPU/1GiB/3GiB for 30min; one sandbox only",
                    "unimplemented": UNIMPLEMENTED}
        (output / "environment.json").write_text(json.dumps(metadata, indent=2))

        state.write_text(json.dumps({"label": label, "cloud": True}))
        live_started = True
        infra.topology(output / "live", root(), image_id=qualified["built_image_id"],
                       live_action=controller_action(infra, output), scope=scope, qualified=qualified,
                       deployment_label=label)
        status = 0
    except BaseException as exc:
        (output / "host-error.log").write_text(monitor.sanitize(f"{type(exc).__name__}: {exc}"))
    finally:
        if not cleanup(label, output, cloud=live_started):
            status = 1
    print(f"Readiness lab exit={status}; sanitized evidence: {output}")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--preflight", action="store_true", help="key-free Docker boundaries and controller/config checks only")
    mode.add_argument("--cleanup", action="store_true")
    mode.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scope", choices=("full", "native"), default="full",
                        help="native runs only fresh-chat idle/busy continuation trials")
    args = parser.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    if args.run or args.preflight or args.inside:
        signal.signal(signal.SIGTERM, interrupted)
    if args.preflight:
        infra = infrastructure()
        output = infra.ROOT / "runs" / ("shared-" + uuid.uuid4().hex)
        print(f"Key-free artifacts: {output}", flush=True)
        local_preflight(infra, output, "lathe-ci-next-" + secrets.token_hex(8))
        print("Shared infrastructure and controller/config preflight passed; exact owned resources removed.")
        return 0
    if args.cleanup:
        state = root() / "monitor-artifacts" / "readiness-state.json"
        if not state.exists():
            print("No readiness run manifest: nothing owned")
            return 0
        saved = json.loads(state.read_text())
        label = saved["label"]
        return 0 if cleanup(label, root() / "monitor-artifacts" / "readiness-results" / label,
                            cloud=saved.get("cloud", True)) else 1
    if args.inside:
        require(Path("/tmp/monitor_e2e.py").is_file(), "Internal controller requires disposable helper copy")
        return asyncio.run(inside(args.scope, config_only=args.config_only))
    if not args.run:
        require(all((root() / p).is_file() for p in
                    ("lathe.py", "monitor_e2e.py", "test_deployment.py", "testing_support.py")),
                "Required checkout helper missing")
        print(json.dumps({"network_or_cloud_work": False,
            "topology": "two loopback OWUI processes, shared container filesystem, PostgreSQL/pgvector + Redis",
            "images": infrastructure().IMAGES, "model": MODEL, "scope": args.scope,
            "routing": ["A", "B"] if args.scope == "native" else ["A", "B", "A", "replace B", "B"],
            "saved_chat_endpoints": ["POST /api/v1/chats/new", "POST /api/chat/completions",
                                     "GET /api/v1/chats/{id}"],
            "limits": {"requests": MAX_REQUESTS, "input_bytes": MAX_PROMPT_BYTES,
                       "output_tokens_per_request": 4096, "wall_minutes": 25},
            "unimplemented": UNIMPLEMENTED,
            "provider_reservation_usd": str(BUDGET), "provider_policy": PROVIDER,
            "warning": "Requires explicit --run and trusted provider secrets. No production config is read."
        }, indent=2))
        return 0
    return host(args.scope)


if __name__ == "__main__":
    raise SystemExit(main())

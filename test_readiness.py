"""Offline safeguards for the opt-in disposable lab; no provider/Docker calls."""
import copy
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
import httpx

import readiness_lab as lab


def payload():
    return {"model": lab.MODEL, "provider": copy.deepcopy(lab.PROVIDER), "max_tokens": 4096,
            "messages": [{"role": "user", "content": "hello"}]}


@pytest.mark.parametrize("change", [
    lambda p: p.update(model="another/model"),
    lambda p: p["provider"].pop("zdr"),
    lambda p: p["provider"]["max_price"].update(prompt=1),
    lambda p: p.update(max_tokens=True),
    lambda p: p.update(max_tokens=4097),
    lambda p: p.update(plugins=[{"id": "web"}]),
    lambda p: p.update(n=2),
    lambda p: p.update(modalities=["image"]),
    lambda p: p["messages"][0].update(content=[{"type": "image_url"}]),
])
def test_unbudgeted_requests_refused_without_reservation(change):
    request = payload()
    change(request)
    budget = lab.Reservation()
    with pytest.raises(RuntimeError):
        budget.reserve(request, 1000)
    assert budget.calls == 0 and budget.reserved == 0


def test_budget_is_reserved_before_dispatch_and_never_overdrawn(monkeypatch):
    monkeypatch.setattr(lab, "BUDGET", Decimal("0.18"))
    budget = lab.Reservation()
    request = payload()
    cost = budget.reserve(request, lab.MAX_PROMPT_BYTES)
    assert cost > 0 and budget.calls == 1
    budget.reserve(request, lab.MAX_PROMPT_BYTES)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        budget.reserve(request, lab.MAX_PROMPT_BYTES)
    assert budget.reserved == cost * 2 <= lab.BUDGET


def test_request_cap(monkeypatch):
    monkeypatch.setattr(lab, "MAX_REQUESTS", 1)
    budget = lab.Reservation()
    budget.reserve(payload(), 100)
    with pytest.raises(RuntimeError):
        budget.reserve(payload(), 100)


def test_larger_text_ceiling_still_exhausts_real_two_dollar_budget():
    budget = lab.Reservation()
    cost = budget.reserve(payload(), lab.MAX_PROMPT_BYTES)
    while budget.reserved + cost <= lab.BUDGET and budget.calls < lab.MAX_REQUESTS:
        budget.reserve(payload(), lab.MAX_PROMPT_BYTES)
    assert budget.reserved <= Decimal("2.00")
    assert budget.calls < lab.MAX_REQUESTS
    before = (budget.calls, budget.reserved)
    with pytest.raises(RuntimeError, match="reservation budget exhausted"):
        budget.reserve(payload(), lab.MAX_PROMPT_BYTES)
    assert (budget.calls, budget.reserved) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "oversized", "chunked_oversized", "invalid", "budget", "upstream"])
async def test_real_aiohttp_admission_before_read_and_rejection(case, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    events, forwarded = [], []
    request = payload()
    request["messages"][0]["content"] = "private-body-" + "x" * (
        lab.MAX_PROMPT_BYTES if "oversized" in case else 100)
    body = json.dumps(request).encode() if case != "invalid" else b"private-invalid-json"
    budget = lab.Reservation()
    if case == "budget":
        monkeypatch.setattr(lab, "BUDGET", Decimal("0"))

    async def handle(incoming):
        forwarded.append(await incoming.read())
        return web.Response(status=503 if case == "upstream" else 200)

    app = lab.relay_application(SimpleNamespace(handle=handle), budget,
                                lambda kind, **data: events.append({"kind": kind, **data}))

    async def chunks():
        for start in range(0, len(body), 4096):
            yield body[start:start + 4096]

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions",
            data=chunks() if case == "chunked_oversized" else body,
            headers={"Authorization": "Bearer private-test-secret"})
        await response.read()
    expected = 413 if "oversized" in case else 400 if case in ("invalid", "budget") else 503 if case == "upstream" else 200
    assert response.status == expected
    assert events[0]["kind"] == "relay_admission"
    assert events[-1]["kind"] == "relay_status" and events[-1]["status"] == expected
    assert events[0]["observation"] == events[-1]["observation"]
    assert events[-1]["declared_bytes"] == (None if case == "chunked_oversized" else len(body))
    assert "private" not in json.dumps(events)
    if case in ("valid", "upstream"):
        assert forwarded == [body] and budget.calls == 1
        assert events[1]["prompt_tokens_reserved"] == len(body) * 4 + 16384
    else:
        assert not forwarded and budget.calls == 0 and budget.reserved == 0
        if case == "oversized":
            assert events[-1]["read_bytes"] is None  # Declared length rejected before request.read().


def test_default_and_plan_do_not_import_live_helpers(monkeypatch, capsys):
    monkeypatch.setattr(lab, "setup_imports", lambda: pytest.fail("Live helper imported"))
    for args in (["readiness_lab.py"], ["readiness_lab.py", "--plan"]):
        monkeypatch.setattr(sys, "argv", args)
        assert lab.main() == 0
        assert json.loads(capsys.readouterr().out)["network_or_cloud_work"] is False


def test_native_scope_plan_and_host_selection_without_live_work(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["readiness_lab.py", "--plan", "--scope", "native"])
    assert lab.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["scope"] == "native" and plan["network_or_cloud_work"] is False
    scopes = []
    monkeypatch.setattr(lab, "host", lambda scope: scopes.append(scope) or 0)
    monkeypatch.setattr(lab.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["readiness_lab.py", "--run", "--scope", "native"])
    assert lab.main() == 0 and scopes == ["native"]


@pytest.mark.parametrize("cloud_fails,removal_sticks", [(False, False), (True, False), (False, True)])
def test_cleanup_orders_and_independently_verifies(monkeypatch, tmp_path, cloud_fails, removal_sticks):
    label = "lathe-ci-next-0123456789abcdef"
    infra = lab.infrastructure()
    owner = "1" * 32
    network = "lathe-preflight-" + owner
    manifest = {"owner": owner, "network": network,
                "containers": [network + suffix for suffix in ("-pg", "-redis", "-app")]}
    (tmp_path / "live").mkdir()
    infra.save(tmp_path / "live/owner.json", manifest)
    names = set(manifest["containers"])
    networks = {network}
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "ps":
            return "\n".join(names)
        if args[:2] == ("network", "ls"):
            return "\n".join(networks)
        if args[0] == "inspect":
            return json.dumps([{"Name": "/" + args[1], "Id": args[1],
                                "Config": {"Labels": {infra.LABEL: owner}}}])
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Id": network, "Labels": {infra.LABEL: owner}, "Containers": {}}])
        if args[0] == "rm" and not removal_sticks:
            names.discard(args[-1])
        if args[:2] == ("network", "rm"):
            networks.discard(args[-1])
        return ""

    async def cloud_cleanup():
        assert not names and not networks
        if cloud_fails:
            raise KeyboardInterrupt("interrupted cloud cleanup")

    async def no_sandboxes(*args):
        return []

    monkeypatch.setattr(infra, "docker", command)
    monkeypatch.setattr(lab, "infrastructure", lambda: infra)
    monkeypatch.setattr(lab, "owned_sandboxes", no_sandboxes)
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace(sanitize=lambda x: x))
    monkeypatch.setitem(sys.modules, "test_deployment", SimpleNamespace(cleanup_test_sandboxes=cloud_cleanup))
    monkeypatch.setenv("DAYTONA_API_KEY", "offline-fake-key")
    assert lab.cleanup(label, tmp_path) is (not cloud_fails and not removal_sticks)
    record = json.loads(next(tmp_path.glob("cleanup-*.json")).read_text())
    assert record["owui_stopped_before_cloud_cleanup"] is (not removal_sticks)
    assert any(c[0] == "rm" for c in calls)
    assert not networks


def test_cleanup_refuses_non_lab_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace())
    monkeypatch.setattr(lab, "run_command", lambda *a, **k: pytest.fail("Unsafe Docker call"))
    with pytest.raises(RuntimeError, match="Invalid owned"):
        lab.cleanup("production", tmp_path)


@pytest.mark.asyncio
async def test_worker_identity_precedes_existing_frontend_catchall(monkeypatch):
    import importlib.util
    from pathlib import Path

    async def original(*args, **kwargs):
        pass

    async def spa_fallback(*args):
        pytest.fail("Identity request reached existing SPA fallback")
    class App:
        state = SimpleNamespace(CHAT_COMPLETION_HANDLER=original)
        __call__ = spa_fallback
    app = App()
    monkeypatch.setitem(sys.modules, "open_webui", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "open_webui.main", SimpleNamespace(app=app))
    monkeypatch.setitem(sys.modules, "guard", SimpleNamespace(install_egress_guard=lambda: None))
    monkeypatch.setenv("READINESS_WORKER", "A")
    read_text = Path.read_text
    def text(path, *a, **k):
        if str(path) == "/run/readiness/migration.json":
            return '{"pid":1,"count":1}'
        if str(path) == "/app/backend/data/readiness-marker":
            return "marker"
        return read_text(path, *a, **k)
    monkeypatch.setattr(Path, "read_text", text)
    spec = importlib.util.spec_from_file_location("shared_worker", lab.root() / "readiness-infra/worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    sent = []
    async def send(message):
        sent.append(message)
    await worker.app({"type": "http", "path": "/_readiness/identity"}, None, send)
    result = json.loads(sent[-1]["body"])
    assert result["worker"] == "A" and result["pid"] > 0 and result["incarnation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "large_snapshot", "valve_resource_override", "lost_key", "policy_drift"])
async def test_real_provisioning_configuration_and_installed_valve_gate(fault):
    label, key, email = "lathe-ci-next-0123456789abcdef", "offline-key", "completion@lathe.invalid"
    installed = {"daytona_api_key": key, "deployment_label": label,
                 "foreground_timeout_seconds": 30, "sandbox_create_overrides": "{}"}
    requests, trace = [], []

    def respond(request):
        nonlocal installed
        path = request.url.path
        requests.append((request.method, path))
        if path == "/api/snapshots/daytona-small":
            return httpx.Response(200, json={"name": "daytona-small", "id": "snapshot-id",
                "state": "active", "cpu": 8 if fault == "large_snapshot" else 1,
                "mem": 1, "disk": 3, "gpu": 0})
        if path.endswith("/valves/update"):
            installed = json.loads(request.content)
            # The exact object production posts to OWUI, not a copied builder.
            assert json.loads(installed["sandbox_create_overrides"]) == {"snapshot": "daytona-small", "ttlMinutes": 30}
            assert installed["daytona_api_key"] == key
            assert installed["foreground_timeout_seconds"] == 30
            if fault == "valve_resource_override":
                installed["sandbox_create_overrides"] = '{"snapshot":"daytona-small","ttlMinutes":30,"cpu":1}'
            if fault == "lost_key":
                installed["daytona_api_key"] = ""
            if fault == "policy_drift":
                installed["auto_delete_minutes"] = 60
            return httpx.Response(200, json=installed)
        if path.endswith("/valves"):
            return httpx.Response(200, json=installed)
        if path == "/api/sandbox":
            create = json.loads(request.content)
            assert create == {"snapshot": "daytona-small", "ttlMinutes": 30, "name": label,
                "labels": {label: email}, "autoStopInterval": 5, "autoArchiveInterval": 5, "autoDeleteInterval": 0}
            assert trace[-1][0] == "installed_valves_verified"
            return httpx.Response(200, json={"id": "sandbox-id", "cpu": 1, "memory": 1, "disk": 3,
                                            "labels": {label: email}, "volumes": []})
        pytest.fail(f"Unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        coroutine = lab.provision_small(http, "http://owui", {"Authorization": "Bearer admin"}, key, label, email,
                                       lambda kind, **data: trace.append((kind, data)))
        if fault:
            with pytest.raises(RuntimeError):
                await coroutine
            assert ("POST", "/api/sandbox") not in requests
        else:
            info = await coroutine
            assert info["id"] == "sandbox-id"
            assert requests[-2:] == [("GET", "/api/v1/tools/id/lathe_test/valves"), ("POST", "/api/sandbox")]


@pytest.mark.parametrize("failure", [None, "preflight", "config", "live", "export"])
def test_host_runs_shared_keyfree_gates_before_live_and_always_cleans(tmp_path, monkeypatch, failure):
    import hashlib
    for name in ("lathe.py", "testing_support.py"):
        (tmp_path / name).write_text(name)
    hashes = {name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
              for name in ("lathe.py", "testing_support.py")}
    calls = []
    qualified = {"built_image_id": "sha256:qualified", "staged_sha256": hashes}
    def preflight(directory, source):
        calls.append("preflight")
        if failure == "preflight":
            raise RuntimeError("injected preflight failure")
        return qualified
    def topology(directory, source, **kwargs):
        phase = "config" if kwargs.get("config_only") else "live"
        calls.append(phase)
        assert kwargs["image_id"] == "sha256:qualified"
        assert kwargs["qualified"] is qualified
        if failure == phase:
            raise RuntimeError("injected controller failure")
        if phase == "live" and failure == "export":
            raise PermissionError("injected export failure")
    async def prices():
        assert calls == ["preflight", "config"]
        calls.append("prices")
        return {}
    def cleanup(label, directory, *, cloud):
        calls.append("cleanup-cloud" if cloud else "cleanup-keyfree")
        return True
    monkeypatch.setenv("DAYTONA_API_KEY", "unused-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused-test-key")
    monkeypatch.setattr(lab, "root", lambda: tmp_path)
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace(sanitize=lambda value: value))
    monkeypatch.setattr(lab, "infrastructure", lambda: SimpleNamespace(
        preflight=preflight, topology=topology, IMAGES={"owui": "pinned"}))
    monkeypatch.setattr(lab, "verify_prices", prices)
    monkeypatch.setattr(lab, "cleanup", cleanup)
    def command(*args, **kwargs):
        assert args[0] == "git", "host attempted legacy Docker bootstrap"
        return "checkout"
    monkeypatch.setattr(lab, "run_command", command)
    assert lab.host("native") == (0 if failure is None else 1)
    assert calls[-1] == ("cleanup-keyfree" if failure in ("preflight", "config") else "cleanup-cloud")
    if failure == "preflight":
        assert calls == ["preflight", "cleanup-keyfree"]
    if failure == "config":
        assert calls == ["preflight", "config", "cleanup-keyfree"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_worker", [None, "A", "B"])
async def test_cross_worker_loaded_configuration_gate(tmp_path, monkeypatch, stale_worker):
    import hashlib
    import lathe
    from pathlib import Path
    installed = {"daytona_api_key": "synthetic", "auto_create_sandbox": False}
    normalized = lathe.Tools.Valves(**installed).model_dump()
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    real_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"staged" if str(path) == "/opt/staged/lathe.py" else real_read(path))
    seen = []
    def respond(request):
        worker = "A" if request.url.port == lab.PORTS["A"] else "B"
        seen.append((worker, request.url.path))
        if request.url.path == "/openai/config":
            value = {"ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": ["http://127.0.0.1:9099/v1"],
                     "OPENAI_API_KEYS": ["relay"], "OPENAI_API_CONFIGS": {"0": {"model_ids": [lab.MODEL]}}}
        elif request.url.path == "/api/v1/models/model":
            value = {"base_model_id": lab.MODEL, "params": {"function_calling": "native", "max_tokens": 4096,
                     "custom_params": {"provider": lab.PROVIDER}}}
        elif request.url.path == "/api/models":
            value = {"data": [{"id": "workspace"}]}
        elif request.url.path.endswith("/valves"):
            value = installed
        elif request.url.path == "/_readiness/loaded":
            value = {"worker": worker, "pid": request.url.port, "incarnation": worker,
                     "tools": ["interpret", "read", "bash"], "valves_sha256": "stale" if stale_worker == worker else digest,
                     "source_sha256": hashlib.sha256(b"staged").hexdigest()}
        else:
            pytest.fail(str(request.url))
        return httpx.Response(200, json=value)
    suite = SimpleNamespace(MODEL="workspace", auth_headers=lambda **kwargs: {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        check = lab.verify_workers(http, suite, "relay", installed, lambda *a, **k: None)
        if stale_worker:
            with pytest.raises(RuntimeError, match="loaded toolkit mismatch"):
                await check
        else:
            await check
            assert {name for name, path in seen if path == "/_readiness/loaded"} == {"A", "B"}


def test_transport_gate_subprocess_never_inherits_platform_credentials(monkeypatch):
    import subprocess
    monkeypatch.setenv("DAYTONA_API_KEY", "must-not-pass-to-fixture")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-pass-to-fixture")
    expected = {"status": "passed", "synthetic_loopback_only": True, "platform_acceptance": False}
    def run(args, **kwargs):
        assert args == [sys.executable, "/opt/readiness/transport_preflight.py"]
        assert kwargs["env"] == {"PYTHONUNBUFFERED": "1"}
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(args, 0, json.dumps(expected).encode(), b"")
    monkeypatch.setattr(lab.subprocess, "run", run)
    assert lab.check_transport() == expected

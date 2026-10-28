"""Offline safeguards for the opt-in disposable lab; no provider/Docker calls."""
import copy
import json
import sys
from pathlib import Path
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


@pytest.mark.parametrize("cloud_fails,removal_sticks,local_receipt_fails,final_receipt_fails,daemon_fails", [
    (False, False, False, False, False),
    (True, False, False, False, False),
    (False, True, False, False, False),
    (False, False, True, False, False),
    (True, False, True, False, False),
    (False, True, True, False, False),
    (False, False, False, True, False),
    (True, False, True, True, False),
    (False, False, False, False, True),
])
def test_cleanup_orders_and_independently_verifies(monkeypatch, tmp_path, cloud_fails, removal_sticks,
                                                  local_receipt_fails, final_receipt_fails, daemon_fails):
    from pathlib import Path
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
    events = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[0] == "ps":
            events.append("daemon-read")
            if daemon_fails:
                raise RuntimeError("injected daemon unavailable")
            return "\n".join(names)
        if args[:2] == ("network", "ls"):
            return "\n".join(networks)
        if args[0] == "inspect":
            return json.dumps([{"Name": "/" + args[1], "Id": args[1],
                                "Config": {"Labels": {infra.LABEL: owner}}}])
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Id": network, "Labels": {infra.LABEL: owner},
                                "Containers": {name: {} for name in names}}])
        if args[0] == "rm" and not removal_sticks:
            names.discard(args[-1])
        if args[:2] == ("network", "rm"):
            networks.discard(args[-1])
        return ""

    async def cloud_cleanup(key, label):
        events.append("cloud")
        assert not names and not networks
        if local_receipt_fails:
            failed = events.index("local-receipt-failed")
            assert "daemon-read" in events[failed + 1:-1], "receipt failure was not independently reverified"
        if cloud_fails:
            raise KeyboardInterrupt("interrupted cloud cleanup")
        return []

    actual_save = infra.save
    def save(path, value):
        if path.name == "cleanup.json":
            # This faults the real cleanup's persistence, only AFTER its actual
            # successful daemon verification and exact resource removal.
            assert not names and not networks
            events.append("local-receipt-failed" if local_receipt_fails else "local-receipt")
            if local_receipt_fails:
                raise PermissionError("injected local cleanup receipt failure")
        actual_save(path, value)

    actual_write = Path.write_text
    def write(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith("cleanup-") and final_receipt_fails:
            raise PermissionError("injected final cleanup report failure")
        return actual_write(path, *args, **kwargs)

    monkeypatch.setattr(infra, "docker", command)
    monkeypatch.setattr(infra, "save", save)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(lab, "infrastructure", lambda: infra)
    monkeypatch.setattr(lab, "cloud_cleanup", cloud_cleanup)
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace(sanitize=lambda x: x))
    monkeypatch.setenv("DAYTONA_API_KEY", "offline-fake-key")
    if final_receipt_fails:
        with pytest.raises(ExceptionGroup) as caught:
            lab.cleanup(label, tmp_path)
        errors = [str(exc) for exc in caught.value.exceptions]
        assert "injected final cleanup report failure" in errors[-1]
    else:
        assert lab.cleanup(label, tmp_path) is (not any((cloud_fails, removal_sticks, local_receipt_fails, daemon_fails)))
        record = json.loads(next(tmp_path.glob("cleanup-*.json")).read_text())
        assert record["owui_stopped_before_cloud_cleanup"] is (not removal_sticks and not daemon_fails)
        assert record["cloud_cleanup_attempted"] is (not removal_sticks and not daemon_fails)
        errors = record["errors"]
    if local_receipt_fails and not removal_sticks:
        assert any("injected local cleanup receipt failure" in error for error in errors)
    if cloud_fails:
        assert any("interrupted cloud cleanup" in error for error in errors)
    assert events.count("cloud") == (0 if removal_sticks or daemon_fails else 1)
    if removal_sticks or daemon_fails:
        assert names and networks
        assert sum(c[0] == "ps" for c in calls) >= 3  # initial, independent verification, final teardown
    else:
        assert not names and not networks
        receipts = [i for i, event in enumerate(events) if event.startswith("local-receipt")]
        assert len(receipts) == 2 and receipts[0] < events.index("cloud") < receipts[1]


def test_cleanup_refuses_non_lab_namespace(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "setup_imports", lambda: SimpleNamespace())
    monkeypatch.setattr(lab, "run_command", lambda *a, **k: pytest.fail("Unsafe Docker call"))
    with pytest.raises(RuntimeError, match="Invalid owned"):
        lab.cleanup("production", tmp_path)


@pytest.mark.asyncio
async def test_worker_identity_precedes_existing_frontend_catchall(monkeypatch, tmp_path):
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
    evidence_spec = importlib.util.spec_from_file_location("worker_evidence", lab.root() / "readiness-infra/evidence.py")
    evidence = importlib.util.module_from_spec(evidence_spec)
    evidence_spec.loader.exec_module(evidence)
    evidence.DIRECTORY = tmp_path
    monkeypatch.setitem(sys.modules, "evidence", evidence)
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
    records = evidence.read_observer(result)
    assert len(records) == 1 and records[0] == result["observer_start"]


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


@pytest.mark.parametrize("config_only,missing,should_fail", [
    (True, "worker-B.jsonl", True), (True, "native_idle-canary.json", False),
    (False, "native_idle-canary.json", True),
])
def test_successful_controller_requires_observers_and_applicable_canary_exports(tmp_path, config_only, missing, should_fail):
    events = []
    def docker(*args, **kwargs):
        events.append(args)
        if args[0] == "wait":
            return "0"
        if args[0] == "cp" and args[1].endswith("/" + missing):
            raise RuntimeError("injected missing/unreadable required artifact")
        return ""
    action = lab.controller_action(SimpleNamespace(docker=docker), tmp_path, config_only=config_only)
    if should_fail:
        with pytest.raises(RuntimeError, match="required artifact"):
            action("owned-app")
    else:
        assert action("owned-app") == 0
    assert events[1][0] == "stop"


@pytest.mark.parametrize("fault", ["trace-missing", "stop-and-summary", "wait-interrupted"])
def test_canary_receipts_export_independently_of_other_artifact_failures(tmp_path, fault):
    def docker(*args, **kwargs):
        if args[0] == "wait":
            if fault == "wait-interrupted":
                raise KeyboardInterrupt("injected runner termination")
            return "1"
        if args[0] == "stop" and fault == "stop-and-summary":
            raise RuntimeError("injected stop failure")
        if args[0] == "cp":
            name = args[1].rsplit("/", 1)[1]
            if (fault == "trace-missing" and name == "trace.jsonl") or (
                    fault == "stop-and-summary" and name == "summary.json"):
                raise RuntimeError(f"injected {name} export failure")
            if name.endswith("-canary.json") or fault == "wait-interrupted":
                Path(args[2]).write_text(name)
        return ""
    action = lab.controller_action(SimpleNamespace(docker=docker), tmp_path)
    with pytest.raises((RuntimeError, BaseExceptionGroup, KeyboardInterrupt)) as caught:
        action("owned-app")
    # The independent oracle receipts are exported even though another artifact failed.
    assert all((tmp_path / f"native_{s}-canary.json").exists() for s in ("idle", "busy"))
    if fault == "trace-missing":
        assert "trace.jsonl export failure" in str(caught.value)
    elif fault == "stop-and-summary":
        messages = [str(e) for e in caught.value.exceptions]
        assert messages == ["injected stop failure", "injected summary.json export failure"]
    else:
        assert isinstance(caught.value, KeyboardInterrupt)


LABEL = "lathe-ci-next-0123456789abcdef"
OWNER = "completion@lathe.invalid"


def daytona(views, *, page_size=100, server_filters=True, record=None):
    """MockTransport for Daytona sandbox endpoints with `nextCursor` paging.

    `views` is one org-wide sandbox list per successive list query (per labels
    filter value), modelling the eventually consistent index; the last repeats.
    `server_filters=False` returns unfiltered pages, so the client re-check and
    pagination must find owned sandboxes beyond page one."""
    calls = {}
    def respond(request):
        if record is not None:
            record.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path
        if request.method == "GET" and path == "/api/sandbox":
            labels = request.url.params.get("labels", "{}")  # Absent: unfiltered org listing.
            cursor = int(request.url.params.get("cursor", 0))
            if cursor == 0:
                calls[labels] = calls.get(labels, 0) + 1
            view = views[min(calls[labels], len(views)) - 1]
            wanted = json.loads(labels)
            items = [s for s in view if not server_filters
                     or all(s["labels"].get(k) == v for k, v in wanted.items())]
            page = items[cursor:cursor + page_size]
            nxt = cursor + page_size if cursor + page_size < len(items) else None
            return httpx.Response(200, json={"items": page, "nextCursor": None if nxt is None else str(nxt)})
        if request.method == "GET" and path.startswith("/api/sandbox/"):
            sandbox_id = path.rsplit("/", 1)[1]
            for s in views[-1]:
                if s["id"] == sandbox_id:
                    return httpx.Response(200, json={**s, "state": "started", "volumes": []})
            return httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected Daytona request {request.method} {path}")
    return httpx.MockTransport(respond)


def owned(n, label=LABEL, email=OWNER):
    return {"id": f"0000000{n}-0000-4000-8000-000000000000", "labels": {label: email}}


# Unrelated org sandboxes, including a same-key/other-value one.
NOISE = [{"id": f"noise-{i}", "labels": {"other": "x"}} for i in range(150)] + [
    {"id": "same-key-other-owner", "labels": {LABEL: "someone@else.invalid"}}]


@pytest.mark.parametrize("case", ["eventually-consistent", "page-two", "two-owned", "never-appears"])
async def test_bootstrap_polls_exact_label_until_one_confirmed(case):
    requests = []
    views = {"eventually-consistent": [NOISE, NOISE, NOISE + [owned(1)]],
             "page-two": [NOISE + [owned(1)]], "two-owned": [[owned(1)] + NOISE + [owned(2)]],
             "never-appears": [NOISE]}[case]
    transport = daytona(views, page_size=100, server_filters=case not in ("page-two", "two-owned"), record=requests)
    async with httpx.AsyncClient(transport=transport) as http:
        call = lab.single_owned_sandbox(http, LABEL, OWNER, attempts=5, interval=0)
        if case == "two-owned":
            with pytest.raises(RuntimeError, match=r"observed 2: \['00000001"):
                await call
        elif case == "never-appears":
            with pytest.raises(RuntimeError, match="after 5 polls, observed 0"):
                await call
        else:
            assert (await call)["id"] == owned(1)["id"]
            # Confirmed by the authoritative per-ID endpoint, not the index alone.
            assert requests[-1][:2] == ("GET", f"/api/sandbox/{owned(1)['id']}")
    lists = [r for r in requests if r[1] == "/api/sandbox"]
    assert all(json.loads(r[2]["labels"]) == {LABEL: OWNER} for r in lists)  # Exact server-side filter.
    if case == "page-two":
        assert [r[2].get("cursor") for r in lists] == [None, "100"]  # Owned sandbox is item 152, page 2.


@pytest.mark.parametrize("case", ["clean", "late-arrival", "straggler-page-two"])
async def test_cloud_cleanup_deletes_exact_label_and_verifies_absence(case):
    requests = []
    target = owned(1, email="monitor@lathe.invalid")
    views = {"clean": [NOISE + [target], NOISE],
             # Lagging index: empty twice, then the owned sandbox appears, then gone.
             "late-arrival": [NOISE, NOISE, NOISE + [target], NOISE],
             # Deletion never takes effect, and both owned sandboxes sit beyond page one.
             "straggler-page-two": [NOISE + [owned(9), target]]}[case]
    transport = daytona(views, server_filters=case != "straggler-page-two", record=requests)
    call = lab.cloud_cleanup("offline-key", LABEL, attempts=8, interval=0, transport=transport)
    if case == "straggler-page-two":
        with pytest.raises(RuntimeError, match="remain after 8 checks: 2"):
            await call
    else:
        assert await call == [target["id"]]
    lists = [r for r in requests if r[1] == "/api/sandbox"]
    assert {r[2]["labels"] for r in lists} == {json.dumps({LABEL: e}) for e in lab.LAB_EMAILS}
    deletes = [r[1].rsplit("/", 1)[1] for r in requests if r[0] == "DELETE"]
    # Only exactly-labelled lab sandboxes are deleted, each once; never noise or another owner.
    assert sorted(deletes) == sorted({target["id"]} | ({owned(9)["id"]} if case == "straggler-page-two" else set()))

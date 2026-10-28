"""Offline protection for the monitor's isolation and evidence boundaries."""

import importlib.util
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import monitor_e2e as monitor


@pytest.fixture(autouse=True)
def default_monitor_selection(monkeypatch):
    # Tests must protect both the unchanged default and explicit overrides,
    # regardless of the model chosen for the enclosing live workflow.
    monkeypatch.delenv("LATHE_MONITOR_MODEL", raising=False)


@pytest.mark.parametrize("credential", [
    "dtn_fake-token", "sk-or-v1-fake-token", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.signature",
    "Bearer unknown-secret", "https://new-preview-host.example/path?unknown-signature=secret",
])
def test_artifacts_redact_credentials(tmp_path, credential):
    trace = monitor.Trace(tmp_path)
    trace("chat_output", output={"text": credential})
    stored = trace.path.read_text()
    assert credential not in stored
    assert json.loads(stored)["kind"] == "chat_output"


def test_registered_secrets_and_failure_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "unusual-provider-key")
    monkeypatch.setattr(monitor, "SECRETS", ["generated-password"])
    trace = monitor.Trace(tmp_path)
    trace("scenario_start", name="file copy")
    trace("scenario_fail", error="unusual-provider-key generated-password")
    trace("scenario_start", name="cleanup")
    trace("fatal", error="another failure")
    assert trace.first_failure == "file copy"
    assert "unusual-provider-key" not in trace.path.read_text()
    assert "generated-password" not in trace.path.read_text()


@pytest.mark.parametrize("selected", [monitor.ROUTER, "z-ai/glm-5.3-flash"])
@pytest.mark.parametrize("streaming", [True, False])
async def test_relay_forwards_real_body_and_records_concrete_model(tmp_path, monkeypatch, streaming, selected):
    # Exercise the HTTP handler itself. An independent upstream boundary checks
    # exact forwarding, including plugins and both response formats.
    monkeypatch.setenv("LATHE_MONITOR_MODEL", selected)
    payload = {"model": selected,
               "messages": [{"role": "user", "content": "hello"}], "stream": streaming}
    if selected == monitor.ROUTER:
        payload["plugins"] = monitor.PLUGINS
    encoded = json.dumps(payload).encode()
    serving = "provider/concrete" if selected == monitor.ROUTER else selected
    reply = {"id": "gen-test", "model": serving, "usage": {"total_tokens": 42}}
    content = (("data: " + json.dumps(reply) + "\n\ndata: [DONE]\n\n").encode()
               if streaming else json.dumps(reply).encode())

    def upstream(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.content == encoded
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, content=content,
            headers={"Content-Type": "text/event-stream" if streaming else "application/json"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(monitor.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(upstream), **kw))
    trace = monitor.Trace(tmp_path)
    relay = monitor.RecordingRelay(trace, "test-key")
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    async with TestServer(app) as server, ClientSession() as client:
        async with client.post(server.make_url("/v1/chat/completions"), data=encoded,
                               headers={"Authorization": "Bearer test-key"}) as response:
            assert response.status == 200
            assert await response.read() == content
    assert trace.models == {serving}
    assert not relay.evidence_errors
    relay.verify_evidence()
    rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert rows[0]["kind"] == "inference_request"
    assert rows[0]["model"] == selected
    assert any(row["kind"] == "inference" and row["model"] == serving for row in rows)


async def test_direct_relay_rejects_pareto_plugin_before_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("LATHE_MONITOR_MODEL", "z-ai/glm-5.3-flash")
    monkeypatch.setattr(monitor.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Wrong plugin reached provider"))
    relay = monitor.RecordingRelay(monitor.Trace(tmp_path), "test-key")
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    async with TestServer(app) as server, ClientSession() as client:
        response = await client.post(server.make_url("/v1/chat/completions"),
            json={"model": "z-ai/glm-5.3-flash", "plugins": monitor.PLUGINS},
            headers={"Authorization": "Bearer test-key"})
        assert response.status == 400
    assert relay.evidence_errors


@pytest.mark.parametrize("selected", [None, "z-ai/glm-5.3-flash"])
@pytest.mark.parametrize("display_name", [None, "z-ai/glm-5.3-flash"])
async def test_bootstrap_configures_selected_model_without_router_plugin_leak(monkeypatch, selected, display_name):
    if selected:
        monkeypatch.setenv("LATHE_MONITOR_MODEL", selected)
    model = selected or "openrouter/pareto-code"
    alias = "lathe-ci-direct" if selected else "lathe-ci-pareto"
    created = []
    def owui(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        if request.url.path == "/api/v1/auths/signup":
            return httpx.Response(200, json={"role": "admin", "token": "disposable"})
        assert request.headers["Authorization"] == "Bearer disposable"
        if request.url.path == "/openai/config/update":
            config = json.loads(request.content)
            assert config["OPENAI_API_CONFIGS"]["0"]["model_ids"] == [model]
            assert config["OPENAI_API_BASE_URLS"] == ["http://127.0.0.1:9099/v1"]
        elif request.url.path == "/api/v1/models/create":
            config = json.loads(request.content)
            assert config["id"] == alias
            assert config["base_model_id"] == model
            assert config["name"] == (display_name or (f"CI {model}" if selected else "CI Pareto Medium"))
            assert config["params"]["function_calling"] == "native"
            policy = config["params"]["custom_params"]
            assert policy["provider"] == {"data_collection": "deny"}
            if selected:
                assert "plugins" not in policy
            else:
                assert policy["plugins"] == [{"id": "pareto-router", "min_coding_score": 0.5}]
            created.append(config)
        elif request.url.path == "/api/models":
            return httpx.Response(200, json={"data": [{"id": alias}]})
        else:
            pytest.fail(f"Unexpected request {request.url.path}")
        return httpx.Response(200, json={})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(monitor.httpx, "AsyncClient", lambda **kwargs:
        client_type(transport=httpx.MockTransport(owui), **kwargs))
    assert await monitor.bootstrap("http://127.0.0.1:8080", "fixture-key", display_name=display_name) == "disposable"
    assert len(created) == 1


@pytest.mark.parametrize("change", ["plugins", "model", "auth"])
async def test_relay_rejects_lost_policy_before_upstream(tmp_path, monkeypatch, change):
    def forbidden(**kwargs):
        pytest.fail("Invalid request reached the provider boundary")
    monkeypatch.setattr(monitor.httpx, "AsyncClient", forbidden)
    payload = {"model": monitor.ROUTER, "plugins": monitor.PLUGINS}
    headers = {"Authorization": "Bearer test-key"}
    if change == "plugins":
        payload["plugins"] = [{"id": "pareto-router", "min_coding_score": 0.8}]
    elif change == "model":
        payload["model"] = "other/model"
    else:
        headers = {}
    relay = monitor.RecordingRelay(monitor.Trace(tmp_path), "test-key")
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    async with TestServer(app) as server, ClientSession() as client:
        async with client.post(server.make_url("/v1/chat/completions"), json=payload,
                               headers=headers) as response:
            assert response.status == 400
    assert relay.evidence_errors


@pytest.fixture
def suite(monkeypatch):
    import dotenv
    def forbidden_dotenv(*args, **kwargs):
        pytest.fail("Isolated monitor attempted to load local deployment credentials")
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden_dotenv)
    monkeypatch.setenv("LATHE_ISOLATED_CI", "1")
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-this-run")
    monkeypatch.setenv("OWUI_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("OWUI_TOKEN", "setup-token")
    monkeypatch.setenv("OWUI_TEST_TOKEN", "test-token")
    monkeypatch.setenv("OWUI_TEST_EMAIL", "test@lathe.invalid")
    spec = importlib.util.spec_from_file_location("monitor_suite_test", Path(__file__).with_name("test_deployment.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def suite_transport(suite, monkeypatch):
    """Never let a swallowed mock-boundary assertion look like a passing test."""
    client_type = httpx.AsyncClient
    failures = []

    def install(responder):
        async def guarded(request):
            try:
                result = responder(request)
                if hasattr(result, "__await__"):
                    result = await result
                return result
            except BaseException as exc:
                failures.append(exc)
                raise
        monkeypatch.setattr(suite.httpx, "AsyncClient",
            lambda **kw: client_type(transport=httpx.MockTransport(guarded), **kw))

    install(lambda request: pytest.fail(f"Unexpected HTTP: {request.method} {request.url}"))
    yield install
    assert not failures, failures


async def test_cleanup_owns_only_exact_run_and_follows_pages(suite, monkeypatch):
    # Daytona's ListSandboxesResponseDto pages with `nextCursor`; the owned
    # sandbox is a straggler on page 2, behind other runs and production.
    requests = []

    def daytona(request):
        requests.append((request.method, request.url.path, request.url.params.get("cursor")))
        if request.url.path == "/api/sandbox":
            if request.url.params.get("cursor") == "page2":
                return httpx.Response(200, json={"items": [
                    {"id": "owned", "labels": {"lathe-ci-this-run": "monitor@lathe.invalid"}}], "nextCursor": None})
            return httpx.Response(200, json={"items": [
                {"id": "prod", "labels": {"production": "user"}},
                {"id": "other-ci", "labels": {"lathe-ci-other-run": "monitor@lathe.invalid"}},
            ], "nextCursor": "page2"})
        assert request.url.path == "/api/sandbox/owned"
        return httpx.Response(200 if request.method == "DELETE" else 404)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(suite.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(daytona), **kw))
    await suite.cleanup_test_sandboxes()
    assert requests == [("GET", "/api/sandbox", None), ("GET", "/api/sandbox", "page2"),
                        ("DELETE", "/api/sandbox/owned", None), ("GET", "/api/sandbox/owned", None)]


async def test_known_owner_lookup_uses_exact_server_filter_across_pages(suite):
    requests = []
    def daytona(request):
        requests.append(dict(request.url.params))
        assert json.loads(request.url.params["labels"]) == {"lathe-ci-this-run": "test@lathe.invalid"}
        assert request.url.params["includeErroredDeleted"] == "true"
        if request.url.params.get("cursor") == "p2":
            return httpx.Response(200, json={"items": [
                {"id": "owned", "labels": {"lathe-ci-this-run": "test@lathe.invalid", "secret": "x"}}], "nextCursor": None})
        # A server that under-filters: same key, other owner, must be re-checked away.
        return httpx.Response(200, json={"items": [
            {"id": "other-owner", "labels": {"lathe-ci-this-run": "someone@lathe.invalid"}}], "nextCursor": "p2"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(daytona)) as http:
        found = await suite.labelled_sandboxes(http, "lathe-ci-this-run", ["test@lathe.invalid"])
    # IDs and the owning label only; other labels never leave the helper.
    assert found == [{"id": "owned", "labels": {"lathe-ci-this-run": "test@lathe.invalid"}}]
    assert [r.get("cursor") for r in requests] == [None, "p2"]


def endless_pages(requests):
    """Distinct cursors forever: a misbehaving or adversarial listing."""
    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/api/sandbox", "no deletion while listing is unproven"
        n = int(request.url.params.get("cursor", "0"))
        return httpx.Response(200, json={"items": [{"id": f"noise-{n}", "labels": {"other": "x"}}],
                                         "nextCursor": str(n + 1)})
    return respond


async def test_cleanup_fails_closed_on_unbounded_pagination(suite, monkeypatch):
    requests = []
    client_type = httpx.AsyncClient
    monkeypatch.setattr(suite.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(endless_pages(requests)), **kw))
    with pytest.raises(AssertionError, match="exceeded 200 pages; absence not established"):
        await suite.cleanup_test_sandboxes()
    assert len(requests) == suite.LIST_MAX_PAGES and all(r[0] == "GET" for r in requests)


async def test_monitor_cleanup_path_fails_closed_on_unbounded_pagination(monkeypatch):
    # monitor_e2e delegates to the real test_deployment cleanup; its caller turns
    # any exception into cleanup failure, never a reported absence.
    monkeypatch.setenv("LATHE_ISOLATED_CI", "1")
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-this-run")
    monkeypatch.delitem(sys.modules, "test_deployment", raising=False)
    requests = []
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(endless_pages(requests)), **kw))
    with pytest.raises(AssertionError, match="absence not established"):
        await monitor.cleanup_sandboxes()
    assert requests and all(r[0] == "GET" for r in requests)


async def test_listing_fails_closed_at_wall_clock_deadline(suite):
    requests = []
    ticks = iter(range(10**6))
    clock = lambda: next(ticks) * 50.0  # Lab-scoped: each read advances 50 s.
    async with httpx.AsyncClient(transport=httpx.MockTransport(endless_pages(requests))) as http:
        with pytest.raises(AssertionError, match="exceeded 120.0s; absence not established"):
            await suite.labelled_sandboxes(http, "lathe-ci-this-run", clock=clock)
    assert 0 < len(requests) < suite.LIST_MAX_PAGES


async def test_deletion_proof_is_per_id_and_deadline_bounded(suite):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"id": "owned", "state": "started"})
    ticks = iter(range(10**6))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(RuntimeError, match="owned did not finish deletion within 60"):
            await suite.await_deleted(http, "owned", interval=0, clock=lambda: next(ticks))
    assert set(requests) == {"/api/sandbox/owned"}


@pytest.mark.parametrize("states,polls", [
    (["deleted"], 1), (["destroyed"], 1),
    (["deleting", "destroying", "destroyed"], 3), (["destroying", "deleted"], 2),
])
async def test_deletion_proof_waits_out_transitional_states(suite, states, polls):
    # Only a finished state proves deletion; transitional states keep polling.
    replies = iter(states)
    requests = []
    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"id": "owned", "state": next(replies)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await suite.await_deleted(http, "owned", interval=0)
    assert len(requests) == polls


@pytest.mark.parametrize("display_name", ["Lathe Test", "Lathe"])
@pytest.mark.parametrize("preview_only", [False, True])
async def test_staging_deployment_accepts_current_valves_and_sets_expiry(suite, monkeypatch, display_name, preview_only):
    monkeypatch.setattr(suite.sys, "argv", ["test_deployment.py"] + (["--preview-only"] if preview_only else []))
    seen = []
    def owui(request):
        if request.method == "GET":
            return httpx.Response(404)
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json=payload)
    client_type = httpx.AsyncClient
    suite.OWUI_BASE = "http://127.0.0.1:8080"
    monkeypatch.setattr(suite.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(owui), **kw))
    await suite.deploy_staging_tool({"id": "admin"}, {"id": "test-user"}, display_name=display_name)
    assert seen[0]["content"] == Path("lathe.py").read_text()
    assert seen[0]["id"] == "lathe_test"
    assert seen[0]["name"] == display_name
    valves = seen[1]
    assert valves["deployment_label"] == "lathe-ci-this-run"
    # Exercise the actual valve parser: removed valves must not be posted to OWUI.
    from lathe import Tools
    Tools.Valves(**valves)
    assert set(valves) <= set(Tools.Valves.model_fields)
    assert valves["auto_stop_minutes"] == 5
    assert valves["auto_delete_minutes"] == 0
    assert json.loads(valves["sandbox_create_overrides"])["ttlMinutes"] == 30
    assert seen[0]["access_grants"] == []
    assert seen[2] == {"access_grants": [
        {"principal_type": "user", "principal_id": "test-user", "permission": "read"}]}


@pytest.mark.parametrize("copy_tool,copy_args", [
    ("bash", {"command": "cp /source.txt /copy.txt"}),
    ("bash", {"command": "cp source.txt copy.txt"}),
    ("bash", {"command": "cd / && cp source.txt copy.txt && cmp source.txt copy.txt"}),
    ("interpret", {"code": "import shutil; shutil.copyfile('/source.txt', '/copy.txt')"}),
    ("interpret", {"code": "import shutil; shutil.copyfile('source.txt', 'copy.txt')"}),
    ("write", {"path": "/copy.txt", "content": "FRESH_CANARY"}),
])
@pytest.mark.parametrize("fault", [
    None, "no_manual", "no_copy", "no_inspection", "claim_only", "over_budget",
    "failed_manual", "failed_copy", "wrong_destination", "wrong_marker",
    "inspect_before_copy", "copy_before_manual", "combined_copy_and_inspection",
])
def test_journey_oracle_rejects_plausible_false_success(suite, copy_tool, copy_args, fault):
    output = []
    steps = [
        ("lathe", {}, "# Lathe Toolkit – Overview"),
        (copy_tool, copy_args, "finished"),
        ("read", {"path": "/copy.txt"}, "File: /copy.txt\n1: FRESH_CANARY"),
    ]
    if fault == "inspect_before_copy":
        steps[1], steps[2] = steps[2], steps[1]
    if fault == "copy_before_manual":
        steps[0], steps[1] = steps[1], steps[0]
    if fault == "combined_copy_and_inspection":
        steps = [steps[0], ("bash", {"command": "cp /source.txt /copy.txt; cat /copy.txt"}, "FRESH_CANARY")]
    for name, args, text in steps:
        if (fault, name) in (("no_manual", "lathe"), ("no_copy", copy_tool), ("no_inspection", "read")):
            continue
        if (fault, name) in (("failed_manual", "lathe"), ("failed_copy", copy_tool)):
            text = "Error: operation failed"
        if fault == "wrong_destination" and name == "read":
            args = {"path": "/source.txt"}
        if fault == "wrong_marker" and name == "read":
            text = "File: /copy.txt\n1: stale content"
        output.extend([
            {"type": "function_call", "name": name, "call_id": name, "arguments": json.dumps(args)},
            {"type": "function_call_output", "call_id": name, "output": text},
        ])
    if fault == "claim_only":
        output = [{"type": "message", "content": "I consulted the overview, copied, and verified FRESH_CANARY"}]
    if fault == "over_budget":
        output.extend([{"type": "function_call", "name": "bash"}] * 10)
    if fault:
        with pytest.raises(AssertionError):
            suite.verify_journey(output, "/copy.txt", "FRESH_CANARY")
    else:
        suite.verify_journey(output, "/copy.txt", "FRESH_CANARY")


@pytest.mark.parametrize("selected", [None, "z-ai/glm-5.3-flash"])
@pytest.mark.parametrize("iterations", [None, 24])
def test_launcher_tracks_moving_slim_and_exposes_only_loopback(tmp_path, monkeypatch, selected, iterations):
    import subprocess
    commands = []
    if selected:
        monkeypatch.setenv("LATHE_MONITOR_MODEL", selected)
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-run-2")
    monkeypatch.setattr(monitor.httpx, "get", lambda *a, **kw: pytest.fail(
        "moving :slim launch must not resolve a versioned GitHub release"))
    def docker(*args, **kw):
        commands.append(args)
        data = json.dumps([{"RepoDigests": ["ghcr.io/open-webui/open-webui@sha256:abc"],
                            "Id": "sha256:abc", "Architecture": "amd64"}]) if args[0] == "image" else ""
        return subprocess.CompletedProcess(args, 0, data, "")
    monkeypatch.setattr(monitor, "docker", docker)
    metadata = monitor.launch(tmp_path, **({"tool_call_iterations": iterations} if iterations else {}))
    run = commands[-1]
    image = "ghcr.io/open-webui/open-webui:slim"
    assert commands[0] == ("pull", image)
    assert run[-1] == image
    assert metadata["image"] == image
    assert metadata["image_digest"] == "ghcr.io/open-webui/open-webui@sha256:abc"
    assert "owui_release" not in metadata
    assert run[run.index("--publish") + 1] == "127.0.0.1:0:8080"
    assert "LATHE_TEST_DEPLOYMENT_LABEL" in run
    assert f"LATHE_MONITOR_MODEL={selected or 'openrouter/pareto-code'}" in run
    assert metadata["requested_model"] == (selected or "openrouter/pareto-code")
    assert metadata["plugins"] == ([] if selected else monitor.PLUGINS)
    assert f"CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS={iterations or 12}" in run
    assert metadata["tool_call_iterations"] == (iterations or 12)
    assert not any("chat.adamsmith.as" in arg for arg in run)
    assert not any("type=volume" in arg for arg in run)


def test_inner_monitor_invokes_host_identity_contract(monkeypatch):
    import os
    import subprocess

    observed = []

    def docker(*args, **kwargs):
        observed.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(monitor, "docker", docker)
    monitor.run_inside()
    args, kwargs = observed[0]
    assert args[:4] == (
        "exec", "--user", f"{os.getuid()}:{os.getgid()}", monitor.CONTAINER,
    )
    assert args[-4:] == (
        "/monitor/monitor_e2e.py", "--inside", "--artifacts", "/artifacts",
    )
    assert kwargs == {"check": False, "timeout": 1500}


@pytest.mark.parametrize("over_budget", [False, True])
async def test_socket_delivery_preserves_tool_evidence_and_enforces_budget(suite, monkeypatch, over_budget):
    observed = []
    suite.TRACE = lambda kind, **data: observed.append((kind, data))
    suite.OWUI_BASE = "http://127.0.0.1:8080"
    client = suite.OWUIClient()
    output = [{"type": "function_call", "name": "read", "call_id": "one", "arguments": "{}"},
              {"type": "function_call_output", "call_id": "one", "status": "completed", "output": "CANARY"}]
    if over_budget:
        output += [{"type": "function_call", "name": "bash", "call_id": "two"}]

    async def owui(request):
        payload = json.loads(request.content)
        await client.sio.handlers["/"]["*"]("events", {
            "chat_id": payload["chat_id"], "message_id": payload["id"], "data": {
            "type": "chat:completion", "data": {"output": output, "done": not over_budget}}})
        return httpx.Response(200, json={"task_id": "task"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(suite.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(owui), **kw))
    if over_budget:
        with pytest.raises(AssertionError, match="budget"):
            await client.send("inspect", timeout=1, max_calls=1)
    else:
        assert await client.send("inspect", timeout=1, max_calls=1) == output
    assert any(kind == "tool_progress" and "CANARY" in json.dumps(data) for kind, data in observed)
    assert observed[-1][0] == "chat_output"


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_host_stops_tool_execution_before_sandbox_cleanup(tmp_path, monkeypatch, cleanup_fails):
    import subprocess
    calls = []
    (tmp_path / "environment.json").write_text(json.dumps({"deployment_label": "lathe-ci-owned"}))
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-wrong-run")
    def docker(*args, **kwargs):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "https://preview.example/secret", "")
    async def sandboxes():
        import os
        assert os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] == "lathe-ci-owned"
        calls.append("sandbox cleanup")
        if cleanup_fails:
            raise RuntimeError("delete failed")
    monkeypatch.setattr(monitor, "docker", docker)
    monkeypatch.setattr(monitor, "cleanup_sandboxes", sandboxes)
    assert monitor.cleanup(tmp_path) is cleanup_fails
    assert calls == ["inspect", "stop", "logs", "rm", "sandbox cleanup"]
    assert "https://preview.example/secret" not in (tmp_path / "owui.log").read_text()
    assert json.loads((tmp_path / "cleanup.json").read_text())["success"] is not cleanup_fails


def socket_event(payload, kind="chat:completion", detail=None, **overrides):
    return {"chat_id": payload["chat_id"], "message_id": payload["id"],
            "data": {"type": kind, "data": detail or {}}, **overrides}


@pytest.mark.parametrize("foreign", [
    {"chat_id": "other-chat"}, {"message_id": "resumed-message"},
    {"chat_id": "child-chat", "message_id": "child-message"},
    {"chat_id": None, "message_id": None},
])
async def test_socket_filters_before_retaining_printing_tracing_or_completing(
        suite, suite_transport, capsys, foreign):
    suite.VERBOSE = True
    traced = []
    suite.TRACE = lambda kind, **data: traced.append((kind, data))
    client = suite.OWUIClient()
    emit = client.sio.handlers["/"]["*"]
    output = [{"type": "function_call", "name": "read", "call_id": "one"},
              {"type": "function_call_output", "call_id": "one", "status": "completed", "output": "OWN"}]

    async def owui(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        await emit("events", socket_event(payload, detail={"output": output}))
        for kind, detail in [
            ("chat:completion", {"output": [{"type": "function_call", "call_id": str(i),
                                             "name": "FOREIGN_PRIVATE"} for i in range(3)], "done": True}),
            ("status", {"description": "FOREIGN_PRIVATE"}),
            ("chat:error", {"error": "FOREIGN_PRIVATE"}),
            ("chat:reload", {"description": "FOREIGN_PRIVATE"}),
        ]:
            await emit("events", socket_event(payload, kind, detail, **foreign))
            assert client.last_output == output
            assert not client.done.is_set()
            assert client.budget_error is None
        # Non-envelope events are never request-scoped, even if they mimic IDs.
        await emit("other-event", socket_event(payload, detail={"output": "FOREIGN_PRIVATE"}))
        await emit("events", socket_event(payload, "status", {"description": "own status"}))
        await emit("events", socket_event(payload, detail={"done": True}))
        return httpx.Response(200, json={"task_id": "owned-task"})

    suite_transport(owui)
    assert await client.send("inspect", timeout=1, max_calls=1) == output
    assert client.active_request is None
    retained = json.dumps([client.events, traced]) + capsys.readouterr().out
    assert "FOREIGN_PRIVATE" not in retained
    end = next(data for kind, data in traced if kind == "request_end")
    assert end["completed_calls"] == 1
    assert end["http_status"] == 200 and end["task_id"] == "owned-task"
    assert end["last_status"]["data"]["description"] == "own status"


@pytest.mark.parametrize("first_failure", ["timeout", "http", "success", "cancel"])
async def test_socket_late_events_ignored_between_and_during_requests(
        suite, suite_transport, capsys, first_failure):
    suite.VERBOSE = True
    traced = []
    suite.TRACE = lambda kind, **data: traced.append((kind, data))
    client = suite.OWUIClient()
    emit = client.sio.handlers["/"]["*"]
    requests = []
    accepted = asyncio.Event()
    output = [{"type": "message", "content": "OWN"}]

    async def owui(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            await emit("events", socket_event(payload, "status", {"description": "waiting for tool"}))
            accepted.set()
            if first_failure == "http":
                return httpx.Response(503)
            if first_failure == "success":
                await emit("events", socket_event(payload, detail={"done": True}))
        else:
            await emit("events", socket_event(requests[0], detail={"output": "LATE_PRIVATE", "done": True}))
            assert client.last_output == [] and not client.done.is_set()
            await emit("events", socket_event(payload, detail={"output": output, "done": True}))
        return httpx.Response(200, json={"task_id": "owned-task"})

    suite_transport(owui)
    if first_failure == "cancel":
        task = asyncio.create_task(client.send("first", timeout=1))
        await asyncio.wait_for(accepted.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif first_failure == "success":
        await client.send("first", timeout=1)
    else:
        with pytest.raises(TimeoutError if first_failure == "timeout" else RuntimeError):
            await client.send("first", timeout=.02)
    assert client.active_request is None
    before = json.dumps([client.events, traced])
    await emit("events", socket_event(requests[0], detail={"output": "LATE_PRIVATE", "done": True}))
    assert json.dumps([client.events, traced]) == before
    assert await client.send("second", timeout=1) == output
    assert "LATE_PRIVATE" not in json.dumps([client.events, traced]) + capsys.readouterr().out
    if first_failure == "timeout":
        end = next(data for kind, data in traced if kind == "request_end")
        assert not end["done"] and end["elapsed"] >= .01
        assert end["task_id"] == "owned-task"
        assert end["last_status"]["data"]["description"] == "waiting for tool"


@pytest.mark.parametrize("case", ["valid", "same-user", "test-admin", "pending", "wrong-email", "missing", "monitor"])
async def test_completion_identity_preflight(suite, suite_transport, case, monkeypatch):
    admin = {"id": "admin", "role": "admin", "email": "operator@example.com"}
    user = {"id": "synthetic", "role": "user", "email": "test@lathe.invalid"}
    if case == "same-user":
        user["id"] = "admin"
    if case in ("test-admin", "pending"):
        user["role"] = "admin" if case == "test-admin" else "pending"
    if case == "wrong-email":
        user["email"] = "someone@example.com"
    if case == "missing":
        suite.OWUI_TEST_TOKEN = ""
    if case == "monitor":
        user = {**admin, "email": "monitor@lathe.invalid"}
        suite.OWUI_TEST_EMAIL = user["email"]
    calls = []

    def owui(request):
        assert request.url.path == "/api/v1/auths/"
        calls.append(request.headers["Authorization"])
        return httpx.Response(200, json=admin if calls[-1] == "Bearer setup-token" else user)

    suite_transport(owui)
    if case == "valid":
        assert await suite.check_test_identity() == (admin, user)
    else:
        with pytest.raises(AssertionError):
            await suite.check_test_identity()
    assert calls == ([] if case == "missing" else ["Bearer setup-token", "Bearer test-token"])


@pytest.mark.parametrize("grants,tools,valid", [
    ([{"principal_type": "user", "principal_id": "test", "permission": "read"}], ["lathe_test"], True),
    ([{"principal_type": "user", "principal_id": "*", "permission": "read"}], ["lathe_test"], False),
    ([{"principal_type": "group", "principal_id": "test", "permission": "read"}], ["lathe_test"], False),
    ([{"principal_type": "user", "principal_id": "test", "permission": "write"}], ["lathe_test"], False),
    ([{"principal_type": "user", "principal_id": "test", "permission": "read"}], ["lathe_test", "production"], False),
    ([{"principal_type": "user", "principal_id": "test", "permission": "read"}], [], False),
    (None, ["lathe_test"], False),
])
async def test_staging_access_is_private_and_completion_account_is_restricted(
        suite, suite_transport, grants, tools, valid):
    def owui(request):
        if request.url.path == "/api/v1/tools/id/lathe_test":
            assert request.headers["Authorization"] == "Bearer setup-token"
            return httpx.Response(200, json={"user_id": "admin", "access_grants": grants})
        assert request.url.path == "/api/v1/tools/"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json=[{"id": tool} for tool in tools])

    suite_transport(owui)
    if valid:
        await suite.check_staging_access({"id": "admin"}, {"id": "test"})
    else:
        with pytest.raises(AssertionError):
            await suite.check_staging_access({"id": "admin"}, {"id": "test"})


async def test_socket_auth_uses_only_completion_identity(suite, monkeypatch):
    client = suite.OWUIClient()
    calls = []

    async def connect(*args, **kwargs):
        calls.append(kwargs["auth"])

    async def emit(event, data):
        calls.append(data["auth"])

    async def sleep(seconds):
        pass

    monkeypatch.setattr(client.sio, "connect", connect)
    monkeypatch.setattr(client.sio, "emit", emit)
    # Replace only this suite's connection settle delay, not global asyncio.
    from types import SimpleNamespace
    monkeypatch.setattr(suite, "asyncio", SimpleNamespace(sleep=sleep))
    await client.connect()
    assert calls == [{"token": "test-token"}, {"token": "test-token"}]


async def test_monitor_provisions_non_admin_completion_identity_and_model_access(suite_transport, monkeypatch):
    calls = []

    def owui(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/auths/add":
            assert request.headers["Authorization"] == "Bearer setup-token"
            body = json.loads(request.content)
            assert body["role"] == "user" and body["email"] == "completion@lathe.invalid"
            assert len(body["password"]) >= 32
            return httpx.Response(200, json={"id": "completion-user", "role": "user",
                "token": "completion-token", "email": body["email"]})
        if request.url.path == "/api/v1/models/model/access/update":
            assert request.headers["Authorization"] == "Bearer setup-token"
            assert json.loads(request.content) == {"id": (monitor.requested_model() if len(calls) == 2
                                                        else monitor.workspace_model()), "access_grants": [
                {"principal_type": "user", "principal_id": "completion-user", "permission": "read"}]}
            return httpx.Response(200)
        assert request.url.path == "/api/models"
        assert request.headers["Authorization"] == "Bearer completion-token"
        return httpx.Response(200, json={"data": [{"id": monitor.workspace_model()}]})

    suite_transport(owui)
    account = await monitor.completion_account("http://127.0.0.1:8080", "setup-token")
    assert account["token"] == "completion-token"
    assert calls == ["/api/v1/auths/add", "/api/v1/models/model/access/update",
                     "/api/v1/models/model/access/update", "/api/models"]


@pytest.mark.parametrize("owner", ["admin", "someone-else"])
async def test_staging_update_revokes_access_before_replacing_source_or_valves(suite, suite_transport, owner):
    calls = []

    def owui(request):
        assert request.headers["Authorization"] == "Bearer setup-token"
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={"user_id": owner})

    suite_transport(owui)
    if owner != "admin":
        with pytest.raises(AssertionError, match="owned by another"):
            await suite.deploy_staging_tool({"id": "admin"}, {"id": "test"})
        with pytest.raises(AssertionError, match="owned by another"):
            await suite.delete_staging_tool({"id": "admin"})
        assert all(method == "GET" for method, _, _ in calls)
    else:
        await suite.deploy_staging_tool({"id": "admin"}, {"id": "test"})
        assert [(method, path.rsplit("/", 1)[-1]) for method, path, _ in calls] == [
            ("GET", "lathe_test"), ("POST", "update"), ("POST", "update"),
            ("POST", "update"), ("POST", "update")]
        assert calls[1][1].endswith("/access/update") and calls[1][2] == {"access_grants": []}
        assert calls[2][2]["content"] == Path("lathe.py").read_text()
        assert calls[3][1].endswith("/valves/update")
        assert calls[4][1].endswith("/access/update")
        assert calls[4][2] == {"access_grants": [
            {"principal_type": "user", "principal_id": "test", "permission": "read"}]}


async def test_main_refuses_missing_completion_credentials_before_http_or_cleanup(suite, suite_transport, monkeypatch, capsys):
    suite.OWUI_TEST_TOKEN = ""
    suite.MODEL = "test-model"
    suite.DAYTONA_API_KEY = "daytona-test"
    monkeypatch.setattr(suite.sys, "argv", ["test_deployment.py"])
    assert await suite.main() == 2
    assert "OWUI_TEST_TOKEN" in capsys.readouterr().out


@pytest.mark.parametrize("owner", ["admin", "someone-else"])
async def test_no_deploy_cleans_owned_staging_after_access_failure_but_not_foreign_toolkit(
        suite, suite_transport, monkeypatch, owner):
    suite.MODEL = "test-model"
    suite.DAYTONA_API_KEY = "daytona-test"
    monkeypatch.setattr(suite.sys, "argv", ["test_deployment.py", "--no-deploy"])
    deletions = []

    def owui(request):
        if request.method == "DELETE":
            deletions.append(request.url.path)
            return httpx.Response(200, json=True)
        assert request.method == "GET"
        if request.url.path == "/api/v1/auths/":
            admin = request.headers["Authorization"] == "Bearer setup-token"
            return httpx.Response(200, json={"id": "admin" if admin else "test",
                "role": "admin" if admin else "user", "email": "test@lathe.invalid"})
        if request.url.path == "/api/v1/tools/id/lathe_test":
            return httpx.Response(200, json={"user_id": owner, "access_grants": [
                {"principal_type": "user", "principal_id": "*", "permission": "read"}]})
        if request.url.path == "/api/v1/tools/":
            return httpx.Response(200, json=[{"id": "lathe_test"}])
        if request.url.path == "/api/sandbox":
            return httpx.Response(200, json=[{"id": "owned-sandbox",
                "labels": {suite.DEPLOYMENT_LABEL: "test@lathe.invalid"}}])
        assert request.url.path == "/api/sandbox/owned-sandbox"
        return httpx.Response(404)

    suite_transport(owui)
    if owner == "admin":
        with pytest.raises(AssertionError, match="grant read access only"):
            await suite.main()
        assert deletions == ["/api/sandbox/owned-sandbox", "/api/v1/tools/id/lathe_test/delete"]
    else:
        assert await suite.main() == 2
        assert not deletions

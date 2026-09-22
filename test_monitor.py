"""Offline protection for the monitor's isolation and evidence boundaries."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import monitor_e2e as monitor


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
    trace("scenario_start", name="delegate")
    trace("scenario_fail", error="unusual-provider-key generated-password")
    trace("scenario_start", name="cleanup")
    trace("fatal", error="another failure")
    assert trace.first_failure == "delegate"
    assert "unusual-provider-key" not in trace.path.read_text()
    assert "generated-password" not in trace.path.read_text()


@pytest.mark.parametrize("streaming", [True, False])
async def test_relay_forwards_real_body_and_records_concrete_model(tmp_path, monkeypatch, streaming):
    # Exercise the HTTP handler itself. An independent upstream boundary checks
    # exact forwarding, including plugins and the delegate's non-streaming path.
    payload = {"model": monitor.ROUTER, "plugins": monitor.PLUGINS,
               "messages": [{"role": "user", "content": "hello"}], "stream": streaming}
    encoded = json.dumps(payload).encode()
    reply = {"id": "gen-test", "model": "provider/concrete", "usage": {"total_tokens": 42}}
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
    assert trace.models == {"provider/concrete"}
    assert not relay.evidence_errors


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
    spec = importlib.util.spec_from_file_location("monitor_suite_test", Path(__file__).with_name("test_deployment.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_cleanup_owns_only_exact_run_and_follows_pages(suite, monkeypatch):
    requests = []

    def daytona(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/sandbox":
            if request.url.params.get("cursor") == "page2":
                return httpx.Response(200, json={"items": [
                    {"id": "owned", "labels": {"lathe-ci-this-run": "monitor@lathe.invalid"}}]})
            return httpx.Response(200, json={"items": [
                {"id": "prod", "labels": {"production": "user"}},
                {"id": "other-ci", "labels": {"lathe-ci-other-run": "monitor@lathe.invalid"}},
            ], "cursor": "page2"})
        assert request.url.path == "/api/sandbox/owned"
        return httpx.Response(200 if request.method == "DELETE" else 404)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(suite.httpx, "AsyncClient",
                        lambda **kw: client_type(transport=httpx.MockTransport(daytona), **kw))
    await suite.cleanup_test_sandboxes()
    assert requests == [("GET", "/api/sandbox"), ("GET", "/api/sandbox"),
                        ("DELETE", "/api/sandbox/owned"), ("GET", "/api/sandbox/owned")]


async def test_staging_deployment_disables_volumes_and_sets_expiry(suite, monkeypatch):
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
    await suite.deploy_staging_tool()
    assert seen[0]["content"] == Path("lathe.py").read_text()
    valves = seen[1]
    assert valves["deployment_label"] == "lathe-ci-this-run"
    assert valves["persistent_volume"] is False
    assert valves["auto_stop_minutes"] == 5
    assert valves["auto_delete_minutes"] == 0
    assert json.loads(valves["sandbox_create_overrides"])["ttlMinutes"] == 30


@pytest.mark.parametrize("fault", [None, "no_manual", "no_delegate", "no_inspection", "claim_only", "over_budget"])
def test_journey_oracle_rejects_plausible_false_success(suite, fault):
    output = []
    for name, args, text in [
        ("lathe", {}, "# Lathe Toolkit – Overview"),
        ("delegate", {"task": "copy the source"}, "finished"),
        ("read", {"path": "/copy.txt"}, "File: /copy.txt\n1: FRESH_CANARY"),
    ]:
        if (fault, name) in (("no_manual", "lathe"), ("no_delegate", "delegate"), ("no_inspection", "read")):
            continue
        output.extend([
            {"type": "function_call", "name": name, "call_id": name, "arguments": json.dumps(args)},
            {"type": "function_call_output", "call_id": name, "output": text},
        ])
    if fault == "claim_only":
        output = [{"type": "message", "content": "I consulted the overview, delegated, and verified FRESH_CANARY"}]
    if fault == "over_budget":
        output.extend([{"type": "function_call", "name": "bash"}] * 10)
    if fault:
        with pytest.raises(AssertionError):
            suite.verify_journey(output, "/copy.txt", "FRESH_CANARY")
    else:
        suite.verify_journey(output, "/copy.txt", "FRESH_CANARY")


def test_launcher_pins_digest_and_exposes_only_loopback(tmp_path, monkeypatch):
    import subprocess
    commands = []
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-run-2")
    monkeypatch.setattr(monitor.httpx, "get", lambda *a, **kw: httpx.Response(200,
        request=httpx.Request("GET", "https://api.github.com/releases/latest"),
        json={"tag_name": "v1.2.3", "prerelease": False, "draft": False}))
    def docker(*args, **kw):
        commands.append(args)
        data = json.dumps([{"RepoDigests": ["ghcr.io/open-webui/open-webui@sha256:abc"],
                            "Id": "sha256:abc", "Architecture": "amd64"}]) if args[0] == "image" else ""
        return subprocess.CompletedProcess(args, 0, data, "")
    monkeypatch.setattr(monitor, "docker", docker)
    monitor.launch(tmp_path)
    run = commands[-1]
    assert run[-1] == "ghcr.io/open-webui/open-webui@sha256:abc"
    assert run[run.index("--publish") + 1] == "127.0.0.1:0:8080"
    assert "LATHE_TEST_DEPLOYMENT_LABEL" in run
    assert not any("chat.adamsmith.as" in arg for arg in run)
    assert not any("type=volume" in arg for arg in run)


def test_inner_monitor_writes_artifacts_as_host_user(monkeypatch):
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
        await client.sio.handlers["/"]["*"]("events", {"data": {
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

"""Offline checks for isolated visualization demo plumbing and independent reads."""

import base64
import hashlib
import json
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
import pytest

import demo_local as demo


@pytest.mark.parametrize("missing", ["No such object: fixture", "error: no such object: fixture", "Cannot connect to Docker daemon"])
def test_demo_preflight_accepts_missing_resources_but_not_daemon_failure(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(sys, "argv", ["demo_local.py", "--artifacts", str(tmp_path / "evidence")])
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setenv("DAYTONA_API_KEY", "fixture")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture")
    monkeypatch.setattr(demo.signal, "signal", lambda *args: None)
    seen = []

    def docker(*args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 1, "", missing)

    def infrastructure(path):
        raise RuntimeError("preflight passed")

    monkeypatch.setattr(demo.monitor, "docker", docker)
    monkeypatch.setattr(demo, "prepare_infrastructure", infrastructure)
    monkeypatch.setattr(demo, "cleanup_demo", lambda directory: False)
    error = "Cannot establish demo isolation" if "daemon" in missing else "preflight passed"
    with pytest.raises(RuntimeError, match=error):
        demo.main()
    assert len(seen) == (1 if "daemon" in missing else 4)


@pytest.mark.parametrize("fault", [None, "daemon", "removal", "network", "sandbox"])
def test_demo_cleanup_attempts_every_resource_and_fails_closed(tmp_path, monkeypatch, fault):
    (tmp_path / "environment.json").write_text(json.dumps({"deployment_label": "lathe-ci-demo-fixture"}))
    seen = []
    (tmp_path / f"{demo.WORKER}.log").write_text("original startup evidence")
    def docker(*args, **kwargs):
        seen.append(args)
        if fault == "daemon":
            return subprocess.CompletedProcess(args, 1, "", "Cannot connect to Docker daemon")
        if fault == "removal" and args[0] == "rm":
            return subprocess.CompletedProcess(args, 1, "", "permission denied")
        if fault == "network" and args[:2] == ("network", "rm"):
            return subprocess.CompletedProcess(args, 1, "", "network has active endpoints")
        # Explicitly missing resources are expected on repeated cleanup.
        message = "network demo not found" if args[0] == "network" else "No such container: demo"
        return subprocess.CompletedProcess(args, 1, "", message)
    monkeypatch.setattr(demo.monitor, "docker", docker)
    monkeypatch.setattr(demo.monitor, "cleanup", lambda directory: fault == "sandbox")
    assert demo.cleanup_demo(tmp_path) == bool(fault)
    assert ("rm", "-f", "-v", demo.TLS) in seen
    assert ("rm", "-f", "-v", demo.WORKER) in seen
    assert ("network", "rm", demo.NETWORK) in seen
    assert json.loads((tmp_path / "demo-cleanup.json").read_text())["success"] == (not fault)
    assert (tmp_path / f"{demo.WORKER}.log").read_text() == "original startup evidence"


@pytest.mark.parametrize("fault", [None, "capture", "cleanup", "model", "models", "requests", "evidence", "files", "egress"])
def test_publication_qualification_requires_independent_evidence(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    out = tmp_path / "demo-video/out"
    out.mkdir(parents=True)
    environment = {"deployment_label": "lathe-ci-demo-fixture", "git_sha": "fixture-sha",
                   "requested_model": "z-ai/glm-5.3-flash"}
    routing = {"requested_model": "z-ai/glm-5.3-flash", "serving_models": ["z-ai/glm-5.3-flash"],
               "requests": 3, "evidence_errors": []}
    capture = {"status": "accepted"}
    cleanup = {"success": True}
    files = {"verified": True, "networkBlockAll": True}
    if fault == "capture":
        capture["status"] = "rejected"
    if fault == "cleanup":
        cleanup["success"] = False
    if fault == "model":
        routing["requested_model"] = "openrouter/pareto-code"
    if fault == "models":
        routing["serving_models"] = []
    if fault == "requests":
        routing["requests"] = 0
    if fault == "evidence":
        routing["evidence_errors"] = ["routing contract"]
    if fault == "files":
        files["verified"] = False
    if fault == "egress":
        files["networkBlockAll"] = False
    for name, value in [("environment.json", environment), ("demo-routing.json", routing), ("demo-cleanup.json", cleanup)]:
        (tmp_path / name).write_text(json.dumps(value))
    (out / "capture-report.json").write_text(json.dumps(capture))
    (tmp_path / "independent-files.json").write_text(json.dumps(files))
    (out / "qualification.json").write_text('{"accepted": true}')  # stale prior success
    if fault:
        with pytest.raises(RuntimeError, match="qualification failed"):
            demo.qualify(tmp_path)
        assert not (out / "qualification.json").exists()
    else:
        demo.qualify(tmp_path)
        result = json.loads((out / "qualification.json").read_text())
        assert result["accepted"] is True
        assert result["git_sha"] == "fixture-sha"
        assert result["requested_model"] == "z-ai/glm-5.3-flash"
        assert result["serving_models"] == ["z-ai/glm-5.3-flash"]


def test_capture_environment_does_not_inherit_hosted_targets_or_server_keys(monkeypatch):
    for key in ("DEMO_OWUI_URL", "DEMO_PASSKEY", "DEMO_LOCAL_TOKEN", "OWUI_URL", "OWUI_TOKEN",
                "DAYTONA_API_KEY", "OPENROUTER_API_KEY", "LATHE_PREVIEW_WRAPPER_KEY", "LATHE_DEMO_IDP_SECRET"):
        monkeypatch.setenv(key, "production-secret")
    monkeypatch.setenv("DEMO_SKIP_PREWARM", "1")
    env = demo.capture_environment("disposable-token", "12345")
    assert env["DEMO_OWUI_URL"] == "http://127.0.0.1:12345"
    assert env["DEMO_LOCAL_TOKEN"] == "disposable-token"
    assert env["DEMO_MODEL"] == demo.monitor.workspace_model()
    assert env["DEMO_PROXY_DOMAINS"] == demo.ZONE
    assert "DEMO_SKIP_PREWARM" not in env
    assert "production-secret" not in env.values()


@pytest.mark.parametrize("fault", [None, "verifier", "redirect", "secret", "expired"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_identity_code_exchange_and_verified_user(fault, method):
    verifier = "fixture-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    callback = f"https://lathe-private-abc.{demo.ZONE}/_lathe/auth/callback"
    query = {"client_id": "lathe-demo", "response_type": "code", "state": "worker-state",
             "redirect_uri": callback, "code_challenge": challenge, "code_challenge_method": "S256"}
    now = [100.0]
    async with TestServer(demo.demo_identity_app("fixture-secret", clock=lambda: now[0])) as server, ClientSession() as client:
        discovery = await (await client.get(server.make_url("/demo-idp/.well-known/openid-configuration"))).json()
        assert discovery["issuer"] == demo.ISSUER
        assert discovery["authorization_endpoint"] == f"https://{demo.ZONE}/demo-idp/authorize"
        response = await client.request(method, server.make_url("/demo-idp/authorize"), params=query, allow_redirects=False)
        assert response.status == 302
        returned = parse_qs(urlsplit(response.headers["Location"]).query)
        assert returned["state"] == [query["state"]]
        form = {"client_id": "lathe-demo", "client_secret": "fixture-secret", "grant_type": "authorization_code",
                "code": returned["code"][0], "redirect_uri": callback, "code_verifier": verifier}
        if fault == "verifier":
            form["code_verifier"] = "wrong"
        if fault == "redirect":
            form["redirect_uri"] = "https://other.invalid/callback"
        if fault == "secret":
            form["client_secret"] = "wrong"
        if fault == "expired":
            now[0] += 61
        exchange = await client.post(server.make_url("/demo-idp/token"), data=form)
        if fault:
            assert exchange.status in (400, 401)
            return
        assert exchange.status == 200
        access = (await exchange.json())["access_token"]
        replay = await client.post(server.make_url("/demo-idp/token"), data=form)
        assert replay.status == 400
        assert (await client.get(server.make_url("/demo-idp/userinfo"))).status == 401
        info = await client.get(server.make_url("/demo-idp/userinfo"), headers={"Authorization": "Bearer " + access})
        assert await info.json() == {"sub": "demo-monitor", "email": "monitor@lathe.invalid", "email_verified": True}


@pytest.mark.parametrize("callback", ["https://evil.invalid/_lathe/auth/callback",
    f"http://private.{demo.ZONE}/_lathe/auth/callback",
    f"https://private.{demo.ZONE}/wrong", f"https://private.{demo.ZONE}/_lathe/auth/callback?evil=1",
    f"https://evil.invalid\\.{demo.ZONE}/_lathe/auth/callback",
    f"https://evil.invalid%2f.{demo.ZONE}/_lathe/auth/callback",
    f"https://private.{demo.ZONE}:443/_lathe/auth/callback",
    f"https://user@private.{demo.ZONE}/_lathe/auth/callback"])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_identity_rejects_unbound_callbacks(callback, method):
    async with TestServer(demo.demo_identity_app("secret")) as server, ClientSession() as client:
        response = await client.request(method, server.make_url("/demo-idp/authorize"), params={
            "client_id": "lathe-demo", "response_type": "code", "state": "state",
            "redirect_uri": callback, "code_challenge": "challenge", "code_challenge_method": "S256"},
            allow_redirects=False)
        assert response.status == 400


@pytest.mark.parametrize("port", [443, 18443])
def test_infrastructure_uses_real_worker_private_auth_and_loopback_tls(tmp_path, monkeypatch, port):
    monkeypatch.setenv("DEMO_TLS_PORT", str(port))
    calls = []
    real_run = subprocess.run
    def openssl(command, **kwargs):
        assert command[0] == "openssl"
        (tmp_path / "cert.pem").write_text("fixture certificate")
        (tmp_path / "key.pem").write_text("fixture key")
    monkeypatch.setattr(demo.subprocess, "run", openssl)
    monkeypatch.setattr(demo.monitor, "docker", lambda *args: calls.append(args))
    monkeypatch.setattr(demo.httpx, "get", lambda *args, **kwargs:
        demo.httpx.Response(200, text="preview wrapper"))
    monkeypatch.setattr(demo.monitor, "SECRETS", [])
    monkeypatch.setenv("LATHE_DEMO_IDP_SECRET", "old")
    registration = demo.prepare_infrastructure(tmp_path)
    assert (tmp_path / "worker.mjs").read_bytes() == (demo.ROOT / "preview-wrapper/src/index.js").read_bytes()
    variables = dict(line.split("=", 1) for line in (tmp_path / ".dev.vars").read_text().splitlines())
    assert variables["REGISTER_TOKEN"] == registration
    assert (tmp_path / ".dev.vars").stat().st_mode & 0o777 == 0o600
    assert variables["OIDC_ISSUER"] == demo.ISSUER
    assert variables["OIDC_CLIENT_SECRET"] == demo.os.environ["LATHE_DEMO_IDP_SECRET"]
    assert len(base64.b64decode(variables["REGISTRATION_ENCRYPTION_KEY"])) == 32
    assert json.loads((tmp_path / "wrangler.json").read_text())["vars"]["ZONE"] == demo.ZONE
    nginx = (tmp_path / "nginx.conf").read_text()
    assert f"set $identity_upstream http://{demo.monitor.CONTAINER}:9100;" in nginx
    assert "resolver 127.0.0.11" in nginx
    assert "proxy_pass $identity_upstream$request_uri;" in nginx
    assert any(f"127.0.0.1:{port}:443" in args for args in calls)
    worker = next(args for args in calls if args[:4] == ("run", "-d", "--name", demo.WORKER))
    assert f"LOCAL_UID={demo.os.getuid()}" in worker
    assert f"LOCAL_GID={demo.os.getgid()}" in worker
    # Execute the shipped startup shell with process boundaries replaced, not
    # apt/network/root privileges. Observe the requested identity and npm env.
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, script in {
        "apt-get": "#!/bin/sh\nexit 0\n",
        "setpriv": '#!/bin/sh\nprintf "%s\\n" "$@" > "$DEMO_TEST_PRIVILEGES"\nshift 5\nexec "$@"\n',
        "npx": '#!/bin/sh\ntest "$HOME" = /tmp && test "$NPM_CONFIG_CACHE" = /tmp/npm-cache || exit 1\n'
               'mkdir -p .wrangler/tmp\nprintf "%s\\n" "$@" > worker-arguments\n',
    }.items():
        binary = binaries / name
        binary.write_text(script)
        binary.chmod(0o755)
    env = {**demo.os.environ, "PATH": str(binaries) + ":" + demo.os.environ["PATH"],
           "LOCAL_UID": str(demo.os.getuid()), "LOCAL_GID": str(demo.os.getgid()),
           "DEMO_TEST_PRIVILEGES": str(tmp_path / "privileges")}
    real_run(["sh", "-c", worker[-1]], cwd=tmp_path, env=env, check=True)
    assert (tmp_path / "privileges").read_text().splitlines()[:5] == [
        "--reuid", str(demo.os.getuid()), "--regid", str(demo.os.getgid()), "--clear-groups"]
    assert (tmp_path / ".wrangler/tmp").is_dir()
    assert "dev" in (tmp_path / "worker-arguments").read_text().splitlines()


@pytest.mark.parametrize("stale_cache", [False, True])
async def test_setup_preserves_provider_and_lifecycle_policy(stale_cache):
    model = {"id": demo.monitor.workspace_model(), "base_model_id": "provider/model", "name": "Demo",
             "params": {"function_calling": "native", "max_tokens": 4096,
                        "custom_params": {"provider": {"data_collection": "deny"}}},
             "meta": {"capabilities": {"builtin_tools": False, "vision": True}}}
    valves = {"sandbox_create_overrides": '{"ttlMinutes":30}', "auto_stop_minutes": 5,
              "auto_delete_minutes": 0, "deployment_label": "lathe-ci-demo-fixture",
              "preview_wrapper_key": "fixture-secret"}
    seen = []
    def respond(request):
        nonlocal model, valves
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, dict(request.url.params), body))
        if request.url.path == "/api/models":
            effective = json.loads(json.dumps(model))
            if stale_cache and request.url.params.get("refresh") == "true":
                effective["meta"]["capabilities"]["builtin_tools"] = True
            return demo.httpx.Response(200, json={"data": [{"id": model["id"], "info": effective}]})
        if request.url.path == "/api/v1/models/model/update":
            model = body
            return demo.httpx.Response(200, json=model)
        if request.url.path == "/api/v1/tools/id/lathe_test/valves":
            return demo.httpx.Response(200, json=valves)
        if request.url.path == "/api/v1/tools/id/lathe_test/valves/update":
            valves = body
            return demo.httpx.Response(200, json=valves)
        raise AssertionError(request.url)
    async with demo.httpx.AsyncClient(base_url="http://fixture", transport=demo.httpx.MockTransport(respond)) as api:
        if stale_cache:
            with pytest.raises(RuntimeError, match="effective model"):
                await demo.configure_demo(api)
            assert not any(path.endswith("valves/update") for _, path, _, _ in seen)
            return
        await demo.configure_demo(api)
    assert model["params"]["custom_params"] == {"provider": demo.PROVIDER_POLICY, "max_tokens": demo.MAX_OUTPUT_TOKENS}
    assert model["params"]["function_calling"] == "native"
    assert model["meta"]["capabilities"]["vision"] is True
    assert model["meta"]["capabilities"]["builtin_tools"] is False
    assert json.loads(valves["sandbox_create_overrides"]) == {"ttlMinutes": 30, "networkBlockAll": True}
    assert valves["auto_stop_minutes"] == 5 and valves["auto_delete_minutes"] == 0
    assert valves["preview_wrapper_key"] == "fixture-secret"
    assert any(params.get("refresh") == "true" for _, _, params, _ in seen)


@pytest.mark.parametrize("fault", [None, "egress", "volume", "identity", "html", "external"])
async def test_independent_downloads_verify_policy_and_final_artifacts(tmp_path, monkeypatch, fault):
    original_root = demo.ROOT
    (tmp_path / "demo-video/out").mkdir(parents=True)
    (tmp_path / "demo-video/repair-log.csv").write_bytes((original_root / "demo-video/repair-log.csv").read_bytes())
    (tmp_path / "demo-video/out/artifact.json").write_text('{"path":"/workspace/impact.html"}')
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setenv("LATHE_TEST_DEPLOYMENT_LABEL", "lathe-ci-demo-fixture")
    sandbox = {"id": "sandbox-1", "labels": {"lathe-ci-demo-fixture": "monitor@lathe.invalid"},
               "networkBlockAll": True, "volumes": []}
    files = {"/workspace/impact.html": '<h1>Repair café</h1><p>12 items, 7 repaired</p>' + '<p>Volunteer impact</p>' * 30}
    if fault == "egress":
        sandbox["networkBlockAll"] = False
    if fault == "volume":
        sandbox["volumes"] = [{"id": "other"}]
    if fault == "identity":
        sandbox["labels"] = {"production": "monitor@lathe.invalid"}
    if fault == "html":
        files["/workspace/impact.html"] = "Assistant says everything worked."
    if fault == "external":
        files["/workspace/impact.html"] += '<script src="https://external.invalid/chart.js"></script>'
    downloads = []
    def respond(request):
        if request.url.path == "/api/sandbox":
            assert json.loads(request.url.params["labels"]) == {"lathe-ci-demo-fixture": "monitor@lathe.invalid"}
            return demo.httpx.Response(200, json={"items": [sandbox]})
        assert request.url.path == "/toolbox/sandbox-1/files/download"
        path = request.url.params["path"]
        downloads.append(path)
        return demo.httpx.Response(200, text=files[path])
    async with demo.httpx.AsyncClient(transport=demo.httpx.MockTransport(respond)) as http:
        if fault:
            with pytest.raises(RuntimeError):
                await demo.verify_demo_files(http, tmp_path)
            assert not (tmp_path / "independent-files.json").exists()
        else:
            await demo.verify_demo_files(http, tmp_path)
            evidence = json.loads((tmp_path / "independent-files.json").read_text())
            assert evidence["verified"] is True
            assert evidence["files"] == files
            assert len(downloads) == 1
            assert evidence["reference_summary"] == {"items": 12, "repaired": 7, "minutes": 330}


@pytest.mark.parametrize("fault", [None, "zdr", "deny", "price", "tokens", "budget"])
async def test_live_relay_rejects_privacy_or_budget_drift_before_upstream(monkeypatch, fault):
    from aiohttp import web
    upstream = []
    def trace(*args, **kwargs):
        pass
    async def forward(self, request):
        upstream.append(await request.json())
        self.requests += 1
        return web.json_response({"forwarded": True})
    monkeypatch.setattr(demo.monitor.RecordingRelay, "handle", forward)
    relay = demo.DemoRelay(trace, "fixture")
    payload = {"provider": json.loads(json.dumps(demo.PROVIDER_POLICY)), "max_tokens": demo.MAX_OUTPUT_TOKENS}
    if fault == "zdr": payload["provider"]["zdr"] = False
    if fault == "deny": payload["provider"]["data_collection"] = "allow"
    if fault == "price": payload["provider"].pop("max_price")
    if fault == "tokens": payload["max_tokens"] = demo.MAX_OUTPUT_TOKENS * 2
    if fault == "budget": relay.reserved = 4.50
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    async with TestServer(app) as server, ClientSession() as client:
        response = await client.post(server.make_url("/v1/chat/completions"), json=payload)
        assert response.status == (400 if fault else 200)
    assert len(upstream) == (0 if fault else 1)
    assert bool(relay.evidence_errors) == bool(fault)


async def test_live_relay_accepts_screenshot_sized_requests_with_model_price_bound(monkeypatch):
    from aiohttp import web
    upstream = []
    async def forward(self, request):
        upstream.append(await request.json())
        return web.json_response({"forwarded": True})
    monkeypatch.setattr(demo.monitor.RecordingRelay, "handle", forward)
    relay = demo.DemoRelay(lambda *args, **kwargs: None, "fixture")
    payload = {"provider": demo.PROVIDER_POLICY, "max_tokens": demo.MAX_OUTPUT_TOKENS,
               "messages": [{"role": "user", "content": "x" * (2 * 1024 * 1024)}]}
    async with TestServer(demo.relay_app(relay)) as server, ClientSession() as client:
        response = await client.post(server.make_url("/v1/chat/completions"), json=payload)
        assert response.status == 200
    assert len(upstream) == 1
    assert 0 < relay.reserved < .50

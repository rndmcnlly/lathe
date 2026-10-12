"""Offline checks for disposable private-editor demo plumbing."""

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


@pytest.mark.parametrize("fault", [None, "capture", "cleanup", "model", "models", "requests", "evidence"])
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
    for name, value in [("environment.json", environment), ("demo-routing.json", routing), ("demo-cleanup.json", cleanup)]:
        (tmp_path / name).write_text(json.dumps(value))
    (out / "capture-report.json").write_text(json.dumps(capture))
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
    assert env["DEMO_SKIP_PREWARM"] == "1"
    assert "production-secret" not in env.values()


@pytest.mark.parametrize("fault", [None, "verifier", "redirect", "secret", "expired"])
async def test_identity_code_exchange_and_verified_user(fault):
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
        signin = await client.get(server.make_url("/demo-idp/authorize"), params=query)
        assert "Not Pocket ID" in await signin.text()
        response = await client.post(server.make_url("/demo-idp/authorize"), params=query, allow_redirects=False)
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
async def test_identity_rejects_unbound_callbacks(callback):
    async with TestServer(demo.demo_identity_app("secret")) as server, ClientSession() as client:
        response = await client.post(server.make_url("/demo-idp/authorize"), params={
            "client_id": "lathe-demo", "response_type": "code", "state": "state",
            "redirect_uri": callback, "code_challenge": "challenge", "code_challenge_method": "S256"},
            allow_redirects=False)
        assert response.status == 400


def test_infrastructure_uses_real_worker_private_auth_and_loopback_tls(tmp_path, monkeypatch):
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
    assert any("127.0.0.1:443:443" in args for args in calls)
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

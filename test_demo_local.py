"""Offline checks for disposable private-editor demo plumbing."""

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer
import pytest

import demo_local as demo


def test_capture_environment_does_not_inherit_hosted_targets_or_server_keys(monkeypatch):
    for key in ("DEMO_OWUI_URL", "DEMO_PASSKEY", "DEMO_LOCAL_TOKEN", "OWUI_URL", "OWUI_TOKEN",
                "DAYTONA_API_KEY", "OPENROUTER_API_KEY", "LATHE_PREVIEW_WRAPPER_KEY", "LATHE_DEMO_IDP_SECRET"):
        monkeypatch.setenv(key, "production-secret")
    monkeypatch.setenv("DEMO_SKIP_PREWARM", "1")
    env = demo.capture_environment("disposable-token", "12345")
    assert env["DEMO_OWUI_URL"] == "http://127.0.0.1:12345"
    assert env["DEMO_LOCAL_TOKEN"] == "disposable-token"
    assert env["DEMO_MODEL"] == demo.monitor.MODEL
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
    f"https://private.{demo.ZONE}/wrong", f"https://private.{demo.ZONE}/_lathe/auth/callback?evil=1"])
async def test_identity_rejects_unbound_callbacks(callback):
    async with TestServer(demo.demo_identity_app("secret")) as server, ClientSession() as client:
        response = await client.post(server.make_url("/demo-idp/authorize"), params={
            "client_id": "lathe-demo", "response_type": "code", "state": "state",
            "redirect_uri": callback, "code_challenge": "challenge", "code_challenge_method": "S256"})
        assert response.status == 400


def test_infrastructure_uses_real_worker_private_auth_and_loopback_tls(tmp_path, monkeypatch):
    calls = []
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
    assert variables["OIDC_ISSUER"] == demo.ISSUER
    assert variables["OIDC_CLIENT_SECRET"] == demo.os.environ["LATHE_DEMO_IDP_SECRET"]
    assert len(base64.b64decode(variables["REGISTRATION_ENCRYPTION_KEY"])) == 32
    assert json.loads((tmp_path / "wrangler.json").read_text())["vars"]["ZONE"] == demo.ZONE
    nginx = (tmp_path / "nginx.conf").read_text()
    assert f"set $identity_upstream http://{demo.monitor.CONTAINER}:9100;" in nginx
    assert "resolver 127.0.0.11" in nginx
    assert "proxy_pass $identity_upstream$request_uri;" in nginx
    assert any("127.0.0.1:443:443" in args for args in calls)

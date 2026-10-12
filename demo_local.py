#!/usr/bin/env python3
"""Record the shared-editor demo using disposable local OWUI and Wrangler.

Run: uv run python demo_local.py
Needs Docker, OpenSSL, Node/npm, and the monitor's two provider environment keys.
Port 443 must be free. No cloud deployment or production OWUI configuration.
"""

import argparse
import asyncio
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit

import certifi
import httpx

import monitor_e2e as monitor

ROOT = Path(__file__).resolve().parent
ZONE = "lathe-preview.test"
NETWORK = "lathe-local-demo"
WORKER = "lathe-local-worker"
TLS = "lathe-local-tls"
ISSUER = f"http://{monitor.CONTAINER}:9100/demo-idp"
DEMO_MODEL = "z-ai/glm-5.3-flash"


def demo_identity_app(client_secret, *, clock=time.monotonic):
    """Fixed synthetic identity, not real user authentication or a full OIDC IdP.

    Implements the Worker's discovery/code+PKCE/userinfo protocol. Only local
    Docker peers and the loopback TLS listener can reach this disposable fixture.
    It never accepts user-supplied identity and never bypasses Worker sessions.
    """
    from aiohttp import web
    codes, tokens = {}, {}

    async def discovery(request):
        return web.json_response({"issuer": ISSUER,
            "authorization_endpoint": f"https://{ZONE}/demo-idp/authorize",
            "token_endpoint": ISSUER + "/token", "userinfo_endpoint": ISSUER + "/userinfo"})

    async def authorize(request):
        query = request.query
        callback = urlsplit(query.get("redirect_uri", ""))
        if (query.get("client_id") != "lathe-demo"
                or query.get("response_type") != "code"
                or query.get("code_challenge_method") != "S256"
                or not query.get("code_challenge") or not query.get("state")
                or callback.scheme != "https" or callback.netloc != callback.hostname
                or not callback.hostname.endswith("." + ZONE)
                or callback.path != "/_lathe/auth/callback" or callback.query or callback.fragment):
            raise web.HTTPBadRequest()
        if request.method == "GET":
            return web.Response(content_type="text/html", text=
                '<h1>Disposable demo identity provider</h1>'
                '<p>Fixed synthetic identity: monitor@lathe.invalid. Not Pocket ID or passkey authentication.</p>'
                f'<form method="post" action="?{html.escape(urlencode(dict(query)), quote=True)}">'
                '<button>Sign in as demo user</button></form>')
        code = secrets.token_urlsafe(32)
        codes[code] = (dict(query), clock() + 60)
        # Reconstruct from validated components, never redirect to a raw input URL.
        redirect = f"https://{callback.hostname}/_lathe/auth/callback"
        raise web.HTTPFound(redirect + "?" + urlencode({"code": code, "state": query["state"]}))

    async def token(request):
        form = await request.post()
        if (form.get("client_id") != "lathe-demo"
                or not secrets.compare_digest(form.get("client_secret", ""), client_secret)
                or form.get("grant_type") != "authorization_code"):
            raise web.HTTPUnauthorized()
        stored = codes.pop(form.get("code", ""), None)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(
            form.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
        if (not stored or stored[1] <= clock()
                or stored[0]["redirect_uri"] != form.get("redirect_uri")
                or stored[0]["code_challenge"] != challenge):
            raise web.HTTPBadRequest()
        access = secrets.token_urlsafe(32)
        tokens[access] = clock() + 60
        return web.json_response({"access_token": access, "token_type": "Bearer", "expires_in": 60})

    async def userinfo(request):
        access = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if tokens.get(access, 0) <= clock():
            raise web.HTTPUnauthorized()
        return web.json_response({"sub": "demo-monitor", "email": "monitor@lathe.invalid", "email_verified": True})

    app = web.Application()
    app.router.add_get("/demo-idp/.well-known/openid-configuration", discovery)
    app.router.add_get("/demo-idp/authorize", authorize)
    app.router.add_post("/demo-idp/authorize", authorize)
    app.router.add_post("/demo-idp/token", token)
    app.router.add_get("/demo-idp/userinfo", userinfo)
    return app


async def serve():
    """Inside OWUI: keep the real relay and staged toolkit alive for capture."""
    from aiohttp import web
    directory = Path("/artifacts")
    trace = monitor.Trace(directory)
    relay = monitor.RecordingRelay(trace, os.environ["OPENROUTER_API_KEY"])
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 9099).start()
    identity = web.AppRunner(demo_identity_app(os.environ["LATHE_DEMO_IDP_SECRET"]), access_log=None)
    await identity.setup()
    await web.TCPSite(identity, "0.0.0.0", 9100).start()
    suite = None
    try:
        token = await monitor.bootstrap("http://127.0.0.1:8080", relay.key)
        os.environ.update(OWUI_URL="http://127.0.0.1:8080", OWUI_TOKEN=token,
                          OWUI_MODEL=monitor.workspace_model(), LATHE_TEST_TOOL_ID="lathe_test")
        import test_deployment as suite
        suite.TRACE = trace
        await suite.deploy_staging_tool()
        # Private pipe to the parent, never written to an artifact or console.
        print(json.dumps({"ready": True, "token": token}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        relay.verify_evidence()
    finally:
        try:
            if suite:
                await suite.delete_staging_tool()
        finally:
            await runner.cleanup()
            await identity.cleanup()
            (directory / "demo-routing.json").write_text(json.dumps({
                "requested_model": relay.model,
                "serving_models": sorted(trace.models), "requests": relay.requests,
                "evidence_errors": relay.evidence_errors,
            }, indent=2))


def prepare_infrastructure(path):
    token = secrets.token_urlsafe(32)
    monitor.SECRETS.append(token)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(path / "key.pem"), "-out", str(path / "cert.pem"),
        "-days", "1", "-subj", f"/CN={ZONE}", "-addext",
        f"subjectAltName=DNS:{ZONE},DNS:*.{ZONE}"], check=True, capture_output=True)
    (path / "ca-bundle.pem").write_bytes(Path(certifi.where()).read_bytes() + (path / "cert.pem").read_bytes())
    (path / "worker.mjs").write_bytes((ROOT / "preview-wrapper/src/index.js").read_bytes())
    (path / "wrangler.json").write_text(json.dumps({
        "name": "lathe-local-preview", "main": "worker.mjs", "compatibility_date": "2026-09-01",
        "kv_namespaces": [{"binding": "SUBDOMAINS", "id": "local-demo"}],
        "vars": {"ZONE": ZONE, "HOSTNAME_TEMPLATE": "lathe-{access}"},
    }))
    identity_secret = secrets.token_urlsafe(32)
    encryption_key = base64.b64encode(secrets.token_bytes(32)).decode()
    monitor.SECRETS.extend([identity_secret, encryption_key])
    os.environ["LATHE_DEMO_IDP_SECRET"] = identity_secret
    (path / ".dev.vars").write_text(f"REGISTER_TOKEN={token}\n"
        f"OIDC_ISSUER={ISSUER}\nOIDC_CLIENT_ID=lathe-demo\nOIDC_CLIENT_SECRET={identity_secret}\n"
        f"REGISTRATION_ENCRYPTION_KEY={encryption_key}\n")
    (path / ".dev.vars").chmod(0o600)
    (path / "nginx.conf").write_text(f"""events {{}}
http {{
  resolver 127.0.0.11 valid=5s;
  map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
  server {{
    listen 443 ssl;
    server_name {ZONE} *.{ZONE};
    ssl_certificate /local-demo/cert.pem;
    ssl_certificate_key /local-demo/key.pem;
    client_max_body_size 32m;
    location /demo-idp/ {{
      # OWUI is launched after this listener. Resolve its Docker name on demand.
      set $identity_upstream http://{monitor.CONTAINER}:9100;
      proxy_pass $identity_upstream$request_uri;
    }}
    location / {{
      proxy_pass http://{WORKER}:8787;
      # Wrangler rewrites same-host redirects to its HTTP development listener.
      # Restore the browser-facing TLS scheme only for this exact preview host.
      proxy_redirect http://$host/ https://$host/;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_read_timeout 300s;
    }}
  }}
}}
""")
    monitor.docker("network", "create", NETWORK)
    # Node's bundled trust store is not workerd's trust store. The slim image
    # needs OS CA certificates for Worker fetch() to verify Daytona upstreams.
    monitor.docker("run", "-d", "--name", WORKER, "--network", NETWORK,
        "--mount", f"type=bind,src={path},dst=/local-demo", "--workdir", "/local-demo",
        "node:22-bookworm-slim", "sh", "-c",
        "apt-get update -qq && apt-get install -y --no-install-recommends ca-certificates "
        "&& exec npx --yes wrangler@4.136.1 dev --local --ip 0.0.0.0 --port 8787 "
        "--config wrangler.json --persist-to /tmp/wrangler-state")
    monitor.docker("run", "-d", "--name", TLS, "--network", NETWORK,
        "--network-alias", ZONE, "-p", "127.0.0.1:443:443",
        "--mount", f"type=bind,src={path},dst=/local-demo,readonly",
        "nginx:1.28-alpine", "nginx", "-g", "daemon off;", "-c", "/local-demo/nginx.conf")
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            # Browser-style request through loopback, preserving the Worker host.
            response = httpx.get("https://127.0.0.1/", headers={"Host": ZONE}, verify=False, timeout=3)
            if response.status_code == 200 and "preview wrapper" in response.text:
                return token
        except httpx.TransportError:
            pass
        time.sleep(2)
    raise RuntimeError("Local Wrangler/TLS did not become healthy")


def capture_environment(token, port):
    """Keep stale hosted settings and server credentials out of the browser."""
    server_keys = {"DAYTONA_API_KEY", "OPENROUTER_API_KEY",
                   "LATHE_PREVIEW_WRAPPER_KEY", "LATHE_DEMO_IDP_SECRET"}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DEMO_", "OWUI_")) and k not in server_keys}
    env.update(DEMO_LOCAL_TOKEN=token, DEMO_OWUI_URL=f"http://127.0.0.1:{port}",
               DEMO_MODEL=monitor.workspace_model(), DEMO_PROXY_DOMAINS=ZONE)
    # This opt-in affects prewarm only, never authentication or preview policy.
    if os.environ.get("DEMO_SKIP_PREWARM") == "1":
        env["DEMO_SKIP_PREWARM"] = "1"
    return env


def cleanup_demo(directory):
    """Repeatable teardown of this demo's exact-label sandboxes and local stack."""
    manifest = directory / "environment.json"
    if not manifest.exists():
        return False  # No resources can have been created before this manifest.
    if not json.loads(manifest.read_text())["deployment_label"].startswith("lathe-ci-demo-"):
        raise RuntimeError("Refusing to clean a non-demo manifest")
    errors = []
    try:
        if monitor.cleanup(directory):
            errors.append("OWUI/Daytona cleanup failed")
    except Exception as exc:
        errors.append(monitor.sanitize(str(exc)))
    for name in (TLS, WORKER):
        try:
            logs = monitor.docker("logs", name, check=False)
            (directory / f"{name}.log").write_text(monitor.sanitize(logs.stdout + logs.stderr))
            result = monitor.docker("rm", "-f", "-v", name, check=False)
            if result.returncode and "No such container" not in result.stderr:
                errors.append(monitor.sanitize(result.stderr or f"Could not remove {name}"))
        except Exception as exc:
            errors.append(monitor.sanitize(str(exc)))
    try:
        result = monitor.docker("network", "rm", NETWORK, check=False)
        if result.returncode and not ("network" in result.stderr and "not found" in result.stderr):
            errors.append(monitor.sanitize(result.stderr or "Could not remove demo network"))
    except Exception as exc:
        errors.append(monitor.sanitize(str(exc)))
    (directory / "demo-cleanup.json").write_text(json.dumps({"success": not errors, "errors": errors}, indent=2))
    return bool(errors)


def qualify(directory):
    """Gate release assets on capture, routing evidence, checkout, and teardown."""
    qualification = ROOT / "demo-video/out/qualification.json"
    qualification.unlink(missing_ok=True)
    environment = json.loads((directory / "environment.json").read_text())
    routing = json.loads((directory / "demo-routing.json").read_text())
    cleanup = json.loads((directory / "demo-cleanup.json").read_text())
    capture = json.loads((ROOT / "demo-video/out/capture-report.json").read_text())
    if (capture["status"] != "accepted" or not cleanup["success"]
            or routing["requested_model"] != environment["requested_model"]
            or not routing["serving_models"] or not routing["requests"]
            or routing["evidence_errors"]):
        raise RuntimeError("Demo qualification failed: capture, routing, or cleanup evidence")
    result = {"accepted": True, "git_sha": environment["git_sha"],
              "requested_model": routing["requested_model"], "serving_models": routing["serving_models"],
              "deployment_label": environment["deployment_label"]}
    qualification.write_text(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup", action="store_true", help="Repeat teardown using --artifacts manifest")
    parser.add_argument("--artifacts", type=Path, help="Evidence directory (required for --cleanup)")
    args = parser.parse_args()
    os.environ.setdefault("LATHE_MONITOR_MODEL", DEMO_MODEL)
    if args.inside:
        asyncio.run(serve())
        return
    for key in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"Export {key} before running")
    if args.cleanup:
        if not args.artifacts:
            parser.error("--cleanup requires --artifacts")
        if cleanup_demo(args.artifacts.resolve()):
            raise RuntimeError("Demo cleanup failed")
        return
    for name in (monitor.CONTAINER, WORKER, TLS):
        result = monitor.docker("inspect", name, check=False)
        if result.returncode == 0:
            raise RuntimeError(f"Finish or clean up the existing {name} first")
        if "No such" not in result.stderr:
            raise RuntimeError(monitor.sanitize(f"Cannot establish demo isolation: {result.stderr}"))
    result = monitor.docker("network", "inspect", NETWORK, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"Network {NETWORK} already exists")
    if "not found" not in result.stderr and "No such" not in result.stderr:
        raise RuntimeError(monitor.sanitize(f"Cannot establish demo isolation: {result.stderr}"))
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    directory = args.artifacts.resolve() if args.artifacts else ROOT / "monitor-artifacts" / ("demo-" + secrets.token_hex(6))
    if (directory / "environment.json").exists():
        raise RuntimeError("Use a fresh artifact directory to avoid mixing run evidence")
    directory.mkdir(parents=True)
    (ROOT / "demo-video/out/qualification.json").unlink(missing_ok=True)
    os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] = "lathe-ci-demo-" + secrets.token_hex(12)
    (directory / "environment.json").write_text(json.dumps({
        "deployment_label": os.environ["LATHE_TEST_DEPLOYMENT_LABEL"]}))
    process = None
    with tempfile.TemporaryDirectory(prefix="lathe-demo-") as temporary:
        try:
            path = Path(temporary)
            registration_key = prepare_infrastructure(path)
            os.environ["LATHE_PREVIEW_WRAPPER_KEY"] = registration_key
            monitor.launch(directory, docker_args=(
                "--network", NETWORK,
                "--mount", f"type=bind,src={path},dst=/local-demo,readonly",
                "--env", "SSL_CERT_FILE=/local-demo/ca-bundle.pem",
                "--env", f"LATHE_PREVIEW_WRAPPER_URL=https://{ZONE}/register",
                "--env", "LATHE_DEMO_IDP_SECRET",
                "--env", "LATHE_PREVIEW_WRAPPER_KEY"))
            process = subprocess.Popen(["docker", "exec", "--user", f"{os.getuid()}:{os.getgid()}", "-i", monitor.CONTAINER,
                "python", "/monitor/demo_local.py", "--inside"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(420):
                raise RuntimeError("Disposable demo bootstrap timed out")
            line = process.stdout.readline()
            if not line:
                raise RuntimeError(monitor.sanitize(process.stderr.read()))
            ready = json.loads(line)
            selector.close()
            token = ready["token"]
            monitor.SECRETS.append(token)
            port = monitor.docker("port", monitor.CONTAINER, "8080/tcp").stdout.strip().split(":")[-1]
            env = capture_environment(token, port)
            print(f"Recording local demo at http://127.0.0.1:{port}", flush=True)
            subprocess.run(["npm", "run", "capture"], cwd=ROOT / "demo-video", env=env,
                           check=True, timeout=1200)
        finally:
            inner_failed = False
            if process:
                try:
                    stdout, stderr = process.communicate("done\n", timeout=45)
                    (directory / "demo-host.log").write_text(monitor.sanitize(stdout + stderr))
                    inner_failed = process.returncode != 0
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    inner_failed = True
            failed = cleanup_demo(directory)
            if failed:
                raise RuntimeError(f"Cleanup failed; inspect {directory}")
            if inner_failed:
                raise RuntimeError(f"Demo relay or staging teardown failed; inspect {directory}")
    qualify(directory)
    print(f"Video: {ROOT / 'demo-video/out/demo.webm'}")


if __name__ == "__main__":
    main()

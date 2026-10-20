#!/usr/bin/env python3
"""Record everyday data visualization using disposable local OWUI and Wrangler.

Run: uv run python demo_local.py
Needs Docker, OpenSSL, Node/npm, and the monitor's two provider environment keys.
Uses loopback TLS port 443 by default (DEMO_TLS_PORT overrides the host mapping).
No cloud deployment or production OWUI configuration.
"""

import argparse
import asyncio
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
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
PROVIDER_POLICY = {"data_collection": "deny", "zdr": True,
                   "max_price": {"prompt": .20, "completion": 1.00}}
MAX_OUTPUT_TOKENS = 16384


class DemoRelay(monitor.RecordingRelay):
    """Keep privacy and a conservative $4.50 reservation at the upstream boundary."""
    def __init__(self, trace, key):
        super().__init__(trace, key)
        self.reserved = 0.0

    async def handle(self, request):
        from aiohttp import web
        payload = await request.json()
        if any(payload.get("provider", {}).get(k) != v for k, v in PROVIDER_POLICY.items()):
            self.evidence_errors.append("privacy or price policy missing")
            raise web.HTTPBadRequest(text="Demo requires ZDR/deny/price policy")
        cap = payload.get("max_tokens", payload.get("max_completion_tokens", 0))
        request_bytes = len(await request.read())
        prices = PROVIDER_POLICY["max_price"]
        bound = ((request_bytes + 10000) * prices["prompt"] + cap * prices["completion"]) / 1_000_000
        if not 0 < cap <= MAX_OUTPUT_TOKENS or self.reserved + bound > 4.50 or self.requests >= 40:
            self.trace("demo_budget_rejected", max_tokens=cap, request_bytes=request_bytes,
                       reserved_usd=self.reserved, next_bound_usd=bound)
            self.evidence_errors.append("demo inference budget")
            raise web.HTTPBadRequest(text="Demo inference budget exhausted")
        self.reserved += bound
        self.trace("demo_policy", zdr=True, data_collection="deny", reserved_usd=self.reserved)
        return await super().handle(request)


def tls_port():
    port = int(os.environ.get("DEMO_TLS_PORT", "443"))
    if not 1 <= port <= 65535:
        raise ValueError("DEMO_TLS_PORT must be between 1 and 65535")
    return port


def relay_app(relay):
    """Allow ordinary screenshot-bearing requests, still subject to the budget."""
    from aiohttp import web
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_post("/v1/chat/completions", relay.handle)
    return app


async def configure_demo(api):
    """Use Lathe without native agents and block sandbox outbound traffic.

    Preserve bootstrap's provider policy and all staging lifecycle valves.
    Refresh OWUI's effective model cache after changing builtin capabilities.
    """
    response = await api.get("/api/models")
    response.raise_for_status()
    effective = next(m for m in response.json()["data"] if m["id"] == monitor.workspace_model())
    model = {k: effective["info"][k] for k in ("id", "base_model_id", "name", "params", "meta")}
    model["params"].setdefault("custom_params", {}).setdefault("provider", {}).update(PROVIDER_POLICY)
    # OWUI can omit the ordinary model max_tokens parameter on a UI-native turn.
    # An explicit upstream override keeps the actual request bounded as well.
    model["params"]["max_tokens"] = MAX_OUTPUT_TOKENS
    model["params"]["custom_params"]["max_tokens"] = MAX_OUTPUT_TOKENS
    model["params"]["system"] = "The sandbox has no outbound Internet access. Use available local tools and self-contained assets."
    model["meta"]["capabilities"]["builtin_tools"] = False
    response = await api.post("/api/v1/models/model/update",
                              params={"id": monitor.workspace_model()}, json=model)
    response.raise_for_status()
    response = await api.get("/api/models", params={"refresh": "true"})
    response.raise_for_status()
    effective = next(m for m in response.json()["data"] if m["id"] == monitor.workspace_model())
    if effective["info"]["meta"]["capabilities"]["builtin_tools"]:
        raise RuntimeError("OWUI's effective model unexpectedly enables builtin tools")
    path = "/api/v1/tools/id/lathe_test/valves"
    response = await api.get(path)
    response.raise_for_status()
    valves = response.json()
    overrides = json.loads(valves["sandbox_create_overrides"])
    overrides["networkBlockAll"] = True
    valves["sandbox_create_overrides"] = json.dumps(overrides)
    response = await api.post(path + "/update", json=valves)
    response.raise_for_status()


async def verify_demo_files(http, directory):
    """Read actual sandbox policy and artifacts without the model or toolkit."""
    response = await http.get("https://app.daytona.io/api/sandbox", params={
        "labels": json.dumps({os.environ["LATHE_TEST_DEPLOYMENT_LABEL"]: "monitor@lathe.invalid"})})
    response.raise_for_status()
    data = response.json()
    matches = [s for s in (data if isinstance(data, list) else data.get("items", []))
               if s.get("labels", {}).get(os.environ["LATHE_TEST_DEPLOYMENT_LABEL"]) == "monitor@lathe.invalid"]
    if len(matches) != 1 or matches[0].get("networkBlockAll") is not True or matches[0].get("volumes"):
        raise RuntimeError("Expected one egress-blocked, volume-free demo sandbox")
    sandbox = matches[0]
    artifact = json.loads((ROOT / "demo-video/out/artifact.json").read_text())
    path = artifact["path"]
    if not path.startswith("/") or not path.lower().endswith(".html"):
        raise RuntimeError("Expected an absolute HTML artifact path")
    response = await http.get(f"https://proxy.app.daytona.io/toolbox/{sandbox['id']}/files/download", params={"path": path})
    response.raise_for_status()
    html = response.text
    rows = list(csv.DictReader(io.StringIO((ROOT / "demo-video/repair-log.csv").read_text())))
    summary = {"items": len(rows), "repaired": sum(row["outcome"] == "repaired" for row in rows),
               "minutes": sum(int(row["minutes"]) for row in rows)}
    text = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    if (len(html) < 500 or "repair" not in text.lower()
            or any(not re.search(rf"\b{summary[key]}\b", text) for key in ("items", "repaired"))
            or re.search(r"(?:src|href)\s*=\s*['\"]\s*(?:https?:)?//", html, re.I)
            or re.search(r"(?:@import|url\()\s*['\"]?(?:https?:)?//", html, re.I)):
        raise RuntimeError("Independent infographic read lacks basic results or uses external assets")
    (directory / "independent-files.json").write_text(json.dumps({
        "verified": True, "networkBlockAll": True, "sandbox_id": sandbox["id"],
        "reference_summary": summary, "files": {path: html}}, indent=2))


def demo_identity_app(client_secret, *, clock=time.monotonic):
    """Fixed synthetic identity, not real user authentication or a full OIDC IdP.

    Auto-approves the fixed demo user without showing a sign-in page.
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
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\." + re.escape(ZONE), callback.hostname or "")
                or callback.path != "/_lathe/auth/callback" or callback.query or callback.fragment):
            raise web.HTTPBadRequest()
        # This isolated fixture has exactly one synthetic identity, not a user
        # login decision. Keep the real Worker callback and PKCE exchange intact.
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
    relay = DemoRelay(trace, os.environ["OPENROUTER_API_KEY"])
    app = relay_app(relay)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 9099).start()
    identity = web.AppRunner(demo_identity_app(os.environ["LATHE_DEMO_IDP_SECRET"]), access_log=None)
    await identity.setup()
    await web.TCPSite(identity, "0.0.0.0", 9100).start()
    suite = None
    try:
        token = await monitor.bootstrap("http://127.0.0.1:8080", relay.key, display_name=relay.model)
        os.environ.update(OWUI_URL="http://127.0.0.1:8080", OWUI_TOKEN=token,
                          OWUI_MODEL=monitor.workspace_model(), LATHE_TEST_TOOL_ID="lathe_test")
        import test_deployment as suite
        suite.TRACE = trace
        await suite.deploy_staging_tool(display_name="Lathe")
        deployed = await suite.fetch_staging_tool()
        actual_sha = hashlib.sha256(deployed["content"].encode()).hexdigest()
        expected_sha = hashlib.sha256((ROOT / "lathe.py").read_bytes()).hexdigest()
        if actual_sha != expected_sha:
            raise RuntimeError("Deployed Lathe source differs from the checkout")
        (directory / "source-qualification.json").write_text(json.dumps({
            "deployed_sha256": actual_sha, "checkout_sha256": expected_sha, "matched": True}, indent=2))
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080",
                headers={"Authorization": f"Bearer {token}"}, timeout=30) as api:
            await configure_demo(api)
        # Private pipe to the parent, never written to an artifact or console.
        print(json.dumps({"ready": True, "token": token}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        relay.verify_evidence()
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {os.environ['DAYTONA_API_KEY']}"}, timeout=30) as http:
            await verify_demo_files(http, directory)
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
                "upper_bound_reserved_usd": relay.reserved,
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
        "--env", f"LOCAL_UID={os.getuid()}", "--env", f"LOCAL_GID={os.getgid()}",
        "--mount", f"type=bind,src={path},dst=/local-demo", "--workdir", "/local-demo",
        "node:22-bookworm-slim", "sh", "-c",
        "apt-get update -qq && apt-get install -y --no-install-recommends ca-certificates util-linux "
        # Wrangler writes .wrangler/tmp beside its config even with --persist-to.
        # Drop root before npm/Wrangler so host-owned temporary cleanup works.
        "&& exec setpriv --reuid \"$LOCAL_UID\" --regid \"$LOCAL_GID\" --clear-groups "
        "env HOME=/tmp NPM_CONFIG_CACHE=/tmp/npm-cache "
        "npx --yes wrangler@4.136.1 dev --local --ip 0.0.0.0 --port 8787 "
        "--config wrangler.json --persist-to /tmp/wrangler-state")
    monitor.docker("run", "-d", "--name", TLS, "--network", NETWORK,
        "--network-alias", ZONE, "-p", f"127.0.0.1:{tls_port()}:443",
        "--mount", f"type=bind,src={path},dst=/local-demo,readonly",
        "nginx:1.28-alpine", "nginx", "-g", "daemon off;", "-c", "/local-demo/nginx.conf")
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            # Browser-style request through loopback, preserving the Worker host.
            response = httpx.get(f"https://127.0.0.1:{tls_port()}/", headers={"Host": ZONE}, verify=False, timeout=3)
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
               DEMO_MODEL=monitor.workspace_model(), DEMO_PROXY_DOMAINS=ZONE,
               DEMO_TLS_PORT=str(tls_port()))
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
            # Repeated cleanup must not replace real logs with "No such container".
            if logs.returncode == 0:
                with (directory / f"{name}.log").open("a") as out:
                    out.write(monitor.sanitize(logs.stdout + logs.stderr))
            result = monitor.docker("rm", "-f", "-v", name, check=False)
            if result.returncode and "no such container" not in result.stderr.lower():
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
    files = json.loads((directory / "independent-files.json").read_text())
    if (capture["status"] != "accepted" or not cleanup["success"]
            or routing["requested_model"] != environment["requested_model"]
            or not routing["serving_models"] or not routing["requests"]
            or routing["evidence_errors"] or files.get("verified") is not True
            or files.get("networkBlockAll") is not True):
        raise RuntimeError("Demo qualification failed: capture, routing, or cleanup evidence")
    result = {"accepted": True, "git_sha": environment["git_sha"],
              "requested_model": routing["requested_model"], "serving_models": routing["serving_models"],
              "deployment_label": environment["deployment_label"]}
    qualification.write_text(json.dumps(result, indent=2))
    (directory / "qualification.json").write_text(json.dumps(result, indent=2))


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
        if "no such" not in result.stderr.lower():
            raise RuntimeError(monitor.sanitize(f"Cannot establish demo isolation: {result.stderr}"))
    result = monitor.docker("network", "inspect", NETWORK, check=False)
    if result.returncode == 0:
        raise RuntimeError(f"Network {NETWORK} already exists")
    if "not found" not in result.stderr.lower() and "no such" not in result.stderr.lower():
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
            monitor.launch(directory, tool_call_iterations=24, docker_args=(
                "--network", NETWORK,
                "--env", "DEFAULT_PROMPT_SUGGESTIONS=[]",
                "--env", "ENABLE_CODE_INTERPRETER=false",
                "--env", "ENABLE_FOLLOW_UP_GENERATION=false",
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

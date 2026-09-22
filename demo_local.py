#!/usr/bin/env python3
"""Record the shared-editor demo using disposable local OWUI and Wrangler.

Run: uv run python demo_local.py
Needs Docker, OpenSSL, Node/npm, and the monitor's two provider environment keys.
Port 443 must be free. No cloud deployment or production OWUI configuration.
"""

import asyncio
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

import certifi
import httpx

import monitor_e2e as monitor

ROOT = Path(__file__).resolve().parent
ZONE = "lathe-preview.test"
NETWORK = "lathe-local-demo"
WORKER = "lathe-local-worker"
TLS = "lathe-local-tls"


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
    suite = None
    try:
        token = await monitor.bootstrap("http://127.0.0.1:8080", relay.key)
        os.environ.update(OWUI_URL="http://127.0.0.1:8080", OWUI_TOKEN=token,
                          OWUI_MODEL=monitor.MODEL, LATHE_TEST_TOOL_ID="lathe_test")
        import test_deployment as suite
        suite.TRACE = trace
        await suite.deploy_staging_tool()
        # Private pipe to the parent, never written to an artifact or console.
        print(json.dumps({"ready": True, "token": token}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
    finally:
        if suite:
            await suite.delete_staging_tool()
        await runner.cleanup()
        (directory / "demo-routing.json").write_text(json.dumps({
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
    (path / ".dev.vars").write_text(f"REGISTER_TOKEN={token}\n")
    (path / "nginx.conf").write_text(f"""events {{}}
http {{
  map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
  server {{
    listen 443 ssl;
    server_name {ZONE} *.{ZONE};
    ssl_certificate /local-demo/cert.pem;
    ssl_certificate_key /local-demo/key.pem;
    client_max_body_size 32m;
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


def main():
    if "--inside" in sys.argv:
        asyncio.run(serve())
        return
    for key in ("DAYTONA_API_KEY", "OPENROUTER_API_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"Export {key} before running")
    for name in (monitor.CONTAINER, WORKER, TLS):
        if monitor.docker("inspect", name, check=False).returncode == 0:
            raise RuntimeError(f"Finish or clean up the existing {name} first")
    if monitor.docker("network", "inspect", NETWORK, check=False).returncode == 0:
        raise RuntimeError(f"Network {NETWORK} already exists")
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    directory = ROOT / "monitor-artifacts" / ("demo-" + secrets.token_hex(6))
    directory.mkdir(parents=True)
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
                "--env", "LATHE_PREVIEW_WRAPPER_KEY"))
            process = subprocess.Popen(["docker", "exec", "-i", monitor.CONTAINER,
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
            env = {k: v for k, v in os.environ.items() if not k.startswith(("DEMO_", "OWUI_"))}
            env.update(DEMO_LOCAL_TOKEN=token, DEMO_OWUI_URL=f"http://127.0.0.1:{port}",
                       DEMO_MODEL=monitor.MODEL, DEMO_PROXY_DOMAINS=ZONE)
            print(f"Recording local demo at http://127.0.0.1:{port}", flush=True)
            subprocess.run(["npm", "run", "capture"], cwd=ROOT / "demo-video", env=env,
                           check=True, timeout=1200)
        finally:
            if process:
                try:
                    stdout, stderr = process.communicate("done\n", timeout=45)
                    (directory / "demo-host.log").write_text(monitor.sanitize(stdout + stderr))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            failed = monitor.cleanup(directory)
            for name in (TLS, WORKER):
                logs = monitor.docker("logs", name, check=False)
                (directory / f"{name}.log").write_text(monitor.sanitize(logs.stdout + logs.stderr))
                monitor.docker("rm", "-f", "-v", name, check=False)
            monitor.docker("network", "rm", NETWORK, check=False)
            if failed:
                raise RuntimeError(f"Cleanup failed; inspect {directory}")
    print(f"Video: {ROOT / 'demo-video/out/demo.webm'}")


if __name__ == "__main__":
    main()

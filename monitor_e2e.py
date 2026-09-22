#!/usr/bin/env python3
"""Disposable real-provider monitor. Run with `uv run python monitor_e2e.py`.

The host owns Docker and last-resort cleanup. The inner process bootstraps OWUI,
records the real inference boundary, and invokes the existing deployment suite.
Only sanitized artifacts leave the container. See CI-MONITOR.md.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parent
CONTAINER = "lathe-e2e-monitor"
MODEL = "lathe-ci-pareto"
ROUTER = "openrouter/pareto-code"
PLUGINS = [{"id": "pareto-router", "min_coding_score": 0.5}]
SECRETS = []


def sanitize(text):
    """Redact before persistence, including bearer URLs with unfamiliar formats."""
    for value in [*SECRETS, *(os.environ.get(k, "") for k in
                  ("OPENROUTER_API_KEY", "DAYTONA_API_KEY", "OWUI_TOKEN", "GITHUB_TOKEN"))]:
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?:sk-or-v1-|dtn_)[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s\"'\\]+", "Bearer [REDACTED]", text)
    # Preview URLs are capabilities. Redact all URLs rather than guessing which
    # future Daytona hostname or signature parameter is sensitive.
    return re.sub(r"https?://[^\s\"'<>\\]+", "[URL]", text)


class Trace:
    def __init__(self, directory):
        self.path = directory / "trace.jsonl"
        self.started = time.monotonic()
        self.scenario = "bootstrap"
        self.first_failure = None
        self.models = set()

    def __call__(self, kind, **data):
        if kind == "scenario_start":
            self.scenario = data["name"]
        if kind in ("scenario_fail", "fatal") and not self.first_failure:
            self.first_failure = self.scenario
        if kind == "cleanup_error" and not self.first_failure:
            self.first_failure = "cleanup"
        if kind == "inference" and data.get("model"):
            self.models.add(data["model"])
        row = {"seconds": round(time.monotonic() - self.started, 3),
               "scenario": self.scenario, "kind": kind, **data}
        with self.path.open("a") as out:
            out.write(sanitize(json.dumps(row, default=str)) + "\n")


class RecordingRelay:
    """Loopback-only forwarder, with no synthetic model responses or body edits.

    A workspace model supplies plugins to parent AND delegate requests. Reject
    missing policy at the actual upstream boundary instead of silently falling
    back to the account's (possibly different) router tier.
    """

    def __init__(self, trace, key):
        self.trace = trace
        self.key = key
        self.requests = 0
        self.evidence_errors = []

    async def handle(self, request):
        from aiohttp import web

        body = await request.read()
        payload = json.loads(body)
        if (payload.get("model") != ROUTER or payload.get("plugins") != PLUGINS
                or request.headers.get("Authorization") != f"Bearer {self.key}"):
            self.trace("fatal", error="Inference request lost router policy or authentication")
            self.evidence_errors.append("routing contract")
            raise web.HTTPBadRequest(text="Monitor routing contract violated")
        self.requests += 1
        number = self.requests
        self.trace("inference_request", request=number, model=payload["model"],
                   plugins=payload["plugins"], stream=payload.get("stream", False))
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                content=body, headers={"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json", "HTTP-Referer": "https://openwebui.com/",
                "X-Title": "Open WebUI"}) as upstream:
                response = web.StreamResponse(status=upstream.status_code,
                    headers={"Content-Type": upstream.headers.get("content-type", "application/json")})
                await response.prepare(request)
                buffer = b""
                recorded = set()

                def observe(raw):
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        return
                    if data.get("error"):
                        self.trace("provider_error", request=number, error=data["error"])
                    model = data.get("model")
                    if model and model not in recorded:
                        recorded.add(model)
                        self.trace("inference", request=number, model=model, id=data.get("id"))
                    if data.get("usage"):
                        self.trace("usage", request=number, usage=data["usage"])

                streaming = "text/event-stream" in upstream.headers.get("content-type", "")
                async for chunk in upstream.aiter_bytes():
                    buffer += chunk
                    if streaming:
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if line.startswith(b"data:"):
                                observe(line[5:].strip())
                    await response.write(chunk)
                if not streaming:
                    observe(buffer)
                elif buffer.startswith(b"data:"):
                    observe(buffer[5:].strip())
                if not recorded:
                    self.trace("provider_error", request=number, status=upstream.status_code,
                               error="No concrete serving model observed")
                    if upstream.is_success:
                        self.evidence_errors.append(f"request {number}: missing serving model")
                await response.write_eof()
                return response


async def bootstrap(base, key):
    """Create the first admin and a native-tool workspace model on fresh OWUI.

    Kept separate so a future demo-video runner can reuse the disposable host
    and the same repository secrets rather than targeting a daily-use server.
    """
    password = secrets.token_urlsafe(32)
    SECRETS.append(password)
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        for _ in range(180):
            try:
                response = await client.get("/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(2)
        else:
            raise RuntimeError("Disposable Open WebUI did not become healthy")
        response = await client.post("/api/v1/auths/signup", json={
            "name": "Lathe Monitor", "email": "monitor@lathe.invalid", "password": password})
        response.raise_for_status()
        account = response.json()
        if account.get("role") != "admin":
            raise RuntimeError("First disposable account is not admin")
        token = account["token"]
        SECRETS.append(token)
        client.headers["Authorization"] = f"Bearer {token}"
        response = await client.post("/openai/config/update", json={
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": ["http://127.0.0.1:9099/v1"],
            "OPENAI_API_KEYS": [key],
            "OPENAI_API_CONFIGS": {"0": {"enable": True, "model_ids": [ROUTER]}},
        })
        response.raise_for_status()
        response = await client.post("/api/v1/models/create", json={
            "id": MODEL, "base_model_id": ROUTER, "name": "CI Pareto Medium",
            "params": {"function_calling": "native", "max_tokens": 4096,
                       "custom_params": {"plugins": PLUGINS,
                                         "provider": {"data_collection": "deny"}}},
            "meta": {"capabilities": {"vision": True, "builtin_tools": False}},
        })
        response.raise_for_status()
        response = await client.get("/api/models")
        response.raise_for_status()
        if MODEL not in {m["id"] for m in response.json()["data"]}:
            raise RuntimeError("Disposable workspace model is missing")
    return token


async def inside(directory):
    from aiohttp import web

    trace = Trace(directory)
    relay = RecordingRelay(trace, os.environ["OPENROUTER_API_KEY"])
    app = web.Application()
    app.router.add_post("/v1/chat/completions", relay.handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 9099).start()
    result = 1
    try:
        base = "http://127.0.0.1:8080"
        token = await bootstrap(base, relay.key)
        os.environ.update(OWUI_URL=base, OWUI_TOKEN=token, OWUI_MODEL=MODEL,
                          LATHE_ISOLATED_CI="1", LATHE_TEST_TOOL_ID="lathe_test")
        import test_deployment as suite
        suite.TRACE = trace
        result = await suite.main()
        if relay.evidence_errors or not trace.models or ROUTER in trace.models or MODEL in trace.models:
            result = 1
            raise RuntimeError("Concrete OpenRouter serving-model evidence is missing")
    except Exception as exc:
        result = 1
        trace("fatal", error=f"{type(exc).__name__}: {exc}")
        print(sanitize(f"Monitor failed: {type(exc).__name__}: {exc}"))
    finally:
        await runner.cleanup()
        summary = {"exit_code": result, "first_failing_scenario": trace.first_failure,
                   "serving_models": sorted(trace.models), "inference_requests": relay.requests}
        (directory / "summary.json").write_text(sanitize(json.dumps(summary, indent=2)) + "\n")
    return result


def docker(*args, check=True, timeout=600):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(sanitize(f"Docker {args[0]} failed: {result.stderr}"))
    return result


async def cleanup_sandboxes():
    # Explicitly disable dotenv: the monitor never imports local OWUI settings.
    os.environ["LATHE_ISOLATED_CI"] = "1"
    import test_deployment as suite
    await suite.cleanup_test_sandboxes()


def cleanup(directory):
    """Stop tool execution before external cleanup, including after cancellation."""
    failed = False
    state = directory / "environment.json"
    if state.exists():
        os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] = json.loads(state.read_text())["deployment_label"]
    elif not os.environ.get("LATHE_TEST_DEPLOYMENT_LABEL", "").startswith("lathe-ci-"):
        # No launch identity means no sandbox could have been created by us.
        return False
    try:
        if docker("inspect", CONTAINER, check=False).returncode == 0:
            docker("stop", "--time", "10", CONTAINER, check=False, timeout=30)
            logs = docker("logs", CONTAINER, check=False, timeout=30)
            (directory / "owui.log").write_text(sanitize(logs.stdout + logs.stderr))
    finally:
        removal = docker("rm", "--force", "--volumes", CONTAINER, check=False, timeout=60)
        if removal.returncode and "No such container" not in removal.stderr:
            failed = True
        try:
            asyncio.run(cleanup_sandboxes())
        except Exception as exc:
            failed = True
            (directory / "cleanup-error.log").write_text(sanitize(str(exc)))
    (directory / "cleanup.json").write_text(json.dumps({"success": not failed}) + "\n")
    return failed


def launch(directory, *, docker_args=()):
    """Resolve latest stable, pin its digest, then launch a loopback-only host."""
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}
    response = httpx.get("https://api.github.com/repos/open-webui/open-webui/releases/latest",
                         headers=headers, timeout=30)
    response.raise_for_status()
    release = response.json()
    tag = release["tag_name"]
    if release.get("prerelease") or release.get("draft") or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise RuntimeError("Latest OWUI release is not a stable version")
    image = f"ghcr.io/open-webui/open-webui:{tag}-slim"
    print(f"Pulling {image}", flush=True)
    docker("pull", image, timeout=900)
    info = json.loads(docker("image", "inspect", image).stdout)[0]
    digest = next(d for d in info["RepoDigests"] if d.startswith("ghcr.io/open-webui/open-webui@"))
    metadata = {"owui_release": tag, "image": image, "image_digest": digest,
                "image_id": info["Id"], "architecture": info["Architecture"],
                "lathe_sha256": hashlib.sha256((ROOT / "lathe.py").read_bytes()).hexdigest(),
                "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "router": ROUTER, "plugins": PLUGINS,
                "deployment_label": os.environ["LATHE_TEST_DEPLOYMENT_LABEL"]}
    (directory / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    docker("run", "--detach", "--name", CONTAINER,
           "--publish", "127.0.0.1:0:8080", "--mount", f"type=bind,src={ROOT},dst=/monitor,readonly",
           "--mount", f"type=bind,src={directory},dst=/artifacts",
           "--env", "OPENROUTER_API_KEY", "--env", "DAYTONA_API_KEY",
           "--env", "LATHE_TEST_DEPLOYMENT_LABEL",
           "--env", "LATHE_ISOLATED_CI=1", "--env", "ENABLE_OLLAMA_API=false",
           "--env", "ENABLE_SIGNUP=true", "--env", "WEBUI_AUTH=true",
           "--env", "SCARF_NO_ANALYTICS=true", "--env", "DO_NOT_TRACK=true",
           "--env", "CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS=12",
           "--env", "HF_HUB_OFFLINE=1", *docker_args, digest)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup", action="store_true", help="Last-resort cleanup after interruption")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "monitor-artifacts")
    args = parser.parse_args()
    directory = args.artifacts.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.inside:
        return asyncio.run(inside(directory))
    if not os.environ.get("DAYTONA_API_KEY") or not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("DAYTONA_API_KEY and OPENROUTER_API_KEY must be supplied in the environment")
    if args.cleanup:
        return int(cleanup(directory))
    # Refuse an existing container instead of deleting another local run.
    if docker("inspect", CONTAINER, check=False).returncode == 0:
        parser.error(f"{CONTAINER} already exists; finish its run or use --cleanup")
    identity = (f"{os.environ['GITHUB_RUN_ID']}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
                if os.environ.get("GITHUB_RUN_ID") else secrets.token_hex(12))
    os.environ["LATHE_TEST_DEPLOYMENT_LABEL"] = "lathe-ci-" + identity
    # This directory is suite-owned. Prevent an interrupted rerun from mixing
    # old success evidence with its new failure, without touching unknown files.
    for name in ("environment.json", "trace.jsonl", "summary.json", "suite.log",
                 "owui.log", "host-error.log", "cleanup-error.log", "cleanup.json"):
        (directory / name).unlink(missing_ok=True)
    (directory / "environment.json").write_text(json.dumps({
        "deployment_label": os.environ["LATHE_TEST_DEPLOYMENT_LABEL"],
        "lathe_sha256": hashlib.sha256((ROOT / "lathe.py").read_bytes()).hexdigest(),
    }) + "\n")

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    result = 1
    try:
        launch(directory)
        run = docker("exec", CONTAINER, "python", "/monitor/monitor_e2e.py",
                     "--inside", "--artifacts", "/artifacts", check=False, timeout=1500)
        output = sanitize(run.stdout + run.stderr)
        (directory / "suite.log").write_text(output)
        print(output, flush=True)
        result = run.returncode
    except (Exception, KeyboardInterrupt) as exc:
        message = sanitize(f"{type(exc).__name__}: {exc}")
        (directory / "host-error.log").write_text(message)
        print(message, flush=True)
    finally:
        if cleanup(directory):
            result = 1
        summary_path = directory / "summary.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        if result and not summary.get("first_failing_scenario"):
            summary["first_failing_scenario"] = "host lifecycle"
        summary["exit_code"] = result
        summary_path.write_text(sanitize(json.dumps(summary, indent=2)) + "\n")
    return result


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Host controller: no dotenv, cloud SDK, compose daemon, or third-party deps."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
LABEL = "tools.lathe.readiness-infra"
IMAGES = json.loads((ROOT / "images.json").read_text())


def docker(*args, timeout=60, stdin=None, log=None):
    try:
        result = subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"docker {args[0]} timed out after {timeout}s") from None
    if log is not None:
        log.write_text((result.stdout + result.stderr).decode(errors="replace"))
    if result.returncode:
        # Our arguments can contain ephemeral DB/JWT secrets. Do not render them.
        raise RuntimeError(f"docker {args[0]} failed: " + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout.decode()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def validate_manifest(manifest):
    owner = manifest["owner"]
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise RuntimeError("invalid ownership manifest")
    expected = [f"lathe-preflight-{owner}-{suffix}" for suffix in ("pg", "redis", "app")]
    if manifest["containers"] != expected or manifest["network"] != f"lathe-preflight-{owner}":
        raise RuntimeError("ownership manifest names do not match exact run")
    return manifest


def load_manifest(directory):
    return validate_manifest(json.loads((directory / "owner.json").read_text()))


def validate_images():
    bases = re.findall(r"^FROM\s+(\S+)\s*$", (ROOT / "Dockerfile").read_text(), re.MULTILINE | re.IGNORECASE)
    from_count = len(re.findall(r"^\s*FROM\s", (ROOT / "Dockerfile").read_text(), re.MULTILINE | re.IGNORECASE))
    if bases != [IMAGES["owui"]] or from_count != 1:
        raise RuntimeError("images.json OWUI reference must equal the single Dockerfile FROM")


def cleanup(directory, *, manifest=None):
    # The active controller retains its exact ownership even if evidence storage
    # disappears. Standalone --cleanup still requires the on-disk manifest.
    manifest = load_manifest(directory) if manifest is None else validate_manifest(manifest)
    owner = manifest["owner"]
    # Resolve names only against a complete daemon listing: an inspect failure
    # could mean a dead daemon, not absence. Never interpret it as clean.
    existing = set(docker("ps", "-a", "--format", "{{.Names}}").splitlines())
    for name in manifest["containers"]:
        if name not in existing:
            continue
        info = json.loads(docker("inspect", name))[0]
        if info["Name"] != "/" + name or info["Config"]["Labels"].get(LABEL) != owner:
            raise RuntimeError("refusing cleanup: container ownership mismatch")
        docker("rm", "-f", "-v", info["Id"])
    existing_networks = set(docker("network", "ls", "--format", "{{.Name}}").splitlines())
    if manifest["network"] in existing_networks:
        info = json.loads(docker("network", "inspect", manifest["network"]))[0]
        if info["Labels"].get(LABEL) != owner or info["Containers"]:
            raise RuntimeError("refusing cleanup: network ownership mismatch or foreign endpoints")
        docker("network", "rm", info["Id"])
    remaining = set(docker("ps", "-a", "--format", "{{.Names}}").splitlines())
    networks = set(docker("network", "ls", "--format", "{{.Name}}").splitlines())
    if remaining.intersection(manifest["containers"]) or manifest["network"] in networks:
        raise RuntimeError("exact owned resources remain after cleanup")
    if docker("ps", "-aq", "--filter", f"label={LABEL}={owner}").strip() or docker(
            "network", "ls", "-q", "--filter", f"label={LABEL}={owner}").strip():
        raise RuntimeError("unexpected resources with this run's owner label")
    save(directory / "cleanup.json", {"verified_absent": True, **manifest})


def stage(app, source):
    # Only two explicitly named files cross from the repo. No build context can
    # see .env, ignored artifacts, or any host credentials.
    archive = io.BytesIO()
    hashes = {}
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name in ("lathe.py", "testing_support.py"):
            data = (source / name).read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            entry = tarfile.TarInfo("staged/" + name)
            entry.size, entry.mode = len(data), 0o444
            tar.addfile(entry, io.BytesIO(data))
    docker("cp", "-", app + ":/opt/", stdin=archive.getvalue())
    return hashes


def topology(directory, source, *, image_id=None, live_action=None, scope="native", config_only=False,
             qualified=None, deployment_label=None):
    """Run one fresh topology, always removing its exact owned resources.

    The only live seam is the controller action. Images, source staging, service
    health, migration/worker implementation and cleanup are shared with preflight.
    """
    directory.mkdir(parents=True, exist_ok=False)
    owner = uuid.uuid4().hex
    network = f"lathe-preflight-{owner}"
    pg, redis, app = [network + "-" + suffix for suffix in ("pg", "redis", "app")]
    manifest = {"owner": owner, "network": network, "containers": [pg, redis, app]}
    save(directory / "owner.json", manifest)
    evidence = {"images": IMAGES, "source_sha256": hashlib.sha256((source / "lathe.py").read_bytes()).hexdigest(),
                 "testing_support_sha256": hashlib.sha256((source / "testing_support.py").read_bytes()).hexdigest(),
                 "controller_sha256": {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                     for name in ("readiness_lab.py", "monitor_e2e.py", "test_deployment.py") if (source / name).is_file()},
                "infra_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in ROOT.iterdir() if p.is_file()},
                "status": "starting"}
    phase = "build"
    started = time.monotonic()
    try:
        validate_images()
        if qualified is not None:
            for key in ("images", "source_sha256", "testing_support_sha256", "infra_sha256", "controller_sha256"):
                if evidence[key] != qualified[key]:
                    raise RuntimeError("qualified infrastructure/source changed: " + key)
            if image_id != qualified["built_image_id"]:
                raise RuntimeError("live image differs from qualified image")
        tag = "lathe-readiness-infra:" + owner
        # Build output contains only the allowlisted context and public packages.
        if image_id is None:
            docker("build", "--progress=plain", "-t", tag, str(ROOT), timeout=240, log=directory / "build.txt")
            image_id = docker("image", "inspect", tag, "--format", "{{.Id}}").strip()
        evidence["built_image_id"] = image_id
        phase = "pull dependencies"
        for key in ("postgres", "redis"):
            docker("pull", IMAGES[key], timeout=180)
        phase = "private services"
        live = live_action is not None and not config_only
        docker("network", "create", *([] if live else ["--internal"]),
               "--label", f"{LABEL}={owner}", network)
        evidence["egress_policy"] = "live-python-host-allowlist" if live else "internal-key-free"
        password = secrets.token_hex(24)

        def create(name, image, options, command=()):
            docker("create", "--name", name, "--label", f"{LABEL}={owner}",
                   "--network", network, *options, image, *command)

        create(pg, IMAGES["postgres"], ["--network-alias", "pg", "--tmpfs", "/var/lib/postgresql/data",
               "-e", "POSTGRES_USER=readiness", "-e", "POSTGRES_DB=readiness",
               "-e", "POSTGRES_PASSWORD=" + password])
        create(redis, IMAGES["redis"], ["--network-alias", "redis", "--tmpfs", "/data"],
               ["redis-server", "--save", "", "--appendonly", "no"])
        docker("start", pg, redis)
        for name, command in ((pg, ["pg_isready", "-U", "readiness"]), (redis, ["redis-cli", "ping"])):
            for _ in range(60):
                try:
                    docker("exec", name, *command, timeout=5)
                    break
                except RuntimeError:
                    time.sleep(0.5)
            else:
                raise RuntimeError("database/cache health gate timeout")
        docker("exec", pg, "psql", "-U", "readiness", "-d", "readiness", "-v", "ON_ERROR_STOP=1",
               "-c", "CREATE EXTENSION IF NOT EXISTS vector;")
        env = {"PGHOST": "pg", "REDIS_HOST": "redis", "READINESS_RUN": owner,
               "WEBUI_SECRET_KEY": secrets.token_hex(32), "WEBUI_JWT_SECRET_KEY": secrets.token_hex(32),
               "DATABASE_URL": f"postgresql://readiness:{password}@pg:5432/readiness",
               "PGVECTOR_DB_URL": f"postgresql://readiness:{password}@pg:5432/readiness",
               "REDIS_URL": "redis://redis:6379/0", "WEBSOCKET_REDIS_URL": "redis://redis:6379/0"}
        options = ["--tmpfs", "/app/backend/data", "--tmpfs", "/run/readiness"]
        command = ()
        if live_action:
            if not re.fullmatch(r"lathe-ci-next-[a-f0-9]{16}", deployment_label or ""):
                raise RuntimeError("invalid live deployment label")
            env.update(READINESS_EGRESS="live" if live else "preflight", ENABLE_PERSISTENT_CONFIG="true",
                       CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS="8", RAG_EMBEDDING_ENGINE="openai",
                       LATHE_ISOLATED_CI="1", READINESS_SCOPE=scope,
                       LATHE_TEST_DEPLOYMENT_LABEL=deployment_label)
            options += ["--entrypoint", "python"]
            if live:
                options += ["-e", "DAYTONA_API_KEY", "-e", "OPENROUTER_API_KEY"]
            else:
                env.update(DAYTONA_API_KEY="key-free-synthetic", OPENROUTER_API_KEY="key-free-synthetic")
            command = ("/tmp/readiness_lab.py", "--inside", "--scope", scope,
                       *(["--config-only"] if config_only else []))
        for key, value in env.items():
            options += ["-e", f"{key}={value}"]
        create(app, image_id, options, command)
        evidence["staged_sha256"] = stage(app, source)
        if evidence["staged_sha256"] != {"lathe.py": evidence["source_sha256"],
                                        "testing_support.py": evidence["testing_support_sha256"]}:
            raise RuntimeError("qualification inputs changed before staging")
        phase = "OWUI boundaries"
        if live_action:
            for name in ("readiness_lab.py", "monitor_e2e.py", "test_deployment.py", "testing_support.py", "lathe.py"):
                docker("cp", str(source / name), app + ":/tmp/" + name)
        docker("start", app)
        for name in (pg, redis, app):
            info = json.loads(docker("inspect", name))[0]
            if any(m["Type"] != "tmpfs" for m in info["Mounts"]) or info["HostConfig"]["PortBindings"]:
                raise RuntimeError("non-ephemeral mount or host port detected")
        if live_action:
            evidence["live_exit_code"] = live_action(app)
            if evidence["live_exit_code"]:
                raise RuntimeError("live scenario failed")
        else:
            output = docker("exec", app, "python", "/opt/readiness/preflight.py", timeout=300)
            evidence["preflight"] = json.loads(output.strip().splitlines()[-1])
            if any(evidence["preflight"][key] != evidence[key]
                   for key in ("source_sha256", "testing_support_sha256")):
                raise RuntimeError("container qualification input hashes differ from staged bytes")
        evidence["status"] = "passed"
    except BaseException as exc:
        evidence.update(status="failed", phase=phase, failure=f"{type(exc).__name__}: {exc}")
        if phase == "OWUI boundaries":
            try:
                evidence["completed_checks"] = json.loads(docker("exec", app, "cat", "/run/readiness/checks.json"))
            except (RuntimeError, ValueError):
                pass
        raise
    finally:
        signal.alarm(0)
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        # A second interrupt must not strand resources halfway through cleanup.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        primary = sys.exception()
        errors = []
        try:
            evidence["elapsed_seconds"] = round(time.monotonic() - started, 2)
            save(directory / "result.json", evidence)
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                cleanup(directory, manifest=manifest)
            except BaseException as exc:
                errors.append(exc)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if errors:
            # Keep the original failure, evidence-write failure, and cleanup or
            # receipt-write failure visible together instead of masking one.
            if primary is not None:
                errors.insert(0, primary)
            raise BaseExceptionGroup("preflight/finalization failures", errors)
    return evidence


def preflight(directory, source):
    return topology(directory, source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--cleanup", type=Path, metavar="RUN_DIRECTORY")
    parser.add_argument("--artifacts", type=Path, help="new directory for sanitized evidence")
    parser.add_argument("--source", type=Path, default=ROOT.parent)
    args = parser.parse_args()

    def cancelled(signum, frame):
        raise RuntimeError(f"cancelled by signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, cancelled)
    signal.alarm(900)
    if args.cleanup:
        cleanup(args.cleanup.resolve())
    else:
        directory = args.artifacts or ROOT / "runs" / uuid.uuid4().hex
        print(f"Artifacts: {directory.resolve()}", flush=True)
        preflight(directory.resolve(), args.source.resolve())
        print("Infrastructure preflight passed; exact owned resources verified absent.")


if __name__ == "__main__":
    main()

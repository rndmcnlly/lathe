#!/usr/bin/env python3
"""Key-free real Docker initializer barrier, exercising lab.postgres_ready."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import secrets
import tarfile
import threading
import time
import uuid
from unittest.mock import patch

import lab


def trial(directory):
    directory.mkdir(parents=True, exist_ok=False)
    owner = uuid.uuid4().hex
    network = "lathe-preflight-" + owner
    pg = network + "-pg"
    manifest = {"owner": owner, "network": network,
                "containers": [network + "-" + suffix for suffix in ("pg", "redis", "app")]}
    lab.save(directory / "owner.json", manifest)
    evidence = {"image": lab.IMAGES["postgres"], "status": "starting",
                "helper_sha256": hashlib.sha256(Path(lab.__file__).read_bytes()).hexdigest()}
    real_docker = lab.docker
    try:
        lab.docker("pull", lab.IMAGES["postgres"], timeout=180)
        lab.docker("network", "create", "--internal", "--label", f"{lab.LABEL}={owner}", network)
        lab.docker("create", "--name", pg, "--label", f"{lab.LABEL}={owner}", "--network", network,
                   "--tmpfs", "/var/lib/postgresql/data", "-e", "POSTGRES_USER=readiness",
                   "-e", "POSTGRES_DB=readiness", "-e", "POSTGRES_PASSWORD=" + secrets.token_hex(24),
                   lab.IMAGES["postgres"])
        # Copy a bounded barrier into this exact owned container, never mount a
        # host path/volume. The official entrypoint runs it on its temp server.
        fixture = b'''#!/bin/sh
set -eu
touch /tmp/initializer-paused
for i in $(seq 1 45); do
    if [ -f /tmp/initializer-release ]; then exit 0; fi
    sleep 1
done
exit 1
'''
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            entry = tarfile.TarInfo("00-readiness-barrier.sh")
            entry.size, entry.mode = len(fixture), 0o755
            tar.addfile(entry, io.BytesIO(fixture))
        lab.docker("cp", "-", pg + ":/docker-entrypoint-initdb.d/", stdin=archive.getvalue())
        lab.docker("start", pg)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                lab.docker("exec", pg, "test", "-f", "/tmp/initializer-paused", timeout=3)
                break
            except RuntimeError:
                time.sleep(0.1)
        else:
            raise RuntimeError("initializer barrier never reached")
        info = json.loads(lab.docker("inspect", pg))[0]
        if any(m["Type"] != "tmpfs" for m in info["Mounts"]) or info["HostConfig"]["PortBindings"]:
            raise RuntimeError("unexpected mounts or host ports")
        evidence["image_id"] = info["Image"]
        entrypoint = lab.docker("exec", pg, "cat", "/usr/local/bin/docker-entrypoint.sh")
        temp = entrypoint.split("docker_temp_server_start() {", 1)[1].split("\n}", 1)[0]
        if "listen_addresses=''" not in temp:
            raise RuntimeError("pinned temporary server is not explicitly Unix-only")
        evidence.update(entrypoint_sha256=hashlib.sha256(entrypoint.encode()).hexdigest(),
                        unix_only_initializer=True, fixture_sha256=hashlib.sha256(fixture).hexdigest())
        lab.docker("exec", pg, "pg_isready", "-U", "readiness", timeout=3)
        evidence["unix_ready_during_init"] = True
        rejected = threading.Event()
        sql_during_pause = []
        released = threading.Event()

        def observed(*args, **kwargs):
            if "psql" in args and not released.is_set():
                sql_during_pause.append(True)
            try:
                return real_docker(*args, **kwargs)
            except RuntimeError:
                if "pg_isready" in args and "127.0.0.1" in args and not released.is_set():
                    rejected.set()
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch.object(lab, "docker", observed):
                future = executor.submit(lab.postgres_ready, pg, timeout=20)
                try:
                    deadline = time.monotonic() + 5
                    while not rejected.is_set() and not future.done() and time.monotonic() < deadline:
                        rejected.wait(0.05)
                    if not rejected.is_set() or future.done() or sql_during_pause:
                        raise RuntimeError("production gate advanced or failed during initializer pause")
                    evidence["production_tcp_rejected_during_init"] = True
                    evidence["production_did_not_advance_during_init"] = True
                finally:
                    released.set()
                    real_docker("exec", pg, "touch", "/tmp/initializer-release", timeout=3)
                evidence["postgres"] = future.result(timeout=25)
        evidence["status"] = "passed"
    except BaseException:
        evidence["status"] = "failed"
        try:
            evidence["postgres_startup"] = lab.postgres_diagnostics(pg)
        except Exception:
            evidence["postgres_startup"] = {"diagnostics_unavailable": True}
        raise
    finally:
        try:
            lab.save(directory / "result.json", evidence)
        finally:
            lab.cleanup(directory, manifest=manifest)
    return evidence


if __name__ == "__main__":
    directory = lab.ROOT / "runs" / ("postgres-" + uuid.uuid4().hex)
    print("Initializer trial artifacts:", directory, flush=True)
    print(json.dumps(trial(directory)))

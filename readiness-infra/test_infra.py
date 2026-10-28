"""Offline checks for destructive-action boundaries, not Docker simulations."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import subprocess
import errno
import hashlib

import pytest

spec = importlib.util.spec_from_file_location("infra_lab", Path(__file__).with_name("lab.py"))
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


@pytest.fixture
def owned(tmp_path):
    owner = "1" * 32
    network = "lathe-preflight-" + owner
    manifest = {"owner": owner, "network": network,
                "containers": [network + "-" + suffix for suffix in ("pg", "redis", "app")]}
    lab.save(tmp_path / "owner.json", manifest)
    return tmp_path, manifest


@pytest.mark.parametrize("mutation", ["owner", "containers", "network"])
def test_manifest_cannot_target_arbitrary_resources(owned, mutation):
    path, manifest = owned
    manifest[mutation] = "unrelated" if mutation != "containers" else ["unrelated"]
    lab.save(path / "owner.json", manifest)
    with pytest.raises(RuntimeError):
        lab.load_manifest(path)


def test_cleanup_refuses_other_owner(owned, monkeypatch):
    path, manifest = owned
    name = manifest["containers"][0]
    commands = []

    def docker(*args, **kwargs):
        commands.append(args)
        if args[0] == "ps":
            return name + "\n"
        if args[0] == "inspect":
            return json.dumps([{"Name": "/" + name, "Config": {"Labels": {lab.LABEL: "other"}}}])
        pytest.fail("cleanup crossed an ownership boundary")

    monkeypatch.setattr(lab, "docker", docker)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        lab.cleanup(path)
    assert not any(command[0] == "rm" for command in commands)


def test_cleanup_refuses_network_with_foreign_endpoints(owned, monkeypatch):
    path, manifest = owned

    def docker(*args, **kwargs):
        if args[0] == "ps":
            return ""
        if args[:2] == ("network", "ls"):
            return manifest["network"] + "\n"
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Labels": {lab.LABEL: manifest["owner"]}, "Containers": {"foreign": {}}}])
        pytest.fail("network with foreign endpoints must never be removed")

    monkeypatch.setattr(lab, "docker", docker)
    with pytest.raises(RuntimeError, match="foreign endpoints"):
        lab.cleanup(path)


def test_absence_is_verified_and_cleanup_is_idempotent(owned, monkeypatch):
    path, _ = owned
    monkeypatch.setattr(lab, "docker", lambda *args, **kwargs: "")
    lab.cleanup(path)
    lab.cleanup(path)
    assert json.loads((path / "cleanup.json").read_text())["verified_absent"] is True


def test_daemon_failure_is_not_absence(owned, monkeypatch):
    path, _ = owned

    def failed(*args, **kwargs):
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(lab, "docker", failed)
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        lab.cleanup(path)
    assert not (path / "cleanup.json").exists()


def test_staging_is_an_exact_two_file_allowlist(tmp_path, monkeypatch):
    for name in ("lathe.py", "testing_support.py", ".env"):
        (tmp_path / name).write_text(name)
    captured = []
    monkeypatch.setattr(lab, "docker", lambda *args, **kwargs: captured.append(kwargs["stdin"]))
    hashes = lab.stage("owned-app", tmp_path)
    with tarfile.open(fileobj=io.BytesIO(captured[0])) as archive:
        assert archive.getnames() == ["staged/lathe.py", "staged/testing_support.py"]
        assert archive.extractfile("staged/lathe.py").read() == b"lathe.py"
        assert hashes == {name: hashlib.sha256(archive.extractfile("staged/" + name).read()).hexdigest()
                          for name in ("lathe.py", "testing_support.py")}


def test_timeout_does_not_retain_secret_arguments(monkeypatch):
    def timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(lab.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="docker create timed out") as caught:
        lab.docker("create", "-e", "WEBUI_SECRET_KEY=must-not-retain", timeout=1)
    assert "must-not-retain" not in str(caught.value)


@pytest.mark.parametrize("base", ["other@sha256:123", "${OWUI_IMAGE}",
                                  lab.IMAGES["owui"] + "\nFROM another"])
def test_actual_dockerfile_base_must_equal_recorded_image(tmp_path, monkeypatch, base):
    (tmp_path / "Dockerfile").write_text("FROM " + base + "\n")
    monkeypatch.setattr(lab, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="single Dockerfile FROM"):
        lab.validate_images()


def test_shipped_dockerfile_base_matches_image_record():
    lab.validate_images()


def test_controller_rejects_base_drift_before_build(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM wrong-image\n")
    for name in ("lathe.py", "testing_support.py"):
        (tmp_path / name).write_text(name)
    monkeypatch.setattr(lab, "ROOT", tmp_path)

    def docker(*args, **kwargs):
        assert args[0] != "build", "base drift must fail before Docker build"
        assert args[0] == "ps" or args[:2] == ("network", "ls")
        return ""

    monkeypatch.setattr(lab, "docker", docker)
    monkeypatch.setattr(lab.signal, "alarm", lambda *args: None)
    monkeypatch.setattr(lab.signal, "signal", lambda *args: None)
    with pytest.raises(RuntimeError, match="single Dockerfile FROM"):
        lab.preflight(tmp_path / "run", tmp_path)
    assert json.loads((tmp_path / "run" / "cleanup.json").read_text())["verified_absent"]


@pytest.mark.parametrize("write_fault", ["disk-full", "permission", "removed-directory", "receipt-only"])
def test_controller_cleans_owned_resources_despite_evidence_failures(tmp_path, monkeypatch, write_fault):
    """Call the production controller and cleanup, with only Docker/I/O faults replaced."""
    source = tmp_path / "source"
    source.mkdir()
    for name in ("lathe.py", "testing_support.py"):
        (source / name).write_text(name)
    run = tmp_path / "run"
    containers, networks, removed = {}, {}, []
    actual_save = lab.save

    def save(path, value):
        if path.name == "result.json" and write_fault != "receipt-only":
            if write_fault == "removed-directory":
                (run / "owner.json").unlink()
                run.rmdir()
                raise FileNotFoundError("injected removed evidence directory")
            code = errno.ENOSPC if write_fault == "disk-full" else errno.EACCES
            raise OSError(code, "injected result-write failure")
        if path.name == "cleanup.json":
            raise PermissionError("injected cleanup receipt-write failure")
        actual_save(path, value)

    def docker(*args, **kwargs):
        if args[0] == "build":
            manifest = lab.load_manifest(run)
            for name in manifest["containers"]:
                containers[name] = {"Name": "/" + name, "Id": name,
                                    "Config": {"Labels": {lab.LABEL: manifest["owner"]}}}
            networks[manifest["network"]] = {"Id": manifest["network"],
                "Labels": {lab.LABEL: manifest["owner"]}, "Containers": {}}
            raise RuntimeError("injected original build failure")
        if args[0] == "ps":
            return "\n".join(containers)
        if args[0] == "inspect":
            return json.dumps([containers[args[1]]])
        if args[0] == "rm":
            removed.append(containers.pop(args[-1])["Name"])
            return ""
        if args[:2] == ("network", "ls"):
            return "\n".join(networks)
        if args[:2] == ("network", "inspect"):
            return json.dumps([networks[args[-1]]])
        if args[:2] == ("network", "rm"):
            removed.append(networks.pop(args[-1])["Id"])
            return ""
        pytest.fail(f"unexpected Docker operation: {args[0]}")

    monkeypatch.setattr(lab, "save", save)
    monkeypatch.setattr(lab, "docker", docker)
    monkeypatch.setattr(lab.signal, "alarm", lambda *args: None)
    monkeypatch.setattr(lab.signal, "signal", lambda *args: None)
    with pytest.raises(BaseExceptionGroup) as caught:
        lab.preflight(run, source)
    failures = caught.value.exceptions
    assert "injected original build failure" in str(failures[0])
    assert "injected cleanup receipt-write failure" in str(failures[-1])
    assert len(failures) == (2 if write_fault == "receipt-only" else 3)
    assert not containers and not networks and len(removed) == 4


@pytest.mark.parametrize("callback_failure", [RuntimeError("cloud callback failed"), PermissionError("evidence export failed")])
@pytest.mark.parametrize("failure_phase", ["controller", "postgres", "diagnostics", "oracle-write"])
def test_shared_lifecycle_always_cleans_after_controller_failure(tmp_path, monkeypatch, callback_failure, failure_phase):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("lathe.py", "testing_support.py", "readiness_lab.py", "monitor_e2e.py", "test_deployment.py"):
        (source / name).write_text(name)
    containers, networks = {}, {}
    def docker(*args, **kwargs):
        if "psql" in args:
            return "CREATE EXTENSION\n127.0.0.1|5432|1\n"
        if args[:2] == ("network", "create"):
            owner = args[args.index("--label") + 1].split("=")[1]
            networks[args[-1]] = {"Id": args[-1], "Labels": {lab.LABEL: owner}, "Containers": {}}
        elif args[0] == "create":
            name = args[args.index("--name") + 1]
            owner = args[args.index("--label") + 1].split("=")[1]
            containers[name] = {"Id": name, "Name": "/" + name,
                                "Config": {"Labels": {lab.LABEL: owner}}, "Mounts": [],
                                "HostConfig": {"PortBindings": {}}}
        elif args[0] == "inspect":
            return json.dumps([containers[args[1]]])
        elif args[0] == "ps":
            return "\n".join(containers)
        elif args[:2] == ("network", "ls"):
            return "\n".join(networks)
        elif args[:2] == ("network", "inspect"):
            return json.dumps([networks[args[-1]]])
        elif args[0] == "rm":
            containers.pop(args[-1])
        elif args[:2] == ("network", "rm"):
            networks.pop(args[-1])
        elif args[0] not in {"pull", "start", "exec", "cp"}:
            pytest.fail("unexpected Docker operation " + args[0])
        return ""
    def action(app):
        assert app in containers
        if failure_phase == "oracle-write":
            import asyncio
            import httpx
            spec = importlib.util.spec_from_file_location("oracle_evidence", Path(__file__).with_name("evidence.py"))
            evidence = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(evidence)
            original = Path.write_text
            def write(path, *args, **kwargs):
                if path.name == "native_idle-canary.json":
                    raise callback_failure
                return original(path, *args, **kwargs)
            monkeypatch.setattr(Path, "write_text", write)
            async def oracle():
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                        httpx.Response(200, stream=httpx.ByteStream(b"a" * 32)))) as http:
                    await evidence.retain_canary(http, sandbox="11111111-1111-1111-1111-111111111111",
                        source="/home/daytona/next-" + "a" * 32 + ".child",
                        copied="/home/daytona/next-" + "a" * 32 + ".continued", headers={},
                        scenario="native_idle", directory=tmp_path / "oracle")
            asyncio.run(oracle())
            pytest.fail("actual canary receipt persistence did not fail")
        raise callback_failure
    monkeypatch.setattr(lab, "docker", docker)
    if failure_phase in ("postgres", "diagnostics"):
        def failed_gate(pg):
            assert pg in containers
            raise callback_failure
        def diagnostics(pg):
            assert pg in containers, "startup evidence must precede teardown"
            if failure_phase == "diagnostics":
                raise PermissionError("injected diagnostic failure")
            return {"observed_before_teardown": True}
        monkeypatch.setattr(lab, "postgres_ready", failed_gate)
        monkeypatch.setattr(lab, "postgres_diagnostics", diagnostics)
    with pytest.raises(type(callback_failure), match=str(callback_failure)):
        lab.topology(tmp_path / "run", source, image_id="sha256:qualified", live_action=action,
                     deployment_label="lathe-ci-next-0123456789abcdef")
    assert not containers and not networks
    assert json.loads((tmp_path / "run/cleanup.json").read_text())["verified_absent"]
    if failure_phase in ("postgres", "diagnostics"):
        result = json.loads((tmp_path / "run/result.json").read_text())
        assert result["phase"] == "PostgreSQL startup"
        assert result["postgres_startup"] == ({"diagnostics_unavailable": True} if failure_phase == "diagnostics"
                                               else {"observed_before_teardown": True})


@pytest.fixture
def postgres_clock(monkeypatch):
    from types import SimpleNamespace
    now = [0.0]
    def advance(seconds):
        now[0] += seconds
    monkeypatch.setattr(lab, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=advance))


def test_postgres_gate_ignores_unix_initializer_and_executes_tcp_sql(monkeypatch, postgres_clock):
    commands = []
    def docker(*args, **kwargs):
        commands.append(args)
        # An omitted host would accept the temporary Unix server immediately.
        if "pg_isready" in args and sum("pg_isready" in c for c in commands) == 1:
            assert args[args.index("-h") + 1] == "127.0.0.1"
            raise RuntimeError("temporary server has no TCP listener")
        if "psql" in args:
            return "CREATE EXTENSION\n127.0.0.1|5432|1\n"
        return ""
    monkeypatch.setattr(lab, "docker", docker)
    assert lab.postgres_ready("owned-pg")["sql_tcp_verified"]
    assert len(commands) == 3
    for args in commands:
        assert args[args.index("-h") + 1] == "127.0.0.1"
        assert args[args.index("-p") + 1] == "5432"


@pytest.mark.parametrize("fault", ["sql-fails", "unix-sql", "missing-vector", "empty-output"])
def test_postgres_gate_rejects_sql_faults_without_retry(monkeypatch, fault):
    calls = []
    def docker(*args, **kwargs):
        if "psql" in args:
            calls.append(args)
            if fault == "sql-fails":
                raise RuntimeError("injected SQL failure")
            return {"unix-sql": "|5432|1", "missing-vector": "127.0.0.1|5432|0",
                    "empty-output": ""}[fault]
        return ""
    monkeypatch.setattr(lab, "docker", docker)
    with pytest.raises(RuntimeError):
        lab.postgres_ready("owned-pg")
    assert len(calls) == 1


def test_postgres_tcp_timeout_never_attempts_sql(monkeypatch, postgres_clock):
    def docker(*args, **kwargs):
        assert "pg_isready" in args and "psql" not in args
        raise RuntimeError("no final listener")
    monkeypatch.setattr(lab, "docker", docker)
    with pytest.raises(RuntimeError, match="final TCP readiness timeout"):
        lab.postgres_ready("owned-pg", timeout=5)


def test_postgres_diagnostics_emit_only_bounded_allowlisted_facts(monkeypatch):
    secret = "SECRET_SQL_AND_PASSWORD"
    def docker(*args, **kwargs):
        if args[0] == "inspect":
            return json.dumps({"Running": True, "OOMKilled": False, "ExitCode": 0, "Error": secret})
        assert args[:3] == ("logs", "--tail", "60") and kwargs["timeout"] == 5
        return "listening on Unix socket " + secret + "\nERROR: " + secret
    monkeypatch.setattr(lab, "docker", docker)
    result = lab.postgres_diagnostics("owned-pg")
    assert secret not in json.dumps(result)
    assert result["tail_events"]["unix_listener"] == result["tail_events"]["error"] == 1


def test_manual_preflight_cannot_inherit_credentials_or_enter_live_run():
    import yaml
    workflow = yaml.safe_load((lab.ROOT.parent / ".github/workflows/monitor.yml").read_text())
    jobs = workflow["jobs"]
    assert "inputs.lab != 'readiness-preflight'" in jobs["monitor"]["if"]
    readiness = jobs["readiness"]
    assert "env" not in workflow and "env" not in readiness
    preflight_found = False
    for step in readiness["steps"]:
        # Both credential injection and live controller invocation require the
        # other explicit selector, including last-resort cloud cleanup.
        if "secrets." in json.dumps(step) or "readiness_lab.py --run" in step.get("run", ""):
            assert step["if"] in ("inputs.lab == 'readiness'", "always() && inputs.lab == 'readiness'")
        if "readiness_lab.py --preflight" in step.get("run", ""):
            assert step["if"] == "inputs.lab == 'readiness-preflight'"
            assert "env" not in step and "secrets." not in json.dumps(step)
            preflight_found = True
    assert preflight_found


@pytest.mark.parametrize("policy", ["preflight", "live"])
def test_python_egress_denials_are_observable(tmp_path, monkeypatch, policy):
    import socket
    spec = importlib.util.spec_from_file_location("infra_guard", Path(__file__).with_name("guard.py"))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    monkeypatch.setenv("PGHOST", "pg")
    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("READINESS_EGRESS", policy)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))])
    # Track these attributes before production instrumentation changes them.
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket.socket, "connect_ex", socket.socket.connect_ex)
    original_open = Path.open
    def opened(path, *args, **kwargs):
        if str(path) == "/run/readiness/egress-denied":
            path = tmp_path / "denied"
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    guard.install_egress_guard()
    with pytest.raises(OSError, match="forbids external"):
        socket.getaddrinfo("not-allowed.invalid", 443)
    with socket.socket() as sock:
        with pytest.raises(OSError, match="forbids external"):
            sock.connect(("203.0.113.1", 443))
    attempts = [json.loads(line) for line in (tmp_path / "denied").read_text().splitlines()]
    assert [(item["operation"], item["host"]) for item in attempts] == [
        ("lookup", "not-allowed.invalid"), ("connect", "203.0.113.1")]


@pytest.fixture
def transport_modules(monkeypatch):
    import sys
    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    guard = load("infra_guard", "guard.py")
    monkeypatch.setitem(sys.modules, "guard", guard)
    return guard, load("transport_preflight", "transport_preflight.py")


@pytest.mark.parametrize("host,expected", [
    ("APP.DAYTONA.IO.", "app.daytona.io"), (b"APP.DAYTONA.IO.", "app.daytona.io"),
    ("app.daytona.io", "app.daytona.io"), (b"app.daytona.io", "app.daytona.io"),
    ("bücher.invalid", "xn--bcher-kva.invalid"), (b"xn--bcher-kva.invalid.", "xn--bcher-kva.invalid"),
    (b"127.0.0.1", "127.0.0.1"), ("0:0:0:0:0:0:0:1", "::1"),
    ("app.daytona.io.attacker.invalid", "app.daytona.io.attacker.invalid"),
])
def test_hostname_normalization_preserves_exact_dns_identity(transport_modules, host, expected):
    guard, _ = transport_modules
    assert guard.canonical_host(host) == expected


@pytest.mark.parametrize("host", [b"app.daytona.io\xff", b"app.daytona.io\x00", "app.daytona.io..",
                                "app.daytona.io%00", " app.daytona.io", "app.daytona.io:443",
                                "user@app.daytona.io", "app_daytona.io", b"", 123])
def test_malformed_hostname_representations_are_not_coerced(transport_modules, host):
    guard, _ = transport_modules
    with pytest.raises((ValueError, UnicodeError)):
        guard.canonical_host(host)


@pytest.mark.asyncio
async def test_actual_httpx_live_policy_transport_with_loopback_dns_only(transport_modules):
    import socket
    guard, transport = transport_modules
    hooks = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    receipt = await transport.qualify()
    assert receipt["status"] == "passed" and receipt["synthetic_loopback_only"]
    assert receipt["platform_acceptance"] is False and receipt["provider_requests"] == 0
    assert "bytes" in receipt["httpx_lookup_input_types"]
    assert receipt["binary_download_verified"] and len(receipt["http_requests"]) == 4
    assert [x["reason"] for x in receipt["observed_denials"]] == [
        "not_allowed", "not_allowed", "not_allowed", "not_allowed", "malformed_hostname", "malformed_hostname"]
    assert (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex) == hooks


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["bytes-regression", "suffix-policy-bypass"])
async def test_httpx_gate_detects_representation_and_policy_faults(transport_modules, monkeypatch, fault):
    import httpx
    guard, transport = transport_modules
    canonical = guard.canonical_host
    def broken(host):
        if fault == "bytes-regression" and isinstance(host, bytes):
            raise ValueError("injected missing bytes support")
        value = canonical(host)
        return value.removesuffix(".attacker.invalid") if fault == "suffix-policy-bypass" else value
    monkeypatch.setattr(guard, "canonical_host", broken)
    if fault == "bytes-regression":
        with pytest.raises(httpx.ConnectError, match="malformed hostname representation"):
            await transport.qualify()
    else:
        with pytest.raises(RuntimeError, match="forbidden fixture request was admitted"):
            await transport.qualify()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["preflight", "live-worker"])
async def test_httpx_transport_keeps_preflight_and_worker_policy_restricted(transport_modules, monkeypatch, policy):
    import os
    import httpx
    from unittest.mock import patch
    guard, transport = transport_modules
    def restricted(**kwargs):
        with patch.dict(os.environ, {"READINESS_EGRESS": "preflight" if policy == "preflight" else "live"}):
            guard.install_egress_guard(controller=policy != "live-worker", denied_path=kwargs["denied_path"])
    monkeypatch.setattr(transport, "install_egress_guard", restricted)
    # Preflight must reject the first Daytona request. A live worker may reach
    # Daytona, but not the controller-only OpenRouter fixture request.
    with pytest.raises(httpx.ConnectError, match="forbids external network access"):
        await transport.qualify()

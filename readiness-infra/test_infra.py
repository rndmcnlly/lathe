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
def test_shared_lifecycle_always_cleans_after_controller_failure(tmp_path, monkeypatch, callback_failure):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("lathe.py", "testing_support.py", "readiness_lab.py", "monitor_e2e.py", "test_deployment.py"):
        (source / name).write_text(name)
    containers, networks = {}, {}
    def docker(*args, **kwargs):
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
        raise callback_failure
    monkeypatch.setattr(lab, "docker", docker)
    with pytest.raises(type(callback_failure), match=str(callback_failure)):
        lab.topology(tmp_path / "run", source, image_id="sha256:qualified", live_action=action,
                     deployment_label="lathe-ci-next-0123456789abcdef")
    assert not containers and not networks
    assert json.loads((tmp_path / "run/cleanup.json").read_text())["verified_absent"]


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

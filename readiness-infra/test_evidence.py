"""Offline harness replay: no OWUI dispatch, simulated model or platform calls."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("readiness_evidence", Path(__file__).with_name("evidence.py"))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.fixture
def replay(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/native-idle.json").read_text())
    expected = {}
    # Synthetic startup receipts use the production writer and retained process
    # identity shapes. Native records below are actual retained events, not dispatches.
    for name, identity in fixture["identities"].items():
        start = evidence.append_observer(identity, "worker_start", directory=tmp_path)
        expected[name] = {**identity, "observer_start": start}
    for record in fixture["records"]:
        path = tmp_path / f"worker-{record['worker']}.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")
    return fixture, expected, tmp_path


def test_real_idle_shape_with_active_A_and_zero_completion_B(replay):
    fixture, expected, directory = replay
    records = evidence.collect_observers(expected, directory=directory)
    assert len([r for r in records if r["worker"] == "B"]) == 1
    assert evidence.verify_native_context(records, **fixture["context"])["worker"] == "A"


def test_replacement_preserves_history_and_requires_current_start(replay):
    fixture, expected, directory = replay
    before = copy.deepcopy(expected["B"])
    new = {"worker": "B", "pid": 64, "incarnation": "a" * 32}
    start = evidence.append_observer(new, "worker_start", previous_pid=51, directory=directory)
    expected["B"] = {**new, "observer_start": start}
    records = evidence.collect_observers(expected, directory=directory)
    assert [r["pid"] for r in records if r["worker"] == "B"] == [51, 64]
    assert evidence.verify_native_context(records, **fixture["context"])["pid"] == 34
    with pytest.raises(RuntimeError, match="current identity mismatch"):
        evidence.read_observer(before, directory=directory)
    # Removing a complete old startup line is still a history failure, not a
    # valid idle/current stream simply because its last JSON line is intact.
    (directory / "worker-B.jsonl").write_text(json.dumps(start) + "\n")
    with pytest.raises(RuntimeError, match="startup history"):
        evidence.collect_observers(expected, directory=directory)


def test_replacement_cannot_recreate_missing_or_wrong_prior_stream(replay):
    _, _, directory = replay
    new = {"worker": "B", "pid": 64, "incarnation": "a" * 32}
    with pytest.raises(RuntimeError, match="previous worker PID"):
        evidence.append_observer(new, "worker_start", previous_pid=123, directory=directory)
    (directory / "worker-B.jsonl").unlink()
    with pytest.raises(FileNotFoundError):
        evidence.append_observer(new, "worker_start", previous_pid=51, directory=directory)


@pytest.mark.parametrize("fault", ["missing", "empty", "truncated", "json", "wrong-worker", "wrong-incarnation", "wrong-start", "stale-event"])
def test_bad_streams_are_not_silently_empty(replay, fault):
    _, expected, directory = replay
    path = directory / "worker-B.jsonl"
    if fault == "missing":
        path.unlink()
    elif fault == "empty":
        path.write_bytes(b"")
    elif fault == "truncated":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    elif fault == "json":
        path.write_text("{bad}\n")
    elif fault == "wrong-start":
        expected["B"]["observer_start"]["time"] += 1
    else:
        record = json.loads(path.read_text())
        if fault == "wrong-worker":
            record["worker"] = "A"
        elif fault == "wrong-incarnation":
            record["incarnation"] = "b" * 32
        else:
            record.update(kind="http_completion", incarnation="b" * 32, status=200)
        path.write_text(path.read_text() + json.dumps(record) + "\n")
    with pytest.raises((OSError, ValueError, RuntimeError)):
        evidence.collect_observers(expected, directory=directory)


def test_collection_io_error_is_not_an_idle_worker(replay, monkeypatch):
    _, expected, directory = replay
    original = Path.open
    def opened(path, *args, **kwargs):
        if path == directory / "worker-B.jsonl":
            raise PermissionError("injected unreadable observer")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(PermissionError, match="unreadable observer"):
        evidence.collect_observers(expected, directory=directory)


@pytest.mark.parametrize("fault", ["parent", "initial", "delegation", "model", "tool", "system_hash", "child", "human-resume"])
def test_replayed_association_context_and_no_human_resubmission(replay, fault):
    fixture, expected, directory = replay
    context = copy.deepcopy(fixture["context"])
    if fault in ("parent", "initial", "delegation"):
        context["association"][{"parent": "parent_chat_id", "initial": "parent_message_id", "delegation": "delegation_id"}[fault]] = "wrong"
    elif fault == "human-resume":
        context["submitted"].append(context["continuation"])
    else:
        context[fault] = "wrong"
    with pytest.raises(RuntimeError):
        evidence.verify_native_context(evidence.collect_observers(expected, directory=directory), **context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "different", "short", "long", "nonhex", "path", "pair", "write", "length", "encoding"])
async def test_bounded_independent_canary_download_receipts(replay, monkeypatch, fault):
    fixture, expected, directory = replay
    canary = fixture["canary"]
    payload = canary["retained_read_payload"].encode()
    source, copied = canary["source"], canary["copied"]
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["accept-encoding"] == "identity"
        body = payload
        if request.url.params["path"] == copied:
            body = {"different": b"f" * 32, "short": b"a", "long": b"a" * 33, "nonhex": b"s" * 32}.get(fault, body)
        headers = {"content-length": "33"} if fault == "length" else {"content-encoding": "gzip"} if fault == "encoding" else {}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
    if fault == "path":
        source = "/home/daytona/.env"
    if fault == "pair":
        copied = "/home/daytona/next-" + "f" * 32 + ".continued"
    if fault == "write":
        original = Path.write_text
        def write(path, *args, **kwargs):
            if path.name == "native_idle-canary.json":
                raise PermissionError("injected canary receipt failure")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", write)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        call = evidence.retain_canary(http, sandbox=canary["sandbox"], source=source, copied=copied,
                                      headers={"Authorization": "Bearer offline-secret"}, scenario="native_idle", directory=directory)
        if fault:
            with pytest.raises((RuntimeError, PermissionError)):
                await call
        else:
            await call
    receipt = directory / "native_idle-canary.json"
    if fault in (None, "different"):
        record = json.loads(receipt.read_text())
        assert record["equal"] is (fault is None)
        for file in record["files"]:
            data = base64.b64decode(file["base64"], validate=True)
            assert len(data) == file["byte_count"] == 32 and hashlib.sha256(data).hexdigest() == file["sha256"]
        assert "offline-secret" not in receipt.read_text()
        if fault is None:
            # A subsequent collector error cannot remove the already durable oracle.
            (directory / "worker-B.jsonl").unlink()
            with pytest.raises(FileNotFoundError):
                evidence.collect_observers(expected, directory=directory)
            assert receipt.exists()
    else:
        assert not receipt.exists()
    if fault in ("path", "pair"):
        assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "missing-B", "wrong-context"])
async def test_production_collection_pipeline_persists_oracle_before_observer_failure(replay, fault):
    fixture, expected, directory = replay
    context = copy.deepcopy(fixture["context"])
    canary = fixture["canary"]
    payload = canary["retained_read_payload"].encode()
    if fault == "missing-B":
        (directory / "worker-B.jsonl").unlink()
    elif fault == "wrong-context":
        context["tool"] = "wrong"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(payload)))) as http:
        operation = evidence.collect_native_evidence(http, expected=expected, context=context, directory=directory,
            canary=dict(sandbox=canary["sandbox"], source=canary["source"], copied=canary["copied"],
                        headers={}, scenario="native_idle"))
        if fault:
            with pytest.raises((FileNotFoundError, RuntimeError)):
                await operation
        else:
            oracle, entry = await operation
            assert oracle["equal"] and entry["message_id"] == context["continuation"]
    receipt = json.loads((directory / "native_idle-canary.json").read_text())
    assert receipt["equal"] and all(base64.b64decode(file["base64"]) == payload for file in receipt["files"])

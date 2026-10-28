"""Small lab-only observer and canary receipts, never a dispatch mechanism."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

DIRECTORY = Path("/lab/artifacts")
IDENTITY = ("worker", "pid", "incarnation")
MAX_STREAM_BYTES = 1_000_000


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def identity_of(record):
    value = {key: record[key] for key in IDENTITY}
    require(value["worker"] in ("A", "B") and type(value["pid"]) is int and value["pid"] > 0
            and isinstance(value["incarnation"], str) and re.fullmatch(r"[0-9a-f]{32}", value["incarnation"]),
            "invalid observer identity")
    return value


def append_observer(identity, kind, *, directory=None, previous_pid=None, **data):
    directory = DIRECTORY if directory is None else directory
    identity = identity_of(identity)
    require(not set(data).intersection({"kind", "time", *IDENTITY}), "observer identity fields cannot be overridden")
    path = directory / f"worker-{identity['worker']}.jsonl"
    record = {"kind": kind, "time": time.time(), **identity, **data}
    mode = "r+"
    if kind == "worker_start":
        directory.mkdir(parents=True, exist_ok=True)
        if previous_pid is None:
            mode = "x"  # First startup must not silently adopt an old artifact.
            record["previous_incarnation"] = None
        else:
            _, previous = _read_stream(identity["worker"], directory)
            require(previous["pid"] == previous_pid, "replacement previous worker PID mismatch")
            record["previous_incarnation"] = previous["incarnation"]
    # Non-start events cannot silently recreate a deleted startup stream.
    with path.open(mode) as stream:
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(record) + "\n")
    return record


def _read_stream(name, directory):
    path = directory / f"worker-{name}.jsonl"
    with path.open("rb") as stream:
        raw = stream.read(MAX_STREAM_BYTES + 1)
    require(0 < len(raw) <= MAX_STREAM_BYTES and raw.endswith(b"\n"), "empty, oversized or truncated observer stream")
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    current, starts = None, set()
    for record in records:
        observed = identity_of(record)
        require(observed["worker"] == name, "wrong worker in observer stream")
        timestamp = record.get("time")
        require(type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp > 0, "invalid observer timestamp")
        if record["kind"] == "worker_start":
            require(record.get("previous_incarnation") == (current["incarnation"] if current else None),
                    "missing or reordered observer startup history")
            require(observed["incarnation"] not in starts, "reused observer incarnation")
            starts.add(observed["incarnation"])
            current = record
        else:
            require(record["kind"] in ("http_completion", "native_handler_entry"), "unknown observer record")
            require(current is not None and observed == identity_of(current), "event without matching startup/incarnation")
    require(current is not None, "observer startup missing")
    return records, current


def read_observer(expected, *, directory=None):
    directory = DIRECTORY if directory is None else directory
    identity = identity_of(expected)
    records, current = _read_stream(identity["worker"], directory)
    require(identity_of(current) == identity, "observer current identity mismatch")
    require(expected.get("observer_start") == current, "HTTP identity/start receipt does not match observer stream")
    return records


def collect_observers(expected, *, directory=None):
    require(set(expected) == {"A", "B"}, "both worker identities required")
    return [record for name in ("A", "B")
            for record in read_observer(expected[name], directory=directory)]


def select_continuation(messages, delegation):
    """The one server-owned resumed parent reply: None while pending, never first-of-many."""
    results = {key for key, message in messages.items() if message.get("role") == "user"
               and (message.get("meta") or {}).get("delegation_id") == delegation}
    resumed = [message for message in messages.values()
               if message.get("role") == "assistant" and message.get("parentId") in results]
    require(len(resumed) <= 1, "multiple resumed continuations for one delegation")
    return resumed[0] if resumed and resumed[0].get("done") is True else None


def verify_native_context(records, *, parent, child, initial, continuation, child_messages,
                          association, delegation, submitted, model, tool, system_hash):
    require(association.get("parent_chat_id") == parent and association.get("parent_message_id") == initial
            and association.get("delegation_id") == delegation and association.get("internal") is True
            and association.get("type") == "subagent" and association.get("mode") == "background",
            "native child association mismatch")
    require(initial in submitted and continuation not in submitted, "continuation was a submitted human request")
    entries = [record for record in records if record["kind"] == "native_handler_entry"]
    # Worker-side check too: every parent-chat handler entry the controller did
    # not submit must be this single continuation, whatever the saved chat shows.
    resumed = {entry.get("message_id") for entry in entries
               if entry.get("chat_id") == parent and entry.get("message_id") not in submitted}
    require(resumed == {continuation}, "missing or multiple resumed parent continuations observed")
    matches = [entry for entry in entries if entry.get("message_id") == continuation]
    require(len(matches) == 1, "missing or ambiguous native continuation observation")
    entry = matches[0]
    require(entry.get("chat_id") == parent and entry.get("model") == model
            and entry.get("tool_ids") == [tool] and entry.get("system_hash") == system_hash,
            "continuation lost actual association/model/tool/system context")
    require(any(e.get("chat_id") == child and e.get("message_id") in child_messages
                and e.get("internal") is True and e.get("model") == model and e.get("tool_ids") == [tool]
                for e in entries), "missing associated native child observation")
    return entry


async def retain_canary(http, *, sandbox, source, copied, headers, scenario, directory=None):
    """Only this controller's paired synthetic hex canaries may cross to evidence."""
    directory = DIRECTORY if directory is None else directory
    require(scenario in ("native_idle", "native_busy"), "invalid canary scenario")
    require(isinstance(sandbox, str) and str(uuid.UUID(sandbox)) == sandbox, "invalid canary sandbox ID")
    match = re.fullmatch(r"/home/daytona/next-([0-9a-f]{32})\.child", source)
    require(match is not None and copied == source.removesuffix(".child") + ".continued", "invalid synthetic canary paths")
    files, payloads = [], []
    for path in (source, copied):
        async with http.stream("GET", f"https://proxy.app.daytona.io/toolbox/{sandbox}/files/download",
                               params={"path": path}, headers={**headers, "Accept-Encoding": "identity"}) as response:
            response.raise_for_status()
            require(response.headers.get("content-encoding", "identity") == "identity", "unexpected canary encoding")
            length = response.headers.get("content-length")
            require(length is None or length == "32", "unexpected canary content length")
            payload = bytearray()
            async for chunk in response.aiter_raw(chunk_size=33):
                payload.extend(chunk)
                require(len(payload) <= 32, "oversized canary download")
        data = bytes(payload)
        require(re.fullmatch(rb"[0-9a-f]{32}", data) is not None, "invalid 32-byte synthetic canary")
        payloads.append(data)
        files.append({"sandbox_id": sandbox, "path": path, "byte_count": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "base64": base64.b64encode(data).decode("ascii")})
    receipt = {"scenario": scenario, "synthetic_canary_only": True, "equal": payloads[0] == payloads[1], "files": files}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{scenario}-canary.json").write_text(json.dumps(receipt, indent=2) + "\n")
    require(receipt["equal"], "independent native file oracle mismatch (bounded receipt retained)")
    return receipt


async def collect_native_evidence(http, *, canary, expected, context, check=None, directory=None):
    """Persist the canary BEFORE behavior traces/assertions or observer collection.

    `check` is the controller's model/tool-iteration diagnosis; it returns extra
    context. If the oracle fails, the check still runs so a missing copy is
    reported alongside why (e.g. the model never called read), not instead of it.
    """
    try:
        oracle = await retain_canary(http, **canary, directory=directory)
    except Exception as failure:
        if check is not None:
            try:
                await check()
            except Exception as diagnosis:
                raise ExceptionGroup("native oracle and behavior failures", [failure, diagnosis]) from None
        raise
    if check is not None:
        context = {**context, **await check()}
    records = collect_observers(expected, directory=directory)
    return oracle, verify_native_context(records, **context)

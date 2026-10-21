"""Independent toolkit processes, real sandbox-side processes, no Daytona HTTP.

Only provisioning and the HTTP execution boundary are replaced. Every command
executes the embedded production resolver/supervisor against a shared filesystem.
"""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

import pytest

import lathe


WORKER = r'''
import asyncio, json, shlex, sys
from pathlib import Path
import httpx
import lathe

options = json.load(sys.stdin)
tools = lathe.Tools()
tools.valves.deployment_label = options["deployment"]
tools.valves.daytona_proxy_url = "https://synthetic.test"
tools.valves.daytona_api_key = "never-in-sandbox"
if options.get("fault"):
    old, new = options["fault"]
    assert old in lathe._INTERPRETER_SCRIPT
    lathe._INTERPRETER_SCRIPT = lathe._INTERPRETER_SCRIPT.replace(old, new)
original_paths = lathe._interpreter_paths
def paths(valves, sandbox_id, user_identity, chat_id):
    durable, _ = original_paths(valves, sandbox_id, user_identity, chat_id)
    key = Path(durable).name
    return str(Path(options["root"]) / "data" / key), str(Path(options["root"]) / "run" / key[:32])
lathe._interpreter_paths = paths
builder = lathe._build_interpreter_rpc_script
lathe._build_interpreter_rpc_script = lambda durable, runtime, request: builder(durable, runtime, request, workspace=options["root"])
async def sandbox(*args):
    return options["sandbox"], None
async def orientation(*args):
    pass
lathe._ensure_sandbox = sandbox
lathe._ensure_chat_init = orientation
violations = []
async def execute(request):
    if request.method != "POST" or request.url.path != "/" + options["sandbox"] + "/process/execute":
        violations.append(str(request.url))
        raise AssertionError("unexpected HTTP")
    args = shlex.split(json.loads(request.content)["command"])
    assert args[:2] == ["python3", "-c"]
    assert "never-in-sandbox" not in args[2]
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", args[2], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await child.communicate()
    return httpx.Response(200, json={"exitCode": child.returncode, "result": stdout.decode() + stderr.decode()})
constructor = httpx.AsyncClient
lathe.httpx.AsyncClient = lambda *args, **kw: constructor(*args, **kw, transport=httpx.MockTransport(execute))
async def main():
    result = await tools.interpret(options["code"], timeout=options["timeout"], __chat_id__=options["chat"], __user__={"email": options["user"], "id": options["user"]})
    assert not violations, violations
    print(json.dumps(result))
asyncio.run(main())
'''


@pytest.fixture
def sandbox_python():
    # AF_UNIX paths on macOS are short. The default pytest path exceeds the
    # kernel limit, so this fixture needs a short temporary runtime directory.
    with tempfile.TemporaryDirectory(prefix="lathe-ip-", dir="/tmp") as root:
        clients = []
        service_pids = set()

        def remember():
            for path in Path(root).glob("run/*/process.json"):
                record = json.loads(path.read_text())
                service_pids.update(pid for pid in record.values() if pid)

        def paths(deployment="test", user="owner@example.test", chat="a", sandbox="sb"):
            valves = lathe.Tools.Valves(deployment_label=deployment)
            durable, _ = lathe._interpreter_paths(valves, sandbox, user, chat)
            key = Path(durable).name
            return Path(root) / "data" / key, Path(root) / "run" / key[:32]

        async def start(code, **kwargs):
            options = dict(root=root, code=code, deployment="test", user="owner@example.test", chat="a", sandbox="sb", timeout=120)
            options.update(kwargs)
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", WORKER, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            clients.append(child)
            child.stdin.write(json.dumps(options).encode())
            await child.stdin.drain()
            child.stdin.close()
            return child

        async def finish(child):
            stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
            remember()
            assert child.returncode == 0, stderr.decode()
            return json.loads(stdout)

        async def call(code, **kwargs):
            return await finish(await start(code, **kwargs))

        async def rpc(request, **kwargs):
            durable, runtime = paths(**kwargs)
            script = lathe._build_interpreter_rpc_script(str(durable), str(runtime), request, workspace=root)
            child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
            remember()
            assert child.returncode == 0, stderr.decode()
            return json.loads(stdout)

        async def until(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if predicate():
                    remember()
                    return
                await asyncio.sleep(0.025)
            pytest.fail("sandbox condition did not become observable")

        yield SimpleNamespace(root=Path(root), paths=paths, start=start, finish=finish,
                              call=call, rpc=rpc, until=until, remember=remember)
        remember()
        for child in clients:
            if child.returncode is None:
                child.kill()
        # These are fixture-owned daemons, never discovered by broad process
        # names. SIGTERM lets supervisors reap their Python subprocesses.
        for path in Path(root).glob("run/*/process.json"):
            pid = json.loads(path.read_text())["supervisor"]
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(0.1)
        for pid in service_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def test_interpreter_independent_workers_and_reloads(sandbox_python):
    lab = sandbox_python
    assert await lab.call("x = 35") == "(no output)"
    assert await lab.call("x += 1; print(x)") == "36"
    assert await lab.call("print(x)") == "36"
    # Every call above imports a fresh toolkit in a distinct OS process.
    durable, _ = lab.paths()
    records = [json.loads(p.read_text()) for p in (durable / "runs").glob("*.json")]
    assert len({r["generation"] for r in records}) == 1


async def test_interpreter_simultaneous_first_use(sandbox_python):
    lab = sandbox_python
    code = "x = globals().get('x', 0) + 1; print(x)"
    results = await asyncio.gather(lab.call(code), lab.call(code))
    # Admission is exclusive: a competing caller may complete next or get busy,
    # but must never execute in a second, unpublished interpreter.
    successes = [r for r in results if not r.startswith("Error:")]
    assert sorted(successes) in (["1"], ["1", "2"])
    assert all(r in successes or "Session is executing run" in r for r in results)
    assert await lab.call("print(x)") == str(len(successes))


async def test_interpreter_stale_initializer_cannot_replace_live_owner(sandbox_python):
    lab = sandbox_python
    await lab.call("x = 35")
    durable, runtime = lab.paths()
    before = (durable / "state.json").read_bytes()
    # A bootstrap process paused before acquiring the lifetime lock may resume
    # after another initializer wins. Execute that actual service entry point.
    script = (lathe._INTERPRETER_SCRIPT + "\nserve(" + repr(lathe._INTERPRETER_SCRIPT)
              + ", Path(" + repr(str(durable)) + "), Path(" + repr(str(runtime)) + "), " + repr(str(lab.root)) + ")")
    contender = await asyncio.create_subprocess_exec(sys.executable, "-c", script)
    await asyncio.wait_for(contender.wait(), 5)
    assert contender.returncode == 0
    assert (durable / "state.json").read_bytes() == before
    assert await lab.call("print(x)") == "35"


@pytest.mark.parametrize("other", [
    {"chat": "b"}, {"user": "other@example.test"}, {"deployment": "other"},
    {"sandbox": "sb2"}, {"deployment": "test|owner@example.test", "user": "a"},
])
async def test_interpreter_namespaces(sandbox_python, other):
    lab = sandbox_python
    await lab.call("x = 'original'")
    result = await lab.call("print('x' in globals())", **other)
    assert result == "False"
    assert await lab.call("print(x)") == "original"


async def test_interpreter_busy_and_requester_death(sandbox_python):
    lab = sandbox_python
    marker = lab.root / "started"
    release = lab.root / "release"
    worker = await lab.start(f"import pathlib, time\npathlib.Path({str(marker)!r}).touch()\nwhile not pathlib.Path({str(release)!r}).exists(): time.sleep(.02)\nx = 77")
    await lab.until(marker.exists)
    worker.kill()
    await worker.wait()
    assert "Session is executing run" in await lab.call("x = 99")
    release.touch()
    durable, _ = lab.paths()
    await lab.until(lambda: json.loads((durable / "state.json").read_text())["active"] is None)
    assert await lab.call("print(x)") == "77"


async def test_interpreter_lost_response_idempotency(sandbox_python):
    lab = sandbox_python
    run_id = uuid.uuid4().hex
    request = {"op": "submit", "id": run_id, "code": "x = globals().get('x', 0) + 1", "timeout": 120}
    await lab.rpc(request)  # Pretend the response was lost to OWUI.
    durable, _ = lab.paths()
    await lab.until(lambda: json.loads((durable / "state.json").read_text())["active"] is None)
    assert (await lab.rpc(request))["status"] == "complete"
    assert await lab.call("print(x)") == "1"
    changed = await lab.rpc({**request, "code": "x = 999"})
    assert changed["status"] == "blocked"
    assert await lab.call("print(x)") == "1"


async def test_interpreter_process_loss_warns_and_preserves_files(sandbox_python):
    lab = sandbox_python
    marker = lab.root / "ordinary-file"
    await lab.call(f"x = 42\nopen({str(marker)!r}, 'w').write('saved')")
    _, runtime = lab.paths()
    pid = json.loads((runtime / "process.json").read_text())["supervisor"]
    os.kill(pid, signal.SIGTERM)
    await lab.until(lambda: not (runtime / "rpc.sock").exists())
    result = await lab.call("print('x' in globals())")
    assert "session was reset" in result and result.endswith("False")
    assert marker.read_text() == "saved"
    assert await lab.call("print('next')") == "next"


async def test_interpreter_supervisor_crash_blocks_replacement(sandbox_python):
    lab = sandbox_python
    marker = lab.root / "started"
    worker = await lab.start(f"import pathlib, time\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(60)\nx = 42")
    await lab.until(marker.exists)
    durable, runtime = lab.paths()
    before = json.loads((durable / "state.json").read_text())
    process = json.loads((runtime / "process.json").read_text())
    os.kill(process["supervisor"], signal.SIGKILL)
    result = await lab.call("print('must not execute')")
    assert "outcome is unknown" in result and result.startswith("Error:")
    assert json.loads((durable / "state.json").read_text()) == before
    os.kill(process["kernel"], signal.SIGKILL)
    worker.kill()
    await worker.wait()


@pytest.mark.parametrize("contents", ["not json", '{}', '{"version":1,"generation":7,"active":null}'])
async def test_interpreter_corrupt_journal_fails_closed(sandbox_python, contents):
    lab = sandbox_python
    durable, _ = lab.paths()
    durable.mkdir(parents=True)
    (durable / "state.json").write_text(contents)
    result = await lab.call("print('must not execute')")
    assert "recovery blocked" in result
    assert (durable / "state.json").read_text() == contents


async def test_interpreter_timeout_and_output_contract(sandbox_python):
    lab = sandbox_python
    assert await lab.call("print('Error: expected output')") == "Error: expected output"
    result = await lab.call("print('é' * 500000); import sys; sys.stderr.write('discarded')")
    assert len(result.encode()) < 53 * 1024 and "Text preview truncated" in result
    result = await lab.call("x = 42; import time; time.sleep(60)", timeout=1)
    assert "timed out" in result and "Do not automatically replay" in result
    result = await lab.call("print('x' in globals())")
    assert "session was reset" in result and result.endswith("False")


async def test_interpreter_delimiter_bearing_tuple_isolation(sandbox_python):
    lab = sandbox_python
    await lab.call("x = 'left'", user="a|b", chat="c")
    assert await lab.call("print('x' in globals())", user="a", chat="b|c") == "False"
    assert await lab.call("print(x)", user="a|b", chat="c") == "left"


async def test_interpreter_unpublished_run_blocks_and_completed_run_reconciles(sandbox_python):
    lab = sandbox_python
    durable, _ = lab.paths()
    runs = durable / "runs"
    runs.mkdir(parents=True)
    run_id = uuid.uuid4().hex
    # Crash after publishing a run but before publishing its active claim.
    (runs / f"{run_id}.json").write_text(json.dumps({"status": "running"}))
    assert "outcome is unknown" in await lab.call("print('must not execute')")

    # Separate session: result committed, crash before clearing active claim.
    durable, _ = lab.paths(chat="complete-before-crash")
    runs = durable / "runs"
    runs.mkdir(parents=True)
    (runs / f"{run_id}.json").write_text(json.dumps({"status": "complete"}))
    (durable / "state.json").write_text(json.dumps({"version": 1, "generation": "previous", "active": run_id}))
    result = await lab.call("print('recovered')", chat="complete-before-crash")
    assert "session was reset" in result and result.endswith("recovered")


async def test_interpreter_abrupt_python_exit_and_result_retention(sandbox_python):
    lab = sandbox_python
    result = await lab.call("import os; os._exit(0)")
    assert "exited before returning a result" in result
    result = await lab.call("print('ready')")
    assert "session was reset" in result and result.endswith("ready")
    durable, _ = lab.paths()
    old = list((durable / "runs").glob("*.json"))
    for path in old:
        os.utime(path, (time.time() - 86401, time.time() - 86401))
    assert await lab.call("print('next')") == "next"
    assert all(not p.exists() for p in old)


@pytest.mark.parametrize("fault,scenario", [
    (("namespace)", "dict(namespace))"), "continuity"),
    (("reset = state[\"generation\"] is not None", "reset = False"), "reset"),
])
async def test_interpreter_contracts_detect_injected_faults(sandbox_python, fault, scenario):
    lab = sandbox_python
    await lab.call("x = 35", fault=fault)
    if scenario == "continuity":
        observed = await lab.call("print(x)", fault=fault)
        with pytest.raises(AssertionError):
            assert observed == "35", "continuity contract must catch per-call namespaces"
    else:
        _, runtime = lab.paths()
        pid = json.loads((runtime / "process.json").read_text())["supervisor"]
        os.kill(pid, signal.SIGTERM)
        await lab.until(lambda: not (runtime / "rpc.sock").exists())
        observed = await lab.call("print('x' in globals())", fault=fault)
        with pytest.raises(AssertionError):
            assert "session was reset" in observed, "reset contract must catch suppressed generation warnings"

"""Offline behavioral contracts. Run with `uv run pytest` (no credentials).

Exercise shipped code; replace only network/model boundaries. Each test owns
its files and state. Live Daytona and OWUI checks remain explicit scripts.
"""

import __future__
import asyncio
import base64
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import typing
from unittest.mock import AsyncMock

import httpx
import pytest

import lathe
from testing_support import EXPECTED_SCHEMA, normalize_schema

USER = {"email": "owner@example.test", "id": "trusted-user"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []
    async def refuse(self, request):
        attempts.append(f"{request.method} {request.url}")
        raise AssertionError("offline suite attempted real HTTP")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    yield
    assert not attempts, attempts


@pytest.fixture
def transport():
    """Keep evidence of broken test boundaries even if production catches it."""
    violations = []

    def create(handler):
        async def guarded(request):
            try:
                response = handler(request)
                return await response if inspect.isawaitable(response) else response
            except httpx.HTTPError:
                raise  # Deliberately injected transport failures are valid stimuli.
            except Exception as exc:
                violations.append(f"{request.method} {request.url}: {type(exc).__name__}: {exc}")
                raise
        return httpx.MockTransport(guarded)

    yield create
    assert not violations, "Unexpected I/O or broken test responder:\n" + "\n".join(violations)


@pytest.fixture
def tools():
    t = lathe.Tools()
    t.valves.daytona_api_key = "test-key"
    t.valves.deployment_label = "test"
    t.valves.daytona_api_url = "https://api.test"
    t.valves.daytona_proxy_url = "https://proxy.test"
    return t


@pytest.fixture
def http(monkeypatch, transport):
    """Install a strict HTTP boundary, retaining real httpx response semantics."""
    constructor = httpx.AsyncClient
    clients = []

    def install(handler):
        def create(*args, **kwargs):
            # Delegate's in-process model transport must remain real.
            kwargs.setdefault("transport", transport(handler))
            client = constructor(*args, **kwargs)
            clients.append(client)
            return client
        monkeypatch.setattr(lathe, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": create}))
        return clients

    return install


@pytest.fixture
def clock(monkeypatch):
    """Advance Lathe's polling clock without modifying asyncio for other code."""
    real_sleep = asyncio.sleep
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds
        await real_sleep(0)

    monkeypatch.setattr(lathe, "time", SimpleNamespace(**{
        **vars(lathe.time), "time": lambda: now[0], "monotonic": lambda: now[0]}))
    monkeypatch.setattr(lathe, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": sleep}))
    return lambda: now[0]


@pytest.fixture
def sandbox(monkeypatch):
    """Bypass VM provisioning, retaining Tools dispatch and message delivery."""
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(return_value=("sb", None)))
    monkeypatch.setattr(lathe, "_ensure_chat_init", AsyncMock())


@pytest.fixture(params=[False, True], ids=["import", "owui-future-exec"])
def module(request, monkeypatch):
    if not request.param:
        return lathe
    # Real source, real factory: copied demonstrations of get_type_hints do
    # not protect against removing annotation resolution from production.
    module = ModuleType("lathe_loader_test")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    source = Path(lathe.__file__).read_text()
    code = compile(source, lathe.__file__, "exec",
                   flags=__future__.annotations.compiler_flag, dont_inherit=True)
    exec(code, module.__dict__)
    return module


def test_tools_interface(module):
    methods = dict(inspect.getmembers(module.Tools(), inspect.ismethod))
    assert {n for n in methods if not n.startswith("_")} == set(EXPECTED_SCHEMA)
    types = {str: "string", int: "integer", bool: "boolean", list: "array"}
    for name, expected in EXPECTED_SCHEMA.items():
        method = methods[name]
        hints = typing.get_type_hints(method)
        params = inspect.signature(method).parameters
        actual = {}
        for key, param in params.items():
            if key.startswith("__"):
                continue
            hint = typing.get_origin(hints[key]) or hints[key]
            required = param.default is inspect.Parameter.empty
            actual[key] = (types[hint], required, None if required else param.default)
        assert actual == expected, name
        assert inspect.getdoc(method), name
        assert "__user__" in params and "__event_emitter__" in params
        if name not in {"lathe", "handoff", "destroy"}:
            assert "__chat_id__" in params, name
    assert "__event_call__" in inspect.signature(methods["destroy"]).parameters
    assert {"__model__", "__metadata__", "__request__"} <= set(
        inspect.signature(methods["delegate"]).parameters)


def test_delegate_interface(module):
    t = module.Tools()
    t._chat_state["chat"] = {"init": True, "pending": []}
    for with_chat in (False, True):
        tools = module._build_delegate_tools(
            t.valves, "sb", None, [],
            chat_state=t._chat_state if with_chat else None, chat_id="chat")
        expected_names = {"bash", "read", "write", "edit", "glob", "grep"}
        if with_chat:
            expected_names.add("interpret")
        assert {tool.name for tool in tools} == expected_names
        for tool in tools:
            expected = dict(EXPECTED_SCHEMA[tool.name])
            if tool.name == "bash":
                expected["foreground_seconds"] = ("integer", False, 15)
            assert normalize_schema(tool.tool_def.parameters_json_schema) == expected
            assert tool.description
            assert all(p.get("description") for p in
                       tool.tool_def.parameters_json_schema["properties"].values())


@pytest.mark.parametrize("name,kwargs", [
    ("read", {"path": "/x", "start": "2"}),
    ("edit", {"path": "/x", "old_string": "a", "new_string": "b", "replace_all": "false"}),
    ("glob", {"pattern": "*", "max_lines": "4"}),
    ("grep", {"pattern": "x", "max_lines": []}),
    ("interpret", {"code": "1", "timeout": "2"}),
    ("bash", {"command": "true", "foreground_seconds": "0"}),
    ("delegate", {"task": "x", "context_files": "/x"}),
    ("delegate", {"task": "x", "max_steps": "2"}),
])
async def test_bad_types_rejected_before_io(module, monkeypatch, name, kwargs):
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await getattr(module.Tools(), name)(**kwargs)
    assert "expected type" in result
    context.assert_not_called()


async def test_loaded_wrapper_dispatches_typed_arguments(module, monkeypatch, tmp_path):
    observed = []
    monkeypatch.setattr(module, "_ensure_sandbox", AsyncMock(return_value=("sb", None)))
    monkeypatch.setattr(module, "_ensure_chat_init", AsyncMock())

    async def execute(valves, sid, client, script, **kwargs):
        observed.append(script)
        return run_script(script)

    monkeypatch.setattr(module, "_run_sandbox_script", execute)
    path = tmp_path / "input.txt"
    path.write_text("first\nselected-line\nlast\n")
    result = await module.Tools().read(str(path), start=2, stop=3, __user__=USER)
    assert result.splitlines()[1:] == ["2: selected-line"]
    assert len(observed) == 1


def run_script(source, **kwargs):
    return subprocess.run([sys.executable, "-c", source], capture_output=True,
                          text=True, check=True, timeout=10, **kwargs).stdout.rstrip("\n")


def script_call(kind, function, *args):
    return run_script(getattr(lathe, f"_{kind}_SCRIPT") +
                      f"\nprint({function}({', '.join(repr(a) for a in args)}))")


@pytest.mark.parametrize("content", ["", "café\n世界\n", "it's a \\\"test\\\"\nlast"])
def test_write_roundtrip(tmp_path, content):
    path = tmp_path / "nested" / "it's a test.txt"
    for value in ("old content", content):
        result = script_call("WRITE", "write_file", str(path), value)
        assert not result.startswith("Error:")
        assert path.read_bytes() == value.encode()
        assert f"{len(value.encode())} bytes" in result


@pytest.mark.parametrize("start,stop,selected", [
    (1, 0, list(range(1, 11))), (3, 5, [3, 4]), (-3, 0, [8, 9, 10]),
    (-5, -3, [6, 7]), (0, 3, [1, 2]), (5, 3, []), (50, 0, []),
])
def test_read_ranges(tmp_path, start, stop, selected):
    path = tmp_path / "lines.txt"
    path.write_text("".join(f"line{i}\n" for i in range(1, 11)))
    result = script_call("READ", "read_file", str(path), start, stop)
    assert result.splitlines()[1:] == [f"{i}: line{i}" for i in selected]


@pytest.mark.parametrize("old,new,all_,expected,error", [
    ("foo", "baz", False, "foo bar foo\n", True),
    ("absent", "baz", False, "foo bar foo\n", True),
    ("foo", "baz", True, "baz bar baz\n", False),
    ("bar", "it's\na test", False, "foo it's\na test foo\n", False),
])
def test_edit_is_unambiguous_or_non_destructive(tmp_path, old, new, all_, expected, error):
    path = tmp_path / "edit.txt"
    path.write_text("foo bar foo\n")
    result = script_call("EDIT", "edit_file", str(path), old, new, all_)
    assert result.startswith("Error:") == error
    assert path.read_text() == expected


def test_read_limit_and_missing_file(tmp_path):
    path = tmp_path / "large.txt"
    assert script_call("READ", "read_file", str(path), 1, 0).startswith("Error:")
    assert script_call("EDIT", "edit_file", str(path), "x", "y", False).startswith("Error:")
    path.write_text("x\n" * 3000)
    assert len(script_call("READ", "read_file", str(path), 1, 0).splitlines()[1:]) == 2000


@pytest.fixture
def tree(tmp_path):
    files = {"a.py": "needle\n", "b.txt": "needle\n", "sub/c.py": "no hit\n"}
    files.update({f"sub/deep/{i}.py": "needle\nneedle\n" for i in range(10)})
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path.resolve()


@pytest.mark.parametrize("kind", ["GLOB", "GREP"])
def test_search_scope_and_absolute_paths(tree, tmp_path_factory, kind):
    def search(pattern):
        args = (str(tree), pattern, 100) if kind == "GLOB" else (str(tree), "needle", pattern, 100)
        return script_call(kind, kind.lower() + "_hierarchy", *args)
    relative = search("**/*.py,!**/deep/**")
    absolute = search(f"{tree}/**/*.py,!{tree}/**/deep/**")
    assert relative.splitlines()[1:] == absolute.splitlines()[1:]
    assert "deep/" not in "\n".join(relative.splitlines()[1:])
    assert str(tree / "a.py") in relative
    assert "b.txt" not in relative
    assert search("!**/deep/**,**/*.py").splitlines()[1:] == relative.splitlines()[1:]
    assert "b.txt" in search("**/*.py,**/*.txt")
    assert search("!**/*.py").startswith("Error:")
    outside = tmp_path_factory.mktemp("outside").resolve()
    (outside / "other.py").write_text("needle\n")
    assert str(outside / "other.py") in search(f"{outside}/*.py")


@pytest.mark.parametrize("kind,total", [("GLOB", 13), ("GREP", 22)])
@pytest.mark.parametrize("budget", [4, 8, 100])
def test_search_budget_conserves_matches(tree, kind, total, budget):
    args = (str(tree), "**/*", budget) if kind == "GLOB" else (str(tree), "needle", "**/*", budget)
    result = script_call(kind, kind.lower() + "_hierarchy", *args)
    header, *body = result.splitlines()
    assert int(header.split()[0]) == total
    assert len(body) <= budget
    counts = [re.search(r"(?:\(|and )(\d+) (?:more )?matches", line) for line in body]
    assert sum(int(m[1]) if m else 1 for m in counts) == total
    if budget == 100:
        assert all(line.startswith(str(tree)) for line in body)
        assert len(body) == total


def test_grep_invalid_regex_and_no_hits(tree):
    assert script_call("GREP", "grep_hierarchy", str(tree), "[", "**/*", 10).startswith("Error:")
    assert script_call("GREP", "grep_hierarchy", str(tree), "absent", "**/*", 10).startswith("0 matches")


@pytest.mark.parametrize("name,kwargs", [
    ("read", {}), ("write", {"content": "x"}),
    ("edit", {"old_string": "x", "new_string": "y"}), ("view", {}),
])
async def test_relative_file_path_never_reaches_http(tools, name, kwargs):
    result = await getattr(lathe, "_core_" + name)(tools.valves, "sb", None, path="relative", **kwargs)
    assert "absolute path" in result


async def test_file_core_http_roundtrip(tools, tmp_path, transport):
    # Execute the actual HTTP command locally: covers quoting and argument
    # assembly as well as real disk effects, rather than echoing canned text.
    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        assert request.headers["authorization"] == "Bearer test-key"
        command = shlex.split(json.loads(request.content)["command"])
        assert command[:2] == ["python3", "-c"]
        result = run_script(command[2])
        return httpx.Response(200, json={"exitCode": 0, "result": result})
    path = str(tmp_path / "it's a file.txt")
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        await lathe._core_write(tools.valves, "sb", client, path=path, content="alpha\ncafé\n")
        await lathe._core_edit(tools.valves, "sb", client, path=path, old_string="café", new_string="世界")
        result = await lathe._core_read(tools.valves, "sb", client, path=path, start=2, stop=3)
    assert result.splitlines()[1:] == ["2: 世界"]
    assert Path(path).read_text() == "alpha\n世界\n"


@pytest.mark.parametrize("raw", ["invalid", "[]", '{"BAD-KEY":"x"}', '{"A":1}'])
def test_invalid_environment_rejected(raw):
    with pytest.raises(ValueError):
        lathe._parse_env_vars(raw)


@pytest.mark.parametrize("key", ["name", "labels", "volumes"])
def test_create_overrides_cannot_replace_identity(key):
    with pytest.raises(ValueError, match=key):
        lathe._parse_create_overrides(json.dumps({key: "override"}))
    assert lathe._parse_create_overrides('{"cpu":2}') == {"cpu": 2}


def test_bash_script_preserves_environment_and_failure(tmp_path):
    value = "it's $HOME `whoami`\n世界"
    pairs = lathe._parse_env_vars(json.dumps({"SECRET": value}))
    script = lathe._build_bash_script(
        'printf "%s" "$SECRET"; false; printf should-not-run', pairs,
        str(tmp_path / "pid"), str(tmp_path / "log"))
    result = subprocess.run(["bash"], input=script, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert result.stdout == value
    assert (tmp_path / "log").read_text() == value
    # macOS ships Bash 3 (no BASHPID); Linux sidecar PIDs are a live-suite concern.


@pytest.mark.parametrize("text", ["", "short\ntext", "x\n" * 3000, ("é" * 100 + "\n") * 600])
def test_output_tail_is_bounded_and_recoverable(text):
    output, truncated, meta = lathe._truncate_tail(text)
    assert len(output.encode()) <= 50 * 1024
    assert len(output.splitlines()) <= 2000
    assert text.endswith(output)
    formatted = lathe._format_bash_result(output, 7, truncated, meta, spill_path="/logs/full")
    assert "Exit code: 7" in formatted
    if truncated:
        assert "/logs/full" in formatted
        assert meta["total_bytes"] == len(text.encode())
    else:
        assert output == text


def test_onboard_merges_global_and_project_context(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "it's a project"
    for base, instruction, description in [
        (home / ".agents", "global instructions", "global skill"),
        (project, "project instructions", "project skill"),
    ]:
        base.mkdir(parents=True)
        (base / "AGENTS.md").write_text(instruction)
        skill = (base if base == home / ".agents" else base / ".agents") / "skills" / "shared"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: shared\ndescription: {description}\n---\nPRIVATE BODY")
    result = run_script(lathe._build_onboard_script(str(project)), env={**os.environ, "HOME": str(home)})
    assert "global instructions" in result and "project instructions" in result
    assert "project skill" in result and "global skill" not in result
    assert "PRIVATE BODY" not in result
    assert str(project / "AGENTS.md") in result


@pytest.mark.parametrize("volume,wrapped", [(False, False), (True, False), (True, True)])
async def test_rendered_manual(tools, volume, wrapped):
    tools.valves.persistent_volume = volume
    tools.valves.preview_wrapper_url = "https://wrapper.test" if wrapped else ""
    for page in tools._MANPAGES:
        result = await tools.lathe(page)
        assert len(result) > 500 and not result.startswith("Error:")
        assert not re.search(r"\{(?:tool_catalog|volume_note|destroy_volume_note|preview_access_note)\}", result)
    overview = await tools.lathe()
    assert ("/home/daytona/volume" in overview) == volume
    assert "bash(" in overview and "delegate(" in overview
    assert ("try the wrapper" in overview) == wrapped
    assert "overview" in await tools.lathe("nonexistent")


async def test_chat_init_and_notice_delivery(tools, transport):
    calls = []
    def handler(request):
        calls.append(request)
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            return httpx.Response(200, json={"exitCode": 0, "result": "workspace snapshot"})
        assert (request.method, request.url.path) == ("GET", "/sandbox/sb")
        return httpx.Response(200, json={"cpu": 2})
    user = {**USER, "valves": tools.UserValves(env_vars='{"TOKEN":"hidden-value"}')}
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        for _ in range(2):
            await lathe._ensure_chat_init(tools.valves, "sb", client, tools._chat_state, "a", user)
    assert len(calls) == 2
    tools._chat_state["b"] = {"init": True, "pending": []}
    lathe._push_bg_notice(tools._chat_state, "a", "job complete")
    assert lathe._drain_harness_messages(tools._chat_state, "b", None) == []
    delivered = "\n".join(lathe._drain_harness_messages(tools._chat_state, "a", "restarted"))
    assert all(value in delivered for value in ["workspace snapshot", "TOKEN", "job complete", "restarted"])
    assert "hidden-value" not in delivered
    assert lathe._drain_harness_messages(tools._chat_state, "a", None) == []


@pytest.mark.parametrize("prior,live,expected,lost", [
    (None, [], "new", False), ("old", [{"id": "old"}], "old", False),
    ("old", [], "new", True),
])
async def test_interpreter_context_recovery(tools, transport, prior, live, expected, lost):
    tools._chat_state["a"] = {"init": True, "pending": []}
    if prior:
        tools._chat_state["a"]["interpreter_context_id"] = prior
    calls = []
    def handler(request):
        calls.append(request.method)
        assert request.url.path == "/sb/process/interpreter/context"
        assert request.method in {"GET", "POST"}
        return httpx.Response(200, json={"contexts": live} if request.method == "GET" else {"id": "new"})
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        assert await lathe._ensure_interpreter_context(tools.valves, "sb", client, tools._chat_state, "a") == (expected, lost)
    assert tools._chat_state["a"]["interpreter_context_id"] == expected
    assert calls.count("POST") == (expected == "new")


async def test_background_bash_poll_delivers_once(tools, sandbox, http, clock):
    tools._chat_state["chat"] = {"init": True, "pending": []}
    statuses = iter([httpx.Response(503), httpx.Response(200, json={"commands": [{"id": "cmd"}]}),
                     httpx.Response(200, json={"commands": [{"id": "cmd", "exitCode": 7}]})])
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            return httpx.Response(200, json={"exitCode": 0, "result": "caller output"})
        assert request.method == "GET"
        if request.url.path == "/sb/process/session/session":
            return next(statuses)
        assert request.url.path == "/sb/process/session/session/command/cmd/logs"
        return httpx.Response(200, text="final output")
    http(handler)
    await lathe._poll_bg_bash(tools.valves, "sb", "session", "cmd", "job", 0, tools._chat_state, "chat")
    assert len(requests) == 4
    # Observe delivery through an ordinary wrapper, not by draining its queue.
    assert await tools.read("/file", __user__=USER, __chat_id__="other") == "caller output"
    result = await tools.read("/file", __user__=USER, __chat_id__="chat")
    assert result.count("Background job completed") == 1
    assert all(s in result for s in ["job", "Exit code: 7", "final output", "caller output"])
    assert await tools.read("/file", __user__=USER, __chat_id__="chat") == "caller output"


async def test_background_bash_poll_deadline(tools, http, clock, monkeypatch):
    tools._chat_state["chat"] = {"init": True, "pending": []}
    requests = []
    def handler(request):
        requests.append(request)
        assert (request.method, request.url.path) == ("GET", "/sb/process/session/session")
        return httpx.Response(200, json={"commands": [{"id": "cmd"}]})
    http(handler)
    monkeypatch.setattr(lathe, "_BG_BASH_POLL_MAX_SECONDS", 5)
    await asyncio.wait_for(lathe._poll_bg_bash(
        tools.valves, "sb", "session", "cmd", "job", 0, tools._chat_state, "chat"), 1)
    assert clock() >= 5
    assert requests and len(requests) < 10
    assert tools._chat_state["chat"]["pending"] == []


@pytest.mark.parametrize("state,status", [("destroying", 200), ("destroyed", 200), ("started", 404)])
async def test_stale_discovery_never_resurrects_sandbox(tools, transport, state, status):
    tools.valves.auto_create_sandbox = False
    tools.valves.sandbox_missing_message = "provision externally"
    candidate = {"id": "sb", "labels": {"test": USER["email"]}, "state": "started"}
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.path == "/sandbox":
            return httpx.Response(200, json={"items": [candidate]})
        assert request.url.path == "/sandbox/sb"
        return httpx.Response(status, json={**candidate, "state": state})
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        with pytest.raises(RuntimeError, match="provision externally"):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)
    assert calls == ["/sandbox", "/sandbox/sb"]


async def test_duplicate_sandbox_association_refuses_use_and_destroy(tools, http):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        assert (request.method, request.url.path) == ("GET", "/sandbox")
        return httpx.Response(200, json=[{"id": sid, "labels": {"test": USER["email"]}} for sid in ("a", "b")])
    http(handler)
    async with lathe.httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="Multiple sandboxes"):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)
    assert "Multiple sandboxes" in await tools.destroy(__user__=USER, __event_call__=AsyncMock(return_value=True))
    assert calls == [("GET", "/sandbox")] * 2


async def test_destroy_requires_consent_and_authoritative_completion(tools, http, clock):
    calls = []
    responses = iter([
        httpx.Response(200, json=[{"id": "sb", "labels": {"test": USER["email"]}}]),
        httpx.Response(200, json={"id": "sb", "state": "started"}),
        httpx.Response(204), httpx.Response(200, json={"state": "destroying"}), httpx.Response(404),
    ])
    def handler(request):
        calls.append((request.method, request.url.path))
        return next(responses)
    http(handler)
    assert "cannot confirm" in await tools.destroy(__user__=USER)
    assert "cancelled" in await tools.destroy(__user__=USER, __event_call__=AsyncMock(return_value=False))
    assert calls == []
    result = await tools.destroy(__user__=USER, __event_call__=AsyncMock(return_value=True))
    assert result.startswith("Destroyed 1 sandbox")
    assert calls == [("GET", "/sandbox"), ("GET", "/sandbox/sb"), ("DELETE", "/sandbox/sb"),
                     ("GET", "/sandbox/sb"), ("GET", "/sandbox/sb")]


@pytest.mark.parametrize("payload,mime", [
    (PNG, "png"), (b"\xff\xd8\xff\xe0", "jpeg"), (b"GIF89a", "gif"),
    (b"RIFF0000WEBP", "webp"), (b"<svg/>", None), (b"\x89PNG", None),
])
async def test_view_content_detection(tools, transport, payload, mime):
    def handler(request):
        assert (request.method, request.url.path) == ("GET", "/sb/files/download")
        assert request.url.params["path"] == "/misleading.txt"
        return httpx.Response(200, content=payload)
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        result = await lathe._core_view(tools.valves, "sb", client, path="/misleading.txt")
    if mime:
        assert result == f"data:image/{mime};base64,{base64.b64encode(payload).decode()}"
    else:
        assert result.startswith("Error:")


async def test_image_return_defers_notices_until_text_call(tools, sandbox, http):
    def handler(request):
        if request.url.path == "/sb/files/download":
            assert request.method == "GET" and request.url.params["path"] == "/image"
            return httpx.Response(200, content=PNG)
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        return httpx.Response(200, json={"exitCode": 0, "result": "text result"})
    http(handler)
    tools._chat_state["a"] = {"init": True, "pending": ["pending notice"]}
    result = await tools.view("/image", __user__=USER, __chat_id__="a")
    assert result == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert await tools.read("/text", __user__=USER, __chat_id__="a") == "pending notice\n\ntext result"
    assert await tools.read("/text", __user__=USER, __chat_id__="a") == "text result"


@pytest.mark.parametrize("model", [
    {"architecture": {"input_modalities": ["text"]}, "info": {"meta": {"capabilities": {"vision": True}}}},
    {"info": {"meta": {"capabilities": {"vision": False}}}},
])
async def test_text_model_view_refused_before_io(tools, monkeypatch, model):
    context = AsyncMock(side_effect=AssertionError("vision gate reached I/O"))
    monkeypatch.setattr(lathe, "_tool_context", context)
    result = await tools.view("/image", __metadata__={"model": model}, __model__={"architecture": {"input_modalities": ["image"]}})
    assert "does not accept image input" in result
    context.assert_not_called()


@pytest.fixture
def preview(tools, sandbox, http):
    upstream, protected, secret = "https://secret.preview.test/", "https://owner.preview.test/", "installation-secret"
    tools.valves.preview_wrapper_url = "https://wrapper.test/register"
    tools.valves.preview_wrapper_key = secret
    good = {"url": protected, "access_mode": "owner-authenticated", "expires_at": "2099-01-01T00:00:00Z"}

    async def invoke(payload=None, status=200, target="http:5000", user=USER,
                     access="private"):
        calls, events = [], []
        async def emit(event):
            events.append(event)
        def handler(request):
            calls.append(request)
            if request.url.host == "wrapper.test":
                assert (request.method, request.url.path) == ("POST", "/register")
                if payload == "timeout":
                    raise httpx.ReadTimeout(upstream + secret)
                if payload == "malformed":
                    return httpx.Response(200, text=upstream + secret)
                return httpx.Response(status, json=good if payload is None else payload)
            if request.url.path.endswith("/signed-preview-url"):
                assert request.method == "GET" and request.url.host == "api.test"
                assert re.fullmatch(r"/sandbox/sb/ports/(5000|8080)/signed-preview-url", request.url.path)
                assert request.url.params["expiresInSeconds"] == "86400"
                return httpx.Response(200, json={"url": upstream})
            assert request.url.host == "proxy.test"
            assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
            return httpx.Response(200, json={"exitCode": 0, "result": "READY PID=123"})
        http(handler)
        result = await tools.expose(target, access, __user__=user, __event_emitter__=emit)
        return result, calls, json.dumps(events)
    return SimpleNamespace(invoke=invoke, upstream=upstream, protected=protected, secret=secret, good=good)


@pytest.mark.parametrize("target,slot", [("http:5000", "5000"), ("dufs", "5000"), ("code-server", "8080")])
async def test_preview_uses_trusted_identity_and_hides_credentials(preview, target, slot):
    result, calls, events = await preview.invoke(target=target)
    assert preview.protected in result and "Owner-authenticated" in result
    assert all(s not in result + events for s in [preview.upstream, preview.secret])
    registrations = [r for r in calls if r.url.host == "wrapper.test"]
    assert len(registrations) == 1
    assert json.loads(registrations[0].content) == {
        "owner": {"subject": USER["id"], "email": USER["email"]}, "slot": slot,
        "upstream_url": preview.upstream}
    assert registrations[0].headers["Authorization"] == "Bearer " + preview.secret


async def test_public_preview_accepts_public_wrapper_result(preview):
    payload = {**preview.good, "access_mode": "public-wrapped"}
    result, calls, events = await preview.invoke(payload, access="public")
    assert preview.protected in result and "Public wrapped preview" in result
    assert all(s not in result + events for s in [preview.upstream, preview.secret])
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1


@pytest.mark.parametrize("failure,status", [
    ("timeout", 200), ("malformed", 200),
    ({"access_mode": "owner-authenticated"}, 200), ({"error": "refused"}, 409),
])
async def test_public_preview_falls_back_to_direct_url(preview, failure, status):
    payload = ({**preview.good, **failure} if isinstance(failure, dict) and status == 200
               else failure)
    result, calls, _ = await preview.invoke(payload, status, access="public")
    assert preview.upstream in result and "Public direct preview" in result
    assert preview.secret not in result
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1


@pytest.mark.parametrize("failure", ["upstream", "public", "http", "leak", "expired", "naive", "500", "302", "malformed", "timeout"])
async def test_preview_failures_are_closed_and_redacted(preview, failure):
    changes = {
        "upstream": {"url": preview.upstream}, "public": {"access_mode": "public"},
        "http": {"url": "http://insecure.test/"}, "leak": {"url": preview.protected + "?leak=" + preview.upstream},
        "expired": {"expires_at": "2000-01-01T00:00:00Z"}, "naive": {"expires_at": "2099-01-01T00:00:00"},
    }
    payload = {**preview.good, **changes[failure]} if failure in changes else failure
    status = int(failure) if failure.isdigit() else 200
    if status != 200:
        payload = {"error": preview.upstream + preview.secret}
    result, calls, events = await preview.invoke(payload, status)
    assert result.startswith("Error:")
    assert all(s not in result + events for s in [preview.protected, preview.upstream, preview.secret])
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1


async def test_preview_configuration_and_direct_mode(preview, tools):
    result, calls, _ = await preview.invoke(access="secret")
    assert result.startswith("Error:") and not calls
    result, calls, _ = await preview.invoke(user={"email": USER["email"]})
    assert result.startswith("Error:") and not calls
    tools.valves.preview_wrapper_key = ""
    result, calls, _ = await preview.invoke()
    assert result.startswith("Error:") and not calls
    tools.valves.preview_wrapper_url = ""
    result, calls, _ = await preview.invoke()
    assert result.startswith("Error:") and preview.upstream not in result
    result, calls, _ = await preview.invoke(access="public")
    assert preview.upstream in result and "bearer credential" in result
    assert not any(r.url.host == "wrapper.test" for r in calls)
    result, calls, _ = await preview.invoke(target="ssh")
    assert result.startswith("Error:") and not calls


@pytest.mark.parametrize("background,fail", [(False, False), (True, False), (True, True)])
async def test_real_delegate_execution_and_completion(tools, sandbox, http, monkeypatch, tmp_path, background, fail):
    """Real Agent, tool cores and files; observe completion as the caller does."""
    release = asyncio.Event()
    if not background:
        release.set()
    requests, emissions = [], []
    source = tmp_path / "input.txt"
    source.write_text("secret discovered by tool\n")
    root = tmp_path / "sidecars"
    monkeypatch.setattr(lathe, "_EPHEMERAL_ROOT", str(root))
    for chat in ("chat", "other"):
        tools._chat_state[chat] = {"init": True, "pending": []}

    def execute(request):
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        assert request.headers["authorization"] == "Bearer test-key"
        command = shlex.split(json.loads(request.content)["command"])
        assert command[:2] == ["python3", "-c"]
        return httpx.Response(200, json={"exitCode": 0, "result": run_script(command[2])})
    clients = http(execute)

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        requests.append((scope, json.loads(body)))
        await release.wait()
        if fail:
            response = {"error": {"message": "synthetic model failure", "type": "invalid_request_error"}}
            status = 400
        else:
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "read-1", "type": "function", "function": {
                        "name": "read", "arguments": json.dumps({"path": str(source)})}}]}
                finish = "tool_calls"
            else:
                message = {"role": "assistant", "content": "verified result"}
                finish = "stop"
            response = {"id": "completion", "object": "chat.completion", "created": 0,
                        "model": "selected-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            status = 200
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(response).encode()})

    async def emit(event):
        emissions.append(event)

    async def read_in(chat):
        return await tools.read(str(source), __user__=USER, __chat_id__=chat)

    request = SimpleNamespace(app=app, state=SimpleNamespace(token=SimpleNamespace(credentials="user-token")))
    task_description = f"Read {source} and report"
    # Capture newly spawned tasks only for failure cleanup, never as an oracle.
    existing_tasks = asyncio.all_tasks()
    try:
        result = await tools.delegate(task_description, max_steps=3,
            foreground_seconds=0 if background else 5, __user__=USER, __chat_id__="chat",
            __model__={"id": "wrong-model"}, __metadata__={"model": {"id": "selected-model"}},
            __request__=request, __event_emitter__=emit)
        directories = list((root / "delegate").iterdir())
        assert len(directories) == 1, result
        sidecars = directories[0]
        assert (sidecars / "task").read_text() == task_description
        emission_count = len(emissions)
        notice = "Background delegation failed" if fail else "Background delegation completed"
        if background:
            descriptor = re.search(r"^DELEGATE=(.+)$", result, re.MULTILINE)
            assert descriptor and sidecars.name == descriptor[1], result
            assert not (sidecars / "result").exists() and not (sidecars / "error").exists()
            assert any(client.is_closed for client in clients)
            assert notice not in await read_in("chat")
        else:
            assert "verified result" in result and "1 tool call(s)" in result
        release.set()
        async with asyncio.timeout(5):
            if background:
                while notice not in (delivered := await read_in("chat")):
                    await asyncio.sleep(0)
                assert delivered.count(notice) == 1
            while not all(client.is_closed for client in clients):
                await asyncio.sleep(0)
        if background:
            assert len(emissions) == emission_count, "delegate emitted to a closed foreground stream"
        assert notice not in await read_in("chat")
        assert notice not in await read_in("other")
        if fail:
            assert "synthetic model failure" in (sidecars / "error").read_text()
            assert not (sidecars / "result").exists()
        else:
            assert (sidecars / "result").read_text() == "verified result"
            assert json.loads((sidecars / "usage").read_text())["tool_calls"] == 1
            assert len(requests) == 2
            assert any(m.get("role") == "tool" and "1: secret discovered by tool" in m["content"]
                       for m in requests[1][1]["messages"])
        # Check the recorded model protocol outside production's exception handlers.
        assert requests
        for scope, body in requests:
            assert scope["path"] == "/api/chat/completions"
            assert dict(scope["headers"])[b"authorization"] == b"Bearer user-token"
            assert body["model"] == "selected-model" and body["chat_id"] == "chat"
    finally:
        release.set()
        tasks = asyncio.all_tasks() - existing_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client in clients:
            await client.aclose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

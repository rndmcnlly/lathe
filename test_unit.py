"""Offline behavioral contracts. Run with `uv run pytest` (no credentials).

Exercise shipped code; replace only network/model boundaries. Each test owns
its files and state. Live Daytona and OWUI checks remain explicit scripts.
"""

import __future__
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import textwrap
import time
from types import ModuleType, SimpleNamespace
import typing
from unittest.mock import AsyncMock

import httpx
import pytest

import lathe
from testing_support import EXPECTED_SCHEMA

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

    def install(handler):
        def create(*args, **kwargs):
            kwargs["transport"] = transport(handler)
            return constructor(*args, **kwargs)
        monkeypatch.setattr(lathe, "httpx", SimpleNamespace(**{**vars(httpx), "AsyncClient": create}))

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
    types = {str: "string", int: "integer", bool: "boolean", list: "array", dict: "object"}
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
        if name not in {"lathe", "destroy"}:
            assert "__chat_id__" in params, name
    assert "__event_call__" in inspect.signature(methods["destroy"]).parameters


@pytest.mark.parametrize("name,kwargs,location,expected,actual", [
    ("lathe", {"manpage": 7}, "manpage", "str", "int"),
    ("onboard", {"path": 7}, "path", "str", "int"),
    ("bash", {"command": 7}, "command", "str", "int"),
    ("bash", {"command": "true", "workdir": 7}, "workdir", "str", "int"),
    ("bash", {"command": "true", "foreground_seconds": True}, "foreground_seconds", "int", "bool"),
    ("read", {"path": 7}, "path", "str", "int"),
    ("read", {"path": "/x", "start": True}, "start", "int", "bool"),
    ("read", {"path": "/x", "stop": True}, "stop", "int", "bool"),
    ("write", {"path": 7, "content": "x"}, "path", "str", "int"),
    ("write", {"path": "/x", "content": 7}, "content", "str", "int"),
    ("edit", {"path": 7, "old_string": "a", "new_string": "b"}, "path", "str", "int"),
    ("edit", {"path": "/x", "old_string": 7, "new_string": "b"}, "old_string", "str", "int"),
    ("edit", {"path": "/x", "old_string": "a", "new_string": 7}, "new_string", "str", "int"),
    ("edit", {"path": "/x", "old_string": "a", "new_string": "b", "replace_all": 0}, "replace_all", "bool", "int"),
    ("glob", {"pattern": 7}, "pattern", "str", "int"),
    ("glob", {"pattern": "*", "max_lines": True}, "max_lines", "int", "bool"),
    ("grep", {"pattern": 7}, "pattern", "str", "int"),
    ("grep", {"pattern": "x", "files": 7}, "files", "str", "int"),
    ("grep", {"pattern": "x", "max_lines": True}, "max_lines", "int", "bool"),
    ("interpret", {"code": 7}, "code", "str", "int"),
    ("interpret", {"code": "1", "timeout": True}, "timeout", "int", "bool"),
    ("view", {"path": 7}, "path", "str", "int"),
    ("expose", {"target": 7, "access": "private"}, "target", "str", "int"),
    ("expose", {"target": "http:5000", "access": 7}, "access", "str", "int"),
    ("expose", {"target": "http:5000", "access": "private", "tag": 7}, "tag", "str", "int"),
    ("expose", {"target": "http:5000", "access": "public", "upstream_headers": []}, "upstream_headers", "dict[str, str]", "list"),
    ("expose", {"target": "http:5000", "access": "public", "upstream_headers": {7: "x"}}, "upstream_headers", "dict[str, str]", "int"),
    ("expose", {"target": "http:5000", "access": "public", "upstream_headers": {"Authorization": 7}}, "upstream_headers", "dict[str, str]", "int"),
])
async def test_bad_types_rejected_before_io(
    module, monkeypatch, name, kwargs, location, expected, actual,
):
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await getattr(module.Tools(), name)(**kwargs)
    assert result == f"Error: parameter '{location}' expected type {expected}"
    assert "7" not in result
    context.assert_not_called()


@pytest.mark.parametrize("name,kwargs", [
    ("onboard", {"path": "/workspace"}),
    ("bash", {"command": "true", "workdir": "/workspace", "foreground_seconds": 0}),
    ("read", {"path": "/x", "start": 1, "stop": 0}),
    ("edit", {"path": "/x", "old_string": "a", "new_string": "b", "replace_all": False}),
    ("view", {"path": "/image"}),
    ("expose", {"target": "http:5000", "access": "private", "tag": "files"}),
])
async def test_valid_types_pass_wrapper_boundary(module, monkeypatch, name, kwargs):
    context = AsyncMock(return_value="accepted")
    monkeypatch.setattr(module, "_tool_context", context)
    assert await getattr(module.Tools(), name)(**kwargs) == "accepted"
    context.assert_awaited_once()


async def test_valid_lathe_string_passes_wrapper_boundary(module):
    assert "Lathe toolkit version:" in await module.Tools().lathe("version")


@pytest.mark.parametrize("kwargs", [
    {"upstream_headers": {"secret-key-canary": 7}},
    {"upstream_headers": {7: "secret-value-canary"}},
    {"upstream_headers": {"Authorization": ["secret-value-canary"]}},
])
async def test_validation_never_renders_inputs_or_dictionary_key_locations(module, monkeypatch, kwargs):
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await module.Tools().expose("http:5000", "private", **kwargs)
    assert result == "Error: parameter 'upstream_headers' expected type dict[str, str]"
    assert "secret" not in result and "Authorization" not in result
    context.assert_not_called()


@pytest.mark.parametrize("items", [("secret",), [7], [{"secret-key": False}]])
async def test_factory_strictly_validates_nested_list_members(module, monkeypatch, items):
    async def core(items: list[dict[str, str]]) -> str:
        raise AssertionError("invalid input reached core")
    method = module._standard_tool(core, emit_start="Starting", emit_done="Done")
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await method(module.Tools(), items)
    assert result == "Error: parameter 'items' expected type list[dict[str, str]]"
    context.assert_not_called()


@pytest.mark.parametrize("start", ["1", 1.0, None])
async def test_strict_validation_does_not_coerce_json_scalars(module, monkeypatch, start):
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    assert await module.Tools().read("/x", start=start) == "Error: parameter 'start' expected type int"
    context.assert_not_called()


async def test_strict_validation_accepts_python_builtin_subclasses(module, monkeypatch):
    class Offset(int):
        pass
    context = AsyncMock(return_value="accepted")
    monkeypatch.setattr(module, "_tool_context", context)
    assert await module.Tools().read("/x", start=Offset(1)) == "accepted"
    context.assert_awaited_once()


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
    (-5, -3, [6, 7]), (-5, 9, [6, 7, 8]), (3, -3, [3, 4, 5, 6, 7]),
    (0, 3, [1, 2]), (5, 3, []), (50, 0, []),
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


def test_positive_read_streams_without_unbounded_read(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("".join(f"line{i}\n" for i in range(100_000)))
    source = lathe._READ_SCRIPT + f'''
import builtins
_real_open = builtins.open
class GuardedFile:
    def __init__(self, file): self.file = file
    def __enter__(self): return self
    def __exit__(self, *args): return self.file.__exit__(*args)
    def __iter__(self): return iter(self.file)
    def read(self, size=-1):
        if size < 0: raise AssertionError("unbounded read")
        return self.file.read(size)
    def readline(self, size=-1):
        if size < 0: raise AssertionError("unbounded readline")
        return self.file.readline(size)
def guarded_open(*args, **kwargs): return GuardedFile(_real_open(*args, **kwargs))
builtins.open = guarded_open
print(read_file({str(path)!r}, 50_000, 50_003))
'''
    result = run_script(source)
    assert "100000 lines total" in result
    assert result.splitlines()[1:] == [
        "50000: line49999", "50001: line50000", "50002: line50001"]


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
@pytest.mark.parametrize("budget", [1, 4, 100])
def test_rg_search_bounds_and_concrete_evidence(tree, kind, budget):
    result = script_call(kind, "search", str(tree), None if kind == "GLOB" else "needle", "**/*.{py,txt}", budget)
    header, *body = result.splitlines()
    assert len(body) <= budget
    assert body
    assert ("limit reached" in header) == (budget < (13 if kind == "GLOB" else 22))
    for line in body:
        if kind == "GLOB":
            assert Path(line).is_file()
        else:
            path, number, text = line.rsplit(":", 2)
            assert text == Path(path).read_text().splitlines()[int(number) - 1]
            assert "needle" in text


@pytest.mark.parametrize("kind", ["GLOB", "GREP"])
def test_rg_globs_use_standard_braces_and_relative_scope(tree, kind):
    def search(scope):
        return script_call(kind, "search", str(tree), None if kind == "GLOB" else "needle", scope, 100)
    assert str(tree / "a.py") in search("**/*.{py,txt}")
    assert str(tree / "b.txt") in search("**/*.{py,txt}")
    assert str(tree / "b.txt") not in search("**/*.py")
    assert search(str(tree / "*.py")).startswith("Error:")
    assert search("**/*.py,**/*.txt").startswith("0 results")


def test_rg_regex_errors_binary_and_large_output(tmp_path):
    root = tmp_path.resolve()
    (root / "binary").write_bytes(b"needle\0" * 1000)
    path = root / "large.txt"
    path.write_text("needle\n" * 50000 + "late marker\n")
    assert script_call("GREP", "search", str(root), "[", "**/*", 5).startswith("Error:")
    assert script_call("GREP", "search", str(root), "absent", "**/*", 5).startswith("0 results")
    result = script_call("GREP", "search", str(root), "needle", "**/*.txt", 5)
    assert len(result.splitlines()) == 6 and "limit reached" in result
    assert script_call("GREP", "search", str(root), "late marker", "**/*", 5).endswith(f"{path}:50001:late marker")
    path.write_text("needle" + "x" * 100000 + "\n")
    result = script_call("GREP", "search", str(root), "needle", "**/*", 5)
    assert len(result) < 1000 and "needle" in result


@pytest.mark.parametrize("fault", [None, "digest", "url", "member"])
def test_rg_blank_sandbox_acquisition_is_verified_and_cached(tmp_path, monkeypatch, fault):
    # Execute the production bootstrap with an in-memory HTTPS fixture and a
    # real tar archive. No dependency on rg being installed in the test process.
    namespace = {}
    exec(lathe._SEARCH_SCRIPT, namespace)
    root = tmp_path / "lathe"
    original_path = namespace["Path"]
    namespace["Path"] = lambda value: root if value == "/tmp/lathe" else original_path(value)
    monkeypatch.setattr(namespace["shutil"], "which", lambda name: None)
    name = "ripgrep-99.1.2-x86_64-unknown-linux-musl"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        payload = b"#!/bin/sh\necho rg-fixture\n"
        member = tarfile.TarInfo(name + "/rg")
        member.size = len(payload)
        if fault == "member":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
            member.size = 0
        tar.addfile(member, io.BytesIO(payload))
    data = archive.getvalue()
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/99.1.2/{name}.tar.gz"
    release = {"tag_name": "99.1.2", "assets": [{
        "name": name + ".tar.gz", "browser_download_url": url if fault != "url" else "https://invalid.test/rg",
        "digest": "sha256:" + (hashlib.sha256(data).hexdigest() if fault != "digest" else "0" * 64),
    }]}
    calls = []
    def fetch(requested, timeout):
        calls.append(requested)
        assert timeout == 20
        if requested == "https://api.github.com/repos/BurntSushi/ripgrep/releases/latest":
            return io.BytesIO(json.dumps(release).encode())
        assert requested == url
        return io.BytesIO(data)
    monkeypatch.setattr(namespace["urllib"].request, "urlopen", fetch)
    if fault:
        with pytest.raises(ValueError):
            namespace["_ripgrep"]()
        assert not (root / "rg").exists()
    else:
        binary = namespace["_ripgrep"]()
        assert subprocess.check_output([binary], text=True).strip() == "rg-fixture"
        assert namespace["_ripgrep"]() == binary
        assert len(calls) == 2

@pytest.mark.parametrize("name,kwargs", [
    ("read", {}), ("write", {"content": "x"}),
    ("edit", {"old_string": "x", "new_string": "y"}), ("view", {}),
])
async def test_relative_file_path_never_reaches_http(tools, name, kwargs):
    result = await getattr(lathe, "_core_" + name)(tools.valves, "sb", None, path="relative", **kwargs)
    assert "absolute path" in result


@pytest.mark.parametrize("kind", ["glob", "grep"])
@pytest.mark.parametrize("budget,expected", [(0, 1), (999, 500)])
async def test_search_core_executes_quoted_arguments_and_clamps(tools, tmp_path, transport, kind, budget, expected):
    (tmp_path / "a'quoted.txt").write_text("needle's marker\n" * 600)
    def handler(request):
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        command = json.loads(request.content)["command"]
        source = shlex.split(command)[2].replace("/home/daytona/workspace", str(tmp_path))
        return httpx.Response(200, json={"exitCode": 0, "result": run_script(source)})
    async with httpx.AsyncClient(transport=transport(handler)) as client:
        kwargs = {"pattern": "**/*.txt"} if kind == "glob" else {"pattern": "needle's marker", "files": "**/*.txt"}
        result = await getattr(lathe, "_core_" + kind)(tools.valves, "sb", client, max_lines=budget, **kwargs)
    assert len(result.splitlines()[1:]) == (1 if kind == "glob" else expected)
    assert str(tmp_path / "a'quoted.txt") in result


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
        str(tmp_path / "pid"), str(tmp_path / "log"), str(tmp_path / "keep"),
        str(tmp_path / "lease"))
    script_path = tmp_path / "wrapper"
    script_path.write_text(script)
    result = subprocess.run(["bash", str(script_path)], text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert result.stdout == value
    assert (tmp_path / "log").read_text() == value
    assert not script_path.exists()
    assert (tmp_path / "keep").read_text() == ""
    assert (tmp_path / "lease").is_file()
    # macOS ships Bash 3 (no BASHPID); Linux sidecar PIDs are a live-suite concern.


async def test_completed_bash_retention_is_bounded(tools, monkeypatch, tmp_path, transport):
    root = tmp_path / "lathe"
    monkeypatch.setattr(lathe, "_EPHEMERAL_ROOT", str(root))
    command_root = root / "cmd"
    command_root.mkdir(parents=True)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    original_reap_builder = lathe._build_bash_reap_script
    monkeypatch.setattr(
        lathe, "_build_bash_reap_script",
        lambda preserve_log_for=None, release_lease_for=None: original_reap_builder(
            preserve_log_for, release_lease_for, str(proc_root),
        ),
    )

    def sidecar(command_id, *, pid="", spill=False, lease=False):
        directory = command_root / command_id
        directory.mkdir()
        (directory / "exit").write_text("0\n")
        (directory / "keep").write_text(pid)
        (directory / "sh").write_text("export SECRET=plaintext\n")
        (directory / "log").write_text("command output\n")
        if spill:
            (directory / "spill").touch()
        if lease:
            (directory / "lease").touch()
        return directory

    ordinary = sidecar("ordinary")
    retained_pid = os.getpid()
    retained_start = "123456"
    retained_proc = proc_root / str(retained_pid)
    retained_proc.mkdir()
    retained_proc.joinpath("stat").write_text(
        f"{retained_pid} (test process) " + " ".join(
            ["S"] + ["0"] * 18 + [retained_start]
        )
    )
    retained = sidecar("retained", pid=f"{retained_pid}:{retained_start}\n")
    old_spill = sidecar("old-spill", spill=True)
    leased = sidecar("leased", lease=True)
    expired_failure = sidecar("expired-failure")
    expired = time.time() - 24 * 60 * 60 - 1
    os.utime(expired_failure / "exit", (expired, expired))
    deleted_sessions = []
    delete_attempts = {}

    def handler(request):
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            command = shlex.split(json.loads(request.content)["command"])
            return httpx.Response(200, json={"exitCode": 0, "result": run_script(command[2])})
        if request.method == "DELETE" and "/process/session/" in request.url.path:
            session = request.url.path.rsplit("/", 1)[-1]
            delete_attempts[session] = delete_attempts.get(session, 0) + 1
            if session in ("lathe-cmd-ordinary", "lathe-cmd-expired-failure") and delete_attempts[session] == 1:
                return httpx.Response(503)
            deleted_sessions.append(session)
            return httpx.Response(204)
        assert (request.method, request.url.path) == ("DELETE", "/sb/files/")
        shutil.rmtree(request.url.params["path"])
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        await lathe._reap_bash_commands(tools.valves, "sb", client)
        assert {p.name for p in ordinary.iterdir()} == {"exit", "log", "reap"}
        assert old_spill.exists()
        assert leased.exists()
        assert {p.name for p in retained.iterdir()} == {"keep", "log", "exit"}
        assert "lathe-cmd-retained" not in deleted_sessions
        assert "lathe-cmd-old-spill" in deleted_sessions
        assert expired_failure.exists(), "failed session cleanup must retain its retry record"

        await lathe._reap_bash_commands(tools.valves, "sb", client)
        assert (ordinary / "log").read_text() == "command output\n"
        assert delete_attempts["lathe-cmd-ordinary"] == 2
        assert delete_attempts["lathe-cmd-expired-failure"] == 2
        assert not expired_failure.exists()

        current = sidecar("current")
        await lathe._reap_bash_commands(
            tools.valves, "sb", client, preserve_log_for="current",
        )
        assert {p.name for p in current.iterdir()} == {"log", "exit", "reap", "session_done"}
        assert "plaintext" not in "".join(p.read_text() for p in current.iterdir())

        await lathe._reap_bash_commands(
            tools.valves, "sb", client, release_lease_for="leased",
        )
        assert (leased / "exit").read_text() == "0\n"

        # A reused PID with a different process start time is not retained.
        retained_proc.joinpath("stat").write_text(
            f"{retained_pid} (replacement) " + " ".join(
                ["S"] + ["0"] * 18 + ["999999"]
            )
        )
        await lathe._reap_bash_commands(tools.valves, "sb", client)
        assert "lathe-cmd-retained" in deleted_sessions
        assert (retained / "log").read_text() == "command output\n"

        await lathe._reap_bash_commands(tools.valves, "sb", client)
        assert (current / "log").read_text() == "command output\n"
        assert delete_attempts["lathe-cmd-current"] == 1, "retained evidence must not repeat session cleanup"
        expired = time.time() - 24 * 60 * 60 - 1
        for directory in (current, ordinary, retained, old_spill, leased):
            os.utime(directory / "exit", (expired, expired))
        await lathe._reap_bash_commands(tools.valves, "sb", client)
        assert not list(command_root.iterdir())


async def test_bash_launch_failure_discards_session_and_wrapper(tools, sandbox, http):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            return httpx.Response(200, json={"exitCode": 0, "result": ""})
        if (request.method, request.url.path) == ("POST", "/sb/files/upload"):
            return httpx.Response(200)
        if (request.method, request.url.path) == ("POST", "/sb/process/session"):
            return httpx.Response(201)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(500, text="launch failed")
        if request.method == "DELETE":
            return httpx.Response(204)
        pytest.fail(f"unexpected request: {request.method} {request.url.path}")

    http(handler)
    result = await tools.bash("true", __user__=USER)
    assert result.startswith("API error: HTTP 500")
    assert any(method == "DELETE" and path == "/sb/files/" for method, path in requests)
    assert any(method == "DELETE" and "/process/session/lathe-cmd-" in path
               for method, path in requests)


@pytest.mark.parametrize("command,log_size", [
    ("python3 -c 'print(\"x\" * 1000000)'", 1000001),
    ("python3 -c 'import sys; sys.stdout.buffer.write(bytes([255]) * 20000)'", 20000),
    ("python3 -c 'print(\"[Backgrounded after\" * 4000)'", 76001),
])
async def test_bash_large_log_survives_unrelated_chat(tools, sandbox, http, monkeypatch, tmp_path, clock, command, log_size):
    monkeypatch.setattr(lathe, "_EPHEMERAL_ROOT", str(tmp_path / "lathe"))
    for chat in ("a", "b"):
        tools._chat_state[chat] = {"init": True, "pending": []}
    deleted = []
    exits = {}

    def handler(request):
        if request.method == "POST" and request.url.path == "/sb/files/upload":
            path = Path(request.url.params["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0])
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/sb/process/session":
            return httpx.Response(201)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            command = json.loads(request.content)["command"]
            process = subprocess.run(command, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
            exits[request.url.path.removesuffix("/exec")] = process.returncode
            return httpx.Response(200, json={"cmdId": "cmd"})
        if request.method == "POST" and request.url.path == "/sb/process/execute":
            script = shlex.split(json.loads(request.content)["command"])[2]
            return httpx.Response(200, json={"exitCode": 0, "result": run_script(script)})
        if request.method == "GET" and "/process/session/" in request.url.path:
            assert not request.url.path.endswith("/logs"), "unbounded log download"
            return httpx.Response(200, json={"commands": [{"id": "cmd", "exitCode": exits[request.url.path]}]})
        assert request.method == "DELETE" and "/process/session/" in request.url.path
        deleted.append(request.url.path)
        return httpx.Response(204)

    http(handler)
    events = []
    async def emit(event):
        events.append(event)
    output = await tools.bash(command, workdir=str(tmp_path),
                              __user__=USER, __chat_id__="a", __event_emitter__=emit)
    assert events[-1]["data"]["description"] == "Command complete"
    assert "Bounded tail preview" in output and len(output.encode()) < 53 * 1024
    log = next((tmp_path / "lathe" / "cmd").glob("*/log"))
    assert str(log) in output, "clipped previews must identify recoverable full output"
    await tools.bash("echo unrelated", workdir=str(tmp_path), __user__=USER, __chat_id__="b")
    preview = await tools.read(str(log), __user__=USER, __chat_id__="a")
    assert "Text preview truncated" in preview and len(preview.encode()) < 53 * 1024
    assert log.stat().st_size == log_size
    assert (log.parent / "exit").read_text().strip() == "0"
    assert deleted, "sessions should be cleaned independently of result files"
    emissions = []
    async def emit(event):
        emissions.append(event)
    tools._chat_state["a"]["pending"].extend(["workspace orientation", "background job finished"])
    failure = await tools.bash("echo failed; false", workdir=str(tmp_path), __user__=USER,
                               __chat_id__="a", __event_emitter__=emit)
    assert failure == "workspace orientation\n\nbackground job finished\n\nExit code: 1\nfailed\n"
    assert emissions[-1]["data"]["description"] == "Command complete"
    subsequent = await tools.bash("printf next", workdir=str(tmp_path), __user__=USER, __chat_id__="a")
    assert subsequent == "next"
    literal = await tools.bash("printf 'Error: expected output'", workdir=str(tmp_path),
                               __user__=USER, __chat_id__="a")
    assert literal == "Error: expected output"


@pytest.mark.parametrize("start,stop", [(1, 0), (-1, 0), (1, -1)])
def test_read_oversized_line_has_explicit_byte_preview(tmp_path, start, stop):
    path = tmp_path / "huge.txt"
    path.write_text("é" * 500000 + "\nlast\n")
    result = run_script(lathe._READ_SCRIPT + f"\nprint(read_file({str(path)!r}, {start}, {stop}))")
    assert len(result.encode()) < 53 * 1024
    if start == -1:
        assert "2: last" in result
    else:
        assert "Text preview truncated" in result and str(path) in result


@pytest.mark.parametrize("newline", ["\r", "\r\n", "\n"])
@pytest.mark.parametrize("start,stop", [(2, 0), (-2, 0), (1, -1)])
def test_bounded_read_preserves_universal_newlines(tmp_path, newline, start, stop):
    path = tmp_path / "newlines.txt"
    path.write_bytes(newline.join(["a", "b", "c", ""]).encode())
    result = script_call("READ", "read_file", str(path), start, stop)
    expected = ["1: a", "2: b"] if stop == -1 else ["2: b", "3: c"]
    assert "3 lines total" in result
    assert result.splitlines()[1:] == expected
    assert "\r" not in result


def test_read_byte_clipping_labels_requested_not_shown_range(tmp_path):
    path = tmp_path / "numbering.txt"
    path.write_text(("x" * 24 + "\n") * 2000)
    result = script_call("READ", "read_file", str(path), 1, 0)
    assert "requested lines 1-2000 (preview clipped)" in result
    assert "showing lines" not in result
    assert "2000: " not in result


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("failure", ["http", "script", "malformed"])
async def test_bash_preview_failure_preserves_lifecycle(
    tools, sandbox, http, monkeypatch, tmp_path, clock, background, failure,
):
    monkeypatch.setattr(lathe, "_EPHEMERAL_ROOT", str(tmp_path / "lathe"))
    tools._chat_state["chat"] = {"init": True, "pending": []}
    polls = 0
    previews = 0
    deleted = []
    def handler(request):
        nonlocal polls, previews
        if request.method == "POST" and request.url.path == "/sb/files/upload":
            path = Path(request.url.params["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0])
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/sb/process/session":
            return httpx.Response(201)
        if request.method == "POST" and request.url.path.endswith("/exec"):
            subprocess.run(json.loads(request.content)["command"], shell=True, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return httpx.Response(200, json={"cmdId": "cmd"})
        if request.method == "GET" and "/process/session/" in request.url.path:
            polls += 1
            command = {"id": "cmd"}
            if not background or polls > 1:
                command["exitCode"] = 0
            return httpx.Response(200, json={"commands": [command]})
        if request.method == "POST" and request.url.path == "/sb/process/execute":
            script = shlex.split(json.loads(request.content)["command"])[2]
            if "f.seek" in script:
                previews += 1
                if previews == 1:
                    if failure == "http": return httpx.Response(503)
                    if failure == "script": return httpx.Response(200, json={"exitCode": 1, "result": "preview failed"})
                    return httpx.Response(200, json={"exitCode": 0, "result": "not-json"})
            return httpx.Response(200, json={"exitCode": 0, "result": run_script(script)})
        assert request.method == "DELETE" and "/process/session/" in request.url.path
        deleted.append(request.url.path)
        return httpx.Response(204)
    http(handler)
    result = await tools.bash("echo lifecycle-output", workdir=str(tmp_path),
        foreground_seconds=0 if background else 30, __user__=USER, __chat_id__="chat")
    assert "Preview unavailable" in result
    if background:
        assert "Backgrounded after" in result
        log = next((tmp_path / "lathe" / "cmd").glob("*/log"))
        async with asyncio.timeout(5):
            while True:
                result = await tools.read(str(log), __user__=USER, __chat_id__="chat")
                if "Background job completed" in result: break
                await asyncio.sleep(0)
        assert "lifecycle-output" in result
    assert deleted, "finished session cleanup must survive a preview failure"
    log = next((tmp_path / "lathe" / "cmd").glob("*/log"))
    assert log.read_text() == "lifecycle-output\n"


async def test_failed_write_does_not_emit_success(tools, sandbox, http, tmp_path):
    tools._chat_state["chat"] = {"init": True, "pending": []}
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied")
    emissions = []
    def handler(request):
        assert request.url.path == "/sb/process/execute"
        script = shlex.split(json.loads(request.content)["command"])[2]
        return httpx.Response(200, json={"exitCode": 0, "result": run_script(script)})
    http(handler)
    async def emit(event):
        emissions.append(event)
    result = await tools.write(str(parent_file / "file"), "data", __user__=USER,
                               __chat_id__="chat", __event_emitter__=emit)
    assert result.startswith("Error:")
    assert not any(e["data"]["description"] == "Write complete" for e in emissions)


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


@pytest.mark.parametrize("build_script", [lathe._build_onboard_script, lathe._build_snapshot_script])
def test_onboarding_merges_global_and_project_skills(tmp_path, build_script):
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
    unique = home / ".agents" / "skills" / "global-only"
    unique.mkdir()
    (unique / "SKILL.md").write_text("---\nname: global-only\ndescription: unique global skill\n---\nPRIVATE BODY")
    result = lathe._render_context(run_script(build_script(str(project)), env={**os.environ, "HOME": str(home)}))
    if build_script is lathe._build_onboard_script:
        assert "global instructions" in result and "project instructions" in result
        assert str(project / "AGENTS.md") in result
    else:
        assert "global instructions" not in result and "project instructions" not in result
    assert "- **shared**: project skill" in result
    assert "- **shared**: global skill" not in result
    assert "- **global-only**: unique global skill" in result
    assert f"`{unique / 'SKILL.md'}`" in result
    assert f"`{project / '.agents' / 'skills' / 'shared' / 'SKILL.md'}`" in result
    assert f"`{home / '.agents' / 'skills' / 'shared' / 'SKILL.md'}`" not in result
    assert "PRIVATE BODY" not in result


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("build_script", [lathe._build_onboard_script, lathe._build_snapshot_script])
def test_onboarding_announces_skill_locations_even_without_skills(tmp_path, existing, build_script):
    home = tmp_path / "home"
    project = tmp_path / "project"
    global_skills = home / ".agents" / "skills"
    project_skills = project / ".agents" / "skills"
    if existing:
        global_skills.mkdir(parents=True)
        project_skills.mkdir(parents=True)
    result = lathe._render_context(run_script(build_script(str(project)), env={**os.environ, "HOME": str(home)}))
    assert f"Scanning global skills via {global_skills}/<skill-name>/SKILL.md" in result
    assert f"Scanning project skills via {project_skills}/<skill-name>/SKILL.md" in result
    assert "<skill-name>/SKILL.md" in result


@pytest.mark.parametrize("build_script", [lathe._build_onboard_script, lathe._build_snapshot_script])
@pytest.mark.parametrize("xdg", [False, True])
def test_skill_catalog_scopes_precedence_and_read_only_discovery(tmp_path, build_script, xdg):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("project instructions")
    data = tmp_path / "custom data" if xdg else home / ".local/share"
    environment = {**os.environ, "HOME": str(home)}
    environment.pop("XDG_DATA_HOME", None)
    if xdg:
        environment["XDG_DATA_HOME"] = str(data)
    # Discovery works without a skill store and may neither fetch nor grow one.
    guard = (
        "import socket\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('catalog attempted network access')\n"
        "socket.socket = forbidden\n"
    )
    script = guard + build_script(str(project))
    cold = lathe._render_context(run_script(script, env=environment))
    assert str(project) in cold
    if build_script is lathe._build_onboard_script:
        assert "project instructions" in cold
    assert "# Available Skills" not in cold
    assert not data.exists()

    # Old bundles and other applications' skill stores are not discovery roots.
    for base in (home / ".cache/lathe" / ("a" * 40), data / "other-agent"):
        stale = base / "skills/stale/SKILL.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("---\nname: stale-support\ndescription: ignored\n---\n")
    if xdg:
        ignored = home / ".local/share/lathe/skills/ignored/SKILL.md"
        ignored.parent.mkdir(parents=True)
        ignored.write_text("---\nname: ignored-default-root\n---\n")

    shared = home / ".agents"
    local = data / "lathe"
    project_agents = project / ".agents"
    for base, label in [(shared, "shared"), (local, "Lathe"), (project_agents, "project")]:
        skill = base / "skills" / "my-file-browser" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: file-browser\ndescription: {label}\n---\nPRIVATE BODY")
        unique = base / "skills" / "unique" / "SKILL.md"
        unique.parent.mkdir()
        unique.write_text(f"---\nname: {label}-only\ndescription: unique\n---\n")
        result = lathe._render_context(run_script(script, env=environment))
        assert f"- **file-browser**: {label}" in result
        assert f"`{skill}`" in result
        assert result.count("- **file-browser**:") == 1
        assert "PRIVATE BODY" not in result
        assert "stale-support" not in result and "ignored-default-root" not in result
    for base in (shared, local, project_agents):
        assert f"`{base / 'skills/unique/SKILL.md'}`" in result

    # Forgetting is literal: later discovery does not regenerate deleted skills.
    for base, fallback in [(project_agents, "Lathe"), (local, "shared"), (shared, None)]:
        skill = base / "skills/my-file-browser/SKILL.md"
        skill.unlink()
        result = lathe._render_context(run_script(script, env=environment))
        assert not skill.exists()
        if fallback:
            assert f"- **file-browser**: {fallback}" in result
        else:
            assert "- **file-browser**:" not in result


def test_auto_init_scans_default_workspace_skills(tmp_path):
    home = tmp_path / "home"
    result = lathe._render_context(run_script(lathe._SNAPSHOT_SCRIPT, env={**os.environ, "HOME": str(home)}))
    assert f"Scanning global skills via {home / '.agents' / 'skills'}/<skill-name>/SKILL.md" in result
    assert "Scanning project skills via /home/daytona/workspace/.agents/skills/<skill-name>/SKILL.md" in result


@pytest.mark.parametrize("build_script", [lathe._build_onboard_script, lathe._build_snapshot_script])
def test_skill_catalog_real_yaml_and_malformed_neighbors(tmp_path, build_script):
    project = tmp_path / "project"
    root = project / ".agents/skills"
    contents = {
        "quoted": 'name: "quoted: skill"\ndescription: "Handles colons: and # hashes"',
        "folded": 'name: folded\ndescription: >-\n  Two lines\n  folded together.',
        "broken": 'name: [unterminated',
        "unsafe": "!!python/object/apply:builtins.dict [[[name, injected]]]",
        "wrong-shape": '[not, a, mapping]',
        "wrong-types": 'name: [not, a, string]\ndescription: 3',
        "bad-timestamp": 'description: 2026-99-99',
        "huge": 'description: ' + 'x' * 17000,
        "deep": 'name: ' + '[' * 1500 + 'x' + ']' * 1500,
    }
    for name, metadata in contents.items():
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\n{metadata}\n---\nPRIVATE BODY")
    # Simulate blank sandbox Python: even an accidental host-library import fails.
    guard = (
        "import builtins, socket\n"
        "original_import = builtins.__import__\n"
        "def limited_import(name, *args, **kwargs):\n"
        "    if name in ('yaml', 'pydantic', 'httpx'):\n"
        "        raise AssertionError('sandbox imported host dependency')\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = limited_import\n"
        "def forbidden(*args, **kwargs):\n"
        "    raise AssertionError('sandbox attempted network')\n"
        "socket.socket = forbidden\n"
    )
    raw = run_script(guard + build_script(str(project)),
                     env={**os.environ, "HOME": str(tmp_path / "home")})
    assert "PRIVATE BODY" not in raw
    records = json.loads(raw)["skills"]
    assert all(len(record["metadata"]) <= 16384 for record in records)
    result = lathe._render_context(raw)
    assert "- **quoted: skill**: Handles colons: and # hashes" in result
    assert "- **folded**: Two lines folded together." in result
    for name in ("broken", "unsafe", "wrong-shape", "wrong-types", "bad-timestamp", "huge", "deep"):
        assert f"- **{name}**: Metadata unavailable; read the skill for instructions." in result
        assert f"`{root / name / 'SKILL.md'}`" in result


async def test_onboard_and_first_snapshot_render_yaml_on_host(tools, sandbox, monkeypatch, http, tmp_path):
    skill = tmp_path / ".agents/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: "host: parsed"\ndescription: >-\n  folded\n  description\n---\nPRIVATE BODY')
    uploaded = {}
    async def upload(valves, sandbox_id, client, path, content):
        uploaded[path] = content.decode()
    monkeypatch.setattr(lathe, "_upload_file", upload)
    monkeypatch.setattr(lathe, "_SNAPSHOT_SCRIPT", lathe._build_snapshot_script(str(tmp_path)))
    def handler(request):
        if request.url.path == "/sandbox/sb":
            return httpx.Response(200, json={"cpu": 2})
        assert request.url.path == "/sb/process/execute"
        command = shlex.split(json.loads(request.content)["command"])
        script = command[2] if command[1] == "-c" else uploaded[command[1]]
        raw = run_script(script, env={**os.environ, "HOME": str(tmp_path / "home")})
        return httpx.Response(200, json={"exitCode": 0, "result": raw})
    http(handler)
    onboard = await tools.onboard(str(tmp_path), __user__=USER)
    tools._chat_state["yaml"] = {"init": True, "pending": []}
    async with lathe.httpx.AsyncClient() as client:
        await lathe._chat_auto_init(tools.valves, "sb", client, tools._chat_state, "yaml", USER)
    snapshot = "\n".join(lathe._drain_harness_messages(tools._chat_state, "yaml", None))
    for result in (onboard, snapshot):
        assert "- **host: parsed**: folded description" in result
        assert f"`{skill}`" in result
        assert "PRIVATE BODY" not in result


@pytest.mark.parametrize("wrapped", [False, True])
async def test_rendered_manual(tools, wrapped):
    tools.valves.preview_wrapper_url = "https://wrapper.test" if wrapped else ""
    for page in tools._MANPAGES:
        result = await tools.lathe(page)
        assert len(result) > 500 and not result.startswith("Error:")
        assert not re.search(r"\{(?:tool_catalog|volume_note|destroy_volume_note|preview_access_note)\}", result)
    overview = await tools.lathe()
    assert "bash(" in overview
    assert ("try the wrapper" in overview) == wrapped
    assert "overview" in await tools.lathe("nonexistent")


@pytest.mark.parametrize("configured", [False, True])
async def test_chat_init_and_notice_delivery(tools, transport, configured):
    if configured:
        tools.valves.onboarding_instructions = "Use /opt/intranet packages. Keep ${HOME} literal."
        tools.valves.handbook_url = "https://handbook.test/draft.md"
    calls = []
    def handler(request):
        calls.append(request)
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            return httpx.Response(200, json={"exitCode": 0, "result": json.dumps({"sections": ["workspace snapshot"], "skills": []})})
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
    if configured:
        assert tools.valves.onboarding_instructions in delivered
        assert tools.valves.handbook_url in delivered
    assert lathe._drain_harness_messages(tools._chat_state, "a", None) == []


@pytest.mark.parametrize("field", ["onboarding_instructions", "handbook_url"])
async def test_installation_manual_guidance_is_literal_and_optional(tools, field):
    assert getattr(tools.valves, field) == ""
    assert lathe._installation_guidance(tools.valves) == ""
    before = {page: await tools.lathe(page) for page in ("overview", "services")}
    value = "https://handbook.test/draft.md?x={preview_access_note}&y=${HOME}"
    setattr(tools.valves, field, value)
    for page in before:
        result = await tools.lathe(page)
        assert result.startswith(before[page])
        assert value in result
    setattr(tools.valves, field, " \n ")
    for page in before:
        assert await tools.lathe(page) == before[page]


async def test_first_image_queues_admin_guidance(tools, monkeypatch, http):
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(return_value=("sb", None)))
    tools.valves.onboarding_instructions = "Use the local package mirror."
    tools.valves.handbook_url = "https://handbook.test/current.md"

    def handler(request):
        if request.url.path == "/sandbox/sb":
            return httpx.Response(503)
        if request.url.path == "/sb/files/download":
            return httpx.Response(200, content=PNG)
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        return httpx.Response(200, json={"exitCode": 0, "result": "sandbox text"})

    http(handler)
    image = await tools.view("/image", __user__=USER, __chat_id__="admin-guidance")
    assert image == "data:image/png;base64," + base64.b64encode(PNG).decode()
    result = await tools.read("/text", __user__=USER, __chat_id__="admin-guidance")
    assert tools.valves.onboarding_instructions in result
    assert tools.valves.handbook_url in result
    result = await tools.read("/text", __user__=USER, __chat_id__="admin-guidance")
    assert tools.valves.onboarding_instructions not in result
    assert tools.valves.handbook_url not in result


@pytest.mark.parametrize("tool", ["read", "view"])
@pytest.mark.parametrize("failure", ["http", "missing"])
async def test_first_failure_delivers_context_once(tools, monkeypatch, http, tmp_path, tool, failure):
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(
        side_effect=[("sb", "sandbox recreated"), ("sb", None)]))
    snapshot_done = False
    failing = True

    def handler(request):
        nonlocal snapshot_done
        if request.url.path == "/sandbox/sb":
            return httpx.Response(200, json={"cpu": 2})
        if request.url.path == "/sb/process/execute":
            script = shlex.split(json.loads(request.content)["command"])[2]
            if not snapshot_done:
                snapshot_done = True
                return httpx.Response(200, json={"exitCode": 0, "result": json.dumps({"sections": ["workspace snapshot"], "skills": []})})
            if failing and failure == "http":
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, json={"exitCode": 0, "result": run_script(script)})
        assert request.url.path == "/sb/files/download"
        assert tool == "view" and failing
        return httpx.Response(503 if failure == "http" else 404)

    http(handler)
    user = {**USER, "valves": tools.UserValves(env_vars='{"GITHUB_TOKEN":"hidden-value"}')}
    result = await getattr(tools, tool)(str(tmp_path / "missing"), __user__=user, __chat_id__="a")
    assert all(s in result for s in ["sandbox recreated", "workspace snapshot", "GITHUB_TOKEN"])
    assert "hidden-value" not in result
    assert "error:" in result.lower()
    failing = False
    path = tmp_path / "next.txt"
    path.write_text("next result")
    subsequent = await tools.read(str(path), __user__=user, __chat_id__="a")
    assert "next result" in subsequent
    assert not any(s in subsequent for s in ["sandbox recreated", "workspace snapshot", "GITHUB_TOKEN"])


@pytest.mark.parametrize("prefix", ["", "earlier output\n" * 10000], ids=["short", "large"])
async def test_background_bash_poll_delivers_once(tools, sandbox, http, clock, tmp_path, monkeypatch, prefix):
    monkeypatch.setattr(lathe, "_EPHEMERAL_ROOT", str(tmp_path))
    log = tmp_path / "cmd" / "job" / "log"
    log.parent.mkdir(parents=True)
    ending = "\n".join(f"final output {n}" for n in range(5))
    log.write_text(prefix + ending + "\n")
    tools._chat_state["chat"] = {"init": True, "pending": []}
    statuses = iter([httpx.Response(503), httpx.Response(200, json={"commands": [{"id": "cmd"}]}),
                     httpx.Response(200, json={"commands": [{"id": "cmd", "exitCode": 7}]})])
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if (request.method, request.url.path) == ("POST", "/sb/process/execute"):
            script = shlex.split(json.loads(request.content)["command"])[2]
            if "f.seek" in script:
                return httpx.Response(200, json={"exitCode": 0, "result": run_script(script)})
            return httpx.Response(200, json={"exitCode": 0, "result": "caller output"})
        assert request.method == "GET"
        if request.url.path == "/sb/process/session/session":
            return next(statuses)
        assert request.url.path == "/sb/process/session/session/command/cmd/logs"
        return httpx.Response(200, text="final output")
    http(handler)
    await lathe._poll_bg_bash(tools.valves, "sb", "session", "cmd", "job", 0, tools._chat_state, "chat")
    assert len(requests) == 5
    # Observe delivery through an ordinary wrapper, not by draining its queue.
    assert await tools.read("/file", __user__=USER, __chat_id__="other") == "caller output"
    result = await tools.read("/file", __user__=USER, __chat_id__="chat")
    assert result.count("Background job completed") == 1
    assert all(s in result for s in ["job", "Exit code: 7", "final output", "caller output"])
    displayed_tail = result.split("Last lines of output:\n", 1)[1].split("\nFull output:", 1)[0]
    assert displayed_tail == "\n".join(f"  {line}" for line in ending.splitlines())
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


async def test_started_tool_request_budget(tools, http):
    tools._chat_state["chat"] = {"init": True, "pending": []}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "state": "started", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb":
            return httpx.Response(200, json={"id": "sb", "state": "started"})
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        command = json.loads(request.content)["command"]
        result = "" if command == "bash -c 'mkdir -p /home/daytona/workspace'" else "file contents"
        return httpx.Response(200, json={"exitCode": 0, "result": result})

    http(handler)
    for _ in range(2):
        before = len(calls)
        assert await tools.read("/file", __user__=USER, __chat_id__="chat") == "file contents"
        assert len(calls) - before == 4
        assert calls[before:before + 2] == [("GET", "/sandbox"), ("GET", "/sandbox/sb")]


@pytest.mark.parametrize("initial", ["created", "conflicted", "stopped", "starting", "error"])
@pytest.mark.parametrize("failure", ["http", "timeout", "mkdir"])
async def test_startup_readiness_retries_and_creates_workspace(
    tools, transport, clock, tmp_path, initial, failure,
):
    observations = iter([initial, "started"])
    workspace = tmp_path / "workspace with spaces"
    attempts = []

    def handler(request):
        if request.url.path == "/sandbox" and request.method == "GET":
            return httpx.Response(200, json=[] if initial in ("created", "conflicted") else [{
                "id": "sb", "state": "started", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox" and request.method == "POST":
            assert "volumes" not in json.loads(request.content)
            if initial == "conflicted":
                return httpx.Response(409)
            return httpx.Response(200, json={"id": "sb", "state": "started"})
        if request.url.path == "/sandbox/test/owner@example.test":
            return httpx.Response(200, json={
                "id": "sb", "state": "started", "name": "test/owner@example.test",
                "labels": {"test": USER["email"]},
            })
        if request.url.path == "/sandbox/sb" and request.method == "GET":
            return httpx.Response(200, json={
                "id": "sb", "state": next(observations), "recoverable": True,
            })
        if request.url.path in ("/sandbox/sb/start", "/sandbox/sb/recover"):
            assert request.method == "POST"
            return httpx.Response(200, json={})
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        command = json.loads(request.content)["command"]
        attempts.append(command)
        if len(attempts) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("toolbox starting", request=request)
            if failure == "http":
                return httpx.Response(503)
            return httpx.Response(200, json={"exitCode": 1, "result": "mkdir failed"})
        # Execute the shipped readiness command, with only its absolute root
        # redirected to a real temporary directory.
        argv = shlex.split(command)
        argv[2] = argv[2].replace("/home/daytona/workspace", shlex.quote(str(workspace)))
        result = subprocess.run(argv, capture_output=True, text=True)
        return httpx.Response(200, json={"exitCode": result.returncode, "result": result.stdout})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        sandbox_id, _ = await lathe._ensure_sandbox(tools.valves, USER["email"], client)
    assert sandbox_id == "sb" and workspace.is_dir()
    assert len(attempts) == 2 and clock() >= 1


@pytest.mark.parametrize("failure", ["http", "timeout", "mkdir"])
async def test_started_workspace_failure_never_runs_tool(tools, http, clock, failure):
    tools._chat_state["chat"] = {"init": True, "pending": []}
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb":
            return httpx.Response(200, json={"id": "sb", "state": "started"})
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        assert json.loads(request.content)["command"] == "bash -c 'mkdir -p /home/daytona/workspace'"
        if failure == "timeout":
            raise httpx.ReadTimeout("sandbox stopped after lookup", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, json={"exitCode": 1, "result": "workspace path is a file"})

    http(handler)
    result = await tools.read("/file", __user__=USER, __chat_id__="chat")
    assert "workspace could not be prepared" in result and "Retry this tool call" in result
    assert len(calls) == 3 and clock() == 0


async def test_startup_workspace_failure_is_bounded(tools, transport, clock):
    attempts = []

    def handler(request):
        if (request.method, request.url.path) == ("GET", "/sandbox"):
            return httpx.Response(200, json=[])
        if (request.method, request.url.path) == ("POST", "/sandbox"):
            return httpx.Response(200, json={"id": "sb", "state": "started"})
        assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
        attempts.append(request)
        return httpx.Response(200, json={"exitCode": 1, "result": "mkdir failed"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        with pytest.raises(RuntimeError, match="could not prepare the workspace"):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)
    assert len(attempts) == 30 and clock() == 30


@pytest.mark.parametrize("state,status", [
    ("deleted", 200), ("destroyed", 200), ("started", 404),
])
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


async def test_concurrent_first_calls_converge_by_unique_name(tools, transport):
    label_lookups = 0
    create_calls = 0
    both_looked_up = asyncio.Event()
    calls = []
    winner = {
        "id": "winner", "name": "test/owner@example.test", "state": "started",
        "labels": {"test": USER["email"]},
    }

    async def handler(request):
        nonlocal label_lookups, create_calls
        calls.append((request.method, request.url.path))
        if (request.method, request.url.path) == ("GET", "/sandbox"):
            label_lookups += 1
            if label_lookups == 2:
                both_looked_up.set()
            await both_looked_up.wait()
            return httpx.Response(200, json=[])
        if (request.method, request.url.path) == ("POST", "/sandbox"):
            create_calls += 1
            if create_calls == 1:
                return httpx.Response(200, json=winner)
            return httpx.Response(409, text="Sandbox with name already exists")
        if (request.method, request.url.path) == (
            "GET", "/sandbox/test/owner@example.test",
        ):
            return httpx.Response(200, json=winner)
        assert (request.method, request.url.path) == ("POST", "/winner/process/execute")
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with (
        httpx.AsyncClient(transport=transport(handler)) as first,
        httpx.AsyncClient(transport=transport(handler)) as second,
    ):
        results = await asyncio.gather(
            lathe._ensure_sandbox(tools.valves, USER["email"], first),
            lathe._ensure_sandbox(tools.valves, USER["email"], second),
        )

    assert sorted(results, key=lambda result: result[1] or "") == [
        ("winner", None),
        ("winner", "[Sandbox was created — this is a fresh environment with no prior files]"),
    ]
    assert label_lookups == 2 and create_calls == 2
    assert calls.count(("GET", "/sandbox/test/owner@example.test")) == 1
    assert calls.count(("POST", "/winner/process/execute")) == 2


async def test_create_conflict_without_authoritative_winner_is_retryable(tools, transport):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if (request.method, request.url.path) == ("GET", "/sandbox"):
            return httpx.Response(200, json=[])
        if (request.method, request.url.path) == ("POST", "/sandbox"):
            return httpx.Response(409, text="Sandbox with name already exists")
        assert (request.method, request.url.path) == (
            "GET", "/sandbox/test/owner@example.test",
        )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        with pytest.raises(RuntimeError, match="Retry this tool call"):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)

    assert calls == [
        ("GET", "/sandbox"),
        ("POST", "/sandbox"),
        ("GET", "/sandbox/test/owner@example.test"),
    ]


@pytest.mark.parametrize("state", ["deleting", "destroying"])
async def test_deletion_in_progress_never_creates_replacement(tools, transport, clock, state):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        assert request.url.path == "/sandbox/sb"
        return httpx.Response(200, json={"id": "sb", "state": state})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        with pytest.raises(RuntimeError, match="Retry after deletion completes"):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)

    assert calls == [("GET", "/sandbox"), ("GET", "/sandbox/sb")]
    assert clock() == 0


@pytest.mark.parametrize("state,expected_warning,start_calls", [
    ("started", None, 0),
    ("stopped", "running processes were lost", 1),
    ("archived", "running processes were lost", 1),
    ("paused", "resumed from paused state", 1),
])
async def test_lifecycle_ready_and_restartable_states(
    tools, transport, clock, state, expected_warning, start_calls,
):
    observations = iter([state, "started"] if start_calls else [state])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb" and request.method == "GET":
            return httpx.Response(200, json={"id": "sb", "state": next(observations)})
        if request.url.path == "/sandbox/sb/start":
            return httpx.Response(200, json={"id": "sb", "state": "starting"})
        assert request.url.path == "/sb/process/execute"
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        sandbox_id, warning = await lathe._ensure_sandbox(
            tools.valves, USER["email"], client,
        )

    assert sandbox_id == "sb"
    assert (expected_warning is None and warning is None) or expected_warning in warning
    assert calls.count(("POST", "/sandbox/sb/start")) == start_calls
    assert calls.count(("POST", "/sb/process/execute")) == 1
    assert (clock() > 0) == bool(start_calls)


@pytest.mark.parametrize("initial,terminal", [
    ("stopping", "stopped"),
    ("archiving", "archived"),
])
async def test_lifecycle_transition_restarts_after_terminal_state(
    tools, transport, clock, initial, terminal,
):
    # Daytona's read model may briefly remain at the terminal state after the
    # start request. That must not cause duplicate start requests.
    states = iter([initial, terminal, terminal, "starting", "started"])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb" and request.method == "GET":
            return httpx.Response(200, json={"id": "sb", "state": next(states)})
        if request.url.path == "/sandbox/sb/start":
            return httpx.Response(200, json={"id": "sb", "state": "starting"})
        assert request.url.path == "/sb/process/execute"
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        sandbox_id, warning = await lathe._ensure_sandbox(
            tools.valves, USER["email"], client,
        )

    assert sandbox_id == "sb"
    assert "running processes were lost" in warning
    assert calls[:7] == [
        ("GET", "/sandbox"),
        ("GET", "/sandbox/sb"),
        ("GET", "/sandbox/sb"),
        ("POST", "/sandbox/sb/start"),
        ("GET", "/sandbox/sb"),
        ("GET", "/sandbox/sb"),
        ("GET", "/sandbox/sb"),
    ]
    assert calls.count(("POST", "/sandbox/sb/start")) == 1
    assert clock() > 0


@pytest.mark.parametrize("state", [
    "unknown", "pending_build", "pulling_snapshot", "building_snapshot", "creating",
    "starting", "resuming", "restoring", "resizing", "snapshotting", "forking",
])
async def test_lifecycle_in_flight_states_wait_without_starting(
    tools, transport, clock, state,
):
    observations = iter([state, "started"])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb":
            return httpx.Response(200, json={"id": "sb", "state": next(observations)})
        assert request.url.path == "/sb/process/execute"
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        assert await lathe._ensure_sandbox(
            tools.valves, USER["email"], client,
        ) == ("sb", None)

    assert ("POST", "/sandbox/sb/start") not in calls
    assert clock() > 0


async def test_lifecycle_pausing_waits_then_resumes(tools, transport, clock):
    observations = iter(["pausing", "paused", "resuming", "started"])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb" and request.method == "GET":
            return httpx.Response(200, json={"id": "sb", "state": next(observations)})
        if request.url.path == "/sandbox/sb/start":
            return httpx.Response(200, json={"id": "sb", "state": "resuming"})
        assert request.url.path == "/sb/process/execute"
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        assert await lathe._ensure_sandbox(
            tools.valves, USER["email"], client,
        ) == ("sb", "[Sandbox was resumed from paused state]")

    assert calls.count(("POST", "/sandbox/sb/start")) == 1
    assert clock() > 0


@pytest.mark.parametrize("state,details,message", [
    ("build_failed", {}, "snapshot build failed"),
    ("error", {"recoverable": False, "errorReason": "broken host"}, "broken host"),
    ("new_daytona_state", {}, "unsupported Daytona lifecycle state"),
])
async def test_lifecycle_terminal_failures_are_immediate(
    tools, transport, clock, state, details, message,
):
    def handler(request):
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        assert (request.method, request.url.path) == ("GET", "/sandbox/sb")
        return httpx.Response(200, json={"id": "sb", "state": state, **details})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        with pytest.raises(RuntimeError, match=message):
            await lathe._ensure_sandbox(tools.valves, USER["email"], client)
    assert clock() == 0


async def test_lifecycle_recoverable_error_recovers_once(tools, transport, clock):
    observations = iter([
        {"state": "error", "recoverable": True, "errorReason": "runner lost"},
        {"state": "restoring"},
        {"state": "started"},
    ])
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/sandbox":
            return httpx.Response(200, json=[{
                "id": "sb", "labels": {"test": USER["email"]},
            }])
        if request.url.path == "/sandbox/sb" and request.method == "GET":
            return httpx.Response(200, json={"id": "sb", **next(observations)})
        if request.url.path in ("/sandbox/sb/recover", "/sandbox/sb/start"):
            return httpx.Response(200, json={"id": "sb"})
        assert request.url.path == "/sb/process/execute"
        return httpx.Response(200, json={"exitCode": 0, "result": "ready"})

    async with httpx.AsyncClient(transport=transport(handler)) as client:
        sandbox_id, warning = await lathe._ensure_sandbox(
            tools.valves, USER["email"], client,
        )

    assert sandbox_id == "sb" and "recovered from error" in warning
    assert calls.count(("POST", "/sandbox/sb/recover")) == 1
    assert calls.count(("POST", "/sandbox/sb/start")) == 1
    assert clock() > 0


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


@pytest.mark.parametrize("tool,command,args,method,path,payload", [
    ("peek", "process.status", {"process": "atspi"}, "GET", "process/atspi/status", {}),
    ("peek", "process.logs", {"process": "novnc"}, "GET", "process/novnc/logs", {}),
    ("peek", "display.info", {}, "GET", "display/info", {}),
    ("peek", "display.windows", {}, "GET", "display/windows", {}),
    ("peek", "mouse.position", {}, "GET", "mouse/position", {}),
    ("peek", "accessibility.tree", {"scope": "pid", "pid": 123}, "GET", "a11y/tree",
     {"scope": "pid", "pid": "123", "maxDepth": "2"}),
    ("peek", "accessibility.find", {"name": "Submit", "states": ["visible"]}, "POST", "a11y/find",
     {"scope": "focused", "name": "Submit", "states": ["visible"], "nameMatch": "substring", "limit": 20}),
    ("poke", "start", {}, "POST", "start", {}),
    ("poke", "stop", {}, "POST", "stop", {}),
    ("poke", "process.restart", {"process": "xfce4"}, "POST", "process/xfce4/restart", {}),
    ("poke", "mouse.move", {"x": 5, "y": 6}, "POST", "mouse/move", {"x": 5, "y": 6}),
    ("poke", "mouse.click", {"x": 5, "y": 6}, "POST", "mouse/click",
     {"x": 5, "y": 6, "button": "left", "double": False}),
    ("poke", "mouse.drag", {"startX": 1, "startY": 2, "endX": 3, "endY": 4}, "POST", "mouse/drag",
     {"startX": 1, "startY": 2, "endX": 3, "endY": 4, "button": "left"}),
    ("poke", "mouse.scroll", {"x": 5, "y": 6, "direction": "up"}, "POST", "mouse/scroll",
     {"x": 5, "y": 6, "direction": "up", "amount": 1}),
    ("poke", "mouse.down", {}, "POST", "mouse/down", {"button": "left"}),
    ("poke", "mouse.up", {"x": 7, "y": 8}, "POST", "mouse/up", {"x": 7, "y": 8, "button": "left"}),
    ("poke", "keyboard.type", {"text": "α\nHello"}, "POST", "keyboard/type", {"text": "α\nHello", "delay": 0}),
    ("poke", "keyboard.press", {"key": "c", "modifiers": ["ctrl"]}, "POST", "keyboard/key",
     {"key": "c", "modifiers": ["ctrl"]}),
    ("poke", "keyboard.hotkey", {"keys": "ctrl+l"}, "POST", "keyboard/hotkey", {"keys": "ctrl+l"}),
    ("poke", "keyboard.down", {"key": "shift"}, "POST", "keyboard/down", {"key": "shift"}),
    ("poke", "keyboard.up", {"key": "shift"}, "POST", "keyboard/up", {"key": "shift"}),
    ("poke", "accessibility.focus", {"id": "node"}, "POST", "a11y/node/focus", {"id": "node"}),
    ("poke", "accessibility.invoke", {"id": "node"}, "POST", "a11y/node/invoke", {"id": "node"}),
    ("poke", "accessibility.set_value", {"id": "node", "value": "abc"}, "POST", "a11y/node/value",
     {"id": "node", "value": "abc"}),
])
async def test_computer_dispatch(tools, sandbox, http, tool, command, args, method, path, payload):
    calls = []
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        if (request.method, request.url.path) != (method, "/sb/computeruse/" + path):
            # The only other traffic is the running-display check.
            assert (request.method, request.url.path) == ("GET", "/sb/computeruse/process/xvfb/status")
            return httpx.Response(200, json={"processName": "xvfb", "running": True})
        calls.append(request)
        assert (dict(request.url.params) if method == "GET" else json.loads(request.content)) == payload
        return httpx.Response(200, json={"running": True, "canary": "observed-state"})
    http(handler)
    result = await getattr(tools, tool)(command, args, __user__=USER)
    assert "Error:" not in result
    assert len(calls) == 1
    if tool == "peek":
        assert "observed-state" in result


def fake_desktop(running, *, page_result=None, polls_down=0):
    """Toolbox responder for a desktop whose display starts on POST start."""
    state = {"running": running, "polls_down": polls_down, "log": [], "scripts": []}

    def handler(request):
        path = request.url.path.removeprefix("/sb")
        state["log"].append(f"{request.method} {path}")
        if path == "/process/execute":
            state["scripts"].append(json.loads(request.content)["command"])
            return httpx.Response(200, json={"exitCode": 0, "result": json.dumps(page_result) + "\n"})
        name = re.fullmatch(r"/computeruse/process/(\w+)/status", path)
        if name:
            up = state["running"] or name[1] == "atspi"
            if state["running"] and state["polls_down"]:
                state["polls_down"] -= 1
                up = False
            return httpx.Response(200, json={"processName": name[1], "running": up})
        if path == "/computeruse/status":
            return httpx.Response(200, json={"status": "active" if state["running"] else "partial"})
        if path == "/computeruse/start":
            state["running"] = True
            return httpx.Response(200, json={"message": "started"})
        if path.startswith("/computeruse/screenshot"):
            return httpx.Response(200, content=PNG)
        # Input on a down display crashes Daytona's computer-use service.
        assert state["running"], f"{path} sent to a down desktop"
        return httpx.Response(200, json={"x": 1, "y": 2})

    state["handler"] = handler
    return state


@pytest.mark.parametrize("command,args", [
    ("mouse.click", {"x": 1, "y": 2}),
    ("keyboard.type", {"text": "hello"}),
    ("page.open", {"url": "https://example.org/"}),
])
async def test_poke_starts_down_desktop_before_acting(tools, sandbox, http, clock, command, args):
    desktop = fake_desktop(False, polls_down=1, page_result={"ok": True, "text": "Opened."})
    http(desktop["handler"])
    result = await tools.poke(command, args, __user__=USER)
    assert "Error:" not in result and "started" in result
    log = desktop["log"]
    acted = log.index("POST /process/execute" if command.startswith("page.") else
                      "POST /computeruse/" + command.replace(".", "/"))
    assert log.index("POST /computeruse/start") < acted
    # The action waited for both the display and the session after start.
    assert "GET /computeruse/process/xfce4/status" in log[log.index("POST /computeruse/start"):acted]
    if command.startswith("page."):
        assert '"launch": true' in desktop["scripts"][0]


@pytest.mark.parametrize("command,args", [
    ("mouse.click", {"x": 1, "y": 2}),
    ("page.eval", {"js": "1 + 1"}),
])
async def test_poke_reuses_running_desktop_without_start(tools, sandbox, http, command, args):
    desktop = fake_desktop(True, page_result={"ok": True, "text": "Result: 2"})
    http(desktop["handler"])
    result = await tools.poke(command, args, __user__=USER)
    assert "Error:" not in result and "started" not in result
    assert "POST /computeruse/start" not in desktop["log"]


@pytest.mark.parametrize("command,args", [
    ("status", {}), ("screenshot", {}), ("mouse.position", {}), ("display.info", {}),
    ("accessibility.tree", {}), ("page.list", {}), ("page.text", {"selector": "main"}),
    ("page.wait", {"gone": "Just a moment", "timeout": 30}),
])
async def test_peek_never_starts_or_touches_a_down_display(tools, sandbox, http, command, args):
    desktop = fake_desktop(False, page_result={"ok": True, "text": "No shared browser is running."})
    http(desktop["handler"])
    result = await tools.peek(command, args, __user__=USER)
    assert not any(entry.startswith("POST /computeruse") for entry in desktop["log"])
    assert all("/process/" in e or e.endswith("/computeruse/status") for e in desktop["log"])
    if command.startswith("page."):
        assert '"launch": false' in desktop["scripts"][0]
    elif command == "status":
        # Truthful summary, not Daytona's misleading aggregate "partial".
        assert "Desktop is down" in result and "poke starts it" in result
        assert "Running: atspi." in result
    else:
        assert result.startswith("Error:") and "desktop is down" in result


@pytest.mark.parametrize("tool,command", [
    (tool, command) for tool, registry in (("peek", lathe._PEEK_COMMANDS), ("poke", lathe._POKE_COMMANDS))
    for command in registry])
async def test_argument_errors_teach_the_command_signature(tools, monkeypatch, tool, command):
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(side_effect=AssertionError("reached I/O")))
    result = await getattr(tools, tool)(command, {"seconds": 5}, __user__=USER)
    signature = f"{command} args: {lathe._arg_signature(command)}"
    assert result.startswith("Error:") and signature in result and "\n" not in signature
    for name in lathe._COMPUTER_MODELS[command].model_fields:
        assert name in signature


@pytest.mark.parametrize("command,args,expected", [
    ("page.wait", {"seconds": 5, "timeout": 45, "tab": 7},
     ["seconds", "timeout", "tab", "timeout?: int 1-30 = 10", "exactly one of text/selector/url/gone"]),
    ("page.wait", {"timeout": 5}, ["page.wait requires exactly one", "page.wait args: {text?: str"]),
    ("page.eval", {"expr": "1"}, ["expr", "js: Field required", "page.eval args: {js: str"]),
    ("page.save", {"url": "https://example.org/p.pdf"}, ["path: Field required", "path: absolute path"]),
    ("page.save", {"path": "/tmp/p.pdf", "url": "https://example.org/p.pdf", "format": "pdf"},
     ["at most one of url, format"]),
])
async def test_argument_errors_report_every_field(tools, monkeypatch, command, args, expected):
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(side_effect=AssertionError("reached I/O")))
    tool = "peek" if command in lathe._PEEK_COMMANDS else "poke"
    result = await getattr(tools, tool)(command, args, __user__=USER)
    assert result.startswith("Error:")
    for text in expected:
        assert text in result


def test_decision_time_descriptions_carry_browser_signatures():
    for method, registry in (("peek", lathe._PEEK_COMMANDS), ("poke", lathe._POKE_COMMANDS)):
        doc = inspect.getdoc(getattr(lathe.Tools, method))
        for command in registry:
            if command.startswith("page."):
                assert f"{command} {lathe._arg_signature(command)}" in doc
        assert len(doc) < 2000


class FakeCDPSession:
    """Answers the page script's CDP calls; unexpected calls fail the test."""
    def __init__(self, content_type, served=b"%PDF-1.7 served bytes"):
        self.content_type, self.served, self.calls = content_type, served, []

    def value(self, expression, timeout=30):
        self.calls.append(expression[:40])
        if expression == "document.contentType":
            return self.content_type
        if expression == "location.href":
            return "https://example.org/paper.pdf"
        if expression == "document.documentElement.outerHTML":
            return "<html>page</html>"
        if expression.startswith("(async (u, limit)"):
            return {"status": 200, "type": "application/pdf", "data": base64.b64encode(self.served).decode()}
        raise AssertionError(f"unexpected evaluation {expression[:60]}")

    def call(self, method, params=None, timeout=30):
        self.calls.append(method)
        if method == "Page.printToPDF":
            return {"data": base64.b64encode(b"%PDF-printed").decode()}
        if method == "Page.captureSnapshot":
            return {"data": "MIME-Version: 1.0"}
        raise AssertionError(f"unexpected CDP call {method}")


@pytest.fixture
def page_script():
    namespace = {}
    exec(compile(lathe._PAGE_SCRIPT, "page_script", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("content_type,name,fmt,written,error", [
    ("text/html", "page.pdf", None, b"%PDF-printed", None),
    ("text/html", "page.HTM", None, b"<html>page</html>", None),
    ("text/html", "page.mht", None, b"MIME-Version: 1.0", None),
    ("text/html", "page.bin", "html", b"<html>page</html>", None),
    ("text/html", "page", None, None, "cannot tell the format"),
    ("application/pdf", "paper.pdf", None, b"%PDF-1.7 served bytes", None),
    ("application/pdf", "paper", "pdf", b"%PDF-1.7 served bytes", None),
    ("application/pdf", "paper.html", "html", None, "has no html form"),
])
def test_page_save_mode_selection(page_script, tmp_path, content_type, name, fmt, written, error):
    session = FakeCDPSession(content_type)
    args = {"path": str(tmp_path / name), **({"format": fmt} if fmt else {})}
    if error:
        with pytest.raises(page_script["Fail"], match=error):
            page_script["page_save"](session, {}, args)
        assert not (tmp_path / name).exists()
        return
    result = page_script["page_save"](session, {}, args)
    assert (tmp_path / name).read_bytes() == written and str(len(written)) in result
    # Chromium's PDF viewer must never be printed.
    assert ("Page.printToPDF" in session.calls) is (content_type == "text/html" and written == b"%PDF-printed")


class NodeDOMSession:
    """Evaluates the page script's real JavaScript in Node against a stub DOM.
    Each evaluation of a condition advances to the next page state."""
    def __init__(self, *pages):
        self.pages, self.evaluations = list(pages), 0

    def value(self, expression, timeout=30):
        title, body, frames = self.pages[min(self.evaluations, len(self.pages) - 1)]
        self.evaluations += 1
        stub = ("globalThis.document = {title: %s, body: {innerText: %s}, "
                "querySelectorAll: () => %s.map(src => ({src}))};"
                "globalThis.location = {href: 'https://example.org/'};"
                % (json.dumps(title), json.dumps(body), json.dumps(list(frames))))
        out = subprocess.run(["node", "-e", stub + "process.stdout.write(JSON.stringify(eval("
                              + json.dumps(expression) + ")))"],
                             capture_output=True, text=True, check=True, timeout=30).stdout
        return json.loads(out)

    def where(self, target):
        return "Tab"


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="Node evaluates the page JavaScript")
CLOUDFLARE = ("Just a moment...", "example.org\nPerforming security verification\nRay ID", [])


@needs_node
@pytest.mark.parametrize("pages,kind,want,expected", [
    # Cloudflare names its interstitial only in the title: it is present, not gone.
    ([CLOUDFLARE], "gone", "just a moment", "Timed out"),
    ([CLOUDFLARE], "text", "Just a Moment", "Condition met (text)"),
    ([("Paper", "Abstract", [])], "gone", "just a moment", "Already absent at the first check"),
    ([CLOUDFLARE, CLOUDFLARE, ("Paper", "Abstract", [])], "gone", "just a moment", "Condition met (gone)"),
])
def test_page_wait_matches_title_and_reports_absent_text(page_script, monkeypatch, pages, kind, want, expected):
    now = [0.0]
    monkeypatch.setitem(page_script, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)))
    result = page_script["page_wait"](NodeDOMSession(*pages), {}, {kind: want, "timeout": 1})
    assert expected in result
    if "Already absent" in expected:
        assert "Condition met" not in result


@needs_node
@pytest.mark.parametrize("page,challenge", [
    (CLOUDFLARE, True),
    (("Just a moment...", "", []), True),
    (("Sign in", "Please wait", ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x"]), True),
    (("Paper", "Abstract", ["https://www.youtube.com/embed/x"]), False),
])
def test_challenge_notice_detects_cloudflare_and_carries_handoff(page_script, page, challenge):
    note = page_script["challenge_note"](NodeDOMSession(page))
    assert bool(note) is challenge
    if challenge:
        assert 'expose(target="vnc", access="private")' in note and "page.wait" in note


@pytest.mark.parametrize("tool,command,right", [
    ("peek", "page.open", "poke"), ("peek", "page.click", "poke"), ("peek", "mouse.click", "poke"),
    ("poke", "page.text", "peek"), ("poke", "page.list", "peek"), ("poke", "screenshot", "peek"),
])
async def test_wrong_tool_errors_name_the_right_call(tools, monkeypatch, tool, command, right):
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(side_effect=AssertionError("reached I/O")))
    result = await getattr(tools, tool)(command, {}, __user__=USER)
    assert result.startswith("Error:") and f'{right}(command="{command}"' in result


async def test_page_poke_survives_dead_computer_use_service(tools, sandbox, http):
    # Chromium does not depend on Daytona's crashed plugin; desktop input does.
    log = []
    def handler(request):
        log.append(request.url.path)
        if request.url.path == "/sb/process/execute":
            return httpx.Response(200, json={"exitCode": 0, "result": json.dumps({"ok": True, "text": "Opened."})})
        return httpx.Response(500, json={"message": "internal server error: connection is shut down"})
    http(handler)
    result = await tools.poke("page.open", {"url": "https://example.org/"}, __user__=USER)
    assert "Opened." in result and "connection is shut down" in result
    assert "/sb/computeruse/start" not in log
    result = await tools.poke("mouse.click", {"x": 1, "y": 2}, __user__=USER)
    assert result.startswith("Error:") and "/sb/computeruse/mouse/click" not in log


async def test_live_view_offer_follows_first_page_poke_once_per_chat(tools, monkeypatch, http):
    # Real per-chat initialization owns the state the offer is recorded in.
    monkeypatch.setattr(lathe, "_ensure_sandbox", AsyncMock(return_value=("sb", None)))
    http(fake_desktop(True, page_result={"ok": True, "text": "Opened."})["handler"])
    offer = 'expose(target="vnc", access="private")'
    desktop = await tools.poke("mouse.click", {"x": 1, "y": 2}, __user__=USER, __chat_id__="a")
    first = await tools.poke("page.open", {"url": "https://example.org/"}, __user__=USER, __chat_id__="a")
    again = await tools.poke("page.eval", {"js": "1"}, __user__=USER, __chat_id__="a")
    other = await tools.poke("page.open", {"url": "https://example.org/"}, __user__=USER, __chat_id__="b")
    assert offer not in desktop and offer not in again
    assert offer in first and offer in other
    # Context follows the page's own result so the result still opens with it.
    assert first.index("Opened.") < first.index(offer)


async def test_stop_warns_about_input_after_restart(tools, sandbox, http):
    http(fake_desktop(True)["handler"])
    result = await tools.poke("stop", __user__=USER)
    assert "crashes" in result and "sandbox restarts" in result


async def test_status_reports_running_processes(tools, sandbox, http):
    http(fake_desktop(True)["handler"])
    result = await tools.peek("status", __user__=USER)
    assert result.startswith("Desktop is active.") and "xvfb, xfce4, x11vnc, novnc" in result


@pytest.mark.parametrize("command,args,message,shown", [
    ("keyboard.press", {"key": "KP_Enter"}, 'unsupported key "KP_Enter"; see the docs', 'unsupported key "KP_Enter"'),
    ("keyboard.type", {"text": "secret-input-value"}, "cannot type secret-input-value now", "cannot type [typed text] now"),
    ("keyboard.type", {"text": "secret-input-value"}, "cannot type 'secret-inp...'", "cannot type '[typed text]...'"),
    ("accessibility.set_value", {"id": "n", "value": "hunter"}, "bad value hunter", None),
    ("mouse.click", {"x": 1, "y": 2}, "x" * 2000, "x" * 300 + "..."),
])
async def test_computer_errors_pass_provider_message_bounded_and_redacted(tools, sandbox, http, command, args, message, shown):
    calls = []
    def handler(request):
        if request.url.path.endswith("/process/xvfb/status"):
            return httpx.Response(200, json={"running": True})
        calls.append(request)
        return httpx.Response(500, json={"statusCode": 500, "message": message, "source": "DAYTONA_DAEMON"})
    http(handler)
    result = await tools.poke(command, args, __user__=USER)
    assert result.startswith("Error: computer command failed (HTTP 500)") and "bash with DISPLAY=:0" in result
    for field in ("text", "value"):
        if field in args:
            assert args[field][:8] not in result
    assert (shown in result) if shown else ("hunter" not in result)
    assert "x" * 301 not in result
    assert len(calls) == 1


async def test_browser_script_errors_are_redacted(tools, sandbox, http):
    desktop = fake_desktop(True, page_result={
        "ok": False, "text": "JavaScript error: ReferenceError: secretFunctionName is not defined"})
    http(desktop["handler"])
    result = await tools.poke("page.eval", {"js": "secretFunctionName()"}, __user__=USER)
    assert result.startswith("Error: page.eval failed: JavaScript error: ReferenceError: [typed text]")
    assert "secretFunction" not in result


@pytest.mark.parametrize("tool,command,args", [
    ("peek", "start", {}), ("peek", "mouse.click", {"x": 1, "y": 2}),
    ("poke", "screenshot", {}), ("poke", "display.info", {}),
    ("peek", "../process/execute", {}), ("poke", "unknown", {}),
    ("poke", "start", {"command": "secret-input"}),
    ("poke", "keyboard.type", {"text": 7}),
    ("poke", "keyboard.press", {"key": "c", "modifiers": [7]}),
    ("poke", "mouse.click", {"x": True, "y": 2}),
    ("poke", "mouse.click", {"x": "1", "y": 2}),
    ("poke", "mouse.click", {"x": -1, "y": 2}),
    ("poke", "mouse.click", {"x": 1, "y": 2, "double": "false"}),
    ("poke", "mouse.down", {"x": 1}),
    ("poke", "keyboard.type", {"text": "x" * 16385}),
    ("peek", "screenshot", {"region": {"x": 0, "y": 0, "width": False, "height": 20}}),
    ("peek", "screenshot.compressed", {"scale": 0.5}),
    ("peek", "process.status", {"process": "at-spi"}),
    ("poke", "page.list", {}), ("peek", "page.open", {"url": "https://example.org/"}),
    ("poke", "page.open", {"url": "javascript:alert(1)"}),
    ("poke", "page.open", {"url": "https://example.org/", "tab": "abcd", "newTab": True}),
    ("poke", "page.open", {"url": "https://example.org/", "tab": "../json/close"}),
    ("poke", "page.click", {}),
    ("poke", "page.click", {"text": "Save", "selector": "button"}),
    ("poke", "page.type", {"text": "secret-input"}),
    ("poke", "page.type", {"selector": "#q", "text": "secret-input", "submit": "true"}),
    ("poke", "page.save", {"path": "paper.pdf", "format": "pdf"}),
    ("poke", "page.save", {"path": "/tmp/paper.pdf", "format": "docx"}),
    ("poke", "page.save", {"path": "/tmp/paper.pdf", "format": "pdf", "url": "https://example.org/p.pdf"}),
    ("poke", "page.save", {"path": "/tmp/paper.pdf", "url": "file:///etc/passwd"}),
    ("poke", "page.save", {"path": "/tmp/paper.png", "format": "png"}),
    ("poke", "page.eval", {"js": 7}),
    ("poke", "page.wait", {"text": "ready"}),
    ("peek", "page.wait", {}),
    ("peek", "page.wait", {"text": "ready", "gone": "Just a moment"}),
    ("peek", "page.wait", {"selector": "#q", "url": "/done"}),
    ("peek", "page.wait", {"text": "ready", "timeout": 31}),
    ("peek", "page.wait", {"text": "ready", "timeout": 0}),
    ("peek", "page.wait", {"text": "ready", "timeout": 2.5}),
    ("peek", "page.wait", {"text": ""}),
    ("peek", "page.text", {"maxChars": True}),
    ("peek", "page.text", {"maxChars": 10_000_000}),
    ("peek", "accessibility.tree", {"scope": "pid"}),
    ("peek", "accessibility.tree", {"pid": 10}),
    ("peek", "accessibility.find", {"states": [1]}),
    ("poke", "process.restart", {"process": "../../execute"}),
    ("peek", 12, {}), ("poke", "start", []),
])
async def test_computer_rejects_misuse_before_io(tools, monkeypatch, tool, command, args):
    ensure = AsyncMock(side_effect=AssertionError("invalid command reached provisioning"))
    monkeypatch.setattr(lathe, "_ensure_sandbox", ensure)
    result = await getattr(tools, tool)(command, args, __user__=USER)
    assert result.startswith("Error:") and "secret-input" not in result
    ensure.assert_not_called()


@pytest.mark.parametrize("encoding", ["raw", "json", "pascal"])
@pytest.mark.parametrize("compressed,region", [(False, False), (False, True), (True, False), (True, True)])
async def test_computer_screenshot_byte_exact(tools, sandbox, http, encoding, compressed, region):
    args = {"showCursor": True}
    if compressed:
        args.update(format="png", quality=90)
    if region:
        args["region"] = {"x": 10, "y": 20, "width": 30, "height": 40}
    def handler(request):
        if request.url.path.endswith("/process/xvfb/status"):
            return httpx.Response(200, json={"running": True})
        suffix = "screenshot" + ("/region" if region else "") + ("/compressed" if compressed else "")
        assert request.method == "GET" and request.url.path == "/sb/computeruse/" + suffix
        expected = {"showCursor": "true"}
        if compressed:
            expected.update(format="png", quality="90")
        if region:
            expected.update(x="10", y="20", width="30", height="40")
        assert dict(request.url.params) == expected
        if encoding == "raw":
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, json={"Screenshot" if encoding == "pascal" else "screenshot": base64.b64encode(PNG).decode()})
    http(handler)
    tools._chat_state["screen"] = {"init": True, "pending": ["deferred notice"]}
    result = await tools.peek("screenshot.compressed" if compressed else "screenshot", args,
                              __user__=USER, __chat_id__="screen")
    assert result == "data:image/png;base64," + base64.b64encode(PNG).decode()
    http(fake_desktop(True)["handler"])
    text = await tools.peek("status", __user__=USER, __chat_id__="screen")
    assert "deferred notice" in text


async def test_computer_vision_gate_is_image_only(tools, sandbox, http, monkeypatch):
    context = AsyncMock(side_effect=AssertionError("nonvision image reached I/O"))
    with monkeypatch.context() as m:
        m.setattr(lathe, "_tool_context", context)
        result = await tools.peek("screenshot", __metadata__={"model": {"architecture": {"input_modalities": ["text"]}}},
                                  __model__={"architecture": {"input_modalities": ["image"]}})
        assert "vision-capable" in result
        context.assert_not_called()
    http(fake_desktop(True)["handler"])
    result = await tools.peek("status", __model__={"info": {"meta": {"capabilities": {"vision": False}}}}, __user__=USER)
    assert "active" in result


@pytest.mark.parametrize("failure", ["http", "timeout", "redirect", "bad-image", "oversize", "bad-json"])
async def test_computer_failures_are_bounded_redacted_and_not_retried(tools, sandbox, http, failure):
    calls = []
    def handler(request):
        if request.url.path.endswith("/process/xvfb/status"):
            return httpx.Response(200, json={"running": True})
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-input")
        if failure == "http":
            return httpx.Response(500, text="secret-input")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://leak.test/secret-input"})
        if failure == "oversize":
            return httpx.Response(200, content=b"x" * (6 * 1024 * 1024))
        if failure == "bad-image":
            return httpx.Response(200, json={"screenshot": base64.b64encode(b"secret-input").decode()})
        return httpx.Response(200, text="secret-input")
    http(handler)
    screenshot = failure in {"bad-image", "oversize", "bad-json"}
    result = await (tools.peek("screenshot", __user__=USER) if screenshot else
                    tools.poke("keyboard.type", {"text": "secret-input"}, __user__=USER))
    assert result.startswith("Error:") and "secret-input" not in result
    assert len(calls) == 1


@pytest.mark.parametrize("access,wrapped", [("private", True), ("public", True), ("public", False)])
async def test_vnc_preview_uses_existing_policy_and_landing_path(tools, sandbox, http, access, wrapped):
    tools.valves.preview_wrapper_url = "https://wrapper.test/register" if wrapped else ""
    tools.valves.preview_wrapper_key = "wrapper-secret" if wrapped else ""
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/sb/computeruse/status":
            assert request.method == "GET"
            return httpx.Response(200, json={"Status": "active"})
        if request.url.host == "api.test":
            assert request.url.path == "/sandbox/sb/ports/6080/signed-preview-url"
            return httpx.Response(200, json={"url": "https://signed.test/?token=secret-token"})
        assert (request.method, request.url.host, request.url.path) == ("POST", "wrapper.test", "/register")
        registration = json.loads(request.content)
        assert registration["access"] == access and registration["owner"]["email"] == USER["email"]
        return httpx.Response(200, json={"url": "https://protected.test/", "expires_at": "2099-01-01T00:00:00Z"})
    http(handler)
    result = await tools.expose("vnc", access, __user__=USER)
    assert "/vnc.html?" in result and "path=websockify" in result
    assert "logged-in accounts" in result
    assert ("secret-token" in result) is not wrapped
    assert len(calls) == (3 if wrapped else 2)


async def test_vnc_does_not_start_desktop_or_downgrade_private(tools, sandbox, http):
    calls = []
    def handler(request):
        calls.append(request)
        assert (request.method, request.url.path) == ("GET", "/sb/computeruse/status")
        return httpx.Response(200, json={"status": "inactive"})
    http(handler)
    result = await tools.expose("vnc", "private", __user__=USER)
    assert result.startswith("Error:") and "not active" in result
    assert len(calls) == 1
    calls.clear()
    http(lambda r: httpx.Response(200, json={"status": "active"}))
    result = await tools.expose("vnc", "private", __user__=USER)
    assert result.startswith("Error:") and "no direct URL" in result


@pytest.fixture
def preview(tools, sandbox, http):
    upstream, protected, secret = "https://secret.preview.test/", "https://owner.preview.test/", "installation-secret"
    tools.valves.preview_wrapper_url = "https://wrapper.test/register"
    tools.valves.preview_wrapper_key = secret
    good = {"url": protected, "expires_at": "2099-01-01T00:00:00Z"}

    async def invoke(payload=None, status=200, target="http:5000", user=USER,
                     access="private", tag="", setup_failure=False, upstream_headers=None):
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
                port = int(request.url.path.split("/")[-2])
                assert 1024 <= port <= 65535
                assert request.url.params["expiresInSeconds"] == "86400"
                return httpx.Response(200, json={"url": upstream})
            assert request.url.host == "proxy.test"
            assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
            if setup_failure:
                return httpx.Response(200, json={"exitCode": 1, "result": "checksum mismatch"})
            return httpx.Response(200, json={"exitCode": 0, "result": "READY PID=123"})
        http(handler)
        result = await tools.expose(target, access, tag, upstream_headers=upstream_headers or {},
                                    __user__=user, __event_emitter__=emit)
        return result, calls, json.dumps(events)
    return SimpleNamespace(invoke=invoke, upstream=upstream, protected=protected, secret=secret, good=good)


@pytest.mark.parametrize("target", ["http:1024", "http:5000", "http:65535"])
async def test_preview_uses_trusted_identity_and_hides_credentials(preview, target):
    result, calls, events = await preview.invoke(target=target)
    assert preview.protected in result and "Owner-authenticated" in result
    assert all(s not in result + events for s in [preview.upstream, preview.secret])
    registrations = [r for r in calls if r.url.host == "wrapper.test"]
    assert len(registrations) == 1
    assert json.loads(registrations[0].content) == {
        "owner": {"subject": USER["id"], "email": USER["email"]},
        "upstream_url": preview.upstream, "access": "private"}
    assert registrations[0].headers["Authorization"] == "Bearer " + preview.secret


@pytest.mark.parametrize("target", ["dufs", "ttyd", "code-server", "site:/absolute", "http:1023", "http:65536", "http:no"])
async def test_removed_or_invalid_expose_targets_fail_before_io(tools, monkeypatch, target):
    context = AsyncMock(side_effect=AssertionError("invalid target reached I/O"))
    monkeypatch.setattr(lathe, "_tool_context", context)
    assert (await tools.expose(target, "private", __user__=USER)).startswith("Error:")
    context.assert_not_called()


async def test_preview_tag_is_optional_untrusted_wrapper_hint(preview):
    result, calls, _ = await preview.invoke(tag="vscode")
    assert preview.protected in result
    request = next(r for r in calls if r.url.host == "wrapper.test")
    assert json.loads(request.content)["tag"] == "vscode"

    result, calls, _ = await preview.invoke(tag="official.example")
    assert result.startswith("Error: tag") and not calls


async def test_public_preview_accepts_public_wrapper_result(preview):
    result, calls, events = await preview.invoke(access="public")
    assert preview.protected in result and "Public wrapped preview" in result
    assert all(s not in result + events for s in [preview.upstream, preview.secret])
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1
    request = next(r for r in calls if r.url.host == "wrapper.test")
    assert json.loads(request.content)["access"] == "public"


@pytest.mark.parametrize("access", ["public", "private"])
@pytest.mark.parametrize("target", ["http:5000", "http:8000"])
async def test_preview_headers_are_registration_scoped_and_not_echoed(preview, access, target):
    headers = {"Authorization": "Basic YXBwOnNlY3JldA==", "X-App-Mode": "demo"}
    result, calls, events = await preview.invoke(
        {**preview.good, "upstream_headers_applied": True}, access=access,
        target=target, upstream_headers=headers)
    assert preview.protected in result
    assert all(value not in result + events for value in headers.values())
    registration = json.loads(next(r.content for r in calls if r.url.host == "wrapper.test"))
    assert registration["upstream_headers"] == headers
    assert registration["access"] == access
    assert registration["owner"] == {"subject": USER["id"], "email": USER["email"]}
    # A later headerless call must not inherit the previous exposure's values.
    _, calls, _ = await preview.invoke(access=access)
    assert "upstream_headers" not in json.loads(next(r.content for r in calls if r.url.host == "wrapper.test"))


@pytest.mark.parametrize("headers", [
    {"X" * 64: "x" * 4096, "X-Empty": ""},
    {f"X-{i}": "value" for i in range(16)},
    {"X-A": "x" * 4096, "X-B": "x" * 4090},
])
async def test_upstream_header_bounds_are_inclusive(preview, headers):
    result, calls, _ = await preview.invoke(
        {**preview.good, "upstream_headers_applied": True}, access="public", upstream_headers=headers)
    assert preview.protected in result
    request = next(r for r in calls if r.url.host == "wrapper.test")
    assert json.loads(request.content)["upstream_headers"] == headers


@pytest.mark.parametrize("access", ["public", "private"])
@pytest.mark.parametrize("failure", ["missing", "false", "string", "integer", "timeout", "malformed", "refused"])
async def test_header_requests_cannot_silently_downgrade(preview, access, failure):
    payload = dict(preview.good)
    if failure in ("false", "string", "integer"):
        payload["upstream_headers_applied"] = {"false": False, "string": "true", "integer": 1}[failure]
    elif failure in ("timeout", "malformed"):
        payload = failure
    elif failure == "refused":
        payload = {"error": "Bearer secret-app-credential"}
    result, calls, events = await preview.invoke(
        payload, 409 if failure == "refused" else 200, access=access,
        upstream_headers={"Authorization": "Bearer secret-app-credential"})
    assert result.startswith("Error:")
    assert all(value not in result + events for value in (
        preview.upstream, preview.protected, preview.secret, "secret-app-credential"))
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1


@pytest.mark.parametrize("access", ["public", "private"])
async def test_header_requests_require_wrapper_before_signing(preview, tools, access):
    tools.valves.preview_wrapper_key = ""
    result, calls, events = await preview.invoke(
        access=access, upstream_headers={"Authorization": "secret-app-credential"})
    assert result.startswith("Error:") and not calls
    assert "secret-app-credential" not in result + events


@pytest.mark.parametrize("headers", [
    {"Host": "credential-canary"}, {"cOoKiE": "credential-canary"},
    {"Origin": "credential-canary"}, {"X-Forwarded-Owner": "credential-canary"},
    {"Connection": "credential-canary"}, {"Content-Length": "12"},
    {"Sec-WebSocket-Protocol": "credential-canary"}, {"X-Daytona-Token": "credential-canary"},
    {"Bad Name": "credential-canary"}, {"": "credential-canary"}, {"X" * 65: "value"},
    {"X-App": "credential-canary\r\nHost: attacker"}, {"X-App": "\x00"},
    {"X-App": "\t"}, {"X-App": "é"}, {"X-App": "\x7f"},
    {"X-App": "x" * 4097}, {f"X-{i}": "value" for i in range(17)},
    {"X-A": "x" * 4096, "X-B": "x" * 4096},
    {"Authorization": "credential-canary", "authorization": "other"},
])
async def test_invalid_upstream_headers_rejected_before_io(module, monkeypatch, headers):
    context = AsyncMock(side_effect=AssertionError("invalid header input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await module.Tools().expose("http:5000", "public", upstream_headers=headers)
    assert result.startswith("Error: upstream_headers")
    assert "credential-canary" not in result
    context.assert_not_called()


@pytest.mark.parametrize("failure,status", [
    ("timeout", 200), ("malformed", 200),
    ({"expires_at": "2099-01-01T00:00:00Z"}, 200), ({"error": "refused"}, 409),
])
async def test_public_preview_falls_back_to_direct_url(preview, failure, status):
    payload = failure
    result, calls, _ = await preview.invoke(payload, status, access="public")
    assert preview.upstream in result and "Public direct preview" in result
    assert preview.secret not in result
    assert len([r for r in calls if r.url.host == "wrapper.test"]) == 1


@pytest.mark.parametrize("failure", ["upstream", "missing-url", "http", "leak", "expired", "naive", "500", "302", "malformed", "timeout"])
async def test_preview_failures_are_closed_and_redacted(preview, failure):
    changes = {
        "upstream": {"url": preview.upstream}, "missing-url": {"url": None},
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


@pytest.mark.parametrize("failure", [503, "transport", "json", "shape"])
async def test_interpret_ambiguous_response_never_replays(tools, sandbox, http, failure):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == "/sb/process/execute"
        if failure == "transport":
            raise httpx.ReadError("lost response", request=request)
        if failure == 503:
            return httpx.Response(503)
        return httpx.Response(200, json={"exitCode": 0, "result": "not json" if failure == "json" else "{}"})
    http(handler)
    result = await tools.interpret("x = 1", __user__=USER, __chat_id__="chat")
    assert "outcome may be unknown" in result and "Do not automatically replay" in result
    assert len(calls) == 1


@pytest.mark.parametrize("kwargs,chat,detail", [
    ({"code": " "}, "chat", "empty code"),
    ({"code": "x = 1", "timeout": -1}, "chat", "zero or positive"),
    ({"code": "x = 1"}, "", "conversation ID"),
    ({"code": "#" + "x" * 100000}, "chat", "command budget"),
])
async def test_interpret_rejects_invalid_submissions_without_rpc(tools, sandbox, http, kwargs, chat, detail):
    http(lambda request: pytest.fail("invalid submission must not issue interpreter RPC"))
    result = await tools.interpret(**kwargs, __user__=USER, __chat_id__=chat)
    assert detail in result


if __name__ == "__main__":
    raise SystemExit(pytest.main(sys.argv[1:]))

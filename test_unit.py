"""Offline behavioral contracts. Run with `uv run pytest` (no credentials).

Exercise shipped code; replace only network/model boundaries. Each test owns
its files and state. Live Daytona and OWUI checks remain explicit scripts.
"""

import __future__
import asyncio
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import textwrap
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

    class TrackedAsyncClient(constructor):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1
            await super().aclose()

        async def __aexit__(self, exc_type, exc_value, traceback):
            self.close_calls += 1
            await super().__aexit__(exc_type, exc_value, traceback)

    def install(handler):
        def create(*args, **kwargs):
            # Delegate's in-process model transport must remain real.
            kwargs.setdefault("transport", transport(handler))
            client = TrackedAsyncClient(*args, **kwargs)
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
    ("delegate", {"task": 7}, "task", "str", "int"),
    ("delegate", {"task": "x", "context_files": "/x"}, "context_files", "list[str]", "str"),
    ("delegate", {"task": "x", "context_files": [7]}, "context_files[0]", "str", "int"),
    ("delegate", {"task": "x", "max_steps": True}, "max_steps", "int", "bool"),
    ("delegate", {"task": "x", "foreground_seconds": True}, "foreground_seconds", "int", "bool"),
    ("expose", {"target": 7, "access": "private"}, "target", "str", "int"),
    ("expose", {"target": "dufs", "access": 7}, "access", "str", "int"),
    ("expose", {"target": "dufs", "access": "private", "tag": 7}, "tag", "str", "int"),
])
async def test_bad_types_rejected_before_io(
    module, monkeypatch, name, kwargs, location, expected, actual,
):
    context = AsyncMock(side_effect=AssertionError("invalid input reached I/O"))
    monkeypatch.setattr(module, "_tool_context", context)
    result = await getattr(module.Tools(), name)(**kwargs)
    assert result == f"Error: parameter '{location}' expected type {expected}, got {actual}"
    assert "7" not in result
    context.assert_not_called()


@pytest.mark.parametrize("name,kwargs", [
    ("onboard", {"path": "/workspace"}),
    ("bash", {"command": "true", "workdir": "/workspace", "foreground_seconds": 0}),
    ("read", {"path": "/x", "start": 1, "stop": 0}),
    ("edit", {"path": "/x", "old_string": "a", "new_string": "b", "replace_all": False}),
    ("view", {"path": "/image"}),
    ("delegate", {"task": "x", "context_files": ["/x"], "max_steps": 1,
                  "foreground_seconds": 0}),
    ("expose", {"target": "dufs", "access": "private", "tag": "files"}),
])
async def test_valid_types_pass_wrapper_boundary(module, monkeypatch, name, kwargs):
    context = AsyncMock(return_value="accepted")
    monkeypatch.setattr(module, "_tool_context", context)
    assert await getattr(module.Tools(), name)(**kwargs) == "accepted"
    context.assert_awaited_once()


async def test_valid_lathe_string_passes_wrapper_boundary(module):
    assert "Lathe toolkit version:" in await module.Tools().lathe("version")


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


@pytest.mark.parametrize("kind", ["GLOB", "GREP"])
def test_wildcard_free_directories_are_recursive_and_files_remain_exact(tmp_path, kind):
    root = tmp_path.resolve()
    files = {
        "src/top.py": "needle top\n",
        "src/nested/deep.py": "needle deep\n",
        "src/nested/other.txt": "needle other\n",
        "outside.py": "needle outside\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def search(pattern):
        args = (str(root), pattern, 100) if kind == "GLOB" else (
            str(root), "needle", pattern, 100)
        return script_call(kind, kind.lower() + "_hierarchy", *args)

    for directory_pattern in ("src", str(root / "src")):
        result = search(directory_pattern)
        assert str(root / "src/top.py") in result
        assert str(root / "src/nested/deep.py") in result
        assert str(root / "src/nested/other.txt") in result
        assert str(root / "outside.py") not in result

    for file_pattern in ("src/top.py", str(root / "src/top.py")):
        result = search(file_pattern)
        assert str(root / "src/top.py") in result
        assert str(root / "src/nested/deep.py") not in result

    for exclusion in ("!src", f"!{root / 'src'}"):
        result = search(f"**/*,{exclusion}")
        assert str(root / "outside.py") in result
        assert str(root / "src/top.py") not in result
        assert str(root / "src/nested/deep.py") not in result


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


@pytest.mark.parametrize("kind", ["GLOB", "GREP"])
def test_search_rejects_brace_expansion_and_accepts_comma_alternatives(tree, kind):
    function = kind.lower() + "_hierarchy"
    prefix = (str(tree),) if kind == "GLOB" else (str(tree), "needle")
    result = script_call(kind, function, *prefix, "**/*.{py,txt}", 100)
    assert result.startswith("Error:")
    assert "brace expansion is not supported" in result
    alternative = script_call(kind, function, *prefix, "**/*.py,**/*.txt", 100)
    assert str(tree / "a.py") in alternative
    assert str(tree / "b.txt") in alternative


def test_grep_skips_binary_and_bounds_large_file_matches(tmp_path):
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"needle\0" + b"x" * 1_000_000)
    large = tmp_path / "large.txt"
    large.write_text("needle\n" * 50_000 + "late marker\n")

    dense = script_call("GREP", "grep_hierarchy", str(tmp_path), "needle", "**/*", 5)
    assert dense.splitlines()[0].startswith("50000 matches across 1 files")
    assert len(dense.splitlines()[1:]) <= 5
    assert str(binary) not in dense

    late = script_call("GREP", "grep_hierarchy", str(tmp_path), "late marker", "**/*", 5)
    assert f"{large}:50001: late marker" in late
    assert str(binary) not in late


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
    tools.valves.persistent_volume = False
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


async def test_create_conflict_without_authoritative_winner_is_retryable(tools, transport):
    tools.valves.persistent_volume = False
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


@pytest.fixture
def preview(tools, sandbox, http):
    upstream, protected, secret = "https://secret.preview.test/", "https://owner.preview.test/", "installation-secret"
    tools.valves.preview_wrapper_url = "https://wrapper.test/register"
    tools.valves.preview_wrapper_key = secret
    good = {"url": protected, "expires_at": "2099-01-01T00:00:00Z"}

    async def invoke(payload=None, status=200, target="http:5000", user=USER,
                     access="private", tag="", setup_failure=False):
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
                assert port in (5000, 7681, 8080) or 20000 <= port < 60000
                assert request.url.params["expiresInSeconds"] == "86400"
                return httpx.Response(200, json={"url": upstream})
            assert request.url.host == "proxy.test"
            assert (request.method, request.url.path) == ("POST", "/sb/process/execute")
            if setup_failure:
                return httpx.Response(200, json={"exitCode": 1, "result": "checksum mismatch"})
            return httpx.Response(200, json={"exitCode": 0, "result": "READY PID=123"})
        http(handler)
        result = await tools.expose(target, access, tag, __user__=user, __event_emitter__=emit)
        return result, calls, json.dumps(events)
    return SimpleNamespace(invoke=invoke, upstream=upstream, protected=protected, secret=secret, good=good)


@pytest.mark.parametrize("target", ["http:5000", "dufs", "site:/Workspace/My Site", "ttyd", "code-server"])
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


async def test_ttyd_is_private_only_before_io(tools, monkeypatch):
    context = AsyncMock(side_effect=AssertionError("public ttyd reached I/O"))
    monkeypatch.setattr(lathe, "_tool_context", context)
    result = await tools.expose("ttyd", "public", __user__=USER)
    assert result.startswith("Error: ttyd is private-only")
    context.assert_not_called()


async def test_ttyd_uses_verified_idempotent_fast_path(preview):
    result, calls, _ = await preview.invoke(target="ttyd", tag="terminal")
    assert "Terminal URL" in result and "full writable shell" in result
    setup = next(r for r in calls if r.url.host == "proxy.test")
    request = json.loads(setup.content)
    script = request["command"]
    assert request["timeout"] == 60000
    assert "ttyd/releases/latest" in script
    assert 'a["name"] == "ttyd.x86_64"' in script
    assert 'a["name"] == "SHA256SUMS"' in script
    assert 'test "$ACTUAL" = "$EXPECTED"' in script
    assert "Port 7681 is occupied by a non-ttyd process" in script
    assert script.count("nohup /tmp/lathe/ttyd") == 1
    registration = next(r for r in calls if r.url.host == "wrapper.test")
    assert json.loads(registration.content)["access"] == "private"


async def test_ttyd_setup_failure_stops_before_preview(preview):
    result, calls, _ = await preview.invoke(target="ttyd", setup_failure=True)
    assert result.startswith("Error: ttyd setup failed")
    assert "checksum mismatch" in result
    assert not any(r.url.host == "wrapper.test" for r in calls)


@pytest.mark.parametrize("service", ["dufs", "code-server"])
@pytest.mark.parametrize("fault", [None, "stale", "release", "digest"])
def test_managed_archive_install_is_release_consistent_verified_and_atomic(
    tmp_path, service, fault,
):
    install_root = tmp_path / "lathe"
    install_root.mkdir()
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    if service == "dufs":
        repo, tag = "sigoden/dufs", "v9.8.7"
        asset_name = f"dufs-{tag}-x86_64-unknown-linux-musl.tar.gz"
        staged = fixtures / "dufs"
        staged.write_text("#!/bin/sh\nexit 0\n")
        staged.chmod(0o755)
        archive_members = [(staged, "dufs")]
        script = lathe._build_dufs_ensure_script(str(install_root), str(tmp_path))
        installed = install_root / "dufs"
        port = 5000
    else:
        repo, tag = "coder/code-server", "v9.8.7"
        asset_name = "code-server-9.8.7-linux-amd64.tar.gz"
        staged = fixtures / "code-server"
        staged.write_text("#!/bin/sh\nexit 0\n")
        staged.chmod(0o755)
        archive_members = [(staged, "code-server-9.8.7-linux-amd64/bin/code-server")]
        script = lathe._build_code_server_ensure_script(str(install_root), str(tmp_path))
        installed = install_root / "code-server/bin/code-server"
        port = 8080

    if fault == "stale":
        if service == "dufs":
            installed.write_text("incomplete")
        else:
            installed.parent.mkdir(parents=True)
            (installed.parent.parent / "partial-download").write_text("incomplete")

    archive = fixtures / "asset.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for source, arcname in archive_members:
            bundle.add(source, arcname=arcname)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    asset_url = f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"
    release = {
        "url": f"https://api.github.com/repos/{repo}/releases/12345",
        "tag_name": tag,
        "assets": [
            {"name": "decoy.tar.gz", "browser_download_url": "https://invalid.test/decoy", "digest": "sha256:" + "0" * 64},
            {"name": asset_name, "browser_download_url": asset_url, "digest": f"sha256:{digest}"},
        ],
    }
    if fault == "release":
        release["assets"][1]["browser_download_url"] = asset_url.replace(f"/{tag}/", "/v0.0.0/")
    elif fault == "digest":
        release["assets"][1]["digest"] = "sha256:" + "0" * 64
    release_path = fixtures / "release.json"
    release_path.write_text(json.dumps(release))

    curl_log = tmp_path / "curl.log"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        set -e
        printf '%s\\n' "$*" >> {shlex.quote(str(curl_log))}
        URL=""
        OUT=""
        while test "$#" -gt 0; do
          case "$1" in
            -o) OUT=$2; shift 2 ;;
            -*) shift ;;
            *) URL=$1; shift ;;
          esac
        done
        case "$URL" in
          https://api.github.com/repos/*/releases/latest) sleep 0.2; cp {shlex.quote(str(release_path))} "$OUT" ;;
          {shlex.quote(asset_url)}) cp {shlex.quote(str(archive))} "$OUT" ;;
          *) exit 90 ;;
        esac
        """))
    fake_curl.chmod(0o755)
    fake_ss = fake_bin / "ss"
    fake_ss.write_text(f"#!/bin/sh\nprintf '%s\\n' 'LISTEN 0 128 0.0.0.0:{port} users:((\"{service}\",pid=123,fd=3))'\n")
    fake_ss.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    if fault == "stale" and service == "code-server":
        processes = [
            subprocess.Popen(
                ["bash", "-c", script], text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            for _ in range(2)
        ]
        completed = [
            subprocess.CompletedProcess(process.args, process.wait(timeout=20), *process.communicate())
            for process in processes
        ]
        assert all(result.returncode == 0 for result in completed), completed
        result = completed[0]
    else:
        result = subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True, env=env, timeout=20,
        )
    calls = curl_log.read_text().splitlines()
    assert "/releases/latest" in calls[0]
    if fault in (None, "stale"):
        assert result.returncode == 0, result.stderr
        assert installed.read_text() == "#!/bin/sh\nexit 0\n"
        assert os.access(installed, os.X_OK)
        expected_calls = 4 if fault == "stale" and service == "code-server" else 2
        assert len(calls) == expected_calls and all(
            asset_url in call for call in calls if "/releases/latest" not in call
        )
    else:
        assert result.returncode != 0
        assert not installed.exists()
        assert not any(path.name.startswith(".install.") for path in install_root.iterdir())
        assert len(calls) == (1 if fault == "release" else 2)


async def test_site_fast_path_preserves_path_and_manages_only_its_server(preview):
    root = "/home/daytona/workspace/My Site"
    port = lathe._site_port(root)
    result, calls, _ = await preview.invoke(
        target=f"site:{root}", tag="demo",
    )
    assert "Static site URL" in result
    assert root in result
    setup = next(r for r in calls if r.url.host == "proxy.test")
    request = json.loads(setup.content)
    script = request["command"]
    assert request["timeout"] == 15000
    assert f"SITE_ROOT='{root}'" in script
    assert f"python3 -m http.server {port}" in script
    assert f"Assigned site port {port} is occupied by another process" in script
    assert "hash collision" in script
    registration = next(r for r in calls if r.url.host == "wrapper.test")
    assert json.loads(registration.content)["access"] == "private"


def test_site_ports_are_stable_and_allow_parallel_sites():
    first = lathe._site_port("/workspace/redesign-a")
    assert first == lathe._site_port("/workspace/redesign-a")
    assert first != lathe._site_port("/workspace/redesign-b")
    assert 20000 <= first < 60000


async def test_site_requires_absolute_path_before_io(tools, monkeypatch):
    context = AsyncMock(side_effect=AssertionError("relative site reached I/O"))
    monkeypatch.setattr(lathe, "_tool_context", context)
    result = await tools.expose("site:relative/path", "private", __user__=USER)
    assert result.startswith("Error: site path must be an absolute path")
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


def delegate_request(app=lambda scope, receive, send: None):
    return SimpleNamespace(
        app=app,
        state=SimpleNamespace(token=SimpleNamespace(credentials="user-token")),
    )


@pytest.mark.parametrize("context_files,missing", [(["relative"], False), (["/missing"], True)])
async def test_delegate_context_rejection_closes_every_created_client(
    tools, sandbox, http, context_files, missing,
):
    def respond(request):
        assert missing and request.url.path == "/sb/files/download"
        return httpx.Response(404)

    clients = http(respond)
    result = await tools.delegate(
        "task", context_files=context_files, __user__=USER,
        __request__=delegate_request(), __model__={"id": "model"},
    )
    assert result.startswith("Error:")
    assert len(clients) == 1
    assert [client.close_calls for client in clients] == [1]


@pytest.mark.parametrize("stage", ["provider", "tool", "prompt", "agent", "sidecar"])
async def test_delegate_setup_exception_closes_every_owned_client(
    tools, sandbox, http, monkeypatch, stage,
):
    clients = http(lambda request: pytest.fail(f"unexpected request: {request.url}"))

    def fail(*args, **kwargs):
        raise RuntimeError(f"{stage} setup failed")

    if stage == "provider":
        import pydantic_ai.providers.openai
        monkeypatch.setattr(pydantic_ai.providers.openai, "OpenAIProvider", fail)
    elif stage == "tool":
        monkeypatch.setattr(lathe, "_build_delegate_tools", fail)
    elif stage == "prompt":
        monkeypatch.setattr(lathe, "_build_delegate_prompt", fail)
    elif stage == "agent":
        monkeypatch.setattr(sys.modules["pydantic_ai"], "Agent", fail)
    else:
        monkeypatch.setattr(lathe, "_core_write", AsyncMock(side_effect=RuntimeError("sidecar setup failed")))

    result = await tools.delegate(
        "task", __user__=USER, __request__=delegate_request(),
        __model__={"id": "model"},
    )
    assert result == f"Error: {stage} setup failed"
    assert len(clients) == (2 if stage == "provider" else 3)
    assert [client.close_calls for client in clients] == [1] * len(clients)


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
        delegate_clients = clients[:3]
        assert len(delegate_clients) == 3
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
            assert [client.close_calls for client in delegate_clients] == [1, 0, 0]
            assert notice not in await read_in("chat")
        else:
            assert "verified result" in result and "1 tool call(s)" in result
        release.set()
        async with asyncio.timeout(5):
            if background:
                while notice not in (delivered := await read_in("chat")):
                    await asyncio.sleep(0)
                assert delivered.count(notice) == 1
            while not all(client.is_closed for client in delegate_clients):
                await asyncio.sleep(0)
        assert [client.close_calls for client in delegate_clients] == [1, 1, 1]
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
            assert body["model"] == "selected-model" and "chat_id" not in body
    finally:
        release.set()
        tasks = asyncio.all_tasks() - existing_tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client in clients:
            if not client.is_closed:
                await client.aclose()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

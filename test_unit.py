#!/usr/bin/env python3
"""
Unit tests for lathe.py — pure Python, no network, no sandbox.

Usage:
    uv run --script test_unit.py
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pydantic"]
# ///

import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))


# ── Test result tracking ─────────────────────────────────────────────

class Results:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, condition, detail=""):
        if condition:
            print(f"  PASS: {name}")
            self.passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            self.failed += 1


# ── Tests ────────────────────────────────────────────────────────────

async def test_parse_env_vars(R: Results):
    from lathe import _parse_env_vars

    print("\n── _parse_env_vars: valid JSON object ──")
    pairs = _parse_env_vars('{"FOO":"bar","BAZ":"qux"}')
    R.check("parses two pairs", len(pairs) == 2, f"got {len(pairs)}")
    R.check("first key is FOO", pairs[0] == ("FOO", "bar"), str(pairs[0]))
    R.check("second key is BAZ", pairs[1] == ("BAZ", "qux"), str(pairs[1]))

    print("\n── _parse_env_vars: empty / default ──")
    R.check("empty string returns []", _parse_env_vars("") == [], str(_parse_env_vars("")))
    R.check("bare {} returns []", _parse_env_vars("{}") == [], str(_parse_env_vars("{}")))
    R.check("whitespace only returns []", _parse_env_vars("   ") == [], str(_parse_env_vars("   ")))

    print("\n── _parse_env_vars: values with special chars ──")
    pairs = _parse_env_vars('{"KEY":"val=ue","OTHER":"has spaces","QUOTE":"it\'s"}')
    R.check("value with = preserved", ("KEY", "val=ue") in pairs, str(pairs))
    R.check("value with spaces preserved", ("OTHER", "has spaces") in pairs, str(pairs))
    R.check("value with quote preserved", ("QUOTE", "it's") in pairs, str(pairs))

    print("\n── _parse_env_vars: invalid keys raise ──")
    try:
        _parse_env_vars('{"GOOD":"yes","123bad":"no","also-bad":"no","_ok":"yes"}')
        R.check("invalid keys raise ValueError", False, "no exception raised")
    except ValueError as e:
        R.check("invalid keys raise ValueError", True)
        R.check("error mentions bad key", "123bad" in str(e) or "also-bad" in str(e), str(e))

    print("\n── _parse_env_vars: non-string values raise ──")
    try:
        _parse_env_vars('{"A":"ok","B":123,"C":true}')
        R.check("non-string values raise ValueError", False, "no exception raised")
    except ValueError as e:
        R.check("non-string values raise ValueError", True)
        R.check("error mentions bad key", "'B'" in str(e) or "'C'" in str(e), str(e))

    print("\n── _parse_env_vars: invalid JSON raises ──")
    try:
        _parse_env_vars("not json")
        R.check("garbage raises ValueError", False, "no exception raised")
    except ValueError as e:
        R.check("garbage raises ValueError", True)
        R.check("garbage error mentions JSON", "JSON" in str(e), str(e))

    try:
        _parse_env_vars('["a","b"]')
        R.check("array raises ValueError", False, "no exception raised")
    except ValueError as e:
        R.check("array raises ValueError", True)
        R.check("array error mentions object", "object" in str(e), str(e))


async def test_onboard_script(R: Results):
    from lathe import _build_onboard_script

    print("\n── _build_onboard_script: generates valid Python ──")
    script = _build_onboard_script("/home/daytona/workspace/myproject")
    try:
        compile(script, "<onboard>", "exec")
        R.check("script compiles", True)
    except SyntaxError as e:
        R.check("script compiles", False, str(e))

    R.check("script has PROJECT assignment", "PROJECT = '/home/daytona/workspace/myproject'" in script, script[:200])
    R.check("script references ~/.agents", "~/.agents" in script, "missing global path")
    R.check("script has ERROR_NO_CONTEXT sentinel", "ERROR_NO_CONTEXT" in script, "missing sentinel")

    print("\n── _build_onboard_script: handles tricky paths ──")
    script = _build_onboard_script("/home/daytona/workspace/it's a \"test\"")
    try:
        compile(script, "<onboard>", "exec")
        R.check("tricky path compiles", True)
    except SyntaxError as e:
        R.check("tricky path compiles", False, str(e))


async def test_truncate(R: Results):
    from lathe import _truncate_tail, _MAX_LINES, _MAX_BYTES

    print("\n── _truncate_tail: no truncation needed ──")
    short = "line 1\nline 2\nline 3"
    out, trunc, meta = _truncate_tail(short)
    R.check("short text not truncated", not trunc, f"truncated={trunc}")
    R.check("short text unchanged", out == short, out[:80])

    print("\n── _truncate_tail: line limit ──")
    many_lines = "\n".join(f"line {i}" for i in range(5000))
    out, trunc, meta = _truncate_tail(many_lines)
    R.check("many lines truncated", trunc, f"truncated={trunc}")
    R.check("truncated by lines", meta["truncated_by"] == "lines", meta.get("truncated_by"))
    R.check("keeps last N lines", out.endswith("line 4999"), out[-30:])
    R.check("total_lines correct", meta["total_lines"] == 5000, meta.get("total_lines"))
    out_line_count = out.count("\n") + 1
    R.check(f"output has <= {_MAX_LINES} lines", out_line_count <= _MAX_LINES, f"got {out_line_count}")

    print("\n── _truncate_tail: byte limit ──")
    fat_lines = "\n".join(f"{'x' * 99}" for _ in range(600))
    out, trunc, meta = _truncate_tail(fat_lines)
    R.check("fat lines truncated", trunc, f"truncated={trunc}")
    R.check("truncated by bytes", meta["truncated_by"] == "bytes", meta.get("truncated_by"))
    out_bytes = len(out.encode("utf-8"))
    R.check(f"output <= {_MAX_BYTES} bytes", out_bytes <= _MAX_BYTES, f"got {out_bytes}")

    print("\n── _truncate_tail: empty string ──")
    out, trunc, meta = _truncate_tail("")
    R.check("empty string not truncated", not trunc, f"truncated={trunc}")


async def test_shell_quote(R: Results):
    from lathe import _shell_quote

    print("\n── _shell_quote: basic quoting ──")
    R.check("simple string", _shell_quote("hello") == "'hello'", _shell_quote("hello"))
    R.check("empty string", _shell_quote("") == "''", _shell_quote(""))
    R.check("spaces preserved", _shell_quote("hello world") == "'hello world'", _shell_quote("hello world"))
    R.check("single quote escaped", _shell_quote("it's") == "'it'\\''s'", _shell_quote("it's"))
    R.check("dollar sign literal", _shell_quote("$HOME") == "'$HOME'", _shell_quote("$HOME"))
    R.check("backticks literal", _shell_quote("`whoami`") == "'`whoami`'", _shell_quote("`whoami`"))


async def test_require_abs_path(R: Results):
    from lathe import _require_abs_path

    print("\n── _require_abs_path: validation ──")
    R.check("absolute path passes", _require_abs_path("/home/daytona/file.txt") is None, "should be None")
    R.check("relative path fails", _require_abs_path("workspace/file.txt") is not None, "should be error")
    R.check("error mentions absolute", "absolute" in (_require_abs_path("file.txt") or ""), _require_abs_path("file.txt"))
    R.check("custom param name in error", "mypath" in (_require_abs_path("bad", "mypath") or ""), _require_abs_path("bad", "mypath"))


async def test_human_size(R: Results):
    from lathe import _human_size

    print("\n── _human_size: formatting ──")
    R.check("bytes", _human_size(500) == "500 B", _human_size(500))
    R.check("kilobytes", _human_size(1024) == "1.0 KB", _human_size(1024))
    R.check("megabytes", _human_size(1024 * 1024) == "1.0 MB", _human_size(1024 * 1024))
    R.check("large", "GB" in _human_size(2 * 1024**3), _human_size(2 * 1024**3))


async def test_prepend_warning(R: Results):
    from lathe import _prepend_warning

    print("\n── _prepend_warning: behavior ──")
    R.check("no warning returns result", _prepend_warning("result", None) == "result", "")
    R.check("warning prepended", _prepend_warning("result", "warn") == "warn\nresult", _prepend_warning("result", "warn"))


async def test_build_tool_catalog(R: Results):
    from lathe import _build_tool_catalog, Tools

    print("\n── _build_tool_catalog: introspection ──")
    tools = Tools()
    catalog = _build_tool_catalog(tools)
    R.check("catalog has bash", "bash(" in catalog, catalog[:200])
    R.check("catalog has read", "read(" in catalog, catalog[:200])
    R.check("catalog has write", "write(" in catalog, catalog[:200])
    R.check("catalog has edit", "edit(" in catalog, catalog[:200])
    R.check("catalog has expose", "expose(" in catalog, catalog[:200])
    R.check("catalog has destroy", "destroy(" in catalog, catalog[:200])
    R.check("catalog has onboard", "onboard(" in catalog, catalog[:200])
    R.check("catalog excludes lathe", "lathe(" not in catalog, "lathe should be excluded")
    R.check("catalog excludes private", "_" not in catalog.split("(")[0] if "(" in catalog else True, "private methods should be excluded")


# ── Test registry and runner ─────────────────────────────────────────

TESTS = {
    "parse_env_vars": test_parse_env_vars,
    "onboard_script": test_onboard_script,
    "truncate": test_truncate,
    "shell_quote": test_shell_quote,
    "require_abs_path": test_require_abs_path,
    "human_size": test_human_size,
    "prepend_warning": test_prepend_warning,
    "build_tool_catalog": test_build_tool_catalog,
}


async def main():
    args = sys.argv[1:]

    if "--list" in args:
        print("Available unit tests:")
        for name in TESTS:
            print(f"  {name}")
        return

    selected = args if args else list(TESTS.keys())
    for name in selected:
        if name not in TESTS:
            print(f"Unknown test: {name}. Use --list to see available tests.")
            sys.exit(1)

    R = Results()
    t0 = time.time()

    print(f"{'='*50}")
    print(f"UNIT TESTS ({', '.join(selected)})")
    print(f"{'='*50}")

    # Unit tests are instant — run concurrently
    await asyncio.gather(*(TESTS[name](R) for name in selected))

    elapsed = time.time() - t0
    total = R.passed + R.failed
    print(f"\n{'='*50}")
    print(f"Results: {R.passed} passed, {R.failed} failed out of {total}  ({elapsed:.1f}s)")
    if R.failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

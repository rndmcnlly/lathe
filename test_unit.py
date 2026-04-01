#!/usr/bin/env python3
"""
Unit tests for lathe.py — pure Python, no network, no sandbox.

Usage:
    uv run python test_unit.py
"""

import asyncio
import subprocess
import sys
import os
import tempfile
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


async def test_build_tool_catalog(R: Results):
    from lathe import _build_tool_catalog, Tools

    print("\n── _build_tool_catalog: filtering ──")
    tools = Tools()
    catalog = _build_tool_catalog(tools)
    R.check("catalog excludes lathe", "lathe(" not in catalog, "lathe should be excluded")
    R.check("catalog excludes private", "_" not in catalog.split("(")[0] if "(" in catalog else True, "private methods should be excluded")
    # Sanity: catalog is non-empty (at least one real tool listed)
    R.check("catalog is non-empty", len(catalog) > 0, "catalog should list at least one tool")


async def test_glob_script(R: Results):
    from lathe import _GLOB_SCRIPT

    def run_glob(base_dir, pattern, max_lines):
        """Exec the glob script via subprocess, return stdout."""
        script = (
            _GLOB_SCRIPT
            + f"\nprint(glob_hierarchy({base_dir!r}, {pattern!r}, {max_lines!r}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"SCRIPT ERROR: {result.stderr}"
        return result.stdout.rstrip("\n")

    def parse_header(output):
        """Extract match count from header line."""
        first_line = output.split("\n")[0]
        return int(first_line.split(" ")[0])

    def body_lines(output):
        """All lines after the header."""
        return output.split("\n")[1:]

    # ── Build a known directory tree ─────────────────────────────
    #
    #   tmp/
    #     a.py
    #     b.py
    #     c.txt
    #     sub/
    #       d.py
    #       e.py
    #       deep/
    #         f.py
    #         g.py
    #         h.py
    #     other/
    #       i.py
    #     empty/
    #     chain/
    #       only/
    #         child/
    #           leaf.py

    with tempfile.TemporaryDirectory() as tmp:
        # Create files
        for name in ["a.py", "b.py", "c.txt"]:
            open(os.path.join(tmp, name), "w").close()

        os.makedirs(os.path.join(tmp, "sub", "deep"))
        for name in ["d.py", "e.py"]:
            open(os.path.join(tmp, "sub", name), "w").close()
        for name in ["f.py", "g.py", "h.py"]:
            open(os.path.join(tmp, "sub", "deep", name), "w").close()

        os.makedirs(os.path.join(tmp, "other"))
        open(os.path.join(tmp, "other", "i.py"), "w").close()

        os.makedirs(os.path.join(tmp, "empty"))

        os.makedirs(os.path.join(tmp, "chain", "only", "child"))
        open(os.path.join(tmp, "chain", "only", "child", "leaf.py"), "w").close()

        # ── Full expansion with generous budget ──────────────────
        print("\n── glob_script: full expansion ──")
        out = run_glob(tmp, "**/*.py", 100)
        R.check("no script error", not out.startswith("SCRIPT ERROR"), out[:200])
        R.check("header shows 9 matches", parse_header(out) == 9, out.split("\n")[0])
        lines = body_lines(out)
        R.check("9 file lines", len(lines) == 9, f"got {len(lines)}")
        R.check("all paths absolute", all(l.startswith("/") for l in lines), lines[:3])
        R.check("no collapsed dirs", not any("matches)" in l for l in lines), str(lines))
        R.check("no budget note in header", "budget" not in out.split("\n")[0], out.split("\n")[0])

        # ── Tight budget forces collapsing ───────────────────────
        print("\n── glob_script: tight budget ──")
        out = run_glob(tmp, "**/*.py", 5)
        R.check("header still shows 9", parse_header(out) == 9, out.split("\n")[0])
        lines = body_lines(out)
        R.check("body within budget", len(lines) <= 5, f"got {len(lines)} lines")
        R.check("budget note in header", "budget" in out.split("\n")[0], out.split("\n")[0])
        # sub/ has the most matches (5) so it should be collapsed
        collapsed = [l for l in lines if "matches)" in l]
        R.check("at least one collapsed dir", len(collapsed) >= 1, str(lines))

        # ── Match counts are conserved ───────────────────────────
        print("\n── glob_script: count conservation ──")
        out = run_glob(tmp, "**/*.py", 5)
        total = parse_header(out)
        lines = body_lines(out)
        # Count: each plain file = 1, each "(N matches)" = N, each "... and N more" = N
        accounted = 0
        for l in lines:
            if "... and " in l and " more matches" in l:
                accounted += int(l.split("... and ")[1].split(" more")[0])
            elif "matches)" in l:
                accounted += int(l.split("(")[1].split(" ")[0])
            else:
                accounted += 1
        R.check("counts conserved", accounted == total,
                f"accounted {accounted} vs header {total}")

        # ── Single-child chains expand for free ──────────────────
        print("\n── glob_script: single-child chain ──")
        out = run_glob(tmp, "chain/**/*.py", 5)
        R.check("header shows 1 match", parse_header(out) == 1, out.split("\n")[0])
        lines = body_lines(out)
        R.check("leaf.py fully expanded", len(lines) == 1, f"got {len(lines)}")
        R.check("shows absolute path to leaf",
                lines[0].endswith("chain/only/child/leaf.py"), lines[0])

        # ── Pattern filters correctly ────────────────────────────
        print("\n── glob_script: pattern filtering ──")
        out = run_glob(tmp, "*.txt", 100)
        R.check("txt header shows 1", parse_header(out) == 1, out.split("\n")[0])
        R.check("only c.txt", body_lines(out)[0].endswith("c.txt"), body_lines(out))

        # ── No matches ───────────────────────────────────────────
        print("\n── glob_script: no matches ──")
        out = run_glob(tmp, "**/*.rs", 100)
        R.check("reports 0 matches", out.startswith("0 matches"), out[:50])

        # ── Bad directory ────────────────────────────────────────
        print("\n── glob_script: bad directory ──")
        out = run_glob("/nonexistent/path", "**/*", 100)
        R.check("reports error", out.startswith("Error:"), out[:50])

        # ── Partial expansion ────────────────────────────────────
        print("\n── glob_script: partial expansion ──")
        # sub/ has 5 matches (2 files + deep/ with 3). Expanding sub/
        # costs 2 net lines (3 children - 1). Budget=7 means:
        #   root: 2 files + 3 collapsed dirs = 5 lines
        #   expand sub/ (+2): 2 files + deep/(collapsed) = 7 lines
        #   expand deep/ (+2 net) would be 9 — over budget.
        # But deep/ has 3 children and expanding costs 2 net lines.
        # At budget=8, deep/ expansion fits (7+2=9 > 8) — nope.
        # At budget=9, deep fits. So budget=8 should leave deep/ collapsed.
        #
        # For actual partial expansion we need a dir with many children.
        # Make a wide/ dir with 10 files:
        os.makedirs(os.path.join(tmp, "wide"))
        for j in range(10):
            open(os.path.join(tmp, "wide", f"w{j}.py"), "w").close()

        # Now root has 2 files + 4 dirs = 6 lines.
        # wide/ has 10 children; full expansion costs 9 net lines.
        # Budget=10: 6 + 9 = 15 > 10, so wide/ gets partial expansion.
        # Budget_for_children = 10 - 6 = 4 items shown + overflow line.
        out = run_glob(tmp, "**/*.py", 10)
        lines = body_lines(out)
        R.check("body within budget", len(lines) <= 10, f"got {len(lines)}")
        overflow = [l for l in lines if "... and " in l and " more matches" in l]
        R.check("has overflow line", len(overflow) >= 1, str(lines))
        # Verify conservation still holds
        total = parse_header(out)
        accounted = 0
        for l in lines:
            if "... and " in l and " more matches" in l:
                accounted += int(l.split("... and ")[1].split(" more")[0])
            elif "matches)" in l:
                accounted += int(l.split("(")[1].split(" ")[0])
            else:
                accounted += 1
        R.check("partial expansion counts conserved", accounted == total,
                f"accounted {accounted} vs header {total}")

        # ── Multi-pattern union ──────────────────────────────────
        print("\n── glob_script: multi-pattern union ──")
        out = run_glob(tmp, "**/*.py,**/*.txt", 100)
        total = parse_header(out)
        # 19 .py files (original 9 + 10 in wide/) + 1 .txt = 20
        R.check("union includes both extensions", total == 20,
                f"expected 20, got {total}")
        lines = body_lines(out)
        has_py = any(l.endswith(".py") for l in lines)
        has_txt = any(l.endswith(".txt") for l in lines)
        R.check("has .py files", has_py, str(lines[:3]))
        R.check("has .txt files", has_txt, str(lines[:3]))

        # ── Negation excludes matches ────────────────────────────
        print("\n── glob_script: negation ──")
        out = run_glob(tmp, "**/*.py,!**/deep/**", 100)
        total = parse_header(out)
        # 19 .py total minus 3 in deep/ = 16
        R.check("negation removes deep/", total == 16,
                f"expected 16, got {total}")
        lines = body_lines(out)
        has_deep = any("deep" in l for l in lines)
        R.check("no deep/ files in output", not has_deep, str([l for l in lines if "deep" in l]))

        # ── Negation is order-independent ────────────────────────
        print("\n── glob_script: negation order-independent ──")
        out_a = run_glob(tmp, "**/*.py,!**/wide/**", 100)
        out_b = run_glob(tmp, "!**/wide/**,**/*.py", 100)
        R.check("order does not matter",
                parse_header(out_a) == parse_header(out_b),
                f"{parse_header(out_a)} vs {parse_header(out_b)}")

        # ── Braces not split ─────────────────────────────────────
        print("\n── glob_script: braces preserved ──")
        # {py,txt} brace expansion — if Python supports it, should
        # match both; if not, 0 matches.  Either way, no crash.
        out = run_glob(tmp, "*.{py,txt}", 100)
        R.check("brace pattern no error",
                not out.startswith("Error:") and not out.startswith("SCRIPT ERROR"),
                out[:80])
        # Verify braces + comma delimiter coexist:
        # "*.{py,txt},!**/deep/**" should parse as two terms, not three.
        out = run_glob(tmp, "**/*.{py,txt},!**/deep/**", 100)
        total = parse_header(out)
        lines = body_lines(out)
        has_deep = any("deep" in l for l in lines)
        R.check("brace+negation excludes deep/", not has_deep,
                str([l for l in lines if "deep" in l]))

        # ── No positive patterns is an error ─────────────────────
        print("\n── glob_script: no positive patterns ──")
        out = run_glob(tmp, "!**/*.py", 100)
        R.check("rejects all-negative", out.startswith("Error:"), out[:80])

        # ── Negation count conservation ──────────────────────────
        print("\n── glob_script: negation count conservation ──")
        out = run_glob(tmp, "**/*,!**/wide/**,!**/deep/**", 5)
        total = parse_header(out)
        lines = body_lines(out)
        accounted = 0
        for l in lines:
            if "... and " in l and " more matches" in l:
                accounted += int(l.split("... and ")[1].split(" more")[0])
            elif "matches)" in l:
                accounted += int(l.split("(")[1].split(" ")[0])
            else:
                accounted += 1
        R.check("negation counts conserved", accounted == total,
                f"accounted {accounted} vs header {total}")


# ── Test registry and runner ─────────────────────────────────────────

TESTS = {
    "parse_env_vars": test_parse_env_vars,
    "onboard_script": test_onboard_script,
    "truncate": test_truncate,
    "shell_quote": test_shell_quote,
    "require_abs_path": test_require_abs_path,
    "build_tool_catalog": test_build_tool_catalog,
    "glob_script": test_glob_script,
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

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
    R.check("script uses os.listdir", "os.listdir" in script, "missing directory listing call")

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

        # ── Absolute pattern under base is silently relativized ──
        print("\n── glob_script: absolute pattern relativized ──")
        # A model that knows the workspace root naturally uses absolute
        # paths like /tmp/something/**/*.py.  The fix should strip the
        # base prefix and produce the same result as the relative form.
        # Use the resolved path because Path.resolve() normalises symlinks
        # (e.g. /var -> /private/var on macOS), matching what the script
        # itself does when it calls Path(base_dir).resolve().
        import pathlib as _pathlib
        abs_pattern = str(_pathlib.Path(tmp).resolve()) + "/**/*.py"
        rel_pattern = "**/*.py"
        out_abs = run_glob(tmp, abs_pattern, 100)
        out_rel = run_glob(tmp, rel_pattern, 100)
        R.check("abs pattern no script error",
                not out_abs.startswith("SCRIPT ERROR") and not out_abs.startswith("Error:"),
                out_abs[:200])
        R.check("abs pattern matches same count as relative",
                parse_header(out_abs) == parse_header(out_rel),
                f"abs={parse_header(out_abs)}, rel={parse_header(out_rel)}")

        # ── Absolute pattern outside base works ─────────────────
        print("\n── glob_script: absolute pattern outside base ──")
        # Create a sibling directory outside the "workspace" tmp dir
        # and verify we can glob into it with an absolute pattern.
        import pathlib as _pathlib2
        sibling = tempfile.mkdtemp()
        try:
            open(os.path.join(sibling, "outside.py"), "w").close()
            resolved_sibling = str(_pathlib2.Path(sibling).resolve())
            out_outside = run_glob(tmp, resolved_sibling + "/**/*.py", 100)
            R.check("outside-base pattern no error",
                    not out_outside.startswith("SCRIPT ERROR") and not out_outside.startswith("Error:"),
                    out_outside[:200])
            R.check("outside-base finds file",
                    parse_header(out_outside) == 1,
                    out_outside.split("\n")[0])
            R.check("outside-base shows absolute path",
                    "outside.py" in out_outside,
                    out_outside)
        finally:
            import shutil
            shutil.rmtree(sibling)


async def test_grep_script(R: Results):
    from lathe import _GREP_SCRIPT

    def run_grep(base_dir, regex, files_pattern, max_lines):
        """Exec the grep script via subprocess, return stdout."""
        script = (
            _GREP_SCRIPT
            + f"\nprint(grep_hierarchy({base_dir!r}, {regex!r}, {files_pattern!r}, {max_lines!r}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return f"SCRIPT ERROR: {result.stderr}"
        return result.stdout.rstrip("\n")

    def parse_header_matches(output):
        """Extract total match count from header."""
        return int(output.split("\n")[0].split(" ")[0])

    def parse_header_files(output):
        """Extract file count from header."""
        first = output.split("\n")[0]
        # "N matches across M files for ..."
        return int(first.split(" across ")[1].split(" ")[0])

    def body_lines(output):
        return output.split("\n")[1:]

    # ── Build a known directory tree with known content ──────────
    #
    #   tmp/
    #     a.py          contains: "def foo():", "def bar():"
    #     b.py          contains: "def baz():"
    #     c.txt         contains: "def txt_func():"
    #     sub/
    #       d.py        contains: "def sub_one():", "def sub_two():", "def sub_three():"
    #       e.py        contains: "class Helper:"
    #     other/
    #       f.py        contains: "def other_func():"

    with tempfile.TemporaryDirectory() as tmp:
        def writef(relpath, content):
            full = os.path.join(tmp, relpath)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

        writef("a.py", "def foo():\n    pass\ndef bar():\n    pass\n")
        writef("b.py", "def baz():\n    pass\n")
        writef("c.txt", "def txt_func():\n    pass\n")
        writef("sub/d.py", "def sub_one():\n    pass\ndef sub_two():\n    pass\ndef sub_three():\n    pass\n")
        writef("sub/e.py", "class Helper:\n    pass\n")
        writef("other/f.py", "def other_func():\n    pass\n")

        # ── Full expansion with generous budget ──────────────────
        print("\n── grep_script: full expansion ──")
        out = run_grep(tmp, "def ", "**/*.py", 100)
        R.check("no script error", not out.startswith("SCRIPT ERROR"), out[:200])
        total = parse_header_matches(out)
        R.check("header shows 7 matches", total == 7, out.split("\n")[0])
        n_files = parse_header_files(out)
        R.check("header shows 4 files", n_files == 4, out.split("\n")[0])
        lines = body_lines(out)
        # All 7 matches should be expanded as file:line: text
        match_lines = [l for l in lines if ":" in l and "matches)" not in l]
        R.check("7 match lines", len(match_lines) == 7, f"got {len(match_lines)}")
        R.check("no budget note", "budget" not in out.split("\n")[0], out.split("\n")[0])

        # ── File scope filtering ─────────────────────────────────
        print("\n── grep_script: file scope ──")
        out = run_grep(tmp, "def ", "**/*.txt", 100)
        total = parse_header_matches(out)
        R.check("txt scope finds 1", total == 1, out.split("\n")[0])

        # ── Tight budget collapses files ─────────────────────────
        print("\n── grep_script: tight budget ──")
        # 4 files with matches; budget=5 means d.py (3 matches) can
        # expand (+2 net) for 6 total, which won't fit. So all stay
        # collapsed except single-match files (free to expand).
        out = run_grep(tmp, "def ", "**/*.py", 5)
        lines = body_lines(out)
        R.check("body within budget", len(lines) <= 5, f"got {len(lines)}")
        R.check("budget note in header", "budget" in out.split("\n")[0], out.split("\n")[0])
        collapsed = [l for l in lines if "matches)" in l and "... and" not in l]
        R.check("at least one collapsed file", len(collapsed) >= 1, str(lines))

        # ── Match count conservation ─────────────────────────────
        print("\n── grep_script: count conservation ──")
        out = run_grep(tmp, "def ", "**/*.py", 5)
        total = parse_header_matches(out)
        lines = body_lines(out)
        accounted = 0
        for l in lines:
            if "... and " in l and " more match" in l:
                accounted += int(l.split("... and ")[1].split(" more")[0])
            elif "matches)" in l and "files)" not in l:
                # "file (N matches)" — extract N
                accounted += int(l.split("(")[1].split(" ")[0])
            elif "matches in " in l:
                # "dir/ (N matches in M files)" — extract N
                accounted += int(l.split("(")[1].split(" ")[0])
            else:
                accounted += 1
        R.check("counts conserved", accounted == total,
                f"accounted {accounted} vs header {total}")

        # ── Negation in file scope ───────────────────────────────
        print("\n── grep_script: file negation ──")
        out = run_grep(tmp, "def ", "**/*.py,!**/sub/**", 100)
        total = parse_header_matches(out)
        # a.py(2) + b.py(1) + other/f.py(1) = 4
        R.check("negation removes sub/", total == 4,
                f"expected 4, got {total}")
        lines = body_lines(out)
        has_sub = any("sub" in l for l in lines)
        R.check("no sub/ in output", not has_sub, str([l for l in lines if "sub" in l]))

        # ── No matches ───────────────────────────────────────────
        print("\n── grep_script: no matches ──")
        out = run_grep(tmp, "ZZZNOMATCH", "**/*.py", 100)
        R.check("reports 0 matches", "0 matches" in out, out[:50])

        # ── Bad regex ────────────────────────────────────────────
        print("\n── grep_script: bad regex ──")
        out = run_grep(tmp, "[invalid", "**/*.py", 100)
        R.check("reports regex error", out.startswith("Error:"), out[:80])

        # ── Bad directory ────────────────────────────────────────
        print("\n── grep_script: bad directory ──")
        out = run_grep("/nonexistent/path", "foo", "**/*", 100)
        R.check("reports dir error", out.startswith("Error:"), out[:50])

        # ── Single-match file expands for free ───────────────────
        print("\n── grep_script: single match file ──")
        out = run_grep(tmp, "class ", "**/*.py", 100)
        total = parse_header_matches(out)
        R.check("finds 1 class match", total == 1, out.split("\n")[0])
        lines = body_lines(out)
        R.check("match line has line number",
                any(":1: class Helper:" in l for l in lines), str(lines))

        # ── Partial file expansion ───────────────────────────────
        print("\n── grep_script: partial file expansion ──")
        # d.py has 3 matches. With budget=2 and 4 files:
        # all collapsed = 4 lines > budget 2.
        # Actually we need a scenario where files fit but match lines don't.
        # Make a file with many matches:
        writef("many.py", "\n".join(f"def func_{i}():" for i in range(20)))
        out = run_grep(tmp, "def ", "many.py", 10)
        total = parse_header_matches(out)
        R.check("many.py has 20 matches", total == 20, out.split("\n")[0])
        lines = body_lines(out)
        R.check("body within budget", len(lines) <= 10, f"got {len(lines)}")
        overflow = [l for l in lines if "... and " in l and " more match" in l]
        R.check("has overflow line", len(overflow) == 1, str(lines))

        # ── Line truncation ──────────────────────────────────────
        print("\n── grep_script: line truncation ──")
        writef("long.py", "def " + "x" * 300 + "():\n    pass\n")
        out = run_grep(tmp, "def ", "long.py", 100)
        lines = body_lines(out)
        R.check("long line truncated", lines[0].endswith("..."), lines[0][-20:])
        # 200 char max + file:line: prefix + "..."
        content_part = lines[0].split(": ", 1)[1] if ": " in lines[0] else lines[0]
        R.check("truncated within limit", len(content_part) <= 210,
                f"got {len(content_part)}")

        # ── Absolute files pattern under base is relativized ─────
        print("\n── grep_script: absolute files pattern relativized ──")
        # Use the resolved path (see glob test comment above).
        import pathlib as _pathlib
        abs_files = str(_pathlib.Path(tmp).resolve()) + "/**/*.py"
        rel_files = "**/*.py"
        out_abs = run_grep(tmp, "def ", abs_files, 100)
        out_rel = run_grep(tmp, "def ", rel_files, 100)
        R.check("abs files pattern no script error",
                not out_abs.startswith("SCRIPT ERROR") and not out_abs.startswith("Error:"),
                out_abs[:200])
        R.check("abs files pattern matches same count as relative",
                parse_header_matches(out_abs) == parse_header_matches(out_rel),
                f"abs={parse_header_matches(out_abs)}, rel={parse_header_matches(out_rel)}")

        # ── Absolute files pattern outside base works ────────────
        print("\n── grep_script: absolute files pattern outside base ──")
        # Create a sibling directory outside the "workspace" tmp dir
        # and verify we can grep into it with an absolute files pattern.
        import pathlib as _pathlib2
        sibling = tempfile.mkdtemp()
        try:
            with open(os.path.join(sibling, "outside.py"), "w") as f:
                f.write("def outside_func():\n    pass\n")
            resolved_sibling = str(_pathlib2.Path(sibling).resolve())
            out_outside = run_grep(tmp, "def ", resolved_sibling + "/**/*.py", 100)
            R.check("outside-base files pattern no error",
                    not out_outside.startswith("SCRIPT ERROR") and not out_outside.startswith("Error:"),
                    out_outside[:200])
            R.check("outside-base grep finds match",
                    parse_header_matches(out_outside) == 1,
                    out_outside.split("\n")[0])
            R.check("outside-base grep shows file",
                    "outside.py" in out_outside,
                    out_outside)
        finally:
            import shutil
            shutil.rmtree(sibling)


async def test_delegate_infrastructure(R: Results):
    from lathe import (
        _build_delegate_system_prompt, _DELEGATE_WITHHELD,
        _DELEGATE_NUDGE_REMAINING,
        _build_tool_catalog, Tools,
    )

    print("\n── delegate: system prompt (static content) ──")
    # Test with a representative budget
    prompt_10 = _build_delegate_system_prompt(10)
    R.check("system prompt non-empty", len(prompt_10) > 100,
            f"length={len(prompt_10)}")
    R.check("system prompt mentions absolute paths",
            "absolute" in prompt_10.lower(),
            "should mention absolute path requirement")
    R.check("system prompt mentions /home/daytona/workspace",
            "/home/daytona/workspace" in prompt_10,
            "should mention default working directory")

    print("\n── delegate: system prompt (step budget) ──")
    R.check("prompt includes step count",
            "10 steps" in prompt_10,
            "should state the budget")
    R.check("prompt mentions planning",
            "plan" in prompt_10.lower(),
            "should tell sub-agent to plan")
    R.check("prompt mentions summary",
            "summary" in prompt_10.lower(),
            "should mention reserving steps for summary")
    R.check("prompt mentions handoff value",
            "handoff" in prompt_10.lower() or "hand off" in prompt_10.lower(),
            "should emphasize handing off work over rushing to finish")

    # Verify budget is parameterized, not hardcoded
    prompt_5 = _build_delegate_system_prompt(5)
    R.check("budget is parameterized (5 vs 10)",
            "5 steps" in prompt_5 and "10 steps" not in prompt_5,
            f"5-step prompt should say '5 steps'")
    prompt_1 = _build_delegate_system_prompt(1)
    R.check("budget works for edge case (1 step)",
            "1 steps" in prompt_1,
            "1-step prompt should state the budget")

    print("\n── delegate: nudge threshold ──")
    R.check("nudge threshold is positive int",
            isinstance(_DELEGATE_NUDGE_REMAINING, int) and _DELEGATE_NUDGE_REMAINING > 0,
            f"got {_DELEGATE_NUDGE_REMAINING}")
    R.check("nudge threshold is reasonable (<=5)",
            _DELEGATE_NUDGE_REMAINING <= 5,
            f"got {_DELEGATE_NUDGE_REMAINING}, should not be too aggressive")

    print("\n── delegate: withheld tools ──")
    R.check("withheld is a set", isinstance(_DELEGATE_WITHHELD, set),
            type(_DELEGATE_WITHHELD).__name__)
    R.check("lathe withheld", "lathe" in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("onboard withheld", "onboard" in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("expose withheld", "expose" in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("destroy withheld", "destroy" in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("delegate withheld (no recursion)", "delegate" in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("bash NOT withheld", "bash" not in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))
    R.check("read NOT withheld", "read" not in _DELEGATE_WITHHELD, str(_DELEGATE_WITHHELD))

    print("\n── delegate: tool catalog includes delegate ──")
    tools = Tools()
    catalog = _build_tool_catalog(tools)
    R.check("catalog includes delegate", "delegate(" in catalog,
            "delegate should appear in tool catalog")
    R.check("catalog includes bash", "bash(" in catalog,
            "bash should appear in tool catalog")
    # Verify delegate params are shown (task, context_files, max_steps — no standalone context)
    delegate_line = [l for l in catalog.split("\n") if "delegate(" in l]
    R.check("delegate line exists", len(delegate_line) == 1,
            f"got {len(delegate_line)} lines")
    if delegate_line:
        R.check("delegate shows task param", "task" in delegate_line[0],
                delegate_line[0])
        R.check("delegate shows context_files param", "context_files" in delegate_line[0],
                delegate_line[0])
        R.check("delegate shows max_steps param", "max_steps" in delegate_line[0],
                delegate_line[0])
        R.check("delegate shows foreground_seconds param", "foreground_seconds" in delegate_line[0],
                delegate_line[0])
        # Ensure the old standalone "context" param is gone (context_files contains
        # "context" as a substring, so check the actual param list in parens)
        import re as _re
        paren_match = _re.search(r"delegate\(([^)]*)\)", delegate_line[0])
        if paren_match:
            params_str = paren_match.group(1)
            param_names = [p.strip() for p in params_str.split(",")]
            R.check("no standalone context param", "context" not in param_names,
                    f"params: {param_names}")


async def test_delegate_prompt_build(R: Results):
    """Test _build_delegate_prompt assembles the sub-agent prompt correctly."""
    from lathe import _build_delegate_prompt

    print("\n── delegate prompt: task only ──")
    msg = _build_delegate_prompt("Do the thing", [])
    R.check("task-only starts with ## Task", msg.startswith("## Task"), msg[:30])
    R.check("task-only contains task text", "Do the thing" in msg, msg)
    R.check("task-only no Context section", "## Context" not in msg, msg)
    R.check("task-only no Reference Files section", "## Reference Files" not in msg, msg)

    print("\n── delegate prompt: task with inline context ──")
    msg = _build_delegate_prompt("Do the thing. Error: something broke", [])
    R.check("has Task section", "## Task" in msg, msg)
    R.check("no Context section", "## Context" not in msg, msg)
    R.check("inline context present in task", "Error: something broke" in msg, msg)
    R.check("no Reference Files section", "## Reference Files" not in msg, msg)

    print("\n── delegate prompt: task + context_files ──")
    files = ["### /home/daytona/workspace/AGENTS.md\n\nBe careful."]
    msg = _build_delegate_prompt("Do the thing", files)
    R.check("has Task section", "## Task" in msg, msg)
    R.check("no Context section", "## Context" not in msg, msg)
    R.check("has Reference Files section", "## Reference Files" in msg, msg)
    R.check("file path in output", "/home/daytona/workspace/AGENTS.md" in msg, msg)
    R.check("file content in output", "Be careful." in msg, msg)

    print("\n── delegate prompt: task + context_files (multiple) ──")
    files = [
        "### /home/daytona/workspace/AGENTS.md\n\nBe careful.",
        "### /home/daytona/workspace/.agents/skills/deploy/SKILL.md\n\nDeploy instructions.",
    ]
    msg = _build_delegate_prompt("Fix the deploy. Build failed with exit 1", files)
    R.check("has Task and Reference Files sections",
            "## Task" in msg and "## Reference Files" in msg,
            msg[:200])
    R.check("no Context section", "## Context" not in msg, msg)
    R.check("task text", "Fix the deploy" in msg, msg)
    R.check("inline context in task", "Build failed with exit 1" in msg, msg)
    R.check("first file present", "AGENTS.md" in msg, msg)
    R.check("second file present", "SKILL.md" in msg, msg)
    R.check("second file content", "Deploy instructions." in msg, msg)

    print("\n── delegate prompt: section ordering ──")
    # Task must come before Reference Files
    task_pos = msg.index("## Task")
    ref_pos = msg.index("## Reference Files")
    R.check("Task before Reference Files", task_pos < ref_pos, f"{task_pos} vs {ref_pos}")


async def test_delegate_tools_build(R: Results):
    """Test that _build_delegate_tools produces the expected set of tools."""
    from lathe import _build_delegate_tools

    print("\n── delegate tools: structure ──")

    # We can't call the tools (they need a real sandbox), but we can
    # verify the factory produces the right number and names.
    # Use a mock valves/sandbox_id/client — the factory only captures
    # them in closures, doesn't call anything during construction.
    class FakeValves:
        daytona_api_key = "fake"
        daytona_api_url = "https://fake.api"
        daytona_proxy_url = "https://fake.proxy"

    tools = _build_delegate_tools(FakeValves(), "fake-sandbox-id", None, [])
    tool_names = {t.name for t in tools}

    R.check("produces 6 tools", len(tools) == 6, f"got {len(tools)}")
    R.check("has bash", "bash" in tool_names, str(tool_names))
    R.check("has read", "read" in tool_names, str(tool_names))
    R.check("has write", "write" in tool_names, str(tool_names))
    R.check("has edit", "edit" in tool_names, str(tool_names))
    R.check("has glob", "glob" in tool_names, str(tool_names))
    R.check("has grep", "grep" in tool_names, str(tool_names))

    # Verify withheld tools are NOT present
    from lathe import _DELEGATE_WITHHELD
    for withheld in _DELEGATE_WITHHELD:
        R.check(f"does not have {withheld}", withheld not in tool_names,
                str(tool_names))

    # Verify _doc_from_core decorator populates docstrings from _core_*
    print("\n── delegate tools: docstrings from _core_* ──")
    from lathe import _core_read, _core_write, _core_edit, _core_bash, _core_glob, _core_grep
    import inspect
    cores = {
        "bash": _core_bash, "read": _core_read, "write": _core_write,
        "edit": _core_edit, "glob": _core_glob, "grep": _core_grep,
    }
    for t in tools:
        core_fn = cores[t.name]
        core_doc = inspect.getdoc(core_fn) or ""
        # The tool's description should be the summary from the core docstring
        # (everything before the first :param line)
        summary = core_doc.split(":param")[0].strip()
        R.check(f"{t.name} description from core",
                t.description == summary,
                f"got {t.description!r}, expected {summary!r}")
        # Verify param descriptions made it into the schema
        td = t.tool_def
        schema_props = td.parameters_json_schema.get("properties", {})
        for line in core_doc.split("\n"):
            line = line.strip()
            if line.startswith(":param "):
                # Parse ":param name: description"
                rest = line[len(":param "):]
                pname, pdesc = rest.split(":", 1)
                pname = pname.strip()
                pdesc = pdesc.strip()
                if pname in ("valves", "sandbox_id", "client", "emit", "user_pairs"):
                    continue  # infrastructure params not in closure signature
                if pname in schema_props:
                    got_desc = schema_props[pname].get("description", "")
                    R.check(f"{t.name}.{pname} has description",
                            got_desc == pdesc,
                            f"got {got_desc!r}, expected {pdesc!r}")


async def test_build_bash_script(R: Results):
    from lathe import _build_bash_script

    print("\n── _build_bash_script: basic structure ──")
    script = _build_bash_script("echo hello", [], "/tmp/cmd/test/pid", "/tmp/cmd/test/log")
    R.check("starts with shebang", script.startswith("#!/usr/bin/env bash\n"), script[:30])
    R.check("has set -e", "set -e -o pipefail" in script, script[:100])
    R.check("has DEBIAN_FRONTEND", "DEBIAN_FRONTEND=noninteractive" in script, "missing env")
    R.check("writes PID", "/tmp/cmd/test/pid" in script, "missing PID path")
    R.check("tees to log", "/tmp/cmd/test/log" in script, "missing log path")
    R.check("has command", "echo hello" in script, "missing command")
    R.check("ends with newline", script.endswith("\n"), "should end with newline")

    print("\n── _build_bash_script: user env vars ──")
    script = _build_bash_script("ls", [("FOO", "bar"), ("SECRET", "it's a \"test\"")],
                                "/tmp/cmd/x/pid", "/tmp/cmd/x/log")
    R.check("exports FOO", "export FOO='bar'" in script, script[:300])
    R.check("exports SECRET with quoting", "export SECRET=" in script, script[:300])
    # The value should be shell-quoted
    R.check("SECRET value quoted", "'it'\\''s a \"test\"'" in script, script[:300])

    print("\n── _build_bash_script: no env vars ──")
    script = _build_bash_script("pwd", [], "/tmp/cmd/y/pid", "/tmp/cmd/y/log")
    R.check("no export lines", "export FOO" not in script, "should have no user exports")
    R.check("still has CI=true", "CI=true" in script, "missing CI env")


async def test_format_bash_result(R: Results):
    from lathe import _format_bash_result, _MAX_BYTES

    print("\n── _format_bash_result: successful command ──")
    result = _format_bash_result("hello world", 0, False, {})
    R.check("returns output", result == "hello world", result)

    print("\n── _format_bash_result: non-zero exit code ──")
    result = _format_bash_result("some error", 1, False, {})
    R.check("prepends exit code", result.startswith("Exit code: 1\n"), result[:30])
    R.check("includes output", "some error" in result, result)

    print("\n── _format_bash_result: empty output ──")
    result = _format_bash_result("", 0, False, {})
    R.check("empty becomes (no output)", result == "(no output)", result)

    result = _format_bash_result("   \n  ", 0, False, {})
    R.check("whitespace-only becomes (no output)", result == "(no output)", result)

    print("\n── _format_bash_result: truncated with spill path ──")
    meta = {
        "total_lines": 5000,
        "total_bytes": 200000,
        "shown_start_line": 3001,
        "shown_end_line": 5000,
        "truncated_by": "lines",
    }
    result = _format_bash_result("tail content", 0, True, meta, spill_path="/tmp/cmd/abc/log")
    R.check("has truncation notice", "[Showing lines" in result, result[-100:])
    R.check("mentions spill path", "/tmp/cmd/abc/log" in result, result[-100:])

    print("\n── _format_bash_result: truncated by bytes ──")
    meta["truncated_by"] = "bytes"
    result = _format_bash_result("tail content", 0, True, meta, spill_path="/tmp/cmd/abc/log")
    R.check("mentions byte limit", "limit" in result, result[-150:])

    print("\n── _format_bash_result: backgrounded command ──")
    result = _format_bash_result("partial output", None, False, {},
                                 background_info={"elapsed": 30, "cmd_id": "abc-123"})
    R.check("has backgrounded notice", "Backgrounded after 30s" in result, result)
    R.check("has CMD id", "CMD=abc-123" in result, result)
    R.check("has sidecar refs", "/tmp/cmd/$CMD/" in result, result)
    R.check("mentions manpage", "lathe(manpage=" in result, result)

    print("\n── _format_bash_result: backgrounded with empty output ──")
    result = _format_bash_result("", None, False, {},
                                 background_info={"elapsed": 5, "cmd_id": "xyz"})
    R.check("empty becomes (no output yet)", "(no output yet)" in result, result[:30])

    print("\n── _format_bash_result: exit code 0 not shown ──")
    result = _format_bash_result("ok", 0, False, {})
    R.check("exit code 0 not prepended", not result.startswith("Exit code:"), result[:20])


async def test_core_read_mock(R: Results):
    """Test _core_read with a mock httpx client."""
    from lathe import _core_read
    import io as _io

    class FakeValves:
        daytona_api_key = "fake"
        daytona_api_url = "https://fake.api"
        daytona_proxy_url = "https://fake.proxy"

    class FakeResponse:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text
        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    class FakeClient:
        def __init__(self, responses):
            self._responses = responses
            self._call_idx = 0
        async def get(self, url, **kwargs):
            resp = self._responses[self._call_idx]
            self._call_idx += 1
            return resp

    print("\n── _core_read: relative path rejected ──")
    result = await _core_read(FakeValves(), "sb-id", FakeClient([]),
                              path="relative/path.py")
    R.check("rejects relative path", "absolute path" in result, result[:80])

    print("\n── _core_read: 404 handling ──")
    client = FakeClient([FakeResponse(404)])
    result = await _core_read(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace/missing.py")
    R.check("reports file not found", "File not found" in result, result)

    print("\n── _core_read: 400 handling (directory) ──")
    client = FakeClient([FakeResponse(400)])
    result = await _core_read(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace")
    R.check("reports bad request", "Bad request" in result, result)
    R.check("suggests directory", "directory" in result, result)

    print("\n── _core_read: normal file ──")
    content = "line1\nline2\nline3\n"
    client = FakeClient([FakeResponse(200, content)])
    result = await _core_read(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace/test.py")
    R.check("has file header", "File: /home/daytona/workspace/test.py" in result, result[:80])
    R.check("has total lines", "3 lines total" in result, result[:80])
    R.check("has line numbers", "1: line1" in result, result)
    R.check("has line 2", "2: line2" in result, result)
    R.check("has line 3", "3: line3" in result, result)

    print("\n── _core_read: offset and limit ──")
    content = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    client = FakeClient([FakeResponse(200, content)])
    result = await _core_read(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace/big.py",
                              offset=3, limit=2)
    R.check("shows correct range", "showing lines 3-4" in result, result[:100])
    R.check("starts at line 3", "3: line3" in result, result)
    R.check("has line 4", "4: line4" in result, result)
    R.check("no line 5", "5: line5" not in result, result)


async def test_core_write_mock(R: Results):
    """Test _core_write with a mock httpx client."""
    from lathe import _core_write

    class FakeValves:
        daytona_api_key = "fake"
        daytona_api_url = "https://fake.api"
        daytona_proxy_url = "https://fake.proxy"

    class FakeResponse:
        def __init__(self, status_code=200):
            self.status_code = status_code
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self):
            self.calls = []
        async def post(self, url, **kwargs):
            self.calls.append({"url": url, "kwargs": kwargs})
            return FakeResponse()

    print("\n── _core_write: relative path rejected ──")
    result = await _core_write(FakeValves(), "sb-id", FakeClient(),
                               path="relative.py", content="hello")
    R.check("rejects relative path", "absolute path" in result, result[:80])

    print("\n── _core_write: creates parent and uploads ──")
    client = FakeClient()
    result = await _core_write(FakeValves(), "sb-id", client,
                               path="/home/daytona/workspace/sub/file.py",
                               content="hello\nworld\n")
    R.check("reports bytes", "12 bytes" in result, result)
    R.check("reports lines", "2 lines" in result, result)
    R.check("reports path", "/home/daytona/workspace/sub/file.py" in result, result)
    R.check("made 2 calls (mkdir + upload)", len(client.calls) == 2,
            f"got {len(client.calls)} calls")
    # First call should be folder creation
    R.check("first call is folder", "/files/folder" in client.calls[0]["url"],
            client.calls[0]["url"])
    R.check("second call is upload", "/files/upload" in client.calls[1]["url"],
            client.calls[1]["url"])


async def test_core_edit_mock(R: Results):
    """Test _core_edit with a mock httpx client."""
    from lathe import _core_edit

    class FakeValves:
        daytona_api_key = "fake"
        daytona_api_url = "https://fake.api"
        daytona_proxy_url = "https://fake.proxy"

    class FakeResponse:
        def __init__(self, status_code=200, text=""):
            self.status_code = status_code
            self.text = text
        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, get_response):
            self._get_response = get_response
            self.post_calls = []
        async def get(self, url, **kwargs):
            return self._get_response
        async def post(self, url, **kwargs):
            self.post_calls.append({"url": url, "kwargs": kwargs})
            return FakeResponse()

    print("\n── _core_edit: relative path rejected ──")
    result = await _core_edit(FakeValves(), "sb-id",
                              FakeClient(FakeResponse(200, "")),
                              path="rel.py", old_string="a", new_string="b")
    R.check("rejects relative path", "absolute path" in result, result[:80])

    print("\n── _core_edit: file not found ──")
    result = await _core_edit(FakeValves(), "sb-id",
                              FakeClient(FakeResponse(404)),
                              path="/home/daytona/workspace/x.py",
                              old_string="a", new_string="b")
    R.check("reports not found", "File not found" in result, result)

    print("\n── _core_edit: no match ──")
    result = await _core_edit(FakeValves(), "sb-id",
                              FakeClient(FakeResponse(200, "hello world")),
                              path="/home/daytona/workspace/x.py",
                              old_string="ZZZNOMATCH", new_string="b")
    R.check("reports no match", "not found" in result, result)

    print("\n── _core_edit: ambiguous match ──")
    result = await _core_edit(FakeValves(), "sb-id",
                              FakeClient(FakeResponse(200, "foo foo foo")),
                              path="/home/daytona/workspace/x.py",
                              old_string="foo", new_string="bar")
    R.check("reports multiple matches", "3 matches" in result, result)
    R.check("suggests replace_all", "replace_all" in result, result)

    print("\n── _core_edit: single match succeeds ──")
    client = FakeClient(FakeResponse(200, "hello world"))
    result = await _core_edit(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace/x.py",
                              old_string="hello", new_string="goodbye")
    R.check("reports success", "Replaced 1 occurrence" in result, result)
    R.check("uploaded new content", len(client.post_calls) == 1,
            f"got {len(client.post_calls)} calls")

    print("\n── _core_edit: replace_all ──")
    client = FakeClient(FakeResponse(200, "foo bar foo"))
    result = await _core_edit(FakeValves(), "sb-id", client,
                              path="/home/daytona/workspace/x.py",
                              old_string="foo", new_string="baz",
                              replace_all=True)
    R.check("reports 2 replacements", "2 occurrence" in result, result)


async def test_delegate_bash_foreground(R: Results):
    """Test that delegate bash uses a shorter foreground timeout."""
    from lathe import _DELEGATE_BASH_FOREGROUND_SECONDS

    print("\n── delegate bash foreground default ──")
    R.check("shorter than 30s", _DELEGATE_BASH_FOREGROUND_SECONDS < 30,
            f"got {_DELEGATE_BASH_FOREGROUND_SECONDS}")
    R.check("at least 5s", _DELEGATE_BASH_FOREGROUND_SECONDS >= 5,
            f"got {_DELEGATE_BASH_FOREGROUND_SECONDS}")


async def test_format_delegate_background(R: Results):
    """Test the background delegate descriptor formatting."""
    from lathe import _format_delegate_background

    print("\n── _format_delegate_background: basic structure ──")
    result = _format_delegate_background("abc-123", 30, "Sub-agent thinking... (1/10)")
    R.check("has backgrounded notice", "Backgrounded after 30s" in result, result[:100])
    R.check("has DELEGATE id", "DELEGATE=abc-123" in result, result)
    R.check("has sidecar refs", "/tmp/delegate/$DELEGATE/" in result, result)
    R.check("has log preview", "Sub-agent thinking" in result, result)
    R.check("mentions manpage", "lathe(manpage=" in result, result)

    print("\n── _format_delegate_background: empty preview ──")
    result = _format_delegate_background("xyz-789", 5, "")
    R.check("empty becomes no progress yet", "(no progress yet)" in result, result[:50])
    R.check("still has DELEGATE", "DELEGATE=xyz-789" in result, result)

    print("\n── _format_delegate_background: whitespace-only preview ──")
    result = _format_delegate_background("qqq", 10, "   \n  ")
    R.check("whitespace becomes no progress yet", "(no progress yet)" in result, result[:50])


async def test_delegate_foreground_constant(R: Results):
    """Test the delegate foreground timeout constant."""
    from lathe import _DELEGATE_FOREGROUND_SECONDS

    print("\n── delegate foreground constant ──")
    R.check("is positive int",
            isinstance(_DELEGATE_FOREGROUND_SECONDS, int) and _DELEGATE_FOREGROUND_SECONDS > 0,
            f"got {_DELEGATE_FOREGROUND_SECONDS}")
    R.check("reasonable default (15-60s)",
            15 <= _DELEGATE_FOREGROUND_SECONDS <= 60,
            f"got {_DELEGATE_FOREGROUND_SECONDS}")


async def test_delegate_catalog_foreground_param(R: Results):
    """Test that delegate's foreground_seconds param appears in the tool catalog."""
    from lathe import _build_tool_catalog, Tools

    print("\n── delegate catalog: foreground_seconds param ──")
    tools = Tools()
    catalog = _build_tool_catalog(tools)
    delegate_line = [l for l in catalog.split("\n") if "delegate(" in l]
    R.check("delegate line exists", len(delegate_line) == 1,
            f"got {len(delegate_line)} lines")
    if delegate_line:
        R.check("delegate shows foreground_seconds param",
                "foreground_seconds" in delegate_line[0],
                delegate_line[0])


async def test_delegate_background_branching(R: Results):
    """Test the foreground/background asyncio branching pattern used by delegate().

    This exercises the exact same pattern as delegate(): ensure_future +
    wait_for(shield(event.wait())) + mutable emit flag.
    """
    from lathe import _DELEGATE_FOREGROUND_SECONDS

    # ── Helper: simulates _run_agent with configurable delay ─────────
    async def fake_agent(delay: float, done_event: asyncio.Event,
                         result: dict, emit_flag: list):
        """Mimics _run_agent: waits, writes result, sets event."""
        try:
            await asyncio.sleep(delay)
            result.update({"ok": True, "output": "done", "step_count": 1})
        except asyncio.CancelledError:
            result.update({"ok": False, "error": "cancelled"})
        finally:
            done_event.set()

    # ── Path 1: task finishes within foreground window ────────────────
    print("\n── delegate branching: foreground completion ──")
    done = asyncio.Event()
    result: dict = {}
    emit_flag = [True]

    task = asyncio.ensure_future(fake_agent(0.01, done, result, emit_flag))
    try:
        await asyncio.wait_for(asyncio.shield(done.wait()), timeout=1.0)
    except asyncio.TimeoutError:
        pass

    R.check("fg: event is set", done.is_set(), f"done={done.is_set()}")
    R.check("fg: result is ok", result.get("ok") is True, str(result))
    R.check("fg: emit still True", emit_flag[0] is True, str(emit_flag))
    await task  # clean up

    # ── Path 2: task exceeds foreground window → backgrounded ────────
    print("\n── delegate branching: auto-background ──")
    done = asyncio.Event()
    result = {}
    emit_flag = [True]

    task = asyncio.ensure_future(fake_agent(5.0, done, result, emit_flag))
    try:
        await asyncio.wait_for(asyncio.shield(done.wait()), timeout=0.05)
    except asyncio.TimeoutError:
        pass

    R.check("bg: event NOT set", not done.is_set(), f"done={done.is_set()}")
    R.check("bg: result still empty", len(result) == 0, str(result))

    # Simulate what delegate does: flip the emit flag
    emit_flag[0] = False
    R.check("bg: emit flag flipped", emit_flag[0] is False, str(emit_flag))

    # The background task should still be running and eventually complete
    await asyncio.wait_for(done.wait(), timeout=10.0)
    R.check("bg: task eventually completes", done.is_set(), f"done={done.is_set()}")
    R.check("bg: result populated", result.get("ok") is True, str(result))
    await task  # clean up

    # ── Path 3: immediate background (fg_seconds=0 → timeout=0.01) ───
    print("\n── delegate branching: immediate background ──")
    done = asyncio.Event()
    result = {}
    emit_flag = [True]

    task = asyncio.ensure_future(fake_agent(0.5, done, result, emit_flag))
    try:
        await asyncio.wait_for(asyncio.shield(done.wait()), timeout=0.01)
    except asyncio.TimeoutError:
        pass

    R.check("imm: event NOT set immediately", not done.is_set(),
            f"done={done.is_set()}")
    emit_flag[0] = False

    await asyncio.wait_for(done.wait(), timeout=5.0)
    R.check("imm: task completes after background", done.is_set(),
            f"done={done.is_set()}")
    R.check("imm: result ok", result.get("ok") is True, str(result))
    await task  # clean up

    # ── Verify shield prevents cancellation ──────────────────────────
    print("\n── delegate branching: shield prevents cancel ──")
    done = asyncio.Event()
    result = {}

    task = asyncio.ensure_future(fake_agent(0.5, done, result, [True]))
    try:
        await asyncio.wait_for(asyncio.shield(done.wait()), timeout=0.01)
    except asyncio.TimeoutError:
        pass

    # The key invariant: the background task was NOT cancelled by wait_for
    R.check("shield: task not cancelled", not task.cancelled(),
            f"cancelled={task.cancelled()}")
    await asyncio.wait_for(done.wait(), timeout=5.0)
    R.check("shield: task completed ok", result.get("ok") is True, str(result))
    await task


# ── Test registry and runner ─────────────────────────────────────────

async def test_handoff(R: Results):
    from lathe import _HANDOFF_INSTRUCTIONS, _DELEGATE_WITHHELD, _build_tool_catalog, Tools

    print("\n── handoff: instructions content ──")
    R.check("instructions non-empty",
            len(_HANDOFF_INSTRUCTIONS) > 200,
            f"length={len(_HANDOFF_INSTRUCTIONS)}")
    R.check("instructions mention no tool calls",
            "do not make any tool calls" in _HANDOFF_INSTRUCTIONS.lower(),
            "should enforce no-tools rule")
    R.check("instructions mention horizontal rule",
            "---" in _HANDOFF_INSTRUCTIONS,
            "should tell agent to write a separator")
    R.check("instructions mention user instruction above line",
            "user instruction" in _HANDOFF_INSTRUCTIONS.lower(),
            "should describe the user-facing instruction")
    R.check("instructions mention user's chat style",
            "evident" in _HANDOFF_INSTRUCTIONS.lower()
            and "language" in _HANDOFF_INSTRUCTIONS.lower(),
            "should instruct to match the user's evident chat style and language")
    R.check("instructions mention Goal section",
            "### Goal" in _HANDOFF_INSTRUCTIONS,
            "should have Goal section template")
    R.check("instructions mention Accomplished section",
            "### Accomplished" in _HANDOFF_INSTRUCTIONS,
            "should have Accomplished section template")
    R.check("instructions mention Unresolved section",
            "### Unresolved" in _HANDOFF_INSTRUCTIONS,
            "should have Unresolved section template")
    R.check("instructions mention Key files section",
            "### Key files" in _HANDOFF_INSTRUCTIONS,
            "should have Key files section template")
    R.check("instructions mention What didn't work",
            "### What didn" in _HANDOFF_INSTRUCTIONS,
            "should have What didn't work section template")
    R.check("instructions mention verbatim quotes",
            "verbatim" in _HANDOFF_INSTRUCTIONS.lower(),
            "should encourage verbatim quotes to prevent drift")
    R.check("instructions mention sandbox persists",
            "persist" in _HANDOFF_INSTRUCTIONS.lower(),
            "should note that sandbox survives across conversations")
    R.check("instructions mention no-continue rule",
            "delete" in _HANDOFF_INSTRUCTIONS.lower()
            and "handoff" in _HANDOFF_INSTRUCTIONS.lower(),
            "should tell agent to resist continuing after handoff")

    print("\n── handoff: withheld from delegate ──")
    R.check("handoff withheld from delegate",
            "handoff" in _DELEGATE_WITHHELD,
            f"should be in withheld set: {_DELEGATE_WITHHELD}")

    print("\n── handoff: tool catalog ──")
    tools = Tools()
    catalog = _build_tool_catalog(tools)
    R.check("handoff in tool catalog",
            "handoff(" in catalog,
            "handoff should appear in tool catalog")

    print("\n── handoff: manpage index ──")
    R.check("handoff in manpage index",
            "handoff" in tools._MANPAGE_INDEX,
            "handoff should have a manpage index entry")
    R.check("handoff manpage exists",
            "handoff" in tools._MANPAGES,
            "handoff should have a manpage")


async def test_harness_messages(R: Results):
    from lathe import _prepend_harness_messages, _drain_harness_messages
    from cachetools import LRUCache

    print("\n── _prepend_harness_messages: empty list ──")
    R.check("no messages = passthrough",
            _prepend_harness_messages("hello", []) == "hello")

    print("\n── _prepend_harness_messages: single message ──")
    result = _prepend_harness_messages("tool output", ["[warning]"])
    R.check("warning prepended", result.startswith("[warning]"), result[:50])
    R.check("tool output follows", "tool output" in result, result)

    print("\n── _prepend_harness_messages: multiple messages ──")
    result = _prepend_harness_messages("output", ["msg1", "msg2", "msg3"])
    R.check("all messages present", "msg1" in result and "msg2" in result and "msg3" in result, result)
    R.check("output at end", result.endswith("output"), result[-20:])
    R.check("messages separated by blank lines", "\n\n" in result)

    print("\n── _drain_harness_messages: no chat_id ──")
    cache = LRUCache(maxsize=10)
    messages = _drain_harness_messages(cache, None, None)
    R.check("no chat_id = empty", messages == [], str(messages))

    messages = _drain_harness_messages(cache, None, "[restarted]")
    R.check("no chat_id + warning = just warning", messages == ["[restarted]"], str(messages))

    print("\n── _drain_harness_messages: chat_id with pending ──")
    cache["chat-1"] = {"init": True, "pending": ["snapshot data", "job done"]}
    messages = _drain_harness_messages(cache, "chat-1", "[restarted]")
    R.check("drains warning + pending",
            messages == ["[restarted]", "snapshot data", "job done"], str(messages))
    R.check("pending cleared after drain",
            cache["chat-1"]["pending"] == [], str(cache["chat-1"]))

    print("\n── _drain_harness_messages: second drain is empty ──")
    messages = _drain_harness_messages(cache, "chat-1", None)
    R.check("second drain empty", messages == [], str(messages))

    print("\n── _drain_harness_messages: unknown chat_id ──")
    messages = _drain_harness_messages(cache, "chat-unknown", None)
    R.check("unknown chat = empty", messages == [], str(messages))


async def test_chat_state(R: Results):
    from lathe import Tools
    from cachetools import LRUCache

    print("\n── Tools._chat_state: exists on init ──")
    tools = Tools()
    R.check("_chat_state exists", hasattr(tools, "_chat_state"))
    R.check("_chat_state is LRUCache", isinstance(tools._chat_state, LRUCache))
    R.check("_chat_state maxsize is 1024", tools._chat_state.maxsize == 1024,
            f"got {tools._chat_state.maxsize}")
    R.check("_chat_state starts empty", len(tools._chat_state) == 0)


async def test_snapshot_script(R: Results):
    from lathe import _SNAPSHOT_SCRIPT

    print("\n── _SNAPSHOT_SCRIPT: generates valid Python ──")
    try:
        compile(_SNAPSHOT_SCRIPT, "<snapshot>", "exec")
        R.check("script compiles", True)
    except SyntaxError as e:
        R.check("script compiles", False, str(e))

    R.check("script references workspace",
            "/home/daytona/workspace" in _SNAPSHOT_SCRIPT)
    R.check("script uses os.listdir",
            "os.listdir" in _SNAPSHOT_SCRIPT)
    R.check("script checks uname",
            "uname" in _SNAPSHOT_SCRIPT)
    R.check("script checks python3",
            "python3" in _SNAPSHOT_SCRIPT)
    R.check("script checks node",
            "node" in _SNAPSHOT_SCRIPT)


async def test_ensure_chat_init(R: Results):
    from lathe import _ensure_chat_init
    from cachetools import LRUCache

    print("\n── _ensure_chat_init: no chat_id is no-op ──")
    cache = LRUCache(maxsize=10)
    # Should not raise, should not modify cache
    await _ensure_chat_init(None, "sb-id", None, cache, None, {})
    R.check("empty chat_id = no-op", len(cache) == 0)
    await _ensure_chat_init(None, "sb-id", None, cache, "", {})
    R.check("blank chat_id = no-op", len(cache) == 0)

    print("\n── _ensure_chat_init: second call is no-op ──")
    # Pre-populate as if already initialized
    cache["chat-already"] = {"init": True, "pending": ["existing"]}
    await _ensure_chat_init(None, "sb-id", None, cache, "chat-already", {})
    R.check("existing pending preserved",
            cache["chat-already"]["pending"] == ["existing"],
            str(cache["chat-already"]))

    print("\n── _ensure_chat_init: new chat creates state ──")
    # For a truly new chat with no valves/client, auto-init will fail
    # gracefully (best-effort) but should still create the state entry.
    await _ensure_chat_init(None, "sb-id", None, cache, "chat-new", {})
    R.check("new chat entry created", "chat-new" in cache)
    R.check("new chat marked init", cache["chat-new"].get("init") is True)
    R.check("new chat has pending list",
            isinstance(cache["chat-new"].get("pending"), list))


async def test_chat_id_in_signatures(R: Results):
    """Verify all sandbox-using Tools methods accept __chat_id__."""
    import inspect
    from lathe import Tools

    print("\n── __chat_id__ in tool signatures ──")
    tools = Tools()
    # Tools that use _ensure_sandbox should have __chat_id__
    sandbox_tools = ["bash", "read", "write", "edit", "glob", "grep",
                     "onboard", "delegate", "expose"]
    for name in sandbox_tools:
        method = getattr(tools, name, None)
        if method is None:
            R.check(f"{name} exists", False, "method not found")
            continue
        sig = inspect.signature(method)
        R.check(f"{name} has __chat_id__",
                "__chat_id__" in sig.parameters,
                f"params: {list(sig.parameters.keys())}")

    # Tools that don't use _ensure_sandbox should NOT have __chat_id__
    # (or it's fine if they do, but let's check they still work)
    non_sandbox = ["lathe", "handoff"]
    for name in non_sandbox:
        method = getattr(tools, name, None)
        if method is None:
            R.check(f"{name} exists", False, "method not found")


async def test_push_bg_notice(R: Results):
    from lathe import _push_bg_notice
    from cachetools import LRUCache

    print("\n── _push_bg_notice: pushes to existing chat ──")
    cache = LRUCache(maxsize=10)
    cache["chat-1"] = {"init": True, "pending": ["existing"]}
    _push_bg_notice(cache, "chat-1", "job done")
    R.check("notice appended", cache["chat-1"]["pending"] == ["existing", "job done"],
            str(cache["chat-1"]["pending"]))

    print("\n── _push_bg_notice: no chat_id is no-op ──")
    _push_bg_notice(cache, None, "notice")
    _push_bg_notice(cache, "", "notice")
    R.check("None chat_id no-op", True)  # no crash = pass

    print("\n── _push_bg_notice: evicted chat_id is no-op ──")
    _push_bg_notice(cache, "chat-gone", "notice")
    R.check("evicted chat_id no-op", "chat-gone" not in cache)

    print("\n── _push_bg_notice: empty pending list ──")
    cache["chat-2"] = {"init": True, "pending": []}
    _push_bg_notice(cache, "chat-2", "first notice")
    R.check("pushed to empty pending", cache["chat-2"]["pending"] == ["first notice"],
            str(cache["chat-2"]["pending"]))

    print("\n── _push_bg_notice: missing pending key ──")
    cache["chat-3"] = {"init": True}
    _push_bg_notice(cache, "chat-3", "notice")
    R.check("created pending list", cache["chat-3"]["pending"] == ["notice"],
            str(cache["chat-3"]))


async def test_format_bg_notices(R: Results):
    from lathe import _format_bg_bash_notice, _format_bg_delegate_notice

    print("\n── _format_bg_bash_notice: success with output ──")
    result = _format_bg_bash_notice("abc-123", 0, 47, "line1\nline2\nline3")
    R.check("has CMD id", "CMD-abc-123" in result, result[:80])
    R.check("has exit code", "Exit code: 0" in result, result)
    R.check("has elapsed", "47s" in result, result)
    R.check("has output lines", "line3" in result, result)

    print("\n── _format_bg_bash_notice: failure ──")
    result = _format_bg_bash_notice("xyz", 1, 10, "error: bad thing")
    R.check("has exit code 1", "Exit code: 1" in result, result)
    R.check("has error output", "bad thing" in result, result)

    print("\n── _format_bg_bash_notice: empty output ──")
    result = _format_bg_bash_notice("qqq", 0, 5, "")
    R.check("has no output marker", "(no output)" in result, result)

    print("\n── _format_bg_bash_notice: unknown exit code ──")
    result = _format_bg_bash_notice("rrr", None, 3, "stuff")
    R.check("has unknown exit code", "unknown" in result, result)

    print("\n── _format_bg_bash_notice: long output truncated to 5 lines ──")
    long_output = "\n".join(f"line {i}" for i in range(20))
    result = _format_bg_bash_notice("sss", 0, 30, long_output)
    R.check("has last line", "line 19" in result, result)
    R.check("no early line", "line 10" not in result, result)

    print("\n── _format_bg_delegate_notice: success ──")
    result = _format_bg_delegate_notice("del-123", 95, 12, 34, "task completed", None)
    R.check("has DELEGATE id", "DELEGATE-del-123" in result, result[:80])
    R.check("has steps", "12 step(s)" in result, result)
    R.check("has tool calls", "34 tool call(s)" in result, result)
    R.check("has elapsed", "95s" in result, result)
    R.check("has preview", "task completed" in result, result)
    R.check("says completed", "completed" in result.lower(), result[:80])

    print("\n── _format_bg_delegate_notice: failure ──")
    result = _format_bg_delegate_notice("del-456", 30, 3, 5, "", "timeout error")
    R.check("has DELEGATE id", "DELEGATE-del-456" in result, result[:80])
    R.check("says failed", "failed" in result.lower(), result[:80])
    R.check("has error", "timeout error" in result, result)

    print("\n── _format_bg_delegate_notice: empty preview ──")
    result = _format_bg_delegate_notice("del-789", 10, 1, 2, "", None)
    R.check("has no result marker", "(no result text)" in result, result)


TESTS = {
    "parse_env_vars": test_parse_env_vars,
    "onboard_script": test_onboard_script,
    "truncate": test_truncate,
    "shell_quote": test_shell_quote,
    "require_abs_path": test_require_abs_path,
    "build_tool_catalog": test_build_tool_catalog,
    "glob_script": test_glob_script,
    "grep_script": test_grep_script,
    "delegate_infrastructure": test_delegate_infrastructure,
    "delegate_prompt_build": test_delegate_prompt_build,
    "delegate_tools_build": test_delegate_tools_build,
    "build_bash_script": test_build_bash_script,
    "format_bash_result": test_format_bash_result,
    "core_read_mock": test_core_read_mock,
    "core_write_mock": test_core_write_mock,
    "core_edit_mock": test_core_edit_mock,
    "delegate_bash_foreground": test_delegate_bash_foreground,
    "format_delegate_background": test_format_delegate_background,
    "delegate_foreground_constant": test_delegate_foreground_constant,
    "delegate_catalog_foreground_param": test_delegate_catalog_foreground_param,
    "delegate_background_branching": test_delegate_background_branching,
    "handoff": test_handoff,
    "harness_messages": test_harness_messages,
    "chat_state": test_chat_state,
    "snapshot_script": test_snapshot_script,
    "ensure_chat_init": test_ensure_chat_init,
    "chat_id_signatures": test_chat_id_in_signatures,
    "push_bg_notice": test_push_bg_notice,
    "format_bg_notices": test_format_bg_notices,
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

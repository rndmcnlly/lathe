#!/usr/bin/env python3
"""
Test harness for lathe.py toolkit.
Exercises all tools against the live sandbox provider API.

Usage:
    uv run --script test_harness.py                # run all tests
    uv run --script test_harness.py unit           # unit tests only (no sandbox)
    uv run --script test_harness.py bash edit      # specific groups only
    uv run --script test_harness.py --list         # list available groups

TODO: The tool docstrings are executable promptware — they are the interface
contract between the toolkit and the model. A misleading docstring is a bug
that no amount of integration testing catches, because these tests call the
Python methods directly with correct arguments. What's needed is an eval
harness where an agent that has *not* read the source code is given only the
tool schemas (names, docstrings, param descriptions) and asked to accomplish
tasks (e.g. "start a web server and get an expose URL"). Score on: does
it background the server, does it pick the right port, does it avoid known
pitfalls. This tests the docstrings as prompts, not the code as code.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pydantic", "python-dotenv", "beautifulsoup4", "markdownify"]
# ///

import asyncio
import sys
import os
import time

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from lathe import Tools

API_KEY = os.environ.get("DAYTONA_API_KEY")
TEST_EMAIL = "test-harness@daytona-owui-test"


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


async def mock_emitter(event):
    data = event.get("data", {})
    desc = data.get("description", "")
    done = data.get("done", False)
    marker = "✓" if done else "…"
    print(f"  [{marker}] {desc}")


# ── Unit tests (no sandbox needed) ──────────────────────────────────

async def test_unit_parse_env_vars(R: Results):
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


async def test_unit_onboard_script(R: Results):
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


async def test_unit_truncate(R: Results):
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


# ── Integration tests (need sandbox) ────────────────────────────────

async def test_int_bash(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    # These are all independent — run concurrently
    async def t_simple():
        print("\n── bash: simple command ──")
        r = await tools.bash("echo hello world", **ctx)
        R.check("echo returns output", "hello world" in r, r[:200])

    async def t_compound():
        print("\n── bash: compound command ──")
        r = await tools.bash("echo one && echo two && echo three", **ctx)
        R.check("compound && works", "one" in r and "two" in r and "three" in r, r[:200])

    async def t_pipes():
        print("\n── bash: pipes ──")
        r = await tools.bash("echo 'hello world' | wc -w", **ctx)
        R.check("pipe works", "2" in r, r[:200])

    async def t_exit():
        print("\n── bash: exit code ──")
        r = await tools.bash("exit 42", **ctx)
        R.check("non-zero exit reported", "Exit code: 42" in r, r[:200])

    async def t_workdir():
        print("\n── bash: working directory ──")
        r = await tools.bash("pwd", **ctx)
        R.check("default cwd is /home/daytona/workspace", "/home/daytona/workspace" in r, r[:200])
        r = await tools.bash("pwd", workdir="/tmp", **ctx)
        R.check("custom cwd works", "/tmp" in r, r[:200])

    async def t_quoting():
        print("\n── bash: quoted flag values ──")
        r = await tools.bash('echo "--state" "open" | cat', **ctx)
        R.check("quoted flag values pass through", "--state" in r and "open" in r, r[:200])

        print("\n── bash: single quotes ──")
        r = await tools.bash("echo 'hello world'", **ctx)
        R.check("single quotes work", "hello world" in r, r[:200])

        print("\n── bash: mixed quoting ──")
        r = await tools.bash("""echo "it's a 'test'" && echo 'say "hello"'""", **ctx)
        R.check("mixed quotes work", "it's a 'test'" in r and 'say "hello"' in r, r[:200])

        print("\n── bash: backslashes ──")
        r = await tools.bash(r"echo 'back\slash'", **ctx)
        R.check("backslashes preserved", "back\\slash" in r, r[:200])

    async def t_vars():
        print("\n── bash: dollar signs and variables ──")
        r = await tools.bash('FOO=bar && echo "val=$FOO"', **ctx)
        R.check("variable expansion works", "val=bar" in r, r[:200])

    async def t_set_e():
        print("\n── bash: set -e aborts on error ──")
        r = await tools.bash("false\necho should-not-reach", **ctx)
        R.check("set -e aborts on first failure", "Exit code:" in r, r[:200])
        R.check("second command did not run", "should-not-reach" not in r, r[:200])

        print("\n── bash: pipefail catches pipe errors ──")
        r = await tools.bash("false | cat", **ctx)
        R.check("pipefail reports failure", "Exit code:" in r, r[:200])

        print("\n── bash: || true overrides set -e ──")
        r = await tools.bash("false || true\necho survived", **ctx)
        R.check("|| true suppresses abort", "survived" in r, r[:200])

    # First call creates sandbox; after that everything can fan out
    await t_simple()
    await asyncio.gather(t_compound(), t_pipes(), t_exit(), t_workdir(), t_quoting(), t_vars(), t_set_e())


async def test_int_write_read_edit(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    print("\n── write: create file ──")
    test_content = "line one\nline two\nline three\n"
    r = await tools.write("/home/daytona/workspace/test_file.txt", test_content, **ctx)
    R.check("write reports success", "Wrote" in r and "test_file.txt" in r, r[:200])

    print("\n── read: full file ──")
    r = await tools.read("/home/daytona/workspace/test_file.txt", **ctx)
    R.check("read returns content", "line one" in r and "line two" in r, r[:200])
    R.check("read has line numbers", "1: line one" in r, r[:200])
    R.check("read shows total lines", "3 lines total" in r, r[:200])

    # Independent reads
    async def t_offset():
        print("\n── read: offset and limit ──")
        r = await tools.read("/home/daytona/workspace/test_file.txt", offset=2, limit=1, **ctx)
        R.check("offset/limit works", "2: line two" in r, r[:200])
        R.check("respects limit", "line three" not in r, r[:200])

    async def t_notfound():
        print("\n── read: file not found ──")
        r = await tools.read("/home/daytona/workspace/nonexistent.txt", **ctx)
        R.check("reports file not found", "Error" in r or "not found" in r.lower(), r[:200])

    await asyncio.gather(t_offset(), t_notfound())

    print("\n── edit: single replacement ──")
    r = await tools.edit("/home/daytona/workspace/test_file.txt", "line two", "LINE TWO EDITED", **ctx)
    R.check("edit reports success", "Replaced 1" in r, r[:200])

    r = await tools.read("/home/daytona/workspace/test_file.txt", **ctx)
    R.check("edit persisted", "LINE TWO EDITED" in r, r[:200])
    R.check("other lines untouched", "line one" in r and "line three" in r, r[:200])

    # Independent edit tests
    async def t_edit_notfound():
        print("\n── edit: old_string not found ──")
        r = await tools.edit("/home/daytona/workspace/test_file.txt", "this text does not exist", "replacement", **ctx)
        R.check("reports not found", "not found" in r.lower(), r[:200])

    async def t_edit_multi():
        print("\n── edit: multiple matches without replace_all ──")
        await tools.write("/home/daytona/workspace/dup_test.txt", "aaa\nbbb\naaa\nbbb\naaa\n", **ctx)
        r = await tools.edit("/home/daytona/workspace/dup_test.txt", "aaa", "zzz", **ctx)
        R.check("rejects ambiguous edit", "3 matches" in r or "multiple" in r.lower(), r[:200])

        print("\n── edit: replace_all ──")
        r = await tools.edit("/home/daytona/workspace/dup_test.txt", "aaa", "zzz", replace_all=True, **ctx)
        R.check("replace_all reports count", "Replaced 3" in r, r[:200])
        r = await tools.read("/home/daytona/workspace/dup_test.txt", **ctx)
        R.check("replace_all applied", "aaa" not in r and "zzz" in r, r[:200])

    async def t_edit_missing():
        print("\n── edit: file not found ──")
        r = await tools.edit("/home/daytona/workspace/nonexistent.txt", "foo", "bar", **ctx)
        R.check("edit on missing file errors", "Error" in r or "not found" in r.lower(), r[:200])

    async def t_nested():
        print("\n── write: nested path ──")
        r = await tools.write("/home/daytona/workspace/deep/nested/dir/file.txt", "nested content\n", **ctx)
        R.check("write to nested path succeeds", "Wrote" in r, r[:200])
        r = await tools.read("/home/daytona/workspace/deep/nested/dir/file.txt", **ctx)
        R.check("nested file readable", "nested content" in r, r[:200])

    await asyncio.gather(t_edit_notfound(), t_edit_multi(), t_edit_missing(), t_nested())

    # cleanup
    await tools.bash("rm -rf /home/daytona/workspace/test_file.txt /home/daytona/workspace/dup_test.txt /home/daytona/workspace/deep", **ctx)


async def test_int_onboard(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    # Clean up any prior state (project dirs + global ~/.agents)
    await tools.bash("rm -rf /home/daytona/workspace/test_project /home/daytona/workspace/empty_project /home/daytona/.agents", **ctx)

    print("\n── onboard: missing context (no project, no global) ──")
    r = await tools.onboard("/home/daytona/workspace/empty_project", **ctx)
    R.check("fails without any context", "Error" in r, r[:300])
    R.check("error mentions ~/.agents/", "~/.agents/" in r, r[:300])

    print("\n── onboard: project AGENTS.md only ──")
    await tools.write("/home/daytona/workspace/test_project/AGENTS.md", "# Test Agent\nYou are a helpful test agent.\n", **ctx)
    r = await tools.onboard("/home/daytona/workspace/test_project", **ctx)
    R.check("returns AGENTS.md content", "helpful test agent" in r, r[:300])
    R.check("labeled as project context", "Project Agent Instructions" in r, r[:300])
    R.check("no skills section when none exist", "Available Skills" not in r, r[:300])

    print("\n── onboard: with project skills ──")
    await tools.write(
        "/home/daytona/workspace/test_project/.agents/skills/test-skill/SKILL.md",
        "---\nname: test-skill\ndescription: A skill for testing things.\n---\n\n# Test Skill\nDetailed instructions here.\n",
        **ctx,
    )
    r = await tools.onboard("/home/daytona/workspace/test_project", **ctx)
    R.check("returns AGENTS.md", "helpful test agent" in r, r[:500])
    R.check("lists skill name", "test-skill" in r, r[:500])
    R.check("lists skill description", "testing things" in r, r[:500])
    R.check("includes SKILL.md path", "SKILL.md" in r, r[:500])
    R.check("does NOT include skill body", "Detailed instructions here" not in r, r[:500])

    print("\n── onboard: global AGENTS.md only (no project context) ──")
    await tools.write("/home/daytona/.agents/AGENTS.md", "# Global Agent\nGlobal preferences and conventions.\n", **ctx)
    r = await tools.onboard("/home/daytona/workspace/empty_project", **ctx)
    R.check("returns global AGENTS.md", "Global preferences" in r, r[:500])
    R.check("labeled as global context", "Global Agent Instructions" in r, r[:300])

    print("\n── onboard: global + project AGENTS.md together ──")
    r = await tools.onboard("/home/daytona/workspace/test_project", **ctx)
    R.check("contains global context", "Global preferences" in r, r[:500])
    R.check("contains project context", "helpful test agent" in r, r[:500])
    R.check("global labeled", "Global Agent Instructions" in r, r[:500])
    R.check("project labeled", "Project Agent Instructions" in r, r[:500])

    print("\n── onboard: global skills ──")
    await tools.write(
        "/home/daytona/.agents/skills/global-skill/SKILL.md",
        "---\nname: global-skill\ndescription: A global reusable skill.\n---\n\n# Global Skill\nGlobal skill body.\n",
        **ctx,
    )
    r = await tools.onboard("/home/daytona/workspace/test_project", **ctx)
    R.check("lists global skill", "global-skill" in r, r[:800])
    R.check("lists project skill", "test-skill" in r, r[:800])
    R.check("global skill description shown", "global reusable skill" in r, r[:800])

    print("\n── onboard: project skill overrides global on name collision ──")
    await tools.write(
        "/home/daytona/.agents/skills/test-skill/SKILL.md",
        "---\nname: test-skill\ndescription: Global version of test-skill (should be overridden).\n---\n\nGlobal body.\n",
        **ctx,
    )
    r = await tools.onboard("/home/daytona/workspace/test_project", **ctx)
    R.check("project skill wins on collision", "A skill for testing things" in r, r[:800])
    R.check("global version overridden", "should be overridden" not in r, r[:800])
    # The path should point to the project-local skill, not the global one
    R.check("collision path is project-local", "/home/daytona/workspace/test_project/" in r, r[:800])

    print("\n── onboard: global skills only (no project) ──")
    r = await tools.onboard("/home/daytona/workspace/empty_project", **ctx)
    R.check("global-only has skills section", "Available Skills" in r, r[:800])
    R.check("global-only lists global-skill", "global-skill" in r, r[:800])
    R.check("global-only lists test-skill from global", "test-skill" in r, r[:800])

    # Clean up
    await tools.bash("rm -rf /home/daytona/workspace/test_project /home/daytona/workspace/empty_project /home/daytona/.agents", **ctx)


async def test_int_truncation(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)
    import re

    print("\n── bash: output truncation with log file ──")
    result = await tools.bash(
        "for i in $(seq 1 3000); do echo \"output line $i\"; done",
        **ctx,
    )
    R.check("truncated output has notice", "[Showing lines" in result, result[-200:])
    R.check("notice mentions log file", "/tmp/cmd/" in result, result[-200:])
    R.check("last line present in output", "output line 3000" in result, result[-200:])
    R.check("first line NOT in truncated output", "output line 1\n" not in result, "line 1 should be truncated away")

    spill_match = re.search(r"/tmp/cmd/[0-9a-f-]+/log", result)
    if spill_match:
        spill_path = spill_match.group(0)
        verify, head_result = await asyncio.gather(
            tools.bash(f"wc -l < {spill_path}", **ctx),
            tools.bash(f"head -n 3 {spill_path}", **ctx),
        )
        R.check("log file has all 3000 lines", "3000" in verify, verify.strip())
        R.check("can retrieve early lines from log file", "output line 1" in head_result, head_result[:100])
    else:
        R.check("log file path found in notice", False, "no path match found")

    print("\n── bash: small output NOT truncated ──")
    result = await tools.bash("echo hello", **ctx)
    R.check("small output has no truncation notice", "[Showing lines" not in result, result[:200])


async def test_int_bash_sessions(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)
    import re

    print("\n── bash sessions: state directory layout ──")
    # The command itself creates /tmp/cmd/<uuid>/ — so we list dirs,
    # then identify the one that contains our sentinel in its log.
    result = await tools.bash(
        "echo state-dir-sentinel-42", **ctx,
    )
    R.check("simple command returns output", "state-dir-sentinel-42" in result, result[:200])

    # Find the state dir whose log contains our sentinel
    find_result = await tools.bash(
        "grep -rl 'state-dir-sentinel-42' /tmp/cmd/*/log 2>/dev/null | head -1",
        **ctx,
    )
    sentinel_log = find_result.strip()
    sentinel_dir = sentinel_log.rsplit("/", 1)[0] if "/log" in sentinel_log else ""

    if sentinel_dir:
        dir_result = await tools.bash(
            f"ls {sentinel_dir}/", **ctx,
        )
        R.check("state dir has log file", "log" in dir_result, dir_result[:200])
        R.check("state dir has exit file", "exit" in dir_result, dir_result[:200])
        R.check("state dir has sh file", "sh" in dir_result, dir_result[:200])
        R.check("state dir has pid file", "pid" in dir_result, dir_result[:200])

        exit_result = await tools.bash(
            f"cat {sentinel_dir}/exit", **ctx,
        )
        R.check("exit file contains 0 for success", exit_result.strip() == "0", exit_result.strip())
    else:
        R.check("found sentinel state dir", False, f"grep returned: {find_result[:200]}")

    print("\n── bash sessions: non-zero exit code in state dir ──")
    fail_result = await tools.bash("echo fail-sentinel-99 && exit 7", **ctx)
    R.check("non-zero exit reported", "Exit code: 7" in fail_result, fail_result[:200])

    find_fail = await tools.bash(
        "grep -rl 'fail-sentinel-99' /tmp/cmd/*/log 2>/dev/null | head -1",
        **ctx,
    )
    fail_dir = find_fail.strip().rsplit("/", 1)[0] if "/log" in find_fail.strip() else ""
    if fail_dir:
        exit_result = await tools.bash(
            f"cat {fail_dir}/exit", **ctx,
        )
        R.check("exit file contains 7 for failure", exit_result.strip() == "7", exit_result.strip())
    else:
        R.check("found fail sentinel state dir", False, f"grep returned: {find_fail[:200]}")

    print("\n── bash sessions: log file captures output ──")
    await tools.bash("echo logtest-alpha && echo logtest-beta", **ctx)
    find_log = await tools.bash(
        "grep -rl 'logtest-alpha' /tmp/cmd/*/log 2>/dev/null | head -1",
        **ctx,
    )
    log_path = find_log.strip()
    if log_path:
        log_result = await tools.bash(f"cat {log_path}", **ctx)
        R.check("log file has first line", "logtest-alpha" in log_result, log_result[:200])
        R.check("log file has second line", "logtest-beta" in log_result, log_result[:200])
    else:
        R.check("found logtest state dir", False, f"grep returned: {find_log[:200]}")

    print("\n── bash sessions: foreground_seconds override ──")
    # A short foreground_seconds=2 with a 5s sleep should auto-background
    bg_result = await tools.bash(
        "echo bg-start && sleep 5 && echo bg-done",
        foreground_seconds=2,
        **ctx,
    )
    R.check("short timeout triggers backgrounding", "Backgrounded" in bg_result, bg_result[:300])
    R.check("background descriptor has CMD=", "CMD=" in bg_result, bg_result[:300])
    R.check("background descriptor has Ref line", "Ref /tmp/cmd/$CMD/" in bg_result, bg_result[:300])
    R.check("background descriptor has manpage pointer", 'manpage="background"' in bg_result, bg_result[:300])

    # Extract CMD uuid from background descriptor and reconstruct full path
    cmd_match = re.search(r"CMD=([0-9a-f-]{36})", bg_result)
    if cmd_match:
        bg_cmd_dir = f"/tmp/cmd/{cmd_match.group(1)}"

        # Wait for the backgrounded command to finish using the suggested pattern
        wait_result = await tools.bash(
            f"while [ ! -f {bg_cmd_dir}/exit ]; do sleep 1; done; "
            f"cat {bg_cmd_dir}/exit; echo '---'; cat {bg_cmd_dir}/log",
            foreground_seconds=30,
            **ctx,
        )
        R.check("can wait for backgrounded command", "bg-done" in wait_result, wait_result[:300])
        R.check("backgrounded command exit code is 0", "\n0\n" in wait_result or wait_result.strip().startswith("0"), wait_result[:100])
    else:
        R.check("CMD dir found in background descriptor", False, "no CMD= match")

    print("\n── bash sessions: long foreground_seconds avoids backgrounding ──")
    # A foreground_seconds=60 with a 3s sleep should NOT background
    fg_result = await tools.bash(
        "sleep 3 && echo fg-completed",
        foreground_seconds=60,
        **ctx,
    )
    R.check("long timeout avoids backgrounding", "fg-completed" in fg_result, fg_result[:200])
    R.check("no background descriptor", "Backgrounded" not in fg_result, fg_result[:200])

    # Clean up
    await tools.bash("rm -rf /tmp/cmd", **ctx)


async def test_int_env_vars(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    class FakeUserValves:
        env_vars = '{"LATHE_TEST_SECRET":"hunter2","LATHE_TEST_GREETING":"hello world"}'

    user_with_valves = {**user, "valves": FakeUserValves()}

    async def t_basic():
        print("\n── bash: UserValves env vars injected ──")
        r = await tools.bash("echo $LATHE_TEST_SECRET", __user__=user_with_valves, __event_emitter__=mock_emitter)
        R.check("secret var is available", "hunter2" in r, r[:200])
        r = await tools.bash("echo $LATHE_TEST_GREETING", __user__=user_with_valves, __event_emitter__=mock_emitter)
        R.check("greeting var with spaces works", "hello world" in r, r[:200])

    async def t_tricky():
        print("\n── bash: env vars with shell-tricky values ──")
        class TrickyUserValves:
            env_vars = '{"TRICKY":"it\'s a \\\"test\\\" with $HOME and `whoami`"}'
        u = {**user, "valves": TrickyUserValves()}
        r = await tools.bash("echo \"$TRICKY\"", __user__=u, __event_emitter__=mock_emitter)
        R.check("tricky value not expanded", "$HOME" in r and "`whoami`" in r, r[:200])
        R.check("quotes preserved in value", "\"test\"" in r, r[:200])

    async def t_empty():
        print("\n── bash: empty env vars no-op ──")
        u = {**user, "valves": type("V", (), {"env_vars": "{}"})()}
        r = await tools.bash("echo works", __user__=u, __event_emitter__=mock_emitter)
        R.check("empty env vars still runs", "works" in r, r[:200])

    async def t_no_valves():
        print("\n── bash: no valves key no-op ──")
        r = await tools.bash("echo still_works", **ctx)
        R.check("no valves key still runs", "still_works" in r, r[:200])

    await asyncio.gather(t_basic(), t_tricky(), t_empty(), t_no_valves())


async def test_int_ensure_sandbox(R: Results, tools: Tools, user: dict):
    import httpx
    import json as _json
    from lathe import _headers, _ensure_sandbox

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("\n── _ensure_sandbox: identity ──")
        sandbox_id, _warning = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
        R.check("returns a sandbox id", sandbox_id and isinstance(sandbox_id, str), repr(sandbox_id))
        sandbox_id_2, warning_2 = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
        R.check("same sandbox on repeat call", sandbox_id == sandbox_id_2, f"{sandbox_id[:12]} != {sandbox_id_2[:12]}")
        R.check("no warning when already running", warning_2 is None, repr(warning_2))

        print("\n── _ensure_sandbox: isolation ──")
        resp = await client.get(
            f"{tools.valves.daytona_api_url}/sandbox",
            params={"labels": _json.dumps({"test-harness": TEST_EMAIL})},
            headers=_headers(tools.valves),
        )
        resp.raise_for_status()
        filtered = resp.json() or []
        R.check("label filter returns exactly 1", len(filtered) == 1, f"got {len(filtered)}")
        if filtered:
            R.check("filtered sandbox has correct label",
                     filtered[0].get("labels", {}).get("test-harness") == TEST_EMAIL,
                     str(filtered[0].get("labels", {})))

        resp = await client.get(
            f"{tools.valves.daytona_api_url}/sandbox",
            params={"labels": _json.dumps({"test-harness": "stranger@example.com"})},
            headers=_headers(tools.valves),
        )
        resp.raise_for_status()
        stranger_results = resp.json() or []
        R.check("stranger sees 0 sandboxes", len(stranger_results) == 0, f"got {len(stranger_results)}")

        print("\n── _ensure_sandbox: empty deployment_label guard ──")
        saved_label = tools.valves.deployment_label
        tools.valves.deployment_label = ""
        try:
            await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
            R.check("empty label raises RuntimeError", False, "no exception raised")
        except RuntimeError as e:
            R.check("empty label raises RuntimeError", "Deployment label" in str(e), str(e))
        finally:
            tools.valves.deployment_label = saved_label

        print("\n── _ensure_sandbox: duplicate guard ──")
        resp = await client.post(
            f"{tools.valves.daytona_api_url}/sandbox",
            headers=_headers(tools.valves),
            json={
                "language": tools.valves.sandbox_language,
                "name": f"test-harness/{TEST_EMAIL}-duplicate",
                "labels": {"test-harness": TEST_EMAIL},
                "autoStopInterval": tools.valves.auto_stop_minutes,
                "autoArchiveInterval": tools.valves.auto_archive_minutes,
                "autoDeleteInterval": -1,
            },
        )
        resp.raise_for_status()
        duplicate_id = resp.json()["id"]
        print(f"  Created duplicate sandbox {duplicate_id[:12]}...")

        try:
            await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
            R.check("duplicate raises RuntimeError", False, "no exception raised")
        except RuntimeError as e:
            msg = str(e)
            R.check("duplicate raises RuntimeError", "Found 2 sandboxes" in msg, msg[:200])
            R.check("duplicate error lists sandbox ids", duplicate_id[:12] in msg, msg[:200])

        print(f"  Deleting duplicate sandbox {duplicate_id[:12]}...")
        resp = await client.delete(
            f"{tools.valves.daytona_api_url}/sandbox/{duplicate_id}",
            headers=_headers(tools.valves),
            params={"force": "true"},
        )
        resp.raise_for_status()

        for _attempt in range(15):
            await asyncio.sleep(1)
            resp = await client.get(
                f"{tools.valves.daytona_api_url}/sandbox",
                params={"labels": _json.dumps({"test-harness": TEST_EMAIL})},
                headers=_headers(tools.valves),
            )
            remaining = [
                s for s in (resp.json() or [])
                if s.get("labels", {}).get("test-harness") == TEST_EMAIL
            ]
            if len(remaining) <= 1:
                break
        print(f"  Deleted ({len(remaining)} sandbox(es) remain).")

        sandbox_id_3, _ = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
        R.check("back to normal after dup cleanup", sandbox_id_3 == sandbox_id, f"{sandbox_id_3[:12]} != {sandbox_id[:12]}")

        print("\n── _ensure_sandbox: volume is mounted ──")
        from lathe import VOLUME_MOUNT_PATH, _toolbox
        resp = await client.post(
            _toolbox(tools.valves, sandbox_id, "/process/execute"),
            headers=_headers(tools.valves),
            json={"command": f"bash -c \"grep '{VOLUME_MOUNT_PATH}' /proc/mounts && echo MOUNTED\"", "timeout": 5000},
        )
        resp.raise_for_status()
        data = resp.json()
        R.check("volume is mounted", data.get("exitCode") == 0 and "MOUNTED" in data.get("result", ""),
                 f"exitCode={data.get('exitCode')}, result={data.get('result', '')[:200]}")

        # Verify volume is writable
        resp = await client.post(
            _toolbox(tools.valves, sandbox_id, "/process/execute"),
            headers=_headers(tools.valves),
            json={"command": f"echo vol_test > {VOLUME_MOUNT_PATH}/_ensure_sandbox_test.txt && cat {VOLUME_MOUNT_PATH}/_ensure_sandbox_test.txt", "timeout": 5000},
        )
        resp.raise_for_status()
        data = resp.json()
        R.check("volume is writable", data.get("exitCode") == 0 and "vol_test" in data.get("result", ""),
                 f"exitCode={data.get('exitCode')}, result={data.get('result', '')[:200]}")


async def test_int_expose(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    print("\n── expose: invalid port (too low) ──")
    r = await tools.expose(port=80, **ctx)
    R.check("port 80 rejected", "Error" in r and "3000" in r, r[:200])

    print("\n── expose: invalid port (too high) ──")
    r = await tools.expose(port=10000, **ctx)
    R.check("port 10000 rejected", "Error" in r and "9999" in r, r[:200])

    print("\n── expose: no port and no ssh ──")
    r = await tools.expose(**ctx)
    R.check("default args rejected", "Error" in r, r[:200])

    print("\n── expose: start a server then get URL ──")
    await tools.bash(
        "python3 -m http.server 8080 &",
        **ctx,
    )
    import asyncio as _asyncio
    await _asyncio.sleep(1)

    r = await tools.expose(port=8080, **ctx)
    R.check("expose returns URL", "Public URL" in r, r[:300])
    R.check("URL contains https://", "https://" in r, r[:300])
    R.check("mentions 1 hour validity", "1 hour" in r, r[:300])
    R.check("mentions warning", "warning" in r.lower(), r[:300])

    # Clean up the background server
    await tools.bash("pkill -f 'http.server 8080' || true", **ctx)

    print("\n── expose: SSH access ──")
    r = await tools.expose(ssh=True, **ctx)
    R.check("ssh returns command", "ssh" in r.lower(), r[:300])
    R.check("mentions validity", "60 min" in r, r[:300])
    R.check("contains ssh command block", "```" in r, r[:300])


async def test_int_destroy(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)
    from lathe import _headers, VOLUME_MOUNT_PATH

    print("\n── destroy: safety guard (confirm=false) ──")
    result = await tools.destroy(**ctx)
    R.check("default confirm=false aborts", "aborted" in result.lower(), result[:200])
    R.check("abort message mentions confirm", "confirm" in result.lower(), result[:200])

    result = await tools.destroy(confirm=False, **ctx)
    R.check("explicit confirm=false aborts", "aborted" in result.lower(), result[:200])

    # Verify sandbox still exists after abort
    result = await tools.bash("echo still_alive", **ctx)
    R.check("sandbox survives abort", "still_alive" in result, result[:200])

    print("\n── destroy: wipes sandbox but preserves volume ──")
    # Write a marker file to the volume before destroying
    result = await tools.bash(
        f"echo destroy_test > {VOLUME_MOUNT_PATH}/destroy_test.txt",
        **ctx,
    )
    R.check("write marker to volume", "Error" not in result, result[:200])

    result = await tools.destroy(confirm=True, **ctx)
    R.check("destroy reports success", "Destroyed" in result and "1 sandbox" in result, result[:200])
    R.check("destroy mentions volume intact", "intact" in result.lower(), result[:200])

    import httpx, json as _json
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{tools.valves.daytona_api_url}/sandbox",
            params={"labels": _json.dumps({"test-harness": TEST_EMAIL})},
            headers=_headers(tools.valves),
        )
        remaining = [
            s for s in (resp.json() or [])
            if s.get("labels", {}).get("test-harness") == TEST_EMAIL
        ]
        R.check("sandbox gone after destroy", len(remaining) == 0, f"got {len(remaining)}")

    print("\n── destroy: volume data survives sandbox destruction ──")
    # bash() creates a fresh sandbox (with volume re-mounted)
    result = await tools.bash(
        f"cat {VOLUME_MOUNT_PATH}/destroy_test.txt",
        **ctx,
    )
    R.check("volume data survives destroy", "destroy_test" in result, result[:200])

    print("\n── destroy: no sandbox to destroy ──")
    result = await tools.destroy(confirm=True, **ctx)
    # First destroy the sandbox that was just created
    # Then try again with nothing left
    result = await tools.destroy(confirm=True, **ctx)
    R.check("destroy with nothing reports no sandbox", "No sandbox found" in result, result[:200])

    print("\n── destroy: next tool call creates fresh sandbox ──")
    result = await tools.bash("echo reborn", **ctx)
    R.check("fresh sandbox works", "reborn" in result, result[:200])

    print("\n── final cleanup: destroy reborn sandbox ──")
    result = await tools.destroy(
        confirm=True,
        **ctx,
    )
    R.check("final destroy succeeds", "Destroyed" in result, result[:200])


# ── Test group registry ──────────────────────────────────────────────

UNIT_GROUPS = {
    "parse_env_vars": test_unit_parse_env_vars,
    "onboard_script": test_unit_onboard_script,
    "truncate": test_unit_truncate,
}

INTEGRATION_GROUPS = {
    "bash": test_int_bash,
    "write_read_edit": test_int_write_read_edit,
    "onboard": test_int_onboard,
    "truncation": test_int_truncation,
    "bash_sessions": test_int_bash_sessions,
    "env_vars": test_int_env_vars,
    "expose": test_int_expose,
    "ensure_sandbox": test_int_ensure_sandbox,
    "destroy": test_int_destroy,  # must run last among integration tests
}

ALL_GROUPS = {**UNIT_GROUPS, **INTEGRATION_GROUPS}


async def run_tests():
    args = sys.argv[1:]

    if "--list" in args:
        print("Unit groups (no sandbox):")
        for name in UNIT_GROUPS:
            print(f"  {name}")
        print("\nIntegration groups (need sandbox + DAYTONA_API_KEY):")
        for name in INTEGRATION_GROUPS:
            print(f"  {name}")
        print("\nSpecial selectors:")
        print("  unit         — all unit tests")
        print("  integration  — all integration tests")
        return

    # Resolve selectors
    if not args:
        selected = list(ALL_GROUPS.keys())
    else:
        selected = []
        for arg in args:
            if arg == "unit":
                selected.extend(UNIT_GROUPS.keys())
            elif arg == "integration":
                selected.extend(INTEGRATION_GROUPS.keys())
            elif arg in ALL_GROUPS:
                selected.append(arg)
            else:
                print(f"Unknown group: {arg}. Use --list to see available groups.")
                sys.exit(1)

    # Deduplicate preserving order
    seen = set()
    selected = [g for g in selected if not (g in seen or seen.add(g))]

    need_integration = any(g in INTEGRATION_GROUPS for g in selected)
    if need_integration and not API_KEY:
        print("Error: DAYTONA_API_KEY not set. Add it to .env or export it.")
        print("(Run with 'unit' to skip integration tests.)")
        sys.exit(1)

    R = Results()
    t0 = time.time()

    # Run unit tests first
    unit_selected = [g for g in selected if g in UNIT_GROUPS]
    int_selected = [g for g in selected if g in INTEGRATION_GROUPS]

    if unit_selected:
        print(f"\n{'='*50}")
        print(f"UNIT TESTS ({', '.join(unit_selected)})")
        print(f"{'='*50}")
        # Unit tests are instant — run them all concurrently
        await asyncio.gather(*(UNIT_GROUPS[g](R) for g in unit_selected))

        if R.failed > 0 and int_selected:
            print(f"\n{R.failed} unit test(s) failed — skipping integration tests.")
            int_selected = []

    if int_selected:
        print(f"\n{'='*50}")
        print(f"INTEGRATION TESTS ({', '.join(int_selected)})")
        print(f"{'='*50}")

        # Shared tools + user across all integration tests
        tools = Tools()
        tools.valves.daytona_api_key = API_KEY
        tools.valves.deployment_label = "test-harness"
        user = {"email": TEST_EMAIL, "id": "test-id", "name": "Test User"}

        # Pre-warm: create/start sandbox once before fanning out
        import httpx as _httpx
        from lathe import _ensure_sandbox
        print("\n── pre-warm: ensuring sandbox is ready ──")
        async with _httpx.AsyncClient() as _prewarm_client:
            await _ensure_sandbox(tools.valves, TEST_EMAIL, _prewarm_client, emitter=mock_emitter)

        # Integration tests that are independent can run concurrently,
        # but destroy must run last and ensure_sandbox has side effects.
        # Group into: concurrent batch + sequential tail.
        concurrent = [g for g in int_selected if g not in ("destroy", "ensure_sandbox")]
        sequential = [g for g in int_selected if g in ("ensure_sandbox",)]
        tail = [g for g in int_selected if g == "destroy"]

        if concurrent:
            await asyncio.gather(*(INTEGRATION_GROUPS[g](R, tools, user) for g in concurrent))
        for g in sequential:
            await INTEGRATION_GROUPS[g](R, tools, user)
        for g in tail:
            await INTEGRATION_GROUPS[g](R, tools, user)

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
    asyncio.run(run_tests())

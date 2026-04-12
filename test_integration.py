#!/usr/bin/env python3
"""
Integration tests for lathe.py — exercises all tools against the live
Daytona sandbox API by calling Python methods directly.

Usage:
    uv run python test_integration.py              # run all tests
    uv run python test_integration.py bash edit    # specific groups
    uv run python test_integration.py --list       # list available groups

Requires DAYTONA_API_KEY in .env (or environment).
"""

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


async def test_int_bash_backgrounding(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)
    import re

    print("\n── bash backgrounding: short timeout triggers auto-background ──")
    # A short foreground_seconds=2 with a 5s sleep should auto-background
    bg_result = await tools.bash(
        "echo bg-start && sleep 5 && echo bg-done",
        foreground_seconds=2,
        **ctx,
    )
    R.check("short timeout triggers backgrounding", "Backgrounded" in bg_result, bg_result[:300])
    R.check("background descriptor has CMD=", "CMD=" in bg_result, bg_result[:300])
    R.check("background descriptor has manpage pointer", 'manpage="background"' in bg_result, bg_result[:300])

    # Extract CMD uuid from background descriptor and verify the command
    # actually finishes (observable via a second bash call)
    cmd_match = re.search(r"CMD=([0-9a-f-]{36})", bg_result)
    if cmd_match:
        bg_cmd_dir = f"/tmp/cmd/{cmd_match.group(1)}"

        # Wait for the backgrounded command to finish, then check its log
        wait_result = await tools.bash(
            f"while [ ! -f {bg_cmd_dir}/exit ]; do sleep 1; done; "
            f"cat {bg_cmd_dir}/log",
            foreground_seconds=30,
            **ctx,
        )
        R.check("backgrounded command completes", "bg-done" in wait_result, wait_result[:300])
    else:
        R.check("CMD dir found in background descriptor", False, "no CMD= match")

    print("\n── bash backgrounding: long timeout avoids backgrounding ──")
    # A foreground_seconds=60 with a 3s sleep should NOT background
    fg_result = await tools.bash(
        "sleep 3 && echo fg-completed",
        foreground_seconds=60,
        **ctx,
    )
    R.check("long timeout avoids backgrounding", "fg-completed" in fg_result, fg_result[:200])
    R.check("no background descriptor", "Backgrounded" not in fg_result, fg_result[:200])


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
        # First ensure we have a baseline sandbox
        sandbox_id, _ = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)

        # Create a second sandbox with the same label to trigger Lathe's duplicate detection
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


async def test_int_expose(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)

    print("\n── expose: invalid port (too low) ──")
    r = await tools.expose(target="http:80", **ctx)
    R.check("port 80 rejected", "Error" in r and "3000" in r, r[:200])

    print("\n── expose: invalid port (too high) ──")
    r = await tools.expose(target="http:10000", **ctx)
    R.check("port 10000 rejected", "Error" in r and "9999" in r, r[:200])

    print("\n── expose: bare port number rejected ──")
    r = await tools.expose(target="3000", **ctx)
    R.check("bare port rejected", "Error" in r and "http:" in r.lower(), r[:200])

    print("\n── expose: nonsense target ──")
    r = await tools.expose(target="ftp", **ctx)
    R.check("nonsense target rejected", "Error" in r, r[:200])

    print("\n── expose: start a server then get URL ──")
    await tools.bash(
        "python3 -m http.server 8080 &",
        **ctx,
    )
    import asyncio as _asyncio
    await _asyncio.sleep(1)

    r = await tools.expose(target="http:8080", **ctx)
    R.check("expose returns URL", "Public URL" in r, r[:300])
    R.check("URL contains https://", "https://" in r, r[:300])
    R.check("mentions 1 hour validity", "1 hour" in r, r[:300])
    R.check("mentions warning", "warning" in r.lower(), r[:300])

    # Clean up the background server
    await tools.bash("pkill -f 'http.server 8080' || true", **ctx)

    print("\n── expose: SSH access ──")
    r = await tools.expose(target="ssh", **ctx)
    R.check("ssh returns command", "ssh" in r.lower(), r[:300])
    R.check("mentions validity", "60 min" in r, r[:300])
    R.check("contains ssh command block", "```" in r, r[:300])


async def test_int_destroy(R: Results, tools: Tools, user: dict):
    ctx = dict(__user__=user, __event_emitter__=mock_emitter)
    from lathe import _headers

    print("\n── destroy: safety guard (confirm=false) ──")
    result = await tools.destroy(**ctx)
    R.check("default confirm=false aborts", "aborted" in result.lower(), result[:200])
    R.check("abort message mentions confirm", "confirm" in result.lower(), result[:200])

    result = await tools.destroy(confirm=False, **ctx)
    R.check("explicit confirm=false aborts", "aborted" in result.lower(), result[:200])

    # Verify sandbox still exists after abort
    result = await tools.bash("echo still_alive", **ctx)
    R.check("sandbox survives abort", "still_alive" in result, result[:200])

    print("\n── destroy: confirmed destruction ──")
    result = await tools.destroy(confirm=True, **ctx)
    R.check("destroy reports success", "Destroyed" in result and "1 sandbox" in result, result[:200])
    R.check("destroy mentions volume intact", "intact" in result.lower(), result[:200])

    print("\n── destroy: no sandbox to destroy ──")
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

async def test_int_interpret(R: Results, tools: Tools, user: dict):
    import uuid
    chat_id = f"test-interpret-{uuid.uuid4().hex[:8]}"
    ctx = dict(__user__=user, __event_emitter__=mock_emitter, __chat_id__=chat_id)

    print("\n── interpret: basic output ──")
    r = await tools.interpret("print('hello from interpreter')", **ctx)
    R.check("basic print works", "hello from interpreter" in r, r[:300])

    print("\n── interpret: state persistence ──")
    await tools.interpret("x = 42", **ctx)
    r = await tools.interpret("print(x + 1)", **ctx)
    R.check("state persists across calls", "43" in r, r[:300])

    print("\n── interpret: import persistence ──")
    await tools.interpret("import math", **ctx)
    r = await tools.interpret("print(math.pi)", **ctx)
    R.check("import persists", "3.14159" in r, r[:300])

    print("\n── interpret: error handling ──")
    r = await tools.interpret("1 / 0", **ctx)
    R.check("ZeroDivisionError reported", "ZeroDivisionError" in r, r[:300])

    print("\n── interpret: state survives after error ──")
    r = await tools.interpret("print(x)", **ctx)
    R.check("x still defined after error", "42" in r, r[:300])

    print("\n── interpret: multi-line code ──")
    r = await tools.interpret("for i in range(3):\n    print(i)", **ctx)
    R.check("loop output", "0" in r and "1" in r and "2" in r, r[:300])

    print("\n── interpret: empty code rejected ──")
    r = await tools.interpret("   ", **ctx)
    R.check("empty code error", "Error" in r and "empty" in r.lower(), r[:300])

    print("\n── interpret: fresh chat gets fresh context ──")
    chat_id2 = f"test-interpret-{uuid.uuid4().hex[:8]}"
    ctx2 = dict(__user__=user, __event_emitter__=mock_emitter, __chat_id__=chat_id2)
    r = await tools.interpret("print(x)", **ctx2)
    R.check("fresh chat has no x", "NameError" in r or "not defined" in r, r[:300])


TESTS = {
    "bash": test_int_bash,
    "write_read_edit": test_int_write_read_edit,
    "onboard": test_int_onboard,
    "truncation": test_int_truncation,
    "bash_backgrounding": test_int_bash_backgrounding,
    "env_vars": test_int_env_vars,
    "expose": test_int_expose,
    "interpret": test_int_interpret,
    "ensure_sandbox": test_int_ensure_sandbox,
    "destroy": test_int_destroy,  # must run last
}


async def main():
    args = sys.argv[1:]

    if "--list" in args:
        print("Available integration tests:")
        for name in TESTS:
            print(f"  {name}")
        return

    selected = args if args else list(TESTS.keys())
    for name in selected:
        if name not in TESTS:
            print(f"Unknown test: {name}. Use --list to see available tests.")
            sys.exit(1)

    if not API_KEY:
        print("Error: DAYTONA_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    R = Results()
    t0 = time.time()

    print(f"{'='*50}")
    print(f"INTEGRATION TESTS ({', '.join(selected)})")
    print(f"{'='*50}")

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

    # Independent tests run concurrently; ensure_sandbox and destroy run sequentially
    concurrent = [g for g in selected if g not in ("destroy", "ensure_sandbox")]
    sequential = [g for g in selected if g in ("ensure_sandbox",)]
    tail = [g for g in selected if g == "destroy"]

    if concurrent:
        await asyncio.gather(*(TESTS[g](R, tools, user) for g in concurrent))
    for g in sequential:
        await TESTS[g](R, tools, user)
    for g in tail:
        await TESTS[g](R, tools, user)

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

#!/usr/bin/env python3
"""
Test harness for daytona_sandbox.py toolkit.
Exercises all four tools against the live Daytona API.

Usage:
    uv run --script test_harness.py
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pydantic", "python-dotenv"]
# ///

import asyncio
import sys
import os

from dotenv import load_dotenv

load_dotenv()

# Import the toolkit
sys.path.insert(0, os.path.dirname(__file__))
from daytona_sandbox import Tools

API_KEY = os.environ.get("DAYTONA_API_KEY")
if not API_KEY:
    print("Error: DAYTONA_API_KEY not set. Add it to .env or export it.")
    sys.exit(1)
TEST_EMAIL = "test-harness@daytona-owui-test"


async def mock_emitter(event):
    """Print status events for visibility."""
    data = event.get("data", {})
    desc = data.get("description", "")
    done = data.get("done", False)
    marker = "✓" if done else "…"
    print(f"  [{marker}] {desc}")


async def run_tests():
    tools = Tools()
    tools.valves.daytona_api_key = API_KEY
    tools.valves.deployment_label = "test-harness"

    user = {"email": TEST_EMAIL, "id": "test-id", "name": "Test User"}

    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Test 1: bash ─────────────────────────────────────────────
    print("\n── bash: simple command ──")
    result = await tools.bash("echo hello world", __user__=user, __event_emitter__=mock_emitter)
    check("echo returns output", "hello world" in result, result[:200])

    print("\n── bash: compound command ──")
    result = await tools.bash("echo one && echo two && echo three", __user__=user, __event_emitter__=mock_emitter)
    check("compound && works", "one" in result and "two" in result and "three" in result, result[:200])

    print("\n── bash: pipes ──")
    result = await tools.bash("echo 'hello world' | wc -w", __user__=user, __event_emitter__=mock_emitter)
    check("pipe works", "2" in result, result[:200])

    print("\n── bash: exit code ──")
    result = await tools.bash("exit 42", __user__=user, __event_emitter__=mock_emitter)
    check("non-zero exit reported", "Exit code: 42" in result, result[:200])

    print("\n── bash: working directory ──")
    result = await tools.bash("pwd", __user__=user, __event_emitter__=mock_emitter)
    check("default cwd is /home/daytona/workspace", "/home/daytona/workspace" in result, result[:200])

    result = await tools.bash("pwd", workdir="/tmp", __user__=user, __event_emitter__=mock_emitter)
    check("custom cwd works", "/tmp" in result, result[:200])

    print("\n── bash: quoted flag values ──")
    # This is the exact pattern that failed in production: --flag "value"
    # Previously, the bash -c "..." wrapping mangled quoted flag values
    result = await tools.bash(
        'echo "--state" "open" | cat',
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("quoted flag values pass through", "--state" in result and "open" in result, result[:200])

    print("\n── bash: single quotes ──")
    result = await tools.bash(
        "echo 'hello world'",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("single quotes work", "hello world" in result, result[:200])

    print("\n── bash: mixed quoting ──")
    result = await tools.bash(
        """echo "it's a 'test'" && echo 'say "hello"'""",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("mixed quotes work", "it's a 'test'" in result and 'say "hello"' in result, result[:200])

    print("\n── bash: backslashes ──")
    result = await tools.bash(
        r"echo 'back\slash'",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("backslashes preserved", "back\\slash" in result, result[:200])

    print("\n── bash: dollar signs and variables ──")
    result = await tools.bash(
        'FOO=bar && echo "val=$FOO"',
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("variable expansion works", "val=bar" in result, result[:200])

    print("\n── bash: set -e aborts on error ──")
    result = await tools.bash(
        "false\necho should-not-reach",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("set -e aborts on first failure", "Exit code:" in result, result[:200])
    check("second command did not run", "should-not-reach" not in result, result[:200])

    print("\n── bash: pipefail catches pipe errors ──")
    result = await tools.bash(
        "false | cat",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("pipefail reports failure", "Exit code:" in result, result[:200])

    print("\n── bash: || true overrides set -e ──")
    result = await tools.bash(
        "false || true\necho survived",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("|| true suppresses abort", "survived" in result, result[:200])

    # ── Test 2: write ────────────────────────────────────────────
    print("\n── write: create file ──")
    test_content = "line one\nline two\nline three\n"
    result = await tools.write(
        "workspace/test_file.txt", test_content,
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("write reports success", "Wrote" in result and "test_file.txt" in result, result[:200])

    # ── Test 3: read ─────────────────────────────────────────────
    print("\n── read: full file ──")
    result = await tools.read(
        "workspace/test_file.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("read returns content", "line one" in result and "line two" in result, result[:200])
    check("read has line numbers", "1: line one" in result, result[:200])
    check("read shows total lines", "3 lines total" in result, result[:200])

    print("\n── read: offset and limit ──")
    result = await tools.read(
        "workspace/test_file.txt", offset=2, limit=1,
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("offset/limit works", "2: line two" in result, result[:200])
    check("respects limit", "line three" not in result, result[:200])

    print("\n── read: file not found ──")
    result = await tools.read(
        "workspace/nonexistent.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("reports file not found", "Error" in result or "not found" in result.lower(), result[:200])

    # ── Test 4: edit ─────────────────────────────────────────────
    print("\n── edit: single replacement ──")
    result = await tools.edit(
        "workspace/test_file.txt", "line two", "LINE TWO EDITED",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("edit reports success", "Replaced 1" in result, result[:200])

    # Verify the edit stuck
    result = await tools.read(
        "workspace/test_file.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("edit persisted", "LINE TWO EDITED" in result, result[:200])
    check("other lines untouched", "line one" in result and "line three" in result, result[:200])

    print("\n── edit: old_string not found ──")
    result = await tools.edit(
        "workspace/test_file.txt", "this text does not exist", "replacement",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("reports not found", "not found" in result.lower(), result[:200])

    print("\n── edit: multiple matches without replace_all ──")
    # Write a file with duplicate content
    await tools.write(
        "workspace/dup_test.txt", "aaa\nbbb\naaa\nbbb\naaa\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.edit(
        "workspace/dup_test.txt", "aaa", "zzz",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("rejects ambiguous edit", "3 matches" in result or "multiple" in result.lower(), result[:200])

    print("\n── edit: replace_all ──")
    result = await tools.edit(
        "workspace/dup_test.txt", "aaa", "zzz", replace_all=True,
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("replace_all reports count", "Replaced 3" in result, result[:200])

    result = await tools.read(
        "workspace/dup_test.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("replace_all applied", "aaa" not in result and "zzz" in result, result[:200])

    print("\n── edit: file not found ──")
    result = await tools.edit(
        "workspace/nonexistent.txt", "foo", "bar",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("edit on missing file errors", "Error" in result or "not found" in result.lower(), result[:200])

    # ── Test 5: write creates parent dirs ────────────────────────
    print("\n── write: nested path ──")
    result = await tools.write(
        "workspace/deep/nested/dir/file.txt", "nested content\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("write to nested path succeeds", "Wrote" in result, result[:200])

    result = await tools.read(
        "workspace/deep/nested/dir/file.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("nested file readable", "nested content" in result, result[:200])

    # ── Test 6: onboard ──────────────────────────────────────────
    # Clean slate for onboard tests (paths relative to /home/daytona, matching write() paths)
    await tools.bash("rm -rf /home/daytona/workspace/test_project /home/daytona/workspace/empty_project", __user__=user, __event_emitter__=mock_emitter)

    print("\n── onboard: missing context ──")
    result = await tools.onboard(
        "/home/daytona/workspace/empty_project",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("fails without AGENTS.md or .agents/", "Error" in result, result[:200])

    print("\n── onboard: AGENTS.md only ──")
    await tools.write(
        "workspace/test_project/AGENTS.md",
        "# Test Agent\nYou are a helpful test agent.\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.onboard(
        "/home/daytona/workspace/test_project",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("returns AGENTS.md content", "helpful test agent" in result, result[:300])
    check("no skills section when none exist", "Available Skills" not in result, result[:300])

    print("\n── onboard: with skills ──")
    await tools.write(
        "workspace/test_project/.agents/skills/test-skill/SKILL.md",
        "---\nname: test-skill\ndescription: A skill for testing things.\n---\n\n# Test Skill\nDetailed instructions here.\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.onboard(
        "/home/daytona/workspace/test_project",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("returns AGENTS.md", "helpful test agent" in result, result[:500])
    check("lists skill name", "test-skill" in result, result[:500])
    check("lists skill description", "testing things" in result, result[:500])
    check("includes SKILL.md path", "SKILL.md" in result, result[:500])
    check("does NOT include skill body", "Detailed instructions here" not in result, result[:500])

    # ── Cleanup ──────────────────────────────────────────────────
    print("\n── cleanup ──")
    await tools.bash("rm -rf workspace/test_file.txt workspace/dup_test.txt workspace/deep workspace/test_project", __user__=user, __event_emitter__=mock_emitter)

    # Stop the sandbox to conserve resources
    import httpx
    from daytona_sandbox import _headers
    async with httpx.AsyncClient(timeout=30.0) as client:
        sandboxes_resp = await client.get(
            f"{tools.valves.daytona_api_url}/sandbox",
            params={"label": f"test-harness:{TEST_EMAIL}"},
            headers=_headers(tools.valves),
        )
        for s in sandboxes_resp.json():
            print(f"  Stopping test sandbox {s['id'][:12]}...")
            await client.post(
                f"{tools.valves.daytona_api_url}/sandbox/{s['id']}/stop",
                headers=_headers(tools.valves),
            )

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
    if failed:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(run_tests())

#!/usr/bin/env python3
"""
Test harness for lathe.py toolkit.
Exercises all seven tools against the live sandbox provider API.

Usage:
    uv run --script test_harness.py
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pydantic", "python-dotenv", "fastapi", "pygments"]
# ///

import asyncio
import sys
import os

from dotenv import load_dotenv

load_dotenv()

# Import the toolkit
sys.path.insert(0, os.path.dirname(__file__))
from lathe import Tools

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

    # ── Test 7: attach ───────────────────────────────────────────
    from fastapi.responses import HTMLResponse

    print("\n── attach: Python file ──")
    py_content = 'def greet(name):\n    return f"Hello, {name}!"\n\nprint(greet("world"))\n'
    await tools.write(
        "workspace/attach_test.py", py_content,
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/attach_test.py",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    body = result.body.decode("utf-8")
    check("has Content-Disposition inline", result.headers.get("content-disposition") == "inline", result.headers.get("content-disposition", ""))
    check("contains filename in header", "attach_test.py" in body, "")
    check("contains line count", "4 lines" in body, "")
    check("contains byte count", str(len(py_content.encode("utf-8"))) in body, "")
    check("contains height reporting script", "iframe:height" in body, "")
    check("contains copy button", "Copy" in body and "copyFile" in body, "")
    check("contains save button", "Save" in body and "saveFile" in body, "")

    print("\n── attach: syntax highlighting ──")
    # Pygments should produce <span style= tokens for Python
    check("has Pygments highlighting spans", 'style="' in body and "<span" in body, "no inline styles found")
    check("contains the function content", "greet" in body, "")

    print("\n── attach: plain text file ──")
    await tools.write(
        "workspace/attach_test.txt", "just plain text\nno highlighting\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/attach_test.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("txt returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    body_txt = result.body.decode("utf-8")
    check("txt contains content", "just plain text" in body_txt, "")
    check("txt contains filename", "attach_test.txt" in body_txt, "")

    print("\n── attach: file not found ──")
    result = await tools.attach(
        "workspace/nonexistent_file.xyz",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("missing file returns error string", isinstance(result, str) and "Error" in result, str(result)[:200])

    print("\n── attach: base64 round-trip ──")
    import base64
    # Verify the base64 payload in the HTML decodes to the original content
    import re
    b64_match = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', body)
    check("base64 payload present", b64_match is not None, "no atob() found")
    if b64_match:
        decoded = base64.b64decode(b64_match.group(1)).decode("utf-8")
        check("base64 decodes to original content", decoded == py_content, f"got {decoded[:80]}...")

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

    # ── Test 8: attach image file ──────────────────────────────
    print("\n── attach: PNG image ──")
    # Create a minimal valid 1x1 red PNG (67 bytes)
    import struct, zlib as _zlib
    def _make_tiny_png():
        def _chunk(ctype, data):
            c = ctype + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", _zlib.crc32(c) & 0xFFFFFFFF)
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        raw_row = b"\x00\xff\x00\x00"  # filter byte + RGB
        idat = _chunk(b"IDAT", _zlib.compress(raw_row))
        iend = _chunk(b"IEND", b"")
        return sig + ihdr + idat + iend

    png_bytes = _make_tiny_png()
    # Write via bash since write() is text-oriented
    import base64 as _b64
    png_b64 = _b64.b64encode(png_bytes).decode()
    await tools.bash(
        f"echo '{png_b64}' | base64 -d > /home/daytona/workspace/test_image.png",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/test_image.png",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("image returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    img_body = result.body.decode("utf-8")
    check("image has <img> tag", "<img " in img_body, "")
    check("image has data URI", "data:image/png;base64," in img_body, "")
    check("image has filename", "test_image.png" in img_body, "")
    check("image has Save button", "Save" in img_body and "saveFile" in img_body, "")
    check("image does NOT have Copy button", "copyFile" not in img_body, "image shouldn't have Copy")
    check("image does NOT have line numbers", "gutter" not in img_body, "image shouldn't have line gutter")
    check("image has height reporting", "iframe:height" in img_body, "")

    print("\n── attach: SVG image ──")
    svg_content = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect fill="red" width="10" height="10"/></svg>'
    await tools.write(
        "workspace/test_image.svg", svg_content,
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/test_image.svg",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("SVG returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    svg_body = result.body.decode("utf-8")
    check("SVG renders as image not code", "<img " in svg_body, "SVG should render as <img>")
    check("SVG has correct MIME", "image/svg+xml" in svg_body, "")

    # ── Test 9: attach binary file ───────────────────────────────
    print("\n── attach: ZIP binary file ──")
    # Create a minimal zip file in the sandbox
    await tools.bash(
        "cd /home/daytona/workspace && echo 'hello from zip' > _zipme.txt && zip test_archive.zip _zipme.txt && rm _zipme.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/test_archive.zip",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("zip returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    zip_body = result.body.decode("utf-8")
    check("zip shows filename", "test_archive.zip" in zip_body, "")
    check("zip shows file type", "ZIP" in zip_body, "")
    check("zip has download card (no code viewer)", "gutter" not in zip_body, "binary shouldn't have line gutter")
    check("zip has Save button (under 10MB)", "saveFile" in zip_body, "small zip should be downloadable")
    check("zip does NOT have Copy button", "copyFile" not in zip_body, "binary shouldn't have Copy")
    check("zip has height reporting", "iframe:height" in zip_body, "")

    print("\n── attach: binary by content (not extension) ──")
    # Write raw bytes that aren't valid UTF-8, with an ambiguous extension
    await tools.bash(
        r"printf '\x80\x81\x82\xff\xfe\xfd' > /home/daytona/workspace/test_binary.dat",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.attach(
        "workspace/test_binary.dat",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("non-UTF8 dat returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
    dat_body = result.body.decode("utf-8")
    check("non-UTF8 renders as binary card", "gutter" not in dat_body and "<img" not in dat_body, "should be download card")
    check("non-UTF8 has Save button", "saveFile" in dat_body, "small binary should be downloadable")

    # ── Test 10: _classify_file unit tests ───────────────────────
    from lathe import _classify_file

    print("\n── _classify_file: image extensions ──")
    for ext in ["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "avif"]:
        check(f".{ext} classified as image", _classify_file(f"file.{ext}", b"") == "image", f"got {_classify_file(f'file.{ext}', b'')}")

    print("\n── _classify_file: binary extensions ──")
    for ext in ["zip", "tar", "gz", "pdf", "exe", "whl", "sqlite", "mp3", "mp4", "woff2"]:
        check(f".{ext} classified as binary", _classify_file(f"file.{ext}", b"") == "binary", f"got {_classify_file(f'file.{ext}', b'')}")

    print("\n── _classify_file: text by content ──")
    check("valid UTF-8 is text", _classify_file("file.unknown", b"hello world") == "text", "")
    check("empty file is text", _classify_file("noext", b"") == "text", "")
    check("UTF-8 with BOM is text", _classify_file("f.cfg", b"\xef\xbb\xbfhello") == "text", "")

    print("\n── _classify_file: binary by content ──")
    check("invalid UTF-8 is binary", _classify_file("file.dat", b"\x80\x81\x82") == "binary", "")
    # Note: null bytes are valid UTF-8 (U+0000), so b"hello\x00world" classifies as text.
    # .bin is not in _BINARY_EXTS, so classification falls through to decode heuristic.
    check("null bytes are valid UTF-8 (text)", _classify_file("file.bin", b"hello\x00world") == "text", "")
    # But .bin-like extensions that ARE in the binary list get caught by extension
    check(".exe classified by extension", _classify_file("file.exe", b"hello\x00world") == "binary", "")

    # ── Test 11: _render_image_html unit tests ───────────────────
    from lathe import _render_image_html, _render_binary_html

    print("\n── _render_image_html: structure ──")
    img_html = _render_image_html(b"\x89PNG fake", "photo.png", "dir/photo.png")
    check("image html has doctype", "<!DOCTYPE html>" in img_html, "")
    check("image html has img tag", "<img " in img_html, "")
    check("image html has correct mime", "image/png" in img_html, "")
    check("image html has filename", "photo.png" in img_html, "")
    check("image html has byte count", "9" in img_html, "should show 9 bytes")  # len(b"\x89PNG fake")
    check("image html has save function", "saveFile" in img_html, "")
    check("image html has resize observer", "ResizeObserver" in img_html, "")

    # ── Test 12: _render_binary_html unit tests ──────────────────
    print("\n── _render_binary_html: small file ──")
    bin_html = _render_binary_html(b"\x00" * 100, "data.zip", "path/data.zip")
    check("binary html has doctype", "<!DOCTYPE html>" in bin_html, "")
    check("binary html has filename", "data.zip" in bin_html, "")
    check("binary html shows ZIP type", "ZIP" in bin_html, "")
    check("binary html has save for small file", "saveFile" in bin_html, "")
    check("binary html has resize observer", "ResizeObserver" in bin_html, "")

    print("\n── _render_binary_html: large file (over 10MB) ──")
    from lathe import _EMBED_SIZE_CAP
    # Don't actually allocate 10MB — just mock by testing the threshold logic
    # We'll create a bytes object just over the cap
    fake_large = b"\x00" * (_EMBED_SIZE_CAP + 1)
    big_html = _render_binary_html(fake_large, "huge.tar.gz", "huge.tar.gz")
    check("large binary has no saveFile", "saveFile" not in big_html, "should not embed >10MB")
    check("large binary shows too-large message", "Too large" in big_html, "")
    check("large binary still has filename", "huge.tar.gz" in big_html, "")
    check("large binary still has resize observer", "ResizeObserver" in big_html, "")
    # Free the large allocation immediately
    del fake_large

    print("\n── _render_binary_html: human-readable sizes ──")
    html_1b = _render_binary_html(b"\x00", "f.bin", "f.bin")
    check("1 byte shows as bytes", "1 B" in html_1b, "")
    html_1kb = _render_binary_html(b"\x00" * 2048, "f.bin", "f.bin")
    check("2048 bytes shows as KB", "KB" in html_1kb, "")
    html_1mb = _render_binary_html(b"\x00" * (2 * 1024 * 1024), "f.bin", "f.bin")
    check("2MB shows as MB", "MB" in html_1mb, "")

    # ── Test 13: _highlight_code helper ──────────────────────────
    from lathe import _highlight_code
    import html as html_mod

    print("\n── _highlight_code: Python ──")
    hl = _highlight_code('def foo():\n    return 42\n', "test.py")
    check("highlight produces spans", "<span" in hl, hl[:100])
    check("highlight has inline styles", 'style="' in hl, hl[:100])
    check("highlight preserves content", "foo" in hl and "42" in hl, hl[:100])

    print("\n── _highlight_code: unknown extension ──")
    hl_unk = _highlight_code("just text", "file.unknownext")
    check("unknown ext still produces output", "just text" in hl_unk, hl_unk[:100])

    print("\n── _highlight_code: no extension ──")
    hl_none = _highlight_code("raw content", "Makefile")
    check("no-ext file produces output", "raw content" in hl_none or "content" in hl_none, hl_none[:100])

    # ── Test 14: _truncate_tail unit tests ──────────────────────────
    from lathe import _truncate_tail, _MAX_LINES, _MAX_BYTES

    print("\n── _truncate_tail: no truncation needed ──")
    short = "line 1\nline 2\nline 3"
    out, trunc, meta = _truncate_tail(short)
    check("short text not truncated", not trunc, f"truncated={trunc}")
    check("short text unchanged", out == short, out[:80])

    print("\n── _truncate_tail: line limit ──")
    many_lines = "\n".join(f"line {i}" for i in range(5000))
    out, trunc, meta = _truncate_tail(many_lines)
    check("many lines truncated", trunc, f"truncated={trunc}")
    check("truncated by lines", meta["truncated_by"] == "lines", meta.get("truncated_by"))
    check("keeps last N lines", out.endswith("line 4999"), out[-30:])
    check("total_lines correct", meta["total_lines"] == 5000, meta.get("total_lines"))
    out_line_count = out.count("\n") + 1
    check(f"output has <= {_MAX_LINES} lines", out_line_count <= _MAX_LINES, f"got {out_line_count}")

    print("\n── _truncate_tail: byte limit ──")
    # Create output that's under line limit but over byte limit
    # Each line is ~100 bytes, 600 lines = ~60KB > 50KB limit
    fat_lines = "\n".join(f"{'x' * 99}" for _ in range(600))
    out, trunc, meta = _truncate_tail(fat_lines)
    check("fat lines truncated", trunc, f"truncated={trunc}")
    check("truncated by bytes", meta["truncated_by"] == "bytes", meta.get("truncated_by"))
    out_bytes = len(out.encode("utf-8"))
    check(f"output <= {_MAX_BYTES} bytes", out_bytes <= _MAX_BYTES, f"got {out_bytes}")

    print("\n── _truncate_tail: empty string ──")
    out, trunc, meta = _truncate_tail("")
    check("empty string not truncated", not trunc, f"truncated={trunc}")

    # ── Test 15: bash output truncation (integration) ────────────
    print("\n── bash: output truncation with spill file ──")
    # Generate 3000 lines of output — should trigger truncation
    result = await tools.bash(
        "for i in $(seq 1 3000); do echo \"output line $i\"; done",
        __user__=user, __event_emitter__=mock_emitter,
    )
    check("truncated output has notice", "[Showing lines" in result, result[-200:])
    check("notice mentions full output file", "/tmp/_bash_output_" in result, result[-200:])
    check("last line present in output", "output line 3000" in result, result[-200:])
    check("first line NOT in truncated output", "output line 1\n" not in result, "line 1 should be truncated away")

    # Verify the spill file exists and contains everything
    import re
    spill_match = re.search(r"/tmp/_bash_output_\w+\.log", result)
    if spill_match:
        spill_path = spill_match.group(0)
        # Check the file is accessible and has the full content
        verify = await tools.bash(
            f"wc -l < {spill_path}",
            __user__=user, __event_emitter__=mock_emitter,
        )
        check("spill file has all 3000 lines", "3000" in verify, verify.strip())

        # Check we can read a specific slice from it
        head_result = await tools.bash(
            f"head -n 3 {spill_path}",
            __user__=user, __event_emitter__=mock_emitter,
        )
        check("can retrieve early lines from spill file", "output line 1" in head_result, head_result[:100])
    else:
        check("spill file path found in notice", False, "no path match found")

    print("\n── bash: small output NOT truncated ──")
    result = await tools.bash("echo hello", __user__=user, __event_emitter__=mock_emitter)
    check("small output has no truncation notice", "[Showing lines" not in result, result[:200])

    # ── Cleanup ──────────────────────────────────────────────────
    print("\n── cleanup ──")
    await tools.bash("rm -rf workspace/test_file.txt workspace/dup_test.txt workspace/deep workspace/test_project workspace/attach_test.py workspace/attach_test.txt workspace/test_image.png workspace/test_image.svg workspace/test_archive.zip workspace/test_binary.dat /tmp/_bash_output_*.log", __user__=user, __event_emitter__=mock_emitter)

    # Stop the sandbox to conserve resources
    import httpx
    from lathe import _headers
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

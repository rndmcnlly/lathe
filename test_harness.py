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
tasks (e.g. "start a web server and give me a preview link"). Score on: does
it background the server, does it pick the right port, does it avoid known
pitfalls. This tests the docstrings as prompts, not the code as code.
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "pydantic", "python-dotenv", "fastapi", "pygments"]
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


async def test_unit_classify_file(R: Results):
    from lathe import _classify_file

    print("\n── _classify_file: image extensions ──")
    for ext in ["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "avif"]:
        R.check(f".{ext} classified as image", _classify_file(f"file.{ext}", b"") == "image", f"got {_classify_file(f'file.{ext}', b'')}")

    print("\n── _classify_file: binary extensions ──")
    for ext in ["zip", "tar", "gz", "pdf", "exe", "whl", "sqlite", "woff2"]:
        R.check(f".{ext} classified as binary", _classify_file(f"file.{ext}", b"") == "binary", f"got {_classify_file(f'file.{ext}', b'')}")

    print("\n── _classify_file: media by extension (no magic bytes) ──")
    # Files with media extensions but empty content — extension fallback via mimetypes
    for ext, expected in [("mp3", "media"), ("mp4", "media"), ("wav", "media"),
                          ("ogg", "media"), ("flac", "media"), ("webm", "media"),
                          ("mkv", "media"), ("mov", "media"), ("avi", "media"),
                          ("m4a", "media"), ("aac", "media"), ("opus", "media")]:
        R.check(f".{ext} classified as media", _classify_file(f"file.{ext}", b"") == expected, f"got {_classify_file(f'file.{ext}', b'')}")

    print("\n── _classify_file: media by magic bytes (wrong/no extension) ──")
    # Real media content with misleading extensions — magic bytes should win
    R.check("RIFF/WAVE magic → media", _classify_file("file.dat", b"RIFF\x00\x00\x00\x00WAVE") == "media", "")
    R.check("RIFF/AVI magic → media", _classify_file("file.bin", b"RIFF\x00\x00\x00\x00AVI ") == "media", "")
    R.check("ID3 magic → media", _classify_file("file.bin", b"ID3\x03\x00") == "media", "")
    R.check("fLaC magic → media", _classify_file("file.bin", b"fLaC\x00\x00") == "media", "")
    R.check("OggS magic → media", _classify_file("file.bin", b"OggS\x00\x00") == "media", "")
    R.check("ftyp magic → media", _classify_file("file.bin", b"\x00\x00\x00\x1cftypisom") == "media", "")
    R.check("EBML magic → media", _classify_file("file.bin", b"\x1a\x45\xdf\xa3") == "media", "")

    print("\n── _classify_file: text by content ──")
    R.check("valid UTF-8 is text", _classify_file("file.unknown", b"hello world") == "text", "")
    R.check("empty file is text", _classify_file("noext", b"") == "text", "")
    R.check("UTF-8 with BOM is text", _classify_file("f.cfg", b"\xef\xbb\xbfhello") == "text", "")

    print("\n── _classify_file: binary by content ──")
    R.check("invalid UTF-8 is binary", _classify_file("file.dat", b"\x80\x81\x82") == "binary", "")
    R.check("null bytes are valid UTF-8 (text)", _classify_file("file.bin", b"hello\x00world") == "text", "")
    R.check(".exe classified by extension", _classify_file("file.exe", b"hello\x00world") == "binary", "")


async def test_unit_sniff_media(R: Results):
    from lathe import _sniff_media_mime

    print("\n── _sniff_media_mime: WAV ──")
    R.check("WAV detected", _sniff_media_mime(b"RIFF\x00\x00\x00\x00WAVE") == "audio/wav", "")

    print("\n── _sniff_media_mime: AVI ──")
    R.check("AVI detected", _sniff_media_mime(b"RIFF\x00\x00\x00\x00AVI ") == "video/x-msvideo", "")

    print("\n── _sniff_media_mime: MP3 (ID3) ──")
    R.check("MP3 ID3 detected", _sniff_media_mime(b"ID3\x03\x00\x00\x00") == "audio/mpeg", "")

    print("\n── _sniff_media_mime: MP3 (sync word) ──")
    R.check("MP3 sync detected", _sniff_media_mime(b"\xff\xfb\x90\x00") == "audio/mpeg", "")

    print("\n── _sniff_media_mime: FLAC ──")
    R.check("FLAC detected", _sniff_media_mime(b"fLaC\x00\x00\x00\x22") == "audio/flac", "")

    print("\n── _sniff_media_mime: Ogg ──")
    R.check("Ogg detected", _sniff_media_mime(b"OggS\x00\x02\x00\x00") == "audio/ogg", "")

    print("\n── _sniff_media_mime: MP4 (isom) ──")
    R.check("MP4 isom detected", _sniff_media_mime(b"\x00\x00\x00\x1cftypisom") == "video/mp4", "")

    print("\n── _sniff_media_mime: M4A ──")
    R.check("M4A detected", _sniff_media_mime(b"\x00\x00\x00\x1cftypM4A ") == "audio/mp4", "")

    print("\n── _sniff_media_mime: QuickTime ──")
    R.check("MOV detected", _sniff_media_mime(b"\x00\x00\x00\x14ftypqt  ") == "video/quicktime", "")

    print("\n── _sniff_media_mime: WebM ──")
    webm_header = b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm" + b"\x00" * 36
    R.check("WebM detected", _sniff_media_mime(webm_header) == "video/webm", "")

    print("\n── _sniff_media_mime: MKV ──")
    R.check("MKV detected", _sniff_media_mime(b"\x1a\x45\xdf\xa3\x01\x00\x00") == "video/x-matroska", "")

    print("\n── _sniff_media_mime: non-media ──")
    R.check("empty returns None", _sniff_media_mime(b"") is None, "")
    R.check("short returns None", _sniff_media_mime(b"\x00\x01") is None, "")
    R.check("text returns None", _sniff_media_mime(b"hello world") is None, "")
    R.check("PNG returns None", _sniff_media_mime(b"\x89PNG\r\n\x1a\n") is None, "")
    R.check("ZIP returns None", _sniff_media_mime(b"PK\x03\x04") is None, "")


async def test_unit_render_media_html(R: Results):
    from lathe import _render_media_html, _media_mime

    # Build minimal real-ish media bytes for tests
    wav_bytes = b"RIFF\x00\x00\x00\x00WAVEfmt "
    mp4_bytes = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00"
    mp3_bytes = b"ID3\x03\x00\x00\x00\x00\x00\x00"

    print("\n── _render_media_html: video (mp4) ──")
    html = _render_media_html(mp4_bytes, "clip.mp4", "/tmp/clip.mp4")
    R.check("mp4 has <video> tag", "<video controls" in html, "")
    R.check("mp4 has video/mp4 mime", "video/mp4" in html, "")
    R.check("mp4 has Save button", "saveFile" in html, "")
    R.check("mp4 has resize observer", "ResizeObserver" in html, "")
    R.check("mp4 has no <audio>", "<audio" not in html, "")

    print("\n── _render_media_html: audio (wav) ──")
    html = _render_media_html(wav_bytes, "beep.wav", "/tmp/beep.wav")
    R.check("wav has <audio> tag", "<audio controls" in html, "")
    R.check("wav has audio/wav mime", "audio/wav" in html, "")
    R.check("wav has no <video>", "<video" not in html, "")

    print("\n── _render_media_html: audio (mp3) ──")
    html = _render_media_html(mp3_bytes, "song.mp3", "/tmp/song.mp3")
    R.check("mp3 has <audio> tag", "<audio controls" in html, "")
    R.check("mp3 has audio/mpeg mime", "audio/mpeg" in html, "")

    # Note: oversized-fallback test removed — media is always offloaded to OWUI
    # storage before reaching _render_media_html; this function always embeds.

    print("\n── _render_media_html: magic-detected file with wrong extension ──")
    html = _render_media_html(wav_bytes, "mystery.bin", "/tmp/mystery.bin")
    R.check("magic sniff finds WAV in .bin", "<audio controls" in html, "")
    R.check("magic sniff uses audio/wav", "audio/wav" in html, "")


async def test_unit_render_html(R: Results):
    from lathe import _render_image_html, _render_binary_html

    print("\n── _render_image_html: structure ──")
    img_html = _render_image_html(b"\x89PNG fake", "photo.png", "dir/photo.png")
    R.check("image html has doctype", "<!DOCTYPE html>" in img_html, "")
    R.check("image html has img tag", "<img " in img_html, "")
    R.check("image html has correct mime", "image/png" in img_html, "")
    R.check("image html has filename", "photo.png" in img_html, "")
    R.check("image html has byte count", "9" in img_html, "should show 9 bytes")
    R.check("image html has save function", "saveFile" in img_html, "")
    R.check("image html has resize observer", "ResizeObserver" in img_html, "")

    print("\n── _render_binary_html: structure ──")
    bin_html = _render_binary_html(b"\x00" * 100, "data.zip", "path/data.zip")
    R.check("binary html has doctype", "<!DOCTYPE html>" in bin_html, "")
    R.check("binary html has filename", "data.zip" in bin_html, "")
    R.check("binary html shows ZIP type", "ZIP" in bin_html, "")
    R.check("binary html always has saveFile", "saveFile" in bin_html, "")
    R.check("binary html has resize observer", "ResizeObserver" in bin_html, "")

    # Note: large-file / too-large-to-embed test removed — binary files are always
    # offloaded to OWUI storage before reaching _render_binary_html; this function
    # always embeds content for the Save button.

    print("\n── _render_binary_html: human-readable sizes ──")
    html_1b = _render_binary_html(b"\x00", "f.bin", "f.bin")
    R.check("1 byte shows as bytes", "1 B" in html_1b, "")
    html_1kb = _render_binary_html(b"\x00" * 2048, "f.bin", "f.bin")
    R.check("2048 bytes shows as KB", "KB" in html_1kb, "")
    html_1mb = _render_binary_html(b"\x00" * (2 * 1024 * 1024), "f.bin", "f.bin")
    R.check("2MB shows as MB", "MB" in html_1mb, "")


async def test_unit_highlight(R: Results):
    from lathe import _highlight_code

    print("\n── _highlight_code: Python ──")
    hl = _highlight_code('def foo():\n    return 42\n', "test.py")
    R.check("highlight produces spans", "<span" in hl, hl[:100])
    R.check("highlight has inline styles", 'style="' in hl, hl[:100])
    R.check("highlight preserves content", "foo" in hl and "42" in hl, hl[:100])

    print("\n── _highlight_code: unknown extension ──")
    hl_unk = _highlight_code("just text", "file.unknownext")
    R.check("unknown ext still produces output", "just text" in hl_unk, hl_unk[:100])

    print("\n── _highlight_code: no extension ──")
    hl_none = _highlight_code("raw content", "Makefile")
    R.check("no-ext file produces output", "raw content" in hl_none or "content" in hl_none, hl_none[:100])


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

    # These are all independent — run concurrently
    async def t_simple():
        print("\n── bash: simple command ──")
        r = await tools.bash("echo hello world", __user__=user, __event_emitter__=mock_emitter)
        R.check("echo returns output", "hello world" in r, r[:200])

    async def t_compound():
        print("\n── bash: compound command ──")
        r = await tools.bash("echo one && echo two && echo three", __user__=user, __event_emitter__=mock_emitter)
        R.check("compound && works", "one" in r and "two" in r and "three" in r, r[:200])

    async def t_pipes():
        print("\n── bash: pipes ──")
        r = await tools.bash("echo 'hello world' | wc -w", __user__=user, __event_emitter__=mock_emitter)
        R.check("pipe works", "2" in r, r[:200])

    async def t_exit():
        print("\n── bash: exit code ──")
        r = await tools.bash("exit 42", __user__=user, __event_emitter__=mock_emitter)
        R.check("non-zero exit reported", "Exit code: 42" in r, r[:200])

    async def t_workdir():
        print("\n── bash: working directory ──")
        r = await tools.bash("pwd", __user__=user, __event_emitter__=mock_emitter)
        R.check("default cwd is /home/daytona/workspace", "/home/daytona/workspace" in r, r[:200])
        r = await tools.bash("pwd", workdir="/tmp", __user__=user, __event_emitter__=mock_emitter)
        R.check("custom cwd works", "/tmp" in r, r[:200])

    async def t_quoting():
        print("\n── bash: quoted flag values ──")
        r = await tools.bash('echo "--state" "open" | cat', __user__=user, __event_emitter__=mock_emitter)
        R.check("quoted flag values pass through", "--state" in r and "open" in r, r[:200])

        print("\n── bash: single quotes ──")
        r = await tools.bash("echo 'hello world'", __user__=user, __event_emitter__=mock_emitter)
        R.check("single quotes work", "hello world" in r, r[:200])

        print("\n── bash: mixed quoting ──")
        r = await tools.bash("""echo "it's a 'test'" && echo 'say "hello"'""", __user__=user, __event_emitter__=mock_emitter)
        R.check("mixed quotes work", "it's a 'test'" in r and 'say "hello"' in r, r[:200])

        print("\n── bash: backslashes ──")
        r = await tools.bash(r"echo 'back\slash'", __user__=user, __event_emitter__=mock_emitter)
        R.check("backslashes preserved", "back\\slash" in r, r[:200])

    async def t_vars():
        print("\n── bash: dollar signs and variables ──")
        r = await tools.bash('FOO=bar && echo "val=$FOO"', __user__=user, __event_emitter__=mock_emitter)
        R.check("variable expansion works", "val=bar" in r, r[:200])

    async def t_set_e():
        print("\n── bash: set -e aborts on error ──")
        r = await tools.bash("false\necho should-not-reach", __user__=user, __event_emitter__=mock_emitter)
        R.check("set -e aborts on first failure", "Exit code:" in r, r[:200])
        R.check("second command did not run", "should-not-reach" not in r, r[:200])

        print("\n── bash: pipefail catches pipe errors ──")
        r = await tools.bash("false | cat", __user__=user, __event_emitter__=mock_emitter)
        R.check("pipefail reports failure", "Exit code:" in r, r[:200])

        print("\n── bash: || true overrides set -e ──")
        r = await tools.bash("false || true\necho survived", __user__=user, __event_emitter__=mock_emitter)
        R.check("|| true suppresses abort", "survived" in r, r[:200])

    # First call creates sandbox; after that everything can fan out
    await t_simple()
    await asyncio.gather(t_compound(), t_pipes(), t_exit(), t_workdir(), t_quoting(), t_vars(), t_set_e())


async def test_int_write_read_edit(R: Results, tools: Tools, user: dict):

    print("\n── write: create file ──")
    test_content = "line one\nline two\nline three\n"
    r = await tools.write("workspace/test_file.txt", test_content, __user__=user, __event_emitter__=mock_emitter)
    R.check("write reports success", "Wrote" in r and "test_file.txt" in r, r[:200])

    print("\n── read: full file ──")
    r = await tools.read("workspace/test_file.txt", __user__=user, __event_emitter__=mock_emitter)
    R.check("read returns content", "line one" in r and "line two" in r, r[:200])
    R.check("read has line numbers", "1: line one" in r, r[:200])
    R.check("read shows total lines", "3 lines total" in r, r[:200])

    # Independent reads
    async def t_offset():
        print("\n── read: offset and limit ──")
        r = await tools.read("workspace/test_file.txt", offset=2, limit=1, __user__=user, __event_emitter__=mock_emitter)
        R.check("offset/limit works", "2: line two" in r, r[:200])
        R.check("respects limit", "line three" not in r, r[:200])

    async def t_notfound():
        print("\n── read: file not found ──")
        r = await tools.read("workspace/nonexistent.txt", __user__=user, __event_emitter__=mock_emitter)
        R.check("reports file not found", "Error" in r or "not found" in r.lower(), r[:200])

    await asyncio.gather(t_offset(), t_notfound())

    print("\n── edit: single replacement ──")
    r = await tools.edit("workspace/test_file.txt", "line two", "LINE TWO EDITED", __user__=user, __event_emitter__=mock_emitter)
    R.check("edit reports success", "Replaced 1" in r, r[:200])

    r = await tools.read("workspace/test_file.txt", __user__=user, __event_emitter__=mock_emitter)
    R.check("edit persisted", "LINE TWO EDITED" in r, r[:200])
    R.check("other lines untouched", "line one" in r and "line three" in r, r[:200])

    # Independent edit tests
    async def t_edit_notfound():
        print("\n── edit: old_string not found ──")
        r = await tools.edit("workspace/test_file.txt", "this text does not exist", "replacement", __user__=user, __event_emitter__=mock_emitter)
        R.check("reports not found", "not found" in r.lower(), r[:200])

    async def t_edit_multi():
        print("\n── edit: multiple matches without replace_all ──")
        await tools.write("workspace/dup_test.txt", "aaa\nbbb\naaa\nbbb\naaa\n", __user__=user, __event_emitter__=mock_emitter)
        r = await tools.edit("workspace/dup_test.txt", "aaa", "zzz", __user__=user, __event_emitter__=mock_emitter)
        R.check("rejects ambiguous edit", "3 matches" in r or "multiple" in r.lower(), r[:200])

        print("\n── edit: replace_all ──")
        r = await tools.edit("workspace/dup_test.txt", "aaa", "zzz", replace_all=True, __user__=user, __event_emitter__=mock_emitter)
        R.check("replace_all reports count", "Replaced 3" in r, r[:200])
        r = await tools.read("workspace/dup_test.txt", __user__=user, __event_emitter__=mock_emitter)
        R.check("replace_all applied", "aaa" not in r and "zzz" in r, r[:200])

    async def t_edit_missing():
        print("\n── edit: file not found ──")
        r = await tools.edit("workspace/nonexistent.txt", "foo", "bar", __user__=user, __event_emitter__=mock_emitter)
        R.check("edit on missing file errors", "Error" in r or "not found" in r.lower(), r[:200])

    async def t_nested():
        print("\n── write: nested path ──")
        r = await tools.write("workspace/deep/nested/dir/file.txt", "nested content\n", __user__=user, __event_emitter__=mock_emitter)
        R.check("write to nested path succeeds", "Wrote" in r, r[:200])
        r = await tools.read("workspace/deep/nested/dir/file.txt", __user__=user, __event_emitter__=mock_emitter)
        R.check("nested file readable", "nested content" in r, r[:200])

    await asyncio.gather(t_edit_notfound(), t_edit_multi(), t_edit_missing(), t_nested())

    # cleanup
    await tools.bash("rm -rf workspace/test_file.txt workspace/dup_test.txt workspace/deep", __user__=user, __event_emitter__=mock_emitter)


async def test_int_attach(R: Results, tools: Tools, user: dict):
    from fastapi.responses import HTMLResponse
    import base64
    import re
    import struct
    import zlib as _zlib

    # Setup: create test files concurrently
    py_content = 'def greet(name):\n    return f"Hello, {name}!"\n\nprint(greet("world"))\n'
    svg_content = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect fill="red" width="10" height="10"/></svg>'

    def _make_tiny_png():
        def _chunk(ctype, data):
            c = ctype + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", _zlib.crc32(c) & 0xFFFFFFFF)
        sig = b"\x89PNG\r\n\x1a\n"
        ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        raw_row = b"\x00\xff\x00\x00"
        idat = _chunk(b"IDAT", _zlib.compress(raw_row))
        iend = _chunk(b"IEND", b"")
        return sig + ihdr + idat + iend

    png_b64 = base64.b64encode(_make_tiny_png()).decode()

    await asyncio.gather(
        tools.write("workspace/attach_test.py", py_content, __user__=user, __event_emitter__=mock_emitter),
        tools.write("workspace/attach_test.txt", "just plain text\nno highlighting\n", __user__=user, __event_emitter__=mock_emitter),
        tools.write("workspace/test_image.svg", svg_content, __user__=user, __event_emitter__=mock_emitter),
        tools.bash(f"echo '{png_b64}' | base64 -d > /home/daytona/workspace/test_image.png", __user__=user, __event_emitter__=mock_emitter),
        tools.bash("cd /home/daytona/workspace && echo 'hello from zip' > _zipme.txt && zip test_archive.zip _zipme.txt && rm _zipme.txt", __user__=user, __event_emitter__=mock_emitter),
        tools.bash(r"printf '\x80\x81\x82\xff\xfe\xfd' > /home/daytona/workspace/test_binary.dat", __user__=user, __event_emitter__=mock_emitter),
    )

    # Now run all attach tests concurrently
    async def t_py():
        print("\n── attach: Python file ──")
        result = await tools.attach("workspace/attach_test.py", __user__=user, __event_emitter__=mock_emitter)
        R.check("returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        body = result.body.decode("utf-8")
        R.check("has Content-Disposition inline", result.headers.get("content-disposition") == "inline", result.headers.get("content-disposition", ""))
        R.check("contains filename in header", "attach_test.py" in body, "")
        R.check("contains line count", "4 lines" in body, "")
        R.check("contains byte count", str(len(py_content.encode("utf-8"))) in body, "")
        R.check("contains height reporting script", "iframe:height" in body, "")
        R.check("contains copy button", "Copy" in body and "copyFile" in body, "")
        R.check("contains save button", "Save" in body and "saveFile" in body, "")

        print("\n── attach: syntax highlighting ──")
        R.check("has Pygments highlighting spans", 'style="' in body and "<span" in body, "no inline styles found")
        R.check("contains the function content", "greet" in body, "")

        print("\n── attach: base64 round-trip ──")
        b64_match = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', body)
        R.check("base64 payload present", b64_match is not None, "no atob() found")
        if b64_match:
            decoded = base64.b64decode(b64_match.group(1)).decode("utf-8")
            R.check("base64 decodes to original content", decoded == py_content, f"got {decoded[:80]}...")

    async def t_txt():
        print("\n── attach: plain text file ──")
        result = await tools.attach("workspace/attach_test.txt", __user__=user, __event_emitter__=mock_emitter)
        R.check("txt returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        body_txt = result.body.decode("utf-8")
        R.check("txt contains content", "just plain text" in body_txt, "")
        R.check("txt contains filename", "attach_test.txt" in body_txt, "")

    async def t_notfound():
        print("\n── attach: file not found ──")
        result = await tools.attach("workspace/nonexistent_file.xyz", __user__=user, __event_emitter__=mock_emitter)
        R.check("missing file returns error string", isinstance(result, str) and "Error" in result, str(result)[:200])

    async def t_png():
        print("\n── attach: PNG image ──")
        result = await tools.attach("workspace/test_image.png", __user__=user, __event_emitter__=mock_emitter)
        R.check("image returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        if not isinstance(result, HTMLResponse):
            return
        img_body = result.body.decode("utf-8")
        R.check("image has <img> tag", "<img " in img_body, "")
        R.check("image has data URI", "data:image/png;base64," in img_body, "")
        R.check("image has filename", "test_image.png" in img_body, "")
        R.check("image has Save button", "Save" in img_body and "saveFile" in img_body, "")
        R.check("image does NOT have Copy button", "copyFile" not in img_body, "image shouldn't have Copy")
        R.check("image does NOT have line numbers", "gutter" not in img_body, "image shouldn't have line gutter")
        R.check("image has height reporting", "iframe:height" in img_body, "")

    async def t_svg():
        print("\n── attach: SVG image ──")
        result = await tools.attach("workspace/test_image.svg", __user__=user, __event_emitter__=mock_emitter)
        R.check("SVG returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        if not isinstance(result, HTMLResponse):
            return
        svg_body = result.body.decode("utf-8")
        R.check("SVG renders as image not code", "<img " in svg_body, "SVG should render as <img>")
        R.check("SVG has correct MIME", "image/svg+xml" in svg_body, "")

    async def t_zip():
        print("\n── attach: ZIP binary file ──")
        result = await tools.attach("workspace/test_archive.zip", __user__=user, __event_emitter__=mock_emitter)
        R.check("zip returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        zip_body = result.body.decode("utf-8")
        R.check("zip shows filename", "test_archive.zip" in zip_body, "")
        R.check("zip shows file type", "ZIP" in zip_body, "")
        R.check("zip has download card (no code viewer)", "gutter" not in zip_body, "binary shouldn't have line gutter")
        R.check("zip has Save button (under 10MB)", "saveFile" in zip_body, "small zip should be downloadable")
        R.check("zip does NOT have Copy button", "copyFile" not in zip_body, "binary shouldn't have Copy")
        R.check("zip has height reporting", "iframe:height" in zip_body, "")

    async def t_dat():
        print("\n── attach: binary by content (not extension) ──")
        result = await tools.attach("workspace/test_binary.dat", __user__=user, __event_emitter__=mock_emitter)
        R.check("non-UTF8 dat returns HTMLResponse", isinstance(result, HTMLResponse), type(result).__name__)
        if not isinstance(result, HTMLResponse):
            return
        dat_body = result.body.decode("utf-8")
        R.check("non-UTF8 renders as binary card", "gutter" not in dat_body and "<img" not in dat_body, "should be download card")
        R.check("non-UTF8 has Save button", "saveFile" in dat_body, "small binary should be downloadable")

    await asyncio.gather(t_py(), t_txt(), t_notfound(), t_png(), t_svg(), t_zip(), t_dat())

    # cleanup
    await tools.bash("rm -f workspace/attach_test.py workspace/attach_test.txt workspace/test_image.png workspace/test_image.svg workspace/test_archive.zip workspace/test_binary.dat", __user__=user, __event_emitter__=mock_emitter)


async def test_int_onboard(R: Results, tools: Tools, user: dict):

    await tools.bash("rm -rf /home/daytona/workspace/test_project /home/daytona/workspace/empty_project", __user__=user, __event_emitter__=mock_emitter)

    print("\n── onboard: missing context ──")
    r = await tools.onboard("/home/daytona/workspace/empty_project", __user__=user, __event_emitter__=mock_emitter)
    R.check("fails without AGENTS.md or .agents/", "Error" in r, r[:200])

    print("\n── onboard: AGENTS.md only ──")
    await tools.write("workspace/test_project/AGENTS.md", "# Test Agent\nYou are a helpful test agent.\n", __user__=user, __event_emitter__=mock_emitter)
    r = await tools.onboard("/home/daytona/workspace/test_project", __user__=user, __event_emitter__=mock_emitter)
    R.check("returns AGENTS.md content", "helpful test agent" in r, r[:300])
    R.check("no skills section when none exist", "Available Skills" not in r, r[:300])

    print("\n── onboard: with skills ──")
    await tools.write(
        "workspace/test_project/.agents/skills/test-skill/SKILL.md",
        "---\nname: test-skill\ndescription: A skill for testing things.\n---\n\n# Test Skill\nDetailed instructions here.\n",
        __user__=user, __event_emitter__=mock_emitter,
    )
    r = await tools.onboard("/home/daytona/workspace/test_project", __user__=user, __event_emitter__=mock_emitter)
    R.check("returns AGENTS.md", "helpful test agent" in r, r[:500])
    R.check("lists skill name", "test-skill" in r, r[:500])
    R.check("lists skill description", "testing things" in r, r[:500])
    R.check("includes SKILL.md path", "SKILL.md" in r, r[:500])
    R.check("does NOT include skill body", "Detailed instructions here" not in r, r[:500])

    await tools.bash("rm -rf /home/daytona/workspace/test_project /home/daytona/workspace/empty_project", __user__=user, __event_emitter__=mock_emitter)


async def test_int_truncation(R: Results, tools: Tools, user: dict):
    import re

    print("\n── bash: output truncation with spill file ──")
    result = await tools.bash(
        "for i in $(seq 1 3000); do echo \"output line $i\"; done",
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("truncated output has notice", "[Showing lines" in result, result[-200:])
    R.check("notice mentions full output file", "/tmp/_bash_output_" in result, result[-200:])
    R.check("last line present in output", "output line 3000" in result, result[-200:])
    R.check("first line NOT in truncated output", "output line 1\n" not in result, "line 1 should be truncated away")

    spill_match = re.search(r"/tmp/_bash_output_\w+\.log", result)
    if spill_match:
        spill_path = spill_match.group(0)
        verify, head_result = await asyncio.gather(
            tools.bash(f"wc -l < {spill_path}", __user__=user, __event_emitter__=mock_emitter),
            tools.bash(f"head -n 3 {spill_path}", __user__=user, __event_emitter__=mock_emitter),
        )
        R.check("spill file has all 3000 lines", "3000" in verify, verify.strip())
        R.check("can retrieve early lines from spill file", "output line 1" in head_result, head_result[:100])
    else:
        R.check("spill file path found in notice", False, "no path match found")

    print("\n── bash: small output NOT truncated ──")
    result = await tools.bash("echo hello", __user__=user, __event_emitter__=mock_emitter)
    R.check("small output has no truncation notice", "[Showing lines" not in result, result[:200])

    await tools.bash("rm -f /tmp/_bash_output_*.log", __user__=user, __event_emitter__=mock_emitter)


async def test_int_env_vars(R: Results, tools: Tools, user: dict):

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
        r = await tools.bash("echo still_works", __user__=user, __event_emitter__=mock_emitter)
        R.check("no valves key still runs", "still_works" in r, r[:200])

    await asyncio.gather(t_basic(), t_tricky(), t_empty(), t_no_valves())


async def test_int_ensure_sandbox(R: Results, tools: Tools, user: dict):
    import httpx
    import json as _json
    from lathe import _headers, _ensure_sandbox

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("\n── _ensure_sandbox: identity ──")
        sandbox_id = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
        R.check("returns a sandbox id", sandbox_id and isinstance(sandbox_id, str), repr(sandbox_id))
        sandbox_id_2 = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
        R.check("same sandbox on repeat call", sandbox_id == sandbox_id_2, f"{sandbox_id[:12]} != {sandbox_id_2[:12]}")

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

        sandbox_id_3 = await _ensure_sandbox(tools.valves, TEST_EMAIL, client, emitter=mock_emitter)
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


async def test_int_ssh(R: Results, tools: Tools, user: dict):

    print("\n── ssh: invalid expires_in_minutes (too low) ──")
    r = await tools.ssh(expires_in_minutes=0, __user__=user, __event_emitter__=mock_emitter)
    R.check("expires_in_minutes=0 rejected", "Error" in r and "1" in r, r[:200])

    print("\n── ssh: invalid expires_in_minutes (too high) ──")
    r = await tools.ssh(expires_in_minutes=1441, __user__=user, __event_emitter__=mock_emitter)
    R.check("expires_in_minutes=1441 rejected", "Error" in r and "1440" in r, r[:200])

    print("\n── ssh: generate SSH access (default 60 min) ──")
    r = await tools.ssh(__user__=user, __event_emitter__=mock_emitter)
    R.check("ssh returns command", "ssh" in r.lower(), r[:300])
    R.check("mentions validity", "60 min" in r, r[:300])
    R.check("contains ssh command block", "```" in r, r[:300])

    print("\n── ssh: generate SSH access (custom 30 min) ──")
    r = await tools.ssh(expires_in_minutes=30, __user__=user, __event_emitter__=mock_emitter)
    R.check("custom expiry returns command", "ssh" in r.lower(), r[:300])
    R.check("mentions 30 min", "30 min" in r, r[:300])


async def test_int_preview(R: Results, tools: Tools, user: dict):

    print("\n── preview: invalid port (too low) ──")
    r = await tools.preview(port=80, __user__=user, __event_emitter__=mock_emitter)
    R.check("port 80 rejected", "Error" in r and "3000" in r, r[:200])

    print("\n── preview: invalid port (too high) ──")
    r = await tools.preview(port=10000, __user__=user, __event_emitter__=mock_emitter)
    R.check("port 10000 rejected", "Error" in r and "9999" in r, r[:200])

    print("\n── preview: start a server then get preview URL ──")
    # Start a simple HTTP server in the background on port 8080
    await tools.bash(
        "python3 -m http.server 8080 &",
        __user__=user,
        __event_emitter__=mock_emitter,
    )
    # Give the server a moment to start
    import asyncio as _asyncio
    await _asyncio.sleep(1)

    r = await tools.preview(port=8080, __user__=user, __event_emitter__=mock_emitter)
    R.check("preview returns URL", "Preview URL" in r, r[:300])
    R.check("URL contains https://", "https://" in r, r[:300])
    R.check("mentions 1 hour validity", "1 hour" in r, r[:300])
    R.check("mentions Daytona warning", "warning" in r.lower(), r[:300])

    print("\n── preview: default port ──")
    r = await tools.preview(__user__=user, __event_emitter__=mock_emitter)
    # Port 3000 may not have a listener, but the API should still return a URL
    R.check("default port returns URL", "Preview URL" in r or "Error" in r, r[:300])

    # Clean up the background server
    await tools.bash("pkill -f 'http.server 8080' || true", __user__=user, __event_emitter__=mock_emitter)


async def test_int_destroy(R: Results, tools: Tools, user: dict):
    from lathe import _headers, VOLUME_MOUNT_PATH

    print("\n── destroy: safety guard (confirm=false) ──")
    result = await tools.destroy(__user__=user, __event_emitter__=mock_emitter)
    R.check("default confirm=false aborts", "aborted" in result.lower(), result[:200])
    R.check("abort message mentions confirm", "confirm" in result.lower(), result[:200])

    result = await tools.destroy(confirm=False, __user__=user, __event_emitter__=mock_emitter)
    R.check("explicit confirm=false aborts", "aborted" in result.lower(), result[:200])

    # Verify sandbox still exists after abort
    result = await tools.bash("echo still_alive", __user__=user, __event_emitter__=mock_emitter)
    R.check("sandbox survives abort", "still_alive" in result, result[:200])

    print("\n── destroy: wipes sandbox but preserves volume ──")
    # Write a marker file to the volume before destroying
    result = await tools.bash(
        f"echo destroy_test > {VOLUME_MOUNT_PATH}/destroy_test.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("write marker to volume", "Error" not in result, result[:200])

    result = await tools.destroy(confirm=True, __user__=user, __event_emitter__=mock_emitter)
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
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("volume data survives destroy", "destroy_test" in result, result[:200])

    print("\n── destroy: wipe_volume deletes volume and data ──")
    # Write another marker to the volume
    result = await tools.bash(
        f"echo wipe_test > {VOLUME_MOUNT_PATH}/wipe_test.txt",
        __user__=user, __event_emitter__=mock_emitter,
    )
    result = await tools.destroy(
        confirm=True, wipe_volume=True,
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("wipe_volume destroy succeeds", "Destroyed" in result, result[:200])
    R.check("wipe_volume mentions volume deleted", "volume also deleted" in result.lower(), result[:200])

    # Next tool call should create a fresh volume + sandbox; old data should be gone
    result = await tools.bash(
        f"cat {VOLUME_MOUNT_PATH}/wipe_test.txt 2>&1 || true",
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("volume data gone after wipe", "No such file" in result, result[:200])

    print("\n── destroy: no sandbox to destroy ──")
    result = await tools.destroy(confirm=True, __user__=user, __event_emitter__=mock_emitter)
    # First destroy the sandbox that was just created
    # Then try again with nothing left
    result = await tools.destroy(confirm=True, __user__=user, __event_emitter__=mock_emitter)
    R.check("destroy with nothing reports no sandbox", "No sandbox found" in result, result[:200])

    print("\n── destroy: next tool call creates fresh sandbox ──")
    result = await tools.bash("echo reborn", __user__=user, __event_emitter__=mock_emitter)
    R.check("fresh sandbox works", "reborn" in result, result[:200])

    print("\n── final cleanup: destroy reborn sandbox ──")
    result = await tools.destroy(
        confirm=True, wipe_volume=True,
        __user__=user, __event_emitter__=mock_emitter,
    )
    R.check("final destroy succeeds", "Destroyed" in result, result[:200])


# ── Test group registry ──────────────────────────────────────────────

UNIT_GROUPS = {
    "parse_env_vars": test_unit_parse_env_vars,
    "classify_file": test_unit_classify_file,
    "sniff_media": test_unit_sniff_media,
    "render_media_html": test_unit_render_media_html,
    "render_html": test_unit_render_html,
    "highlight": test_unit_highlight,
    "truncate": test_unit_truncate,
}

INTEGRATION_GROUPS = {
    "bash": test_int_bash,
    "write_read_edit": test_int_write_read_edit,
    "attach": test_int_attach,
    "onboard": test_int_onboard,
    "truncation": test_int_truncation,
    "env_vars": test_int_env_vars,
    "ssh": test_int_ssh,
    "preview": test_int_preview,
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

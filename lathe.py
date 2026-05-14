"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (lathe, bash, read, write, edit, glob, grep, interpret, delegate, onboard, expose, destroy) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx, httpx-ws, pydantic-ai-slim[openai], cachetools
version: 0.23.0
licence: MIT
"""

import asyncio
import inspect
import io
import json
import logging
import re
import textwrap
import time
import typing
import urllib.parse
import uuid

import httpx
from cachetools import LRUCache
from pydantic import BaseModel, Field


logger = logging.getLogger("lathe")

# ── module-level helpers (invisible to OWUI tool discovery) ──────────
#
# OWUI discovers tools by calling dir() on the Tools instance and
# keeping every callable whose name doesn't start with "_".  The
# underscore filter was only added in Mar 2026 (PR #22408), so older
# deployments expose *all* methods as tools.  To stay safe across
# versions, keep helpers at module scope — OWUI never introspects the
# module, only the class.


def _build_tool_catalog(tools_instance) -> str:
    """Introspect a Tools instance to produce a tool summary table.

    Skips private methods and the lathe() tool itself so the catalog
    describes only the "real" tools the model can call.
    """
    lines = []
    for name, method in inspect.getmembers(tools_instance, predicate=inspect.ismethod):
        if name.startswith("_") or name == "lathe":
            continue
        sig = inspect.signature(method)
        params = [
            p.name for p in sig.parameters.values()
            if not p.name.startswith("__")
        ]
        doc = inspect.getdoc(method) or ""
        # First sentence of the docstring as the summary
        summary = doc.split("\n")[0].rstrip(".") if doc else "(no description)"
        param_str = ", ".join(params) if params else ""
        lines.append(f"  {name}({param_str}) — {summary}")
    return "\n".join(sorted(lines))


def _headers(valves) -> dict:
    return {
        "Authorization": f"Bearer {valves.daytona_api_key}",
        "Content-Type": "application/json",
    }


def _api(valves, path: str) -> str:
    return f"{valves.daytona_api_url.rstrip('/')}{path}"


def _toolbox(valves, sandbox_id: str, path: str) -> str:
    return f"{valves.daytona_proxy_url.rstrip('/')}/{sandbox_id}{path}"


async def _emit(emitter, description: str, done: bool = False):
    if emitter:
        await emitter(
            {
                "type": "status",
                "data": {"description": description, "done": done},
            }
        )


async def _tool_context(emitter, fn):
    """Open a shared HTTP client, call fn(client), catch standard tool exceptions."""
    try:
        async with httpx.AsyncClient() as client:
            return await fn(client)
    except RuntimeError as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"
    except httpx.HTTPStatusError as e:
        await _emit(emitter, f"API error: HTTP {e.response.status_code}", done=True)
        return f"API error: HTTP {e.response.status_code} — {e.response.text[:500]}"
    except Exception as e:
        await _emit(emitter, f"Error: {e}", done=True)
        return f"Error: {e}"


def _prepend_harness_messages(result: str, messages: list[str]) -> str:
    """Prepend harness-injected context to a tool result.

    Harness messages are things the infrastructure decides to push to the
    agent alongside the tool's own output: sandbox lifecycle warnings,
    auto-init snapshots, and (future) background job completion notices.
    Each message is separated by a blank line for readability.
    """
    if not messages:
        return result
    prefix = "\n\n".join(messages)
    return f"{prefix}\n\n{result}"


def _drain_harness_messages(chat_state: LRUCache, chat_id: str | None,
                            warning: str | None) -> list[str]:
    """Collect pending harness messages and the sandbox lifecycle warning.

    Runs after every tool call, making it a natural hook for future
    trace accumulation (#54).
    """
    messages: list[str] = []
    if warning:
        messages.append(warning)
    if chat_id and chat_id in chat_state:
        state = chat_state[chat_id]
        pending = state.get("pending", [])
        if pending:
            messages.extend(pending)
            state["pending"] = []
    return messages


def _push_bg_notice(chat_state: LRUCache, chat_id: str | None, notice: str):
    """Push a background job completion notice into the chat's pending queue.

    Safe to call even if the chat_id has been evicted from the LRU cache
    (silently dropped — if the chat is that old, nobody is waiting).
    """
    if not chat_id or chat_id not in chat_state:
        return
    state = chat_state[chat_id]
    pending = state.get("pending")
    if pending is None:
        state["pending"] = [notice]
    else:
        pending.append(notice)


def _format_bg_bash_notice(cmd_id: str, exit_code: int | None,
                           elapsed: int, tail: str) -> str:
    ec_str = str(exit_code) if exit_code is not None else "unknown"
    lines = [
        f"[Background job completed: CMD-{cmd_id}]",
        f"Exit code: {ec_str} (took {elapsed}s)",
    ]
    tail = tail.strip()
    if tail:
        lines.append("Last lines of output:")
        lines.extend(f"  {tl}" for tl in tail.splitlines()[-5:])
    else:
        lines.append("(no output)")
    return "\n".join(lines)


def _format_bg_delegate_notice(delegate_id: str, elapsed: int,
                               step_count: int, tool_calls: int,
                               result_preview: str, error: str | None) -> str:
    if error:
        lines = [
            f"[Background delegation failed: DELEGATE-{delegate_id}]",
            f"Failed after {elapsed}s: {error}",
        ]
    else:
        lines = [
            f"[Background delegation completed: DELEGATE-{delegate_id}]",
            f"{step_count} step(s), {tool_calls} tool call(s) (took {elapsed}s)",
        ]
        preview = result_preview.strip()
        if preview:
            lines.append("Result preview:")
            lines.extend(f"  {pl}" for pl in preview.splitlines()[-10:])
        else:
            lines.append("(no result text)")
    return "\n".join(lines)


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_env_vars(env_vars: str) -> list[tuple[str, str]]:
    """Parse a JSON object string into (key, value) pairs.

    Expects a JSON object mapping string keys to string values,
    e.g. '{"MY_TOKEN":"abc123","FOO":"bar"}'.
    Keys must match [A-Za-z_][A-Za-z0-9_]*; invalid keys are skipped with a warning.
    Returns [] on empty input (not an error).
    Raises ValueError on malformed input so the caller can surface it to the agent.
    """
    s = env_vars.strip()
    if not s or s == "{}":
        return []
    try:
        mapping = json.loads(s)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"UserValves env_vars is not valid JSON: {exc}. "
            f"Fix the env_vars field in your tool settings (it should look like "
            f'{{\"MY_TOKEN\":\"abc123\"}}) and retry.'
        ) from exc
    if not isinstance(mapping, dict):
        raise ValueError(
            f"UserValves env_vars must be a JSON object, got {type(mapping).__name__}. "
            f'Expected something like {{\"MY_TOKEN\":\"abc123\"}}.'
        )
    pairs: list[tuple[str, str]] = []
    skipped: list[str] = []
    for key, value in mapping.items():
        if not isinstance(key, str) or not isinstance(value, str):
            skipped.append(repr(key))
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            skipped.append(repr(key))
            continue
        pairs.append((key, value))
    if skipped:
        raise ValueError(
            f"UserValves env_vars contains invalid keys or non-string values: "
            f"{', '.join(skipped)}. Keys must match [A-Za-z_][A-Za-z0-9_]*."
        )
    return pairs


def _get_email(user: dict) -> str:
    email = user.get("email", "")
    if not email:
        raise RuntimeError("No email found for user. Cannot provision sandbox.")
    return email


def _extract_pid(output: str) -> str:
    """Extract PID=<number> from ensure-script output. Returns the number or '?'."""
    m = re.search(r"PID=(\d+)", output)
    return m.group(1) if m else "?"


def _require_abs_path(path: str, param_name: str = "path") -> str | None:
    """Return an error string if *path* is not absolute, else None."""
    if not path.startswith("/"):
        return (
            f"Error: {param_name} must be an absolute path "
            f"(e.g. /home/daytona/workspace/file.txt). Got: {path}"
        )
    return None


# ── output truncation (tail-biased, mirrors Pi's design) ────────────

_MAX_LINES = 2000
_MAX_BYTES = 50 * 1024  # 50 KB


def _truncate_tail(text: str) -> tuple[str, bool, dict]:
    """Truncate output keeping the *tail* (where errors and results live).

    Returns (output, was_truncated, metadata).
    Metadata keys when truncated:
      total_lines, total_bytes, shown_start_line, shown_end_line, truncated_by
    """
    total_bytes = len(text.encode("utf-8"))
    lines = text.split("\n")
    total_lines = len(lines)

    if total_lines <= _MAX_LINES and total_bytes <= _MAX_BYTES:
        return text, False, {}

    # Walk backwards, collecting complete lines within both limits
    kept: list[str] = []
    kept_bytes = 0
    truncated_by = "lines"

    for i in range(total_lines - 1, -1, -1):
        line = lines[i]
        line_bytes = len(line.encode("utf-8")) + (1 if kept else 0)  # +1 for \n joiner
        if kept_bytes + line_bytes > _MAX_BYTES:
            truncated_by = "bytes"
            break
        kept.append(line)
        kept_bytes += line_bytes
        if len(kept) >= _MAX_LINES:
            truncated_by = "lines"
            break

    kept.reverse()
    output = "\n".join(kept)
    shown_lines = len(kept)
    start_line = total_lines - shown_lines + 1

    meta = {
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "shown_start_line": start_line,
        "shown_end_line": total_lines,
        "truncated_by": truncated_by,
    }
    return output, True, meta


def _human_size(n: int) -> str:
    """Format byte count as human-readable string."""
    b = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:,.0f} {unit}" if unit == "B" else f"{b:,.1f} {unit}"
        b /= 1024
    return f"{b:,.1f} TB"


# ── shared glob infrastructure (runs on sandbox) ────────────────────
#
# Both _GLOB_SCRIPT and _GREP_SCRIPT need to parse comma-separated
# glob patterns, resolve absolute vs. relative patterns, and collect
# matching files.  This shared fragment is prepended to both scripts
# so the logic lives in one place.

_GLOB_COMMON = r'''
import os
import sys
from pathlib import Path


def _parse_pattern(pattern):
    """Parse comma-separated glob string into (positive, negative) lists.

    Commas inside {braces} are part of glob syntax, not delimiters.
    !-prefixed terms go into the negative list.
    """
    terms, current, depth = [], [], 0
    for ch in pattern:
        if ch == "{": depth += 1
        elif ch == "}": depth -= 1
        elif ch == "," and depth == 0:
            terms.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    terms.append("".join(current).strip())
    positive, negative = [], []
    for term in terms:
        if not term:
            continue
        if term.startswith("!"):
            negative.append(term[1:])
        else:
            positive.append(term)
    return positive, negative


def _resolve_glob(base, g):
    """Return (root, relative_pattern) for a glob term.

    Python 3.14 rejects non-relative patterns in Path.glob().
    For relative patterns, root is the workspace base.
    For absolute patterns, we extract the longest non-glob prefix
    as the root and glob relative to it, so the agent can glob
    anywhere on the filesystem, not just within the workspace.
    """
    if not g.startswith("/"):
        return base, g
    parts = g.split(os.sep)
    root_parts = []
    for i, part in enumerate(parts):
        if any(c in part for c in ("*", "?", "[", "{")):
            break
        root_parts.append(part)
    else:
        root_dir = Path(g).resolve()
        return root_dir, "**/*"
    root_dir = Path(os.sep.join(root_parts) or os.sep).resolve()
    rel = os.sep.join(parts[i:])
    return root_dir, rel


def _collect_files(base, globs):
    """Glob all patterns and return a set of resolved absolute file paths."""
    result = set()
    for g in globs:
        root, rel = _resolve_glob(base, g)
        if not root.is_dir():
            continue
        for p in root.glob(rel):
            if p.is_file():
                result.add(str(p.resolve()))
    return result
'''

# ── hierarchical glob (runs on sandbox) ──────────────────────────────

_GLOB_MAX_LINES = 100

_GLOB_SCRIPT = _GLOB_COMMON + r'''


def glob_hierarchy(base_dir, pattern, max_lines):
    base = Path(base_dir).resolve()
    if not base.is_dir():
        return f"Error: not a directory: {base_dir}"

    positive, negative = _parse_pattern(pattern)
    if not positive:
        return f"Error: pattern must include at least one positive glob (got {pattern!r})"

    included = _collect_files(base, positive)
    if negative:
        included -= _collect_files(base, negative)
    matches = sorted(included)

    if not matches:
        return f"0 matches for {pattern!r} in {base}"

    # ── Compute effective base for trie rendering ────────────────
    # When all results are under the workspace, effective_base == base.
    # When results span other directories, effective_base is their
    # longest common directory prefix, so the trie stays coherent.
    if all(m.startswith(str(base) + os.sep) for m in matches):
        effective_base = str(base)
    else:
        effective_base = os.path.commonpath(matches)
        # commonpath may return a file prefix — ensure it's a directory
        if not os.path.isdir(effective_base):
            effective_base = os.path.dirname(effective_base)

    # ── Build trie ───────────────────────────────────────────────
    root = {"children": {}, "files": [], "count": 0, "path": effective_base}

    for filepath in matches:
        rel = os.path.relpath(filepath, effective_base)
        parts = rel.split(os.sep)
        node = root
        node["count"] += 1
        for part in parts[:-1]:
            if part not in node["children"]:
                node["children"][part] = {
                    "children": {},
                    "files": [],
                    "count": 0,
                    "path": os.path.join(node["path"], part),
                }
            node = node["children"][part]
            node["count"] += 1
        node["files"].append(filepath)

    # ── Budget-driven expansion ──────────────────────────────────
    expanded = {id(root)}
    partial_limit = {}

    def _ordered_children(node):
        items = []
        for name in sorted(node["children"],
                           key=lambda n: node["children"][n]["count"],
                           reverse=True):
            items.append(("dir", node["children"][name]))
        for f in sorted(node["files"]):
            items.append(("file", f))
        return items

    def _count_lines(node):
        if id(node) not in expanded:
            return 1
        limit = partial_limit.get(id(node))
        if limit is not None:
            items = _ordered_children(node)
            if limit < len(items):
                total = 1  # the "... and N more" line
                for kind, item in items[:limit]:
                    total += 1 if kind == "file" else _count_lines(item)
                return total
        total = len(node["files"])
        for child in node["children"].values():
            total += _count_lines(child)
        return total

    def _collapsible(node):
        if id(node) not in expanded:
            yield (node["count"], node)
            return
        for child in node["children"].values():
            yield from _collapsible(child)

    current_lines = _count_lines(root)

    while current_lines < max_lines:
        candidates = list(_collapsible(root))
        if not candidates:
            break

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_count, best_node = candidates[0]
        n_children = len(best_node["files"]) + len(best_node["children"])

        if n_children <= 1:
            expanded.add(id(best_node))
            current_lines = _count_lines(root)
            continue

        net_cost = n_children - 1
        if current_lines + net_cost <= max_lines:
            expanded.add(id(best_node))
            current_lines = _count_lines(root)
            continue

        # Partial expansion
        budget_for_children = max_lines - current_lines
        if budget_for_children < 2:
            break
        expanded.add(id(best_node))
        partial_limit[id(best_node)] = budget_for_children
        current_lines = _count_lines(root)
        break

    # ── Render ───────────────────────────────────────────────────
    output = []

    def render(node):
        if id(node) not in expanded:
            output.append(f"{node['path']}/ ({node['count']} matches)")
            return

        items = _ordered_children(node)
        limit = partial_limit.get(id(node))

        if limit is not None and limit < len(items):
            shown = items[:limit]
            hidden_count = sum(
                1 if k == "file" else item["count"]
                for k, item in items[limit:]
            )
            for kind, item in shown:
                if kind == "file":
                    output.append(item)
                else:
                    render(item)
            output.append(
                f"{node['path']}/ ... and {hidden_count} more matches"
            )
        else:
            for f in sorted(node["files"]):
                output.append(f)
            for name in sorted(node["children"]):
                render(node["children"][name])

    render(root)

    header = f"{len(matches)} matches for {pattern!r} in {effective_base}"
    has_collapsed = bool(list(_collapsible(root))) or bool(partial_limit)
    if has_collapsed:
        header += f" (budget: {max_lines} lines, some directories collapsed)"

    return header + "\n" + "\n".join(output)
'''


# ── hierarchical grep (runs on sandbox) ──────────────────────────────

_GREP_MAX_LINES = 100

_GREP_SCRIPT = _GLOB_COMMON + r'''
import re

_MAX_LINE_WIDTH = 200


def grep_hierarchy(base_dir, regex, files_pattern, max_lines):
    base = Path(base_dir).resolve()
    if not base.is_dir():
        return f"Error: not a directory: {base_dir}"

    # ── Compile regex ────────────────────────────────────────────
    try:
        pat = re.compile(regex)
    except re.error as e:
        return f"Error: invalid regex {regex!r}: {e}"

    positive, negative = _parse_pattern(files_pattern)
    if not positive:
        return f"Error: files pattern must include at least one positive glob (got {files_pattern!r})"

    included = _collect_files(base, positive)
    if negative:
        included -= _collect_files(base, negative)
    files = sorted(included)

    if not files:
        return f"0 files match {files_pattern!r} in {base}"

    # ── Scan files for matches ───────────────────────────────────
    file_matches = {}
    total_matches = 0

    for filepath in files:
        try:
            with open(filepath, "r", errors="replace") as f:
                hits = []
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        text = line.rstrip("\n\r")
                        if len(text) > _MAX_LINE_WIDTH:
                            text = text[:_MAX_LINE_WIDTH] + "..."
                        hits.append((i, text))
                if hits:
                    file_matches[filepath] = hits
                    total_matches += len(hits)
        except (OSError, UnicodeDecodeError):
            continue

    if not file_matches:
        return f"0 matches for {regex!r} in {len(files)} files"

    # ── Compute effective base for trie rendering ────────────────
    matched_paths = list(file_matches.keys())
    if all(m.startswith(str(base) + os.sep) for m in matched_paths):
        effective_base = str(base)
    else:
        effective_base = os.path.commonpath(matched_paths)
        if not os.path.isdir(effective_base):
            effective_base = os.path.dirname(effective_base)

    # ── Build trie of files with matches ─────────────────────────
    root = {
        "children": {}, "files": {}, "path": effective_base,
        "n_files": 0, "n_matches": 0,
    }

    for filepath, hits in sorted(file_matches.items()):
        rel = os.path.relpath(filepath, effective_base)
        parts = rel.split(os.sep)
        node = root
        node["n_files"] += 1
        node["n_matches"] += len(hits)

        for part in parts[:-1]:
            if part not in node["children"]:
                node["children"][part] = {
                    "children": {}, "files": {}, "n_files": 0, "n_matches": 0,
                    "path": os.path.join(node["path"], part),
                }
            node = node["children"][part]
            node["n_files"] += 1
            node["n_matches"] += len(hits)

        node["files"][filepath] = hits

    # ── Two-level budget-driven expansion ────────────────────────
    dir_expanded = {id(root)}
    file_expanded = set()
    file_partial = {}

    def _count_lines(node):
        if id(node) not in dir_expanded:
            return 1
        total = 0
        for fp, hits in node["files"].items():
            if fp in file_expanded:
                limit = file_partial.get(fp)
                if limit is not None and limit < len(hits):
                    total += limit + 1
                else:
                    total += len(hits)
            else:
                total += 1
        for child in node["children"].values():
            total += _count_lines(child)
        return total

    def _dir_candidates(node):
        if id(node) not in dir_expanded:
            yield (node["n_matches"], "dir", node)
            return
        for child in node["children"].values():
            yield from _dir_candidates(child)

    def _file_candidates(node):
        if id(node) not in dir_expanded:
            return
        for fp, hits in node["files"].items():
            if fp not in file_expanded:
                yield (len(hits), "file", fp, hits)
        for child in node["children"].values():
            yield from _file_candidates(child)

    current_lines = _count_lines(root)

    while current_lines < max_lines:
        dir_cands = list(_dir_candidates(root))
        file_cands = list(_file_candidates(root))

        if not dir_cands and not file_cands:
            break

        best_dir = max(dir_cands, key=lambda x: x[0]) if dir_cands else None
        best_file = max(file_cands, key=lambda x: x[0]) if file_cands else None

        best_type = None
        if best_dir and best_file:
            best_type = "dir" if best_dir[0] >= best_file[0] else "file"
        elif best_dir:
            best_type = "dir"
        else:
            best_type = "file"

        if best_type == "dir":
            _, _, node = best_dir
            n_items = len(node["files"]) + len(node["children"])

            if n_items <= 1:
                dir_expanded.add(id(node))
                current_lines = _count_lines(root)
                continue

            net_cost = n_items - 1
            if current_lines + net_cost <= max_lines:
                dir_expanded.add(id(node))
                current_lines = _count_lines(root)
                continue

            if best_file:
                best_type = "file"
            else:
                break

        if best_type == "file":
            _, _, fp, hits = best_file
            n_hits = len(hits)

            if n_hits <= 1:
                file_expanded.add(fp)
                current_lines = _count_lines(root)
                continue

            net_cost = n_hits - 1
            if current_lines + net_cost <= max_lines:
                file_expanded.add(fp)
                current_lines = _count_lines(root)
                continue

            budget_remaining = max_lines - current_lines
            if budget_remaining < 2:
                break
            file_expanded.add(fp)
            file_partial[fp] = budget_remaining
            current_lines = _count_lines(root)
            break

    # ── Render ───────────────────────────────────────────────────
    output = []

    def render(node):
        if id(node) not in dir_expanded:
            if node["n_files"] == 1:
                output.append(f"{node['path']}/ ({node['n_matches']} matches in 1 file)")
            else:
                output.append(f"{node['path']}/ ({node['n_matches']} matches in {node['n_files']} files)")
            return

        for fp in sorted(node["files"]):
            hits = node["files"][fp]
            if fp not in file_expanded:
                output.append(f"{fp} ({len(hits)} matches)")
                continue
            limit = file_partial.get(fp)
            if limit is not None and limit < len(hits):
                for line_num, text in hits[:limit]:
                    output.append(f"{fp}:{line_num}: {text}")
                output.append(f"{fp}: ... and {len(hits) - limit} more matches")
            else:
                for line_num, text in hits:
                    output.append(f"{fp}:{line_num}: {text}")

        for name in sorted(node["children"]):
            render(node["children"][name])

    render(root)

    n_files = len(file_matches)
    header = f"{total_matches} matches across {n_files} files for {regex!r}"
    has_collapsed = (
        any(True for _ in _dir_candidates(root))
        or any(True for _ in _file_candidates(root))
        or bool(file_partial)
    )
    if has_collapsed:
        header += f" (budget: {max_lines} lines, some entries collapsed)"

    return header + "\n" + "\n".join(output)
'''


# ── read script (runs on sandbox) ───────────────────────────────────
#
# Executed on the sandbox via _run_sandbox_script so the file never
# leaves the sandbox: line selection, numbering, and header formatting
# all happen in-process next to the data.  Args are repr()-embedded at
# call time so arbitrary path strings are safe.

_READ_SCRIPT = r'''
import os
import sys


def read_file(path, start, stop):
    """Read a file and return numbered lines with a header.

    start/stop follow the same semantics as _core_read:
      - positive: 1-indexed, stop is exclusive
      - negative: counts from end
      - 0: start=1, stop=end
    Internal cap: 2000 lines.
    """
    if not os.path.exists(path):
        return f"Error: File not found: {path}"

    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"Error: Cannot read {path}: {e}"

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total_lines = len(lines)

    # Resolve start to 0-based index.
    if start == 0:
        start_idx = 0
    elif start > 0:
        start_idx = start - 1
    else:
        start_idx = max(0, total_lines + start)

    # Resolve stop to 0-based exclusive index.
    if stop == 0:
        end_idx = total_lines
    elif stop > 0:
        end_idx = stop - 1
    else:
        end_idx = max(0, total_lines + stop)

    # Clamp to valid range.
    start_idx = max(0, min(start_idx, total_lines))
    end_idx = max(start_idx, min(end_idx, total_lines))

    # Cap at 2000 lines.
    if end_idx - start_idx > 2000:
        end_idx = start_idx + 2000

    selected = lines[start_idx:end_idx]
    numbered = "\n".join(
        f"{start_idx + i + 1}: {line}"
        for i, line in enumerate(selected)
    )
    header = f"File: {path} ({total_lines} lines total)"
    if start_idx > 0 or end_idx < total_lines:
        header += f", showing lines {start_idx + 1}-{end_idx}"
    return f"{header}\n{numbered}"
'''


# ── write script (runs on sandbox) ───────────────────────────────────
#
# Receives path and content as repr()-embedded Python literals.
# Creates parent directories, writes the file, and reports byte/line
# counts.  Content is bounded by model output token limits so size is
# not a practical concern.

_WRITE_SCRIPT = r'''
import os


def write_file(path, content):
    """Write content to path, creating parent directories as needed.

    Returns a one-line result string on success or an error string
    starting with "Error:".
    """
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"Error: Cannot create parent directory {parent}: {e}"

    try:
        with open(path, "w") as f:
            f.write(content)
    except OSError as e:
        return f"Error: Cannot write {path}: {e}"

    n_bytes = len(content.encode("utf-8"))
    n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"Wrote {n_bytes} bytes ({n_lines} lines) to {path}"
'''


# ── edit script (runs on sandbox) ────────────────────────────────────
#
# Performs string replacement entirely on the sandbox, avoiding a full
# round-trip download+upload of the file contents.  Args are
# repr()-embedded at call time.

_EDIT_SCRIPT = r'''
import os


def edit_file(path, old_string, new_string, replace_all):
    """Edit a file by exact string replacement.

    Returns a one-line result string on success or an error string
    starting with "Error:".
    """
    if not os.path.exists(path):
        return f"Error: File not found: {path}"

    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return f"Error: Cannot read {path}: {e}"

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"
    if count > 1 and not replace_all:
        return (
            f"Error: Found {count} matches for old_string in {path}. "
            f"Provide more surrounding context to identify a unique match, "
            f"or set replace_all=true to replace all occurrences."
        )

    if replace_all:
        new_content = content.replace(old_string, new_string)
        replaced = count
    else:
        new_content = content.replace(old_string, new_string, 1)
        replaced = 1

    try:
        with open(path, "w") as f:
            f.write(new_content)
    except OSError as e:
        return f"Error: Cannot write {path}: {e}"

    return f"Replaced {replaced} occurrence(s) in {path}"
'''


# ── shared Daytona I/O helpers ───────────────────────────────────────
#
# Thin wrappers over the Daytona toolbox API endpoints that multiple
# _core_* functions call.  Extracted to avoid duplicating HTTP call
# patterns and error handling across read/write/edit/glob/grep/onboard.


async def _upload_file(valves, sandbox_id: str, client: httpx.AsyncClient,
                       path: str, content: bytes) -> None:
    """Upload raw bytes to a sandbox path (creates parent dirs first)."""
    parent = "/".join(path.rstrip("/").split("/")[:-1])
    if parent:
        await client.post(
            _toolbox(valves, sandbox_id, "/files/folder"),
            headers=_headers(valves),
            json={"path": parent, "mode": "755"},
            timeout=30.0,
        )
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/files/upload"),
        params={"path": path},
        headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
        files={"file": ("file", io.BytesIO(content), "application/octet-stream")},
        timeout=60.0,
    )
    resp.raise_for_status()


async def _download_file(valves, sandbox_id: str, client: httpx.AsyncClient,
                         path: str) -> str:
    """Download a file from the sandbox. Returns file content as text.

    Returns None if the file does not exist (404). Raises on other errors.
    """
    resp = await client.get(
        _toolbox(valves, sandbox_id, "/files/download"),
        params={"path": path},
        headers=_headers(valves),
        timeout=60.0,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


async def _run_sandbox_script(valves, sandbox_id: str, client: httpx.AsyncClient,
                              script: str, *, error_prefix: str,
                              timeout_ms: int = 30000,
                              http_timeout: float = 60.0) -> str:
    """Execute a Python script on the sandbox and return its stdout.

    For short scripts, passes the script inline via ``python3 -c``.
    Returns the script's stdout on success. On non-zero exit, returns
    an error string prefixed with *error_prefix*.
    """
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/process/execute"),
        headers=_headers(valves),
        json={
            "command": f"python3 -c {_shell_quote(script)}",
            "timeout": timeout_ms,
        },
        timeout=http_timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", "")
    exit_code = data.get("exitCode", -1)
    if exit_code != 0:
        return f"Error: {error_prefix} (exit {exit_code}).\n{result[:500]}"
    return result


# ── shared tool cores ───────────────────────────────────────────────
#
# Each _core_* function encapsulates the I/O logic for a tool.  Both
# the Tools class methods and the delegate closures call these, so
# behavior stays in sync.  The dependency signature is explicit:
# (valves, sandbox_id, client, **tool_params) -> str.
#
# Docstrings on _core_* are the **single source of truth** for tool
# descriptions and parameter docs.  Use :param: format.  Both OWUI and
# pydantic-ai parse :param: natively.  _standard_tool and
# _build_delegate_tool copy docstrings onto their generated functions;
# hand-written methods (bash) set __doc__ directly.



def _check_tool_params(kwargs: dict, annotations: dict) -> str | None:
    """Strict type check for tool params at the wrapper boundary.

    Returns an error string if any param has the wrong runtime type,
    or None if all params are valid.  Only checks params that appear
    in both kwargs and annotations.  Skips str params (everything
    arrives as a string at minimum).
    """
    for name, expected_type in annotations.items():
        if name not in kwargs or expected_type is str:
            continue
        value = kwargs[name]
        # get_origin resolves list[str] -> list, etc.
        base_type = typing.get_origin(expected_type) or expected_type
        if not isinstance(value, base_type):
            return (
                f"Error: parameter '{name}' expected type "
                f"{expected_type.__name__ if hasattr(expected_type, '__name__') else str(expected_type)}"
                f", got {type(value).__name__}: {value!r}"
            )
    return None


# Infrastructure params in _core_* signatures that are NOT tool params.
# The _standard_tool factory skips these when building the OWUI-visible
# method signature.
_CORE_INFRA_PARAMS = frozenset({
    "valves", "sandbox_id", "client", "emit", "user_pairs",
    "on_background", "chat_state", "chat_id",
})


def _standard_tool(core_fn, *, emit_start: str, emit_done: str,
                   extra_core_kwargs=None):
    """Build a Tools class method that wraps a _core_* function.

    Generates a method with the correct OWUI-visible signature (tool params
    from core_fn + OWUI dunder params), docstring from core_fn, and all
    standard boilerplate (ensure_sandbox, ensure_chat_init, emit, drain
    harness messages).

    The generated method has an explicit ``__signature__`` so
    ``inspect.signature()`` returns the exact parameter list OWUI expects.
    ``functools.wraps`` does NOT copy ``__signature__``; we set it manually.

    Args:
        core_fn: The _core_* function to wrap.
        emit_start: Status message emitted before calling the core.
            May contain ``{kwargs}`` which is replaced with a dict of
            the tool kwargs at call time, useful for messages like
            "Reading {kwargs[path]}...".  Use a plain string without
            ``{kwargs}`` for static messages.
        emit_done: Status message emitted after the core returns.
        extra_core_kwargs: Optional callable(self, sandbox_id, __chat_id__)
            returning a dict of extra kwargs to pass to the core function
            (e.g. chat_state and chat_id for interpret).
    """
    # ── Extract tool-visible parameters from the core function ───────
    core_sig = inspect.signature(core_fn)
    tool_params = []
    for name, param in core_sig.parameters.items():
        if name in _CORE_INFRA_PARAMS:
            continue
        # Convert keyword-only params to positional-or-keyword for OWUI
        tool_params.append(
            param.replace(kind=inspect.Parameter.POSITIONAL_OR_KEYWORD)
        )

    # ── Build the synthetic signature ────────────────────────────────
    # self + tool params + OWUI dunder params
    synth_params = [
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ] + tool_params + [
        inspect.Parameter("__user__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default={}),
        inspect.Parameter("__chat_id__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=""),
        inspect.Parameter("__event_emitter__", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default=None),
    ]
    synth_sig = inspect.Signature(synth_params)

    # Names of tool params for extracting kwargs at call time
    tool_param_names = [p.name for p in tool_params]
    # Annotation map for strict type checking at the boundary
    tool_annotations = {
        p.name: p.annotation
        for p in tool_params
        if p.annotation is not inspect.Parameter.empty
    }

    async def _method(self, *args, **kwargs):
        # Bind positional + keyword args to the synthetic signature.
        # Skip 'self' (already bound by Python's method machinery).
        bound = synth_sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        ba = bound.arguments

        # Extract OWUI dunder params
        __user__ = ba.pop("__user__")
        __chat_id__ = ba.pop("__chat_id__")
        __event_emitter__ = ba.pop("__event_emitter__")
        ba.pop("self", None)

        # Remaining args are tool kwargs
        tool_kwargs = {k: ba[k] for k in tool_param_names if k in ba}

        # Strict type check at the wrapper boundary.
        type_err = _check_tool_params(tool_kwargs, tool_annotations)
        if type_err:
            return type_err

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(
                self.valves, email, client, __event_emitter__,
            )
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            # Format emit_start with tool kwargs if it contains {kwargs}
            if "{kwargs" in emit_start:
                start_msg = emit_start.format(kwargs=tool_kwargs)
            else:
                start_msg = emit_start
            await _emit(__event_emitter__, start_msg)

            # Build core call kwargs
            call_kwargs = dict(tool_kwargs)
            if extra_core_kwargs:
                call_kwargs.update(
                    extra_core_kwargs(self, sandbox_id, __chat_id__)
                )

            result = await core_fn(
                self.valves, sandbox_id, client, **call_kwargs,
            )

            await _emit(__event_emitter__, emit_done, done=True)
            messages = _drain_harness_messages(
                self._chat_state, __chat_id__, _sb_warning,
            )
            return _prepend_harness_messages(result, messages)

        return await _tool_context(__event_emitter__, _run)

    # ── Set the correct signature, annotations, and docstring ────────
    _method.__signature__ = synth_sig
    # OWUI uses get_type_hints() (which reads __annotations__) for the
    # JSON schema type mapping, NOT inspect.signature().annotation.
    # Without this, all _standard_tool params fall back to "string".
    _method.__annotations__ = {
        p.name: p.annotation
        for p in synth_params
        if p.annotation is not inspect.Parameter.empty and p.name != "self"
    }
    _method.__doc__ = inspect.getdoc(core_fn) or ""
    _method.__name__ = core_fn.__name__.replace("_core_", "")
    _method.__qualname__ = f"Tools.{_method.__name__}"

    return _method


async def _core_read(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str, start: int = 1, stop: int = 0) -> str:
    """Read a file from the sandbox. Returns numbered lines.
    Supports Python-style negative indexing: start=-10 reads from
    10 lines before the end, stop=-3 stops 3 lines before the end.
    All positive values are 1-indexed. stop is exclusive (half-open).
    stop=0 (default) means end of file.

    :param path: Absolute path to the file.
    :param start: First line to return (1-indexed). Negative counts from end. 0 is treated as 1.
    :param stop: Line to stop before (exclusive, 1-indexed). Negative counts from end. 0 means end of file.
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _READ_SCRIPT
        + f"\nprint(read_file({path!r}, {start!r}, {stop!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="read script failed",
    )


async def _core_write(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                      path: str, content: str) -> str:
    """Write a file to the sandbox (creates parents automatically).

    For large files, prefer a skeleton-then-edit workflow: write the file first
    with overall structure and ``# PLACEHOLDER: <section>`` markers, then use
    ``edit()`` to expand each placeholder incrementally. This makes progress
    visible to the user in real time and keeps partial work on disk if
    generation is interrupted.

    :param path: Absolute path to write to.
    :param content: The full file content.
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _WRITE_SCRIPT
        + f"\nprint(write_file({path!r}, {content!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="write script failed",
    )


async def _core_edit(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str, old_string: str, new_string: str,
                     replace_all: bool = False) -> str:
    """Edit a file by exact string replacement. Fails on ambiguous matches unless replace_all=true.

    :param path: Absolute path to the file.
    :param old_string: Exact text to find.
    :param new_string: Replacement text.
    :param replace_all: Replace all occurrences (default: false).
    """
    err = _require_abs_path(path)
    if err:
        return err
    script = (
        _EDIT_SCRIPT
        + f"\nprint(edit_file({path!r}, {old_string!r}, {new_string!r}, {replace_all!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="edit script failed",
    )


async def _core_glob(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     pattern: str, max_lines: int = _GLOB_MAX_LINES) -> str:
    """Search for files in the sandbox workspace by glob pattern.
    Returns a hierarchical listing of matches with absolute paths.
    Dense directories are collapsed with match counts; narrow the
    pattern or increase max_lines to expand them.

    :param pattern: Comma-separated globs, !-prefix to exclude. Examples: '**/*.py', 'src/**/*.ts,!**/node_modules/**'.
    :param max_lines: Max output lines (default: 100).
    """
    clamped = max(1, min(500, max_lines))
    base_dir = "/home/daytona/workspace"
    script = (
        _GLOB_SCRIPT
        + f"\nprint(glob_hierarchy({base_dir!r}, {pattern!r}, {clamped!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="glob script failed",
    )


async def _core_grep(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     pattern: str, files: str = "**/*",
                     max_lines: int = _GREP_MAX_LINES) -> str:
    """Search file contents in the sandbox workspace by regex.
    Returns matches grouped by file with line numbers. Dense
    directories and files are collapsed with match counts;
    narrow the file scope or increase max_lines to expand them.

    :param pattern: Regex to search for (e.g. 'import.*asyncio', 'TODO|FIXME').
    :param files: File scope as comma-separated globs (default: '**/*'). !-prefix to exclude.
    :param max_lines: Max output lines (default: 100).
    """
    clamped = max(1, min(500, max_lines))
    base_dir = "/home/daytona/workspace"
    script = (
        _GREP_SCRIPT
        + f"\nprint(grep_hierarchy({base_dir!r}, {pattern!r}, {files!r}, {clamped!r}))"
    )
    return await _run_sandbox_script(
        valves, sandbox_id, client, script, error_prefix="grep script failed",
    )


# ── bash core (session + sidecar protocol) ──────────────────────────


def _build_bash_script(command: str, user_pairs: list[tuple[str, str]],
                       pid_path: str, log_path: str) -> str:
    """Build the bash wrapper script with sidecar file setup."""
    user_env_lines = "".join(
        f"export {k}={_shell_quote(v)}\n" for k, v in user_pairs
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -e -o pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive "
        "GIT_TERMINAL_PROMPT=0 "
        "PIP_NO_INPUT=1 "
        "NPM_CONFIG_YES=true "
        "CI=true\n"
        + user_env_lines
        + f"echo $BASHPID > {_shell_quote(pid_path)}\n"
        + f"exec > >(tee {_shell_quote(log_path)}) 2>&1\n"
        + command
        + "\n"
    )


def _format_bash_result(output: str, exit_code: int | None,
                        was_truncated: bool, meta: dict,
                        spill_path: str | None = None,
                        background_info: dict | None = None) -> str:
    """Format bash output for return to the caller."""
    if background_info is not None:
        # Auto-backgrounded: command still running
        if not output.strip():
            output = "(no output yet)"
        elapsed = background_info["elapsed"]
        cmd_id = background_info["cmd_id"]
        bg_notice = (
            f"\n\n[Backgrounded after {elapsed}s — command is still running]\n"
            f"CMD={cmd_id}\n"
            f"Ref /tmp/cmd/$CMD/{{sh,pid,log,exit}}\n"
            f"See lathe(manpage=\"background\") for peek/poll/kill recipes.\n"
            f"Tell the user the command is running. Don't poll until they ask or "
            f"you have a concrete reason to expect completion."
        )
        return output + bg_notice

    # Finished command
    if exit_code is not None and exit_code != 0:
        output = f"Exit code: {exit_code}\n{output}"

    if not output.strip():
        output = "(no output)"

    if was_truncated and spill_path:
        start = meta["shown_start_line"]
        end = meta["shown_end_line"]
        total = meta["total_lines"]
        total_size = _human_size(meta["total_bytes"])
        if meta["truncated_by"] == "lines":
            notice = (
                f"\n\n[Showing lines {start}-{end} of {total}. "
                f"Full output ({total_size}): {spill_path}]"
            )
        else:
            notice = (
                f"\n\n[Showing lines {start}-{end} of {total} "
                f"({_human_size(_MAX_BYTES)} limit, full output is {total_size}). "
                f"Full output: {spill_path}]"
            )
        output += notice

    return output


async def _core_bash(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     command: str, workdir: str = "/home/daytona/workspace",
                     user_pairs: list[tuple[str, str]],
                     foreground_seconds: int = 30,
                     emit=None,
                     on_background=None) -> str:
    """Execute a bash command in the sandbox. Non-interactive only.
    Commands that finish within the foreground window return output directly.
    Long-running commands auto-background and return a descriptor with log
    file paths for monitoring.

    When creating git commits, add an Assisted-by: Lathe trailer to the
    commit message to acknowledge AI assistance.

    :param command: The bash command to execute.
    :param workdir: Working directory (default: /home/daytona/workspace).
    :param foreground_seconds: Seconds to wait before auto-backgrounding. Omit or set -1 to use the server default. Set 0 for immediate background (fire-and-forget). Use higher positive values for known-slow commands.
    """
    cmd_id = str(uuid.uuid4())
    cmd_dir = f"/tmp/cmd/{cmd_id}"
    log_path = f"{cmd_dir}/log"
    pid_path = f"{cmd_dir}/pid"
    exit_path = f"{cmd_dir}/exit"
    script_path = f"{cmd_dir}/sh"

    script = _build_bash_script(command, user_pairs, pid_path, log_path)

    # Upload the script (creates parent dirs automatically)
    await client.post(
        _toolbox(valves, sandbox_id, "/files/upload"),
        params={"path": script_path},
        headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
        files={"file": ("file", io.BytesIO(script.encode("utf-8")), "application/octet-stream")},
        timeout=60.0,
    )

    # ── Create a per-command session ─────────────────────────────
    # Each bash() call gets its own session so commands never
    # queue behind each other.  This is critical: a shared
    # session serialises commands, so monitoring a backgrounded
    # build via tail/cat would block until the build finishes.
    session_id = f"lathe-cmd-{cmd_id}"
    resp = await client.post(
        _toolbox(valves, sandbox_id, f"/process/session"),
        headers=_headers(valves),
        json={"sessionId": session_id},
        timeout=30.0,
    )
    if resp.status_code not in (200, 409):
        resp.raise_for_status()

    # ── Execute asynchronously in the session ────────────────────
    # The actual command writes exit code to a sidecar file so
    # the agent can check completion even after backgrounding.
    # Session exec has no cwd parameter, so we cd explicitly.
    exec_command = (
        f"cd {_shell_quote(workdir)} && "
        f"bash {script_path}; EC=$?; "
        f"echo $EC > {_shell_quote(exit_path)}; "
        f"(exit $EC)"
    )
    resp = await client.post(
        _toolbox(valves, sandbox_id, f"/process/session/{session_id}/exec"),
        headers=_headers(valves),
        json={
            "command": exec_command,
            "runAsync": True,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    session_cmd_id = resp.json().get("cmdId", "")

    # ── Foreground polling window ────────────────────────────────
    # 0 = immediate background (fire-and-forget).  Positive values
    # are clamped to [1, 300].
    if foreground_seconds <= 0:
        fg_timeout = 0.01  # single poll iteration, then background
    else:
        fg_timeout = max(1, min(300, foreground_seconds))
    deadline = time.time() + fg_timeout
    poll_interval = 0.25
    last_status_at = time.time()
    finished = False
    exit_code = None

    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.5, 2.0)

        # Check command status via session info
        resp = await client.get(
            _toolbox(valves, sandbox_id, f"/process/session/{session_id}"),
            headers=_headers(valves),
            timeout=15.0,
        )
        if resp.status_code != 200:
            continue
        session_info = resp.json()
        commands = session_info.get("commands", [])

        # Find our command by id
        for cmd in commands:
            if cmd.get("id") == session_cmd_id:
                ec = cmd.get("exitCode")
                if ec is not None:
                    exit_code = ec
                    finished = True
                break

        if finished:
            break

        # Emit progress every ~5s
        now = time.time()
        if now - last_status_at >= 5.0:
            elapsed = int(now - (deadline - fg_timeout))
            await _emit(emit, f"Running... ({elapsed}s)")
            last_status_at = now

    # ── Fetch logs ──────────────────────────────────────────────
    # NOTE: We intentionally do NOT delete the session here.
    # Daytona session deletion kills all processes spawned within
    # it, including children backgrounded with nohup/&. Since
    # "nohup server & ... expose()" is the primary workflow for
    # exposing services, deleting the session would silently kill
    # the server the user just asked for. Sessions are lightweight
    # and the sandbox itself is reaped on idle, so accumulation
    # is not a practical concern.
    logs_resp = await client.get(
        _toolbox(valves, sandbox_id, f"/process/session/{session_id}/command/{session_cmd_id}/logs"),
        headers=_headers(valves),
        timeout=30.0,
    )
    result = logs_resp.text if logs_resp.status_code == 200 else ""

    output, was_truncated, meta = _truncate_tail(result)

    if not finished:
        elapsed = int(time.time() - (deadline - fg_timeout))
        if on_background:
            on_background(sandbox_id=sandbox_id, session_id=session_id,
                          session_cmd_id=session_cmd_id, cmd_id=cmd_id,
                          start_time=deadline - fg_timeout)
        return _format_bash_result(
            output, exit_code, was_truncated, meta,
            background_info={"elapsed": elapsed, "cmd_id": cmd_id},
        )

    # Command finished within foreground window
    spill_path = log_path if was_truncated else None
    return _format_bash_result(
        output, exit_code, was_truncated, meta,
        spill_path=spill_path,
    )


# ── Background bash completion polling ───────────────────────────────
#
# When a bash command auto-backgrounds, the outer bash() Tool method
# spawns this coroutine to poll for completion and push a notice into
# the chat's harness message queue.  The next tool call in the same
# chat will see it prepended to the result.
#
# Max poll duration is 10 minutes.  If the command takes longer, the
# notice is silently dropped (the model can still poll sidecar files).

_BG_BASH_POLL_MAX_SECONDS = 600

async def _poll_bg_bash(valves, sandbox_id: str, session_id: str,
                        session_cmd_id: str, cmd_id: str, start_time: float,
                        chat_state: LRUCache, chat_id: str):
    """Poll a backgrounded bash command and push a completion notice."""
    poll_interval = 2.0
    deadline = time.time() + _BG_BASH_POLL_MAX_SECONDS
    exit_code = None

    async with httpx.AsyncClient() as poll_client:
        try:
            while time.time() < deadline:
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 10.0)
                try:
                    resp = await poll_client.get(
                        _toolbox(valves, sandbox_id, f"/process/session/{session_id}"),
                        headers=_headers(valves),
                        timeout=15.0,
                    )
                    if resp.status_code != 200:
                        continue
                    commands = resp.json().get("commands", [])
                    for cmd in commands:
                        if cmd.get("id") == session_cmd_id:
                            ec = cmd.get("exitCode")
                            if ec is not None:
                                exit_code = ec
                            break
                    else:
                        continue
                    if exit_code is not None:
                        break
                except Exception:
                    continue  # transient network error, keep polling
            else:
                return  # timed out, silently give up

            # Fetch tail of log for the notice
            elapsed = int(time.time() - start_time)
            tail = ""
            try:
                log_resp = await poll_client.get(
                    _toolbox(valves, sandbox_id,
                             f"/process/session/{session_id}/command/{session_cmd_id}/logs"),
                    headers=_headers(valves),
                    timeout=30.0,
                )
                if log_resp.status_code == 200:
                    tail = log_resp.text
            except Exception as e:
                logger.debug("bg-poll: failed to fetch log tail: %s", e)

            notice = _format_bg_bash_notice(cmd_id, exit_code, elapsed, tail)
            _push_bg_notice(chat_state, chat_id, notice)

        except Exception as e:
            logger.debug("bg-poll: poller failed: %s", e)


# ── Code interpreter ─────────────────────────────────────────────────
#
# The Daytona toolbox exposes a persistent Python REPL via a WebSocket
# endpoint.  Variables, imports, and definitions survive across calls
# within the same "context".  We create one context per OWUI chat (keyed
# by __chat_id__) and store its ID in _chat_state.  If the sandbox
# restarts (killing the interpreter process), we detect the stale
# context, create a fresh one, and warn the model that state was lost.
#
# The WS protocol:
#   1. Connect to /process/interpreter/execute (HTTP upgrade)
#   2. Send one JSON frame: {code, contextId, timeout}
#   3. Receive N JSON frames: {type: "stdout"|"stderr"|"error", text, ...}
#   4. Server sends WS close: code 1000 = normal, 4008 = timeout
#
# httpx-ws (aconnect_ws) handles the upgrade using the same
# httpx.AsyncClient we already have, keeping the HTTP stack cohesive.

_INTERPRET_DEFAULT_TIMEOUT = 120  # seconds; server default is 600


async def _ensure_interpreter_context(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    chat_state: LRUCache, chat_id: str,
) -> str:
    """Return the interpreter context ID for this chat, creating one if needed.

    Stores the context ID in chat_state[chat_id]["interpreter_context_id"].
    If the stored context is stale (404 on probe), creates a fresh one and
    updates the state.  Returns (context_id, lost_state) where lost_state
    is True if a previously valid context was replaced.
    """
    state = chat_state.get(chat_id, {})
    ctx_id = state.get("interpreter_context_id")

    if ctx_id:
        # Probe: is the context still alive?  The list endpoint is cheap.
        try:
            resp = await client.get(
                _toolbox(valves, sandbox_id, "/process/interpreter/context"),
                headers=_headers(valves),
                timeout=15.0,
            )
            if resp.status_code == 200:
                contexts = resp.json().get("contexts", [])
                if any(c.get("id") == ctx_id for c in contexts):
                    return ctx_id, False
        except Exception as e:
            logger.debug("interpreter context probe failed: %s", e)
        # Context is gone (sandbox restarted, etc.)  Fall through to create.

    # Create a new context.
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/process/interpreter/context"),
        headers=_headers(valves),
        json={"language": "python", "cwd": "/home/daytona/workspace"},
        timeout=30.0,
    )
    resp.raise_for_status()
    new_id = resp.json().get("id")
    if not new_id:
        raise RuntimeError("Daytona returned an interpreter context with no ID")

    # Store in chat state.
    if chat_id in chat_state:
        chat_state[chat_id]["interpreter_context_id"] = new_id
    else:
        chat_state[chat_id] = {"init": True, "pending": [],
                               "interpreter_context_id": new_id}

    lost_state = ctx_id is not None  # had one before, now replaced
    return new_id, lost_state


def _format_interpret_result(stdout: str, stderr: str,
                             errors: list[dict], lost_state: bool) -> str:
    """Format the result of an interpret() call for the model."""
    parts = []

    if lost_state:
        parts.append(
            "[Warning: the interpreter session was reset (sandbox restarted). "
            "All previously defined variables, imports, and state are gone. "
            "Re-run any necessary setup.]"
        )

    if errors:
        for err in errors:
            name = err.get("name", "Error")
            value = err.get("value", "")
            tb = err.get("traceback", "")
            if tb:
                parts.append(tb.rstrip())
            else:
                parts.append(f"{name}: {value}")

    if stdout.strip():
        parts.append(stdout.rstrip())

    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip()}")

    if not parts:
        parts.append("(no output)")

    return "\n\n".join(parts)


async def _core_interpret(valves, sandbox_id: str, client: httpx.AsyncClient,
                          chat_state: LRUCache, chat_id: str, *,
                          code: str, timeout: int = _INTERPRET_DEFAULT_TIMEOUT) -> str:
    """Run Python code in a persistent REPL session. Variables, imports, and
    definitions survive across calls within the same conversation.
    Use this for iterative data exploration, incremental computation, or
    any workflow where you need state to persist between executions.
    For one-shot shell commands or non-Python work, prefer bash() instead.

    :param code: Python code to execute. Top-level await is not supported.
    :param timeout: Max execution time in seconds (default: 120). Use 0 for no limit.
    """
    from httpx_ws import aconnect_ws, WebSocketDisconnect

    if not code.strip():
        return "Error: empty code. Provide Python code to execute."

    context_id, lost_state = await _ensure_interpreter_context(
        valves, sandbox_id, client, chat_state, chat_id,
    )

    # Build the WS URL from the proxy base (same host, /process/interpreter/execute).
    ws_url = _toolbox(valves, sandbox_id, "/process/interpreter/execute")

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    errors: list[dict] = []
    timed_out = False

    try:
        async with aconnect_ws(
            ws_url, client,
            keepalive_ping_interval_seconds=20,
            headers=_headers(valves),
        ) as ws:
            # Send the execute request.
            await ws.send_json({
                "code": code,
                "contextId": context_id,
                "timeout": timeout if timeout > 0 else 0,
            })

            # Receive output frames until the server closes the connection.
            while True:
                try:
                    msg = await ws.receive_json()
                except WebSocketDisconnect as exc:
                    if exc.code == 4008:
                        timed_out = True
                    break

                msg_type = msg.get("type", "")
                if msg_type == "stdout":
                    stdout_parts.append(msg.get("text", ""))
                elif msg_type == "stderr":
                    stderr_parts.append(msg.get("text", ""))
                elif msg_type == "error":
                    errors.append(msg)

    except Exception as e:
        return f"Error connecting to interpreter: {e}"

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)

    result = _format_interpret_result(stdout, stderr, errors, lost_state)
    if timed_out:
        result = f"[Execution timed out after {timeout}s]\n\n{result}"

    return result


def _build_onboard_script(project_path: str) -> str:
    """Build a Python script that collects agent context from a sandbox.

    Always produces a flat directory listing of project_path (one level,
    absolute paths, dirs marked with trailing /).  Additionally searches
    two locations for agent context:
      1. ~/.agents/          — global agent instructions and skills
      2. <project_path>/     — project-local instructions and skills

    Skills are merged into a single catalog.  On name collision, the
    project-level entry wins (more specific scope takes precedence).
    """
    # The script is a self-contained Python program executed on the sandbox.
    # project_path is injected via repr() so it's safely quoted as a
    # Python string literal.  The rest of the script uses no interpolation.
    return (
        "import os, glob\n\nPROJECT = " + repr(project_path)
        + "\n" + textwrap.dedent("""\
        GLOBAL  = os.path.expanduser("~/.agents")

        sections = []

        # ── Directory listing (flat, one level) ──────────────────────

        if os.path.isdir(PROJECT):
            entries = sorted(os.listdir(PROJECT))
            lines = []
            for e in entries:
                full = os.path.join(PROJECT, e)
                lines.append(full + "/" if os.path.isdir(full) else full)
            if lines:
                sections.append("# Directory: " + PROJECT + "\\n\\n" + "\\n".join(lines))
            else:
                sections.append("# Directory: " + PROJECT + "\\n\\n(empty)")
        else:
            sections.append("# Directory: " + PROJECT + "\\n\\n(not found)")

        # ── Collect AGENTS.md files ──────────────────────────────────

        def read_agents_md(base, heading):
            p = os.path.join(base, "AGENTS.md")
            if not os.path.isfile(p):
                return None
            with open(p) as f:
                return f"# {heading} ({p})\\n\\n{f.read()}"

        global_md = read_agents_md(GLOBAL, "Global Agent Instructions")
        if global_md:
            sections.append(global_md)

        project_md = read_agents_md(PROJECT, "Project Agent Instructions")
        if project_md:
            sections.append(project_md)

        # ── Collect and merge skills ─────────────────────────────────

        def collect_skills(base):
            skills_dir = os.path.join(base, "skills")
            if not os.path.isdir(skills_dir):
                return
            for skill_md in sorted(glob.glob(os.path.join(skills_dir, "*/SKILL.md"))):
                dir_name = os.path.basename(os.path.dirname(skill_md))
                name = dir_name
                desc = ""
                try:
                    with open(skill_md) as f:
                        lines = f.readlines()
                except OSError:
                    continue
                # Parse YAML frontmatter (minimal, no deps)
                if lines and lines[0].strip() == "---":
                    for line in lines[1:]:
                        if line.strip() == "---":
                            break
                        if line.startswith("name:"):
                            name = line[len("name:"):].strip()
                        elif line.startswith("description:"):
                            desc = line[len("description:"):].strip()
                yield name, desc, skill_md

        # Global first, then project overrides on collision
        skills = {}   # name -> (desc, path)
        order  = []   # first-seen order
        for name, desc, path in collect_skills(GLOBAL):
            if name not in skills:
                order.append(name)
            skills[name] = (desc, path)
        for name, desc, path in collect_skills(os.path.join(PROJECT, ".agents")):
            if name not in skills:
                order.append(name)
            skills[name] = (desc, path)

        if order:
            lines = [
                "# Available Skills",
                "",
                "Load a skill's full instructions with read(path) when the task matches its description.",
                "",
            ]
            for name in order:
                desc, path = skills[name]
                lines.append(f"- **{name}**: {desc}")
                lines.append("  `" + path + "`")
            sections.append("\\n".join(lines))

        # ── Output ───────────────────────────────────────────────────

        print("\\n\\n---\\n\\n".join(sections))
    """)
    )


# ── chat auto-init (first tool call per conversation) ───────────────
#
# On the first tool call in a new chat, run a lightweight snapshot of the
# workspace and sandbox environment.  The result is prepended to the tool
# output as a harness message, giving the model orientation context without
# a dedicated tool call.  Subsequent calls in the same chat skip the
# snapshot (tracked by chat_id in an LRU cache on the Tools instance).
#
# The snapshot is deliberately lighter than onboard(): no AGENTS.md, no
# skills catalog, no deep directory scan.  Just enough to eliminate the
# 2-5 exploratory turns models typically spend probing "what's here?" and
# "what's installed?".

_SNAPSHOT_SCRIPT = (
    "import subprocess, os\n"
    + textwrap.dedent("""\
    sections = []

    # ── Directory listing (flat, one level) ──────────────────────
    for label, path in [("Workspace", "/home/daytona/workspace"),
                        ("Volume", "/home/daytona/volume")]:
        if os.path.isdir(path):
            entries = sorted(os.listdir(path))
            lines = []
            for e in entries:
                full = os.path.join(path, e)
                lines.append(full + "/" if os.path.isdir(full) else full)
            if lines:
                sections.append(label + ":\\n" + "\\n".join(lines))

    # ── System info ──────────────────────────────────────────────
    info_lines = []
    for label, cmd in [
        ("OS", ["uname", "-a"]),
        ("Python", ["python3", "--version"]),
        ("Node", ["node", "--version"]),
    ]:
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.STDOUT, timeout=5
            ).decode().strip()
            info_lines.append(f"{label}: {out}")
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired):
            pass
    if info_lines:
        sections.append("\\n".join(info_lines))

    print("\\n\\n".join(sections))
    """)
)


async def _chat_auto_init(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    chat_state: LRUCache, chat_id: str,
    user: dict,
    emitter=None,
) -> None:
    """Run the first-call-per-chat environment snapshot.

    Deposits the snapshot as a pending harness message in the chat state.
    Non-fatal: silently swallows errors so it never blocks the actual
    tool call.  Also fetches sandbox resource metadata from the Daytona
    API (cpu, memory, disk, region) without an extra sandbox-side command.
    """
    parts: list[str] = ["[Auto-init: first tool call in this conversation]"]

    # ── Sandbox-side snapshot (workspace listing + versions) ─────
    try:
        resp = await client.post(
            _toolbox(valves, sandbox_id, "/process/execute"),
            headers=_headers(valves),
            json={"command": f"python3 -c {_shell_quote(_SNAPSHOT_SCRIPT)}", "timeout": 10000},
            timeout=15.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("exitCode") == 0 and data.get("result", "").strip():
                parts.append(data["result"].strip())
    except Exception as e:
        logger.debug("auto-init: snapshot failed: %s", e)

    # ── Sandbox metadata from Daytona API ────────────────────────
    try:
        resp = await client.get(
            _api(valves, f"/sandbox/{sandbox_id}"),
            headers=_headers(valves),
            timeout=15.0,
        )
        if resp.status_code == 200:
            sb = resp.json()
            cpu = sb.get("cpu")
            mem = sb.get("memory")
            disk = sb.get("disk")
            region = sb.get("region")
            meta_parts = []
            if cpu is not None:
                meta_parts.append(f"{cpu} vCPU")
            if mem is not None:
                meta_parts.append(f"{mem} GB RAM")
            if disk is not None:
                meta_parts.append(f"{disk} GB disk")
            if region:
                meta_parts.append(f"region: {region}")
            if meta_parts:
                parts.append("Sandbox: " + ", ".join(meta_parts))
    except Exception as e:
        logger.debug("auto-init: sandbox metadata fetch failed: %s", e)

    # ── User env var names (values never exposed) ────────────────
    try:
        user_valves = user.get("valves")
        if user_valves:
            raw_env = getattr(user_valves, "env_vars", "") or ""
            pairs = _parse_env_vars(raw_env)
            if pairs:
                names = ", ".join(k for k, _v in pairs)
                parts.append(f"User env vars: {names}")
    except Exception as e:
        logger.debug("auto-init: env var parsing failed: %s", e)

    if len(parts) > 1:
        state = chat_state[chat_id]
        state["pending"].append("\n".join(parts))


async def _ensure_chat_init(
    valves, sandbox_id: str, client: httpx.AsyncClient,
    chat_state: LRUCache, chat_id: str | None,
    user: dict,
    emitter=None,
) -> None:
    """Ensure auto-init has run for this chat.  No-op if chat_id is None
    (older OWUI versions that don't pass it) or if already initialized."""
    if not chat_id:
        return
    if chat_id in chat_state and chat_state[chat_id].get("init"):
        return
    # First tool call in this chat: create state and run snapshot.
    # Future: a "trace" key here (list of dicts) could accumulate a
    # structured record of every tool call (tool name, key args, outcome)
    # for flush to the persistent volume at handoff() time.  See #54.
    chat_state[chat_id] = {"init": True, "pending": []}
    await _chat_auto_init(
        valves, sandbox_id, client, chat_state, chat_id, user, emitter,
    )


# ── delegate() sub-agent infrastructure ─────────────────────────────

# Default foreground wait for delegate() before auto-backgrounding.
_DELEGATE_FOREGROUND_SECONDS = 30





def _format_delegate_background(delegate_id: str, elapsed: int, log_preview: str) -> str:
    """Format the background descriptor returned when a delegate is auto-backgrounded."""
    did = delegate_id
    if not log_preview.strip():
        log_preview = "(no progress yet)"
    return (
        f"{log_preview}\n\n"
        f"[Backgrounded after {elapsed}s — sub-agent is still running]\n"
        f"DELEGATE={did}\n"
        f"Ref /tmp/delegate/$DELEGATE/{{task,log,result,error,usage}}\n"
        f"See lathe(manpage=\"delegate\") for background monitoring recipes.\n"
        f"Tell the user the delegate is running. Don't poll until they ask or "
        f"you have a concrete reason to expect completion."
    )


def _build_delegate_prompt(task: str, file_sections: list[str]) -> str:
    """Build the user message for a delegate sub-agent."""
    parts: list[str] = [f"## Task\n\n{task}"]
    if file_sections:
        parts.append(f"## Reference Files\n\n" + "\n\n".join(file_sections))
    return "\n\n".join(parts)



def _build_delegate_system_prompt(max_steps: int, *, has_volume: bool = True) -> str:
    """Build the delegate system prompt, embedding the step budget."""
    volume_line = (
        "- /home/daytona/volume is persistent storage that survives sandbox destruction.\n"
        if has_volume else ""
    )
    return textwrap.dedent(f"""\
        You are a focused sub-agent with direct access to a Linux sandbox.
        You have been delegated a specific task by the calling agent.

        ## Step budget

        You have {max_steps} steps total. Each step is one inference call (thinking +
        tool calls count as one step). When you stop calling tools and produce a
        text response, that is your final step.

        Plan accordingly:
        - For a {max_steps}-step budget, reserve at least the last step for writing
          your summary. Do not start new investigation branches when you are near
          the limit.
        - If you are running low on steps, hand off your work rather than rushing
          to finish. A clear handoff is far more valuable than getting cut off
          with no output. Your handoff should include:
            1. What you accomplished.
            2. What remains unresolved — specific next steps, not vague TODOs.
            3. Absolute paths to critical files in the sandbox that the calling
               agent or a follow-up sub-agent would need to pick up the work
               (source files you modified, test files that are failing, config
               you discovered, logs worth reading).

        ## Rules

        - Be autonomous. Make decisions. Do not ask questions — the user cannot see you.
        - Your final text response (when you stop calling tools) is your deliverable.
          It will be returned to the calling agent as a single tool result. Make it a
          concise summary of what you did and what you found. Include relevant data,
          file paths, error messages, or code snippets — whatever the caller needs to
          act on your findings.
        - Do not repeat the task description back. Jump straight into working.
        - All file paths must be absolute (e.g. /home/daytona/workspace/file.py).
        - The default working directory is /home/daytona/workspace.
        {volume_line}\
        - Commands are non-interactive. Use -y flags where needed.
        - You cannot expose URLs or interact with the user. Focus on the sandbox.
        - If you encounter an error, try to recover or work around it. Report what
          happened in your final summary.
        """)

# Tools withheld from the sub-agent and their reasons (for reference):
#   lathe()    — sub-agent gets instructions via system prompt
#   onboard()  — caller already has project context
#   expose()   — user-facing; sub-agent has no user to give a URL to
#   destroy()  — irreversible lifecycle operation
#   delegate() — no recursion
_DELEGATE_WITHHELD = {"lathe", "onboard", "expose", "destroy", "delegate", "handoff"}


# ── handoff() instructions ──────────────────────────────────────────
#
# Returned verbatim by the handoff() tool.  The agent reads these
# instructions and writes the handoff document as streamed reply text.
# No tool calls, no sandbox I/O — the agent already has everything it
# needs in context.
#
# Future (#54): handoff() could flush an in-memory trace (accumulated
# via _drain_harness_messages / the _run closures) to a JSONL file on
# the persistent volume, then reference it in _HANDOFF_INSTRUCTIONS so
# the receiving agent can selectively grep it.  This would require
# handoff() to acquire a client and sandbox_id (currently it bypasses
# _tool_context entirely).  Deferred: the prose-only handoff is
# adequate today, and the trace format should be co-designed with the
# micro-compaction filter (#51) so both use a shared line schema.

_HANDOFF_INSTRUCTIONS = textwrap.dedent("""\
    You just called handoff(). Your job now is to write a handoff
    document as your reply to the user. Follow these rules exactly:

    ## Format

    Your reply has three parts, in order:

    1. **User instruction** — a short sentence telling the user to
       start a new conversation (with agent tools enabled) and paste
       everything below the line as their opening message.

    2. **Horizontal rule** — a markdown `---` on its own line.

    3. **Handoff body** — everything below the line is what the user
       will paste into the new conversation. The next agent reads it
       cold.        Start with a single orienting sentence in the user's
       evident chat style (same language, register, formality),
       e.g. "We were just working in a long session on the task
       below." Then the structured sections
       (skip any that are empty):

       ### Goal
       What the user is trying to accomplish. Include verbatim quotes
       of key user requests to prevent drift.

       ### Accomplished
       What was done in this conversation. Be specific: name files
       modified, commands run, decisions made and why.

       ### Unresolved
       What remains. Specific next steps, not vague TODOs. If a task
       was partially done, say exactly where it left off.

       ### Key files
       Absolute sandbox paths the next agent should read or pass as
       context_files. Only include paths that actually exist and
       matter — not every file touched.

       ### What didn't work
       Approaches that were tried and failed, so the next agent
       doesn't repeat them. Skip this section if nothing failed.

    ## Rules

    - Do NOT make any tool calls. Write the handoff as your reply
      text, then stop.
    - Do NOT continue working on the task after writing the handoff.
      The conversation is over. If the user replies asking you to
      continue, tell them to delete the message that triggered the
      handoff and continue from there instead — otherwise the handoff
      text bloats the context with duplicated information.
    - The handoff body should be dense and factual. The next agent
      will read it cold — assume no prior context.
    - Reference sandbox file paths rather than inlining code. The
      sandbox persists across conversations — files written in this
      session will be there in the next one. The next agent can read
      them or pass them as context_files to delegate().
    - Keep the total handoff under ~2000 words. A focused handoff is
      more useful than an exhaustive one.
    """)


_DELEGATE_BASH_FOREGROUND_SECONDS = 15

# Nudge threshold: inject a wrap-up reminder when this many steps remain.
_DELEGATE_NUDGE_REMAINING = 2

def _build_delegate_tool(core_fn, *, infra_args: tuple,
                         default_overrides: dict | None = None,
                         extra_infra_kwargs: dict | None = None):
    """Build a single pydantic-ai Tool from a _core_* function.

    Creates a closure with the correct Python signature (param names,
    types, defaults) so pydantic-ai can introspect it for JSON schema.
    Uses exec() to produce a real function (pydantic-ai reads
    get_type_hints(), which ignores __signature__ patches).

    Args:
        core_fn: The _core_* function to wrap.
        infra_args: Positional args to prepend (valves, sandbox_id, client).
        default_overrides: Override default values for specific params,
            e.g. {"foreground_seconds": 15} for bash in delegate context.
        extra_infra_kwargs: Extra keyword args passed to core_fn that are
            NOT tool-visible (e.g. user_pairs for bash).
    """
    from pydantic_ai import Tool

    core_sig = inspect.signature(core_fn)
    tool_params = []
    for name, param in core_sig.parameters.items():
        if name in _CORE_INFRA_PARAMS:
            continue
        if default_overrides and name in default_overrides:
            param = param.replace(default=default_overrides[name])
        tool_params.append(param)

    # Build the function source with correct signature
    param_parts = []
    for p in tool_params:
        ann = p.annotation
        ann_name = ann.__name__ if hasattr(ann, "__name__") else str(ann)
        if p.default != inspect.Parameter.empty:
            param_parts.append(f"{p.name}: {ann_name} = _defaults[{p.name!r}]")
        else:
            param_parts.append(f"{p.name}: {ann_name}")
    sig_str = ", ".join(param_parts)

    kwarg_parts = [f"{p.name}={p.name}" for p in tool_params]
    kwargs_str = ", ".join(kwarg_parts)

    defaults = {
        p.name: p.default
        for p in tool_params
        if p.default != inspect.Parameter.empty
    }

    fn_name = core_fn.__name__.replace("_core_", "")

    # The exec namespace provides the captured variables and type annotations
    ns: dict = {
        "_core_fn": core_fn,
        "_infra_args": infra_args,
        "_extra_kwargs": extra_infra_kwargs or {},
        "_defaults": defaults,
    }
    # Import type names so annotations resolve in the exec'd function
    for p in tool_params:
        ann = p.annotation
        if hasattr(ann, "__name__") and ann.__name__ not in ns:
            ns[ann.__name__] = ann

    code = (
        f"async def {fn_name}({sig_str}) -> str:\n"
        f"    return await _core_fn(*_infra_args, **_extra_kwargs, {kwargs_str})\n"
    )
    exec(code, ns)
    fn = ns[fn_name]
    fn.__doc__ = inspect.getdoc(core_fn) or ""

    return Tool(fn)


def _build_delegate_tools(valves, sandbox_id: str, client: httpx.AsyncClient,
                          user_pairs: list[tuple[str, str]],
                          chat_state: LRUCache = None, chat_id: str = ""):
    """Build pydantic-ai Tool objects that operate against a resolved sandbox.

    Returns a list of Tool instances. Each tool is a thin closure over the
    already-resolved sandbox_id and client -- no per-call sandbox lookup.
    The closures delegate to the shared _core_* functions.
    """
    infra = (valves, sandbox_id, client)

    all_tools = [
        _build_delegate_tool(
            _core_bash, infra_args=infra,
            default_overrides={"foreground_seconds": _DELEGATE_BASH_FOREGROUND_SECONDS},
            extra_infra_kwargs={"user_pairs": user_pairs},
        ),
        _build_delegate_tool(_core_read, infra_args=infra),
        _build_delegate_tool(_core_write, infra_args=infra),
        _build_delegate_tool(_core_edit, infra_args=infra),
        _build_delegate_tool(_core_glob, infra_args=infra),
        _build_delegate_tool(_core_grep, infra_args=infra),
    ]

    # interpret needs chat context; only available when chat_state and chat_id
    # are provided.  It also needs extra infra kwargs.
    if chat_state and chat_id:
        all_tools.append(_build_delegate_tool(
            _core_interpret, infra_args=infra,
            extra_infra_kwargs={"chat_state": chat_state, "chat_id": chat_id},
        ))

    return all_tools


VOLUME_MOUNT_PATH = "/home/daytona/volume"

# ── Service fast-path constants ──────────────────────────────────────

_DUFS_BIN = "/tmp/dufs"
_DUFS_PORT = 5000  # dufs default
_DUFS_ROOT = "/home/daytona/workspace"

# Single idempotent script: install if missing, start if not listening.
# Exit 0 = ready (prints READY); non-zero = install or start failed.
_DUFS_ENSURE_SCRIPT = (
    f'set -e; '
    f'if ! test -x {_DUFS_BIN}; then '
    f'  TAG=$(curl -sf https://api.github.com/repos/sigoden/dufs/releases/latest '
    f'    | python3 -c "import sys,json; print(json.load(sys.stdin)[\'tag_name\'])") '
    f'  && curl -sL "https://github.com/sigoden/dufs/releases/download/${{TAG}}/'
    f'dufs-${{TAG}}-x86_64-unknown-linux-musl.tar.gz" '
    f'  | tar xz -C /tmp && chmod +x {_DUFS_BIN}; '
    f'fi; '
    f'if ! ss -tlnp | grep -q ":{_DUFS_PORT} "; then '
    f'  nohup {_DUFS_BIN} {_DUFS_ROOT} --allow-all > /tmp/dufs.log 2>&1 & '
    f'  sleep 0.5; '
    f'fi; '
    f'PID=$(ss -tlnp | grep ":{_DUFS_PORT} " | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2); '
    f'echo "READY PID=$PID"'
)

_CS_BIN = "/tmp/code-server/bin/code-server"
_CS_PORT = 8080
_CS_ROOT = "/home/daytona/workspace"

_CS_ENSURE_SCRIPT = (
    f'set -e; '
    f'if ! test -x {_CS_BIN}; then '
    f'  curl -fsSL https://code-server.dev/install.sh '
    f'  | sh -s -- --method=standalone --prefix=/tmp/code-server; '
    f'fi; '
    f'if ! ss -tlnp | grep -q ":{_CS_PORT} "; then '
    f'  nohup {_CS_BIN} --bind-addr 0.0.0.0:{_CS_PORT} --auth none {_CS_ROOT} '
    f'  > /tmp/code-server.log 2>&1 & '
    f'  sleep 1; '
    f'fi; '
    f'PID=$(ss -tlnp | grep ":{_CS_PORT} " | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2); '
    f'echo "READY PID=$PID"'
)


async def _ensure_volume(valves, volume_name: str, client: httpx.AsyncClient) -> str:
    """Get or create a Daytona volume by name. Polls until ready. Returns the volume ID."""
    encoded_name = urllib.parse.quote(volume_name, safe="")
    get_url = _api(valves, f"/volumes/by-name/{encoded_name}")

    # Try to fetch existing volume (treat deleting volumes as absent)
    resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
    need_create = (
        resp.status_code != 200
        or resp.json().get("state") in ("pending_delete", "deleting")
    )
    if need_create:
        # Create the volume.  Retry loop handles the race where a
        # recently-deleted volume name hasn't fully freed up yet.
        for attempt in range(30):
            resp = await client.post(
                _api(valves, "/volumes"),
                headers=_headers(valves),
                json={"name": volume_name},
                timeout=30.0,
            )
            if resp.status_code == 400 and "already exists" in resp.text:
                # Deletion still propagating — wait and retry.
                await asyncio.sleep(2)
                resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
                if resp.status_code == 200:
                    state = resp.json().get("state")
                    if state not in ("pending_delete", "deleting"):
                        break
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError(
                f"Could not create volume '{volume_name}' — "
                f"name still reserved by a deleting volume after 60s of retries"
            )

    vol = resp.json()
    vol_id = vol["id"]

    # Poll until the volume is ready (creation involves S3 provisioning).
    # Tolerate transient 404s — the by-name index may lag behind creation.
    if vol.get("state") == "ready":
        return vol_id

    deadline = time.time() + 60
    poll_interval = 1.0
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        resp = await client.get(get_url, headers=_headers(valves), timeout=30.0)
        if resp.status_code == 404:
            poll_interval = min(poll_interval * 1.2, 5.0)
            continue
        resp.raise_for_status()
        vol = resp.json()
        if vol.get("state") == "ready":
            return vol_id
        poll_interval = min(poll_interval * 1.2, 5.0)

    raise RuntimeError(f"Volume '{volume_name}' did not reach ready state within 60s (state: {vol.get('state')})")


async def _wait_for_toolbox(valves, sandbox_id: str, client: httpx.AsyncClient, emitter=None):
    """Poll the toolbox API until it responds, then ensure workspace dir exists."""
    for attempt in range(30):
        try:
            resp = await client.post(
                _toolbox(valves, sandbox_id, "/process/execute"),
                headers=_headers(valves),
                json={"command": "echo ready", "timeout": 5000},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("exitCode") == 0 and "ready" in data.get("result", ""):
                    await client.post(
                        _toolbox(valves, sandbox_id, "/process/execute"),
                        headers=_headers(valves),
                        json={
                            "command": "bash -c 'mkdir -p /home/daytona/workspace'",
                            "timeout": 5000,
                        },
                        timeout=10.0,
                    )
                    return
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            logger.debug("wait-for-toolbox: attempt %d failed: %s", attempt, e)
        await asyncio.sleep(1)
        if attempt == 2:
            await _emit(emitter, "Waiting for sandbox to become ready...")
    raise RuntimeError("Sandbox started but toolbox daemon did not become responsive (30s)")


async def _ensure_sandbox(valves, email: str, client: httpx.AsyncClient, emitter=None) -> tuple[str, str | None]:
    """Find or create a running sandbox for this user.

    Returns (sandbox_id, warning) where warning is None if the sandbox was
    already running, or a short message describing what recovery was needed.
    """
    if not valves.daytona_api_key:
        raise RuntimeError(
            "Daytona API key not configured. Ask an admin to set it in Tool settings."
        )

    if not valves.deployment_label:
        raise RuntimeError(
            "Deployment label not configured. Ask an admin to set it in Tool settings."
        )

    label_key = valves.deployment_label
    labels_filter = json.dumps({label_key: email})

    # 1. Look up existing sandbox by label
    resp = await client.get(
        _api(valves, "/sandbox"),
        params={"labels": labels_filter},
        headers=_headers(valves),
        timeout=30.0,
    )
    resp.raise_for_status()
    sandboxes = resp.json() or []

    matches = [s for s in sandboxes if s.get("labels", {}).get(label_key) == email]

    if len(matches) > 1:
        ids = ", ".join(s["id"] for s in matches)
        raise RuntimeError(
            f"Found {len(matches)} sandboxes labelled {label_key}={email} ({ids}). "
            f"Expected at most 1. Please delete the extras in the Daytona dashboard "
            f"and try again."
        )

    sandbox = matches[0] if matches else None
    warning: str | None = None

    if sandbox is None:
        # 2. Optionally get or create a persistent volume for this user
        create_json: dict = {
            "language": valves.sandbox_language,
            "name": f"{label_key}/{email}",
            "labels": {label_key: email},
            "autoStopInterval": valves.auto_stop_minutes,
            "autoArchiveInterval": valves.auto_archive_minutes,
            "autoDeleteInterval": valves.auto_delete_minutes,
        }
        if valves.persistent_volume:
            volume_name = f"{label_key}/{email}"
            volume_id = await _ensure_volume(valves, volume_name, client)
            create_json["volumes"] = [
                {"volumeId": volume_id, "mountPath": VOLUME_MOUNT_PATH}
            ]

        # 3. Create new sandbox (with or without volume)
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, "/sandbox"),
            headers=_headers(valves),
            json=create_json,
            timeout=30.0,
        )
        resp.raise_for_status()
        sandbox = resp.json()
        warning = "[Sandbox was created — this is a fresh environment with no prior files]"

    sandbox_id = sandbox["id"]
    state = sandbox.get("state", "unknown")

    # 3. Ensure it's running
    if state == "started":
        await _wait_for_toolbox(valves, sandbox_id, client, emitter)
        await _emit(emitter, "Sandbox ready", done=True)
        return sandbox_id, warning

    if state in ("stopped", "archived"):
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/start"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        if not warning:
            warning = (
                "[Sandbox was restarted from archived state — running processes were lost]"
                if state == "archived" else
                "[Sandbox was restarted — running processes were lost]"
            )

    elif state == "error" and sandbox.get("recoverable"):
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/recover"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        resp = await client.post(
            _api(valves, f"/sandbox/{sandbox_id}/start"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        if not warning:
            warning = "[Sandbox was recovered from error — check that expected files and processes still exist]"

    elif state in ("starting", "stopping", "archiving"):
        await _emit(emitter, "Preparing sandbox...")
    else:
        if state == "error":
            raise RuntimeError(
                f"Sandbox is in non-recoverable error state: {sandbox.get('errorReason', 'unknown')}"
            )

    # 4. Poll until started
    deadline = time.time() + 120
    poll_interval = 1.0
    while time.time() < deadline:
        await asyncio.sleep(poll_interval)
        resp = await client.get(
            _api(valves, f"/sandbox/{sandbox_id}"),
            headers=_headers(valves),
            timeout=30.0,
        )
        resp.raise_for_status()
        info = resp.json()
        state = info.get("state", "unknown")

        if state == "started":
            await _wait_for_toolbox(valves, sandbox_id, client, emitter)
            await _emit(emitter, "Sandbox ready", done=True)
            return sandbox_id, warning

        if state == "error":
            raise RuntimeError(
                f"Sandbox entered error state: {info.get('errorReason', 'unknown')}"
            )

        poll_interval = min(poll_interval * 1.2, 5.0)

    raise RuntimeError("Timed out waiting for sandbox to start (120s)")


# ── Tools class (only public methods are visible to OWUI) ───────────


class Tools:
    class Valves(BaseModel):
        daytona_api_key: str = Field(
            "",
            description="Daytona API key",
            json_schema_extra={"input": {"type": "password"}},
        )
        daytona_api_url: str = Field(
            "https://app.daytona.io/api",
            description="Daytona control plane API URL",
        )
        daytona_proxy_url: str = Field(
            "https://proxy.app.daytona.io/toolbox",
            description="Daytona toolbox proxy URL",
        )
        deployment_label: str = Field(
            "",
            description="Label key used to tag sandboxes for this OWUI deployment (e.g. 'chat.example.com')",
        )
        auto_stop_minutes: int = Field(
            15,
            description="Minutes of idle before sandbox stops (0 = never)",
        )
        auto_archive_minutes: int = Field(
            60,
            description="Minutes after stop before sandbox archives",
        )
        auto_delete_minutes: int = Field(
            -1,
            description="Minutes after archive before sandbox is permanently deleted (-1 = never). Example: 129600 = 90 days.",
        )
        persistent_volume: bool = Field(
            True,
            description="Mount a persistent S3/FUSE volume at /home/daytona/volume. Disable for deployments with limited data retention.",
        )
        sandbox_language: str = Field(
            "python",
            description="Default language runtime (python, typescript, javascript)",
        )
        foreground_timeout_seconds: int = Field(
            30,
            description="Seconds to wait for a bash command before auto-backgrounding it (1-300)",
        )


    class UserValves(BaseModel):
        env_vars: str = Field(
            "{}",
            description=(
                'Environment variables injected into every bash command. '
                'JSON object mapping variable names to values, e.g. {"MY_TOKEN":"abc123","FOO":"bar"}. '
                "Values are shell-quoted before injection and never shown to the model."
            ),
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        self.valves = self.Valves()
        # Chat-scoped harness message queue.  Keyed by chat_id, value is a
        # list[str] of messages waiting to be prepended to the next tool
        # result.  The auto-init snapshot deposits the first message;
        # future producers (e.g. background job completion notifications)
        # can push here too, and the next tool call drains the queue.
        #
        # LRU(1024) bounds memory.  Eviction of an old chat_id just means
        # its pending messages are lost (acceptable: the chat is likely
        # dead) and the next tool call in that chat re-runs auto-init
        # (idempotent, ~150 tokens).
        self._chat_state: LRUCache = LRUCache(maxsize=1024)

    # ── agent-facing manual ──────────────────────────────────────────
    #
    # The manpage system is the model's primary orientation surface.
    # Design principles:
    #   - Tool docstrings stay minimal; details are deferred here so
    #     context budget is spent only when the model actually needs help.
    #   - The tool catalog is introspected dynamically so it can never
    #     drift from the actual method list.
    #   - Manpage content is currently static strings, but the
    #     architecture is designed to evolve:
    #
    # TODO(future): Valve-driven behavioral policy injection.
    #   Valves already control mechanism (timeouts, sandbox language,
    #   auto-stop). The next step is for Valve values to also inject
    #   *policy guidance* into manpage content. Mechanism is fixed at
    #   class scan time; policy is runtime-configurable through the manual.
    #
    # TODO(future): Information architecture expansion.
    #   Add per-tool deep dives (manpage="bash"), workflow recipes
    #   (manpage="expose-recipes"), and troubleshooting guides. Existing
    #   tool docstrings can then be further scrunched by adding
    #   breadcrumbs like 'see lathe(manpage="bash") for details'.

    # WARNING: Manpage strings are NOT passed through str.format()
    # unconditionally.  Only pages containing the literal placeholder
    # "{tool_catalog}" are formatted (see the lathe() method).  This
    # means shell snippets with curly braces (${VAR}, {sh,pid,log,exit},
    # {"key":"value"}, etc.) are safe in all other pages.  If you add a
    # new dynamic placeholder, gate the .format() call on its presence
    # rather than calling .format() on every page — otherwise any page
    # with literal braces will blow up with a KeyError at runtime.
    _MANPAGES: dict[str, str] = {
        "egress": textwrap.dedent("""\
            # Lathe — Egress Restrictions

            ## What can the sandbox reach?

            The sandbox can directly reach a broad allowlist of hosts:
            package registries (PyPI, npm, apt), git hosts (GitHub, GitLab,
            Bitbucket), container registries (Docker Hub, ghcr.io), AI APIs
            (OpenAI, Anthropic, OpenRouter, Groq, etc.), CDNs (Cloudflare,
            jsDelivr, unpkg), select cloud storage (S3, GCS), and common
            dev platforms (Vercel, Supabase, Sentry).

            bash("curl ...") and bash("wget ...") work for all allowlisted
            hosts. Most tasks never hit the limit.

            ## When curl fails: egress workarounds

            If a request fails because the sandbox cannot reach a host
            (connection timeout, connection refused on a host you know is
            up), the host is not on the egress allowlist. There is no clean
            way for the agent to independently bypass this — the
            workarounds involve the user.

            **Common — user downloads and uploads via dufs:**
            Ask the user to download the file on their own machine, then
            upload it to the sandbox through the dufs file browser. Call
            expose(target="dufs") to get the URL. This handles any file
            type and any host with no size constraints.

            **Rare — custom browser-side fetch service:**
            For repeated fetch needs (e.g. crawling an API the sandbox
            can't reach), build a small web service in the sandbox that
            presents a UI where the user clicks to initiate fetches from
            their browser. The browser has unrestricted egress but is
            subject to CORS — this only works automatically for targets
            that set Access-Control-Allow-Origin headers. The service
            POSTs results back to itself for the agent to read.

            **Clean solution — Daytona Tier 3:**
            Daytona Tier 3 accounts have unrestricted egress. If the
            admin's Daytona account is Tier 3, none of these workarounds
            are needed — bash("curl ...") reaches any host. The admin can
            check their tier at https://app.daytona.io.

            ## What does NOT work

            - pip install / git clone / npm install to non-allowlisted
              hosts fail even with dufs. Download the artifact first, then
              install from the local file (e.g. pip install ./package.whl).
            - The browser's fetch() API is subject to CORS. A custom fetch
              service cannot silently proxy arbitrary URLs — only
              CORS-friendly ones auto-complete; others require the user to
              download and upload manually.
            """),
        "background": textwrap.dedent("""\
            # Lathe — Background Jobs

            When bash() auto-backgrounds a command, it returns a descriptor with
            two paths:

              CMD=/tmp/cmd/<id>   — the job's sidecar directory
              PID=/tmp/cmd/<id>/pid — process ID file

            ## Sidecar files

            Where CMD=/tmp/cmd/<id>:

              CMD/sh    — the full wrapper script that was executed
              CMD/pid   — PID of the bash process (written before exec)
              CMD/log   — stdout+stderr, written live via tee
              CMD/exit  — exit code; present only when the process ends

            Absence of CMD/exit means the process is still running *or* the
            sandbox was restarted (in which case the PID is stale). To
            distinguish the two, check whether the PID is still alive.

            ## Recipes

            **Peek at live output:**
            ```
            tail $CMD/log
            ```

            **Poll until done (bounded):**
            ```
            for i in 1 2 3 4 5; do
              test -f $CMD/exit && {{ cat $CMD/exit; break; }} || sleep 2
            done
            test -f $CMD/exit || echo STILL_RUNNING
            ```

            **Check if process is alive:**
            ```
            kill -0 $(cat $CMD/pid) 2>/dev/null && echo ALIVE || echo DEAD_OR_GONE
            ```

            **Kill the job:**
            ```
            kill $(cat $CMD/pid)
            ```
            This stops the wrapper process. Note: child processes the job spawned
            will have already reparented to init and will keep running. If the job
            launched named services (e.g. a dev server), kill them by name:
            ```
            pkill -f my_server_name
            ```

            **Kill and confirm wrapper is gone:**
            ```
            kill $(cat $CMD/pid); sleep 1; kill -0 $(cat $CMD/pid) 2>/dev/null && echo STILL_UP || echo GONE
            ```

            ## Caution

            After a sandbox restart, CMD/pid contains a stale PID. A new process
            may have been assigned that ID. Do not kill without first verifying
            the process is the one you launched (e.g. check CMD/log for expected
            output before killing).
            """),
        "interpret": textwrap.dedent("""\
            # Lathe — Persistent Python Interpreter

            interpret() runs Python code in a persistent REPL backed by the
            Daytona code interpreter. Variables, imports, and definitions
            survive across calls within the same conversation.

            ## State model

            - One interpreter context per conversation (chat), created lazily
              on the first interpret() call.
            - State persists across calls: define a function in one call, use
              it in the next. Import a module once, use it everywhere.
            - State is lost when the sandbox stops or restarts (idle timeout,
              destroy, etc.). The tool detects this and warns you to re-run
              setup code.

            ## When to use interpret() vs bash()

            **Use interpret() when:**
            - You need state across calls (accumulating data, iterative
              exploration, building up a computation step by step).
            - You are doing data analysis, numerical computation, or
              prototyping where seeing intermediate results matters.
            - You want to define helper functions and reuse them.

            **Use bash() when:**
            - Running shell commands, installing packages, managing files.
            - Running non-Python programs.
            - Commands need shell features (pipes, redirection, env vars).
            - One-shot scripts with no state to preserve.

            ## Limitations

            - Python only. For other languages, use bash().
            - Top-level ``await`` is not supported. Use ``asyncio.run()``
              for async code.
            - No stdin interaction. The code runs non-interactively.
            - Default timeout is 120 seconds. Pass timeout=0 for no limit,
              or a custom value in seconds.
            - Output is the raw stdout/stderr stream from the REPL. Large
              outputs are not truncated (unlike bash), so be mindful of
              printing huge data structures.

            ## Sub-agent access

            Delegates (sub-agents) share the same interpreter context as the
            outer conversation. A delegate can define a variable that the
            outer model later reads, or vice versa. This is useful for
            parallel data pipelines.
            """),
        "recipes": textwrap.dedent("""\
            # Lathe — Recipes

            Tested scripts for bootstrapping common tools from a cold sandbox.
            These tools live in /tmp and survive sandbox stop/restart but not
            destroy(). If /tmp/dufs or /tmp/code-server is missing, re-run the
            install script.

            ## File browser — dufs

            When the user asks to upload files, download files, browse files,
            or transfer files, the answer is expose(target="dufs"). Do NOT
            attempt to relay file contents through the conversation — give the
            user a URL they can use directly in their browser.

            **One-step setup:**
            ```
            expose(target="dufs")
            ```
            This installs dufs if missing, starts it on port 5000 serving
            /home/daytona/workspace with full upload/download, and returns a
            signed URL. Idempotent — safe to call again after sandbox restart.

            **Custom directory or read-only access:**
            For non-default configurations, install and start dufs manually:
            ```
            nohup /tmp/dufs /home/daytona/workspace/output --allow-all &
            ```
            Then call expose(target="http:5000").

            ## Full IDE — code-server

            When the user asks for an IDE, editor, or VS Code in the browser,
            use code-server.

            **One-step setup:**
            ```
            expose(target="code-server")
            ```
            This installs code-server if missing, starts it on port 8080
            serving /home/daytona/workspace with no auth, and returns a signed
            URL. Idempotent — safe to call again after sandbox restart.

            **Custom configuration:**
            For non-default settings, install and start code-server manually:
            ```
            nohup /tmp/code-server/bin/code-server --bind-addr 0.0.0.0:8080 --auth none /home/daytona/workspace &
            ```
            Then call expose(target="http:8080").
            """),
        "delegate": textwrap.dedent("""\
            # Lathe — Delegate

            ## What delegate() does

            delegate(task, context_files, max_steps, foreground_seconds)
            spawns an autonomous sub-agent that runs the same model against
            the same sandbox. The sub-agent has access to: bash, read,
            write, edit, glob, grep. It does NOT have: lathe, onboard,
            expose, destroy, or delegate (no recursion).

            The sub-agent makes up to max_steps inference calls (default 10,
            max 30), executing tools as needed. It knows its step budget
            upfront and receives a wrap-up nudge when close to the limit, so
            it can prioritize producing a useful summary over starting new
            work.

            ## Foreground vs. background execution

            Like bash(), delegate() has a foreground window controlled by
            foreground_seconds (default 30, max 300). If the sub-agent
            finishes within this window, its result is returned inline
            (same as before). If the window expires, the delegation is
            auto-backgrounded: you get a descriptor with sidecar paths,
            and the sub-agent continues running asynchronously.

            ### Sidecar files

            Where DELEGATE=/tmp/delegate/<id>:

              DELEGATE/task    — the original task description
              DELEGATE/log     — timestamped progress entries (live)
              DELEGATE/result  — final sub-agent output (on success)
              DELEGATE/error   — error message (on failure)
              DELEGATE/usage   — JSON: steps, tool_calls, tokens

            DELEGATE/result or DELEGATE/error appears when the sub-agent
            finishes. Absence of both means it's still running.

            ### Monitoring recipes

            **Check if done:**
            ```
            test -f $DELEGATE/result && echo DONE || test -f $DELEGATE/error && echo FAILED || echo RUNNING
            ```

            **Read the result:**
            ```
            cat $DELEGATE/result
            ```

            **Peek at progress:**
            ```
            cat $DELEGATE/log
            ```

            **Read usage stats:**
            ```
            cat $DELEGATE/usage
            ```

            ## Agent teams and swarms

            Background delegation is the primitive for running agent teams:

            1. Fire off multiple delegates with foreground_seconds=0 (or a
               short window):
               ```
               delegate(task="Refactor module A...", foreground_seconds=0)
               delegate(task="Fix tests in module B...", foreground_seconds=0)
               delegate(task="Write docs for module C...", foreground_seconds=0)
               ```
            2. Each returns a DELEGATE id immediately.
            3. Check completion by reading result files.
            4. Delegates can coordinate through the shared filesystem —
               writing task specs, partial results, or completion sentinels
               for each other to read.

            No special swarm API is needed. The pattern falls out naturally
            from background delegation + shared sandbox filesystem.

            ## When to use delegate

            - **Exploration**: "Find all test files and determine the testing
              framework and coverage structure."
            - **Refactoring**: "Rename all occurrences of getFoo to get_foo
              across the Python codebase, updating tests."
            - **Debugging**: "The build fails with error X. Investigate,
              identify the root cause, and fix it."
            - **Research**: "Read the configuration files and summarize the
              project's dependency tree."
            - **Batch operations**: "Add type hints to all functions in
              src/utils/ that are missing them."
            - **Parallel work** (background): Fan out multiple independent
              tasks, then collect results.

            ## When NOT to use delegate

            - Single-step operations (just call the tool directly).
            - Tasks requiring user interaction (URLs, uploads, questions).
            - Tasks requiring expose() or destroy().

            ## Writing good task descriptions

            The sub-agent cannot ask clarifying questions. Be specific
            and include all relevant context (error messages, prior
            findings, instructions) directly in the task string:

            Bad:  "Fix the tests"
            Good: "Run pytest in /home/daytona/workspace. For each failure,
                   read the failing test and the source it tests, identify
                   the bug, and fix it. Re-run pytest to confirm."

            Bad:  task="Fix the build", context="Error: module X not found"
            Good: task="Fix the build. The error is: module X not found"

            ## The context_files parameter

            Pass a list of absolute sandbox paths (e.g. AGENTS.md, SKILL.md,
            config files) whose contents should be injected into the
            sub-agent's prompt. The files are fetched once at delegation
            time and appear as a "Reference Files" section — the sub-agent
            sees their full contents without spending steps reading them.

            This is the recommended way to pass project context. If you
            discovered relevant files via onboard() or glob(), name them
            here rather than pasting their contents into the task string.

            All paths must be absolute. Delegation fails immediately if
            any path does not exist on the sandbox.

            ## Step budget and handoff behavior

            The sub-agent's system prompt tells it how many steps it has
            and instructs it to reserve time for a handoff summary.
            Additionally, when 2 steps remain, Lathe injects a hard nudge
            telling the sub-agent to stop making tool calls and write a
            structured handoff: what was accomplished, what remains
            unresolved, and absolute paths to critical sandbox files
            needed to continue the work.

            This means: even if the sub-agent misjudges its pacing, it
            gets a hard nudge before the limit cuts it off. The "(no output)"
            failure mode (sub-agent exhausts budget mid-investigation) should
            be much less common. And when a sub-agent does run out of
            steps, its output is actionable — you can immediately
            delegate a follow-up with the named files as context_files.

            If you find "(no output)" still happening, increase max_steps
            or simplify the task.

            ## Cost model

            Each step is a full inference call billed to the same provider.
            A 10-step delegation costs ~10x a single tool call in tokens.
            The usage summary in the tool result shows exact token counts.
            Use max_steps to cap cost for bounded tasks.
            """),
        "handoff": textwrap.dedent("""\
            # Lathe — Handoff

            ## What handoff() does

            handoff() returns instructions for writing a handoff document.
            You write the document as your reply to the user — streamed
            text, not a tool call. The user then starts a new conversation
            and pastes the handoff as their opening message.

            ## When to use it

            - The user asks to "hand off", "compact", or "save context".
            - You notice the conversation is getting long and suggest it.
            - You can suggest the capability on your own, but don't
              initiate the handoff unless the user agrees.

            ## How it works

            1. You call handoff(). No parameters needed.
            2. The tool returns formatting instructions.
            3. You write the handoff document as reply text, following
               those instructions exactly.
            4. You stop. No more tool calls after the handoff.

            The handoff has a short preamble in the user's language
            telling them what to do, a horizontal rule, then the
            structured handoff body (Goal, Accomplished, Unresolved,
            Key files, What didn't work).

            ## Why reply text, not a file?

            Reply text streams to the user in real time. Tool call
            arguments don't appear until the agent finishes writing them
            (long delay for big writes). The user should see the handoff
            forming so they can judge its quality.

            ## The sandbox persists

            The sandbox filesystem survives across conversations. Files
            you wrote in this session will be there when the user starts
            a new conversation. The handoff document can reference sandbox
            paths, and the next agent can read them or use them as
            context_files in delegate() calls.
            """),
        "overview": textwrap.dedent("""\
            # Lathe Toolkit — Overview

            Lathe is a coding-agent toolkit running inside Open WebUI. It gives
            you a persistent Linux sandbox backed by a Daytona VM with a
            cross-conversation filesystem. The sandbox is created transparently
            on first tool use and survives across conversations for the same user.

            - Documentation: https://lathe.tools
            - Source: https://github.com/rndmcnlly/lathe

            Read this page fully before your first tool call. It covers the
            sandbox model, available workflows, and common mistakes.

            ## Sandbox model

            - One sandbox per user, identified by email. The sandbox starts,
              stops, and recovers automatically — you never manage lifecycle.
            - The default working directory is /home/daytona/workspace.
            {volume_note}\
            - The sandbox auto-stops after a configurable idle timeout and
              auto-archives after a further interval. Any tool call transparently
              restarts it. The filesystem (including installed packages and user
              files) survives both stop and archive — only running processes are
              lost.

            ## Tool catalog

            {tool_catalog}

            ## Key workflows

            **Running services and exposing them:**
            The sandbox is a server. Background a web server with nohup, then
            call expose(target="http:N") to get a public HTTPS URL the user can open.
            The sandbox auto-stops on idle, which kills background processes —
            restart the server and call expose() again if needed.

            **File upload/download/browsing:**
            When the user wants to upload, download, or browse files, call
            expose(target="dufs"). This installs and starts dufs automatically
            and returns a URL with drag-and-drop upload/download — one tool call.
            See lathe(manpage="recipes") for custom configurations.

            **Browser IDE:**
            When the user wants an IDE, call expose(target="code-server"). This
            installs and starts code-server automatically and returns a URL —
            VS Code in the browser with terminal, extensions, and file editing.
            See lathe(manpage="recipes") for custom configurations.

            **Interactive shell:**
            For interactive work, call expose(target="ssh") to give the user a
            time-limited SSH command they can paste into their terminal, VS Code
            Remote SSH, or JetBrains Gateway.

            **Project context:**
            Call onboard() at the start of a conversation to get a directory
            listing and load any AGENTS.md or skills. Works even without agent
            files (you still get the listing). Pass "" or the workspace path to
            onboard the default directory. Searches both the project directory
            and ~/.agents/ for global agent instructions and skills.

            **Delegating multi-step work:**
            Use delegate(task="...") to hand off autonomous multi-step tasks
            to a sub-agent. The sub-agent has bash/read/write/edit/glob/grep
            and runs against the same sandbox. Good for: exploration, refactoring,
            debugging, test fixing, research. Use context_files= to inject
            AGENTS.md, skills, or other reference files into the sub-agent's
            prompt without re-reading them. The sub-agent cannot interact with
            the user or expose URLs — it just works and returns a summary.
            Like bash(), delegate() auto-backgrounds if the sub-agent takes longer
            than foreground_seconds (default 30), returning a descriptor with
            sidecar file paths. Use foreground_seconds=0 to fire-and-forget
            multiple delegates in parallel (agent teams / swarms).
            See lathe(manpage="delegate") for details.

            **Persistent Python REPL:**
            Use interpret(code="...") for iterative Python work where state
            needs to persist across calls: data analysis, incremental
            computation, prototyping. Variables, imports, and definitions
            survive within the conversation. For one-shot commands or
            non-Python work, prefer bash(). The interpreter context is
            per-conversation and is lost on sandbox restart.
            See lathe(manpage="interpret") for details.

            **Network requests:**
            Use bash("curl ...") or bash("wget ...") for HTTP requests. The
            sandbox can reach a broad allowlist of hosts directly (package
            registries, git hosts, CDNs, AI APIs, etc.), and bash gives you
            streaming, piping, and natural access to env-var credentials.
            If a request fails due to egress filtering, see
            lathe(manpage="egress") for workarounds.

            **Handing off to a new conversation:**
            When context gets long or the user asks, call handoff() to write
            a structured handoff document. The user pastes it into a new
            conversation as their opening message, and the next agent picks
            up where you left off. The sandbox persists, so all files survive.
            See lathe(manpage="handoff") for details.

            ## Gotchas

            - Commands are non-interactive. No stdin prompts, no curses UIs. Use
              -y or equivalent flags. For interactive work, give the user an
              expose(target="ssh") token.
            - bash() auto-backgrounds commands that exceed ~30 seconds. When this
              happens, it returns a background descriptor with CMD and PID paths.
              Use foreground_seconds= to extend the wait (e.g. foreground_seconds=120
              for known-slow commands or when waiting for a backgrounded command to
              finish). The command keeps running even after backgrounding.
              See lathe(manpage="background") for peek/poll/kill recipes.
            - Do NOT use shell-level & + wait to parallelize work inside a single
              bash() call. Each bash() call already gets its own independent session,
              so the right way to parallelize is to make multiple bash() tool calls
              simultaneously at the agent level. Shell-level & + wait adds no
              benefit and risks tripping the foreground timeout on the wait builtin,
              producing an alarming background descriptor even when the real work
              is already done.
            - bash() output is truncated to the last 2000 lines / 50 KB. If
              truncated, the full output is available in the log file at
              /tmp/cmd/<id>/log — use read() to inspect specific sections.
            - edit() requires an exact string match (including whitespace). If
              the match is ambiguous, provide more surrounding context or use
              replace_all=true.
            - delegate() auto-backgrounds after ~30 seconds (configurable via
              foreground_seconds). Backgrounded delegates write to
              /tmp/delegate/<id>/{{log,result,error,usage}}. Use foreground_seconds=0
              to fire-and-forget for parallel agent teams.
            - expose() URLs expire after ~1 hour (call expose again for a fresh URL). The sandbox itself stops on
              idle (~15 min default), killing servers.
            - destroy() prompts for user confirmation via a dialog before proceeding. Irreversible.{destroy_volume_note}
            - **Network egress may be restricted.** Depending on the admin's
              Daytona tier, the sandbox may only reach a curated allowlist of
              hosts (package registries, git hosts, CDNs, AI APIs, etc.).
              Requests to non-allowlisted hosts silently fail or time out.
              If curl fails on a host you know is up, see
              lathe(manpage="egress") for workarounds.
            """),
    }

    # One-line descriptions for the page index (shown on unknown page
    # lookups and useful for the model to decide which page to request).
    _MANPAGE_INDEX: dict[str, str] = {
        "overview": "Big-picture orientation: sandbox model, tool catalog, key workflows, gotchas.",
        "interpret": "Persistent Python REPL: state model, when to use vs bash, limitations.",
        "delegate": "Sub-agent delegation: foreground/background, sidecar files, agent teams, cost model.",
        "handoff": "Context handoff: writing a handoff document for continuing work in a new conversation.",
        "recipes": "Bootstrap scripts for common tools: dufs (file browser), code-server (IDE).",
        "background": "Background job sidecar files, and peek/poll/kill recipes.",
        "egress": "Egress restrictions, workarounds (dufs upload, browser-side fetch), Tier 3.",
        "version": "Show the installed Lathe toolkit version.",
    }

    async def lathe(
        self,
        manpage: str = "overview",
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Manual for the lathe toolkit. Call lathe(manpage="overview") before your first tool use in a new conversation to learn the sandbox model, available workflows, and gotchas. Costs one tool call, saves many.
        :param manpage: Which manual page to return. Use "overview" for big-picture orientation, "version" for the installed version.
        """
        tool_catalog = _build_tool_catalog(self)

        if manpage == "version":
            mod_doc = globals().get("__doc__", "") or ""
            match = re.search(r"^version:\s*(.+)$", mod_doc, re.MULTILINE)
            ver = match.group(1).strip() if match else "unknown"
            await _emit(__event_emitter__, f"Lathe v{ver}", done=True)
            return f"Lathe toolkit version: {ver}"

        if manpage in self._MANPAGES:
            content = self._MANPAGES[manpage]
            if "{tool_catalog}" in content:
                content = content.format(tool_catalog=tool_catalog)
            volume_note = (
                "- /home/daytona/volume is S3/FUSE-backed persistent storage that\n"
                "              survives sandbox destruction.\n            "
                if self.valves.persistent_volume else ""
            )
            destroy_volume_note = (
                " The volume is preserved." if self.valves.persistent_volume else ""
            )
            content = content.replace("{volume_note}", volume_note)
            content = content.replace("{destroy_volume_note}", destroy_volume_note)
            await _emit(__event_emitter__, f"Manual page: {manpage}", done=True)
            return content

        # Unknown page — return the index so the model can discover what exists
        index_lines = "\n".join(
            f"  {name} — {desc}"
            for name, desc in sorted(self._MANPAGE_INDEX.items())
        )
        await _emit(__event_emitter__, f"Unknown manpage: {manpage}", done=True)
        return (
            f"Unknown manpage \"{manpage}\". Available pages:\n\n"
            f"{index_lines}\n\n"
            f"Call lathe(manpage=\"overview\") for big-picture orientation."
        )

    async def handoff(
        self,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Prepare a handoff document for continuing this work in a new conversation. Call this when the user asks to compact, hand off, or save context for a fresh chat. After calling, write the handoff as your reply — do not make any more tool calls.
        """
        await _emit(__event_emitter__, "Handoff", done=True)
        return _HANDOFF_INSTRUCTIONS

    async def destroy(
        self,
        __user__: dict = {},
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Permanently destroy the sandbox VM. Irreversible.
        Prompts the user for confirmation before proceeding.
        If a persistent volume is attached, its data is preserved and will reappear in the next sandbox.
        """
        # Gate on real human consent via OWUI's confirmation dialog.
        has_volume = self.valves.persistent_volume
        if __event_call__:
            message = (
                "This will permanently destroy the sandbox VM and "
                "all its contents. Persistent volume data is preserved. "
                "This action cannot be undone."
                if has_volume else
                "This will permanently destroy the sandbox VM and "
                "all its contents. This action cannot be undone."
            )
            confirmed = await __event_call__({
                "type": "confirmation",
                "data": {
                    "title": "Destroy sandbox?",
                    "message": message,
                },
            })
            if not confirmed:
                await _emit(__event_emitter__, "Destroy cancelled", done=True)
                return "Destroy cancelled by user."
        else:
            # Fallback: if __event_call__ is not available (old OWUI or
            # testing), refuse to proceed rather than silently destroying.
            return (
                "Error: cannot confirm destruction — interactive confirmation "
                "is not available. Update Open WebUI or try again from the chat UI."
            )
        try:
            email = _get_email(__user__)
            valves = self.valves

            if not valves.daytona_api_key:
                return "Error: Daytona API key not configured."
            if not valves.deployment_label:
                return "Error: Deployment label not configured."

            label_key = valves.deployment_label
            labels_filter = json.dumps({label_key: email})

            await _emit(__event_emitter__, "Looking up sandbox...")

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    _api(valves, "/sandbox"),
                    params={"labels": labels_filter},
                    headers=_headers(valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                sandboxes = resp.json() or []
                matches = [
                    s for s in sandboxes
                    if s.get("labels", {}).get(label_key) == email
                ]

                if not matches:
                    await _emit(__event_emitter__, "No sandbox found", done=True)
                    return "No sandbox found. One will be created on your next tool call."

                deleted = []
                for s in matches:
                    sid = s["id"]
                    await _emit(__event_emitter__, f"Destroying sandbox {sid[:12]}...")
                    resp = await client.delete(
                        _api(valves, f"/sandbox/{sid}"),
                        headers=_headers(valves),
                        params={"force": "true"},
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    deleted.append(sid)

                # Poll until deletion propagates
                for _ in range(30):
                    await asyncio.sleep(1)
                    resp = await client.get(
                        _api(valves, "/sandbox"),
                        params={"labels": labels_filter},
                        headers=_headers(valves),
                        timeout=30.0,
                    )
                    remaining = [
                        s for s in (resp.json() or [])
                        if s.get("labels", {}).get(label_key) == email
                    ]
                    if not remaining:
                        break

                await _emit(__event_emitter__, "Sandbox destroyed", done=True)
                ids = ", ".join(d[:12] for d in deleted)
                if has_volume:
                    return (
                        f"Destroyed {len(deleted)} sandbox(es) ({ids})."
                        f" Your persistent files in {VOLUME_MOUNT_PATH} are intact"
                        f" and will reappear in your next sandbox."
                        f" A fresh sandbox will be created on the next tool call."
                    )
                return (
                    f"Destroyed {len(deleted)} sandbox(es) ({ids})."
                    f" A fresh sandbox will be created on the next tool call."
                )

        except Exception as exc:
            await _emit(__event_emitter__, "Destroy failed", done=True)
            return f"Error: {exc}"

    async def onboard(
        self,
        path: str,
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        """
        Load project context (directory listing, AGENTS.md, skill catalog) at the start of a conversation.
        Always returns a directory listing of the target path. Additionally
        searches the project directory and ~/.agents/ for agent instructions and skills.
        Use read() on a skill's SKILL.md path to load its full instructions later.
        :param path: Absolute path to the project root (e.g. /home/daytona/workspace/myproject).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, "Loading project context...")

            # Models sometimes pass "" despite the docstring requiring an
            # absolute path.  Silently default to the workspace root rather
            # than error -- this is the most useful interpretation.
            p = path.strip()
            if not p:
                p = "/home/daytona/workspace"
            p = p.rstrip("/")
            script = _build_onboard_script(p)

            # Write script to temp file and execute it
            script_path = f"/tmp/_onboard_{uuid.uuid4()}.py"
            await _upload_file(
                self.valves, sandbox_id, client,
                script_path, script.encode("utf-8"),
            )

            resp = await client.post(
                _toolbox(self.valves, sandbox_id, "/process/execute"),
                headers=_headers(self.valves),
                json={
                    "command": f"python3 {script_path}",
                    "timeout": 30000,
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()

            result = data.get("result", "")
            exit_code = data.get("exitCode", -1)

            if exit_code != 0:
                await _emit(__event_emitter__, "Error loading context", done=True)
                return f"Error: onboard script failed (exit {exit_code}): {result[:500]}"

            await _emit(__event_emitter__, "Project context loaded", done=True)
            messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
            return _prepend_harness_messages(result if result else "(empty project context)", messages)

        return await _tool_context(__event_emitter__, _run)

    async def bash(
        self,
        command: str,
        workdir: str = "/home/daytona/workspace",
        foreground_seconds: int = -1,
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        # Strict type check at the wrapper boundary.
        type_err = _check_tool_params(
            {"foreground_seconds": foreground_seconds},
            {"foreground_seconds": int},
        )
        if type_err:
            return type_err

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, "Running command...")

            # Collect user-supplied env vars from UserValves (never logged).
            # Raises ValueError (caught below) if env_vars is malformed.
            user_valves = __user__.get("valves")
            user_pairs: list[tuple[str, str]] = []
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                user_pairs = _parse_env_vars(raw_env)

            # Negative (default) falls back to the valve setting.
            # 0 means immediate background (fire-and-forget), matching
            # delegate() semantics.  Positive values are the foreground
            # window in seconds.
            if foreground_seconds < 0:
                fg_seconds = self.valves.foreground_timeout_seconds
            else:
                fg_seconds = foreground_seconds

            # When the command auto-backgrounds, spawn a polling coroutine
            # to push a completion notice into the chat's message queue.
            def _on_bg(*, sandbox_id, session_id, session_cmd_id, cmd_id, start_time):
                asyncio.ensure_future(_poll_bg_bash(
                    self.valves, sandbox_id, session_id, session_cmd_id,
                    cmd_id, start_time,
                    self._chat_state, __chat_id__,
                ))

            result = await _core_bash(
                self.valves, sandbox_id, client,
                command=command,
                workdir=workdir,
                user_pairs=user_pairs,
                foreground_seconds=fg_seconds,
                emit=__event_emitter__,
                on_background=_on_bg,
            )

            await _emit(__event_emitter__, "Command complete", done=True)
            messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
            return _prepend_harness_messages(result, messages)

        return await _tool_context(__event_emitter__, _run)
    bash.__doc__ = inspect.getdoc(_core_bash)

    read = _standard_tool(
        _core_read,
        emit_start="Reading {kwargs[path]}...",
        emit_done="Read complete",
    )

    glob = _standard_tool(
        _core_glob,
        emit_start="Searching for {kwargs[pattern]}...",
        emit_done="Search complete",
    )

    grep = _standard_tool(
        _core_grep,
        emit_start="Searching for {kwargs[pattern]!r}...",
        emit_done="Search complete",
    )

    write = _standard_tool(
        _core_write,
        emit_start="Writing {kwargs[path]}...",
        emit_done="Write complete",
    )

    edit = _standard_tool(
        _core_edit,
        emit_start="Editing {kwargs[path]}...",
        emit_done="Edit complete",
    )

    interpret = _standard_tool(
        _core_interpret,
        emit_start="Running Python...",
        emit_done="Execution complete",
        extra_core_kwargs=lambda self, sandbox_id, chat_id: {
            "chat_state": self._chat_state,
            "chat_id": chat_id,
        },
    )

    async def delegate(
        self,
        task: str,
        context_files: list[str] = [],
        max_steps: int = 10,
        foreground_seconds: int = -1,
        __user__: dict = {},
        __chat_id__: str = "",
        __model__: dict = {},
        __request__=None,
        __event_emitter__=None,
    ) -> str:
        """
        Delegate a multi-step task to an autonomous sub-agent with sandbox access.
        The sub-agent runs the same model, has bash/read/write/edit/glob/grep tools,
        and returns a summary when done. Use for exploration, refactoring, debugging,
        or any multi-step work that doesn't need user interaction.
        Long-running delegations auto-background and return a descriptor with log/result
        file paths for monitoring, exactly like bash() does for long commands.
        :param task: What the sub-agent should accomplish. Be specific — it cannot ask clarifying questions. Include any context (error messages, prior findings, instructions) directly in the task description.
        :param context_files: Absolute sandbox file paths to inject into the sub-agent's prompt (e.g. AGENTS.md, SKILL.md, config files). Fetched at delegation time — the sub-agent sees their contents without spending steps reading them.
        :param max_steps: Maximum inference calls the sub-agent may make (default: 10, max: 30).
        :param foreground_seconds: Seconds to wait before auto-backgrounding (default: 30, max: 300). Set 0 for immediate background (fire-and-forget). Omit or set -1 to use the default.
        """
        # Strict type check at the wrapper boundary.
        type_err = _check_tool_params(
            {"context_files": context_files, "max_steps": max_steps,
             "foreground_seconds": foreground_seconds},
            {"context_files": list, "max_steps": int, "foreground_seconds": int},
        )
        if type_err:
            return type_err

        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            await _emit(__event_emitter__, "Preparing delegate...")

            # ── Resolve model and build ASGI transport ───────────────
            if __request__ is None:
                return "Error: delegate() requires the OWUI request context (__request__). This tool only works inside Open WebUI."

            model_id = __model__.get("id", "")
            if not model_id:
                return "Error: delegate() could not determine the current model ID."

            # Extract JWT for authenticating against OWUI's own API
            token = ""
            try:
                token = __request__.state.token.credentials
            except AttributeError:
                return "Error: delegate() could not extract authentication token from request."

            # ASGI transport — in-process call to OWUI's FastAPI app.
            # Uses /api/chat/completions which handles all model types:
            # direct connection models, workspace models, AND pipe/manifold
            # models (which have custom routing like Anthropic caching).
            # The /openai/chat/completions endpoint only knows about raw
            # connection models and cannot route pipe models.
            app = __request__.app
            transport = httpx.ASGITransport(app=app)
            inner_client = httpx.AsyncClient(transport=transport, base_url="http://localhost")

            from pydantic_ai import Agent, UsageLimits
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            provider = OpenAIProvider(
                base_url="http://localhost/api",
                api_key=token,
                http_client=inner_client,
            )
            model = OpenAIChatModel(model_id, provider=provider)

            # ── Collect user env vars for sub-agent bash ─────────────
            user_valves = __user__.get("valves")
            user_pairs: list[tuple[str, str]] = []
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                user_pairs = _parse_env_vars(raw_env)

            # ── Background-safe client for the sub-agent ─────────────
            # _tool_context closes `client` when _run() returns, which
            # happens immediately when we background.  The sub-agent's
            # tool closures and sidecar writes need a client that stays
            # open for the lifetime of the background task.  We create
            # bg_client here; _run_agent closes it in its finally block.
            bg_client = httpx.AsyncClient()

            # ── Build sub-agent tools ────────────────────────────────
            tools = _build_delegate_tools(self.valves, sandbox_id, bg_client, user_pairs,
                                            chat_state=self._chat_state, chat_id=__chat_id__)

            # ── Fetch context_files from sandbox ─────────────────────
            file_sections: list[str] = []
            if context_files:
                for fpath in context_files:
                    err = _require_abs_path(fpath, "context_files entry")
                    if err:
                        return err
                for fpath in context_files:
                    resp = await client.get(
                        _toolbox(self.valves, sandbox_id, "/files/download"),
                        params={"path": fpath},
                        headers=_headers(self.valves),
                        timeout=60.0,
                    )
                    if resp.status_code == 404:
                        return (
                            f"Error: context_files entry not found on sandbox: {fpath}\n"
                            f"Verify the path exists before delegating."
                        )
                    resp.raise_for_status()
                    file_sections.append(f"### {fpath}\n\n{resp.text}")

            # ── Build the prompt ─────────────────────────────────────
            user_message = _build_delegate_prompt(task, file_sections)

            # ── Create and run the agent ─────────────────────────────
            clamped_steps = max(1, min(30, max_steps))

            agent = Agent(
                model,
                system_prompt=_build_delegate_system_prompt(clamped_steps, has_volume=self.valves.persistent_volume),
                tools=tools,
                output_type=str,
            )

            # ── Sidecar directory on the sandbox ─────────────────────
            delegate_id = str(uuid.uuid4())
            delegate_dir = f"/tmp/delegate/{delegate_id}"
            log_path = f"{delegate_dir}/log"
            result_path = f"{delegate_dir}/result"
            error_path = f"{delegate_dir}/error"
            usage_path = f"{delegate_dir}/usage"
            task_path = f"{delegate_dir}/task"

            # Write the task file immediately
            await _core_write(
                self.valves, sandbox_id, client,
                path=task_path, content=task,
            )
            # Initialize empty log file
            await _core_write(
                self.valves, sandbox_id, client,
                path=log_path, content="",
            )

            await _emit(__event_emitter__, "Sub-agent starting...")

            # ── Foreground timeout ───────────────────────────────────
            # The signature default is -1 (meaning "use the module default").
            # Explicit 0 means immediate background (fire-and-forget).
            # Any positive value is the actual foreground window.
            if foreground_seconds < 0:
                fg_seconds = _DELEGATE_FOREGROUND_SECONDS
            else:
                fg_seconds = min(300, foreground_seconds)

            # Log lines accumulated during execution (for sidecar and
            # for the background descriptor preview).
            log_lines: list[str] = []

            async def _append_log(line: str):
                """Append a timestamped line to the in-memory log and sandbox file."""
                ts = time.strftime("%H:%M:%S")
                entry = f"[{ts}] {line}"
                log_lines.append(entry)
                try:
                    await _core_write(
                        self.valves, sandbox_id, bg_client,
                        path=log_path, content="\n".join(log_lines) + "\n",
                    )
                except Exception as e:
                    logger.debug("delegate: sidecar log write failed: %s", e)

            # ── The actual sub-agent execution coroutine ─────────────
            # This is separated so it can either run inline (foreground)
            # or be detached as a background task.

            agent_done = asyncio.Event()
            agent_result: dict = {}  # populated by _run_agent
            # Outer scope sets these when backgrounding; _run_agent reads
            # them via closure (Python closures capture the variable, not
            # the value, so outer rebinds are visible to the inner function).
            emit_to_owui = True
            was_backgrounded = False
            bg_start_time = time.time()

            async def _run_agent():
                """Run the sub-agent to completion, writing sidecar files."""
                step_count = 0
                nudge_injected = False

                try:
                    async with agent.iter(
                        user_message,
                        usage_limits=UsageLimits(request_limit=clamped_steps),
                    ) as agent_run:
                        async for node in agent_run:
                            if Agent.is_model_request_node(node):
                                step_count += 1
                                remaining = clamped_steps - step_count
                                status = f"Sub-agent thinking... ({step_count}/{clamped_steps})"
                                await _append_log(status)
                                if emit_to_owui:
                                    await _emit(__event_emitter__, status)
                                # Inject wrap-up nudge when approaching the limit
                                if (
                                    remaining <= _DELEGATE_NUDGE_REMAINING
                                    and remaining > 0
                                    and not nudge_injected
                                ):
                                    from pydantic_ai.messages import ModelRequest, UserPromptPart
                                    nudge_msg = ModelRequest(parts=[UserPromptPart(
                                        content=(
                                            f"[SYSTEM: You have {remaining} step(s) remaining out of "
                                            f"{clamped_steps}. Do not make any more tool calls. Use "
                                            f"your final step to hand off your work. Write:\n"
                                            f"1. What you accomplished.\n"
                                            f"2. What remains unresolved — specific next steps.\n"
                                            f"3. Absolute paths to critical sandbox files the calling "
                                            f"agent or a follow-up sub-agent needs to continue "
                                            f"(modified sources, failing tests, relevant logs).]"
                                        ),
                                    )])
                                    agent_run._graph_run.state.message_history.append(nudge_msg)
                                    nudge_injected = True

                            elif Agent.is_call_tools_node(node):
                                # Extract tool names and args summary
                                calls = []
                                for p in node.model_response.parts:
                                    if hasattr(p, "tool_name"):
                                        name = p.tool_name
                                        # Build a short args hint
                                        try:
                                            args = p.args_as_dict()
                                            if name == "bash" and "command" in args:
                                                cmd = args["command"]
                                                hint = cmd[:60] + ("..." if len(cmd) > 60 else "")
                                                calls.append(f"bash({hint!r})")
                                            elif name in ("read", "write", "edit") and "path" in args:
                                                path = args["path"]
                                                short = path.rsplit("/", 1)[-1]
                                                calls.append(f"{name}({short})")
                                            elif name in ("glob", "grep") and "pattern" in args:
                                                calls.append(f"{name}({args['pattern']!r})")
                                            else:
                                                calls.append(name)
                                        except Exception:
                                            calls.append(name)
                                if calls:
                                    tool_status = f"Sub-agent → {', '.join(calls)}"
                                    await _append_log(tool_status)
                                    if emit_to_owui:
                                        await _emit(__event_emitter__, tool_status)

                        usage = agent_run.usage()
                        result_output = agent_run.result.output if agent_run.result else "(no output)"

                    # ── Write sidecar result files ────────────────────
                    await _core_write(
                        self.valves, sandbox_id, bg_client,
                        path=result_path, content=result_output,
                    )
                    usage_data = {
                        "steps": step_count,
                        "tool_calls": usage.tool_calls,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    }
                    await _core_write(
                        self.valves, sandbox_id, bg_client,
                        path=usage_path, content=json.dumps(usage_data),
                    )
                    await _append_log(
                        f"Completed: {step_count} steps, {usage.tool_calls} tool calls"
                    )

                    agent_result.update({
                        "ok": True,
                        "output": result_output,
                        "step_count": step_count,
                        "usage": usage,
                    })

                except Exception as e:
                    error_type = type(e).__name__
                    error_detail = str(e)
                    # Detect provider-level failures (malformed upstream responses)
                    # and surface a concise, actionable message instead of raw
                    # pydantic validation noise.
                    if "UnexpectedModelBehavior" in error_type or (
                        "validation error" in error_detail.lower()
                        and "ChatCompletion" in error_detail
                    ):
                        error_msg = (
                            f"Delegation failed after {step_count} step(s): "
                            f"the inference provider returned a malformed response "
                            f"(not a valid ChatCompletion). This is usually a transient "
                            f"upstream issue — retry the delegation."
                        )
                    else:
                        error_msg = f"Delegation failed after {step_count} step(s): {error_type}: {error_detail}"
                    agent_result.update({
                        "ok": False,
                        "error": error_msg,
                        "step_count": step_count,
                    })
                    try:
                        await _core_write(
                            self.valves, sandbox_id, bg_client,
                            path=error_path, content=error_msg,
                        )
                        await _append_log(f"Error: {error_msg}")
                    except Exception as e2:
                        logger.debug("delegate: sidecar error write failed: %s", e2)

                finally:
                    agent_done.set()
                    # Push a completion notice if the delegate was backgrounded.
                    if was_backgrounded:
                        elapsed = int(time.time() - bg_start_time)
                        notice = _format_bg_delegate_notice(
                            delegate_id, elapsed,
                            step_count=agent_result.get("step_count", step_count),
                            tool_calls=(agent_result.get("usage").tool_calls
                                        if agent_result.get("usage") else 0),
                            result_preview=agent_result.get("output", ""),
                            error=agent_result.get("error"),
                        )
                        _push_bg_notice(self._chat_state, __chat_id__, notice)
                    await inner_client.aclose()
                    await bg_client.aclose()

            # ── Launch and wait with foreground timeout ───────────────
            # We run _run_agent as a background task. During the foreground
            # window, we poll agent_done. If it finishes in time, we return
            # inline. Otherwise, we return a background descriptor and the
            # task continues.

            bg_task = asyncio.ensure_future(_run_agent())

            # Wait for completion or timeout
            try:
                await asyncio.wait_for(
                    asyncio.shield(agent_done.wait()),
                    timeout=fg_seconds if fg_seconds > 0 else 0.01,
                )
            except asyncio.TimeoutError:
                pass  # foreground window expired, proceed to background

            if agent_done.is_set():
                # ── Finished in foreground ───────────────────────────
                await bg_task

                if agent_result.get("ok"):
                    result_output = agent_result["output"]
                    usage = agent_result["usage"]
                    step_count = agent_result["step_count"]
                    parts = [f"{step_count} step(s)", f"{usage.tool_calls} tool call(s)"]
                    if usage.input_tokens:
                        parts.append(f"{usage.input_tokens:,} prompt + {usage.output_tokens:,} completion tokens")
                    usage_note = f"\n\n[Delegate completed: {', '.join(parts)}]"
                    await _emit(
                        __event_emitter__,
                        f"Delegation complete ({step_count} steps, {usage.tool_calls} tool calls)",
                        done=True,
                    )
                    messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
                    return _prepend_harness_messages(result_output + usage_note, messages)
                else:
                    error_msg = agent_result.get("error", "Unknown error")
                    await _emit(__event_emitter__, "Delegation failed", done=True)
                    messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
                    return _prepend_harness_messages(error_msg, messages)

            else:
                # ── Backgrounded — return descriptor ─────────────────
                # Kill the emitter BEFORE returning so the still-running
                # _run_agent coroutine never tries to call it.  The OWUI
                # response stream will be closed once we return, and
                # awaiting the emitter after that can hang or raise,
                # stalling the background task entirely.
                emit_to_owui = False
                was_backgrounded = True
                bg_start_time = time.time()

                elapsed = fg_seconds
                # Grab recent log lines for the preview
                preview = "\n".join(log_lines[-10:]) if log_lines else ""
                await _emit(
                    __event_emitter__,
                    f"Delegation backgrounded after {elapsed}s",
                    done=True,
                )
                messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
                return _prepend_harness_messages(
                    _format_delegate_background(delegate_id, elapsed, preview),
                    messages,
                )

        return await _tool_context(__event_emitter__, _run)

    async def expose(
        self,
        target: str,
        __user__: dict = {},
        __chat_id__: str = "",
        __event_emitter__=None,
    ) -> str:
        """
        Expose a sandbox service to the user. Pass "dufs" for a one-step file
        browser, "code-server" for a one-step IDE, "http:<port>" for a web
        server you already started, or "ssh" for interactive shell access.
        :param target: What to expose — "dufs" for file upload/download, "code-server" for a browser IDE, "ssh" for a shell, or "http:<port>" (e.g. "http:5000", port range 3000–9999) for an HTTP service you started manually.
        """
        async def _run(client):
            target_stripped = target.strip().lower()

            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _ensure_chat_init(
                self.valves, sandbox_id, client,
                self._chat_state, __chat_id__, __user__, __event_emitter__,
            )

            # ── Shared helper: ensure service + sign URL ────────────
            async def _ensure_and_sign(ensure_script, script_timeout_ms,
                                       http_timeout, svc_port, svc_name,
                                       ready_status, fail_status, result_msg):
                """Run ensure-script if provided, then sign a preview URL."""
                pid = "?"
                if ensure_script:
                    await _emit(__event_emitter__, f"Preparing {svc_name}...")
                    resp = await client.post(
                        _toolbox(self.valves, sandbox_id, "/process/execute"),
                        headers=_headers(self.valves),
                        json={"command": ensure_script, "timeout": script_timeout_ms},
                        timeout=http_timeout,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    result = data.get("result", "")
                    if data.get("exitCode", -1) != 0 or "READY" not in result:
                        await _emit(__event_emitter__, fail_status, done=True)
                        return (
                            f"Error: {svc_name} setup failed (exit {data.get('exitCode')}).\n"
                            f"{result[:500]}\n\n"
                            f"This usually means the sandbox cannot reach the install host "
                            f"(egress filtering). See lathe(manpage=\"egress\") for workarounds, or "
                            f"install {svc_name} manually and use expose(target=\"http:{svc_port}\")."
                        )
                    pid = _extract_pid(result)
                else:
                    await _emit(__event_emitter__, f"Generating URL for port {svc_port}...")

                await _emit(__event_emitter__, "Generating URL...")
                resp = await client.get(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ports/{svc_port}/signed-preview-url"),
                    params={"expiresInSeconds": 3600},
                    headers=_headers(self.valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                url = resp.json().get("url", "")
                if not url:
                    return "Error: Daytona returned an empty URL."

                await _emit(__event_emitter__, ready_status, done=True)
                messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
                return _prepend_harness_messages(result_msg(url, pid), messages)

            if target_stripped == "ssh":
                await _emit(__event_emitter__, "Creating SSH access token...")
                resp = await client.post(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ssh-access"),
                    params={"expiresInMinutes": 60},
                    headers=_headers(self.valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()

                ssh_command = data.get("sshCommand", "")
                if not ssh_command:
                    token = data.get("token", "")
                    if not token:
                        return "Error: Daytona returned neither sshCommand nor token."
                    ssh_command = f"ssh {token}@ssh.app.daytona.io"

                await _emit(__event_emitter__, "SSH access ready", done=True)
                messages = _drain_harness_messages(self._chat_state, __chat_id__, _sb_warning)
                return _prepend_harness_messages(
                    f"SSH command (valid 60 min):\n\n"
                    f"```\n{ssh_command}\n```\n\n"
                    f"The user can paste this into their terminal, VS Code Remote SSH, "
                    f"or JetBrains Gateway.\n\n"
                    f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity. "
                    f"Active SSH sessions keep the sandbox alive.",
                    messages,
                )

            if target_stripped == "dufs":
                return await _ensure_and_sign(
                    ensure_script=_DUFS_ENSURE_SCRIPT,
                    script_timeout_ms=30000, http_timeout=60.0,
                    svc_port=_DUFS_PORT, svc_name="dufs",
                    ready_status="File browser ready",
                    fail_status="dufs setup failed",
                    result_msg=lambda url, pid: (
                        f"File browser URL (valid ~1 hour): {url}\n\n"
                        f"Give this URL to the user. In their browser they can:\n"
                        f"- **Upload**: drag and drop files onto the page\n"
                        f"- **Download**: click any file\n"
                        f"- **Browse**: navigate folders\n\n"
                        f"dufs is serving {_DUFS_ROOT} on port {_DUFS_PORT} (PID {pid})."
                    ),
                )

            if target_stripped == "code-server":
                return await _ensure_and_sign(
                    ensure_script=_CS_ENSURE_SCRIPT,
                    script_timeout_ms=60000, http_timeout=120.0,
                    svc_port=_CS_PORT, svc_name="code-server",
                    ready_status="IDE ready",
                    fail_status="code-server setup failed",
                    result_msg=lambda url, pid: (
                        f"IDE URL (valid ~1 hour): {url}\n\n"
                        f"Give this URL to the user. They get VS Code in the browser with:\n"
                        f"- Full terminal access\n"
                        f"- File editing and navigation\n"
                        f"- Extension support\n\n"
                        f"code-server is serving {_CS_ROOT} on port {_CS_PORT} (PID {pid})."
                    ),
                )

            if not target_stripped.startswith("http:"):
                return (
                    f"Error: target must be \"ssh\", \"dufs\", \"code-server\", or \"http:<port>\" "
                    f"(e.g. \"http:5000\"). Got: \"{target}\""
                )
            port_str = target_stripped.removeprefix("http:")
            try:
                port = int(port_str)
            except (ValueError, TypeError):
                return (
                    f"Error: port in \"http:<port>\" must be a number. "
                    f"Got: \"{target}\""
                )
            if port < 3000 or port > 9999:
                return (
                    f"Error: port must be between 3000 and 9999. Got: {port}"
                )

            return await _ensure_and_sign(
                ensure_script=None,
                script_timeout_ms=0, http_timeout=0,
                svc_port=port, svc_name="service",
                ready_status=f"URL ready (port {port})",
                fail_status="",
                result_msg=lambda url, pid: (
                    f"Public URL (valid ~1 hour): {url}\n\n"
                    f"The user can open this in a new browser tab. "
                    f"They may see a Daytona security warning on first visit — they can click through it.\n\n"
                    f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity regardless of "
                    f"running background processes, killing the server. If the user reports "
                    f"the URL stopped working, restart the server and call expose() again."
                ),
            )

        return await _tool_context(__event_emitter__, _run)

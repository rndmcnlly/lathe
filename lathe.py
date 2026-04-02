"""
title: Lathe
author: Adam Smith
author_url: https://adamsmith.as
description: Coding agent tools (lathe, bash, read, write, edit, glob, grep, delegate, onboard, expose, destroy) backed by per-user sandbox VMs with transparent lifecycle management.
required_open_webui_version: 0.4.0
requirements: httpx, pydantic-ai-slim[openai]
version: 0.13.0
licence: MIT
"""

import asyncio
import inspect
import io
import json
import textwrap
import time
import urllib.parse
import uuid

import httpx
from pydantic import BaseModel, Field


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


def _prepend_warning(result: str, warning: str | None) -> str:
    """Prepend a sandbox lifecycle warning to a tool result if present."""
    return f"{warning}\n{result}" if warning else result


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe use in shell scripts."""
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_env_vars(env_vars: str) -> list[tuple[str, str]]:
    """Parse a JSON object string into (key, value) pairs.

    Expects a JSON object mapping string keys to string values,
    e.g. '{"MY_TOKEN":"abc123","FOO":"bar"}'.
    Keys must match [A-Za-z_][A-Za-z0-9_]*; invalid keys are skipped with a warning.
    Returns [] on empty input (not an error).
    Raises ValueError on malformed input so the caller can surface it to the agent.
    """
    import re
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
    import re
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


# ── hierarchical glob (runs on sandbox) ──────────────────────────────

_GLOB_MAX_LINES = 100

_GLOB_SCRIPT = r'''
import os
import sys
from pathlib import Path


def glob_hierarchy(base_dir, pattern, max_lines):
    base = Path(base_dir).resolve()
    if not base.is_dir():
        return f"Error: not a directory: {base_dir}"

    # ── Parse pattern: comma-separated, !prefix = exclude ────────
    # Result = union(positive) - union(negative), order-independent.
    # Commas inside {braces} are part of glob syntax, not delimiters.
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

    if not positive:
        return f"Error: pattern must include at least one positive glob (got {pattern!r})"

    # ── Exhaustive glob ──────────────────────────────────────────
    base_prefix = str(base) + os.sep

    def _collect(globs):
        result = set()
        for g in globs:
            for p in base.glob(g):
                s = str(p.resolve())
                if p.is_file() and s.startswith(base_prefix):
                    result.add(s)
        return result

    included = _collect(positive)
    if negative:
        included -= _collect(negative)
    matches = sorted(included)

    if not matches:
        return f"0 matches for {pattern!r} in {base}"

    # ── Build trie ───────────────────────────────────────────────
    root = {"children": {}, "files": [], "count": 0, "path": str(base)}

    for filepath in matches:
        rel = os.path.relpath(filepath, base)
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

    header = f"{len(matches)} matches for {pattern!r} in {base}"
    has_collapsed = bool(list(_collapsible(root))) or bool(partial_limit)
    if has_collapsed:
        header += f" (budget: {max_lines} lines, some directories collapsed)"

    return header + "\n" + "\n".join(output)
'''


# ── hierarchical grep (runs on sandbox) ──────────────────────────────

_GREP_MAX_LINES = 100

_GREP_SCRIPT = r'''
import os
import re
import sys
from pathlib import Path

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

    # ── Parse file scope: comma-separated, !prefix = exclude ────
    terms, current, depth = [], [], 0
    for ch in files_pattern:
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

    if not positive:
        return f"Error: files pattern must include at least one positive glob (got {files_pattern!r})"

    # ── Collect files ────────────────────────────────────────────
    base_prefix = str(base) + os.sep

    def _collect(globs):
        result = set()
        for g in globs:
            for p in base.glob(g):
                s = str(p.resolve())
                if p.is_file() and s.startswith(base_prefix):
                    result.add(s)
        return result

    included = _collect(positive)
    if negative:
        included -= _collect(negative)
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

    # ── Build trie of files with matches ─────────────────────────
    root = {
        "children": {}, "files": {}, "path": str(base),
        "n_files": 0, "n_matches": 0,
    }

    for filepath, hits in sorted(file_matches.items()):
        rel = os.path.relpath(filepath, base)
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


# ── shared tool cores ───────────────────────────────────────────────
#
# Each _core_* function encapsulates the I/O logic for a tool.  Both
# the Tools class methods and the delegate closures call these, so
# behavior stays in sync.  The dependency signature is explicit:
# (valves, sandbox_id, client, **tool_params) -> str.
#
# Docstrings on _core_* are the **single source of truth** for tool
# descriptions and parameter docs.  Use :param: format.  The decorator
# _doc_from_core() converts them to Google-style Args: blocks for the
# delegate closures (which pydantic-ai parses for schema generation).
# Tools class methods can add OWUI-specific behavioral guidance beyond
# what the core docstring says.


def _doc_from_core(core_fn):
    """Decorator: copy a _core_* docstring onto a target function.

    Both OWUI and pydantic-ai parse :param: format natively, so no
    conversion is needed.  Extra :param: lines for params not in the
    target's signature (valves, sandbox_id, client, etc.) are silently
    ignored by both schema generators.
    """
    doc = inspect.getdoc(core_fn) or ""
    def decorator(fn):
        fn.__doc__ = doc
        return fn
    return decorator


async def _core_read(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                     path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a file from the sandbox. Returns numbered lines.

    :param path: Absolute path to the file.
    :param offset: Starting line number (1-indexed, default: 1).
    :param limit: Max lines to return (default: 2000).
    """
    err = _require_abs_path(path)
    if err:
        return err
    resp = await client.get(
        _toolbox(valves, sandbox_id, "/files/download"),
        params={"path": path},
        headers=_headers(valves),
        timeout=60.0,
    )
    if resp.status_code == 404:
        return f"Error: File not found: {path}"
    resp.raise_for_status()
    content = resp.text
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    total_lines = len(lines)
    start_idx = max(0, offset - 1)
    end_idx = start_idx + limit
    selected = lines[start_idx:end_idx]
    numbered = "\n".join(
        f"{start_idx + i + 1}: {line}"
        for i, line in enumerate(selected)
    )
    header = f"File: {path} ({total_lines} lines total)"
    if start_idx > 0 or end_idx < total_lines:
        header += f", showing lines {start_idx + 1}-{min(end_idx, total_lines)}"
    return f"{header}\n{numbered}"


async def _core_write(valves, sandbox_id: str, client: httpx.AsyncClient, *,
                      path: str, content: str) -> str:
    """Write a file to the sandbox (creates parents automatically).

    :param path: Absolute path to write to.
    :param content: The full file content.
    """
    err = _require_abs_path(path)
    if err:
        return err
    parent = "/".join(path.rstrip("/").split("/")[:-1])
    if parent:
        await client.post(
            _toolbox(valves, sandbox_id, "/files/folder"),
            headers=_headers(valves),
            json={"path": parent, "mode": "755"},
            timeout=30.0,
        )
    content_bytes = content.encode("utf-8")
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/files/upload"),
        params={"path": path},
        headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
        files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
        timeout=60.0,
    )
    resp.raise_for_status()
    n_bytes = len(content_bytes)
    n_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    return f"Wrote {n_bytes} bytes ({n_lines} lines) to {path}"


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
    resp = await client.get(
        _toolbox(valves, sandbox_id, "/files/download"),
        params={"path": path},
        headers=_headers(valves),
        timeout=60.0,
    )
    if resp.status_code == 404:
        return f"Error: File not found: {path}"
    resp.raise_for_status()
    content = resp.text
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
    else:
        new_content = content.replace(old_string, new_string, 1)
    content_bytes = new_content.encode("utf-8")
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/files/upload"),
        params={"path": path},
        headers={"Authorization": f"Bearer {valves.daytona_api_key}"},
        files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
        timeout=60.0,
    )
    resp.raise_for_status()
    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {path}"


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
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/process/execute"),
        headers=_headers(valves),
        json={"command": f"python3 -c {_shell_quote(script)}", "timeout": 30000},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", "")
    exit_code = data.get("exitCode", -1)
    if exit_code != 0:
        return f"Error: glob script failed (exit {exit_code}).\n{result[:500]}"
    return result


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
    resp = await client.post(
        _toolbox(valves, sandbox_id, "/process/execute"),
        headers=_headers(valves),
        json={"command": f"python3 -c {_shell_quote(script)}", "timeout": 30000},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result", "")
    exit_code = data.get("exitCode", -1)
    if exit_code != 0:
        return f"Error: grep script failed (exit {exit_code}).\n{result[:500]}"
    return result


# ── bash core (session + sidecar protocol) ──────────────────────────


def _build_bash_script(command: str, user_pairs: list[tuple[str, str]],
                       pid_path: str, log_path: str) -> str:
    """Build the bash wrapper script with sidecar file setup.

    The script:
    - Sets standard non-interactive env vars
    - Injects user env vars (shell-quoted)
    - Writes its own PID to pid_path
    - Tees stdout+stderr to log_path
    - Executes the user command
    """
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
    """Format bash output for return to the caller.

    Args:
        output: The raw command output (possibly already truncated).
        exit_code: Process exit code, or None if still running.
        was_truncated: Whether _truncate_tail truncated the output.
        meta: Truncation metadata from _truncate_tail.
        spill_path: Path to the full log file on disk (for truncation notice).
        background_info: If set, dict with keys 'elapsed', 'cmd_id' for
            the auto-background notice.
    """
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
                     emit=None) -> str:
    """Execute a bash command in the sandbox. Non-interactive only.
    Commands that finish within the foreground window return output directly.
    Long-running commands auto-background and return a descriptor with log
    file paths for monitoring.

    :param command: The bash command to execute.
    :param workdir: Working directory (default: /home/daytona/workspace).
    :param foreground_seconds: Seconds to wait before auto-backgrounding (default: 15). Use higher values for known-slow commands.
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


def _build_onboard_script(project_path: str) -> str:
    """Build a Python script that collects agent context from a sandbox.

    Searches two locations:
      1. ~/.agents/          — global agent instructions and skills
      2. <project_path>/     — project-local instructions and skills

    Skills are merged into a single catalog.  On name collision, the
    project-level entry wins (more specific scope takes precedence).
    """
    # The script is a self-contained Python program executed on the sandbox.
    # project_path is injected via repr() so it's safely quoted as a
    # Python string literal.  The rest of the script uses no interpolation.
    return "import os, glob\n\nPROJECT = " + repr(project_path) + "\n" + textwrap.dedent("""\
        GLOBAL  = os.path.expanduser("~/.agents")

        sections = []
        found = False

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
            found = True

        project_md = read_agents_md(PROJECT, "Project Agent Instructions")
        if project_md:
            sections.append(project_md)
            found = True

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
            found = True
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

        if not found:
            print("ERROR_NO_CONTEXT")
            raise SystemExit(1)

        print("\\n\\n---\\n\\n".join(sections))
    """)



# ── delegate() sub-agent infrastructure ─────────────────────────────


def _build_delegate_prompt(task: str, file_sections: list[str]) -> str:
    """Build the user message for a delegate sub-agent.

    Args:
        task: The task description (including any context).
        file_sections: Pre-formatted file sections (each "### path\\n\\ncontent").
    """
    parts: list[str] = [f"## Task\n\n{task}"]
    if file_sections:
        parts.append(f"## Reference Files\n\n" + "\n\n".join(file_sections))
    return "\n\n".join(parts)



_DELEGATE_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a focused sub-agent with direct access to a Linux sandbox.
    You have been delegated a specific task by the calling agent.

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
    - /home/daytona/volume is persistent storage that survives sandbox destruction.
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
_DELEGATE_WITHHELD = {"lathe", "onboard", "expose", "destroy", "delegate"}


_DELEGATE_BASH_FOREGROUND_SECONDS = 15

def _build_delegate_tools(valves, sandbox_id: str, client: httpx.AsyncClient, user_pairs: list[tuple[str, str]]):
    """Build pydantic-ai Tool objects that operate against a resolved sandbox.

    Returns a list of Tool instances. Each tool is a thin closure over the
    already-resolved sandbox_id and client — no per-call sandbox lookup.
    The closures delegate to the shared _core_* functions.
    """
    from pydantic_ai import Tool

    # ── bash ─────────────────────────────────────────────────────────
    @_doc_from_core(_core_bash)
    async def bash(command: str, workdir: str = "/home/daytona/workspace",
                   foreground_seconds: int = _DELEGATE_BASH_FOREGROUND_SECONDS) -> str:
        return await _core_bash(
            valves, sandbox_id, client,
            command=command,
            workdir=workdir,
            user_pairs=user_pairs,
            foreground_seconds=foreground_seconds,
        )

    # ── read ─────────────────────────────────────────────────────────
    @_doc_from_core(_core_read)
    async def read(path: str, offset: int = 1, limit: int = 2000) -> str:
        return await _core_read(
            valves, sandbox_id, client,
            path=path, offset=offset, limit=limit,
        )

    # ── write ────────────────────────────────────────────────────────
    @_doc_from_core(_core_write)
    async def write(path: str, content: str) -> str:
        return await _core_write(
            valves, sandbox_id, client,
            path=path, content=content,
        )

    # ── edit ─────────────────────────────────────────────────────────
    @_doc_from_core(_core_edit)
    async def edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        return await _core_edit(
            valves, sandbox_id, client,
            path=path, old_string=old_string, new_string=new_string,
            replace_all=replace_all,
        )

    # ── glob ─────────────────────────────────────────────────────────
    @_doc_from_core(_core_glob)
    async def glob(pattern: str, max_lines: int = _GLOB_MAX_LINES) -> str:
        return await _core_glob(
            valves, sandbox_id, client,
            pattern=pattern, max_lines=max_lines,
        )

    # ── grep ─────────────────────────────────────────────────────────
    @_doc_from_core(_core_grep)
    async def grep(pattern: str, files: str = "**/*", max_lines: int = _GREP_MAX_LINES) -> str:
        return await _core_grep(
            valves, sandbox_id, client,
            pattern=pattern, files=files, max_lines=max_lines,
        )

    return [Tool(f) for f in (bash, read, write, edit, glob, grep)]


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
        except (httpx.HTTPError, httpx.TimeoutException):
            pass
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
        # 2. Get or create a persistent volume for this user
        volume_name = f"{label_key}/{email}"
        volume_id = await _ensure_volume(valves, volume_name, client)

        # 3. Create new sandbox with volume mounted
        await _emit(emitter, "Preparing sandbox...")
        resp = await client.post(
            _api(valves, "/sandbox"),
            headers=_headers(valves),
            json={
                "language": valves.sandbox_language,
                "name": f"{label_key}/{email}",
                "labels": {label_key: email},
                "autoStopInterval": valves.auto_stop_minutes,
                "autoArchiveInterval": valves.auto_archive_minutes,
                "autoDeleteInterval": -1,
                "volumes": [
                    {
                        "volumeId": volume_id,
                        "mountPath": VOLUME_MOUNT_PATH,
                    }
                ],
            },
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
        pass

    def __init__(self):
        self.valves = self.Valves()

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

            delegate(task, context_files, max_steps) spawns an autonomous
            sub-agent that runs the same model against the same sandbox.
            The sub-agent has access to: bash, read, write, edit, glob,
            grep. It does NOT have: lathe, onboard, expose, destroy, or
            delegate (no recursion).

            The sub-agent makes up to max_steps inference calls (default 10,
            max 30), executing tools as needed. When it decides it's done (or
            hits the step limit), its final text response is returned to you
            as the delegate() tool result.

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

            ## Cost model

            Each step is a full inference call billed to the same provider.
            A 10-step delegation costs ~10x a single tool call in tokens.
            The usage summary in the tool result shows exact token counts.
            Use max_steps to cap cost for bounded tasks.
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
            - /home/daytona/volume is S3/FUSE-backed persistent storage that
              survives sandbox destruction.
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
            Call onboard() at the start of a conversation to load AGENTS.md and
            discover available skills. Searches both the project directory and
            ~/.agents/ for global agent instructions and skills.

            **Delegating multi-step work:**
            Use delegate(task="...") to hand off autonomous multi-step tasks
            to a sub-agent. The sub-agent has bash/read/write/edit/glob/grep
            and runs against the same sandbox. Good for: exploration, refactoring,
            debugging, test fixing, research. Use context_files= to inject
            AGENTS.md, skills, or other reference files into the sub-agent's
            prompt without re-reading them. The sub-agent cannot interact with
            the user or expose URLs — it just works and returns a summary.
            See lathe(manpage="delegate") for details.

            **Network requests:**
            Use bash("curl ...") or bash("wget ...") for HTTP requests. The
            sandbox can reach a broad allowlist of hosts directly (package
            registries, git hosts, CDNs, AI APIs, etc.), and bash gives you
            streaming, piping, and natural access to env-var credentials.
            If a request fails due to egress filtering, see
            lathe(manpage="egress") for workarounds.

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
            - bash() output is truncated to the last 2000 lines / 50 KB. If
              truncated, the full output is available in the log file at
              /tmp/cmd/<id>/log — use read() to inspect specific sections.
            - edit() requires an exact string match (including whitespace). If
              the match is ambiguous, provide more surrounding context or use
              replace_all=true.
            - expose() URLs expire after ~1 hour (call expose again for a fresh URL). The sandbox itself stops on
              idle (~15 min default), killing servers.
            - destroy() is irreversible. The volume is preserved.
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
        "delegate": "Sub-agent delegation: when to use, task writing, context_files, cost model.",
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
            # Extract version from the module docstring (single source of truth).
            import re as _re
            mod_doc = globals().get("__doc__", "") or ""
            match = _re.search(r"^version:\s*(.+)$", mod_doc, _re.MULTILINE)
            ver = match.group(1).strip() if match else "unknown"
            await _emit(__event_emitter__, f"Lathe v{ver}", done=True)
            return f"Lathe toolkit version: {ver}"

        if manpage in self._MANPAGES:
            content = self._MANPAGES[manpage]
            if "{tool_catalog}" in content:
                content = content.format(tool_catalog=tool_catalog)
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

    async def destroy(
        self,
        confirm: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Permanently destroy the sandbox VM. Irreversible. Set confirm=true to proceed.
        Persistent volume data is preserved and will reappear in the next sandbox.
        :param confirm: Must be true to confirm destruction.
        """
        if not confirm:
            return (
                "Destroy aborted: confirm was not set to true. "
                "Set confirm=true to permanently destroy the sandbox and all its contents."
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
                return (
                    f"Destroyed {len(deleted)} sandbox(es) ({ids})."
                    f" Your persistent files in {VOLUME_MOUNT_PATH} are intact"
                    f" and will reappear in your next sandbox."
                    f" A fresh sandbox will be created on the next tool call."
                )

        except Exception as exc:
            await _emit(__event_emitter__, "Destroy failed", done=True)
            return f"Error: {exc}"

    async def onboard(
        self,
        path: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Load project context (AGENTS.md + skill catalog) at the start of a conversation.
        Searches both the project directory and ~/.agents/ for global context.
        Use read() on a skill's SKILL.md path to load its full instructions later.
        :param path: Absolute path to the project root (e.g. /home/daytona/workspace/myproject).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, "Loading project context...")

            p = path.rstrip("/")
            script = _build_onboard_script(p)

            # Write script to temp file and execute it
            script_path = f"/tmp/_onboard_{uuid.uuid4()}.py"
            content_bytes = script.encode("utf-8")
            await client.post(
                _toolbox(self.valves, sandbox_id, "/files/upload"),
                params={"path": script_path},
                headers={"Authorization": f"Bearer {self.valves.daytona_api_key}"},
                files={"file": ("file", io.BytesIO(content_bytes), "application/octet-stream")},
                timeout=60.0,
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

            if "ERROR_NO_CONTEXT" in result:
                await _emit(__event_emitter__, "No project context found", done=True)
                return (
                    f"Error: No agent context found. Searched:\n"
                    f"  - {path}/AGENTS.md\n"
                    f"  - {path}/.agents/skills/\n"
                    f"  - ~/.agents/AGENTS.md\n"
                    f"  - ~/.agents/skills/\n"
                    f"At least one of these must exist to use onboard."
                )

            if exit_code != 0:
                await _emit(__event_emitter__, "Error loading context", done=True)
                return f"Error: onboard script failed (exit {exit_code}): {result[:500]}"

            await _emit(__event_emitter__, "Project context loaded", done=True)
            return _prepend_warning(result if result else "(empty project context)", _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_bash)
    async def bash(
        self,
        command: str,
        workdir: str = "/home/daytona/workspace",
        foreground_seconds: int = 0,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            await _emit(__event_emitter__, "Running command...")

            # Collect user-supplied env vars from UserValves (never logged).
            # Raises ValueError (caught below) if env_vars is malformed.
            user_valves = __user__.get("valves")
            user_pairs: list[tuple[str, str]] = []
            if user_valves:
                raw_env = getattr(user_valves, "env_vars", "") or ""
                user_pairs = _parse_env_vars(raw_env)

            # Per-call override wins; 0 (default) falls back to Valve.
            fg_seconds = (
                foreground_seconds if foreground_seconds > 0
                else self.valves.foreground_timeout_seconds
            )

            result = await _core_bash(
                self.valves, sandbox_id, client,
                command=command,
                workdir=workdir,
                user_pairs=user_pairs,
                foreground_seconds=fg_seconds,
                emit=__event_emitter__,
            )

            await _emit(__event_emitter__, "Command complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_read)
    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int = 2000,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _emit(__event_emitter__, f"Reading {path}...")
            result = await _core_read(
                self.valves, sandbox_id, client,
                path=path, offset=offset, limit=limit,
            )
            await _emit(__event_emitter__, "Read complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_glob)
    async def glob(
        self,
        pattern: str,
        max_lines: int = _GLOB_MAX_LINES,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _emit(__event_emitter__, f"Searching for {pattern}...")
            result = await _core_glob(
                self.valves, sandbox_id, client,
                pattern=pattern, max_lines=max_lines,
            )
            await _emit(__event_emitter__, "Search complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_grep)
    async def grep(
        self,
        pattern: str,
        files: str = "**/*",
        max_lines: int = _GREP_MAX_LINES,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _emit(__event_emitter__, f"Searching for {pattern!r}...")
            result = await _core_grep(
                self.valves, sandbox_id, client,
                pattern=pattern, files=files, max_lines=max_lines,
            )
            await _emit(__event_emitter__, "Search complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_write)
    async def write(
        self,
        path: str,
        content: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _emit(__event_emitter__, f"Writing {path}...")
            result = await _core_write(
                self.valves, sandbox_id, client,
                path=path, content=content,
            )
            await _emit(__event_emitter__, "Write complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    @_doc_from_core(_core_edit)
    async def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)
            await _emit(__event_emitter__, f"Editing {path}...")
            result = await _core_edit(
                self.valves, sandbox_id, client,
                path=path, old_string=old_string, new_string=new_string,
                replace_all=replace_all,
            )
            await _emit(__event_emitter__, "Edit complete", done=True)
            return _prepend_warning(result, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def delegate(
        self,
        task: str,
        context_files: list[str] = [],
        max_steps: int = 10,
        __user__: dict = {},
        __model__: dict = {},
        __request__=None,
        __event_emitter__=None,
    ) -> str:
        """
        Delegate a multi-step task to an autonomous sub-agent with sandbox access.
        The sub-agent runs the same model, has bash/read/write/edit/glob/grep tools,
        and returns a summary when done. Use for exploration, refactoring, debugging,
        or any multi-step work that doesn't need user interaction.
        :param task: What the sub-agent should accomplish. Be specific — it cannot ask clarifying questions. Include any context (error messages, prior findings, instructions) directly in the task description.
        :param context_files: Absolute sandbox file paths to inject into the sub-agent's prompt (e.g. AGENTS.md, SKILL.md, config files). Fetched at delegation time — the sub-agent sees their contents without spending steps reading them.
        :param max_steps: Maximum inference calls the sub-agent may make (default: 10, max: 30).
        """
        async def _run(client):
            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

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

            # ── Build sub-agent tools ────────────────────────────────
            tools = _build_delegate_tools(self.valves, sandbox_id, client, user_pairs)

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
                system_prompt=_DELEGATE_SYSTEM_PROMPT,
                tools=tools,
                output_type=str,
            )

            await _emit(__event_emitter__, "Sub-agent starting...")

            try:
                step_count = 0

                async with agent.iter(
                    user_message,
                    usage_limits=UsageLimits(request_limit=clamped_steps),
                ) as agent_run:
                    async for node in agent_run:
                        if Agent.is_model_request_node(node):
                            step_count += 1
                            await _emit(
                                __event_emitter__,
                                f"Sub-agent thinking... ({step_count}/{clamped_steps})"
                            )
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
                                await _emit(
                                    __event_emitter__,
                                    f"Sub-agent → {', '.join(calls)}"
                                )

                    usage = agent_run.usage()
                    result_output = agent_run.result.output if agent_run.result else "(no output)"

            except Exception as e:
                error_type = type(e).__name__
                await _emit(__event_emitter__, f"Delegation failed: {error_type}", done=True)
                return _prepend_warning(
                    f"Delegation failed after {step_count} step(s): {error_type}: {e}",
                    _sb_warning,
                )
            finally:
                await inner_client.aclose()

            # ── Format result with usage summary ─────────────────────
            parts = [f"{step_count} step(s)", f"{usage.tool_calls} tool call(s)"]
            if usage.input_tokens:
                parts.append(f"{usage.input_tokens:,} prompt + {usage.output_tokens:,} completion tokens")
            usage_note = f"\n\n[Delegate completed: {', '.join(parts)}]"

            await _emit(
                __event_emitter__,
                f"Delegation complete ({step_count} steps, {usage.tool_calls} tool calls)",
                done=True,
            )

            return _prepend_warning(result_output + usage_note, _sb_warning)

        return await _tool_context(__event_emitter__, _run)

    async def expose(
        self,
        target: str,
        __user__: dict = {},
        __event_emitter__=None,
    ) -> str:
        """
        Expose a sandbox service to the user. Pass "dufs" for a one-step file
        browser, "code-server" for a one-step IDE, "http:<port>" for a web
        server you already started, or "ssh" for interactive shell access.
        :param target: What to expose — "dufs" for file upload/download, "code-server" for a browser IDE, "ssh" for a shell, or "http:<port>" (e.g. "http:5000", port range 3000–9999) for an HTTP service you started manually.
        """
        async def _run(client):
            # Parse target: "ssh", "dufs", "code-server", or "http:<port>"
            target_stripped = target.strip().lower()
            is_ssh = target_stripped == "ssh"
            is_dufs = target_stripped == "dufs"
            is_code_server = target_stripped == "code-server"
            port = 0

            if not is_ssh and not is_dufs and not is_code_server:
                if not target_stripped.startswith("http:"):
                    return (
                        f"Error: target must be \"ssh\", \"dufs\", \"code-server\", or \"http:<port>\" "
                        f"(e.g. \"http:5000\"). Got: \"{target}\""
                    )
                port_str = target_stripped[len("http:"):]
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

            email = _get_email(__user__)
            sandbox_id, _sb_warning = await _ensure_sandbox(self.valves, email, client, __event_emitter__)

            if is_ssh:
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
                return _prepend_warning(
                    f"SSH command (valid 60 min):\n\n"
                    f"```\n{ssh_command}\n```\n\n"
                    f"The user can paste this into their terminal, VS Code Remote SSH, "
                    f"or JetBrains Gateway.\n\n"
                    f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity. "
                    f"Active SSH sessions keep the sandbox alive.",
                    _sb_warning,
                )

            # ── dufs fast path: install + start + sign URL ──────────
            if is_dufs:
                await _emit(__event_emitter__, "Preparing dufs file browser...")

                # Single script: install if binary missing, start if port free.
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/process/execute"),
                    headers=_headers(self.valves),
                    json={"command": _DUFS_ENSURE_SCRIPT, "timeout": 30000},
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result", "")
                if data.get("exitCode", -1) != 0 or "READY" not in result:
                    await _emit(__event_emitter__, "dufs setup failed", done=True)
                    return (
                        f"Error: dufs setup failed (exit {data.get('exitCode')}).\n"
                        f"{result[:500]}\n\n"
                        f"This usually means the sandbox cannot reach GitHub (egress filtering). "
                        f"See lathe(manpage=\"egress\") for workarounds, or install dufs manually "
                        f"and use expose(target=\"http:{_DUFS_PORT}\")."
                    )

                pid = _extract_pid(result)

                # Sign the URL
                await _emit(__event_emitter__, "Generating URL...")
                resp = await client.get(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ports/{_DUFS_PORT}/signed-preview-url"),
                    params={"expiresInSeconds": 3600},
                    headers=_headers(self.valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                url = resp.json().get("url", "")
                if not url:
                    return "Error: Daytona returned an empty URL."

                await _emit(__event_emitter__, "File browser ready", done=True)
                return _prepend_warning(
                    f"File browser URL (valid ~1 hour): {url}\n\n"
                    f"Give this URL to the user. In their browser they can:\n"
                    f"- **Upload**: drag and drop files onto the page\n"
                    f"- **Download**: click any file\n"
                    f"- **Browse**: navigate folders\n\n"
                    f"dufs is serving {_DUFS_ROOT} on port {_DUFS_PORT} (PID {pid}).",
                    _sb_warning,
                )

            # ── code-server fast path: install + start + sign URL ───
            if is_code_server:
                await _emit(__event_emitter__, "Preparing code-server IDE...")

                # Single script: install if binary missing, start if port free.
                resp = await client.post(
                    _toolbox(self.valves, sandbox_id, "/process/execute"),
                    headers=_headers(self.valves),
                    json={"command": _CS_ENSURE_SCRIPT, "timeout": 60000},
                    timeout=120.0,
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result", "")
                if data.get("exitCode", -1) != 0 or "READY" not in result:
                    await _emit(__event_emitter__, "code-server setup failed", done=True)
                    return (
                        f"Error: code-server setup failed (exit {data.get('exitCode')}).\n"
                        f"{result[:500]}\n\n"
                        f"This usually means the sandbox cannot reach the install script host "
                        f"(egress filtering). See lathe(manpage=\"egress\") for workarounds, or "
                        f"install code-server manually and use expose(target=\"http:{_CS_PORT}\")."
                    )

                pid = _extract_pid(result)

                # Sign the URL
                await _emit(__event_emitter__, "Generating URL...")
                resp = await client.get(
                    _api(self.valves, f"/sandbox/{sandbox_id}/ports/{_CS_PORT}/signed-preview-url"),
                    params={"expiresInSeconds": 3600},
                    headers=_headers(self.valves),
                    timeout=30.0,
                )
                resp.raise_for_status()
                url = resp.json().get("url", "")
                if not url:
                    return "Error: Daytona returned an empty URL."

                await _emit(__event_emitter__, "IDE ready", done=True)
                return _prepend_warning(
                    f"IDE URL (valid ~1 hour): {url}\n\n"
                    f"Give this URL to the user. They get VS Code in the browser with:\n"
                    f"- Full terminal access\n"
                    f"- File editing and navigation\n"
                    f"- Extension support\n\n"
                    f"code-server is serving {_CS_ROOT} on port {_CS_PORT} (PID {pid}).",
                    _sb_warning,
                )

            # Port exposure path
            await _emit(__event_emitter__, f"Generating URL for port {port}...")

            resp = await client.get(
                _api(self.valves, f"/sandbox/{sandbox_id}/ports/{port}/signed-preview-url"),
                params={"expiresInSeconds": 3600},
                headers=_headers(self.valves),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()

            url = data.get("url", "")
            if not url:
                return "Error: Daytona returned an empty URL."

            await _emit(__event_emitter__, f"URL ready (port {port})", done=True)
            return _prepend_warning(
                f"Public URL (valid ~1 hour): {url}\n\n"
                f"The user can open this in a new browser tab. "
                f"They may see a Daytona security warning on first visit — they can click through it.\n\n"
                f"Note: the sandbox auto-stops after ~{self.valves.auto_stop_minutes} min of inactivity regardless of "
                f"running background processes, killing the server. If the user reports "
                f"the URL stopped working, restart the server and call expose() again.",
                _sb_warning,
            )

        return await _tool_context(__event_emitter__, _run)

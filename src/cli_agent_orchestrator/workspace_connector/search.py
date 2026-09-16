"""Workspace content search — port of ``src/workspace/search.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: the
upstream prefers ripgrep and falls back to a Node walker; this port implements
the pure-Python walker only (the ripgrep engine probe is dropped — the loopback
connector runs on controlled hosts and the walker matches the fallback's
contract exactly, including the 2 MiB per-file cap and 500-char match lines).
CAO additions (D5): the manifest scope (r4 N4 — a hit outside the manifest is
never revealed: no path, text or count) and the content digest.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Iterator, cast

from cli_agent_orchestrator.workspace_connector.workspace_manager import (
    Workspace,
    content_digest,
)

MAX_FILE_BYTES = 2 * 1024 * 1024


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Upstream parity for the fallback engine's glob filter."""
    # Reuse fnmatch per-segment semantics but keep the upstream's
    # "(^|/)glob$" anchoring and case-insensitive match.
    translated = fnmatch.translate(glob)
    if translated.startswith("(?s:"):
        translated = translated[4:]
        if translated.endswith(")\\Z"):
            translated = translated[:-3] + "$"
    return re.compile(r"(^|/)" + translated, re.IGNORECASE)


def search_workspace(
    ws: Workspace,
    opts: dict[str, object],
) -> dict[str, object]:
    """Search file contents; hidden/noise and out-of-manifest files are skipped."""
    query = str(opts.get("query") or "")
    if len(query) < 2:
        return {"matches": [], "matchCount": 0, "truncated": False, "engine": "node"}
    limit = min(200, max(1, int(cast(int | str, opts.get("limit") or 50))))
    path_opt = opts.get("path")
    search_abs, _ = ws.resolve(str(path_opt) if path_opt else ".")

    glob_opt = opts.get("glob")
    glob_regex = _glob_to_regex(str(glob_opt)) if glob_opt else None
    regex = bool(opts.get("regex"))
    matcher = re.compile(query, re.IGNORECASE) if regex else None
    needle = query.lower()

    matches: list[dict[str, object]] = []
    truncated = False

    start_rel = ""
    try:
        start_rel = str(search_abs.relative_to(ws.root))
    except ValueError:
        start_rel = ""

    def walk(dir_abs: Path, dir_rel: str) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            entries = sorted(os_scandir(dir_abs), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if truncated:
                return
            child_rel = f"{dir_rel}/{entry.name}" if dir_rel else entry.name
            if ws.ignore_rules.is_hidden(child_rel) or ws.ignore_rules.is_hidden(child_rel + "/"):
                continue
            # Manifest scoping (r4 N4): never reveal anything outside it.
            if ws.manifest is not None and not ws._in_manifest(child_rel):
                continue
            child_abs = dir_abs / entry.name
            if entry.is_dir(follow_symlinks=False):
                walk(child_abs, child_rel)
            elif entry.is_file(follow_symlinks=False):
                if glob_regex and not glob_regex.search(child_rel):
                    continue
                try:
                    if child_abs.stat().st_size > MAX_FILE_BYTES:
                        continue
                    content = child_abs.read_bytes()
                except OSError:
                    continue
                if b"\x00" in content:
                    continue
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                for i, line in enumerate(text.split("\n"), start=1):
                    hit = bool(matcher and matcher.search(line)) or (
                        not matcher and needle in line.lower()
                    )
                    if hit:
                        matches.append(
                            {
                                "path": child_rel,
                                "line": i,
                                "text": line.rstrip()[:500],
                            }
                        )
                        if len(matches) >= limit:
                            truncated = True
                            return

    walk(search_abs, start_rel)
    result: dict[str, object] = {
        "matches": matches,
        "matchCount": len(matches),
        "truncated": truncated,
        "engine": "node",
    }
    result["contentDigest"] = content_digest(repr(matches))
    return result


def os_scandir(p: Path) -> Iterator[os.DirEntry[str]]:
    return os.scandir(p)

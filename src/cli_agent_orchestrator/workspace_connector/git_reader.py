"""Read-only git status/diff — port of ``src/workspace/git.ts``.

Upstream: XiaoDuoYa/codex-with-chatgpt at commit
8fdd97c188c7678d0d9c43b3769b426940de568a (MIT).  Behavioural port: porcelain
v2 status parsing with sensitive-entry withholding, batched pathspec diffs with
byte-offset pagination and fail-closed batch errors.  CAO additions (D5, r4
N4): ``git_status`` runs with the manifest as its pathspec against the frozen
worktree (clean by construction; a non-empty status is itself a build-stop
signal), and ``git_diff`` diffs only manifest paths between the base commit and
the reviewed commit named in the manifest.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import cast

from cli_agent_orchestrator.workspace_connector.ignore_rules import IgnoreRules
from cli_agent_orchestrator.workspace_connector.workspace_manager import WorkspaceError

MAX_AGGREGATE_DIFF_BYTES = 64 * 1024 * 1024


def run_git(root: str | Path, args: list[str]) -> tuple[bool, str, str, int | None]:
    """Run one git command in the workspace root (upstream parity)."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "GIT_EXTERNAL_DIFF": "", "GIT_PAGER": "cat"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "", "", None
    return proc.returncode == 0, proc.stdout or "", proc.stderr or "", proc.returncode


def git_info(root: str | Path) -> dict[str, object]:
    """Identity summary used by ``workspace_info`` (upstream parity)."""
    ok, out, _, _ = run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if not ok or out.strip() != "true":
        return {"isRepo": False, "branch": None, "commit": None, "dirty": False}
    ok_b, out_b, _, _ = run_git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    ok_c, out_c, _, _ = run_git(root, ["rev-parse", "--short", "HEAD"])
    ok_s, out_s, _, _ = run_git(root, ["status", "--porcelain", "--", "."])
    return {
        "isRepo": True,
        "branch": out_b.strip() if ok_b else None,
        "commit": out_c.strip() if ok_c else None,
        "dirty": bool(out_s.strip()) if ok_s else False,
    }


def _withhold(paths: list[str], rules: IgnoreRules) -> bool:
    return any(rules.is_sensitive(p) for p in paths)


def git_status(
    root: str | Path, ignore_rules: IgnoreRules, pathspec: list[str] | None = None
) -> dict[str, object]:
    """Structured porcelain-v2 status; sensitive entries withheld (upstream parity).

    CAO addition (r4 N4): ``pathspec`` confines the query to the attempt's
    allowlisted manifest; the frozen worktree is clean by construction, so a
    non-empty result is itself a build-stop signal upstream of the connector.
    """
    empty: dict[str, object] = {
        "isRepo": False,
        "branch": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "staged": [],
        "unstaged": [],
        "untracked": [],
        "conflicted": [],
        "hidden": {"changes": 0, "conflicts": 0},
    }
    args = ["status", "--porcelain=v2", "--branch", "--"]
    if pathspec:
        args.extend(f":(literal){path}" for path in pathspec)
    else:
        args.append(".")
    ok, out, _, _ = run_git(root, args)
    if not ok:
        return dict(empty)
    result: dict[str, object] = dict(empty)
    result["isRepo"] = True
    result["hidden"] = {"changes": 0, "conflicts": 0}
    staged: list[dict[str, str]] = []
    unstaged: list[dict[str, str]] = []
    untracked: list[str] = []
    conflicted: list[str] = []
    hidden_changes = 0
    hidden_conflicts = 0

    for line in out.split("\n"):
        if line.startswith("# branch.head "):
            result["branch"] = line[len("# branch.head ") :].strip()
        elif line.startswith("# branch.upstream "):
            result["upstream"] = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            import re

            m = re.search(r"\+(\d+) -(\d+)", line)
            if m:
                result["ahead"] = int(m.group(1))
                result["behind"] = int(m.group(2))
        elif line.startswith("1 ") or line.startswith("2 "):
            head, _, tail = line.partition("\t")
            parts = head.split(" ")
            xy = parts[1] if len(parts) > 1 else ""
            is_rename = line.startswith("2 ")
            if is_rename:
                destination = " ".join(parts[9:])
                origin = tail if tail else ""
            else:
                destination = " ".join(parts[8:])
                origin = ""
            raw_paths = [destination, origin] if origin else [destination]
            file_path = f"{destination} -> {origin}" if origin else destination
            if _withhold([p for p in raw_paths if p], ignore_rules):
                hidden_changes += (1 if xy[:1] != "." else 0) + (1 if xy[1:2] != "." else 0)
                continue
            if xy[:1] != ".":
                staged.append({"path": file_path, "change": xy[:1]})
            if xy[1:2] != ".":
                unstaged.append({"path": file_path, "change": xy[1:2]})
        elif line.startswith("? "):
            file_path = line[2:]
            if _withhold([file_path], ignore_rules):
                hidden_changes += 1
            else:
                untracked.append(file_path)
        elif line.startswith("u "):
            file_path = " ".join(line.split(" ")[10:])
            if _withhold([file_path], ignore_rules):
                hidden_conflicts += 1
            else:
                conflicted.append(file_path)
    result["staged"] = staged
    result["unstaged"] = unstaged
    result["untracked"] = untracked
    result["conflicted"] = conflicted
    result["hidden"] = {"changes": hidden_changes, "conflicts": hidden_conflicts}
    return result


def _chunk_safe_paths(
    paths: list[str], max_count: int = 50, max_bytes: int = 32 * 1024
) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for p in paths:
        p_bytes = len(p.encode("utf-8")) + 12  # overhead for ":(literal)"
        if current and (len(current) >= max_count or current_bytes + p_bytes > max_bytes):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(p)
        current_bytes += p_bytes
    if current:
        batches.append(current)
    return batches


def _is_path_in_scope(file_path: str, scope: str | None) -> bool:
    if not scope or scope == ".":
        return True
    return file_path == scope or file_path.startswith(scope + "/")


def git_diff(
    root: str | Path,
    ignore_rules: IgnoreRules,
    opts: dict[str, object] | None = None,
    rel_path: str | None = None,
    pathspec_paths: list[str] | None = None,
    base_commit: str | None = None,
    reviewed_commit: str | None = None,
) -> dict[str, object]:
    """Byte-offset paginated diff, fail-closed on batch/aggregate errors (upstream parity).

    CAO addition (r4 N4): ``pathspec_paths`` is the attempt's allowlisted
    manifest; the diff covers only those paths between the base commit and the
    reviewed commit named in the manifest.
    """
    options = dict(opts or {})
    mode = str(options.get("mode") or "unstaged")
    offset = max(0, int(cast(int | str, options.get("offset") or 0)))
    max_bytes = min(
        256 * 1024, max(1024, int(cast(int | str, options.get("max_bytes") or 64 * 1024)))
    )
    if (base_commit is None) != (reviewed_commit is None):
        raise ValueError("base_commit and reviewed_commit must be provided together")
    mode_args = ["--cached"] if mode == "staged" else (["HEAD"] if mode == "head" else [])
    if base_commit is not None and reviewed_commit is not None:
        mode_args = [base_commit, reviewed_commit]

    not_repo: dict[str, object] = {
        "isRepo": False,
        "mode": mode,
        "totalBytes": 0,
        "offset": 0,
        "returnedBytes": 0,
        "hasMore": False,
        "nextOffset": None,
        "diff": "",
    }

    listed_pathspecs = (
        [f":(literal){path}" for path in pathspec_paths] if pathspec_paths is not None else ["."]
    )
    list_args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-status",
        "-z",
        "--find-renames=1%",
        *mode_args,
        "--",
        *listed_pathspecs,
    ]
    ok, out, _, _ = run_git(root, list_args)
    if not ok:
        return dict(not_repo)

    tokens = out.split("\x00")
    safe_paths: list[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if not status:
            break
        if status.startswith("R") or status.startswith("C"):
            old_path = tokens[i] if i < len(tokens) else ""
            i += 1
            new_path = tokens[i] if i < len(tokens) else ""
            i += 1
            if old_path and new_path:
                if pathspec_paths is not None:
                    in_scope_paths = old_path in pathspec_paths or new_path in pathspec_paths
                    if not in_scope_paths:
                        continue
                is_safe = not ignore_rules.is_sensitive(old_path) and not ignore_rules.is_sensitive(
                    new_path
                )
                is_relevant = _is_path_in_scope(old_path, rel_path) or _is_path_in_scope(
                    new_path, rel_path
                )
                if is_safe and is_relevant:
                    safe_paths.extend([old_path, new_path])
        else:
            file_path = tokens[i] if i < len(tokens) else ""
            i += 1
            if file_path:
                if pathspec_paths is not None and file_path not in pathspec_paths:
                    continue
                is_safe = not ignore_rules.is_sensitive(file_path)
                is_relevant = _is_path_in_scope(file_path, rel_path)
                if is_safe and is_relevant:
                    safe_paths.append(file_path)

    if not safe_paths:
        return {
            "isRepo": True,
            "mode": mode,
            "totalBytes": 0,
            "offset": 0,
            "returnedBytes": 0,
            "hasMore": False,
            "nextOffset": None,
            "diff": "",
        }

    combined = b""
    total_aggregate = 0
    for batch in _chunk_safe_paths(safe_paths):
        pathspecs = [f":(literal){p}" for p in batch]
        diff_args = [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--find-renames=1%",
            *mode_args,
            "--",
            *pathspecs,
        ]
        ok_d, out_d, _, _ = run_git(root, diff_args)
        if not ok_d:
            # Fail closed on any batch error: never return partial silent success
            return dict(not_repo)
        if out_d:
            chunk = out_d.encode("utf-8")
            if total_aggregate + len(chunk) > MAX_AGGREGATE_DIFF_BYTES:
                return dict(not_repo)
            combined += chunk
            total_aggregate += len(chunk)

    full = combined
    slice_bytes = full[offset : offset + max_bytes]
    text = slice_bytes.decode("utf-8", errors="replace")
    slice_len = len(slice_bytes)
    if offset + slice_len < len(full):
        last_newline = text.rfind("\n")
        if last_newline > 0:
            text = text[: last_newline + 1]
            slice_len = len(text.encode("utf-8"))
    has_more = offset + slice_len < len(full)
    return {
        "isRepo": True,
        "mode": mode,
        "totalBytes": len(full),
        "offset": offset,
        "returnedBytes": slice_len,
        "hasMore": has_more,
        "nextOffset": offset + slice_len if has_more else None,
        "diff": text,
    }

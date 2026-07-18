#!/usr/bin/env python3
"""Compile resolved Python files and collect changed tests before handoff."""

from __future__ import annotations

import argparse
import py_compile
import subprocess
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_paths(*args: str) -> list[Path]:
    """Return repository-relative Python paths emitted by a git query."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git query failed"
        raise RuntimeError(detail)
    return [Path(line) for line in result.stdout.splitlines() if line]


def _all_changed_python() -> list[Path]:
    """Return the sorted union of tracked-changed and untracked Python paths."""
    tracked = _git_paths(
        "diff",
        "--name-only",
        "--diff-filter=ACMR",
        "HEAD",
        "--",
        "*.py",
    )
    untracked = _git_paths(
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "*.py",
    )
    return sorted(set(tracked) | set(untracked), key=lambda path: path.as_posix())


def _display_path(path: Path) -> str:
    """Render a stable repository-relative path when possible."""
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def _is_test_path(path: Path) -> bool:
    """Return whether a path is under the repository's test tree."""
    try:
        relative = path.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == "test"


def _verify_path(path: Path) -> int:
    """Compile one file and collect it when it is a test module."""
    display = _display_path(path)
    try:
        py_compile.compile(str(path), doraise=True)
    except (OSError, py_compile.PyCompileError) as exc:
        print(f"ERROR: py_compile failed for {display}", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1

    if not _is_test_path(path):
        return 0

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", display],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: pytest collection failed for {display}", file=sys.stderr)
        output = result.stdout + result.stderr
        if output:
            print(output.rstrip(), file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Changed or resolved .py paths")
    parser.add_argument(
        "--all-changed",
        action="store_true",
        help="Verify tracked-changed and untracked non-ignored Python files",
    )
    args = parser.parse_args(argv)
    if args.all_changed and args.paths:
        parser.error("--all-changed cannot be combined with explicit paths")

    try:
        paths = _all_changed_python() if args.all_changed else args.paths
    except RuntimeError as exc:
        print(f"ERROR: cannot enumerate changed Python files: {exc}", file=sys.stderr)
        return 1

    normalized = sorted(
        {(REPO_ROOT / path if not path.is_absolute() else path).resolve() for path in paths},
        key=lambda path: _display_path(path),
    )
    for path in normalized:
        if path.suffix != ".py":
            print(f"ERROR: not a Python file: {_display_path(path)}", file=sys.stderr)
            return 1
        if _verify_path(path) != 0:
            return 1

    print(f"Verified {len(normalized)} Python file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

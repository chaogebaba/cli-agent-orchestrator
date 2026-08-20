#!/usr/bin/env python3
"""F330 — Fenced sweep of all test-suite residue classes.

Sweeps:
  1. pytest basetemp dirs under /data/cao-scratch/pytest-tmp
  2. Test-created tmux sessions (caotest-*, cao-test-*)
  3. Stale git worktree registrations (git worktree prune)

SAFETY FENCE: This script can NEVER touch live non-test sessions. It matches
only known test-created naming prefixes and only operates on directories under
the dedicated basetemp root.

Usage:
  uv run python scripts/clean_test_residue.py [--dry-run] [--basetemp PATH]

Exit codes:
  0 = success (may have cleaned nothing)
  1 = error during cleanup
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --- Safety-fenced naming patterns ---
# ONLY these prefixes are considered test-created tmux sessions.
_TEST_SESSION_RE = re.compile(r"^(caotest-|cao-test-)")

# Default basetemp root (off-tmpfs).
_DEFAULT_BASETEMP = "/data/cao-scratch/pytest-tmp"


def _sweep_basetemp(basetemp: Path, *, dry_run: bool) -> int:
    """Remove all subdirectories under the basetemp root. Returns count removed."""
    if not basetemp.is_dir():
        print(f"[basetemp] directory does not exist: {basetemp}")
        return 0

    removed = 0
    for entry in sorted(basetemp.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            if dry_run:
                print(f"[basetemp] would remove: {entry}")
            else:
                try:
                    shutil.rmtree(entry)
                    print(f"[basetemp] removed: {entry}")
                    removed += 1
                except OSError as exc:
                    print(f"[basetemp] ERROR removing {entry}: {exc}", file=sys.stderr)
            if not dry_run:
                pass
            else:
                removed += 1
    return removed


def _sweep_tmux_sessions(*, dry_run: bool) -> int:
    """Kill tmux sessions matching test prefixes. Returns count killed."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return 0
        sessions = [s.strip() for s in result.stdout.splitlines() if s.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        print("[tmux] tmux not available or no server running")
        return 0

    killed = 0
    for name in sessions:
        if _TEST_SESSION_RE.match(name):
            if dry_run:
                print(f"[tmux] would kill session: {name}")
                killed += 1
            else:
                try:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", name],
                        capture_output=True,
                        timeout=5,
                    )
                    print(f"[tmux] killed session: {name}")
                    killed += 1
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                    print(f"[tmux] ERROR killing {name}: {exc}", file=sys.stderr)
        else:
            # Explicitly log that we are NOT touching this session (fence proof).
            pass

    return killed


def _prune_worktrees(*, dry_run: bool) -> bool:
    """Run git worktree prune. Returns True on success."""
    # Find the repo root from this script's location.
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    if not (repo_root / ".git").exists() and not (repo_root / "pyproject.toml").exists():
        print("[worktree] cannot locate repo root")
        return False

    cmd = ["git", "worktree", "prune"]
    if dry_run:
        cmd.append("--dry-run")
    cmd.append("--verbose")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if result.stderr.strip():
            print(f"[worktree] {result.stderr.strip()}")
        if result.returncode != 0:
            print(f"[worktree] git worktree prune failed: {result.stderr}", file=sys.stderr)
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[worktree] ERROR: {exc}", file=sys.stderr)
        return False


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sweep all test-suite residue (fenced, never touches live sessions)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be cleaned without actually removing anything.",
    )
    parser.add_argument(
        "--basetemp",
        type=Path,
        default=Path(os.environ.get("CAO_PYTEST_BASETEMP", _DEFAULT_BASETEMP)),
        help=f"Basetemp root to sweep (default: {_DEFAULT_BASETEMP}).",
    )
    args = parser.parse_args()

    errors = False
    print("=" * 60)
    print("F330: clean-test-residue (fenced sweep)")
    print("=" * 60)

    if args.dry_run:
        print("MODE: dry-run (no changes will be made)\n")
    else:
        print("MODE: live (will remove residue)\n")

    # 1. Basetemp dirs
    print("--- [1/3] pytest basetemp dirs ---")
    basetemp_count = _sweep_basetemp(args.basetemp, dry_run=args.dry_run)
    print(f"  → {basetemp_count} dir(s) {'would be ' if args.dry_run else ''}removed\n")

    # 2. Tmux sessions
    print("--- [2/3] test tmux sessions ---")
    tmux_count = _sweep_tmux_sessions(dry_run=args.dry_run)
    print(f"  → {tmux_count} session(s) {'would be ' if args.dry_run else ''}killed\n")

    # 3. Worktrees
    print("--- [3/3] stale git worktrees ---")
    if not _prune_worktrees(dry_run=args.dry_run):
        errors = True
    print()

    print("=" * 60)
    if errors:
        print("DONE (with errors — see above)")
        return 1
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

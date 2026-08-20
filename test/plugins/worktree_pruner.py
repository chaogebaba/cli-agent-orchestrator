"""F330 — Prune stale git worktrees created by the test suite.

Problem: The test suite creates git worktrees (via worktree_service tests and
integration tests) that are left behind when tests fail. 13 stale worktrees
were found on the host from dead lanes.

Solution: On pytest_sessionfinish, run ``git worktree prune`` in the fork repo
to remove entries pointing to deleted directories. This is safe — it only
removes worktree registrations whose directories no longer exist.

Additionally, worktrees created by the test suite use tmp_path-based directories
that get cleaned up by basetemp pruning, so their registrations become stale
and are ripe for pruning.

Registered via ``pytest_plugins`` in ``test/conftest.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _find_repo_root() -> "Path | None":
    """Find the git repository root for the fork."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent.parent,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Run ``git worktree prune`` to clean up stale worktree registrations."""
    repo_root = _find_repo_root()
    if repo_root is None:
        return

    try:
        result = subprocess.run(
            ["git", "worktree", "prune", "--verbose"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=repo_root,
        )
        if result.returncode == 0 and result.stderr.strip():
            sys.stderr.write(f"[worktree-pruner] pruned stale entries:\n{result.stderr.strip()}\n")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        sys.stderr.write(f"[worktree-pruner] git worktree prune failed: {exc}\n")

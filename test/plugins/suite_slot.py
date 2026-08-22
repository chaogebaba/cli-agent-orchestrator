"""Suite-slot lockfile enforcement at the pytest layer (F272, issue #182).

Mechanically prevents two pytest suite runs from executing concurrently
on the same box — the 15G OOM class kills workers when suites overlap.

Design:
- Acquires an exclusive flock on /data/cao-scratch/.suite-slot.lock in
  pytest_configure (controller process only, not xdist workers).
- ALL runs (serial or parallel) acquire the lock — no carve-outs that
  reopen the race.  Targeted single-test runs also lock (cheap when
  uncontended).
- No-ops when CI=true (GitHub Actions runner: no fleet, no contention).
- Writes holder identity (pid + start time) into the lockfile.
- Default = fail fast with a one-line message naming the holder.
- Opt-in: set CAO_SUITE_SLOT_WAIT=1 to block and wait instead of failing.
- Stale-lock safety: flock(2) releases on process death automatically —
  no pid-liveness heuristics on top.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import sys
from pathlib import Path

import pytest

_LOCK_PATH = Path("/data/cao-scratch/.suite-slot.lock")

# Sentinel to track our lock fd across configure/unconfigure
_lock_fd: int | None = None


def _is_xdist_worker(config: pytest.Config) -> bool:
    """Return True if this process is an xdist worker (not the controller)."""
    return hasattr(config, "workerinput")


def _holder_identity() -> str:
    """Build a one-line identity string for the lock holder."""
    pid = os.getpid()
    terminal_id = os.environ.get("CAO_TERMINAL_ID", "-")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"pid={pid} terminal={terminal_id} since={ts}"


def _read_holder_info() -> str:
    """Read the holder identity from the lockfile, or a fallback."""
    try:
        content = _LOCK_PATH.read_text().strip()
        return content if content else "(unknown holder)"
    except Exception:
        return "(unknown holder)"


def pytest_configure(config: pytest.Config) -> None:
    """Acquire the suite slot lock (controller only, non-CI)."""
    global _lock_fd

    # No-op under CI
    if os.environ.get("CI"):
        return

    # Controller-only: xdist workers must not take the lock
    if _is_xdist_worker(config):
        return

    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o666)

    # Attempt non-blocking exclusive lock
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Lock held by another process
        holder_info = _read_holder_info()

        # Opt-in wait mode
        if os.environ.get("CAO_SUITE_SLOT_WAIT", "").strip() == "1":
            sys.stderr.write(f"[suite-slot] waiting for suite slot held by {holder_info}\n")
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            # Default: fail fast
            os.close(fd)
            pytest.exit(
                f"[suite-slot] BLOCKED: suite slot held by {holder_info}",
                returncode=1,
            )

    # Lock acquired — write holder identity
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    identity = _holder_identity()
    os.write(fd, identity.encode())
    sys.stderr.write(f"[suite-slot] lock acquired — {identity}\n")

    _lock_fd = fd


def pytest_unconfigure(config: pytest.Config) -> None:
    """Release the suite slot lock."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None

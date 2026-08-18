"""Suite-slot enforcement at the pytest layer (WP-SUITE D4, F272).

Moves the fleet-wide suite serialization lock from scripts/run-pytest.sh
(which is trivially bypassed by a bare ``uv run pytest``) into pytest
itself, so every entry path — make targets, bare invocations, future
tooling — holds the lock.

Behaviour:
- Acquires an exclusive flock on /data/cao-scratch/.suite-slot.lock in
  pytest_configure (controller process only, not xdist workers).
- No-ops when CI=true (GitHub Actions runner: no fleet, no contention).
- Writes holder identity into the lockfile on acquisition.
- Default is block-and-wait; CAO_SUITE_SLOT=nowait fails fast.
- Stale locks (holder pid dead) are reclaimed with a loud notice.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import signal
import struct
import sys
import time
from pathlib import Path

import pytest

_LOCK_PATH = Path("/data/cao-scratch/.suite-slot.lock")

# Sentinel to track our lock fd across configure/unconfigure
_lock_fd: int | None = None


def _is_xdist_worker(config: pytest.Config) -> bool:
    """Return True if this process is an xdist worker (not the controller)."""
    return hasattr(config, "workerinput")


def _effective_worker_count(config: pytest.Config) -> int:
    """Return the resolved -n value. 0 means serial (no xdist)."""
    # pytest-xdist stores numprocesses on the config when active
    val = getattr(config, "workerinput", None)
    if val is not None:
        # We are a worker — should not reach here due to the guard, but be safe
        return 0
    # Check the -n option directly
    try:
        numprocesses = config.getoption("numprocesses", default=None)
    except (ValueError, AttributeError):
        return 0
    if numprocesses is None or numprocesses == 0:
        return 0
    return int(numprocesses)


def _holder_identity() -> str:
    """Build a one-line identity string for the lock holder."""
    pid = os.getpid()
    terminal_id = os.environ.get("CAO_TERMINAL_ID", "-")
    tmux_window = os.environ.get("TMUX_PANE", "-")

    try:
        import subprocess

        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        sha = "unknown"

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%MZ")
    return f"pid={pid} terminal={terminal_id} tmux={tmux_window} sha={sha} since={ts}"


def _read_holder_info() -> str:
    """Read the holder identity from the lockfile, or a fallback."""
    try:
        content = _LOCK_PATH.read_text().strip()
        return content if content else "(unknown holder)"
    except Exception:
        return "(unknown holder)"


def _holder_pid_from_file() -> int | None:
    """Extract the holder pid from the lockfile content."""
    try:
        content = _LOCK_PATH.read_text().strip()
        for part in content.split():
            if part.startswith("pid="):
                return int(part[4:])
    except Exception:
        pass
    return None


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _try_reclaim_stale_lock() -> bool:
    """If the current holder is dead, reclaim by truncating and re-trying.

    Returns True if we successfully reclaimed (caller should re-attempt flock).
    """
    holder_pid = _holder_pid_from_file()
    if holder_pid is None:
        return False
    if _pid_alive(holder_pid):
        return False
    # Holder is dead — flock(2) should have auto-released, but the identity
    # file is stale. Announce and let the caller try the flock again.
    holder_info = _read_holder_info()
    sys.stderr.write(
        f"[suite-slot] NOTICE: reclaiming stale lock from dead holder: {holder_info}\n"
    )
    return True


def pytest_configure(config: pytest.Config) -> None:
    """Acquire the suite slot lock (controller only, non-CI)."""
    global _lock_fd

    # AC4.5: no-op under CI
    if os.environ.get("CI"):
        return

    # Controller-only: xdist workers must not take the lock
    if _is_xdist_worker(config):
        return

    # Only lock when parallelism is active (effective workers > 0)
    # Actually per the blueprint: acquire regardless — even serial runs need
    # serialization against parallel runs in other terminals. The slot protects
    # the resource (RAM), not just xdist coordination.
    # Re-reading AC4.1: "when the effective worker count is > 0" — keep that.
    # But a serial `-n 0` run still consumes RAM and can conflict. The blueprint
    # says "when the effective worker count is > 0", so we respect it literally.
    if _effective_worker_count(config) == 0:
        # Serial run — no fleet contention concern per AC4.1
        return

    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    nowait = os.environ.get("CAO_SUITE_SLOT", "").lower() == "nowait"

    fd = os.open(str(_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o666)

    # First attempt: non-blocking
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        # Could not get lock immediately
        _try_reclaim_stale_lock()

        if nowait:
            holder_info = _read_holder_info()
            os.close(fd)
            pytest.exit(
                f"[suite-slot] FAIL: slot busy (CAO_SUITE_SLOT=nowait). "
                f"Holder: {holder_info}",
                returncode=1,
            )

        # Block and wait (AC4.4 default)
        holder_info = _read_holder_info()
        sys.stderr.write(
            f"[suite-slot] waiting for suite slot held by {holder_info}\n"
        )
        fcntl.flock(fd, fcntl.LOCK_EX)

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

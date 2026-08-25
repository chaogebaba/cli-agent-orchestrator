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

Self-destruct watchdog (F437, issue #292; F445 fix, issue #300):
- Per-test timeouts (pytest-timeout) do NOT bound wall clock: a hung
  subprocess wait, a collection hang, or a C-level block escapes them.
  A runaway run once held this slot for 6h46m.
- The controller process that ACQUIRES the lock arms a daemon watchdog
  (threading.Timer) for CAO_SUITE_SLOT_MAX_SECONDS (env, default 3600;
  value <= 0 disables). On fire: print a loud diagnostic to stderr
  (holder identity + elapsed) then hard-kill the pytest DESCENDANT TREE
  only (BFS walk of /proc from os.getpid()) + terminate self via
  os._exit. Ancestors (including a shared agent-TUI process group) are
  NEVER signaled — fixes the 2026-08-24 incident where killpg(getpgrp)
  took down the entire codex reviewer pane. flock releases automatically
  on process exit, freeing the slot.
- Armed ONLY when the lock was actually acquired: the CI path and xdist
  workers (which skip acquisition) never arm — box-run's own -w covers
  those.

Registered via the ``pytest_plugins`` tuple in ``test/conftest.py``.
"""

from __future__ import annotations

import datetime
import fcntl
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

_LOCK_PATH = Path("/data/cao-scratch/.suite-slot.lock")

# Sentinel to track our lock fd across configure/unconfigure
_lock_fd: int | None = None

# Self-destruct watchdog state (controller process only)
_watchdog: threading.Timer | None = None

# Env knob for the wall-clock hard bound; default 1 hour.
_MAX_SECONDS_ENV = "CAO_SUITE_SLOT_MAX_SECONDS"
_DEFAULT_MAX_SECONDS = 3600.0


def _max_seconds() -> float:
    """Resolve the wall-clock hard bound from the environment.

    Returns the configured number of seconds, or ``_DEFAULT_MAX_SECONDS``
    when the env var is unset/blank/unparseable/non-finite. A value <= 0
    disables the watchdog (returned verbatim so the caller can decide not
    to arm).

    Non-finite values (``nan``, ``inf``, ``-inf``, and overflowing literals
    like ``1e309`` which ``float()`` rounds to ``inf``) are rejected: ``nan``
    would make the Timer fire immediately (SIGKILL on startup) and ``inf``
    would crash the Timer thread (OverflowError → no bound at all). They get
    the same warned 3600 fallback as unparseable text.
    """
    raw = os.environ.get(_MAX_SECONDS_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan  # force the warned-fallback path below
    if not math.isfinite(value):
        # Unparseable or non-finite → safe default rather than disabling or
        # firing immediately / crashing the timer thread.
        sys.stderr.write(
            f"[suite-slot] WARNING: {_MAX_SECONDS_ENV}={raw!r} is not a finite "
            f"number; using default {_DEFAULT_MAX_SECONDS:.0f}s\n"
        )
        return _DEFAULT_MAX_SECONDS
    return value


def _get_descendant_pids(root_pid: int) -> list[int]:
    """Return all descendant PIDs of *root_pid* by BFS-walking /proc.

    Only returns processes that are strictly below *root_pid* in the tree —
    never *root_pid* itself, and never any ancestor.
    """
    descendants: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return descendants
    # Build parent→children map from /proc/*/stat
    parent_map: dict[int, list[int]] = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat_data = f.read().decode("utf-8", errors="replace")
            # Format: pid (comm) state ppid ...
            # comm may contain parens; find last ')' then split remaining fields
            close_paren = stat_data.rfind(")")
            fields = stat_data[close_paren + 2:].split()
            ppid = int(fields[1])  # state=fields[0], ppid=fields[1]
            parent_map.setdefault(ppid, []).append(pid)
        except (OSError, IndexError, ValueError):
            continue
    # BFS from root_pid's children
    queue = list(parent_map.get(root_pid, []))
    visited: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        descendants.append(pid)
        queue.extend(parent_map.get(pid, []))
    return descendants


def _watchdog_fire(max_seconds: float, armed_at: float) -> None:
    """Kill the pytest descendant tree and terminate self on watchdog expiry.

    Walks /proc to find all descendants of this process (os.getpid()),
    SIGKILLs them, then terminates self via os._exit. Ancestors — including
    a shared agent-TUI process group — are NEVER signaled.

    flock(2) releases automatically once this process exits, freeing the
    suite slot for the next run.
    """
    elapsed = time.monotonic() - armed_at
    holder_info = _read_holder_info()
    my_pid = os.getpid()
    banner = (
        "\n"
        "==================== SUITE-SLOT WATCHDOG ====================\n"
        f"[suite-slot] SELF-DESTRUCT: wall-clock bound of {max_seconds:.0f}s exceeded\n"
        f"[suite-slot]   elapsed:   {elapsed:.1f}s\n"
        f"[suite-slot]   holder:    {holder_info}\n"
        f"[suite-slot]   controller pid={my_pid} pgid={os.getpgrp()}\n"
        "[suite-slot] killing descendant tree + self (ancestors safe)\n"
        "=============================================================\n"
    )
    try:
        sys.stderr.write(banner)
        sys.stderr.flush()
    except Exception:
        pass

    # Kill only our descendants — never ancestors, never the shared pgid.
    descendants = _get_descendant_pids(my_pid)
    for pid in reversed(descendants):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    # Terminate self hard — os._exit skips atexit/cleanup but that is
    # intentional for a hard watchdog timeout. Exit code 137 mirrors SIGKILL.
    os._exit(137)


def _arm_watchdog() -> None:
    """Arm the self-destruct watchdog if a positive bound is configured.

    Caller MUST hold the acquired lock. Sets the module-global ``_watchdog``
    so ``pytest_unconfigure`` can cancel it on a clean finish.
    """
    global _watchdog

    max_seconds = _max_seconds()
    if max_seconds <= 0:
        sys.stderr.write(
            f"[suite-slot] watchdog disabled ({_MAX_SECONDS_ENV}={max_seconds:.0f})\n"
        )
        return

    armed_at = time.monotonic()
    timer = threading.Timer(max_seconds, _watchdog_fire, args=(max_seconds, armed_at))
    timer.daemon = True
    timer.name = "suite-slot-watchdog"
    timer.start()
    _watchdog = timer
    sys.stderr.write(
        f"[suite-slot] watchdog armed — self-destruct in {max_seconds:.0f}s\n"
    )


def _cancel_watchdog() -> None:
    """Cancel a pending watchdog timer (clean finish before the bound)."""
    global _watchdog
    if _watchdog is not None:
        try:
            _watchdog.cancel()
        except Exception:
            pass
        _watchdog = None


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

    # Lock is genuinely held now — arm the wall-clock self-destruct watchdog.
    _arm_watchdog()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Release the suite slot lock."""
    global _lock_fd
    # Cancel the watchdog first: we finished within the bound.
    _cancel_watchdog()
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None

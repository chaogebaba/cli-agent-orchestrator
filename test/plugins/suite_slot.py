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
  value <= 0 disables).

Kill strategy — layered (F445 R2):

  PRIMARY: At arming, the plugin calls os.setpgid(0, 0) to put this
  pytest controller into its OWN fresh process group (pgid == our pid).
  Ancestors remain in the old group and are structurally unreachable.
  On fire: os.killpg(our_pgid, SIGKILL) kills the entire group —
  including children that were reparented away from our /proc subtree
  but never called setsid() (they inherit our pgid).

  SECONDARY (setsid escapees): A periodic background thread (the ledger
  sampler) records (pid, /proc starttime) for all current descendants of
  this process AND all processes whose pgid matches our group. At fire
  time, the watchdog additionally signals every ledger entry still alive
  whose (pid, starttime) BOTH match the recording. The starttime match
  is the pid-reuse guard: a stale ledger entry whose pid was recycled by
  the kernel will have a different starttime and is skipped — never
  signaled.

  RESIDUAL: A child that both (a) calls setsid() immediately after fork
  AND (b) is never sampled between its creation and watchdog fire can
  still escape. This window is narrow (sampling interval is 2 s) and is
  an acknowledged limitation: containment of such workload requires
  kernel namespaces (cgroups/pidns), which this plugin does not employ.

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

# Ledger sampler thread (secondary layer)
_ledger_thread: threading.Thread | None = None
_ledger_stop: threading.Event = threading.Event()

# The pgid we create at arming (== our pid after setpgid(0,0))
_armed_pgid: int | None = None

# Env knob for the wall-clock hard bound; default 1 hour.
_MAX_SECONDS_ENV = "CAO_SUITE_SLOT_MAX_SECONDS"
_DEFAULT_MAX_SECONDS = 3600.0

# Ledger: maps pid → starttime (clock ticks from /proc/pid/stat field 22).
# Protected by _ledger_lock for concurrent read (fire) / write (sampler).
_ledger: dict[int, int] = {}
_ledger_lock: threading.Lock = threading.Lock()

# Sampling interval for the periodic ledger (seconds).
_LEDGER_SAMPLE_INTERVAL = 2.0


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


def _get_starttime(pid: int) -> int | None:
    """Read the starttime (field 22) of a process from /proc/pid/stat.

    Returns None if the process does not exist or is unreadable.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat_data = f.read().decode("utf-8", errors="replace")
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2:].split()
        # fields[0]=state, fields[1]=ppid, ..., fields[19]=starttime (0-indexed from after ')')
        # In the raw /proc/pid/stat format, starttime is field 22 (1-indexed overall).
        # After stripping "pid (comm) ", remaining fields start at field 3.
        # starttime is field 22 overall → index 22-3 = 19 in the remaining fields.
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _get_pgid(pid: int) -> int | None:
    """Read the process group id (field 5) from /proc/pid/stat.

    Returns None if the process does not exist or is unreadable.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat_data = f.read().decode("utf-8", errors="replace")
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2:].split()
        # pgrp is field 5 overall → index 5-3 = 2 in remaining fields.
        return int(fields[2])
    except (OSError, IndexError, ValueError):
        return None


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


def _sample_ledger(my_pid: int, my_pgid: int) -> None:
    """Sample current descendants and pgid-members into the ledger.

    Called periodically by the ledger sampler thread. Records (pid, starttime)
    for:
      1. All BFS descendants of my_pid (catches direct children).
      2. All processes whose pgid == my_pgid (catches reparented non-setsid
         children that left the /proc subtree but kept our pgid).

    Existing entries are never removed — the ledger grows monotonically over
    the run lifetime so that short-lived children are still tracked at fire.
    """
    seen: dict[int, int] = {}

    # (1) BFS descendants
    for pid in _get_descendant_pids(my_pid):
        st = _get_starttime(pid)
        if st is not None:
            seen[pid] = st

    # (2) Same-pgid processes (catches reparented children that kept pgid)
    try:
        entries = os.listdir("/proc")
    except OSError:
        entries = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == my_pid:
            continue
        pgid = _get_pgid(pid)
        if pgid == my_pgid:
            st = _get_starttime(pid)
            if st is not None:
                seen[pid] = st

    with _ledger_lock:
        _ledger.update(seen)


def _ledger_sampler_loop(my_pid: int, my_pgid: int) -> None:
    """Background thread that periodically samples the descendant ledger."""
    while not _ledger_stop.wait(timeout=_LEDGER_SAMPLE_INTERVAL):
        _sample_ledger(my_pid, my_pgid)


def _watchdog_fire(max_seconds: float, armed_at: float) -> None:
    """Kill the pytest process group + ledger-tracked escapees, then exit.

    Layered kill strategy (ORDER MATTERS — killpg kills us too):
      1. Walk the ledger and SIGKILL any entry whose (pid, starttime) still
         matches — catches setsid escapees that left the group (secondary).
         This runs FIRST because step 2 kills our own process.
      2. os.killpg(our pgid) — catches everything in our group (primary).
         This also kills us (we are in the group), so nothing after this
         line is guaranteed to execute.
      3. os._exit(137) — fallback if we somehow survive the killpg.

    The starttime check is the pid-reuse guard: a recycled PID will have a
    different starttime and is never signaled.
    """
    elapsed = time.monotonic() - armed_at
    holder_info = _read_holder_info()
    my_pid = os.getpid()
    my_pgid = _armed_pgid if _armed_pgid is not None else os.getpgrp()
    banner = (
        "\n"
        "==================== SUITE-SLOT WATCHDOG ====================\n"
        f"[suite-slot] SELF-DESTRUCT: wall-clock bound of {max_seconds:.0f}s exceeded\n"
        f"[suite-slot]   elapsed:   {elapsed:.1f}s\n"
        f"[suite-slot]   holder:    {holder_info}\n"
        f"[suite-slot]   controller pid={my_pid} pgid={my_pgid}\n"
        "[suite-slot] killing ledger-tracked escapees + process group\n"
        "=============================================================\n"
    )
    try:
        sys.stderr.write(banner)
        sys.stderr.flush()
    except Exception:
        pass

    # SECONDARY (runs first): kill ledger entries that escaped via setsid().
    # Must run BEFORE killpg because killpg kills our own process (we're in
    # the target group) — nothing after killpg is guaranteed to execute.
    with _ledger_lock:
        ledger_snapshot = dict(_ledger)

    for pid, recorded_starttime in ledger_snapshot.items():
        if pid == my_pid:
            continue
        # Revalidate identity: starttime must still match.
        current_starttime = _get_starttime(pid)
        if current_starttime is None:
            continue  # already dead
        if current_starttime != recorded_starttime:
            continue  # pid was recycled — skip
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    # PRIMARY: kill the entire process group we created at arming.
    # This sends SIGKILL to all processes in our group INCLUDING ourselves.
    # After this call, the process is likely dead — the lines below are
    # best-effort fallbacks for the unlikely case we survive.
    try:
        os.killpg(my_pgid, signal.SIGKILL)
    except OSError:
        pass

    # Terminate self hard — fallback if killpg didn't kill us (e.g. if we
    # are somehow not in the target group). Exit code 137 mirrors SIGKILL.
    os._exit(137)


def _arm_watchdog() -> None:
    """Arm the self-destruct watchdog if a positive bound is configured.

    Caller MUST hold the acquired lock. Performs:
      1. os.setpgid(0, 0) — puts this process into its own fresh pgid.
      2. Starts the periodic ledger sampler thread.
      3. Starts the Timer that fires _watchdog_fire after max_seconds.

    Sets module-globals so ``pytest_unconfigure`` can cancel on clean finish.
    """
    global _watchdog, _ledger_thread, _armed_pgid

    max_seconds = _max_seconds()
    if max_seconds <= 0:
        sys.stderr.write(
            f"[suite-slot] watchdog disabled ({_MAX_SECONDS_ENV}={max_seconds:.0f})\n"
        )
        return

    # PRIMARY: create a fresh process group for this pytest controller.
    # Ancestors stay in the old group and are structurally unreachable by
    # os.killpg on OUR group.
    try:
        os.setpgid(0, 0)
    except OSError:
        # If we are already a session leader, setpgid fails with EPERM.
        # Fall back: pgid stays as-is (same as r1 BFS-only behavior).
        pass
    _armed_pgid = os.getpgrp()

    my_pid = os.getpid()

    # Do an initial ledger sample immediately.
    _ledger_stop.clear()
    _ledger.clear()
    _sample_ledger(my_pid, _armed_pgid)

    # Start the periodic sampler.
    sampler = threading.Thread(
        target=_ledger_sampler_loop,
        args=(my_pid, _armed_pgid),
        daemon=True,
        name="suite-slot-ledger-sampler",
    )
    sampler.start()
    _ledger_thread = sampler

    # Arm the fire timer.
    armed_at = time.monotonic()
    timer = threading.Timer(max_seconds, _watchdog_fire, args=(max_seconds, armed_at))
    timer.daemon = True
    timer.name = "suite-slot-watchdog"
    timer.start()
    _watchdog = timer
    sys.stderr.write(
        f"[suite-slot] watchdog armed — pgid={_armed_pgid}, "
        f"self-destruct in {max_seconds:.0f}s\n"
    )


def _cancel_watchdog() -> None:
    """Cancel a pending watchdog timer and ledger sampler (clean finish)."""
    global _watchdog, _ledger_thread, _armed_pgid
    if _watchdog is not None:
        try:
            _watchdog.cancel()
        except Exception:
            pass
        _watchdog = None
    # Stop the ledger sampler.
    _ledger_stop.set()
    if _ledger_thread is not None:
        try:
            _ledger_thread.join(timeout=3)
        except Exception:
            pass
        _ledger_thread = None
    _armed_pgid = None


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

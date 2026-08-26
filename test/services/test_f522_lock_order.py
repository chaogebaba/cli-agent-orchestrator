"""F522 (#377) regression: ABBA deadlock between the pane-liveness lock and the
status-monitor lock.

Tonight's P0 (2026-08-26, twice ~2min after startup) wedged the server event
loop on an ABBA lock inversion between two real service singletons:

  * ``pane_liveness.observe()`` (the sampler tick) took the PANE lock and then,
    while holding it, read ``status_monitor.get_published_status()`` — which
    takes the MONITOR lock.  Order: PANE -> MONITOR.
  * ``status_monitor.get_boundary_observation()`` took the MONITOR lock and
    then, while holding it, fused via ``fuse_status`` -> ``pane_liveness.peek()``
    — which takes the PANE lock.  Order: MONITOR -> PANE.

Opposite nesting order under concurrency deadlocks.  The hotfix (commit
6cdf2d86) pre-reads ``get_published_status`` in ``observe()`` BEFORE the pane
lock is taken, so the monitor lock is never acquired while the pane lock is
held and the inversion is impossible.

This test drives the two REAL code paths on the REAL service singletons from
two threads and asserts, under a bounded join, that neither wedges.  On
pre-hotfix code it deadlocks and the bounded join expires (FAIL); on the
hotfix it completes in well under a second (PASS).  No tmux is needed: the
sampler's ``_capture`` is stubbed, so ``observe`` exercises the full
lock-ordering path without a backend.
"""

import threading
import time

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.pane_liveness import pane_liveness
from cli_agent_orchestrator.services.status_monitor import status_monitor

# Bounded wall-clock budget for the whole concurrent phase. Healthy code
# finishes the loops in well under a second; a wedge never finishes, so this
# ceiling is the FAIL edge. Kept comfortably under the task's <10s cap.
_JOIN_TIMEOUT_S = 8.0
_ITERATIONS = 4000

_TID = "f522-lock-order-tid"


@pytest.fixture
def _seeded_singletons(monkeypatch):
    """Wire the two real singletons for the contended paths, tmux-free.

    * Stub ``pane_liveness._capture`` so ``observe`` runs the full lock path
      (pane lock + pre-read of the published status) with no backend/tmux.
    * Seed ``status_monitor._last_status`` = PROCESSING so ``fuse_status``
      rule 3a actually reaches ``pane_liveness.peek`` (the MONITOR->PANE arm);
      an UNKNOWN/None published status would short-circuit before peek and the
      inversion could never form.
    * Prime one usable pane sample so ``peek`` returns a real observation
      rather than ``None`` (again, so the contended arm is actually taken).
    """
    monkeypatch.setattr(pane_liveness, "_capture", lambda tid: ("fp-const", "tail"))

    with status_monitor._lock:
        status_monitor._last_status[_TID] = TerminalStatus.PROCESSING
    # One observe() to populate pane state so peek() is non-None thereafter.
    pane_liveness.observe(_TID, monitor=status_monitor)

    yield

    # Best-effort cleanup. On the pre-hotfix (deadlocking) code a daemon worker
    # may still hold the pane lock forever, so never BLOCK teardown on it —
    # acquire non-blocking and skip if wedged (the daemon dies at process exit).
    if pane_liveness._lock.acquire(blocking=False):
        try:
            pane_liveness._state.pop(_TID, None)
        finally:
            pane_liveness._lock.release()
    if status_monitor._lock.acquire(blocking=False):
        try:
            status_monitor._last_status.pop(_TID, None)
        finally:
            status_monitor._lock.release()


def test_f522_observe_and_boundary_do_not_deadlock(_seeded_singletons):
    """Two threads driving the real ABBA-prone paths must both complete.

    Thread A: ``pane_liveness.observe`` (PANE lock, then reads published status).
    Thread B: ``status_monitor.get_boundary_observation`` -> ``fuse_status`` ->
              ``pane_liveness.peek`` (MONITOR lock, then PANE lock).

    Pre-hotfix these nest in opposite order and wedge; the bounded join then
    expires and the assertions below fail. Post-hotfix both finish fast.
    """
    start = threading.Barrier(2)
    errors: list[BaseException] = []
    done = {"A": False, "B": False}

    def thread_a():
        try:
            start.wait()
            for _ in range(_ITERATIONS):
                pane_liveness.observe(_TID, monitor=status_monitor)
            done["A"] = True
        except BaseException as exc:  # pragma: no cover - defensive
            errors.append(exc)

    def thread_b():
        try:
            start.wait()
            for _ in range(_ITERATIONS):
                status_monitor.get_boundary_observation(_TID)
            done["B"] = True
        except BaseException as exc:  # pragma: no cover - defensive
            errors.append(exc)

    # daemon=True: on the pre-hotfix (deadlocking) code these two workers wedge
    # forever holding opposite locks. Daemon threads let the test process exit
    # cleanly on the FAIL edge (bounded-join assertion) instead of hanging at
    # interpreter shutdown waiting on non-daemon threads that never return.
    ta = threading.Thread(target=thread_a, name="f522-observe", daemon=True)
    tb = threading.Thread(target=thread_b, name="f522-boundary", daemon=True)

    t0 = time.monotonic()
    ta.start()
    tb.start()

    ta.join(timeout=_JOIN_TIMEOUT_S)
    remaining = max(0.0, _JOIN_TIMEOUT_S - (time.monotonic() - t0))
    tb.join(timeout=remaining)

    a_alive = ta.is_alive()
    b_alive = tb.is_alive()

    # If either thread is still running after the bounded join, the lock order
    # inverted and wedged — the exact P0 this test guards against.
    assert not a_alive and not b_alive, (
        "F522 ABBA deadlock: observe/get_boundary_observation wedged "
        f"(A alive={a_alive}, B alive={b_alive}) — the pane lock and the "
        "monitor lock nested in opposite order. observe() must pre-read the "
        "published status BEFORE taking the pane lock (commit 6cdf2d86)."
    )
    assert not errors, f"unexpected error on a worker thread: {errors!r}"
    assert done["A"] and done["B"]

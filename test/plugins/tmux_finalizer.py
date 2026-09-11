"""F330 — Session-scoped tmux session finalizer, robust to crashes.

Problem: Tests that create tmux sessions (via TerminalFactory or cao_server
fixtures) leak sessions when tests fail or xdist workers crash. Observed:
cao-ownership-20866ae7 lingered post-suite with pane cwd inside a dead
pytest fixture dir.

Solution: An autouse session-scoped fixture that, on teardown, kills any
tmux sessions whose names match the test-created prefixes:
  - caotest-*   (TerminalFactory / cao_terminal fixture)
  - cao-test-*  (legacy naming)

Additionally, a pytest_sessionfinish hook sweeps the same prefixes as a
belt-and-suspenders fallback — this fires even if the session fixture's
finalizer is skipped due to xdist worker crash ("node down: Not properly
terminated").

Safety fence #1: ONLY sessions matching the test prefixes are touched. Live
production sessions (cao-<uuid> without the "test" infix) are never affected.

Safety fence #2 — THE SWEEP NEVER RUNS IN AN XDIST WORKER (2026-09-11).
Every xdist worker is its own pytest *session*, so both sweep sites fire in
each worker the moment that worker runs out of work — while its siblings are
still running. All workers share ONE tmux server (one socket per uid) and every
worker's ``cao_server`` exports the same ``CAO_SESSION_PREFIX=cao-test-``, so an
early-finishing worker's sweep killed LIVE sessions belonging to other workers.
Measured on grok-box-009, ``-n 4 -m "not e2e and not slow"``: at 12:12:08.474
gw1's fixture teardown swept and killed ``cao-test-sm-ni-4823d873``, the session
``test_send_to_busy_worker_queues_not_injects`` had just created on gw2 (which
did not finish until 12:12:36); the pane died mid-launch, ``_confirm_launch_health``
confirmed a dead process tree after its 5s deadline, and ``POST /sessions``
answered 500 ``provider_launch_failed``. That is the same cross-worker kill
``test/ux/contract/conftest.py`` fenced off for its own per-test cleanup — this
plugin had reopened it globally.

The leak guarantee is unchanged: the CONTROLLER process registers this plugin
too and its ``pytest_sessionfinish`` runs AFTER every worker has exited (measured
in the same run: controller sweep at 12:12:41, 5s behind the last worker), so a
worker that crashes or leaks is still swept — by the one process that outlives
them all. A serial run (no xdist) has no ``workerinput`` and sweeps as before.

Registered via ``pytest_plugins`` in ``test/conftest.py``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Generator

import pytest

# Prefixes used by the test suite for tmux session names.
# These MUST NOT match production session names (which are cao-<uuid> without "test").
_TEST_SESSION_PREFIXES = ("caotest-", "cao-test-")

# Compiled pattern for efficient matching.
_TEST_SESSION_RE = re.compile(r"^(caotest-|cao-test-)")


def _is_xdist_worker(config: pytest.Config) -> bool:
    """True when this process is an xdist worker (``gw0``…), not the controller.

    Same discriminator ``test/plugins/suite_slot.py`` uses: xdist sets
    ``config.workerinput`` on the worker only.
    """
    return hasattr(config, "workerinput")


def _list_tmux_sessions() -> list[str]:
    """List all tmux session names, suppressing errors if tmux is not running."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def _kill_test_sessions() -> int:
    """Kill all tmux sessions matching test prefixes. Returns count killed."""
    sessions = _list_tmux_sessions()
    killed = 0
    for name in sessions:
        if _TEST_SESSION_RE.match(name):
            try:
                subprocess.run(
                    ["tmux", "kill-session", "-t", name],
                    capture_output=True,
                    timeout=5,
                )
                killed += 1
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
    return killed


def sweep_unless_xdist_worker(config: pytest.Config, origin: str) -> int:
    """Run the prefix sweep, but only from a process that owns the whole run.

    Returns the number of sessions killed (0 when the sweep was declined). This
    is the ONE gate both sweep sites go through — see fence #2 in the module
    docstring for why a worker must never sweep.
    """
    if _is_xdist_worker(config):
        return 0
    killed = _kill_test_sessions()
    if killed:
        sys.stderr.write(f"[tmux-finalizer] {origin} killed {killed} stale test session(s)\n")
    return killed


@pytest.fixture(autouse=True, scope="session")
def _f330_tmux_session_finalizer(
    request: pytest.FixtureRequest,
) -> "Generator[None, None, None]":
    """Session-scoped autouse fixture: sweep test tmux sessions on teardown."""
    yield
    sweep_unless_xdist_worker(request.config, "fixture teardown")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Belt-and-suspenders: sweep test sessions even if fixture teardown was skipped.

    This catches the xdist "node down: Not properly terminated" scenario where
    session fixture finalizers are never called — which is precisely why the
    sweep belongs to the CONTROLLER, whose sessionfinish runs after the last
    worker has exited, and not to a worker that may still have live siblings.
    """
    sweep_unless_xdist_worker(session.config, "sessionfinish sweep")

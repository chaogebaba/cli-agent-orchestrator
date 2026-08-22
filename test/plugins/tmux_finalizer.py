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

Safety fence: ONLY sessions matching the test prefixes are touched. Live
production sessions (cao-<uuid> without the "test" infix) are never affected.

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


@pytest.fixture(autouse=True, scope="session")
def _f330_tmux_session_finalizer() -> "Generator[None, None, None]":
    """Session-scoped autouse fixture: sweep test tmux sessions on teardown."""
    yield
    killed = _kill_test_sessions()
    if killed:
        sys.stderr.write(f"[tmux-finalizer] killed {killed} stale test session(s)\n")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Belt-and-suspenders: sweep test sessions even if fixture teardown was skipped.

    This catches the xdist "node down: Not properly terminated" scenario where
    session fixture finalizers are never called.
    """
    killed = _kill_test_sessions()
    if killed:
        sys.stderr.write(
            f"[tmux-finalizer] sessionfinish sweep killed {killed} stale test session(s)\n"
        )

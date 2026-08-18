"""Contract-tier conftest: tracks and cleans up tmux sessions created by tests.

F4 fix: C-kind tests create sessions via POST /sessions directly (bypassing
cao_terminal fixture). Without cleanup, each test leaks a tmux session.
This module-scoped fixture tracks sessions and kills them on teardown.
"""

import subprocess

import pytest
import requests

from test.fixtures.cao_server import CaoServer


@pytest.fixture(autouse=True)
def _cleanup_contract_sessions(cao_server: CaoServer, request):
    """Track tmux sessions before/after each test and clean up leaked ones."""
    # Capture sessions before test
    pre_sessions = _list_tmux_sessions()

    yield

    # After test: find new sessions and kill them
    post_sessions = _list_tmux_sessions()
    leaked = post_sessions - pre_sessions

    for session_name in leaked:
        # Try HTTP DELETE first (clean DB state)
        try:
            requests.delete(
                f"{cao_server.url}/sessions/{session_name}",
                timeout=5,
            )
        except Exception:
            pass
        # Then kill the tmux session directly
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


def _list_tmux_sessions() -> set:
    """Return set of current tmux session names."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return set(result.stdout.strip().splitlines())
    except Exception:
        pass
    return set()

"""Contract-tier conftest: serialization + targeted cleanup.

F254 P5 hotfix: under xdist (-n 2), the original diff-based tmux cleanup
killed sessions belonging to sibling workers. The fix:
1. Serialize all contract tests via xdist_group("contract-server") so they
   share one cao_server and run sequentially on one worker (D34 pattern).
2. Clean up only sessions created by THIS test (tracked by intercepting
   the POST /sessions calls, not by diffing global tmux state).

Blueprint justification: D34 names groups by resource. The cao_server's
tmux namespace + SQLite DB are the shared resource. Serialization adds
~5s overhead (session fixture amortized) vs the alternative of cross-worker
races on a shared tmux server.
"""

import subprocess
from typing import List

import pytest
import requests

from test.fixtures.cao_server import CaoServer


def pytest_collection_modifyitems(items: List[pytest.Item]) -> None:
    """Assign all test/ux/contract/ tests to xdist_group 'contract-server'.

    This ensures they all land on the same worker and share one cao_server
    session fixture without cross-worker interference.
    """
    for item in items:
        if "/ux/contract/" in str(item.path):
            item.add_marker(pytest.mark.xdist_group("contract-server"))


@pytest.fixture(autouse=True)
def _cleanup_contract_sessions(cao_server: CaoServer, request):
    """Clean up tmux sessions created by THIS test only.

    Instead of diffing global tmux state (which races under xdist), we
    collect session names from the test's HTTP responses and kill only those.
    """
    created_sessions: list[str] = []

    # Store the list on the request node so tests can register sessions
    request.node._contract_sessions = created_sessions

    yield

    # Teardown: kill only sessions WE created
    for session_name in created_sessions:
        try:
            requests.delete(
                f"{cao_server.url}/sessions/{session_name}",
                timeout=5,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


@pytest.fixture
def track_session(request):
    """Helper fixture for contract tests to register created session names."""

    def _track(session_name: str) -> None:
        if hasattr(request.node, "_contract_sessions"):
            request.node._contract_sessions.append(session_name)

    return _track

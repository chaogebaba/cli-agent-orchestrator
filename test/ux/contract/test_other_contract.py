"""C-kind tests for S04, S06, S07, S08, S10, S11, S12.

Drives remaining MCP tool functions against a live cao_server subprocess.
"""

import hashlib
import uuid

import pytest
import requests

from test.fixtures.cao_server import CaoServer


def _setup_session(cao_server: CaoServer, track_fn=None):
    """Create a session + supervisor terminal, return (sup_id, session_name)."""
    session_name = f"contract-{uuid.uuid4().hex[:8]}"
    resp = requests.post(
        f"{cao_server.url}/sessions",
        params={
            "provider": "mock_cli",
            "agent_profile": "developer",
            "session_name": session_name,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    actual_session = data.get("session_name", f"cao-{session_name}")
    if track_fn:
        track_fn(actual_session)
    return data["id"], actual_session


@pytest.mark.ux(surface="S04", invariant="UX-2", kind="C")
class TestListMessagesContractUX2:
    """Contract tests for list_messages / ack_messages: UX-2."""

    def test_list_messages_against_live_server(
        self, cao_server: CaoServer, monkeypatch, track_session, tmp_path
    ):
        """list_messages via MCP impl hits the live server."""
        sup_id, session = _setup_session(cao_server, track_session)
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        from cli_agent_orchestrator.mcp_server.server import _list_messages_impl

        result = _list_messages_impl()
        # Should not raise; returns messages (possibly empty list)
        assert isinstance(result, (list, dict))


@pytest.mark.ux(surface="S06", invariant="UX-5", kind="C")
class TestAuthorityPinsContractUX5:
    """Contract tests for authority pins: UX-5."""

    def test_verify_pin_computes_correct_hash(
        self, cao_server: CaoServer, monkeypatch, track_session, tmp_path
    ):
        """Verify pin computes sha256 of a real file (authority validation)."""
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")

        test_file = tmp_path / "authority.py"
        test_file.write_text("def main(): pass")
        expected_sha = hashlib.sha256(test_file.read_bytes()).hexdigest()

        from cli_agent_orchestrator.services.authority_pin_service import _hash_file

        result_sha, error = _hash_file(str(test_file))
        assert error is None
        assert result_sha == expected_sha


@pytest.mark.ux(surface="S07", invariant="UX-4", kind="C")
class TestBarrierContractUX4:
    """Contract tests for callback barrier: UX-4."""

    def test_barrier_creation_via_assign(
        self, cao_server: CaoServer, monkeypatch, track_session, tmp_path
    ):
        """Assign with barrier parameter creates a barrier on the server."""
        sup_id, session = _setup_session(cao_server, track_session)
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        barrier_label = f"test-barrier-{uuid.uuid4().hex[:6]}"
        result = _assign_impl(
            agent_profile="developer",
            message="barrier contract test",
            working_directory=str(tmp_path),
            barrier=barrier_label,
        )

        # Assign with barrier should succeed (creates terminal + barrier)
        assert result["success"] is True
        assert result["terminal_id"] is not None


@pytest.mark.ux(surface="S08", invariant="UX-4", kind="C")
class TestWorkflowContractUX4:
    """Contract tests for workflow: UX-4."""

    def test_workflow_list_against_live_server(
        self, cao_server: CaoServer, monkeypatch, track_session
    ):
        """workflow_list queries the live server."""
        sup_id, _ = _setup_session(cao_server, track_session)
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        # Query workflow runs (may be empty)
        resp = requests.get(
            f"{cao_server.url}/workflows/runs",
            timeout=10,
        )
        # Should return 200 with empty list or list of runs
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, (list, dict))


@pytest.mark.ux(surface="S10", invariant="UX-6", kind="C")
class TestFleetContractUX6:
    """Contract tests for session_manifest / fleet: UX-6."""

    def test_terminal_queryable_on_live_server(
        self, cao_server: CaoServer, monkeypatch, track_session
    ):
        """Created terminal is queryable via GET /terminals/{id}."""
        sup_id, session = _setup_session(cao_server, track_session)
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        resp = requests.get(f"{cao_server.url}/terminals/{sup_id}", timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == sup_id
        assert data["provider"] == "mock_cli"


@pytest.mark.ux(surface="S11", invariant="UX-6", kind="C")
class TestSiblingsContractUX6:
    """Contract tests for peek/delete/interrupt: UX-6."""

    def test_peek_terminal_against_live_server(
        self, cao_server: CaoServer, monkeypatch, track_session, tmp_path
    ):
        """peek_terminal queries the live server for pane content."""
        sup_id, session = _setup_session(cao_server, track_session)
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        from cli_agent_orchestrator.mcp_server.server import _peek_terminal_impl

        # Peek the supervisor's own terminal
        result = _peek_terminal_impl(terminal_id=sup_id, lines=20)
        assert isinstance(result, (str, dict))


@pytest.mark.ux(surface="S12", invariant="UX-6", kind="C")
class TestProfilesContractUX6:
    """Contract tests for find_profiles / base sessions: UX-6."""

    def test_find_profiles_returns_results(self, cao_server: CaoServer, monkeypatch, track_session):
        """find_profiles returns profile metadata (local operation)."""
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")

        from cli_agent_orchestrator.services.profile_search import search_profiles

        results = search_profiles("developer", limit=5)
        assert isinstance(results, list)

    def test_list_base_sessions_via_api(
        self, cao_server: CaoServer, monkeypatch, track_session
    ):
        """list_base_sessions is a local operation that returns a list."""
        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")

        from cli_agent_orchestrator.services.fork_context_service import list_bases

        bases = list_bases()
        # Should return a list (possibly empty)
        assert isinstance(bases, list)

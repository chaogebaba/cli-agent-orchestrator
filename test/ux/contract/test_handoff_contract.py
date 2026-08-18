"""C-kind tests for S02 (handoff) — UX-1, UX-4.

Drives handoff-path (via _assign_impl) against a live cao_server subprocess.
"""

import uuid

import pytest
import requests

from test.fixtures.cao_server import CaoServer


@pytest.mark.ux(surface="S02", invariant="UX-1", kind="C")
class TestHandoffContractUX1:
    """Contract tests for handoff: UX-1 Arrival invariant."""

    def test_handoff_creates_worker_with_brief(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """Handoff (via _assign_impl) creates a worker on the live server."""
        session_name = f"ho-contract-{uuid.uuid4().hex[:8]}"
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
        sup_id = resp.json()["id"]

        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        brief = "HANDOFF_BRIEF: Complete the refactoring task"
        result = _assign_impl(
            agent_profile="developer",
            message=brief,
            working_directory=str(tmp_path),
        )

        assert result["success"] is True
        worker_id = result["terminal_id"]

        # Verify worker exists on server
        worker_resp = requests.get(
            f"{cao_server.url}/terminals/{worker_id}", timeout=10
        )
        assert worker_resp.status_code == 200
        assert worker_resp.json()["provider"] == "mock_cli"


@pytest.mark.ux(surface="S02", invariant="UX-4", kind="C")
class TestHandoffContractUX4:
    """Contract tests for handoff: UX-4 Return invariant."""

    def test_handoff_worker_has_caller_id(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """Worker created by handoff has caller_id set for callback routing."""
        session_name = f"ho-cb-{uuid.uuid4().hex[:8]}"
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
        sup_id = resp.json()["id"]

        monkeypatch.setenv("CAO_ENDPOINT", cao_server.url)
        monkeypatch.setenv("CAO_TERMINAL_ID", sup_id)

        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        result = _assign_impl(
            agent_profile="developer",
            message="callback routing test",
            working_directory=str(tmp_path),
        )

        assert result["success"] is True
        worker_id = result["terminal_id"]

        # Verify caller_id is set on the worker
        worker_resp = requests.get(
            f"{cao_server.url}/terminals/{worker_id}", timeout=10
        )
        assert worker_resp.status_code == 200
        worker_data = worker_resp.json()
        # caller_id should be the supervisor
        assert worker_data.get("caller_id") == sup_id

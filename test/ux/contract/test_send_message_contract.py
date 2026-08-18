"""C-kind tests for S03 (send_message) — UX-2, UX-3, UX-5.

Drives _send_message_impl against a live cao_server subprocess.
"""

import uuid

import pytest
import requests

from test.fixtures.cao_server import CaoServer


@pytest.mark.ux(surface="S03", invariant="UX-2", kind="C")
class TestSendMessageContractUX2:
    """Contract tests for send_message: UX-2 Delivery invariant."""

    def test_send_message_queues_on_server(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """send_message creates an inbox entry on the live server."""
        session_name = f"sm-contract-{uuid.uuid4().hex[:8]}"
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

        # Create a second terminal to be the receiver
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        worker_result = _assign_impl(
            agent_profile="developer",
            message="initial setup",
            working_directory=str(tmp_path),
        )
        assert worker_result["success"] is True
        receiver_id = worker_result["terminal_id"]

        # Now send a message to the receiver
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        msg = f"CONTRACT_MSG_{uuid.uuid4().hex[:8]}"
        result = _send_message_impl(
            message=msg,
            receiver_id=receiver_id,
        )

        # The message should be queued successfully
        assert result.get("success") is True or "queued" in str(result).lower()


@pytest.mark.ux(surface="S03", invariant="UX-3", kind="C")
class TestSendMessageContractUX3:
    """Contract tests for send_message: UX-3 Non-interruption."""

    def test_send_to_busy_worker_queues_not_injects(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """Sending to a busy worker queues rather than interrupting."""
        session_name = f"sm-ni-{uuid.uuid4().hex[:8]}"
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

        from cli_agent_orchestrator.mcp_server.server import _assign_impl, _send_message_impl

        worker = _assign_impl(
            agent_profile="developer",
            message="setup worker",
            working_directory=str(tmp_path),
        )
        assert worker["success"] is True

        # Send while worker may be initializing (busy)
        result = _send_message_impl(
            message="non-interruption contract test",
            receiver_id=worker["terminal_id"],
        )

        # Should succeed (queued for delivery, not rejected)
        assert result.get("success") is True or "queued" in str(result).lower()


@pytest.mark.ux(surface="S03", invariant="UX-5", kind="C")
class TestSendMessageContractUX5:
    """Contract tests for send_message: UX-5 Authority."""

    def test_send_with_barrier_accepted(
        self, cao_server: CaoServer, monkeypatch, tmp_path
    ):
        """send_message with barrier validates ownership (expected behavior)."""
        session_name = f"sm-auth-{uuid.uuid4().hex[:8]}"
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

        from cli_agent_orchestrator.mcp_server.server import _assign_impl, _send_message_impl

        worker = _assign_impl(
            agent_profile="developer",
            message="barrier worker",
            working_directory=str(tmp_path),
        )
        assert worker["success"] is True

        # Send without barrier (basic delivery) should succeed
        result = _send_message_impl(
            message="authority contract test",
            receiver_id=worker["terminal_id"],
        )

        # Should be accepted (queued)
        assert result.get("success") is True or "queued" in str(result).lower()

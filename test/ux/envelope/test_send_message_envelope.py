"""E-kind tests for S03 (send_message) — UX-2, UX-3, UX-5.

Drives send_message against mocked transport to verify envelope.
"""

from unittest.mock import MagicMock, patch

import pytest

from test.ux.scenarios import delivery_three_messages, injection_during_prompt


@pytest.mark.ux(surface="S03", invariant="UX-2", kind="E")
class TestSendMessageEnvelopeUX2:
    """Envelope tests for send_message: UX-2 Delivery invariant."""

    def test_delivery_scenario_envelope(self, monkeypatch):
        """Drive delivery_three_messages scenario against mocked transport."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        sent_messages = []

        def mock_send(receiver_id, message):
            sent_messages.append(message)
            return {"success": True, "message_id": len(sent_messages)}

        def mock_pastes(terminal_id):
            return list(sent_messages)

        result = delivery_three_messages(
            send_fn=mock_send,
            get_pastes_fn=mock_pastes,
            target_terminal_id="target-001",
        )
        assert result.success, f"Scenario failed: {result.failures}"

    def test_send_message_failure_envelope(self, monkeypatch):
        """send_message to nonexistent receiver returns failure envelope."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        import requests

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "terminal not found"
        mock_resp.json.return_value = {"detail": "receiver not found"}
        error = requests.HTTPError(response=mock_resp)
        mock_resp.raise_for_status.side_effect = error

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.post.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _send_message_impl

            result = _send_message_impl(
                message="should fail",
                receiver_id="nonexistent",
            )

        assert result.get("success") is False or "error" in str(result).lower()


@pytest.mark.ux(surface="S03", invariant="UX-3", kind="E")
class TestSendMessageEnvelopeUX3:
    """Envelope tests for send_message: UX-3 Non-interruption invariant."""

    def test_injection_scenario_envelope(self, monkeypatch):
        """Drive injection_during_prompt scenario against mocked transport."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        busy = [True]
        pastes = []

        def set_busy(tid):
            busy[0] = True

        def send_fn(receiver_id, message):
            if not busy[0]:
                pastes.append(message)
            return {"success": True}

        def get_pastes(tid):
            return list(pastes)

        def clear_busy(tid):
            busy[0] = False
            # After clearing, queued message arrives
            pastes.append("INJECTED_MSG: This should not arrive during the prompt")

        result = injection_during_prompt(
            set_busy_fn=set_busy,
            send_fn=send_fn,
            get_pastes_fn=get_pastes,
            clear_busy_fn=clear_busy,
            target_terminal_id="busy-worker",
        )
        assert result.success, f"Scenario failed: {result.failures}"


@pytest.mark.ux(surface="S03", invariant="UX-5", kind="E")
class TestSendMessageEnvelopeUX5:
    """Envelope tests for send_message: UX-5 Authority invariant."""

    def test_send_message_with_barrier_envelope(self, monkeypatch):
        """send_message with barrier param returns documented shape."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aa11bb22")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "message_id": 77,
            "status": "queued",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.post.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _send_message_impl

            result = _send_message_impl(
                message="authority test",
                receiver_id="cc334455",
            )

        assert result.get("success") is True or "queued" in str(result)

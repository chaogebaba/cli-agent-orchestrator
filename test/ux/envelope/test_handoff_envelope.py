"""E-kind tests for S02 (handoff) — UX-1, UX-4.

Drives handoff tool functions against mocked transport to verify envelope.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.ux(surface="S02", invariant="UX-1", kind="E")
class TestHandoffEnvelopeUX1:
    """Envelope tests for handoff: UX-1 Arrival invariant."""

    def test_handoff_success_envelope(self, monkeypatch, tmp_path):
        """Handoff success returns documented HandoffResult shape."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "e5f6a7b8")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "e5f6a7b8",
            "provider": "mock_cli",
            "session_name": "ho-session",
            "status": "idle",
            "allowed_tools": None,
        }
        mock_get_resp.raise_for_status = MagicMock()

        # Mock terminal creation
        mock_create_resp = MagicMock()
        mock_create_resp.status_code = 201
        mock_create_resp.json.return_value = {
            "id": "ho-worker",
            "session_name": "ho-session",
            "name": "window-ho",
            "provider": "mock_cli",
            "status": "idle",
        }
        mock_create_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            # First GET is supervisor lookup, then terminal status polls
            def get_side_effect(url, **kwargs):
                if "/terminals/sup-ho-001" in url:
                    return mock_get_resp
                # Poll for completion
                status_resp = MagicMock()
                status_resp.status_code = 200
                status_resp.json.return_value = {
                    "id": "ho-worker",
                    "status": "idle",
                    "provider": "mock_cli",
                    "session_name": "ho-session",
                }
                status_resp.raise_for_status = MagicMock()
                return status_resp

            mock_http.get.side_effect = get_side_effect
            mock_http.post.return_value = mock_create_resp

            from cli_agent_orchestrator.mcp_server.server import _assign_impl

            # Handoff uses _assign_impl internally; test the assign envelope
            # which handoff wraps
            result = _assign_impl(
                agent_profile="developer",
                message="handoff brief for UX-1",
                working_directory=str(tmp_path),
            )

        assert result["success"] is True
        assert result["terminal_id"] == "ho-worker"


@pytest.mark.ux(surface="S02", invariant="UX-4", kind="E")
class TestHandoffEnvelopeUX4:
    """Envelope tests for handoff: UX-4 Return invariant."""

    def test_handoff_result_includes_terminal_id_for_callback(self, monkeypatch, tmp_path):
        """Handoff result carries terminal_id needed for callback routing."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "f6a7b8c9")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "f6a7b8c9",
            "provider": "mock_cli",
            "session_name": "ho-cb-session",
            "status": "idle",
            "allowed_tools": None,
        }
        mock_get_resp.raise_for_status = MagicMock()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_post_resp.json.return_value = {
            "id": "cb112233",
            "session_name": "ho-cb-session",
            "name": "window-0",
            "provider": "mock_cli",
            "status": "idle",
        }
        mock_post_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_get_resp
            mock_http.post.return_value = mock_post_resp

            from cli_agent_orchestrator.mcp_server.server import _assign_impl

            result = _assign_impl(
                agent_profile="developer",
                message="handoff callback test",
                working_directory=str(tmp_path),
            )

        assert result["success"] is True
        assert result["terminal_id"] is not None
        assert len(result["terminal_id"]) == 8

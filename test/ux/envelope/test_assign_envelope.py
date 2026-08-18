"""E-kind tests for S01 (assign) — UX-1, UX-6.

Drives _assign_impl against mocked transport to verify the documented
envelope on every branch, including error branches.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from test.ux.scenarios import arrival_two_workers


@pytest.mark.ux(surface="S01", invariant="UX-1", kind="E")
class TestAssignEnvelopeUX1:
    """Envelope tests for assign: UX-1 Arrival invariant."""

    def test_assign_returns_success_envelope(self, monkeypatch, tmp_path):
        """Assign with mocked transport returns documented success shape."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "id": "worker-001",
            "session_name": "test-session",
            "name": "window-0",
            "provider": "mock_cli",
            "status": "idle",
        }
        mock_resp.raise_for_status = MagicMock()

        # Mock GET /terminals/{id} (supervisor lookup)
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "a1b2c3d4",
            "provider": "mock_cli",
            "session_name": "test-session",
            "status": "idle",
            "allowed_tools": None,
        }
        mock_get_resp.raise_for_status = MagicMock()

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_get_resp
            mock_http.post.return_value = mock_resp

            from cli_agent_orchestrator.mcp_server.server import _assign_impl

            result = _assign_impl(
                agent_profile="developer",
                message="test brief for envelope check",
                working_directory=str(tmp_path),
            )

        assert result["success"] is True
        assert "terminal_id" in result
        assert result["terminal_id"] == "worker-001"
        assert "message" in result
        assert "display_name" in result

    def test_assign_failure_envelope_no_terminal_id(self, monkeypatch, tmp_path):
        """Assign without CAO_TERMINAL_ID returns the documented failure."""
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        result = _assign_impl(
            agent_profile="developer",
            message="should fail",
            working_directory=str(tmp_path),
        )

        assert result["success"] is False
        assert result["terminal_id"] is None
        assert "CAO_TERMINAL_ID" in result["message"]

    def test_assign_http_error_envelope(self, monkeypatch, tmp_path):
        """Assign that gets an HTTP error returns failure with detail."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "b2c3d4e5")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        import requests

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "b2c3d4e5",
            "provider": "mock_cli",
            "session_name": "err-session",
            "status": "idle",
            "allowed_tools": None,
        }
        mock_get_resp.raise_for_status = MagicMock()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 500
        mock_post_resp.text = "Internal Server Error"
        mock_post_resp.json.return_value = {"detail": "terminal creation failed"}
        error = requests.HTTPError(response=mock_post_resp)
        mock_post_resp.raise_for_status.side_effect = error

        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http"
        ) as mock_http:
            mock_http.get.return_value = mock_get_resp
            mock_http.post.return_value = mock_post_resp

            from cli_agent_orchestrator.mcp_server.server import _assign_impl

            result = _assign_impl(
                agent_profile="developer",
                message="should produce error envelope",
                working_directory=str(tmp_path),
            )

        assert result["success"] is False
        assert "message" in result

    def test_arrival_scenario_envelope(self, monkeypatch, tmp_path):
        """Drive the arrival_two_workers scenario against mocked transport."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "c3d4e5f6")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        call_count = [0]

        def mock_assign(profile, message, workdir):
            call_count[0] += 1
            return {"success": True, "terminal_id": f"w-{call_count[0]}"}

        def mock_screen(terminal_id):
            # Return the brief that was assigned (we track by terminal_id)
            if terminal_id == "w-1":
                return "BRIEF_ALPHA: Implement the frobnicate module with error handling"
            return "BRIEF_BETA: Refactor the widget factory to use dependency injection"

        result = arrival_two_workers(
            assign_fn=mock_assign,
            get_first_screen_fn=mock_screen,
            working_directory=str(tmp_path),
        )
        assert result.success, f"Scenario failed: {result.failures}"


@pytest.mark.ux(surface="S01", invariant="UX-6", kind="E")
class TestAssignEnvelopeUX6:
    """Envelope tests for assign: UX-6 Visibility invariant."""

    def test_assign_result_contains_visibility_fields(self, monkeypatch, tmp_path):
        """Assign result includes display_name and window_name for fleet visibility."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "d4e5f6a7")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "d4e5f6a7",
            "provider": "mock_cli",
            "session_name": "vis-session",
            "status": "idle",
            "allowed_tools": None,
        }
        mock_get_resp.raise_for_status = MagicMock()

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_post_resp.json.return_value = {
            "id": "vis-worker",
            "session_name": "vis-session",
            "name": "window-1",
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
                message="visibility test",
                working_directory=str(tmp_path),
            )

        assert result["success"] is True
        assert "display_name" in result
        assert "window_name" in result
        assert result["display_name"] is not None

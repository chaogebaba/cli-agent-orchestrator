"""Tests for C4: MCP assign surfaces HTTP error detail."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import _assign_impl


class TestAssignSurfacesHttpErrorDetail:
    """C4: Verify _assign_impl extracts response body detail on HTTP errors."""

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/tmp")
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex")
    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_child_allowed_tools",
        return_value=None,
    )
    @patch("cli_agent_orchestrator.mcp_server.server.cao_http")
    def test_500_with_body_surfaces_detail_code(
        self, mock_cao_http, _allowed_tools, _resolve_provider, _cwd, _nudge, monkeypatch
    ):
        """A 500 response with JSON body {"detail": "seed_exec_failed"} is surfaced."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")

        # Mock GET /terminals/{id} → success
        terminal_meta = MagicMock()
        terminal_meta.json.return_value = {
            "provider": "codex",
            "session_name": "cao-session",
            "allowed_tools": None,
        }
        terminal_meta.raise_for_status = MagicMock()

        # Mock POST /sessions/{name}/terminals → 500 with detail body
        error_response = MagicMock()
        error_response.status_code = 500
        error_response.json.return_value = {"detail": "seed_exec_failed"}
        http_error = requests.HTTPError(response=error_response)
        error_response.raise_for_status.side_effect = http_error

        mock_cao_http.get.return_value = terminal_meta
        mock_cao_http.post.return_value = error_response

        result = _assign_impl("developer", "do something")

        assert result["success"] is False
        assert "seed_exec_failed" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/tmp")
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex")
    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_child_allowed_tools",
        return_value=None,
    )
    @patch("cli_agent_orchestrator.mcp_server.server.cao_http")
    def test_500_without_json_body_falls_back_to_exc_str(
        self, mock_cao_http, _allowed_tools, _resolve_provider, _cwd, _nudge, monkeypatch
    ):
        """A 500 response with no parseable JSON body falls back to str(exc)."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "aaaaaaaa")

        terminal_meta = MagicMock()
        terminal_meta.json.return_value = {
            "provider": "codex",
            "session_name": "cao-session",
            "allowed_tools": None,
        }
        terminal_meta.raise_for_status = MagicMock()

        error_response = MagicMock()
        error_response.status_code = 500
        error_response.json.side_effect = ValueError("no json")
        http_error = requests.HTTPError("500 Server Error", response=error_response)
        error_response.raise_for_status.side_effect = http_error

        mock_cao_http.get.return_value = terminal_meta
        mock_cao_http.post.return_value = error_response

        result = _assign_impl("developer", "do something")

        assert result["success"] is False
        assert "500 Server Error" in result["message"]

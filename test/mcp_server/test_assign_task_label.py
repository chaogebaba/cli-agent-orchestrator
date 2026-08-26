"""F483: Tests for task_label parameter in assign and delete_terminal."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# Valid 8-char hex terminal IDs for tests
SUPERVISOR_ID = "aa000001"
WORKER_ID = "bb000042"
CHILD_ID = "cc000099"


class TestAssignTaskLabel:
    """Test that assign writes fleet-labels.tsv when task_label is provided."""

    @patch("cli_agent_orchestrator.services.fleet_labels.upsert_label")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/tmp")
    def test_assign_calls_upsert_label(
        self, _cwd, mock_create, mock_upsert, monkeypatch
    ):
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)
        mock_create.return_value = (WORKER_ID, None)

        with patch(
            "cli_agent_orchestrator.mcp_server.server.generate_window_name",
            return_value=f"dev:{WORKER_ID}",
        ), patch(
            "cli_agent_orchestrator.mcp_server.server.display_name",
            return_value=f"dev:{WORKER_ID}",
        ), patch(
            "cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION",
            True,
        ):
            result = _assign_impl(
                agent_profile="developer",
                message="do the thing",
                task_label="F483 build",
            )

        assert result["success"] is True
        assert result["terminal_id"] == WORKER_ID
        mock_upsert.assert_called_once_with(WORKER_ID, "F483 build")

    @patch("cli_agent_orchestrator.services.fleet_labels.upsert_label")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/tmp")
    def test_assign_skips_upsert_when_no_label(
        self, _cwd, mock_create, mock_upsert, monkeypatch
    ):
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)
        mock_create.return_value = (WORKER_ID, None)

        with patch(
            "cli_agent_orchestrator.mcp_server.server.generate_window_name",
            return_value=f"dev:{WORKER_ID}",
        ), patch(
            "cli_agent_orchestrator.mcp_server.server.display_name",
            return_value=f"dev:{WORKER_ID}",
        ), patch(
            "cli_agent_orchestrator.mcp_server.server.ENABLE_SENDER_ID_INJECTION",
            True,
        ):
            result = _assign_impl(
                agent_profile="developer",
                message="do the thing",
            )

        assert result["success"] is True
        mock_upsert.assert_not_called()


class TestDeleteTerminalRemovesLabel:
    """Test that delete_terminal removes the fleet-labels.tsv row."""

    @patch("cli_agent_orchestrator.services.fleet_labels.remove_label")
    @patch("cli_agent_orchestrator.mcp_server.server.cao_http")
    def test_delete_removes_label_for_reaped(self, mock_http, mock_remove, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import delete_terminal

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "reaped": [{"id": WORKER_ID, "status": "reaped"}],
            "skipped": [],
            "uncertain": [],
            "unattempted": [],
        }
        mock_http.delete.return_value = response

        result = delete_terminal(WORKER_ID)

        assert result.get("success") is True
        mock_remove.assert_called_once_with(WORKER_ID)

    @patch("cli_agent_orchestrator.services.fleet_labels.remove_label")
    @patch("cli_agent_orchestrator.mcp_server.server.cao_http")
    def test_delete_removes_label_cascade(self, mock_http, mock_remove, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import delete_terminal

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "reaped": [
                {"id": WORKER_ID, "status": "reaped"},
                {"id": CHILD_ID, "status": "reaped"},
            ],
            "skipped": [],
            "uncertain": [],
            "unattempted": [],
        }
        mock_http.delete.return_value = response

        result = delete_terminal(WORKER_ID)

        assert result.get("success") is True
        assert mock_remove.call_count == 2
        mock_remove.assert_any_call(WORKER_ID)
        mock_remove.assert_any_call(CHILD_ID)

    @patch("cli_agent_orchestrator.services.fleet_labels.remove_label")
    @patch("cli_agent_orchestrator.mcp_server.server.cao_http")
    def test_delete_removes_label_no_reaped_key(self, mock_http, mock_remove, monkeypatch):
        """When response has no reaped list, fall back to removing the target id."""
        from cli_agent_orchestrator.mcp_server.server import delete_terminal

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"success": True}
        mock_http.delete.return_value = response

        result = delete_terminal(WORKER_ID)

        assert result.get("success") is True
        mock_remove.assert_called_once_with(WORKER_ID)


class TestUpdateTaskLabel:
    """Test the update_task_label MCP tool."""

    @patch("cli_agent_orchestrator.services.fleet_labels.upsert_label")
    def test_update_task_label(self, mock_upsert, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import update_task_label

        monkeypatch.setenv("CAO_TERMINAL_ID", SUPERVISOR_ID)

        import asyncio

        result = asyncio.run(update_task_label(WORKER_ID, "new label"))

        assert result["success"] is True
        assert result["terminal_id"] == WORKER_ID
        assert result["task_label"] == "new label"
        mock_upsert.assert_called_once_with(WORKER_ID, "new label")

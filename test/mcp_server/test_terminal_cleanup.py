"""Tests for fleet/delete MCP tools and _get_cleanup_nudge helper."""

import asyncio
import os
from unittest.mock import ANY, MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import (
    _current_terminal_id,
    _get_cleanup_nudge,
    _get_terminal_context_from_env,
    _peek_terminal_impl,
    delete_terminal,
    fleet,
)


class TestCurrentTerminalId:
    def test_empty_terminal_id_is_treated_as_unset(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": ""}):
            assert _current_terminal_id() is None


class TestGetCleanupNudge:
    def test_returns_empty_when_no_terminal_id_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_terminal_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                mock_get.return_value.status_code = 500
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_no_session_name(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {}  # no session_name
                mock_get.return_value = mock_resp
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_sessions_fetch_fails(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 500
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_empty_when_below_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 200
                sessions_resp.json.return_value = [{}] * 5  # below threshold of 10
                mock_get.side_effect = [terminal_resp, sessions_resp]
                assert _get_cleanup_nudge() == ""

    def test_returns_nudge_when_at_threshold(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                terminal_resp = MagicMock()
                terminal_resp.status_code = 200
                terminal_resp.json.return_value = {"session_name": "cao-test"}
                sessions_resp = MagicMock()
                sessions_resp.status_code = 200
                sessions_resp.json.return_value = [{}] * 10  # at threshold
                mock_get.side_effect = [terminal_resp, sessions_resp]
                nudge = _get_cleanup_nudge()
                assert "10 terminals" in nudge
                assert "delete_terminal" in nudge

    def test_returns_empty_on_exception(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get",
                side_effect=Exception("network error"),
            ):
                assert _get_cleanup_nudge() == ""

    def test_skips_lookup_for_malformed_terminal_id(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                assert _get_cleanup_nudge() == ""
        mock_get.assert_not_called()


class TestMemoryTerminalContext:
    def test_malformed_terminal_id_degrades_without_lookup(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "supervisor-abc123"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                assert _get_terminal_context_from_env() is None
        mock_get.assert_not_called()


class TestDeleteTerminal:
    @patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
    def test_refuses_ready_base_owner(self, mock_delete):
        error = requests.HTTPError("409")
        error.response = MagicMock(status_code=409)
        mock_delete.return_value.raise_for_status.side_effect = error
        result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
    def test_refuses_protected_profile(self, mock_delete):
        error = requests.HTTPError("409")
        error.response = MagicMock(status_code=409)
        mock_delete.return_value.raise_for_status.side_effect = error
        result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    @patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
    def test_force_overrides_protection(self, mock_delete):
        mock_delete.return_value.raise_for_status.return_value = None
        mock_delete.return_value.json.return_value = {
            "success": True,
            "reaped": [{"id": "t1", "status": "reaped"}],
            "skipped": [],
            "uncertain": [],
            "unattempted": [],
        }
        result = delete_terminal("t1", force=True)
        assert result["success"] is True
        mock_delete.assert_called_once()
        assert mock_delete.call_args.kwargs["params"] == {"force": True, "orphan": False}

    def test_success(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            mock_delete.return_value.json.return_value = {
                "success": True,
                "reaped": [{"id": "t1", "status": "reaped"}],
                "skipped": [],
                "uncertain": [],
                "unattempted": [],
            }
            result = delete_terminal("t1")
        assert result["success"] is True
        assert result["reaped"] == [{"id": "t1", "status": "reaped"}]

    def test_orphan_true_is_forwarded(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            mock_delete.return_value.raise_for_status.return_value = None
            mock_delete.return_value.json.return_value = {
                "success": True,
                "reaped": [{"id": "t1", "status": "reaped"}],
                "skipped": [{"id": "child", "reason": "orphan_requested"}],
                "uncertain": [],
                "unattempted": [],
            }

            result = delete_terminal("t1", orphan=True)

        assert result["success"] is True
        assert mock_delete.call_args.kwargs["params"] == {"force": False, "orphan": True}

    def test_not_found_returns_false(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            http_err = requests.HTTPError()
            http_err.response = MagicMock()
            http_err.response.status_code = 404
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "not found" in result["message"]


class TestFleet:
    def test_returns_one_serialized_fleet_envelope_with_orphan_and_depth(self):
        payload = {
            "session_name": "cao-test",
            "terminals": [
                {
                    "id": "child",
                    "profile": "developer",
                    "provider": "codex",
                    "window_index": "2",
                    "window_name": "worker",
                    "parent_id": "deadbeef",
                    "depth": 2,
                    "orphan": True,
                    "status": "idle",
                    "since_last_input": 1.0,
                    "lifecycle": "ephemeral",
                    "reparented_from": None,
                }
            ],
        }
        response = MagicMock()
        response.json.return_value = payload
        with patch(
            "cli_agent_orchestrator.mcp_server.server.cao_http.get",
            return_value=response,
        ) as get:
            result = asyncio.run(fleet("cao-test"))

        assert result["success"] is True
        assert result["fleet"]["terminals"][0]["orphan"] is True
        assert result["fleet"]["terminals"][0]["depth"] == 2
        get.assert_called_once_with("/sessions/cao-test/fleet", timeout=ANY)

    def test_http_error_non_404(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.delete") as mock_delete:
            http_err = requests.HTTPError("500 Server Error")
            http_err.response = MagicMock()
            http_err.response.status_code = 500
            mock_delete.return_value.raise_for_status.side_effect = http_err
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]

    def test_generic_exception(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server.requests.delete",
            side_effect=Exception("connection refused"),
        ):
            result = delete_terminal("t1")
        assert result["success"] is False
        assert "Failed" in result["message"]


class TestPeekTerminal:
    def test_success_caps_lines_and_returns_output(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "terminal_id": "t1",
                "lines": 200,
                "output": "pane tail",
            }
            mock_get.return_value = response

            result = _peek_terminal_impl("t1", lines=999)

        assert result == {
            "success": True,
            "terminal_id": "t1",
            "lines": 200,
            "output": "pane tail",
        }
        assert mock_get.call_args.kwargs["params"] == {"lines": 200}

    def test_http_error_returns_structured_error(self):
        with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
            http_err = requests.HTTPError("404")
            http_err.response = MagicMock()
            http_err.response.json.return_value = {"detail": "Terminal 't1' not found"}
            response = MagicMock()
            response.raise_for_status.side_effect = http_err
            mock_get.return_value = response

            result = _peek_terminal_impl("t1")

        assert result["success"] is False
        assert result["terminal_id"] == "t1"
        assert "not found" in result["error"]

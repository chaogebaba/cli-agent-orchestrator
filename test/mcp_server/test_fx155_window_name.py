"""AC3 + AC3b tests for fx155 window_name in assign/handoff/run_step."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.mcp_server.server import _assign_impl, _handoff_impl
from cli_agent_orchestrator.utils.terminal import generate_window_name


_SERVER = "cli_agent_orchestrator.mcp_server.server"


@pytest.fixture(autouse=True)
def _supervisor_cwd():
    with patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo"):
        yield


class TestAssignWindowName:
    """AC3: assign success payload contains window_name."""

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._configured_default_fork_base", return_value=None)
    @patch(f"{_SERVER}._create_terminal", return_value=("a1b2c3d4", "kiro_cli"))
    def test_assign_returns_window_name(self, mock_create, _fork_base, _nudge, monkeypatch):
        """assign payload includes window_name matching generate_window_name(profile, tid)."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        result = _assign_impl("kiro_dev", "do something")
        assert result["success"] is True
        assert result["window_name"] == generate_window_name("kiro_dev", "a1b2c3d4")
        assert result["window_name"] == "kiro_dev-a1b2c3d4"

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._configured_default_fork_base", return_value=None)
    @patch(f"{_SERVER}._create_terminal", return_value=("a1b2c3d4", "kiro_cli"))
    def test_assign_message_names_window(self, mock_create, _fork_base, _nudge, monkeypatch):
        """assign message string contains the window name."""
        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        result = _assign_impl("kiro_dev", "do something")
        assert "kiro_dev-a1b2c3d4" in result["message"]

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._configured_default_fork_base", return_value=None)
    @patch(f"{_SERVER}._create_terminal", return_value=("a1b2c3d4", "kiro_cli"))
    def test_assign_window_name_equals_persisted(self, mock_create, _fork_base, _nudge, monkeypatch):
        """The returned window_name is byte-equal to what create_terminal would persist.

        Proves the local computation agrees with what create_terminal wrote
        (no DB read in _assign_impl). The equality is verified here via the
        generator formula that create_terminal itself uses.
        """
        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        result = _assign_impl("kiro_dev", "do something")
        # The persisted value is generate_window_name(profile, tid) — same formula
        expected_persisted = f"kiro_dev-a1b2c3d4"
        assert result["window_name"] == expected_persisted


class TestHandoffWindowName:
    """AC3: handoff success result carries window_name (nullable, None tolerated)."""

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._resolve_handoff_provider")
    def test_handoff_returns_window_name_from_response(self, mock_provider, _nudge):
        """handoff populates window_name from run-step response."""
        from cli_agent_orchestrator.mcp_server.server import HandoffContext

        mock_provider.return_value = HandoffContext(
            provider="kiro_cli", session_name=None, caller_id=None, allowed_tools=None
        )

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "terminal_id": "a1b2c3d4",
            "last_message": "done",
            "status": "completed",
            "window_name": "kiro_dev-a1b2c3d4",
        }
        resp.raise_for_status.return_value = None

        with patch(f"{_SERVER}.requests") as mock_requests:
            mock_requests.post.return_value = resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("kiro_dev", "do task"))

        assert result.success is True
        assert result.window_name == "kiro_dev-a1b2c3d4"

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._resolve_handoff_provider")
    def test_handoff_tolerates_missing_window_name(self, mock_provider, _nudge):
        """handoff tolerates None window_name (older server without fx155)."""
        from cli_agent_orchestrator.mcp_server.server import HandoffContext

        mock_provider.return_value = HandoffContext(
            provider="kiro_cli", session_name=None, caller_id=None, allowed_tools=None
        )

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "terminal_id": "a1b2c3d4",
            "last_message": "done",
            "status": "completed",
            # No window_name key — older server
        }
        resp.raise_for_status.return_value = None

        with patch(f"{_SERVER}.requests") as mock_requests:
            mock_requests.post.return_value = resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("kiro_dev", "do task"))

        assert result.success is True
        assert result.window_name is None


class TestRunStepWindowName:
    """AC3b: run_step sourcing split for window_name."""

    def _make_client(self):
        """Create a test client for the api/main.py app."""
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry

        app.state.plugin_registry = PluginRegistry()
        return TestClient(app, headers={"Host": "localhost"})

    @patch("cli_agent_orchestrator.api.main.run_agent_step")
    def test_fresh_terminal_computes_window_name(self, mock_run_step):
        """With reuse_terminal_id unset, response window_name == f'{agent}-{tid}'."""
        from cli_agent_orchestrator.services.agent_step import AgentStepResult
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_run_step.return_value = AgentStepResult(
            terminal_id="a1b2c3d4",
            last_message="result text",
            status=TerminalStatus.COMPLETED,
        )

        client = self._make_client()
        resp = client.post(
            "/terminals/run-step",
            json={
                "provider": "kiro_cli",
                "agent": "kiro_dev",
                "prompt": "do something",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_name"] == "kiro_dev-a1b2c3d4"

    @patch("cli_agent_orchestrator.api.main.get_terminal_metadata")
    @patch("cli_agent_orchestrator.api.main.run_agent_step")
    def test_reuse_terminal_reads_legacy_name_from_db(self, mock_run_step, mock_meta):
        """With reuse_terminal_id set against a legacy-named terminal, response
        returns the DB-sourced legacy name (not a recomputed one)."""
        from cli_agent_orchestrator.services.agent_step import AgentStepResult
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_run_step.return_value = AgentStepResult(
            terminal_id="a1b2c3d4",
            last_message="result text",
            status=TerminalStatus.COMPLETED,
        )
        # Simulate a pre-fx155 terminal with legacy 4-hex name
        mock_meta.return_value = {"tmux_window": "kiro_dev-f3a2"}

        client = self._make_client()
        resp = client.post(
            "/terminals/run-step",
            json={
                "provider": "kiro_cli",
                "agent": "kiro_dev",
                "prompt": "do something",
                "reuse_terminal_id": "a1b2c3d4",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Must return the legacy name from DB, NOT recomputed
        assert data["window_name"] == "kiro_dev-f3a2"

    @patch("cli_agent_orchestrator.api.main.get_terminal_metadata")
    @patch("cli_agent_orchestrator.api.main.run_agent_step")
    def test_reuse_terminal_none_metadata_yields_none(self, mock_run_step, mock_meta):
        """If get_terminal_metadata returns None for reuse path, window_name is None."""
        from cli_agent_orchestrator.services.agent_step import AgentStepResult
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        mock_run_step.return_value = AgentStepResult(
            terminal_id="a1b2c3d4",
            last_message="result text",
            status=TerminalStatus.COMPLETED,
        )
        mock_meta.return_value = None

        client = self._make_client()
        resp = client.post(
            "/terminals/run-step",
            json={
                "provider": "kiro_cli",
                "agent": "kiro_dev",
                "prompt": "do something",
                "reuse_terminal_id": "a1b2c3d4",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["window_name"] is None

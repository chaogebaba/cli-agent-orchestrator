"""F172: display_name + input resolver + output render site tests."""

import asyncio
import os
import re
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.terminal import display_name, resolve_terminal_id


_SERVER = "cli_agent_orchestrator.mcp_server.server"
_UTILS = "cli_agent_orchestrator.utils.terminal"
_DB = "cli_agent_orchestrator.clients.database"


# ─── display_name() ──────────────────────────────────────────────────────────


class TestDisplayName:
    """Unit tests for the display_name helper."""

    def test_with_explicit_profile(self):
        assert display_name("a1b2c3d4", "kiro_dev") == "kiro_dev-a1b2c3d4"

    def test_with_explicit_profile_none_falls_through(self):
        """When profile is None but DB has it, it's resolved."""
        with patch(f"{_DB}.get_terminal_metadata") as mock_meta:
            mock_meta.return_value = {"agent_profile": "developer"}
            assert display_name("abcdef01") == "developer-abcdef01"

    def test_with_no_profile_and_no_db(self):
        """Falls back to bare id when DB lookup fails."""
        with patch(f"{_DB}.get_terminal_metadata", side_effect=Exception("db down")):
            assert display_name("abcdef01") == "abcdef01"

    def test_with_no_profile_and_no_metadata(self):
        """Falls back to bare id when terminal not found."""
        with patch(f"{_DB}.get_terminal_metadata", return_value=None):
            assert display_name("abcdef01") == "abcdef01"

    def test_with_no_profile_and_empty_profile_in_db(self):
        """Falls back to bare id when DB profile is empty."""
        with patch(f"{_DB}.get_terminal_metadata") as mock_meta:
            mock_meta.return_value = {"agent_profile": ""}
            assert display_name("abcdef01") == "abcdef01"


# ─── resolve_terminal_id() ───────────────────────────────────────────────────


class TestResolveTerminalId:
    """Unit tests for the input resolver."""

    def test_raw_hex_id_passthrough(self):
        """8-char hex id is returned unchanged (no DB hit)."""
        assert resolve_terminal_id("a1b2c3d4") == "a1b2c3d4"

    def test_raw_hex_id_passthrough_no_db(self):
        """Raw id never touches the database."""
        with patch(f"{_DB}.get_terminal_metadata", side_effect=AssertionError("should not call")):
            assert resolve_terminal_id("deadbeef") == "deadbeef"

    def test_display_form_resolves(self):
        """profile-id form resolves to the trailing id."""
        with patch(f"{_DB}.get_terminal_metadata") as mock_meta:
            mock_meta.return_value = {"agent_profile": "kiro_dev"}
            assert resolve_terminal_id("kiro_dev-a1b2c3d4") == "a1b2c3d4"

    def test_display_form_profile_mismatch_raises(self):
        """Mismatched profile prefix raises ValueError."""
        with patch(f"{_DB}.get_terminal_metadata") as mock_meta:
            mock_meta.return_value = {"agent_profile": "developer"}
            with pytest.raises(ValueError, match="developer"):
                resolve_terminal_id("reviewer-a1b2c3d4")

    def test_display_form_unknown_terminal_raises(self):
        """Non-existent terminal id raises ValueError."""
        with patch(f"{_DB}.get_terminal_metadata", return_value=None):
            with pytest.raises(ValueError, match="no terminal"):
                resolve_terminal_id("kiro_dev-deadbeef")

    def test_invalid_format_no_dash_passthrough(self):
        """A string without a valid hex suffix passes through."""
        assert resolve_terminal_id("invalid") == "invalid"

    def test_invalid_suffix_non_hex_passthrough(self):
        """Non-hex suffix passes through."""
        assert resolve_terminal_id("kiro_dev-ZZZZZZZZ") == "kiro_dev-ZZZZZZZZ"

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped."""
        assert resolve_terminal_id("  a1b2c3d4  ") == "a1b2c3d4"

    def test_truncated_profile_prefix_match(self):
        """Truncated profile (from 64-char limit) still resolves."""
        with patch(f"{_DB}.get_terminal_metadata") as mock_meta:
            mock_meta.return_value = {"agent_profile": "very_long_profile_name_for_testing"}
            # prefix = "very_long_profile" (shorter, but actual profile starts with it)
            assert resolve_terminal_id("very_long_profile_name_for_testing-a1b2c3d4") == "a1b2c3d4"


# ─── MCP server render site: assign result ───────────────────────────────────


@pytest.fixture(autouse=True)
def _supervisor_cwd():
    with patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo"):
        yield


class TestAssignDisplayName:
    """F172: assign result uses display_name."""

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._configured_default_fork_base", return_value=None)
    @patch(f"{_SERVER}._create_terminal", return_value=("a1b2c3d4", "kiro_cli"))
    def test_assign_result_has_display_name(self, mock_create, _fork_base, _nudge, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        result = _assign_impl("kiro_dev", "do something")
        assert result["success"] is True
        assert result["display_name"] == "kiro_dev-a1b2c3d4"
        # message leads with display form
        assert result["message"].startswith("Task assigned to kiro_dev-a1b2c3d4")

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}._configured_default_fork_base", return_value=None)
    @patch(f"{_SERVER}._create_terminal", return_value=("a1b2c3d4", "kiro_cli"))
    def test_assign_message_suggests_delete_by_display_name(
        self, mock_create, _fork_base, _nudge, monkeypatch
    ):
        from cli_agent_orchestrator.mcp_server.server import _assign_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        result = _assign_impl("kiro_dev", "do something")
        assert "delete_terminal('kiro_dev-a1b2c3d4')" in result["message"]


# ─── MCP server render site: peek_terminal input resolution ──────────────────


class TestPeekTerminalResolution:
    """F172: peek_terminal accepts display form."""

    @patch(f"{_DB}.get_terminal_metadata", return_value={"agent_profile": "developer"})
    def test_peek_resolves_display_form(self, _meta, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _peek_terminal_impl

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "terminal_id": "a1b2c3d4",
            "lines": 40,
            "output": "hello",
        }
        with patch(f"{_SERVER}.cao_http") as mock_http:
            mock_http.get.return_value = mock_response
            result = _peek_terminal_impl("developer-a1b2c3d4", lines=40)
        assert result["success"] is True
        assert result["terminal_id"] == "a1b2c3d4"
        assert result["display_name"] == "developer-a1b2c3d4"
        # Verify the HTTP call used the raw id
        mock_http.get.assert_called_once()
        call_url = mock_http.get.call_args[0][0]
        assert "/terminals/a1b2c3d4/peek" == call_url


# ─── MCP server render site: send_message injection ──────────────────────────


class TestSendMessageInjection:
    """F172: send_message injects display form in the suffix."""

    @patch(f"{_DB}.get_terminal_metadata", return_value={"agent_profile": "kiro_dev"})
    @patch(f"{_SERVER}._send_to_inbox", return_value={"success": True, "message_id": 1})
    def test_injection_uses_display_form(self, mock_send, _meta, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
        monkeypatch.setenv("CAO_ENABLE_SENDER_ID_INJECTION", "true")
        # Reload the module-level flag
        import cli_agent_orchestrator.mcp_server.server as srv
        original = srv.ENABLE_SENDER_ID_INJECTION
        srv.ENABLE_SENDER_ID_INJECTION = True
        try:
            result = _send_message_impl("deadbeef", "hello world")
        finally:
            srv.ENABLE_SENDER_ID_INJECTION = original
        # Check the message passed to _send_to_inbox
        sent_message = mock_send.call_args[0][1]
        assert "[Message from kiro_dev-a1b2c3d4 (a1b2c3d4)." in sent_message



# ─── S1: Handoff error messages use display_name ─────────────────────────────


class TestHandoffErrorDisplayName:
    """S1: handoff error messages show display_name instead of bare tid."""

    def _make_error_response(self, status_code, body):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.raise_for_status.side_effect = Exception("error")
        mock_resp.json.return_value = body
        return mock_resp

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo")
    def test_input_blocked_uses_display_name(self, _cwd, _nudge, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.json.return_value = {
            "detail": {
                "kind": "input_blocked",
                "message": "approval pending",
                "terminal_id": "a1b2c3d4",
            }
        }
        mock_resp.raise_for_status.side_effect = Exception("conflict")
        with patch(f"{_SERVER}.cao_http") as mock_http:
            mock_http.post.return_value = mock_resp
            result = asyncio.run(_handoff_impl("developer", "task", timeout=60))
        assert "developer-a1b2c3d4" in result.message
        assert "terminal a1b2c3d4" not in result.message

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo")
    def test_timeout_uses_display_name(self, _cwd, _nudge, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        mock_resp = MagicMock()
        mock_resp.status_code = 504
        mock_resp.json.return_value = {
            "detail": {
                "kind": "timeout",
                "message": "exceeded",
                "terminal_id": "b2c3d4e5",
            }
        }
        mock_resp.raise_for_status.side_effect = Exception("timeout")
        with patch(f"{_SERVER}.cao_http") as mock_http:
            mock_http.post.return_value = mock_resp
            result = asyncio.run(_handoff_impl("kiro_dev", "task", timeout=300))
        assert "kiro_dev-b2c3d4e5" in result.message
        assert "terminal b2c3d4e5" not in result.message

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo")
    def test_waiting_user_input_uses_display_name(self, _cwd, _nudge, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.json.return_value = {
            "detail": {
                "kind": "waiting_user_input",
                "message": "needs approval",
                "terminal_id": "c3d4e5f6",
            }
        }
        mock_resp.raise_for_status.side_effect = Exception("conflict")
        with patch(f"{_SERVER}.cao_http") as mock_http:
            mock_http.post.return_value = mock_resp
            result = asyncio.run(_handoff_impl("reviewer", "task", timeout=60))
        assert "reviewer-c3d4e5f6" in result.message
        assert "terminal c3d4e5f6" not in result.message


# ─── S2: HandoffResult has display_name field ────────────────────────────────


class TestHandoffResultDisplayName:
    """S2: HandoffResult model exposes display_name field."""

    def test_model_has_display_name_field(self):
        from cli_agent_orchestrator.mcp_server.models import HandoffResult

        result = HandoffResult(
            success=True,
            message="ok",
            terminal_id="a1b2c3d4",
            display_name="developer-a1b2c3d4",
        )
        assert result.display_name == "developer-a1b2c3d4"

    def test_model_display_name_optional_default_none(self):
        from cli_agent_orchestrator.mcp_server.models import HandoffResult

        result = HandoffResult(success=False, message="failed")
        assert result.display_name is None

    @patch(f"{_SERVER}._get_cleanup_nudge", return_value="")
    @patch(f"{_SERVER}.strict_supervisor_cwd", return_value="/repo")
    def test_success_handoff_populates_display_name(self, _cwd, _nudge, monkeypatch):
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl

        monkeypatch.setenv("CAO_TERMINAL_ID", "00000000")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "terminal_id": "a1b2c3d4",
            "last_message": "done",
            "window_name": "w1",
        }
        mock_resp.raise_for_status = MagicMock()
        with patch(f"{_SERVER}.cao_http") as mock_http:
            mock_http.post.return_value = mock_resp
            result = asyncio.run(_handoff_impl("kiro_dev", "task", timeout=60))
        assert result.success is True
        assert result.display_name == "kiro_dev-a1b2c3d4"

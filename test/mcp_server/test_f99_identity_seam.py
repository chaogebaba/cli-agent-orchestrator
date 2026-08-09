"""F99 seam acceptance tests (AC2 fail-closed, AC3 explicit-target no-diagnose, AC8)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import (
    _diagnose_own_404,
    _get_cleanup_nudge,
    _render_diagnosis,
    delete_terminal,
)


def _response(status_code: int = 404, body: str = "Terminal 'x' not found") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"detail": body}
    return resp


class TestRenderDiagnosis:
    def test_row_gone_renders_pass_block(self):
        text = _render_diagnosis(
            {
                "branch": "row_gone",
                "session": "s",
                "window": "w",
                "pane_pid": 4242,
                "db_matches": [],
            }
        )
        assert "[CAO identity diagnosis]" in text
        assert "GONE from the DB" in text
        assert "s:w" in text
        assert "pane_pid=4242" in text
        assert "no other row claims this window" in text
        assert "Original error follows" in text

    def test_ambiguous_lists_all_ids_never_single(self):
        text = _render_diagnosis(
            {
                "branch": "ambiguous",
                "session": "s",
                "window": "w",
                "pane_pid": 4242,
                "db_matches": ["aaaa1111", "bbbb2222"],
            }
        )
        assert "ambiguous" in text
        assert "aaaa1111" in text
        assert "bbbb2222" in text

    def test_self_proof_fail_renders_not_trusted_block(self):
        text = _render_diagnosis(
            {"branch": "self_proof_fail", "session": "s", "window": "w", "pane_pid": 99999}
        )
        assert "NOT trusted" in text
        assert "pane_pid=99999" in text
        assert "Original 404 unchanged" in text

    def test_no_pane_renders_one_line(self):
        text = _render_diagnosis({"branch": "no_pane"})
        assert "TMUX_PANE absent" in text


class TestDiagnoseOwn404:
    def test_non_404_returns_empty(self):
        with patch(
            "cli_agent_orchestrator.mcp_server.server._diagnose_own_terminal_service"
        ) as svc:
            out = _diagnose_own_404("abcd1234", _response(status_code=500))
            assert out == ""
            svc.assert_not_called()

    def test_404_invokes_service_and_renders(self):
        with (
            patch(
                "cli_agent_orchestrator.mcp_server.server._diagnose_own_terminal_service",
                return_value={"branch": "no_pane"},
            ),
        ):
            out = _diagnose_own_404("abcd1234", _response(status_code=404))
        assert "TMUX_PANE absent" in out


class TestAC3ExplicitTargetNoDiagnose:
    """A 404 on an EXPLICIT target terminal must never fire an own-id diagnosis."""

    @patch("cli_agent_orchestrator.mcp_server.server._diagnose_own_404")
    @patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
    def test_delete_terminal_404_unchanged(self, mock_delete, mock_diag):
        http_err = requests.HTTPError()
        http_err.response = MagicMock()
        http_err.response.status_code = 404
        mock_delete.return_value.raise_for_status.side_effect = http_err
        result = delete_terminal("other8888")
        assert result["success"] is False
        assert "not found" in result["message"]
        mock_diag.assert_not_called()


class TestAC8CleanupNudgeUnchanged:
    def test_cleanup_nudge_404_swallows_no_diagnosis_no_error(self):
        with patch.dict("os.environ", {"CAO_TERMINAL_ID": "abcd1234"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests.get") as mock_get:
                mock_get.return_value.status_code = 404
                assert _get_cleanup_nudge() == ""


class TestAC1PassBranchFiresThroughCallSite:
    """The incident shape: a live pane whose DB row is gone. An own-id call
    site observes 404 → the error detail carries the diagnosis block."""

    @patch("cli_agent_orchestrator.mcp_server.server._send_to_inbox")
    def test_send_message_caller_lookup_404_carries_row_gone_block(self, mock_inbox):
        from cli_agent_orchestrator.mcp_server.server import _send_message_impl

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Terminal 'abcd1234' not found"}
        http_error = requests.HTTPError("404 Client Error")
        http_error.response = mock_response
        mock_response.raise_for_status.side_effect = http_error

        with patch.dict("os.environ", {"CAO_TERMINAL_ID": "abcd1234"}):
            with patch(
                "cli_agent_orchestrator.mcp_server.server.requests.get", return_value=mock_response
            ):
                with patch(
                    "cli_agent_orchestrator.mcp_server.server._diagnose_own_404",
                    return_value="\n\n[CAO identity diagnosis] your terminal row is GONE from the DB",
                ):
                    result = _send_message_impl(None, "Results")

        assert result["success"] is False
        assert "GONE from the DB" in result["error"]
        mock_inbox.assert_not_called()

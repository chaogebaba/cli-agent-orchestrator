"""F439 (#294): MCP-side rendering of the E-TERMINAL-CAP structured error.

The server refuses over-cap assign/handoff with a 409 whose ``detail`` carries
``code="E-TERMINAL-CAP"`` plus ``current_count``, ``cap`` and
``reap_candidates``. The MCP tools must surface that reap-candidate surface to
the supervisor rather than swallowing it into a bare error string (the default
``_extract_error_detail`` returns only STRING details, so without the dedicated
handling the structured payload would be lost).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import (
    _extract_terminal_cap_detail,
    _render_terminal_cap_message,
)

CAP_DETAIL = {
    "code": "E-TERMINAL-CAP",
    "message": "worker-terminal cap reached: 10 live worker terminal(s) at cap 10.",
    "current_count": 10,
    "cap": 10,
    "reap_candidates": [
        {
            "id": "bbbbbbbb",
            "display_name": "developer-bbbbbbbb",
            "idle_since": "2026-01-01T00:00:00+00:00",
        },
        {"id": "cccccccc", "display_name": "reviewer-cccccccc", "idle_since": None},
    ],
}


def _resp(status_code: int, detail):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.json.return_value = {"detail": detail}
    return r


class TestExtractCapDetail:
    def test_recognizes_cap_detail(self):
        assert _extract_terminal_cap_detail(_resp(409, CAP_DETAIL)) == CAP_DETAIL

    def test_recognizes_cap_detail_with_extra_kind(self):
        """run-step route adds kind/terminal_id; still recognized by code."""
        d = dict(CAP_DETAIL, kind="terminal_cap", terminal_id=None)
        assert _extract_terminal_cap_detail(_resp(409, d)) == d

    def test_none_for_other_error(self):
        assert _extract_terminal_cap_detail(_resp(404, "no such terminal")) is None

    def test_none_for_other_structured_error(self):
        other = {"code": "E-SENDER-TOKEN", "message": "bad token"}
        assert _extract_terminal_cap_detail(_resp(401, other)) is None

    def test_none_for_missing_response(self):
        assert _extract_terminal_cap_detail(None) is None

    def test_none_for_non_json_body(self):
        r = MagicMock(spec=requests.Response)
        r.json.side_effect = ValueError("no json")
        assert _extract_terminal_cap_detail(r) is None


class TestRenderCapMessage:
    def test_lists_candidates(self):
        msg = _render_terminal_cap_message(CAP_DETAIL, "Assignment")
        assert "E-TERMINAL-CAP" in msg
        assert "cap 10" in msg
        assert "developer-bbbbbbbb" in msg
        assert "reviewer-cccccccc" in msg
        assert "idle since 2026-01-01T00:00:00+00:00" in msg
        assert "never auto-reaps" in msg

    def test_no_candidates_hint(self):
        detail = dict(CAP_DETAIL, reap_candidates=[])
        msg = _render_terminal_cap_message(detail, "Handoff")
        assert "No idle workers to reap" in msg
        assert "CAO_MAX_WORKER_TERMINALS" in msg


class TestAssignSurfacesCap:
    """_assign_impl renders the cap error when the create POST returns 409."""

    def test_assign_returns_cap_envelope(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CAO_TERMINAL_ID", "a1b2c3d4")
        monkeypatch.setenv("CAO_ENDPOINT", "http://127.0.0.1:19999")

        # Supervisor lookup GET succeeds.
        get_resp = MagicMock()
        get_resp.status_code = 200
        get_resp.json.return_value = {
            "id": "a1b2c3d4",
            "provider": "mock_cli",
            "session_name": "test-session",
            "status": "idle",
            "allowed_tools": None,
        }
        get_resp.raise_for_status = MagicMock()

        # Create POST returns a 409 E-TERMINAL-CAP; raise_for_status raises.
        post_resp = MagicMock(spec=requests.Response)
        post_resp.status_code = 409
        post_resp.json.return_value = {"detail": CAP_DETAIL}
        http_err = requests.HTTPError("409 Client Error")
        http_err.response = post_resp
        post_resp.raise_for_status = MagicMock(side_effect=http_err)

        with patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http:
            mock_http.get.return_value = get_resp
            mock_http.post.return_value = post_resp

            from cli_agent_orchestrator.mcp_server.server import _assign_impl

            result = _assign_impl(
                agent_profile="developer",
                message="brief",
                working_directory=str(tmp_path),
            )

        assert result["success"] is False
        assert result["terminal_id"] is None  # nothing was created
        assert result["error"]["code"] == "E-TERMINAL-CAP"
        assert result["error"]["current_count"] == 10
        assert "developer-bbbbbbbb" in result["message"]

"""F99 acceptance tests for the /terminals/by-window route (AC4, AC5)."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app


class TestByWindowReachable:
    def test_single_row_returns_200(self, client):
        with (
            patch(
                "cli_agent_orchestrator.clients.database.list_terminals_by_session",
                return_value=[
                    {
                        "id": "abcd1234",
                        "tmux_session": "s",
                        "tmux_window": "w",
                        "provider": "claude_code",
                    }
                ],
            ),
            patch(
                "cli_agent_orchestrator.api.main.terminal_service.get_terminal",
                return_value={
                    "id": "abcd1234",
                    "name": "w",
                    "provider": "claude_code",
                    "session_name": "s",
                    "agent_profile": None,
                    "caller_id": None,
                    "caller_mailbox_id": None,
                    "allowed_tools": None,
                    "provider_session_id": None,
                    "engine": None,
                    "group": None,
                    "metadata": None,
                    "status": "idle",
                    "input_gen": 0,
                    "status_gen": 0,
                    "last_active": 0,
                },
            ),
        ):
            response = client.get("/terminals/by-window", params={"session": "s", "window": "w"})
        assert response.status_code == 200
        assert response.json()["id"] == "abcd1234"

    def test_no_row_returns_404(self, client):
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[],
        ):
            response = client.get("/terminals/by-window", params={"session": "s", "window": "w"})
        assert response.status_code == 404


class TestByWindowMultiMatch:
    def test_two_rows_claiming_window_return_409_not_first_match(self, client):
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[
                {"id": "aaaa1111", "tmux_session": "s", "tmux_window": "w"},
                {"id": "bbbb2222", "tmux_session": "s", "tmux_window": "w"},
            ],
        ):
            response = client.get("/terminals/by-window", params={"session": "s", "window": "w"})
        assert response.status_code == 409
        body = response.json()["detail"]
        assert "aaaa1111" in body
        assert "bbbb2222" in body

    def test_route_not_shadowed_by_terminals_terminal_id(self, client):
        """AC4: /terminals/by-window must NOT 422-pattern-match against
        /terminals/{terminal_id} (TerminalId = ^[a-f0-9]{8}$). The literal
        route registered ABOVE the shadowing route must win."""
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[],
        ):
            response = client.get("/terminals/by-window", params={"session": "s", "window": "w"})
        assert response.status_code == 404  # 404 (no row), never 422

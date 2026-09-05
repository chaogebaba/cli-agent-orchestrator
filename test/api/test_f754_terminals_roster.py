"""F754 (#611): GET /terminals — the unscoped live roster the dispatch guard reads.

The guard has to answer "does this 8-hex id still name a terminal?" across
sessions. `/sessions/{name}/terminals` is session-scoped, so a guard built on
it would call a live cross-session id stale — the one failure mode a dispatch
guard must not have. This endpoint is the unscoped answer.
"""

from unittest.mock import patch


class TestTerminalsRoster:
    def test_returns_a_lean_projection_of_every_terminal(self, client):
        rows = [
            {
                "id": "34a7b2c1",
                "tmux_session": "cao-a",
                "tmux_window": "supervisor",
                "agent_profile": "chao_supervisor",
                "provider": "claude_code",
                "working_directory": "/home/chao/VScode_projects/cli-subagents",
                "caller_id": None,
            },
            {
                "id": "ab12cd34",
                "tmux_session": "cao-b",
                "tmux_window": "dev",
                "agent_profile": "codex_dev",
                "provider": "codex",
                "working_directory": "/tmp/wt",
                "caller_id": "34a7b2c1",
            },
        ]
        with patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=rows):
            response = client.get("/terminals")

        assert response.status_code == 200
        payload = response.json()
        assert [row["id"] for row in payload] == ["34a7b2c1", "ab12cd34"]
        # Lean on purpose: a PreToolUse hook fires this on every dispatch.
        assert set(payload[0]) == {
            "id",
            "tmux_session",
            "tmux_window",
            "agent_profile",
            "provider",
        }

    def test_is_not_shadowed_by_the_terminal_id_route(self, client):
        """Registered above /terminals/{terminal_id}, like /terminals/by-window."""
        with patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[]):
            response = client.get("/terminals")
        assert response.status_code == 200
        assert response.json() == []

    def test_database_failure_is_a_500_not_a_crash(self, client):
        with patch(
            "cli_agent_orchestrator.clients.database.list_all_terminals",
            side_effect=RuntimeError("db gone"),
        ):
            response = client.get("/terminals")
        assert response.status_code == 500
        assert "Failed to list terminals" in response.json()["detail"]

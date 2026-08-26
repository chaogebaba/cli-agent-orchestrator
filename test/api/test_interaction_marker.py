"""F507 interaction-marker endpoint: provider-agnostic + A1 tolerance (AC11, AC14)."""

from unittest.mock import patch

from cli_agent_orchestrator.services.question_state import question_state


class TestInteractionMarkerEndpoint:
    def test_404_unknown_terminal(self, client):
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None):
            response = client.post(
                "/terminals/abcd1234/interaction-marker",
                json={"terminal_id": "abcd1234", "kind": "question_open"},
            )
        assert response.status_code == 404

    def test_ac11_provider_agnostic_codex_no_claude_field(self, client):
        """AC11: endpoint accepts + stores a marker for a codex terminal."""
        question_state.forget("c0de1234")
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "c0de1234", "provider": "codex"},
        ):
            response = client.post(
                "/terminals/c0de1234/interaction-marker",
                json={
                    "terminal_id": "c0de1234",
                    "kind": "question_open",
                    "source": "PreToolUse",
                    "event": "PreToolUse",
                },
            )
        assert response.status_code == 200
        assert question_state.is_open("c0de1234") is True
        question_state.forget("c0de1234")

    def test_ac14_pre_transcript_marker_returns_200_not_400(self, client):
        """AC14: an A1 marker for a terminal with no transcript binding ⇒ 200."""
        question_state.forget("abcd1234")
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234", "provider": "claude_code"},
        ):
            response = client.post(
                "/terminals/abcd1234/interaction-marker",
                json={
                    "terminal_id": "abcd1234",
                    "kind": "question_open",
                    "source": "Notification",
                    "event": "Notification",
                },
            )
        assert response.status_code == 200
        assert question_state.is_open("abcd1234") is True
        question_state.forget("abcd1234")

    def test_terminal_id_mismatch_400(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234", "provider": "claude_code"},
        ):
            response = client.post(
                "/terminals/abcd1234/interaction-marker",
                json={"terminal_id": "other", "kind": "question_open"},
            )
        assert response.status_code == 400

    def test_invalid_kind_422(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234", "provider": "claude_code"},
        ):
            response = client.post(
                "/terminals/abcd1234/interaction-marker",
                json={"terminal_id": "abcd1234", "kind": "bogus"},
            )
        assert response.status_code == 422

    def test_clear_marker(self, client):
        question_state.forget("abcd1234")
        question_state.push_marker("abcd1234", "question_open")
        with patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "abcd1234", "provider": "claude_code"},
        ):
            response = client.post(
                "/terminals/abcd1234/interaction-marker",
                json={"terminal_id": "abcd1234", "kind": "question_clear"},
            )
        assert response.status_code == 200
        assert question_state.is_open("abcd1234") is False
        question_state.forget("abcd1234")

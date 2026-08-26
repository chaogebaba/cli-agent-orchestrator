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

    def test_malformed_terminal_id_path_param_rejected_422(self, client):
        """B1/E-MUT-C: the route param is TerminalId (^[a-f0-9]{8}$). A malformed
        id is rejected with 422 BEFORE any handler logic — so metadata is never
        looked up and no marker is stored.

        BITES: relax the endpoint signature `terminal_id: TerminalId` -> `str`
        and this returns 200/404 instead of 422.
        """
        from cli_agent_orchestrator.services.question_state import question_state

        for bad_id in ("codexterm", "ABCD1234", "abcd123", "abcd12345", "zzzzzzzz"):
            with patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata"
            ) as metadata:
                response = client.post(
                    f"/terminals/{bad_id}/interaction-marker",
                    json={"terminal_id": bad_id, "kind": "question_open"},
                )
            assert response.status_code == 422, f"{bad_id!r} should be 422, got {response.status_code}"
            # 422 is raised by route-param validation before the handler body,
            # so metadata is never consulted and nothing is stored.
            metadata.assert_not_called()
            assert question_state.is_open(bad_id) is False

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

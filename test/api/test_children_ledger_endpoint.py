"""F568 D12a children-ledger endpoint: register/release, 404/400/422 shape.

Mirrors the F507 interaction-marker endpoint contract: 404 on an unknown
terminal, 400 on a body/route id mismatch, 422 on a malformed route id or an
invalid ``op``. The mutation itself is exercised in test_children_ledger.py; here
the DB mutators are patched so the endpoint's routing/validation is under test.
"""

from unittest.mock import patch

_KNOWN = {"id": "abcd1234", "provider": "claude_code"}


class TestChildrenLedgerEndpoint:
    def test_404_unknown_terminal(self, client):
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=None):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "register", "child_id": "c1"},
            )
        assert response.status_code == 404

    def test_register_returns_count(self, client):
        with (
            patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN),
            patch("cli_agent_orchestrator.api.main.register_terminal_child", return_value=1) as reg,
        ):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "register", "child_id": "c1"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["op"] == "register"
        assert body["children_count"] == 1
        reg.assert_called_once_with("abcd1234", "c1")

    def test_release_returns_count_and_allows_missing_child_id(self, client):
        with (
            patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN),
            patch("cli_agent_orchestrator.api.main.release_terminal_child", return_value=0) as rel,
        ):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "release"},
            )
        assert response.status_code == 200
        assert response.json()["children_count"] == 0
        # D17: the endpoint now passes release_token (None here) as the 3rd arg.
        rel.assert_called_once_with("abcd1234", None, None)

    def test_register_without_child_id_is_400(self, client):
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "register"},
            )
        assert response.status_code == 400

    def test_terminal_id_mismatch_400(self, client):
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "other", "op": "register", "child_id": "c1"},
            )
        assert response.status_code == 400

    def test_invalid_op_422(self, client):
        with patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "bogus", "child_id": "c1"},
            )
        assert response.status_code == 422

    def test_vanished_terminal_between_check_and_mutation_404(self, client):
        """register returns None (row deleted mid-call) ⇒ 404, not a 500."""
        with (
            patch("cli_agent_orchestrator.api.main.get_terminal_metadata", return_value=_KNOWN),
            patch("cli_agent_orchestrator.api.main.register_terminal_child", return_value=None),
        ):
            response = client.post(
                "/terminals/abcd1234/children-ledger",
                json={"terminal_id": "abcd1234", "op": "register", "child_id": "c1"},
            )
        assert response.status_code == 404

    def test_malformed_route_id_422(self, client):
        for bad_id in ("ABCD1234", "abcd123", "zzzzzzzz"):
            with patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as metadata:
                response = client.post(
                    f"/terminals/{bad_id}/children-ledger",
                    json={"terminal_id": bad_id, "op": "register", "child_id": "c1"},
                )
            assert response.status_code == 422, f"{bad_id!r} -> {response.status_code}"
            metadata.assert_not_called()

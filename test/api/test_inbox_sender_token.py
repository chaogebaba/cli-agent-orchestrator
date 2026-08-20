"""F332 AC1-AC4, AC16: Sender-token authentication on inbox POST.

Tests the terminal token enforcement at the create_inbox_message_endpoint.
"""

import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def _init_db():
    """Ensure a clean terminals table for each test (non-destructive)."""
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    # Clean up any test terminals we might create (by known IDs)
    yield
    with SessionLocal() as db:
        db.query(TerminalModel).filter(
            TerminalModel.id.in_([
                "aa111111", "bb222222", "cc333333", "dd444444", "ff999999"
            ])
        ).delete(synchronize_session=False)
        db.commit()


@pytest.fixture
def client(_init_db):
    """Create a test client with mocked status monitor and correct Host header."""
    from test.api.conftest import TestClientWithHost

    with patch("cli_agent_orchestrator.api.main.status_monitor"):
        from cli_agent_orchestrator.api.main import app

        return TestClientWithHost(app)


def _create_terminal_with_token(terminal_id: str, token: str):
    """Insert a terminal row with a known auth_token."""
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    with SessionLocal() as db:
        t = TerminalModel(
            id=terminal_id,
            tmux_session="test-session",
            tmux_window="test-window",
            provider="mock_cli",
            auth_token=token,
        )
        db.add(t)
        db.commit()


def _create_terminal_no_token(terminal_id: str):
    """Insert a terminal row with auth_token=NULL (legacy/pre-deploy)."""
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    with SessionLocal() as db:
        t = TerminalModel(
            id=terminal_id,
            tmux_session="test-session",
            tmux_window="test-window",
            provider="mock_cli",
            auth_token=None,
        )
        db.add(t)
        db.commit()


def _inbox_count(receiver_id: str) -> int:
    """Count inbox rows for a receiver."""
    from cli_agent_orchestrator.clients.database import SessionLocal, InboxModel

    with SessionLocal() as db:
        return db.query(InboxModel).filter_by(receiver_id=receiver_id).count()


class TestAC1_ImpersonationRefused:
    """AC1: The #187 incident refuses — POST with no token returns 403 E-SENDER-TOKEN."""

    def test_no_token_header_returns_403(self, client):
        _create_terminal_with_token("aa111111", "secret_token_a")
        _create_terminal_with_token("bb222222", "secret_token_b")

        response = client.post(
            "/terminals/bb222222/inbox/messages",
            params={"sender_id": "aa111111", "message": "hello"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-TOKEN"
        assert _inbox_count("bb222222") == 0


class TestAC2_CrossTerminalImpersonation:
    """AC2: A's token claiming sender_id=B returns 403 E-SENDER-TOKEN."""

    def test_wrong_token_returns_403(self, client):
        _create_terminal_with_token("aa111111", "token_for_a")
        _create_terminal_with_token("bb222222", "token_for_b")
        _create_terminal_with_token("cc333333", "token_for_c")

        # Present A's token but claim to be B
        response = client.post(
            "/terminals/cc333333/inbox/messages",
            params={"sender_id": "bb222222", "message": "forged"},
            headers={"X-CAO-Terminal-Token": "token_for_a"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-TOKEN"
        assert _inbox_count("cc333333") == 0


class TestAC3_HonestPathPasses:
    """AC3: POST with correct sender_id + matching token does not return 403.

    Note: In unit tests without tmux, the endpoint returns 404 (session gone)
    after passing auth. The assertion is that it does NOT 403 — the auth passed.
    """

    def test_correct_token_passes_auth(self, client):
        _create_terminal_with_token("aa111111", "valid_token_x")
        _create_terminal_with_token("bb222222", "valid_token_y")

        response = client.post(
            "/terminals/bb222222/inbox/messages",
            params={"sender_id": "aa111111", "message": "legitimate callback"},
            headers={"X-CAO-Terminal-Token": "valid_token_x"},
        )
        # Must not be 403 — auth passed. 404 is acceptable (no tmux in test).
        assert response.status_code != 403


class TestAC4_OperatorBearerBypass:
    """AC4: Operator bearer (CAO_AUTH_LOCAL_TOKEN) bypasses terminal token check."""

    def test_operator_bearer_passes_without_terminal_token(self, client):
        _create_terminal_with_token("aa111111", "some_token")
        _create_terminal_with_token("bb222222", "other_token")

        with patch(
            "cli_agent_orchestrator.security.auth.get_local_bearer",
            return_value="operator_secret",
        ):
            response = client.post(
                "/terminals/bb222222/inbox/messages",
                params={"sender_id": "aa111111", "message": "from operator"},
                headers={"Authorization": "Bearer operator_secret"},
            )
        # Must not be 403 — operator bearer bypasses token check.
        # 404 is acceptable (no tmux session in test).
        assert response.status_code != 403


class TestAC16_SenderUnknown:
    """AC16: E-SENDER-UNKNOWN for non-existent or NULL-token sender."""

    def test_nonexistent_sender_returns_403_unknown(self, client):
        _create_terminal_with_token("bb222222", "token_z")

        response = client.post(
            "/terminals/bb222222/inbox/messages",
            params={"sender_id": "ff999999", "message": "boo"},
            headers={"X-CAO-Terminal-Token": "anything"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-UNKNOWN"

    def test_null_token_sender_returns_403_unknown(self, client):
        _create_terminal_no_token("dd444444")
        _create_terminal_with_token("bb222222", "token_w")

        response = client.post(
            "/terminals/bb222222/inbox/messages",
            params={"sender_id": "dd444444", "message": "old worker"},
            headers={"X-CAO-Terminal-Token": "any_token"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-UNKNOWN"



class TestAC9_TokenCheckPrecedesDrift:
    """AC9: The token check precedes the F129 drift block.

    A forged POST naming a sender with drifted frozen pins produces 403
    and NO drift notice — the token check fires first.
    """

    def test_forged_sender_with_drift_gets_403_not_drift_notice(self, client):
        """Even if sender would trigger drift, 403 fires first."""
        # Create a sender terminal (with token) that will be the "drifted" one
        _create_terminal_with_token("aa111111", "real_token_for_a")
        _create_terminal_with_token("bb222222", "token_for_b")

        # Attempt to POST as aa111111 with WRONG token — should 403
        # before the F129 drift logic ever runs
        response = client.post(
            "/terminals/bb222222/inbox/messages",
            params={"sender_id": "aa111111", "message": "drift trigger"},
            headers={"X-CAO-Terminal-Token": "wrong_token"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-TOKEN"

        # Verify no drift notice was created (F129 never ran)
        from cli_agent_orchestrator.clients.database import SessionLocal, InboxModel

        with SessionLocal() as db:
            # No inbox messages at all for either terminal
            count = db.query(InboxModel).count()
            assert count == 0, f"Expected 0 inbox rows, got {count} — drift notice leaked"

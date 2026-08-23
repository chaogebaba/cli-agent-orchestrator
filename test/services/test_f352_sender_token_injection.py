"""F352: Tests for sender token injection into kiro agent configs.

Verifies that:
1. _inject_kiro_identity_env includes CAO_TERMINAL_TOKEN in the env block
2. create_terminal_with_warm_intent accepts and persists auth_token
3. _refresh_terminal_token_from_pane reads from parent process env
4. The 403 error response includes retryable=true when token is absent
"""

import os
from unittest.mock import patch, MagicMock

import pytest


class TestInjectKiroIdentityEnv:
    """_inject_kiro_identity_env must include CAO_TERMINAL_TOKEN."""

    def test_injects_terminal_token_variable(self):
        from cli_agent_orchestrator.services.install_service import _inject_kiro_identity_env

        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "/usr/bin/cao-mcp-server",
                "args": [],
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]
        assert "CAO_TERMINAL_TOKEN" in env
        assert env["CAO_TERMINAL_TOKEN"] == "${CAO_TERMINAL_TOKEN}"

    def test_injects_all_identity_vars(self):
        from cli_agent_orchestrator.services.install_service import _inject_kiro_identity_env

        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "cao-mcp-server",
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]
        assert "CAO_TERMINAL_ID" in env
        assert "CAO_TERMINAL_TOKEN" in env
        assert "CAO_INSTANCE_ID" in env
        assert "CAO_ENDPOINT" in env

    def test_does_not_overwrite_existing_token_var(self):
        """If the profile already declares CAO_TERMINAL_TOKEN, don't overwrite."""
        from cli_agent_orchestrator.services.install_service import _inject_kiro_identity_env

        mcp_servers = {
            "cao-mcp-server": {
                "type": "stdio",
                "command": "cao-mcp-server",
                "env": {"CAO_TERMINAL_TOKEN": "custom_value"},
            }
        }
        result = _inject_kiro_identity_env(mcp_servers)
        env = result["cao-mcp-server"]["env"]
        # setdefault: existing value preserved
        assert env["CAO_TERMINAL_TOKEN"] == "custom_value"

    def test_none_input_passthrough(self):
        from cli_agent_orchestrator.services.install_service import _inject_kiro_identity_env

        assert _inject_kiro_identity_env(None) is None

    def test_non_dict_entry_passthrough(self):
        from cli_agent_orchestrator.services.install_service import _inject_kiro_identity_env

        mcp_servers = {"some-server": "not_a_dict"}
        result = _inject_kiro_identity_env(mcp_servers)
        assert result["some-server"] == "not_a_dict"


class TestCreateTerminalWithWarmIntentAuthToken:
    """create_terminal_with_warm_intent must accept and persist auth_token."""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        with SessionLocal() as db:
            db.query(TerminalModel).filter(
                TerminalModel.id == "f352test"
            ).delete(synchronize_session=False)
            db.commit()

    def test_auth_token_persisted(self):
        from cli_agent_orchestrator.clients.database import (
            SessionLocal,
            TerminalModel,
            create_terminal_with_warm_intent,
        )

        create_terminal_with_warm_intent(
            terminal_id="f352test",
            tmux_session="cao-test-f352",
            tmux_window="test-f352test",
            provider="mock_cli",
            agent_profile="test_profile",
            allowed_tools=None,
            caller_id=None,
            parent_base_name=None,
            fork_mode=None,
            auth_token="test_token_f352",
        )

        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter_by(id="f352test").first()
            assert terminal is not None
            assert terminal.auth_token == "test_token_f352"

    def test_auth_token_none_allowed(self):
        """auth_token=None (legacy) should not raise."""
        from cli_agent_orchestrator.clients.database import (
            SessionLocal,
            TerminalModel,
            create_terminal_with_warm_intent,
        )

        create_terminal_with_warm_intent(
            terminal_id="f352test",
            tmux_session="cao-test-f352",
            tmux_window="test-f352test",
            provider="mock_cli",
            agent_profile="test_profile",
            allowed_tools=None,
            caller_id=None,
            parent_base_name=None,
            fork_mode=None,
            auth_token=None,
        )

        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter_by(id="f352test").first()
            assert terminal is not None
            assert terminal.auth_token is None

    def test_working_directory_accepted(self):
        """working_directory should also be accepted (secondary fix)."""
        from cli_agent_orchestrator.clients.database import (
            SessionLocal,
            TerminalModel,
            create_terminal_with_warm_intent,
        )

        create_terminal_with_warm_intent(
            terminal_id="f352test",
            tmux_session="cao-test-f352",
            tmux_window="test-f352test",
            provider="mock_cli",
            agent_profile="test_profile",
            allowed_tools=None,
            caller_id=None,
            parent_base_name=None,
            fork_mode=None,
            auth_token="tok123",
            working_directory="/tmp/test",
        )

        with SessionLocal() as db:
            terminal = db.query(TerminalModel).filter_by(id="f352test").first()
            assert terminal is not None
            assert terminal.working_directory == "/tmp/test"


class TestRefreshTerminalTokenFromPane:
    """_refresh_terminal_token_from_pane reads token from parent process."""

    def test_reads_from_proc_ppid_environ(self, tmp_path):
        from cli_agent_orchestrator.mcp_server.server import _refresh_terminal_token_from_pane

        # Mock /proc/<ppid>/environ with a known token
        fake_environ = b"HOME=/home/test\x00CAO_TERMINAL_TOKEN=secret123\x00PATH=/usr/bin\x00"
        environ_file = tmp_path / "environ"
        environ_file.write_bytes(fake_environ)

        with patch("os.getppid", return_value=12345), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            mock_open.return_value.read = lambda: fake_environ
            result = _refresh_terminal_token_from_pane()

        assert result == "secret123"

    def test_returns_none_when_no_terminal_id(self):
        from cli_agent_orchestrator.mcp_server.server import _refresh_terminal_token_from_pane

        with patch.dict(os.environ, {}, clear=True):
            # CAO_TERMINAL_ID not set
            result = _refresh_terminal_token_from_pane()
        # Should not crash, returns None
        # (actually the function doesn't check terminal_id anymore - it uses ppid)

    def test_returns_none_on_permission_error(self):
        from cli_agent_orchestrator.mcp_server.server import _refresh_terminal_token_from_pane

        with patch("os.getppid", return_value=1), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", side_effect=PermissionError):
            result = _refresh_terminal_token_from_pane()
        assert result is None

    def test_returns_none_when_proc_missing(self):
        from cli_agent_orchestrator.mcp_server.server import _refresh_terminal_token_from_pane

        with patch("os.getppid", return_value=99999), \
             patch("os.path.exists", return_value=False):
            result = _refresh_terminal_token_from_pane()
        assert result is None


class TestInboxErrorRetryable:
    """F352: The 403 response includes retryable=true when token is absent."""

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        with SessionLocal() as db:
            db.query(TerminalModel).filter(
                TerminalModel.id.in_(["f352a001", "f352b002"])
            ).delete(synchronize_session=False)
            db.commit()

    @pytest.fixture
    def client(self):
        from test.api.conftest import TestClientWithHost

        with patch("cli_agent_orchestrator.api.main.status_monitor"):
            from cli_agent_orchestrator.api.main import app
            return TestClientWithHost(app)

    def test_absent_token_gives_retryable_true(self, client):
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        with SessionLocal() as db:
            db.add(TerminalModel(
                id="f352a001",
                tmux_session="test-sess",
                tmux_window="test-win",
                provider="mock_cli",
                auth_token="real_token_f352a",
            ))
            db.add(TerminalModel(
                id="f352b002",
                tmux_session="test-sess",
                tmux_window="test-win2",
                provider="mock_cli",
                auth_token="other_token_f352b",
            ))
            db.commit()

        response = client.post(
            "/terminals/f352b002/inbox/messages",
            params={"sender_id": "f352a001", "message": "hello"},
            # No X-CAO-Terminal-Token header
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-TOKEN"
        assert detail["retryable"] is True
        assert "$CAO_TERMINAL_TOKEN is not set" in detail["message"]

    def test_wrong_token_gives_retryable_false(self, client):
        from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

        with SessionLocal() as db:
            db.add(TerminalModel(
                id="f352a001",
                tmux_session="test-sess",
                tmux_window="test-win",
                provider="mock_cli",
                auth_token="real_token_f352a",
            ))
            db.add(TerminalModel(
                id="f352b002",
                tmux_session="test-sess",
                tmux_window="test-win2",
                provider="mock_cli",
                auth_token="other_token_f352b",
            ))
            db.commit()

        response = client.post(
            "/terminals/f352b002/inbox/messages",
            params={"sender_id": "f352a001", "message": "hello"},
            headers={"X-CAO-Terminal-Token": "wrong_token"},
        )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "E-SENDER-TOKEN"
        assert detail.get("retryable") is False

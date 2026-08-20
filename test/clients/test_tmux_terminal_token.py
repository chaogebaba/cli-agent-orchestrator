"""F332 AC13: CAO_TERMINAL_TOKEN is not overridable by operator env (extra_env).

The token is injected AFTER _merge_extra_env, so an attacker-supplied
extra_env={"CAO_TERMINAL_TOKEN": "attacker"} is overwritten by the real token.
Same property CAO_TERMINAL_ID relies on today.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestTerminalTokenNotOverridable:
    """AC13: extra_env cannot override CAO_TERMINAL_TOKEN."""

    def test_create_session_token_wins_over_extra_env(self):
        """In create_session, the issued token overwrites any attacker-supplied value."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        captured_env = {}

        class FakeServer:
            sessions = MagicMock()

            def new_session(self, **kwargs):
                captured_env.update(kwargs.get("environment", {}))
                mock_session = MagicMock()
                mock_session.name = "test-session"
                mock_window = MagicMock()
                mock_window.name = "test-window"
                mock_session.windows.get.return_value = mock_window
                # new_session returns a session
                return mock_session

        client = TmuxClient.__new__(TmuxClient)
        client.server = FakeServer()

        with patch.object(client, "_resolve_and_validate_working_directory", return_value="/tmp"):
            with patch.object(client, "_kill_via_cli"):
                try:
                    client.create_session(
                        session_name="s",
                        window_name="w",
                        terminal_id="abc123ef",
                        working_directory="/tmp",
                        extra_env={"CAO_TERMINAL_TOKEN": "attacker_value"},
                        terminal_token="real_token_issued_by_server",
                    )
                except Exception:
                    pass  # may fail on tmux interaction — we only care about env

        assert captured_env.get("CAO_TERMINAL_TOKEN") == "real_token_issued_by_server"
        assert captured_env.get("CAO_TERMINAL_ID") == "abc123ef"

    def test_create_window_token_wins_over_extra_env(self):
        """In create_window, the issued token overwrites any attacker-supplied value."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        captured_env = {}

        class FakeSession:
            def new_window(self, **kwargs):
                captured_env.update(kwargs.get("environment", {}))
                mock_window = MagicMock()
                mock_window.name = "test-window"
                return mock_window

        client = TmuxClient.__new__(TmuxClient)
        client.server = MagicMock()

        with patch.object(client, "_resolve_and_validate_working_directory", return_value="/tmp"):
            with patch.object(client, "_find_session", return_value=FakeSession()):
                result = client.create_window(
                    session_name="s",
                    window_name="w",
                    terminal_id="def456ab",
                    working_directory="/tmp",
                    extra_env={"CAO_TERMINAL_TOKEN": "attacker_value"},
                    terminal_token="real_token_issued_by_server",
                )

        assert captured_env.get("CAO_TERMINAL_TOKEN") == "real_token_issued_by_server"
        assert captured_env.get("CAO_TERMINAL_ID") == "def456ab"

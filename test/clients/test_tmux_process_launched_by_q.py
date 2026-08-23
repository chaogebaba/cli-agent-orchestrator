"""Issue #206: every spawned tmux pane exports PROCESS_LAUNCHED_BY_Q=1.

kiro-cli's zsh hook (~/.local/share/kiro-cli/shell/zshrc.pre.zsh) exec-wraps
interactive shells as "zsh (kiro-cli-term)" unless PROCESS_LAUNCHED_BY_Q is
non-empty, masking tmux pane_current_command and breaking cline_cli/grok_cli
worker status detection. The export is baked into pane env at BOTH
create_session and create_window (after _merge_extra_env, so operator
--env cannot unset it) and therefore survives tmux/server restarts —
unlike the volatile `tmux set-environment -g` fix that silently vanished.
"""

from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.clients.tmux import TmuxClient


class TestProcessLaunchedByQExported:
    """PROCESS_LAUNCHED_BY_Q=1 lands in the env of every spawned pane."""

    def test_create_session_exports_var(self):
        captured_env = {}

        class FakeServer:
            sessions = MagicMock()

            def new_session(self, **kwargs):
                captured_env.update(kwargs.get("environment", {}))
                mock_session = MagicMock()
                mock_session.windows.get.return_value = MagicMock(name="w")
                return mock_session

        client = TmuxClient.__new__(TmuxClient)
        client.server = FakeServer()

        with patch.object(
            client, "_resolve_and_validate_working_directory", return_value="/tmp"
        ):
            try:
                client.create_session(
                    session_name="s",
                    window_name="w",
                    terminal_id="abc123ef",
                    working_directory="/tmp",
                )
            except Exception:
                pass  # tmux interaction may fail — only env construction matters

        assert captured_env.get("PROCESS_LAUNCHED_BY_Q") == "1"
        assert captured_env.get("CAO_TERMINAL_ID") == "abc123ef"

    def test_create_window_exports_var(self):
        captured_env = {}

        class FakeSession:
            def new_window(self, **kwargs):
                captured_env.update(kwargs.get("environment", {}))
                return MagicMock(name="w")

        client = TmuxClient.__new__(TmuxClient)
        client.server = MagicMock()

        with patch.object(
            client, "_resolve_and_validate_working_directory", return_value="/tmp"
        ):
            with patch.object(client, "_find_session", return_value=FakeSession()):
                client.create_window(
                    session_name="s",
                    window_name="w",
                    terminal_id="def456ab",
                    working_directory="/tmp",
                )

        assert captured_env.get("PROCESS_LAUNCHED_BY_Q") == "1"
        assert captured_env.get("CAO_TERMINAL_ID") == "def456ab"

    def test_operator_cannot_unset_var(self):
        """extra_env={PROCESS_LAUNCHED_BY_Q: ""} is overwritten by CAO's "1"."""
        captured_env = {}

        class FakeSession:
            def new_window(self, **kwargs):
                captured_env.update(kwargs.get("environment", {}))
                return MagicMock(name="w")

        client = TmuxClient.__new__(TmuxClient)
        client.server = MagicMock()

        with patch.object(
            client, "_resolve_and_validate_working_directory", return_value="/tmp"
        ):
            with patch.object(client, "_find_session", return_value=FakeSession()):
                client.create_window(
                    session_name="s",
                    window_name="w",
                    terminal_id="def456ab",
                    working_directory="/tmp",
                    extra_env={"PROCESS_LAUNCHED_BY_Q": ""},
                )

        assert captured_env.get("PROCESS_LAUNCHED_BY_Q") == "1"

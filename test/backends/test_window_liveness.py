from unittest.mock import patch

from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend


def test_window_liveness_preserves_error():
    backend = TmuxBackend(client=object())
    with patch("subprocess.run") as run:
        run.return_value.returncode = 1
        run.return_value.stderr = "transport exploded"
        assert backend.window_liveness("s", "w") == "error"
        run.return_value.stderr = "can't find session: s"
        assert backend.window_liveness("s", "w") == "gone"
        run.return_value.returncode = 0
        run.return_value.stdout = "w\n"
        assert backend.window_liveness("s", "w") == "live"

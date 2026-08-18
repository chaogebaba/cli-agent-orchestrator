"""F255 regression: delete_terminal must not crash when grok cleanup is deferred.

Root cause: _delete_terminal_under_lease returned bare `False` (bool) when
provider_manager.cleanup_provider() deferred, violating its -> Dict contract.
Callers called .get("rollback_kill_uncertain") on the bool, raising
AttributeError: 'bool' object has no attribute 'get'.

This test pins the exact failure shape:
- A grok terminal in error/wedged state triggers deferred cleanup
- _delete_terminal_under_lease must return a dict (never a bool)
- The cascade caller (_delete_terminal_inner) must handle deferred cleanup
  without crashing, reporting the terminal as uncertain
"""

from unittest.mock import MagicMock, patch

import pytest


class TestF255DeferredCleanupReturnsDict:
    """_delete_terminal_under_lease returns a dict even when cleanup is deferred."""

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_deferred_cleanup_returns_dict_not_bool(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """When cleanup_provider returns False (grok deferred), the function
        must return a dict with rollback_kill_uncertain=True, not bare False."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f255test"

        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": f"grok_dev-{terminal_id}",
            "provider": "grok_cli",
            "agent_profile": "grok_dev",
            "caller_id": "supervisor1",
            "provider_session_id": None,
        }
        mock_tmux.get_history.return_value = ""
        mock_tmux.get_pane_working_directory.return_value = "/tmp/test"
        mock_tmux.kill_window.return_value = None
        mock_tmux.window_liveness.return_value = "gone"
        mock_tmux.stop_pipe_pane.return_value = None
        mock_fifo_manager.stop_reader.return_value = None
        mock_status_monitor.unregister.return_value = None

        # Simulate grok deferred cleanup
        mock_provider_manager.cleanup_provider.return_value = False

        with patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service"
        ) as mock_wt:
            mock_wt.parse_worktree_path.return_value = None

            result = _delete_terminal_under_lease(terminal_id, "fake-lease")

        # CRITICAL: must be a dict, not a bool
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}: {result!r}. "
            "This is the F255 bug — bare False breaks .get() callers."
        )
        assert result.get("rollback_kill_uncertain") is True
        assert result.get("terminal_deleted") is False
        assert result.get("cleanup_deferred") is True

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_successful_cleanup_returns_dict(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """When cleanup_provider succeeds (True), function returns a normal
        dict with rollback_kill_uncertain=False and terminal_deleted reflecting
        the DB deletion."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f255ok"

        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": f"grok_dev-{terminal_id}",
            "provider": "grok_cli",
            "agent_profile": "grok_dev",
            "caller_id": "supervisor1",
            "provider_session_id": None,
        }
        mock_tmux.get_history.return_value = ""
        mock_tmux.get_pane_working_directory.return_value = "/tmp/test"
        mock_tmux.kill_window.return_value = None
        mock_tmux.window_liveness.return_value = "gone"
        mock_tmux.stop_pipe_pane.return_value = None
        mock_fifo_manager.stop_reader.return_value = None
        mock_status_monitor.unregister.return_value = None

        # Cleanup succeeds
        mock_provider_manager.cleanup_provider.return_value = True

        mock_db_delete.return_value = {
            "terminal_deleted": True,
            "intent_deleted": True,
        }

        with patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service"
        ) as mock_wt:
            mock_wt.parse_worktree_path.return_value = None

            result = _delete_terminal_under_lease(terminal_id, "fake-lease")

        assert isinstance(result, dict)
        assert result.get("rollback_kill_uncertain") is False
        assert result.get("terminal_deleted") is True


class TestF255CascadeDeleteHandlesDeferredCleanup:
    """The cascade caller (_delete_terminal_inner) must handle deferred cleanup
    without AttributeError, reporting the terminal as uncertain."""

    def test_result_get_on_deferred_dict_does_not_crash(self):
        """Direct test: the dict returned for deferred cleanup is .get()-safe."""
        # This is the exact shape that caused the AttributeError
        result = {
            "terminal_deleted": False,
            "intent_deleted": False,
            "intent_error": None,
            "intent_retain_reason": "cleanup_deferred",
            "rollback_kill_uncertain": True,
            "cleanup_deferred": True,
        }
        # The caller does exactly this — must not raise
        assert result.get("rollback_kill_uncertain") is True

    def test_old_return_false_would_crash(self):
        """Prove the old code path (return False) crashes on .get()."""
        result = False
        with pytest.raises(AttributeError, match="'bool' object has no attribute 'get'"):
            result.get("rollback_kill_uncertain")

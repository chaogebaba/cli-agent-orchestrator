"""F296: cleanup_deferred must not produce rollback_kill_uncertain zombies.

Root cause: F255 fix returned rollback_kill_uncertain=True for the
cleanup_deferred path, but the window kill WAS confirmed. This conflation
stops the cascade and leaves zombie DB rows.

Fix: cleanup_deferred returns rollback_kill_uncertain=False. The cascade
caller treats it as a non-blocking continuation (window dead, safe to
proceed). purge_stale_terminal_records retries cleanup_provider before
DB row deletion.

Tests cover:
- Path B: cleanup_deferred returns rollback_kill_uncertain=False
- Path A: true kill-uncertain still returns rollback_kill_uncertain=True
- Cascade caller: cleanup_deferred does NOT stop cascade
- Cascade caller: true uncertain DOES stop cascade
- purge_stale_terminal_records retries cleanup_provider
"""

from unittest.mock import MagicMock, patch, call

import pytest


class TestF296CleanupDeferredNotUncertain:
    """_delete_terminal_under_lease distinguishes cleanup_deferred from kill-uncertain."""

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_path_b_cleanup_deferred_not_uncertain(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """Path B: window confirmed gone + cleanup deferred → rollback_kill_uncertain=False."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f296pathb"

        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": f"grok_tester-{terminal_id}",
            "provider": "grok_cli",
            "agent_profile": "grok_tester",
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

        # Simulate grok deferred cleanup (processes still active)
        mock_provider_manager.cleanup_provider.return_value = False

        with patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service"
        ) as mock_wt:
            mock_wt.parse_worktree_path.return_value = None

            result = _delete_terminal_under_lease(terminal_id, "fake-lease")

        assert isinstance(result, dict)
        assert result["rollback_kill_uncertain"] is False, (
            "cleanup_deferred must NOT set rollback_kill_uncertain=True; "
            "the window kill was confirmed dead"
        )
        assert result["cleanup_deferred"] is True
        assert result["terminal_deleted"] is False

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_path_a_true_uncertain_still_quarantines(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_validate_lease,
        mock_db_delete,
    ):
        """Path A: window_liveness returns 'error' after kill → rollback_kill_uncertain=True."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        terminal_id = "f296patha"

        mock_get_metadata.return_value = {
            "id": terminal_id,
            "tmux_session": "cao-test",
            "tmux_window": f"grok_tester-{terminal_id}",
            "provider": "grok_cli",
            "agent_profile": "grok_tester",
            "caller_id": "supervisor1",
            "provider_session_id": "some-uuid",
        }
        mock_tmux.get_history.return_value = ""
        mock_tmux.get_pane_working_directory.return_value = "/tmp/test"
        mock_tmux.kill_window.return_value = None
        # Window liveness returns "error" — kill uncertain
        mock_tmux.window_liveness.return_value = "error"
        mock_tmux.stop_pipe_pane.return_value = None
        mock_fifo_manager.stop_reader.return_value = None
        mock_status_monitor.unregister.return_value = None

        with patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service"
        ) as mock_wt, patch(
            "cli_agent_orchestrator.clients.database.quarantine_terminal_owner",
            return_value="associated",
        ) as mock_quarantine:
            mock_wt.parse_worktree_path.return_value = None

            result = _delete_terminal_under_lease(
                terminal_id,
                "fake-lease",
                require_confirmed_death=True,
                quarantine_session_uuid="some-uuid",
            )

        assert isinstance(result, dict)
        assert result["rollback_kill_uncertain"] is True, (
            "True kill-uncertain (window_liveness != 'gone') must still quarantine"
        )
        assert result.get("cleanup_deferred") is not True
        mock_quarantine.assert_called_once_with(
            terminal_id, "some-uuid", "rollback_kill_uncertain"
        )


class TestF296CascadeHandlesCleanupDeferred:
    """The cascade caller continues past cleanup_deferred nodes."""

    def test_cleanup_deferred_does_not_stop_cascade(self):
        """Verify cascade continues when a node returns cleanup_deferred=True."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_inner,
        )
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        terminal_id = "f296root"
        child1_id = "f296ch1"
        child2_id = "f296ch2"

        terminals = [
            {
                "id": terminal_id,
                "tmux_session": "cao-test",
                "tmux_window": f"root-{terminal_id}",
                "caller_id": None,
                "provider_session_id": None,
                "agent_profile": "developer",
                "provider": "claude_code",
            },
            {
                "id": child1_id,
                "tmux_session": "cao-test",
                "tmux_window": f"grok_tester-{child1_id}",
                "caller_id": terminal_id,
                "provider_session_id": "uuid-ch1",
                "agent_profile": "grok_tester",
                "provider": "grok_cli",
            },
            {
                "id": child2_id,
                "tmux_session": "cao-test",
                "tmux_window": f"grok_tester-{child2_id}",
                "caller_id": terminal_id,
                "provider_session_id": "uuid-ch2",
                "agent_profile": "grok_tester",
                "provider": "grok_cli",
            },
        ]

        # child1 returns cleanup_deferred, child2 succeeds normally
        def fake_delete_under_lease(tid, token, **kwargs):
            if tid == child1_id:
                return {
                    "terminal_deleted": False,
                    "intent_deleted": False,
                    "intent_error": None,
                    "intent_retain_reason": "cleanup_deferred",
                    "rollback_kill_uncertain": False,
                    "cleanup_deferred": True,
                }
            return {
                "terminal_deleted": True,
                "intent_deleted": True,
                "intent_error": None,
                "intent_retain_reason": None,
                "rollback_kill_uncertain": False,
            }

        boundary_obs = MagicMock()
        boundary_obs.status = TerminalStatus.IDLE

        with patch(
            "cli_agent_orchestrator.services.terminal_service.list_terminals_by_session",
            return_value=terminals,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service._quiesce_cascade_subtree_pre_plan"
        ), patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            return_value="lifecycle-lease",
        ), patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        ), patch(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            return_value="rebind-token",
        ), patch(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.status_monitor"
        ) as mock_sm, patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease",
            side_effect=fake_delete_under_lease,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.has_deferred_init",
            return_value=False,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend"
        ) as mock_backend:
            mock_sm.get_boundary_observation.return_value = boundary_obs
            mock_backend.return_value = MagicMock(set_window_parent=None)

            result = _delete_terminal_inner(
                terminal_id=terminal_id,
                session_name="cao-test",
                root=terminals[0],
                registry=None,
                force=False,
                orphan=False,
                caller_id=None,
            )

        # Cascade must proceed past child1's cleanup_deferred
        assert result["uncertain"] == [], (
            "cleanup_deferred must NOT produce uncertain entries"
        )
        assert result["unattempted"] == [], (
            "cascade must NOT stop at a cleanup_deferred node"
        )
        # Both children should appear in reaped
        reaped_ids = [r["id"] for r in result["reaped"]]
        assert child1_id in reaped_ids
        assert child2_id in reaped_ids
        # child1 should have status "cleanup_deferred"
        ch1_entry = next(r for r in result["reaped"] if r["id"] == child1_id)
        assert ch1_entry["status"] == "cleanup_deferred"

    def test_true_uncertain_still_stops_cascade(self):
        """Verify cascade still stops when a node returns rollback_kill_uncertain=True."""
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_inner,
        )
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        terminal_id = "f296root2"
        child1_id = "f296aunc"
        child2_id = "f296bskp"

        terminals = [
            {
                "id": terminal_id,
                "tmux_session": "cao-test",
                "tmux_window": f"root-{terminal_id}",
                "caller_id": None,
                "provider_session_id": None,
                "agent_profile": "developer",
                "provider": "claude_code",
            },
            {
                "id": child1_id,
                "tmux_session": "cao-test",
                "tmux_window": f"grok_tester-{child1_id}",
                "caller_id": terminal_id,
                "provider_session_id": "uuid-unch",
                "agent_profile": "grok_tester",
                "provider": "grok_cli",
            },
            {
                "id": child2_id,
                "tmux_session": "cao-test",
                "tmux_window": f"grok_tester-{child2_id}",
                "caller_id": terminal_id,
                "provider_session_id": "uuid-bskp",
                "agent_profile": "grok_tester",
                "provider": "grok_cli",
            },
        ]

        def fake_delete_under_lease(tid, token, **kwargs):
            if tid == child1_id:
                return {
                    "terminal_deleted": False,
                    "intent_deleted": False,
                    "intent_error": None,
                    "intent_retain_reason": None,
                    "rollback_kill_uncertain": True,
                }
            return {
                "terminal_deleted": True,
                "intent_deleted": True,
                "intent_error": None,
                "intent_retain_reason": None,
                "rollback_kill_uncertain": False,
            }

        boundary_obs = MagicMock()
        boundary_obs.status = TerminalStatus.IDLE

        with patch(
            "cli_agent_orchestrator.services.terminal_service.list_terminals_by_session",
            return_value=terminals,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service._quiesce_cascade_subtree_pre_plan"
        ), patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            return_value="lifecycle-lease",
        ), patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        ), patch(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            return_value="rebind-token",
        ), patch(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.status_monitor"
        ) as mock_sm, patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease",
            side_effect=fake_delete_under_lease,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.has_deferred_init",
            return_value=False,
        ), patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend"
        ) as mock_backend:
            mock_sm.get_boundary_observation.return_value = boundary_obs
            mock_backend.return_value = MagicMock(set_window_parent=None)

            result = _delete_terminal_inner(
                terminal_id=terminal_id,
                session_name="cao-test",
                root=terminals[0],
                registry=None,
                force=False,
                orphan=False,
                caller_id=None,
            )

        # True uncertain must stop cascade
        assert len(result["uncertain"]) == 1
        assert result["uncertain"][0]["id"] == child1_id
        assert result["uncertain"][0]["reason"] == "rollback_kill_uncertain"
        # child2 should be in unattempted
        assert child2_id in result["unattempted"]


class TestF296PurgeStaleRetriesCleanup:
    """purge_stale_terminal_records retries cleanup_provider before DB delete."""

    @patch("cli_agent_orchestrator.services.terminal_service.settle_pending_orphan_messages")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch("cli_agent_orchestrator.services.terminal_service.db_list_all_terminals")
    def test_purge_calls_cleanup_provider_before_delete(
        self,
        mock_list_all,
        mock_get_backend,
        mock_provider_manager,
        mock_db_delete,
        mock_settle,
    ):
        """purge_stale_terminal_records calls cleanup_provider for stale rows."""
        from cli_agent_orchestrator.services.terminal_service import (
            purge_stale_terminal_records,
        )

        terminal_id = "f296purge"

        mock_list_all.return_value = [
            {
                "id": terminal_id,
                "tmux_session": "cao-test",
                "tmux_window": f"grok_tester-{terminal_id}",
                "provider": "grok_cli",
                "init_state": "ready",
            }
        ]

        backend = MagicMock()
        backend.window_liveness.return_value = "gone"
        backend.supports_identity_readback = True
        backend.enumerate_windows.return_value = ("ok", [])
        mock_get_backend.return_value = backend

        mock_provider_manager.cleanup_provider.return_value = True
        mock_db_delete.return_value = {"terminal_deleted": True, "intent_deleted": True}
        mock_settle.return_value = MagicMock(busy_aborted=False)

        purged = purge_stale_terminal_records()

        assert purged == 1
        # cleanup_provider must be called before DB delete
        mock_provider_manager.cleanup_provider.assert_called_once_with(terminal_id)
        mock_db_delete.assert_called_once()

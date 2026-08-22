"""Tests for F202 fork companion decisions: D9, D10/D14, D11.

AC9:  Startup FIFO re-arm — live rows get readers + pipe-pane; dead rows skip; idempotent.
AC10: Retention guard — (a) live pane past threshold NOT reclaimed, idle-since RESET;
      (b) dead pane still reclaimed.
AC11: Identity ambiguity raises structured error naming session + both incarnations.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.services.terminal_service import (
    IdentityAmbiguousError,
    purge_stale_terminal_records,
    rearm_fifo_readers_at_startup,
)


# ---------------------------------------------------------------------------
# AC11: Identity ambiguity raises structured error
# ---------------------------------------------------------------------------


class TestIdentityAmbiguousError:
    """D11: purge_identity_ambiguous raises IdentityAmbiguousError."""

    def test_raises_on_multiple_matches(self, monkeypatch):
        """When two windows claim the same terminal ID, IdentityAmbiguousError is raised."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.window_liveness.return_value = "gone"
        backend.supports_identity_readback = True
        backend.enumerate_windows.return_value = (
            "ok",
            [{"name": "win-a"}, {"name": "win-b"}],
        )
        # Both windows return the same terminal identity
        backend.read_pane_identity.return_value = SimpleNamespace(
            reason="ok", identity="term-1"
        )
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "term-1",
                    "tmux_session": "cao-sess",
                    "tmux_window": "old-win",
                    "init_state": "ready",
                }
            ],
        )

        with pytest.raises(IdentityAmbiguousError) as exc_info:
            purge_stale_terminal_records()

        err = exc_info.value
        assert err.terminal_id == "term-1"
        assert err.session == "cao-sess"
        assert set(err.incarnations) == {"win-a", "win-b"}
        # Structured message includes session and incarnations
        assert "cao-sess" in str(err)
        assert "win-a" in str(err)
        assert "win-b" in str(err)

    def test_no_error_on_single_match(self, monkeypatch):
        """A single match does NOT raise — it reconciles the rename normally."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.window_liveness.return_value = "gone"
        backend.supports_identity_readback = True
        backend.enumerate_windows.return_value = ("ok", [{"name": "new-win"}])
        backend.read_pane_identity.return_value = SimpleNamespace(
            reason="ok", identity="term-1"
        )
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "term-1",
                    "tmux_session": "cao-sess",
                    "tmux_window": "old-win",
                    "init_state": "ready",
                }
            ],
        )
        monkeypatch.setattr(terminal_service, "update_terminal_tmux_window", lambda *a: True)

        # Should not raise
        result = purge_stale_terminal_records()
        assert result == 0  # reconciled, not purged


# ---------------------------------------------------------------------------
# AC10a/10b: Retention guard
# ---------------------------------------------------------------------------


class TestRetentionSurvivorGuard:
    """D10+D14: live panes reset idle clock; dead panes still reclaimed."""

    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    def test_live_pane_not_reclaimed_and_clock_reset(
        self, mock_tlog_dir, mock_log_dir, monkeypatch
    ):
        """AC10a: a live pane past the idle retention threshold is NOT reclaimed
        and its recorded idle-since (last_active) is reset to <= threshold age."""
        from cli_agent_orchestrator.services import cleanup_service
        from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data

        mock_log_dir.exists.return_value = False
        mock_tlog_dir.exists.return_value = False

        # Terminal idle for 10 days (past 7-day threshold)
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        terminal = SimpleNamespace(
            id="live-1",
            tmux_session="cao-sess",
            tmux_window="win-live",
            last_active=old_time,
            init_state="ready",
        )

        mock_db = MagicMock()
        # First query().filter().all() = old terminals
        # Second query().filter().all() = inbox messages
        terminal_query = MagicMock()
        terminal_query.all.return_value = [terminal]
        count_query = MagicMock()
        count_query.count.return_value = 0

        # Track calls to filter — terminal cleanup uses two queries
        filter_calls = [terminal_query, count_query]
        filter_idx = [0]

        def mock_filter(*args, **kwargs):
            idx = filter_idx[0]
            filter_idx[0] += 1
            return filter_calls[idx] if idx < len(filter_calls) else MagicMock(
                all=MagicMock(return_value=[]),
                count=MagicMock(return_value=0),
                delete=MagicMock(return_value=0),
            )

        mock_db.query.return_value.filter.side_effect = mock_filter

        mock_session_cls = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(cleanup_service, "SessionLocal", mock_session_cls)

        # Backend says pane is live
        mock_backend = MagicMock()
        mock_backend.window_liveness.return_value = "live"

        mock_delete = MagicMock(return_value={"terminal_deleted": True, "intent_deleted": True})
        monkeypatch.setattr(cleanup_service, "delete_terminal_and_warm_intent", mock_delete)

        with patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=mock_backend,
        ):
            cleanup_old_data()

        # The terminal was NOT deleted
        mock_delete.assert_not_called()

        # The idle clock was RESET — last_active updated to now (within a few seconds)
        assert terminal.last_active is not old_time
        now = datetime.now(timezone.utc)
        assert (now - terminal.last_active).total_seconds() < 5

        # DB commit was called to persist the reset
        mock_db.commit.assert_called()

    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    def test_dead_pane_still_reclaimed(self, mock_tlog_dir, mock_log_dir, monkeypatch):
        """AC10b: a dead pane past the idle retention threshold IS still reclaimed."""
        from cli_agent_orchestrator.services import cleanup_service
        from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data

        mock_log_dir.exists.return_value = False
        mock_tlog_dir.exists.return_value = False

        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        terminal = SimpleNamespace(
            id="dead-1",
            tmux_session="cao-sess",
            tmux_window="win-dead",
            last_active=old_time,
            init_state="ready",
        )

        mock_db = MagicMock()
        terminal_query = MagicMock()
        terminal_query.all.return_value = [terminal]
        count_query = MagicMock()
        count_query.count.return_value = 0

        filter_calls = [terminal_query, count_query]
        filter_idx = [0]

        def mock_filter(*args, **kwargs):
            idx = filter_idx[0]
            filter_idx[0] += 1
            return filter_calls[idx] if idx < len(filter_calls) else MagicMock(
                all=MagicMock(return_value=[]),
                count=MagicMock(return_value=0),
                delete=MagicMock(return_value=0),
            )

        mock_db.query.return_value.filter.side_effect = mock_filter

        mock_session_cls = MagicMock()
        mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(cleanup_service, "SessionLocal", mock_session_cls)

        # Backend says pane is gone
        mock_backend = MagicMock()
        mock_backend.window_liveness.return_value = "gone"

        mock_delete = MagicMock(return_value={"terminal_deleted": True, "intent_deleted": True})
        monkeypatch.setattr(cleanup_service, "delete_terminal_and_warm_intent", mock_delete)

        mock_fifo = MagicMock()
        monkeypatch.setattr(cleanup_service, "fifo_manager", mock_fifo)
        mock_sm = MagicMock()
        monkeypatch.setattr(cleanup_service, "status_monitor", mock_sm)

        with patch(
            "cli_agent_orchestrator.backends.registry.get_backend",
            return_value=mock_backend,
        ):
            cleanup_old_data()

        # The terminal WAS deleted
        mock_delete.assert_called_once_with("dead-1", preserve_warm_intent=False)
        # FIFO reader stopped and status cleared before deletion
        mock_fifo.stop_reader.assert_called_once_with("dead-1")
        mock_sm.clear_terminal.assert_called_once_with("dead-1")


# ---------------------------------------------------------------------------
# AC9: Startup FIFO re-arm
# ---------------------------------------------------------------------------


class TestStartupFifoRearm:
    """D9: rearm_fifo_readers_at_startup creates readers for live rows, skips dead."""

    def test_live_rows_get_rearmed(self, monkeypatch):
        """Live terminal rows get FIFO reader created and pipe-pane re-armed."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.window_liveness.return_value = "live"
        backend.get_history.return_value = "some output"
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        mock_fifo = MagicMock()
        mock_fifo.has_reader.return_value = False
        monkeypatch.setattr(terminal_service, "fifo_manager", mock_fifo)

        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "t1",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-1",
                    "init_state": "ready",
                },
                {
                    "id": "t2",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-2",
                    "init_state": "ready",
                },
            ],
        )

        result = rearm_fifo_readers_at_startup()

        assert result["rearmed"] == 2
        assert result["skipped_gone"] == 0
        assert result["skipped_existing"] == 0

        # create_reader called for each
        assert mock_fifo.create_reader.call_count == 2
        # pipe_pane called for each
        assert backend.pipe_pane.call_count == 2

    def test_dead_rows_skipped(self, monkeypatch):
        """Rows whose window is gone get no reader or pipe-pane."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.window_liveness.return_value = "gone"
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        mock_fifo = MagicMock()
        monkeypatch.setattr(terminal_service, "fifo_manager", mock_fifo)

        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "t1",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-1",
                    "init_state": "ready",
                },
            ],
        )

        result = rearm_fifo_readers_at_startup()

        assert result["rearmed"] == 0
        assert result["skipped_gone"] == 1
        mock_fifo.create_reader.assert_not_called()
        backend.pipe_pane.assert_not_called()

    def test_idempotent_no_duplicate_readers(self, monkeypatch):
        """Re-running startup does not create duplicate readers for already-armed terminals."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.window_liveness.return_value = "live"
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        mock_fifo = MagicMock()
        # Simulate that reader already exists
        mock_fifo.has_reader.return_value = True
        monkeypatch.setattr(terminal_service, "fifo_manager", mock_fifo)

        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "t1",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-1",
                    "init_state": "ready",
                },
            ],
        )

        result = rearm_fifo_readers_at_startup()

        assert result["rearmed"] == 0
        assert result["skipped_existing"] == 1
        mock_fifo.create_reader.assert_not_called()
        backend.pipe_pane.assert_not_called()

    def test_non_ready_rows_skipped(self, monkeypatch):
        """Rows with init_state != 'ready' are skipped entirely."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False
        backend.window_liveness.return_value = "live"
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        mock_fifo = MagicMock()
        monkeypatch.setattr(terminal_service, "fifo_manager", mock_fifo)

        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "t1",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-1",
                    "init_state": "pending",
                },
            ],
        )

        result = rearm_fifo_readers_at_startup()

        assert result["rearmed"] == 0
        assert result["skipped_gone"] == 0
        assert result["skipped_existing"] == 0
        mock_fifo.create_reader.assert_not_called()
        backend.window_liveness.assert_not_called()

    def test_mixed_live_and_dead(self, monkeypatch):
        """N live rows → N rearmed; dead rows → skipped_gone."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = False

        def liveness(session, window):
            if window == "win-live":
                return "live"
            return "gone"

        backend.window_liveness.side_effect = liveness
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        mock_fifo = MagicMock()
        mock_fifo.has_reader.return_value = False
        monkeypatch.setattr(terminal_service, "fifo_manager", mock_fifo)

        monkeypatch.setattr(
            terminal_service,
            "db_list_all_terminals",
            lambda: [
                {
                    "id": "t1",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-live",
                    "init_state": "ready",
                },
                {
                    "id": "t2",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-dead",
                    "init_state": "ready",
                },
                {
                    "id": "t3",
                    "tmux_session": "sess-1",
                    "tmux_window": "win-live",
                    "init_state": "ready",
                },
            ],
        )

        result = rearm_fifo_readers_at_startup()

        assert result["rearmed"] == 2
        assert result["skipped_gone"] == 1
        assert mock_fifo.create_reader.call_count == 2
        assert backend.pipe_pane.call_count == 2

    def test_event_inbox_backend_skips_all(self, monkeypatch):
        """Event-inbox backends (herdr) skip all FIFO operations."""
        from cli_agent_orchestrator.services import terminal_service

        backend = MagicMock()
        backend.supports_event_inbox.return_value = True
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        result = rearm_fifo_readers_at_startup()

        assert result == {"rearmed": 0, "skipped_gone": 0, "skipped_existing": 0}
        backend.window_liveness.assert_not_called()

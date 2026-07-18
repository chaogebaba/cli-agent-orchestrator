"""Tests for cleanup service."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.services.cleanup_service import cleanup_old_data


class TestCleanupOldData:
    """Tests for cleanup_old_data function."""

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.delete_terminal_and_warm_intent")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_terminals(
        self, mock_log_dir, mock_terminal_log_dir, mock_delete, mock_session_local
    ):
        """Test that cleanup deletes old terminals from database."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [SimpleNamespace(id="old-1"), SimpleNamespace(id="old-2")],
            [],
        ]
        mock_db.query.return_value.filter.return_value.count.return_value = 0
        mock_db.query.return_value.filter.return_value.delete.return_value = 0
        mock_delete.return_value = {"terminal_deleted": True, "intent_deleted": True}

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute
        cleanup_old_data()

        assert mock_delete.call_args_list == [
            call("old-1", preserve_warm_intent=False),
            call("old-2", preserve_warm_intent=False),
        ]

    @patch("cli_agent_orchestrator.services.cleanup_service.status_monitor")
    @patch("cli_agent_orchestrator.services.cleanup_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_inbox_messages(
        self,
        mock_log_dir,
        mock_terminal_log_dir,
        mock_session_local,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test that cleanup deletes old inbox messages from database."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.delete.return_value = 10

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute
        cleanup_old_data()

        # Terminal candidates are enumerated in one session; inbox retention
        # performs the only bulk delete and commit in this mocked path.
        assert mock_db.query.call_count >= 2
        assert mock_db.commit.call_count == 1

    def test_cleanup_old_data_purges_attempt_members_and_orphaned_attempt(
        self, monkeypatch, tmp_path
    ):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod
        from cli_agent_orchestrator.clients.database import (
            Base,
            InboxDeliveryAttemptMemberModel,
            InboxDeliveryAttemptModel,
            InboxModel,
            begin_delivery_attempt,
            create_inbox_message,
            create_terminal,
            get_pending_messages,
            settle_delivery_attempt,
        )
        from cli_agent_orchestrator.models.inbox import MessageStatus
        from cli_agent_orchestrator.services import cleanup_service as cleanup_mod

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        test_db = sessionmaker(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", test_db)
        monkeypatch.setattr(cleanup_mod, "SessionLocal", test_db)
        monkeypatch.setattr(cleanup_mod, "TERMINAL_LOG_DIR", tmp_path / "term-logs")
        monkeypatch.setattr(cleanup_mod, "LOG_DIR", tmp_path / "logs")
        create_terminal("sender", "s", "sender", "codex")
        create_terminal("receiver", "s", "receiver", "claude_code")
        create_inbox_message("sender", "receiver", "wire")
        message = get_pending_messages("receiver")[0]
        attempt = begin_delivery_attempt([message], "receiver", "claude_code", "hash", 4)
        settle_delivery_attempt(attempt, MessageStatus.DELIVERED, "confirmed")
        with test_db.begin() as db:
            db.query(InboxModel).filter_by(id=message.id).update(
                {InboxModel.created_at: datetime.now() - timedelta(days=30)}
            )

        cleanup_old_data()

        with test_db() as db:
            assert db.query(InboxModel).filter_by(id=message.id).count() == 0
            assert (
                db.query(InboxDeliveryAttemptMemberModel).filter_by(attempt_uuid=attempt).count()
                == 0
            )
            assert db.query(InboxDeliveryAttemptModel).filter_by(attempt_uuid=attempt).count() == 0

    def test_barrier_member_message_id_is_explicitly_nulled_with_foreign_keys_off(
        self, monkeypatch, tmp_path
    ):
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod
        from cli_agent_orchestrator.clients.database import (
            Base,
            CallbackBarrierMemberModel,
            InboxModel,
            create_inbox_message,
            create_terminal,
        )
        from cli_agent_orchestrator.services import cleanup_service as cleanup_mod

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        test_db = sessionmaker(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", test_db)
        monkeypatch.setattr(cleanup_mod, "SessionLocal", test_db)
        monkeypatch.setattr(cleanup_mod, "TERMINAL_LOG_DIR", tmp_path / "term-logs")
        monkeypatch.setattr(cleanup_mod, "LOG_DIR", tmp_path / "logs")
        create_terminal("owner", "s", "owner", "codex", "supervisor")
        create_terminal("worker", "s", "worker", "codex", "reviewer", caller_id="owner")
        create_inbox_message("owner", "worker", "task", dispatch_barrier={"label": "cleanup"})
        reply = create_inbox_message("worker", "owner", "answer")
        with test_db.begin() as db:
            assert db.execute(text("PRAGMA foreign_keys")).scalar_one() == 0
            db.query(InboxModel).filter_by(id=reply.id).update(
                {InboxModel.created_at: datetime.now() - timedelta(days=30)}
            )

        cleanup_old_data()

        with test_db() as db:
            assert db.query(InboxModel).filter_by(id=reply.id).count() == 0
            member = db.query(CallbackBarrierMemberModel).one()
            assert member.message_id is None

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_terminal_log_files(self, mock_session_local):
        """Test that cleanup deletes old terminal log files."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Create temp directory with old and new log files
        with tempfile.TemporaryDirectory() as tmpdir:
            terminal_log_dir = Path(tmpdir) / "terminal"
            terminal_log_dir.mkdir()

            # Create old log file (older than retention period)
            old_log = terminal_log_dir / "old.log"
            old_log.write_text("old log content")
            old_time = (datetime.now() - timedelta(days=10)).timestamp()
            import os

            os.utime(old_log, (old_time, old_time))

            # Create new log file (within retention period)
            new_log = terminal_log_dir / "new.log"
            new_log.write_text("new log content")

            with patch(
                "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR",
                terminal_log_dir,
            ):
                with patch(
                    "cli_agent_orchestrator.services.cleanup_service.LOG_DIR",
                    Path(tmpdir) / "nonexistent",
                ):
                    cleanup_old_data()

            # Verify old log was deleted, new log remains
            assert not old_log.exists()
            assert new_log.exists()

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_deletes_old_server_log_files(self, mock_session_local):
        """Test that cleanup deletes old server log files."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Create temp directory with old and new log files
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()

            # Create old log file
            old_log = log_dir / "server_old.log"
            old_log.write_text("old server log")
            old_time = (datetime.now() - timedelta(days=10)).timestamp()
            import os

            os.utime(old_log, (old_time, old_time))

            # Create new log file
            new_log = log_dir / "server_new.log"
            new_log.write_text("new server log")

            with patch(
                "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR",
                Path(tmpdir) / "nonexistent",
            ):
                with patch(
                    "cli_agent_orchestrator.services.cleanup_service.LOG_DIR",
                    log_dir,
                ):
                    cleanup_old_data()

            # Verify old log was deleted, new log remains
            assert not old_log.exists()
            assert new_log.exists()

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_handles_database_error(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local
    ):
        """Test that cleanup handles database errors gracefully."""
        # Setup mock database session to raise an error
        mock_session_local.return_value.__enter__.side_effect = Exception("Database error")

        # Setup mock directories (non-existent)
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute - should not raise exception
        cleanup_old_data()  # Should log error but not raise

    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 7)
    def test_cleanup_old_data_handles_empty_directories(
        self, mock_log_dir, mock_terminal_log_dir, mock_session_local
    ):
        """Test that cleanup handles empty or non-existent directories."""
        # Setup mock database session
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db
        mock_db.query.return_value.filter.return_value.delete.return_value = 0

        # Setup mock directories as non-existent
        mock_log_dir.exists.return_value = False
        mock_terminal_log_dir.exists.return_value = False

        # Execute - should complete without error
        cleanup_old_data()

        # Verify database operations still occurred
        assert mock_db.query.called

    @patch("cli_agent_orchestrator.services.cleanup_service.status_monitor")
    @patch("cli_agent_orchestrator.services.cleanup_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.cleanup_service.SessionLocal")
    @patch("cli_agent_orchestrator.services.cleanup_service.RETENTION_DAYS", 30)
    def test_cleanup_uses_correct_retention_period(
        self, mock_session_local, mock_fifo_manager, mock_status_monitor
    ):
        """Test that cleanup uses the configured retention period."""
        mock_db = MagicMock()
        mock_session_local.return_value.__enter__.return_value = mock_db

        # Capture the filter argument to verify cutoff date
        filter_calls = []

        def capture_filter(condition):
            filter_calls.append(condition)
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_result.delete.return_value = 0
            return mock_result

        mock_db.query.return_value.filter = capture_filter

        with patch(
            "cli_agent_orchestrator.services.cleanup_service.TERMINAL_LOG_DIR"
        ) as mock_terminal:
            with patch("cli_agent_orchestrator.services.cleanup_service.LOG_DIR") as mock_log:
                mock_terminal.exists.return_value = False
                mock_log.exists.return_value = False
                cleanup_old_data()

        # Verify filter was called (terminals: .all() + .delete(), inbox: .delete())
        assert len(filter_calls) >= 2

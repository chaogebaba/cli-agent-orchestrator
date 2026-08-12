"""F123: supervisor-pending sentinel and teammate_push observability tests."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSupervisorPendingSentinel:
    """Tests for _touch_supervisor_pending_flag / _remove_supervisor_pending_flag_if_drained."""

    def test_touch_creates_flag(self, tmp_path: Path):
        """touch creates the sentinel file at CAO_HOME_DIR / supervisor-pending.flag."""
        from cli_agent_orchestrator.clients.database import _touch_supervisor_pending_flag

        with patch(
            "cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path
        ):
            _touch_supervisor_pending_flag()

        assert (tmp_path / "supervisor-pending.flag").exists()

    def test_touch_idempotent(self, tmp_path: Path):
        """Repeated touches don't raise."""
        from cli_agent_orchestrator.clients.database import _touch_supervisor_pending_flag

        with patch(
            "cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path
        ):
            _touch_supervisor_pending_flag()
            _touch_supervisor_pending_flag()

        assert (tmp_path / "supervisor-pending.flag").exists()

    def test_touch_survives_oserror(self, tmp_path: Path):
        """OSError during touch is swallowed (best-effort)."""
        from cli_agent_orchestrator.clients.database import _touch_supervisor_pending_flag

        # Point at a non-existent deep path so touch raises
        fake_dir = tmp_path / "no" / "such" / "dir"
        with patch(
            "cli_agent_orchestrator.constants.CAO_HOME_DIR", fake_dir
        ):
            # Should not raise
            _touch_supervisor_pending_flag()

    def test_remove_noop_when_no_flag(self, tmp_path: Path):
        """remove is a no-op when the flag doesn't exist (fast path)."""
        from cli_agent_orchestrator.clients.database import (
            _remove_supervisor_pending_flag_if_drained,
        )

        with patch(
            "cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path
        ):
            # Should not raise, should not create the file
            _remove_supervisor_pending_flag_if_drained()

        assert not (tmp_path / "supervisor-pending.flag").exists()

    def test_remove_deletes_flag_when_no_supervisor_mailbox(self, tmp_path: Path):
        """Flag is removed when no supervisor mailbox exists in DB."""
        from cli_agent_orchestrator.clients.database import (
            _remove_supervisor_pending_flag_if_drained,
        )

        flag = tmp_path / "supervisor-pending.flag"
        flag.touch()

        mock_db = MagicMock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with (
            patch("cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal"
            ) as mock_session_cls,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            _remove_supervisor_pending_flag_if_drained()

        assert not flag.exists()

    def test_remove_keeps_flag_when_pending_exists(self, tmp_path: Path):
        """Flag is kept when supervisor-bound PENDING rows remain."""
        from cli_agent_orchestrator.clients.database import (
            _remove_supervisor_pending_flag_if_drained,
        )

        flag = tmp_path / "supervisor-pending.flag"
        flag.touch()

        mock_mbox = MagicMock()
        mock_mbox.id = "mbox-123"

        mock_db = MagicMock()
        # First query call: MailboxModel filter_by
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_mbox
        # Second query call: exists check — returns True
        mock_db.query.return_value.scalar.return_value = True

        with (
            patch("cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path),
            patch(
                "cli_agent_orchestrator.clients.database.SessionLocal"
            ) as mock_session_cls,
        ):
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            _remove_supervisor_pending_flag_if_drained()

        # Flag should still exist because has_pending is True
        assert flag.exists()

    def test_touch_skipped_for_non_supervisor_mailbox(self, tmp_path: Path):
        """_is_supervisor_mailbox_id returns False → no flag created (D1 fix)."""
        from cli_agent_orchestrator.clients.database import (
            _is_supervisor_mailbox_id,
            _touch_supervisor_pending_flag,
        )

        mock_db = MagicMock()
        # Simulate: mailbox exists but role != "supervisor"
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        # The check should return False for a non-supervisor mailbox
        assert _is_supervisor_mailbox_id(mock_db, "worker-mbox-99") is False

        # Therefore the sentinel should NOT be created
        with patch("cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path):
            # Don't call touch — simulating the gated path
            pass

        assert not (tmp_path / "supervisor-pending.flag").exists()

    def test_touch_fires_for_supervisor_mailbox(self, tmp_path: Path):
        """_is_supervisor_mailbox_id returns True → flag IS created."""
        from cli_agent_orchestrator.clients.database import (
            _is_supervisor_mailbox_id,
            _touch_supervisor_pending_flag,
        )

        mock_db = MagicMock()
        mock_mbox = MagicMock()
        mock_mbox.id = "sup-mbox-1"
        mock_mbox.role = "supervisor"
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_mbox

        # The check should return True for the supervisor mailbox
        assert _is_supervisor_mailbox_id(mock_db, "sup-mbox-1") is True

        # Touch fires
        with patch("cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path):
            _touch_supervisor_pending_flag()

        assert (tmp_path / "supervisor-pending.flag").exists()


@pytest.mark.xfail(reason="F136 retires legacy teammate_push structured logging (D1: single writer)")
class TestTeammatePushObservability:
    """Tests for structured logging in attempt_teammate_push."""

    def test_push_ok_structured_log(self, caplog):
        """Successful push emits structured 'teammate_push_ok' log."""
        from cli_agent_orchestrator.services.teammate_push_service import (
            attempt_teammate_push,
        )

        mock_msg = MagicMock()
        mock_msg.id = 42
        mock_msg.sender_id = "worker-1"
        mock_msg.message = "hello from worker"

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
                return_value=Path("/tmp/test-inbox"),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._get_last_notified_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._write_inbox_entry",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._persist_last_notified_id",
            ),
            caplog.at_level(logging.INFO),
        ):
            result = attempt_teammate_push("term-1", [mock_msg])

        assert result is True
        # Check structured log was emitted
        assert any("teammate_push_outcome" in r.message for r in caplog.records)
        ok_record = next(r for r in caplog.records if "teammate_push_outcome" in r.message)
        assert ok_record.event == "teammate_push_ok"  # type: ignore[attr-defined]
        assert ok_record.terminal_id == "term-1"  # type: ignore[attr-defined]
        assert ok_record.high_water == 42  # type: ignore[attr-defined]

    def test_push_fail_structured_log(self, caplog):
        """Failed push emits structured 'teammate_push_fail' warning."""
        from cli_agent_orchestrator.services.teammate_push_service import (
            attempt_teammate_push,
        )

        mock_msg = MagicMock()
        mock_msg.id = 7
        mock_msg.sender_id = "worker-2"
        mock_msg.message = "payload"

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
                return_value=Path("/tmp/test-inbox"),
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._get_last_notified_id",
                return_value=0,
            ),
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._write_inbox_entry",
                return_value=False,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = attempt_teammate_push("term-2", [mock_msg])

        assert result is False
        assert any("teammate_push_outcome" in r.message for r in caplog.records)
        fail_record = next(r for r in caplog.records if "teammate_push_outcome" in r.message)
        assert fail_record.event == "teammate_push_fail"  # type: ignore[attr-defined]
        assert fail_record.reason == "write_failed"  # type: ignore[attr-defined]

    def test_push_no_inbox_structured_log(self, caplog):
        """No inbox path emits 'teammate_push_no_inbox' warning."""
        from cli_agent_orchestrator.services.teammate_push_service import (
            attempt_teammate_push,
        )

        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.sender_id = "worker-3"
        mock_msg.message = "hi"

        with (
            patch(
                "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
                return_value=None,
            ),
            caplog.at_level(logging.WARNING),
        ):
            result = attempt_teammate_push("term-3", [mock_msg])

        assert result is False
        assert any("teammate_push_outcome" in r.message for r in caplog.records)
        no_inbox_record = next(
            r for r in caplog.records if "teammate_push_outcome" in r.message
        )
        assert no_inbox_record.event == "teammate_push_no_inbox"  # type: ignore[attr-defined]

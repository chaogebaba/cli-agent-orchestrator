"""F150 — Regression tests for supervisor mailbox multi-row scoping.

Covers:
- Bug A: get_current_supervisor_terminal_id with multiple rows
- Bug B: delivery runner self-heal when cc_inbox_path is NULL
- Bug family: database.py and api/main.py scoped supervisor lookups
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import mailbox_service


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f150.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()

# --- PLACEHOLDER_HELPERS ---


def _make_terminal(db: Any, terminal_id: str, session: str = "cao-orch") -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session=session,
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="code_supervisor",
            init_state="ready",
        )
    )


def _make_mailbox(
    db: Any,
    *,
    mailbox_id: str,
    session_name: str,
    terminal_id: str | None,
    generation: int = 1,
    updated_at: datetime | None = None,
    cc_inbox_path: str | None = None,
) -> MailboxModel:
    row = MailboxModel(
        id=mailbox_id,
        session_name=session_name,
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=generation,
        consumed_through_id=0,
        cc_inbox_path=cc_inbox_path,
        cc_inbox_path_version=1 if cc_inbox_path else 0,
        created_at=updated_at or datetime.now(timezone.utc),
        updated_at=updated_at or datetime.now(timezone.utc),
    )
    db.add(row)
    if terminal_id:
        db.add(
            MailboxIncarnationModel(
                mailbox_id=mailbox_id,
                generation=generation,
                terminal_id=terminal_id,
                published_at=updated_at or datetime.now(timezone.utc),
            )
        )
    return row


# ===========================================================================
# Bug A: get_current_supervisor_terminal_id — multi-row safety
# ===========================================================================


class TestGetCurrentSupervisorTerminalId:
    """Regression: must never raise MultipleResultsFound."""

    def test_single_row_returns_terminal_id(self, scratch_db):
        with scratch_db() as db:
            _make_terminal(db, "term-live")
            _make_mailbox(
                db,
                mailbox_id="mb_001",
                session_name="cao-orch1",
                terminal_id="term-live",
            )
            db.commit()

        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result == "term-live"

    def test_multiple_rows_returns_live_terminal(self, scratch_db):
        """With 4 historical mailbox rows, returns the one with a live terminal."""
        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            # Stale rows — terminals don't exist
            _make_mailbox(
                db,
                mailbox_id="mb_stale1",
                session_name="cao-orch1",
                terminal_id="term-dead1",
                updated_at=now - timedelta(hours=3),
            )
            _make_mailbox(
                db,
                mailbox_id="mb_stale2",
                session_name="cao-orch2",
                terminal_id="term-dead2",
                updated_at=now - timedelta(hours=2),
            )
            _make_mailbox(
                db,
                mailbox_id="mb_stale3",
                session_name="cao-orch3",
                terminal_id="term-dead3",
                updated_at=now - timedelta(hours=1),
            )
            # Live row — terminal exists
            _make_terminal(db, "term-live5")
            _make_mailbox(
                db,
                mailbox_id="mb_live",
                session_name="cao-orch5",
                terminal_id="term-live5",
                updated_at=now,
            )
            db.commit()

        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result == "term-live5"

    def test_multiple_rows_no_exception(self, scratch_db):
        """No MultipleResultsFound even with many supervisor rows."""
        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            for i in range(5):
                _make_mailbox(
                    db,
                    mailbox_id=f"mb_{i:03d}",
                    session_name=f"cao-orch{i}",
                    terminal_id=f"term-{i}" if i < 4 else None,
                    updated_at=now - timedelta(hours=5 - i),
                )
            # Only make one terminal exist
            _make_terminal(db, "term-2")
            db.commit()

        # Should not raise
        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result == "term-2"

    def test_no_rows_returns_none(self, scratch_db):
        with scratch_db() as db:
            db.commit()

        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result is None

    def test_all_terminals_dead_falls_back_to_most_recent(self, scratch_db):
        """When no terminal row exists, falls back to most recently updated."""
        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            _make_mailbox(
                db,
                mailbox_id="mb_old",
                session_name="cao-orch1",
                terminal_id="term-dead-old",
                updated_at=now - timedelta(hours=2),
            )
            _make_mailbox(
                db,
                mailbox_id="mb_recent",
                session_name="cao-orch5",
                terminal_id="term-dead-recent",
                updated_at=now,
            )
            db.commit()

        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result == "term-dead-recent"

    def test_null_terminal_id_rows_skipped(self, scratch_db):
        """Rows with NULL current_terminal_id are never selected."""
        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            _make_mailbox(
                db,
                mailbox_id="mb_null",
                session_name="cao-orch1",
                terminal_id=None,
                updated_at=now,
            )
            _make_terminal(db, "term-real")
            _make_mailbox(
                db,
                mailbox_id="mb_real",
                session_name="cao-orch2",
                terminal_id="term-real",
                updated_at=now - timedelta(hours=1),
            )
            db.commit()

        result = mailbox_service.get_current_supervisor_terminal_id()
        assert result == "term-real"


# ===========================================================================
# Bug B: Delivery runner self-heal for no_path
# ===========================================================================


class TestDeliveryRunnerSelfHeal:
    """F150: delivery runner re-discovers inbox path when no_path is returned."""

    def test_self_heal_populates_path_and_delivers(self, scratch_db, tmp_path):
        """When cc_inbox_path is NULL but terminal metadata has it, self-heal works."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        inbox_path = tmp_path / "inbox"
        inbox_path.mkdir()

        with scratch_db() as db:
            _make_terminal(db, "term-sup")
            _make_mailbox(
                db,
                mailbox_id="mb_sup",
                session_name="cao-heal",
                terminal_id="term-sup",
                cc_inbox_path=None,  # NULL — triggers no_path
            )
            db.commit()

        svc = InboxService()
        result = svc._f150_self_heal_inbox_path(
            mailbox_id="mb_sup",
            terminal_id="term-sup",
            generation=1,
        )

        # Without terminal metadata, self-heal returns False
        assert result is False

    def test_self_heal_with_metadata(self, scratch_db, tmp_path, monkeypatch):
        """Self-heal succeeds when terminal metadata provides cc_team_inbox_path."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        inbox_path = tmp_path / "inbox"
        inbox_path.mkdir()

        with scratch_db() as db:
            _make_terminal(db, "term-sup")
            _make_mailbox(
                db,
                mailbox_id="mb_sup",
                session_name="cao-heal",
                terminal_id="term-sup",
                cc_inbox_path=None,
            )
            db.commit()

        # Mock get_terminal_metadata to return a path
        mock_meta = {
            "metadata": {"cc_team_inbox_path": str(inbox_path)},
        }
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda tid: mock_meta,
        )

        svc = InboxService()
        result = svc._f150_self_heal_inbox_path(
            mailbox_id="mb_sup",
            terminal_id="term-sup",
            generation=1,
        )
        assert result is True

        # Verify the mailbox now has the path
        with scratch_db() as db:
            mbox = db.query(MailboxModel).filter_by(id="mb_sup").one()
            assert mbox.cc_inbox_path == str(inbox_path)

    def test_self_heal_no_metadata_returns_false(self, scratch_db, monkeypatch):
        """Self-heal returns False when no terminal metadata exists."""
        from cli_agent_orchestrator.services.inbox_service import InboxService

        with scratch_db() as db:
            _make_terminal(db, "term-sup")
            _make_mailbox(
                db,
                mailbox_id="mb_sup",
                session_name="cao-heal",
                terminal_id="term-sup",
                cc_inbox_path=None,
            )
            db.commit()

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            lambda tid: None,
        )

        svc = InboxService()
        result = svc._f150_self_heal_inbox_path(
            mailbox_id="mb_sup",
            terminal_id="term-sup",
            generation=1,
        )
        assert result is False


# ===========================================================================
# Bug B (startup): _f150_reconcile_supervisor_inbox_paths_at_startup
# ===========================================================================


class TestStartupInboxPathReconciliation:
    """F150: startup reconciles inbox paths for live supervisor mailboxes."""

    def test_reconciles_null_path_at_startup(self, scratch_db, tmp_path, monkeypatch):
        """Startup populates cc_inbox_path from terminal metadata when NULL."""
        inbox_path = tmp_path / "inbox"
        inbox_path.mkdir()

        with scratch_db() as db:
            _make_terminal(db, "term-live")
            _make_mailbox(
                db,
                mailbox_id="mb_live",
                session_name="cao-startup",
                terminal_id="term-live",
                cc_inbox_path=None,
            )
            db.commit()

        mock_meta = {"metadata": {"cc_team_inbox_path": str(inbox_path)}}
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda tid: mock_meta,
        )

        from cli_agent_orchestrator.api.main import (
            _f150_reconcile_supervisor_inbox_paths_at_startup,
        )

        asyncio.run(_f150_reconcile_supervisor_inbox_paths_at_startup())

        with scratch_db() as db:
            mbox = db.query(MailboxModel).filter_by(id="mb_live").one()
            assert mbox.cc_inbox_path == str(inbox_path)

    def test_skips_already_populated_path(self, scratch_db, tmp_path, monkeypatch):
        """Does not overwrite an existing cc_inbox_path."""
        existing_path = str(tmp_path / "existing")
        with scratch_db() as db:
            _make_terminal(db, "term-live")
            _make_mailbox(
                db,
                mailbox_id="mb_live",
                session_name="cao-startup",
                terminal_id="term-live",
                cc_inbox_path=existing_path,
            )
            db.commit()

        call_count = {"n": 0}
        original_set = None

        def mock_set(**kwargs: Any) -> Any:
            call_count["n"] += 1

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.set_supervisor_callback_inbox_path",
            mock_set,
        )

        from cli_agent_orchestrator.api.main import (
            _f150_reconcile_supervisor_inbox_paths_at_startup,
        )

        asyncio.run(_f150_reconcile_supervisor_inbox_paths_at_startup())
        # Should not have called set — path is already populated
        assert call_count["n"] == 0


# ===========================================================================
# Bug family: _remove_supervisor_pending_flag_if_drained scoping
# ===========================================================================


class TestFlagDrainScoping:
    """database.py _remove_supervisor_pending_flag_if_drained uses live supervisor."""

    def test_flag_drain_uses_live_mailbox(self, scratch_db, tmp_path, monkeypatch):
        """With multiple supervisor rows, checks pending against the live one."""
        from cli_agent_orchestrator.clients.database import (
            _remove_supervisor_pending_flag_if_drained,
        )
        from cli_agent_orchestrator import constants

        monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path)
        flag = tmp_path / "supervisor-pending.flag"
        flag.touch()

        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            # Stale mailbox (no terminal)
            _make_mailbox(
                db,
                mailbox_id="mb_stale",
                session_name="cao-old",
                terminal_id="term-dead",
                updated_at=now - timedelta(hours=2),
            )
            # Live mailbox
            _make_terminal(db, "term-live")
            _make_mailbox(
                db,
                mailbox_id="mb_live",
                session_name="cao-new",
                terminal_id="term-live",
                updated_at=now,
            )
            # No pending messages for the live mailbox
            db.commit()

        _remove_supervisor_pending_flag_if_drained()
        # Flag should be removed since no pending messages for the live mailbox
        assert not flag.exists()

    def test_flag_retained_when_live_has_pending(self, scratch_db, tmp_path, monkeypatch):
        """Flag is retained when the live mailbox still has pending messages."""
        from cli_agent_orchestrator.clients.database import (
            _remove_supervisor_pending_flag_if_drained,
            _touch_supervisor_pending_flag,
        )
        from cli_agent_orchestrator import constants

        monkeypatch.setattr(constants, "CAO_HOME_DIR", tmp_path)
        flag = tmp_path / "supervisor-pending.flag"
        flag.touch()

        now = datetime.now(timezone.utc)
        with scratch_db() as db:
            _make_terminal(db, "term-live")
            _make_mailbox(
                db,
                mailbox_id="mb_live",
                session_name="cao-new",
                terminal_id="term-live",
                updated_at=now,
            )
            # Add a pending message for the live mailbox
            db.add(
                InboxModel(
                    sender_id="worker-01",
                    receiver_id="term-live",
                    message="hello",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.PENDING.value,
                    logical_receiver_id="mb_live",
                    created_at=now,
                )
            )
            db.commit()

        _remove_supervisor_pending_flag_if_drained()
        # Flag should remain — pending messages exist
        assert flag.exists()

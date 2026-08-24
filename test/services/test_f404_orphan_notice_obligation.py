"""F404: orphan-notice insert must touch supervisor-pending sentinel AND create delivery obligation.

Regression: _record_p5_orphan_notices previously used raw db.add(InboxModel(...)), bypassing the
enqueue choke-point. Result: pending orphan-trace rows without sentinel or obligation → drain hook
fast-pathed forever → f213 re-wake storm.

This test creates a supervisor mailbox, inserts a message destined for a dead terminal, runs
settle_pending_orphan_messages, and asserts the resulting orphan-notice row has:
  1. The supervisor-pending.flag sentinel file touched
  2. A DeliveryObligationModel row with state=OPEN referencing the notice inbox row
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    settle_pending_orphan_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import mailbox_service


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f404.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    # Point CAO_HOME_DIR to tmp so sentinel goes there
    monkeypatch.setattr("cli_agent_orchestrator.constants.CAO_HOME_DIR", tmp_path)
    yield sessions
    engine.dispose()


def _setup_supervisor_and_orphan(sessions, tmp_path):
    """Create a supervisor mailbox, and a PENDING message FROM the supervisor TO a dead receiver."""
    with sessions.begin() as db:
        # Supervisor terminal
        db.add(
            TerminalModel(
                id="sup00001",
                tmux_session="cao-f404",
                tmux_window="sup00001",
                provider="codex",
                agent_profile="code_supervisor",
                init_state="ready",
            )
        )
        # Supervisor mailbox
        db.add(
            MailboxModel(
                id="mb_sup404",
                session_name="cao-f404",
                role="supervisor",
                current_terminal_id="sup00001",
                generation=1,
                consumed_through_id=0,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_sup404",
                generation=1,
                terminal_id="sup00001",
                published_at=datetime.now(timezone.utc),
            )
        )
        # Insert a PENDING message FROM the supervisor TO a dead terminal (no terminal row)
        # The orphan notice goes back to the SENDER — so it's supervisor-bound.
        db.add(
            InboxModel(
                sender_id="sup00001",
                receiver_id="dead0001",
                logical_receiver_id=None,
                message="task assignment to dead worker",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            )
        )


def test_f404_orphan_notice_creates_sentinel_and_obligation(scratch_db, tmp_path):
    """Orphan settlement notice for supervisor-bound receiver touches sentinel + creates obligation."""
    _setup_supervisor_and_orphan(scratch_db, tmp_path)

    sentinel = tmp_path / "supervisor-pending.flag"
    # Ensure sentinel does not exist before
    if sentinel.exists():
        sentinel.unlink()
    assert not sentinel.exists()

    # Run orphan settlement — this should detect the dead receiver and create a notice
    result = settle_pending_orphan_messages()

    # The orphan message should be settled
    assert result.settled_count == 1

    # Verify the orphan notice was created
    with scratch_db() as db:
        notices = (
            db.query(InboxModel)
            .filter(
                InboxModel.sender_id.startswith("message-trace:"),
                InboxModel.status == MessageStatus.PENDING.value,
            )
            .all()
        )
        assert len(notices) >= 1, "Expected at least one orphan-notice row"
        notice = notices[0]

        # The notice should target the supervisor (wrk00001's caller is sup00001, which has mailbox)
        assert notice.logical_receiver_id == "mb_sup404"

        # F404 FIX ASSERTION 1: sentinel file must exist
        assert sentinel.exists(), (
            "supervisor-pending.flag was NOT touched — F404 regression: "
            "orphan-notice bypassed sentinel"
        )

        # F404 FIX ASSERTION 2: delivery obligation must exist
        obligation = (
            db.query(DeliveryObligationModel)
            .filter_by(inbox_row_id=notice.id)
            .one_or_none()
        )
        assert obligation is not None, (
            "DeliveryObligationModel row missing for orphan-notice — F404 regression: "
            "orphan-notice bypassed obligation creation"
        )
        assert obligation.state == "OPEN"
        assert obligation.mailbox_id == "mb_sup404"


def test_f404_orphan_notice_without_fix_would_fail(scratch_db, tmp_path):
    """Counter-factual: raw db.add without choke-point would NOT create obligation.

    This test verifies the test itself is meaningful by checking that the obligation
    row is specifically tied to the notice (not some other row).
    """
    _setup_supervisor_and_orphan(scratch_db, tmp_path)

    sentinel = tmp_path / "supervisor-pending.flag"
    if sentinel.exists():
        sentinel.unlink()

    # Before settlement, no obligations should exist
    with scratch_db() as db:
        obligations = db.query(DeliveryObligationModel).all()
        assert len(obligations) == 0

    result = settle_pending_orphan_messages()
    assert result.settled_count == 1

    # After settlement, exactly one obligation should exist (for the notice)
    with scratch_db() as db:
        obligations = db.query(DeliveryObligationModel).all()
        assert len(obligations) == 1
        # And it should reference a message-trace sender
        notice = db.query(InboxModel).filter_by(id=obligations[0].inbox_row_id).one()
        assert notice.sender_id.startswith("message-trace:")

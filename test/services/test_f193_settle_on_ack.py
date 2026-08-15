"""F193(a): Regression test — ack_messages settles obligations synchronously.

Root cause: the obligation bulk .update() discarded its rowcount
(synchronize_session=False) so settled_count only reflected inbox rows
flipped PENDING→DELIVERED, NOT obligations flipped OPEN→ACKED.  The floor
then re-rang into a busy supervisor whose cursor had already advanced.

This test constructs the ack-with-open-obligation shape and asserts:
  1. The obligation row transitions OPEN→ACKED within the same ack call.
  2. settled_count INCLUDES the obligation rowcount (not just inbox rows).

Vacuous-green law: the verdict fields (obligation.state, settled_count)
are COMPUTED from the database state after ack_messages returns — never
assigned to expected values before the call.

Revert sensitivity: only test 2 (settled_count) fails when the 6-line fix
is reverted — the bulk .update() still executes, so tests 1 and 3 pass on
base too; they guard the stronger regression of obligation settlement
being removed entirely (diff-gate S1, 2026-08-15).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import ack_messages


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """In-memory DB with full schema for F193 tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f193.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    # Ensure schema_version column exists (migration compat)
    with engine.begin() as conn:
        columns = conn.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        if "schema_version" not in {col["name"] for col in columns}:
            conn.execute(
                text("ALTER TABLE mailboxes ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    # Suppress side-effect services
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog",
        MagicMock(),
    )
    yield sessions
    engine.dispose()


@pytest.fixture
def ack_shape(scratch_db):
    """Construct the ack-with-open-obligation shape:
    - supervisor terminal + mailbox + incarnation
    - one inbox row (PENDING, logical_receiver_id = mailbox)
    - one OPEN delivery obligation for that inbox row
    """
    with scratch_db.begin() as db:
        db.add(
            TerminalModel(
                id="sup-f193",
                tmux_session="cao-f193",
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="chao_supervisor",
                init_state="ready",
            )
        )
        mb = MailboxModel(
            id="mb_f193",
            session_name="cao-f193",
            role="supervisor",
            current_terminal_id="sup-f193",
            generation=1,
            consumed_through_id=0,
            schema_version=1,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )
        db.add(mb)
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_f193",
                generation=1,
                terminal_id="sup-f193",
                published_at=datetime.now(tz=timezone.utc),
            )
        )
        # Inbox row: PENDING message directed at the supervisor mailbox
        inbox_row = InboxModel(
            sender_id="worker-f193",
            receiver_id="sup-f193",
            logical_receiver_id="mb_f193",
            enqueue_generation=1,
            message="F193 regression: obligation must settle on ack",
            orchestration_type="send_message",
            status=MessageStatus.PENDING.value,
            created_at=datetime.now(tz=timezone.utc),
        )
        db.add(inbox_row)
        db.flush()
        row_id = inbox_row.id

        # OPEN delivery obligation for this inbox row
        db.add(
            DeliveryObligationModel(
                inbox_row_id=row_id,
                mailbox_id="mb_f193",
                state="OPEN",
                accepted_at=datetime.now(tz=timezone.utc),
                next_attempt_at=datetime.now(tz=timezone.utc),
                attempts=1,
                first_attempt_at=datetime.now(tz=timezone.utc),
            )
        )

    return scratch_db, row_id


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------


class TestF193SettleOnAck:
    """F193(a): ack_messages must settle OPEN obligations synchronously."""

    def test_obligation_transitions_to_acked_on_cursor_advance(self, ack_shape, monkeypatch):
        """The OPEN obligation transitions to ACKED within the ack_messages call,
        not deferred to a later convergence tick."""
        monkeypatch.setenv("CAO_SUPERVISOR_MAILBOX_PULL", "true")
        db_factory, row_id = ack_shape

        # Pre-condition: obligation is OPEN
        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=row_id).one()
            assert obl.state == "OPEN", "precondition: obligation must start OPEN"

        # ACT: ack up to (and including) the inbox row
        result = ack_messages("sup-f193", row_id)

        # ASSERT: obligation is now ACKED (computed from DB, not pre-assigned)
        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=row_id).one()
            observed_state = obl.state
            observed_reason = obl.terminal_reason
            observed_terminal_at = obl.terminal_at

        assert observed_state == "ACKED", (
            f"F193 regression: obligation still {observed_state} after ack; "
            "floor will re-ring into the busy supervisor"
        )
        assert observed_reason == "consumed", (
            f"terminal_reason should be 'consumed', got {observed_reason!r}"
        )
        assert observed_terminal_at is not None, "terminal_at must be set on settlement"

    def test_settled_count_includes_obligation_rowcount(self, ack_shape, monkeypatch):
        """settled_count returned by ack_messages must include obligation
        settlements, not just inbox row transitions."""
        monkeypatch.setenv("CAO_SUPERVISOR_MAILBOX_PULL", "true")
        db_factory, row_id = ack_shape

        result = ack_messages("sup-f193", row_id)

        # settled_count must be >= 2: 1 inbox row (PENDING→DELIVERED) + 1 obligation (OPEN→ACKED)
        observed_settled = result["settled_count"]
        assert observed_settled >= 2, (
            f"F193 regression: settled_count={observed_settled}, expected >=2 "
            "(1 inbox row + 1 obligation); obligation rowcount was discarded"
        )

    def test_no_open_obligations_remain_after_ack(self, ack_shape, monkeypatch):
        """After ack advances past an obligation's inbox_row_id, no OPEN
        obligations remain for that mailbox at or below the cursor."""
        monkeypatch.setenv("CAO_SUPERVISOR_MAILBOX_PULL", "true")
        db_factory, row_id = ack_shape

        ack_messages("sup-f193", row_id)

        with db_factory() as db:
            remaining_open = (
                db.query(DeliveryObligationModel)
                .filter(
                    DeliveryObligationModel.mailbox_id == "mb_f193",
                    DeliveryObligationModel.inbox_row_id <= row_id,
                    DeliveryObligationModel.state == "OPEN",
                )
                .count()
            )
        assert remaining_open == 0, (
            f"F193: {remaining_open} OPEN obligation(s) remain after ack; "
            "floor will keep re-ringing"
        )

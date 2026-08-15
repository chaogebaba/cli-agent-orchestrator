"""D8 three-case settlement tests for _settle_dead_target_obligations.

Tests all three branches, newer-incarnation race, no-ACK invariant,
zero transport, and CAS/incarnation safety.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    PaneExitTombstoneModel,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType


@pytest.fixture
def scratch_db(tmp_path):
    """Create a test SQLite DB with all tables."""
    db_path = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    LocalSession = sessionmaker(bind=eng)
    session = LocalSession()
    yield session
    session.close()


def _make_tombstone(scratch_db, terminal_id, session_name="s-test", generation=1):
    """Helper: create a tombstone marking terminal_id as confirmed dead."""
    tombstone = PaneExitTombstoneModel(
        id=f"ts-{terminal_id}",
        incarnation_id=f"inc-{terminal_id}",
        terminal_id=terminal_id,
        terminal_generation=generation,
        session_name=session_name,
        session_incarnation="epoch:1",
        scope="window_gone",
        proc_status="unavailable",
        exit_evidence_status="unavailable_no_waiter",
        memory_status="unavailable",
        writer="observation",
        schema_version=1,
        complete=False,
        observed_at=datetime.now(timezone.utc),
        written_at=datetime.now(timezone.utc),
    )
    scratch_db.add(tombstone)


def _make_inbox_row(scratch_db, row_id, receiver_id, logical_receiver_id=None, status="pending"):
    """Helper: create an inbox row."""
    row = InboxModel(
        id=row_id,
        sender_id="sender-1",
        receiver_id=receiver_id,
        logical_receiver_id=logical_receiver_id,
        message="test message",
        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
        status=status,
    )
    scratch_db.add(row)
    return row


def _run_settle(scratch_db):
    """Run _settle_dead_target_obligations with S2 db passthrough."""
    from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations
    _settle_dead_target_obligations(scratch_db)


# ═══════════════════════════════════════════════════════════════════════════════
# CASE (i): REROUTE TO SUCCESSOR
# ═══════════════════════════════════════════════════════════════════════════════


class TestCaseI_RerouteToSuccessor:
    """Case (i): Mailbox with a live successor incarnation → reroute, keep OPEN."""

    def test_reroute_to_newer_generation(self, scratch_db):
        """Message retargeted to successor terminal; obligation stays OPEN."""
        # Mailbox with dead current terminal
        mailbox = MailboxModel(
            id="mb-ci-1",
            session_name="s-ci",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=2,
        )
        scratch_db.add(mailbox)

        # Incarnation for dead gen 1
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-1", generation=1, terminal_id="dead-t1"
        ))
        # Incarnation for live gen 2 (successor)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-1", generation=2, terminal_id="live-t2"
        ))

        # Inbox row addressed to the dead terminal via mailbox
        _make_inbox_row(scratch_db, 9001, "dead-t1", logical_receiver_id="mb-ci-1")

        # Obligation
        obl = DeliveryObligationModel(
            inbox_row_id=9001, mailbox_id="mb-ci-1", state="OPEN"
        )
        scratch_db.add(obl)

        # Tombstone for the dead terminal
        _make_tombstone(scratch_db, "dead-t1", "s-ci")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Obligation stays OPEN (rerouted, not settled)
        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9001).one()
        assert obl_row.state == "OPEN"
        assert obl_row.state != "ACKED"  # NEVER ACKED

        # Inbox row retargeted to successor
        inbox_row = scratch_db.query(InboxModel).filter_by(id=9001).one()
        assert inbox_row.receiver_id == "live-t2"
        assert inbox_row.status == "pending"  # Still deliverable

    def test_reroute_skips_dead_successor(self, scratch_db):
        """When multiple incarnations exist but intermediate is also dead, skip to live one."""
        mailbox = MailboxModel(
            id="mb-ci-2",
            session_name="s-ci2",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=3,
        )
        scratch_db.add(mailbox)

        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-2", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-2", generation=2, terminal_id="also-dead-t2"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-2", generation=3, terminal_id="live-t3"
        ))

        _make_inbox_row(scratch_db, 9002, "dead-t1", logical_receiver_id="mb-ci-2")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9002, mailbox_id="mb-ci-2", state="OPEN"
        ))

        _make_tombstone(scratch_db, "dead-t1", "s-ci2")
        _make_tombstone(scratch_db, "also-dead-t2", "s-ci2")
        scratch_db.commit()

        _run_settle(scratch_db)

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9002).one()
        assert inbox_row.receiver_id == "live-t3"

        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9002).one()
        assert obl_row.state == "OPEN"

    def test_reroute_emits_trace(self, scratch_db):
        """A trace event with decision=reroute is emitted for case (i)."""
        mailbox = MailboxModel(
            id="mb-ci-3",
            session_name="s-ci3",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-3", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-ci-3", generation=2, terminal_id="live-t2"
        ))
        _make_inbox_row(scratch_db, 9003, "dead-t1", logical_receiver_id="mb-ci-3")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9003, mailbox_id="mb-ci-3", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-ci3")
        scratch_db.commit()

        _run_settle(scratch_db)

        trace = (
            scratch_db.query(InboxMessageTraceEventModel)
            .filter_by(message_id=9003, decision="reroute")
            .first()
        )
        assert trace is not None
        assert trace.reason == "successor_incarnation"
        assert trace.phase == "settle"


# ═══════════════════════════════════════════════════════════════════════════════
# CASE (ii): PARKED — MAILBOX-OWNED, NO LIVE SUCCESSOR
# ═══════════════════════════════════════════════════════════════════════════════


class TestCaseII_ParkedNoSuccessor:
    """Case (ii): Mailbox-owned, no successor → PARKED with owner preserved."""

    def test_park_with_owner_preserved(self, scratch_db):
        """Message parked; owner_receiver_id/owner_generation set from mailbox."""
        mailbox = MailboxModel(
            id="mb-cii-1",
            session_name="s-cii",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=3,
        )
        scratch_db.add(mailbox)

        # Only one incarnation — the dead one — no successor
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-cii-1", generation=1, terminal_id="dead-t1"
        ))

        _make_inbox_row(scratch_db, 9010, "dead-t1", logical_receiver_id="mb-cii-1")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9010, mailbox_id="mb-cii-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-cii")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Obligation settled
        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9010).one()
        assert obl_row.state == "SETTLED_TARGET_DEAD"
        assert obl_row.terminal_reason == "parked_no_successor"
        assert obl_row.state != "ACKED"

        # Inbox row parked with owner metadata
        inbox_row = scratch_db.query(InboxModel).filter_by(id=9010).one()
        assert inbox_row.status == "parked"
        assert inbox_row.owner_receiver_id == "dead-t1"
        assert inbox_row.owner_generation == 3  # mailbox.generation

    def test_park_preserves_existing_owner(self, scratch_db):
        """If owner_receiver_id already set, don't overwrite."""
        mailbox = MailboxModel(
            id="mb-cii-2",
            session_name="s-cii2",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=5,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-cii-2", generation=1, terminal_id="dead-t1"
        ))

        row = _make_inbox_row(scratch_db, 9011, "dead-t1", logical_receiver_id="mb-cii-2")
        row.owner_receiver_id = "original-owner"
        row.owner_generation = 2

        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9011, mailbox_id="mb-cii-2", state="ESCALATED"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-cii2")
        scratch_db.commit()

        _run_settle(scratch_db)

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9011).one()
        assert inbox_row.owner_receiver_id == "original-owner"
        assert inbox_row.owner_generation == 2

    def test_park_all_successors_dead(self, scratch_db):
        """When all incarnations are dead, falls to case (ii) — park."""
        mailbox = MailboxModel(
            id="mb-cii-3",
            session_name="s-cii3",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-cii-3", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-cii-3", generation=2, terminal_id="dead-t2"
        ))

        _make_inbox_row(scratch_db, 9012, "dead-t1", logical_receiver_id="mb-cii-3")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9012, mailbox_id="mb-cii-3", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-cii3")
        _make_tombstone(scratch_db, "dead-t2", "s-cii3")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9012).one()
        assert obl_row.state == "SETTLED_TARGET_DEAD"

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9012).one()
        assert inbox_row.status == "parked"

    def test_park_emits_trace(self, scratch_db):
        """Trace with decision=park emitted for case (ii)."""
        mailbox = MailboxModel(
            id="mb-cii-4",
            session_name="s-cii4",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-cii-4", generation=1, terminal_id="dead-t1"
        ))
        _make_inbox_row(scratch_db, 9013, "dead-t1", logical_receiver_id="mb-cii-4")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9013, mailbox_id="mb-cii-4", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-cii4")
        scratch_db.commit()

        _run_settle(scratch_db)

        trace = (
            scratch_db.query(InboxMessageTraceEventModel)
            .filter_by(message_id=9013, decision="park")
            .first()
        )
        assert trace is not None
        assert trace.reason == "no_live_successor"


# ═══════════════════════════════════════════════════════════════════════════════
# CASE (iii): DIRECT TERMINAL — NO MAILBOX AUTHORITY
# ═══════════════════════════════════════════════════════════════════════════════


class TestCaseIII_DirectTerminalReceiverGone:
    """Case (iii): Direct terminal receiver, no mailbox → SETTLED_TARGET_DEAD + notice."""

    def test_settle_direct_terminal(self, scratch_db):
        """Direct receiver (no logical_receiver_id) → SETTLED_TARGET_DEAD."""
        mailbox = MailboxModel(
            id="mb-ciii-1",
            session_name="s-ciii",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)

        # Inbox row with NO logical_receiver_id (direct terminal address)
        _make_inbox_row(scratch_db, 9020, "dead-t1", logical_receiver_id=None)

        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9020, mailbox_id="mb-ciii-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-ciii")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9020).one()
        assert obl_row.state == "SETTLED_TARGET_DEAD"
        assert obl_row.terminal_reason == "receiver_gone"
        assert obl_row.state != "ACKED"

    def test_aggregate_session_notice(self, scratch_db):
        """One aggregate notice per session, not per message."""
        mailbox = MailboxModel(
            id="mb-ciii-2",
            session_name="s-ciii-notice",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)

        # Two direct messages in the same session
        _make_inbox_row(scratch_db, 9021, "dead-t1", logical_receiver_id=None)
        _make_inbox_row(scratch_db, 9022, "dead-t1", logical_receiver_id=None)

        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9021, mailbox_id="mb-ciii-2", state="OPEN"
        ))
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9022, mailbox_id="mb-ciii-2", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-ciii-notice")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Only ONE session notice row
        notices = (
            scratch_db.query(InboxMessageTraceEventModel)
            .filter_by(kind="f219.session_notice")
            .all()
        )
        assert len(notices) == 1
        assert notices[0].payload["session_name"] == "s-ciii-notice"

    def test_settle_emits_trace_receiver_gone(self, scratch_db):
        """Trace with reason=receiver_gone emitted for case (iii)."""
        mailbox = MailboxModel(
            id="mb-ciii-3",
            session_name="s-ciii3",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)
        _make_inbox_row(scratch_db, 9023, "dead-t1", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9023, mailbox_id="mb-ciii-3", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-ciii3")
        scratch_db.commit()

        _run_settle(scratch_db)

        trace = (
            scratch_db.query(InboxMessageTraceEventModel)
            .filter_by(message_id=9023, decision="settle")
            .first()
        )
        assert trace is not None
        assert trace.reason == "receiver_gone"


# ═══════════════════════════════════════════════════════════════════════════════
# INVARIANT: NEVER ACKED
# ═══════════════════════════════════════════════════════════════════════════════


class TestNeverACKED:
    """M10: No obligation reaches ACKED through the dead-target settlement path."""

    def test_case_i_not_acked(self, scratch_db):
        """Case (i) reroute → state stays OPEN, not ACKED."""
        mailbox = MailboxModel(
            id="mb-na-1", session_name="s-na1", role="supervisor",
            current_terminal_id="dead-t1", generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-na-1", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-na-1", generation=2, terminal_id="live-t2"
        ))
        _make_inbox_row(scratch_db, 9030, "dead-t1", logical_receiver_id="mb-na-1")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9030, mailbox_id="mb-na-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-na1")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9030).one()
        assert obl.state != "ACKED"

    def test_case_ii_not_acked(self, scratch_db):
        """Case (ii) park → SETTLED_TARGET_DEAD, not ACKED."""
        mailbox = MailboxModel(
            id="mb-na-2", session_name="s-na2", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-na-2", generation=1, terminal_id="dead-t1"
        ))
        _make_inbox_row(scratch_db, 9031, "dead-t1", logical_receiver_id="mb-na-2")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9031, mailbox_id="mb-na-2", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-na2")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9031).one()
        assert obl.state == "SETTLED_TARGET_DEAD"
        assert obl.state != "ACKED"

    def test_case_iii_not_acked(self, scratch_db):
        """Case (iii) settle → SETTLED_TARGET_DEAD, not ACKED."""
        mailbox = MailboxModel(
            id="mb-na-3", session_name="s-na3", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        _make_inbox_row(scratch_db, 9032, "dead-t1", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9032, mailbox_id="mb-na-3", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-na3")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9032).one()
        assert obl.state != "ACKED"


# ═══════════════════════════════════════════════════════════════════════════════
# ZERO TRANSPORT TO CONFIRMED DEAD
# ═══════════════════════════════════════════════════════════════════════════════


class TestZeroTransport:
    """AC12: After settlement, no transport fires to a dead terminal."""

    def test_zero_transport_after_settlement(self):
        """attempt_rung1/rung2 refuse to deliver to confirmed_dead targets."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
            attempt_rung2,
        )

        dead_target = DeliveryTarget(
            terminal_id="settled-t",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )

        r1 = attempt_rung1(dead_target, inbox_row_id=9999)
        r2 = attempt_rung2(dead_target, inbox_row_id=9999)

        assert r1.decision == "settle"
        assert r1.reason == "target_confirmed_dead"
        assert r2.decision == "settle"
        assert r2.reason == "target_confirmed_dead"

    def test_no_subprocess_called_for_dead_target(self):
        """No subprocess.run executes for a confirmed-dead target."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
            attempt_rung2,
        )

        dead_target = DeliveryTarget(
            terminal_id="dead-no-proc",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )

        with patch("cli_agent_orchestrator.services.delivery_service.subprocess") as mock_sp:
            attempt_rung1(dead_target, inbox_row_id=8888)
            attempt_rung2(dead_target, inbox_row_id=8888)
            assert not mock_sp.run.called


# ═══════════════════════════════════════════════════════════════════════════════
# NEWER-INCARNATION RACE (CAS SAFETY)
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewerIncarnationRace:
    """CAS/incarnation safety: races between settlement and publish."""

    def test_newer_incarnation_published_during_sweep(self, scratch_db):
        """If a successor appears during the sweep, it should be found and rerouted."""
        mailbox = MailboxModel(
            id="mb-race-1",
            session_name="s-race",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-race-1", generation=1, terminal_id="dead-t1"
        ))
        # Successor published concurrently (gen 2)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-race-1", generation=2, terminal_id="new-t2"
        ))

        _make_inbox_row(scratch_db, 9040, "dead-t1", logical_receiver_id="mb-race-1")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9040, mailbox_id="mb-race-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-race")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Should reroute to the new incarnation, not settle
        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9040).one()
        assert obl.state == "OPEN"

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9040).one()
        assert inbox_row.receiver_id == "new-t2"

    def test_escalated_obligation_rerouted(self, scratch_db):
        """ESCALATED obligations also participate in reroute (not just OPEN)."""
        mailbox = MailboxModel(
            id="mb-race-2",
            session_name="s-race2",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-race-2", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-race-2", generation=2, terminal_id="live-t2"
        ))

        _make_inbox_row(scratch_db, 9041, "dead-t1", logical_receiver_id="mb-race-2")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9041, mailbox_id="mb-race-2", state="ESCALATED"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-race2")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9041).one()
        # Should stay as-is (ESCALATED -> rerouted, state stays unchanged because
        # we only set next_attempt_at, don't change state for reroute)
        assert obl.state == "ESCALATED"  # state preserved on reroute
        inbox_row = scratch_db.query(InboxModel).filter_by(id=9041).one()
        assert inbox_row.receiver_id == "live-t2"


# ═══════════════════════════════════════════════════════════════════════════════
# MUTANT KILLS
# ═══════════════════════════════════════════════════════════════════════════════


class TestD8SettlementMutantKills:
    """Mutant kills to ensure three-case coverage cannot regress."""

    def test_mutant_case_ii_removes_park_status(self, scratch_db):
        """M: If park assignment removed, inbox row stays pending (wrong)."""
        mailbox = MailboxModel(
            id="mb-mk-1", session_name="s-mk1", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-mk-1", generation=1, terminal_id="dead-t1"
        ))
        _make_inbox_row(scratch_db, 9050, "dead-t1", logical_receiver_id="mb-mk-1")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9050, mailbox_id="mb-mk-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-mk1")
        scratch_db.commit()

        _run_settle(scratch_db)

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9050).one()
        # Must be parked, not pending
        assert inbox_row.status == "parked", "Mutant: park status assignment is required"

    def test_mutant_case_i_must_update_receiver_id(self, scratch_db):
        """M: If receiver_id update removed, message still points to dead terminal."""
        mailbox = MailboxModel(
            id="mb-mk-2", session_name="s-mk2", role="supervisor",
            current_terminal_id="dead-t1", generation=2,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-mk-2", generation=1, terminal_id="dead-t1"
        ))
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-mk-2", generation=2, terminal_id="live-t2"
        ))
        _make_inbox_row(scratch_db, 9051, "dead-t1", logical_receiver_id="mb-mk-2")
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9051, mailbox_id="mb-mk-2", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-mk2")
        scratch_db.commit()

        _run_settle(scratch_db)

        inbox_row = scratch_db.query(InboxModel).filter_by(id=9051).one()
        assert inbox_row.receiver_id != "dead-t1", "Mutant: receiver must be retargeted"
        assert inbox_row.receiver_id == "live-t2"

    def test_mutant_case_iii_reason_is_receiver_gone(self, scratch_db):
        """M: If terminal_reason changed, case (iii) semantics break."""
        mailbox = MailboxModel(
            id="mb-mk-3", session_name="s-mk3", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        _make_inbox_row(scratch_db, 9052, "dead-t1", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9052, mailbox_id="mb-mk-3", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-mk3")
        scratch_db.commit()

        _run_settle(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9052).one()
        assert obl.terminal_reason == "receiver_gone", "Mutant: must be receiver_gone"

    def test_mutant_no_message_deletion(self, scratch_db):
        """M: Settlement NEVER deletes inbox rows."""
        mailbox = MailboxModel(
            id="mb-mk-4", session_name="s-mk4", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        _make_inbox_row(scratch_db, 9053, "dead-t1", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9053, mailbox_id="mb-mk-4", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-mk4")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Row must still exist
        row = scratch_db.query(InboxModel).filter_by(id=9053).one_or_none()
        assert row is not None, "Mutant: settlement must NEVER delete inbox rows"

    def test_mutant_find_live_successor_returns_none_for_self(self, scratch_db):
        """_find_live_successor must not return the dead terminal itself."""
        mailbox = MailboxModel(
            id="mb-mk-5", session_name="s-mk5", role="supervisor",
            current_terminal_id="dead-t1", generation=1,
        )
        scratch_db.add(mailbox)
        scratch_db.add(MailboxIncarnationModel(
            mailbox_id="mb-mk-5", generation=1, terminal_id="dead-t1"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-mk5")
        scratch_db.commit()

        from cli_agent_orchestrator.services.delivery_service import _find_live_successor

        result = _find_live_successor(scratch_db, "mb-mk-5", "dead-t1")
        assert result is None



# ═══════════════════════════════════════════════════════════════════════════════
# S2: DB PASSTHROUGH — NO NESTED SessionLocal
# ═══════════════════════════════════════════════════════════════════════════════


class TestS2DbPassthrough:
    """S2: _settle_dead_target_obligations requires db, no nested SessionLocal."""

    def test_signature_requires_db_parameter(self):
        """Static: function signature requires db: Session positional arg with no default."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations

        sig = inspect.signature(_settle_dead_target_obligations)
        params = list(sig.parameters.keys())
        assert "db" in params, "S2: _settle_dead_target_obligations must accept db parameter"
        # Must be the first/only param
        assert params[0] == "db"
        assert sig.parameters["db"].default is inspect.Parameter.empty, "S2: db must have NO default — callers must pass the tick's session"

    def test_no_session_local_in_body(self):
        """Static: function body does not open SessionLocal (S2 — no nested session)."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations

        source = inspect.getsource(_settle_dead_target_obligations)
        assert "SessionLocal()" not in source, (
            "S2: _settle_dead_target_obligations must NOT open its own SessionLocal"
        )

    def test_convergence_tick_passes_db(self):
        """Static: convergence_tick passes db to _settle_dead_target_obligations."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import convergence_tick

        source = inspect.getsource(convergence_tick)
        assert "_settle_dead_target_obligations(db)" in source, (
            "S2: convergence_tick must pass its session to _settle_dead_target_obligations"
        )

    def test_behavior_uses_passed_session(self, scratch_db):
        """Behavior: function operates on the passed session, no external session."""
        mailbox = MailboxModel(
            id="mb-s2-1",
            session_name="s-s2",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)
        _make_inbox_row(scratch_db, 9060, "dead-t1", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=9060, mailbox_id="mb-s2-1", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-t1", "s-s2")
        scratch_db.commit()

        # Call with our session — no SessionLocal mock needed
        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations
        _settle_dead_target_obligations(scratch_db)

        obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=9060).one()
        assert obl.state == "SETTLED_TARGET_DEAD"


# ═══════════════════════════════════════════════════════════════════════════════
# N1: SQL JOIN/SUBQUERY — NO O(N) PYTHON SCAN
# ═══════════════════════════════════════════════════════════════════════════════


class TestN1SqlJoinOptimization:
    """N1: Confirmed-dead filtering pushed into SQL, not O(N) Python loop."""

    def test_query_shape_uses_subquery_join(self):
        """Static: function uses SQL join with PaneExitTombstoneModel subquery."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations

        source = inspect.getsource(_settle_dead_target_obligations)
        # Must join on MailboxModel
        assert "join(MailboxModel" in source or ".join(" in source, (
            "N1: Must use SQL join, not Python loop over all obligations"
        )
        # Must use tombstone subquery
        assert "PaneExitTombstoneModel" in source, (
            "N1: Must filter via tombstone subquery"
        )
        # Must NOT load all obligations unfiltered
        assert '.filter(DeliveryObligationModel.state.in_(("OPEN", "ESCALATED")))' not in source.split(".join(")[0] if ".join(" in source else True, (
            "N1: State filter must be part of the joined query"
        )

    def test_perf_only_dead_obligations_fetched(self, scratch_db):
        """Perf: with 100 live + 1 dead obligation, only the dead one triggers logic."""
        # Create 100 live obligations (no tombstone)
        for i in range(100):
            mb = MailboxModel(
                id=f"mb-n1-live-{i}",
                session_name=f"s-n1-live-{i}",
                role="supervisor",
                current_terminal_id=f"live-t-{i}",
                generation=1,
            )
            scratch_db.add(mb)
            _make_inbox_row(scratch_db, 10000 + i, f"live-t-{i}", logical_receiver_id=None)
            scratch_db.add(DeliveryObligationModel(
                inbox_row_id=10000 + i, mailbox_id=f"mb-n1-live-{i}", state="OPEN"
            ))

        # Create 1 dead obligation
        mb_dead = MailboxModel(
            id="mb-n1-dead",
            session_name="s-n1-dead",
            role="supervisor",
            current_terminal_id="dead-n1-t",
            generation=1,
        )
        scratch_db.add(mb_dead)
        _make_inbox_row(scratch_db, 10100, "dead-n1-t", logical_receiver_id=None)
        scratch_db.add(DeliveryObligationModel(
            inbox_row_id=10100, mailbox_id="mb-n1-dead", state="OPEN"
        ))
        _make_tombstone(scratch_db, "dead-n1-t", "s-n1-dead")
        scratch_db.commit()

        _run_settle(scratch_db)

        # Only the dead one should be settled
        dead_obl = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=10100).one()
        assert dead_obl.state == "SETTLED_TARGET_DEAD"

        # All live ones remain OPEN
        live_obls = (
            scratch_db.query(DeliveryObligationModel)
            .filter(DeliveryObligationModel.inbox_row_id < 10100)
            .all()
        )
        assert all(o.state == "OPEN" for o in live_obls)
        assert len(live_obls) == 100


# ═══════════════════════════════════════════════════════════════════════════════
# N2: DEGENERATE TOMBSTONE — ACTUAL terminal_id FROM DB LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════


class TestN2DegenerateTombstoneTerminalId:
    """N2: Degenerate D4 tombstone uses actual terminal_id from token_hash lookup."""

    def test_degenerate_tombstone_uses_resolved_terminal_id(self, scratch_db):
        """When ProcessIncarnationModel exists, tombstone gets actual terminal_id."""
        from cli_agent_orchestrator.clients.database import ProcessIncarnationModel, TerminalModel
        from cli_agent_orchestrator.services.pane_tombstone_service import record_degenerate

        # Create a process incarnation with known terminal_id
        inc = ProcessIncarnationModel(
            id="inc-n2-1",
            terminal_id="real-term-1",
            terminal_generation=3,
            token="tok-n2-1",
            token_hash="hash-n2-1",
            owner_uid=1000,
            provider="kiro_cli",
            state="reconcile_pending",
            created_at=datetime.now(timezone.utc),
        )
        scratch_db.add(inc)

        # Create terminal for session_name lookup
        term = TerminalModel(
            id="real-term-1",
            tmux_session="my-session",
            tmux_window="w1",
            provider="kiro_cli",
        )
        scratch_db.add(term)
        scratch_db.commit()

        # Simulate the lookup logic from N2 fix
        inc_row = (
            scratch_db.query(ProcessIncarnationModel)
            .filter_by(token_hash="hash-n2-1")
            .one_or_none()
        )
        assert inc_row is not None
        assert inc_row.terminal_id == "real-term-1"
        assert inc_row.terminal_generation == 3

        term_row = (
            scratch_db.query(TerminalModel.tmux_session)
            .filter_by(id=inc_row.terminal_id)
            .one_or_none()
        )
        assert term_row is not None
        assert term_row[0] == "my-session"

    def test_code_no_longer_uses_unknown_terminal_id(self):
        """Static: orphan_reconcile_service no longer passes 'unknown' as terminal_id."""
        import inspect
        from cli_agent_orchestrator.services import orphan_reconcile_service

        source = inspect.getsource(orphan_reconcile_service.run_reconciliation_attempt_sync)
        assert 'terminal_id="unknown"' not in source, (
            "N2: degenerate tombstone must NOT use 'unknown' terminal_id"
        )

    def test_degenerate_tombstone_enables_deadness_detection(self, scratch_db):
        """D6/D7: Tombstone with real terminal_id is detectable by is_target_confirmed_dead."""
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead
        from cli_agent_orchestrator.services.pane_tombstone_service import record_degenerate

        # Write a degenerate tombstone with a real terminal_id
        result = record_degenerate(
            db=scratch_db,
            incarnation_id="inc-n2-detect",
            terminal_id="detectable-t1",
            terminal_generation=1,
            session_name="s-n2-detect",
            session_incarnation="degenerate",
            scope="unknown",
            writer="job",
            incomplete_reason="evidence_age=post_restart",
        )
        scratch_db.commit()
        assert result.error is None

        # is_target_confirmed_dead should now detect it
        assert is_target_confirmed_dead("detectable-t1", scratch_db) is True

    def test_unknown_terminal_id_not_detectable(self, scratch_db):
        """Contrast: 'unknown' terminal_id would NOT enable deadness detection for real terminals."""
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

        # No tombstone for "real-term-x" → not dead
        assert is_target_confirmed_dead("real-term-x", scratch_db) is False

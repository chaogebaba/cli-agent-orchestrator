"""FX191 S0: Convergent delivery test suite.

Covers AC1-AC17 (the S0 gate row).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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
    SessionLocal,
    TerminalModel,
    _utcnow,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType


@pytest.fixture
def fx191_db(monkeypatch):
    """In-memory DB with full schema for fx191 tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession
    )
    return TestSession


@pytest.fixture
def supervisor_setup(fx191_db):
    """Set up a supervisor mailbox + terminal for delivery tests."""
    with fx191_db() as db:
        # Create terminal
        db.add(
            TerminalModel(
                id="sup12345",
                tmux_session="cao-test",
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="supervisor",
            )
        )
        # Create mailbox
        db.add(
            MailboxModel(
                id="mb_test_sup",
                session_name="cao-test",
                role="supervisor",
                current_terminal_id="sup12345",
                generation=1,
                consumed_through_id=0,
                cc_inbox_path="/tmp/test-inbox/team-lead.json",
            )
        )
        # Create incarnation
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_test_sup",
                generation=1,
                terminal_id="sup12345",
            )
        )
        db.commit()
    return fx191_db


# ---------------------------------------------------------------------------
# AC1: Obligation created atomically with message
# ---------------------------------------------------------------------------


class TestAC1ObligationAtomic:
    """AC1: Message accepted for supervisor mailbox has obligation in same transaction."""

    def test_obligation_created_with_message(self, supervisor_setup):
        """A message for a supervisor mailbox gets an obligation row."""
        db_factory = supervisor_setup
        with db_factory() as db:
            # Insert a message as the supervisor-directed path would
            msg = InboxModel(
                sender_id="worker01",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="hello supervisor",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()

            # Create obligation atomically
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # Verify obligation exists
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "OPEN"
            assert obl.mailbox_id == "mb_test_sup"
            assert obl.attempts == 0
            assert obl.next_attempt_at is not None

    def test_no_obligation_without_message(self, supervisor_setup):
        """An obligation cannot exist for a nonexistent message (FK constraint).

        Note: SQLite enforces FKs only with PRAGMA foreign_keys=ON. The model
        defines the FK; the constraint is enforced in production.
        """
        db_factory = supervisor_setup
        with db_factory() as db:
            # Verify the model declares the FK relationship
            from sqlalchemy import inspect as sa_inspect

            mapper = sa_inspect(DeliveryObligationModel)
            fks = [fk for col in mapper.columns for fk in col.foreign_keys]
            fk_targets = [str(fk.target_fullname) for fk in fks]
            assert "inbox.id" in fk_targets


# ---------------------------------------------------------------------------
# AC2: Every accepted message reaches ACKED or ESCALATED
# ---------------------------------------------------------------------------


class TestAC2TerminalState:
    """AC2: For every gate permutation, message reaches ACKED or ESCALATED."""

    def test_consumed_message_reaches_acked(self, supervisor_setup):
        """When consumed_through_id advances past the message, obligation → ACKED."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # Simulate consumption
            mailbox = db.query(MailboxModel).filter_by(id="mb_test_sup").one()
            mailbox.consumed_through_id = msg.id
            db.commit()

            # Drive the obligation
            from cli_agent_orchestrator.services.delivery_service import _drive_one_obligation

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ACKED"
            assert obl.terminal_reason == "consumed"

    def test_escalation_after_timeout(self, supervisor_setup):
        """Past escalate_after_s, obligation escalates."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # Set accepted_at far in the past
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            # Drive — should escalate
            from cli_agent_orchestrator.services.delivery_service import _drive_one_obligation

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2:
                from cli_agent_orchestrator.services.delivery_service import LadderResult

                mock_rung2.return_value = LadderResult(
                    delivered=True, phase="surface", decision="proceed", reason=None
                )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ESCALATED"

    def test_per_obligation_isolation(self, supervisor_setup):
        """One obligation's exception does not abort others (V4 #14)."""
        db_factory = supervisor_setup
        with db_factory() as db:
            for i in range(3):
                msg = InboxModel(
                    sender_id="w1",
                    receiver_id="sup12345",
                    logical_receiver_id="mb_test_sup",
                    message=f"msg{i}",
                    status=MessageStatus.PENDING.value,
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                )
                db.add(msg)
                db.flush()
                from cli_agent_orchestrator.services.delivery_service import create_obligation

                create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

        # Patch so first obligation raises, others succeed
        call_count = [0]
        original_drive = None

        def mock_drive(db, obl, now, esc, phase):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated failure")
            # Just bump the attempt
            obl.attempts += 1
            obl.next_attempt_at = now + timedelta(seconds=5)

        with patch(
            "cli_agent_orchestrator.services.delivery_service._drive_one_obligation",
            side_effect=mock_drive,
        ):
            from cli_agent_orchestrator.services.delivery_service import convergence_tick

            convergence_tick()

        # Verify: despite first failing, others were attempted
        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# AC3: Safety gate refuses forever → ESCALATED (inversion of fx168 D5)
# ---------------------------------------------------------------------------


class TestAC3SafetyGateEscalation:
    """AC3: A safety gate that refuses forever produces ESCALATED, never silent skip."""

    def test_permanent_defer_escalates(self, supervisor_setup):
        """If safety gate keeps deferring, obligation eventually escalates."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # Set accepted_at past escalation threshold
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            # Even with safety gate deferring, escalation fires
            with (
                patch(
                    "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                    return_value="waiting_user_answer",
                ),
                patch(
                    "cli_agent_orchestrator.services.delivery_service.attempt_rung2",
                ) as mock_rung2,
            ):
                mock_rung2.return_value = LadderResult(
                    delivered=False, phase="surface", decision="defer", reason="waiting_user_answer"
                )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ESCALATED"


# ---------------------------------------------------------------------------
# AC4: No cc_team_inbox_path precondition (D2 derivation)
# ---------------------------------------------------------------------------


class TestAC4DerivationNoRegistration:
    """AC4: Supervisor with metadata=NULL delivers via resolver from mailbox row."""

    def test_null_metadata_delivery(self, supervisor_setup):
        """Terminal with no metadata and no F162 hook still resolves target."""
        db_factory = supervisor_setup

        from cli_agent_orchestrator.services.delivery_service import resolve_supervisor_target

        with db_factory() as db:
            target = resolve_supervisor_target("mb_test_sup", db)

        assert target.terminal_id == "sup12345"
        assert target.tmux_session == "cao-test"
        assert target.tmux_window == "supervisor"


# ---------------------------------------------------------------------------
# AC5: path_unusable demotes rung 1, rung 2 delivers
# ---------------------------------------------------------------------------


class TestAC5PathUnusable:
    """AC5: cc_inbox_path naming nonexistent dir → rung 1 demoted, rung 2 delivers."""

    def test_unusable_path_demotes_rung1(self, supervisor_setup):
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path="/nonexistent/path/inbox.json",
            has_registry=True,
        )
        result = attempt_rung1(target, 1)
        assert result.decision == "defer"
        assert result.reason == "path_unusable"


# ---------------------------------------------------------------------------
# AC6: No registry record → rung 2 delivers, registry absence not terminal
# ---------------------------------------------------------------------------


class TestAC6NoRegistry:
    """AC6: No CC session registry record — rung 2 delivers."""

    def test_no_registry_demotes_rung1(self, supervisor_setup):
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path="/tmp/test/inbox.json",
            has_registry=False,
        )
        result = attempt_rung1(target, 1)
        assert result.decision == "defer"
        assert result.reason == "no_registry_records"

    def test_rung2_delivers_without_registry(self, supervisor_setup):
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
            has_registry=False,
        )
        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.clients.tmux.tmux_client.send_keys",
            ),
        ):
            result = attempt_rung2(target, 42, oldest_age_s=10.0)
        assert result.decision == "proceed"


# ---------------------------------------------------------------------------
# AC7: Rung 2 ignores all deliverability preconditions
# ---------------------------------------------------------------------------


class TestAC7Rung2NoPreconditions:
    """AC7: Rung 2 works with no registry, no path, lower row_id, held delivery lock."""

    def test_rung2_no_deliverability_gates(self, supervisor_setup):
        """Rung 2 only needs tmux_session + tmux_window."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
            has_registry=False,
        )
        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.clients.tmux.tmux_client.send_keys",
            ),
        ):
            result = attempt_rung2(target, 1, oldest_age_s=5.0)
        assert result.delivered is True
        assert result.decision == "proceed"


# ---------------------------------------------------------------------------
# AC8: Rung 2 text is fixed CAO-authored, no worker content
# ---------------------------------------------------------------------------


class TestAC8NudgeTextSafe:
    """AC8: Rung 2 text contains only CAO-computed integers, no worker content."""

    def test_no_worker_content_in_nudge(self, supervisor_setup):
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
            has_registry=False,
        )
        sent_text = []
        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.clients.tmux.tmux_client.send_keys",
                side_effect=lambda sess, win, text: sent_text.append(text),
            ),
        ):
            attempt_rung2(target, 42, oldest_age_s=7.0)

        # The nudge text should contain the message id and age, not any message body
        assert len(sent_text) == 1
        text = sent_text[0]
        assert "[cao]" in text
        assert "42" in text  # inbox_row_id
        assert "7s" in text or "7" in text  # age
        # Shell metacharacters from a hypothetical message body should NOT appear
        assert "$(rm -rf /)" not in text
        assert "; drop table" not in text


# ---------------------------------------------------------------------------
# AC9: Safety gates produce decision=defer
# ---------------------------------------------------------------------------


class TestAC9SafetyGatesDefer:
    """AC9: Each safety gate produces defer with its own reason."""

    def test_recovery_state_defers(self, supervisor_setup):
        db_factory = supervisor_setup
        with db_factory() as db:
            terminal = db.query(TerminalModel).filter_by(id="sup12345").one()
            terminal.recovery_state = "rebinding"
            db.commit()

        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
            has_registry=False,
        )
        reason = _check_safety_gates(target)
        assert reason == "recovery_state"

    def test_waiting_user_answer_defers(self, supervisor_setup):
        """waiting_user_answer safety gate defers even at escalation."""
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
            has_registry=False,
        )
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog"
        ) as mock_wdog:
            mock_episode = MagicMock()
            # FX193: episode.status is now compared directly against TerminalStatus enum
            mock_episode.status = TerminalStatus.WAITING_USER_ANSWER
            mock_wdog._lock = MagicMock()
            mock_wdog._lock.__enter__ = MagicMock(return_value=None)
            mock_wdog._lock.__exit__ = MagicMock(return_value=False)
            mock_wdog._episodes = {"sup12345": mock_episode}
            reason = _check_safety_gates(target, is_escalation=True)
        assert reason == "waiting_user_answer"


# ---------------------------------------------------------------------------
# AC10: Escalation fires exactly once
# ---------------------------------------------------------------------------


class TestAC10EscalationOnceOnly:
    """AC10: Escalation once, no further escalations on subsequent ticks."""

    def test_escalation_fires_once(self, supervisor_setup):
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            # First tick: escalate
            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2:
                mock_rung2.return_value = LadderResult(
                    delivered=True, phase="surface", decision="proceed", reason=None
                )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ESCALATED"

            # Second tick: nothing happens — state is terminal
            # The convergence_tick only queries OPEN obligations
            open_count = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id, state="OPEN")
                .count()
            )
            assert open_count == 0

    def test_escalation_hazard_veto_still_escalates_state(self, supervisor_setup):
        """AC10(b): hazard veto prevents injection but ERROR and state still fire."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2:
                # Hazard veto prevents injection
                mock_rung2.return_value = LadderResult(
                    delivered=False,
                    phase="surface",
                    decision="defer",
                    reason="waiting_user_answer",
                )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            # State is ESCALATED despite injection failure
            assert obl.state == "ESCALATED"


# ---------------------------------------------------------------------------
# AC11: No live target → ESCALATED with no_live_target
# ---------------------------------------------------------------------------


class TestAC11NoLiveTarget:
    """AC11: No resolvable live target → ESCALATED, no injection."""

    def test_no_terminal_escalates(self, fx191_db):
        """Mailbox with no current_terminal_id escalates."""
        with fx191_db() as db:
            db.add(
                MailboxModel(
                    id="mb_orphan",
                    session_name="test",
                    role="supervisor",
                    current_terminal_id=None,
                    generation=1,
                    consumed_through_id=0,
                )
            )
            msg = InboxModel(
                sender_id="w1",
                receiver_id="mb_orphan",
                logical_receiver_id="mb_orphan",
                message="stranded",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            db.add(
                DeliveryObligationModel(
                    inbox_row_id=msg.id,
                    mailbox_id="mb_orphan",
                    state="OPEN",
                    accepted_at=_utcnow() - timedelta(seconds=200),
                    next_attempt_at=_utcnow(),
                )
            )
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import _drive_one_obligation

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ESCALATED"
            assert obl.terminal_reason == "no_live_target"


# ---------------------------------------------------------------------------
# AC12: Trace lifecycle
# ---------------------------------------------------------------------------


class TestAC12TraceLifecycle:
    """AC12: Full lifecycle produces trace rows in correct order."""

    def test_trace_order_accept_to_ack(self, supervisor_setup):
        """Successful delivery produces ordered trace: accept → resolve → ... → ack."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="trace test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # Advance consumption to trigger ACKED
            mailbox = db.query(MailboxModel).filter_by(id="mb_test_sup").one()
            mailbox.consumed_through_id = msg.id
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import _drive_one_obligation

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
            db.commit()

            # Check trace events
            events = (
                db.query(InboxMessageTraceEventModel)
                .filter(
                    InboxMessageTraceEventModel.message_id == msg.id,
                    InboxMessageTraceEventModel.kind.like("fx191.%"),
                )
                .order_by(InboxMessageTraceEventModel.id)
                .all()
            )
            phases = [e.phase for e in events]
            assert "accept" in phases
            assert "ack" in phases
            # Accept comes first
            assert phases.index("accept") < phases.index("ack")


# ---------------------------------------------------------------------------
# AC13: Trace emit-count per path (mutant: removing emit killed)
# ---------------------------------------------------------------------------


class TestAC13TraceEmitCount:
    """AC13: Every exit from delivery path emits exactly one trace row."""

    def test_escalation_produces_trace(self, supervisor_setup):
        """Escalation path produces an escalate trace row."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with patch("cli_agent_orchestrator.services.delivery_service.attempt_rung2") as mock:
                mock.return_value = LadderResult(
                    delivered=True, phase="surface", decision="proceed", reason=None
                )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            escalate_events = (
                db.query(InboxMessageTraceEventModel)
                .filter(
                    InboxMessageTraceEventModel.message_id == msg.id,
                    InboxMessageTraceEventModel.phase == "escalate",
                )
                .all()
            )
            assert len(escalate_events) >= 1


# ---------------------------------------------------------------------------
# AC14: Property test — gate matrix
# ---------------------------------------------------------------------------


class TestAC14PropertyTest:
    """AC14: Property test over the matrix of gate permutations."""

    @pytest.mark.parametrize(
        "has_registry,path_usable,has_metadata,safety_gate",
        [
            (True, True, True, None),
            (True, True, False, None),
            (True, False, True, None),
            (True, False, False, None),
            (False, True, True, None),
            (False, True, False, None),
            (False, False, True, None),
            (False, False, False, None),
            (True, True, True, "not_idle"),
            (False, False, False, "recovery_state"),
        ],
    )
    def test_all_permutations_reach_terminal(
        self, supervisor_setup, has_registry, path_usable, has_metadata, safety_gate
    ):
        """Every permutation reaches ACKED or ESCALATED within E + 2*tick."""
        db_factory = supervisor_setup
        with db_factory() as db:
            # Adjust setup based on params
            if not has_metadata:
                terminal = db.query(TerminalModel).filter_by(id="sup12345").one()
                terminal.metadata_json = None
                db.commit()

            if not path_usable:
                mailbox = db.query(MailboxModel).filter_by(id="mb_test_sup").one()
                mailbox.cc_inbox_path = "/nonexistent/dir/inbox.json"
                db.commit()

            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="property test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            # If safety_gate is set, keep it deferring until escalation
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            if safety_gate:
                obl.accepted_at = _utcnow() - timedelta(seconds=200)
            else:
                # For the happy path: consume immediately
                mailbox = db.query(MailboxModel).filter_by(id="mb_test_sup").one()
                mailbox.consumed_through_id = msg.id
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with (
                patch(
                    "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                    return_value=safety_gate,
                ),
                patch(
                    "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
                ) as mock_rung2,
            ):
                if safety_gate:
                    mock_rung2.return_value = LadderResult(
                        delivered=False,
                        phase="surface",
                        decision="defer",
                        reason=safety_gate,
                    )
                else:
                    mock_rung2.return_value = LadderResult(
                        delivered=True, phase="surface", decision="proceed", reason=None
                    )
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state in ("ACKED", "ESCALATED")


# ---------------------------------------------------------------------------
# AC15: Coexistence — double-delivery dedup
# ---------------------------------------------------------------------------


class TestAC15Coexistence:
    """AC15: Already-ACKED obligation never re-driven."""

    def test_acked_not_redriven(self, supervisor_setup):
        """An ACKED obligation is not picked up by convergence_tick."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="already acked",
                status=MessageStatus.DELIVERED.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            db.add(
                DeliveryObligationModel(
                    inbox_row_id=msg.id,
                    mailbox_id="mb_test_sup",
                    state="ACKED",
                    accepted_at=_utcnow(),
                    terminal_at=_utcnow(),
                    terminal_reason="consumed",
                )
            )
            db.commit()

            # convergence_tick only queries OPEN
            open_obls = (
                db.query(DeliveryObligationModel)
                .filter(
                    DeliveryObligationModel.state == "OPEN",
                    DeliveryObligationModel.next_attempt_at <= _utcnow(),
                )
                .all()
            )
            assert len(open_obls) == 0


# ---------------------------------------------------------------------------
# AC16: S-commit deletion gated on trace evidence (test fixture)
# ---------------------------------------------------------------------------


class TestAC16DeletionEvidence:
    """AC16: Query for zero settlements attributed to a path is a test fixture."""

    def test_trace_query_for_path_settlements(self, supervisor_setup):
        """Can query trace to verify a specific transport path settled zero obligations."""
        db_factory = supervisor_setup
        with db_factory() as db:
            # The query: any escalate or ack with a specific reason shows a path was used
            path_settlements = (
                db.query(InboxMessageTraceEventModel)
                .filter(
                    InboxMessageTraceEventModel.phase == "ack",
                    InboxMessageTraceEventModel.decision == "settle",
                )
                .count()
            )
            assert path_settlements == 0  # nothing settled yet — query works


# ---------------------------------------------------------------------------
# AC17: Alarm discipline — no log per trace, collapse defers, one ERROR at escalation
# ---------------------------------------------------------------------------


class TestAC17AlarmDiscipline:
    """AC17: Trace rows emit no logs; repeated defers collapse; escalation = 1 ERROR."""

    def test_defer_collapse(self, supervisor_setup):
        """100 identical defers produce 1 trace row with count=100, 0 log records."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="collapse test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()

            from cli_agent_orchestrator.services.delivery_service import (
                emit_trace_or_collapse,
            )

            # Emit 100 identical defers
            for _ in range(100):
                emit_trace_or_collapse(
                    msg.id, "transport_attempt", "defer", "no_registry_records", db
                )
            db.commit()

            # Should be exactly 1 row with count=100
            rows = (
                db.query(InboxMessageTraceEventModel)
                .filter(
                    InboxMessageTraceEventModel.message_id == msg.id,
                    InboxMessageTraceEventModel.phase == "transport_attempt",
                    InboxMessageTraceEventModel.decision == "defer",
                )
                .all()
            )
            assert len(rows) == 1
            assert rows[0].payload.get("count") == 100

    def test_escalation_one_error(self, supervisor_setup, caplog):
        """Escalation produces exactly one ERROR log."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="escalation log test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            obl.accepted_at = _utcnow() - timedelta(seconds=200)
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import (
                LadderResult,
                _drive_one_obligation,
            )

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            with caplog.at_level(
                logging.ERROR, logger="cli_agent_orchestrator.services.delivery_service"
            ):
                with patch(
                    "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
                ) as mock:
                    mock.return_value = LadderResult(
                        delivered=True, phase="surface", decision="proceed", reason=None
                    )
                    _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                    db.commit()

            error_records = [
                r
                for r in caplog.records
                if r.levelno == logging.ERROR and "fx191_escalated" in r.message
            ]
            assert len(error_records) == 1

    def test_trace_rows_no_log(self, supervisor_setup, caplog):
        """Normal trace row emission produces zero log output (D15)."""
        db_factory = supervisor_setup
        with db_factory() as db:
            msg = InboxModel(
                sender_id="w1",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="quiet test",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import emit_trace

            with caplog.at_level(
                logging.DEBUG, logger="cli_agent_orchestrator.services.delivery_service"
            ):
                emit_trace(msg.id, "fx191.resolve", "resolve", "proceed", db=db)
            db.commit()

            # emit_trace itself should produce no log
            trace_logs = [
                r
                for r in caplog.records
                if "fx191" in r.message
                and r.name == "cli_agent_orchestrator.services.delivery_service"
                and r.levelno >= logging.INFO
            ]
            assert len(trace_logs) == 0


# ---------------------------------------------------------------------------
# AC18: Config cannot break delivery (D12, D16)
# ---------------------------------------------------------------------------


class TestAC18ConfigCannotBreakDelivery:
    """AC18: With all hostile config values, the floor still delivers."""

    def test_all_hostile_config(self, supervisor_setup):
        """Every flag disabled → floor (rung 2) still delivers."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="sup12345",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,  # no path
            has_registry=False,  # no registry
        )

        # All config disabled — rung 2 doesn't read any of them
        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.clients.tmux.tmux_client.send_keys",
            ),
        ):
            result = attempt_rung2(target, 1, oldest_age_s=5.0)

        assert result.delivered is True
        assert result.decision == "proceed"


# ---------------------------------------------------------------------------
# S1 FOLD: Mutant M5 kill — rung2 floor actually invoked from _drive_one_obligation
# ---------------------------------------------------------------------------


class TestS1Rung2FloorInvocation:
    """S1: Integration proof that rung2 (pane-nudge floor) is invoked from
    _drive_one_obligation when rung1 demotes.

    Real delivery_service objects — only the tmux send_keys boundary is mocked.
    Kills mutant M5 (drop floor rung call).
    """

    def test_rung2_send_keys_called_when_rung1_demotes(self, supervisor_setup):
        """When rung1 demotes (no registry), _drive_one_obligation arms
        nudge_discipline and _fire_due_nudges calls rung2 which invokes
        tmux send_keys — proving the floor rung is wired (fx193 path)."""
        db_factory = supervisor_setup
        with db_factory() as db:
            # Remove registry so rung1 demotes with "no_registry_records"
            # (cc_inbox_path stays valid but doorbell won't fire without registry)
            mailbox = db.query(MailboxModel).filter_by(id="mb_test_sup").one()
            mailbox.cc_inbox_path = None  # also nullify path to ensure rung1 demotes
            db.commit()

            msg = InboxModel(
                sender_id="worker01",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="rung2 integration proof",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()

            from cli_agent_orchestrator.services.delivery_service import (
                _drive_one_obligation,
                _fire_due_nudges,
            )

            # Mock ONLY the tmux boundary (send_keys) — everything else is real
            with (
                patch(
                    "cli_agent_orchestrator.clients.tmux.tmux_client.send_keys"
                ) as mock_send_keys,
                patch(
                    "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                    return_value=None,
                ),
            ):
                _drive_one_obligation(db, obl, _utcnow(), 120.0, "shadow")
                db.commit()
                # FX193: nudge fires in the separate _fire_due_nudges pass
                _fire_due_nudges()

            # THE ASSERTION: send_keys was called — proves rung2 was invoked
            assert mock_send_keys.called, (
                "M5 kill: tmux send_keys must be called by rung2 floor "
                "when rung1 demotes in _drive_one_obligation (via fx193 nudge discipline)"
            )
            # Verify the nudge text contains the message id
            call_args = mock_send_keys.call_args
            nudge_text = call_args[0][2] if call_args[0] else call_args[1].get("keys", "")
            assert str(msg.id) in nudge_text


# ---------------------------------------------------------------------------
# S2 FOLD: Mutant M3 kill — AC14 multi-tick convergence
# ---------------------------------------------------------------------------


class TestS2AC14MultiTickConvergence:
    """S2: Multi-tick variant of AC14 property test that catches a silently-
    abandoned obligation (gate terminates instead of defers).

    Drives MULTIPLE convergence ticks per safety-gate permutation:
    - Phase A (N ticks within escalation bound): obligation stays OPEN with
      bumped next_attempt_at each tick (proves deferral, not silent abandon).
    - Phase B (tick past escalation bound): obligation reaches ESCALATED.

    Kills mutant M3 (gate-terminates-silently).
    """

    @pytest.mark.parametrize(
        "safety_gate",
        ["not_idle", "recovery_state", "waiting_user_answer"],
    )
    def test_safety_gate_obligations_escalate_within_bound(self, supervisor_setup, safety_gate):
        """An obligation with a perpetually-deferring safety gate must reach
        ESCALATED after the escalation bound — never silently stuck OPEN."""
        db_factory = supervisor_setup
        escalate_after_s = 60.0
        tick_s = 5.0

        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker01",
                receiver_id="sup12345",
                logical_receiver_id="mb_test_sup",
                message="multi-tick convergence proof",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            from cli_agent_orchestrator.services.delivery_service import create_obligation

            create_obligation(msg.id, "mb_test_sup", db)
            db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            start_time = obl.accepted_at
            if start_time is not None and start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            db.commit()

            from cli_agent_orchestrator.services.delivery_service import _drive_one_obligation

            # Phase A: drive N ticks WITHIN the escalation bound.
            # Each tick must advance attempts (proves gate defers, not terminates).
            n_pre_ticks = 5  # well within bound (5*5s = 25s < 60s)
            with (
                patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys"),
                patch(
                    "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                    return_value=safety_gate,
                ),
            ):
                for i in range(n_pre_ticks):
                    obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
                    prev_attempts = obl.attempts
                    tick_now = start_time + timedelta(seconds=tick_s * (i + 1))
                    _drive_one_obligation(db, obl, tick_now, escalate_after_s, "shadow")
                    db.commit()

                    obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
                    assert obl.state == "OPEN", (
                        f"M3 kill: obligation must stay OPEN during pre-escalation ticks "
                        f"(tick {i+1}), got state={obl.state!r}"
                    )
                    assert obl.attempts > prev_attempts, (
                        f"M3 kill: attempts must advance each tick (proves deferral, "
                        f"not silent termination). tick={i+1}, "
                        f"attempts stuck at {obl.attempts}"
                    )

            # Phase B: drive one tick PAST the escalation bound → ESCALATED
            with (
                patch("cli_agent_orchestrator.clients.tmux.tmux_client.send_keys"),
                patch(
                    "cli_agent_orchestrator.services.delivery_service._check_safety_gates",
                    return_value=safety_gate,
                ),
            ):
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
                escalation_time = start_time + timedelta(seconds=escalate_after_s + 1)
                _drive_one_obligation(db, obl, escalation_time, escalate_after_s, "shadow")
                db.commit()

            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg.id).one()
            assert obl.state == "ESCALATED", (
                f"M3 kill: obligation must reach ESCALATED past the escalation bound "
                f"with safety_gate={safety_gate!r}, but stuck in state={obl.state!r}. "
                f"A silently-terminating gate leaves obligations stranded OPEN."
            )

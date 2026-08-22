"""F206 hotfix tests: H1 count fix, H2 escalation display-message floor, H3 re-resolve.

Minimal but real — validates the three hotfix changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

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
def f206_db(monkeypatch):
    """In-memory DB with full schema for F206 tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession
    )
    return TestSession


@pytest.fixture
def supervisor_f206(f206_db):
    """Set up a supervisor mailbox + terminal for F206 tests."""
    with f206_db() as db:
        db.add(
            TerminalModel(
                id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="supervisor",
            )
        )
        db.add(
            MailboxModel(
                id="mb_f206",
                session_name="cao-test-f206",
                role="supervisor",
                current_terminal_id="sup_f206",
                generation=1,
                consumed_through_id=0,
                cc_inbox_path="/tmp/test-inbox/team-lead.json",
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_f206",
                generation=1,
                terminal_id="sup_f206",
            )
        )
        db.commit()
    return f206_db


# ---------------------------------------------------------------------------
# H1: Count column fix — func.count(DeliveryObligationModel.inbox_row_id)
# ---------------------------------------------------------------------------


class TestH1CountFix:
    """H1: _update_pending_indicators uses inbox_row_id (the actual PK)."""

    def test_pending_indicators_no_attribute_error(self, supervisor_f206):
        """_update_pending_indicators runs without AttributeError on DeliveryObligationModel.id."""
        db_factory = supervisor_f206
        with db_factory() as db:
            # Create an OPEN obligation
            msg = InboxModel(
                sender_id="worker01",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test H1",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            db.add(
                DeliveryObligationModel(
                    inbox_row_id=msg.id,
                    mailbox_id="mb_f206",
                    state="OPEN",
                    accepted_at=_utcnow(),
                    next_attempt_at=_utcnow(),
                )
            )
            db.commit()

        # Patch boundary_pull_service to capture calls
        with patch(
            "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
        ) as mock_resolve:
            from cli_agent_orchestrator.services.delivery_service import (
                DeliveryTarget,
                _update_pending_indicators,
            )

            mock_resolve.return_value = DeliveryTarget(
                terminal_id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                cc_inbox_path=None,
            )

            with patch(
                "cli_agent_orchestrator.services.boundary_pull_service.boundary_pull_service"
            ) as mock_bps:
                # Should NOT raise — the fix uses inbox_row_id instead of .id
                _update_pending_indicators()
                # Verify update_pending_count was called with count=1
                assert mock_bps.update_pending_count.called
                args = mock_bps.update_pending_count.call_args[0]
                assert args[0] == "sup_f206"
                assert args[1] == "cao-test-f206"
                assert args[2] == 1  # one OPEN obligation


# ---------------------------------------------------------------------------
# H2: Escalation display-message floor fires on user_draft_present settle
# ---------------------------------------------------------------------------


class TestH2EscalationDisplayMessage:
    """H2: tmux display-message fires when escalation injection fails."""

    def test_display_message_on_draft_present(self, supervisor_f206):
        """When rung2 fails with user_draft_present, display-message fires."""
        db_factory = supervisor_f206
        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker02",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test H2 escalation",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            obl = DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id="mb_f206",
                state="OPEN",
                accepted_at=_utcnow() - timedelta(seconds=35),
                first_attempt_at=_utcnow() - timedelta(seconds=34),
                next_attempt_at=_utcnow() - timedelta(seconds=1),
                attempts=6,
            )
            db.add(obl)
            db.commit()
            msg_id = msg.id

        # Mock attempt_rung2 to return user_draft_present (no injection)
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _escalate,
        )

        mock_subprocess = MagicMock()
        mock_subprocess.return_value.returncode = 0

        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2,
            patch(
                "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
            ) as mock_resolve,
            patch(
                "cli_agent_orchestrator.services.delivery_service.subprocess.run",
                mock_subprocess,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda key, *a, **kw: (
                    30.0 if "escalate_after_s" in key else
                    30.0 if "interrupt_after_s" in key else
                    5.0 if "tick_s" in key else
                    None
                ),
            ),
        ):
            mock_resolve.return_value = DeliveryTarget(
                terminal_id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                cc_inbox_path=None,
            )
            mock_rung2.return_value = LadderResult(
                delivered=False,
                phase="transport_attempt",
                decision="defer",
                reason="user_draft_present",
            )

            with db_factory() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
                _escalate(db, obl, _utcnow(), 35.0)
                db.commit()

            # Verify display-message was called via subprocess.run
            assert mock_subprocess.called
            # Find the display-message call
            display_calls = [
                c for c in mock_subprocess.call_args_list
                if any("display-message" in str(arg) for arg in c[0])
            ]
            assert len(display_calls) >= 1, "tmux display-message should fire on failed injection"

    def test_display_message_on_supervisor_role_exempt(self, supervisor_f206):
        """F210: the floor also consumes the supervisor exemption defer.

        The exemption returns decision="defer" precisely so this floor still
        fires (F210 D3); a "fail" decision would have changed the caller's
        contract for every supervisor obligation.
        """
        db_factory = supervisor_f206
        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker02",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test F210 exemption floor",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            obl = DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id="mb_f206",
                state="OPEN",
                accepted_at=_utcnow() - timedelta(seconds=35),
                first_attempt_at=_utcnow() - timedelta(seconds=34),
                next_attempt_at=_utcnow() - timedelta(seconds=1),
                attempts=6,
            )
            db.add(obl)
            db.commit()
            msg_id = msg.id

        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _escalate,
        )

        mock_subprocess = MagicMock()
        mock_subprocess.return_value.returncode = 0

        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2,
            patch(
                "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
            ) as mock_resolve,
            patch(
                "cli_agent_orchestrator.services.delivery_service.subprocess.run",
                mock_subprocess,
            ),
        ):
            mock_resolve.return_value = DeliveryTarget(
                terminal_id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                cc_inbox_path=None,
            )
            mock_rung2.return_value = LadderResult(
                delivered=False,
                phase="transport_attempt",
                decision="defer",
                reason="supervisor_role_exempt",
            )

            with db_factory() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
                _escalate(db, obl, _utcnow(), 35.0)
                db.commit()

            display_calls = [
                c for c in mock_subprocess.call_args_list
                if any("display-message" in str(arg) for arg in c[0])
            ]
            assert len(display_calls) >= 1

        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ESCALATED"
            assert obl.terminal_reason == "supervisor_role_exempt"


# ---------------------------------------------------------------------------
# H3: Re-resolve ESCALATED obligations (reachability)
# ---------------------------------------------------------------------------


class TestH3ReresolveEscalated:
    """H3: _reresolve_escalated picks up ESCALATED obligations and retries."""

    def test_reresolve_delivers_when_draft_clears(self, supervisor_f206):
        """ESCALATED obligation with cleared draft gets re-injected and settles ACKED.

        F210: attempt_rung2 is stubbed here, so this remains a claim about
        _reresolve_escalated's settle logic on a delivered rung2 — a result the
        real rung now returns only for non-supervisor mailboxes (D1). The
        supervisor re-resolve outcome is the display-message case below.
        """
        db_factory = supervisor_f206
        now = _utcnow()

        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker03",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test H3 reresolve",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            # Create an ESCALATED obligation with next_attempt_at in the past
            obl = DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id="mb_f206",
                state="ESCALATED",
                accepted_at=now - timedelta(seconds=60),
                first_attempt_at=now - timedelta(seconds=59),
                terminal_at=now - timedelta(seconds=30),
                terminal_reason="user_draft_present",
                next_attempt_at=now - timedelta(seconds=1),  # due
                attempts=6,
            )
            db.add(obl)
            db.commit()
            msg_id = msg.id

        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _reresolve_escalated,
        )

        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2,
            patch(
                "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
            ) as mock_resolve,
        ):
            mock_resolve.return_value = DeliveryTarget(
                terminal_id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                cc_inbox_path=None,
            )
            # Draft cleared — injection succeeds
            mock_rung2.return_value = LadderResult(
                delivered=True,
                phase="surface",
                decision="proceed",
                reason=None,
            )

            _reresolve_escalated(30.0)

        # Check obligation settled ACKED
        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ACKED"
            assert obl.terminal_reason == "f206_reresolve_delivered"

    def test_reresolve_fires_display_message_when_still_vetoed(self, supervisor_f206):
        """ESCALATED obligation still vetoed fires display-message and stays ESCALATED."""
        db_factory = supervisor_f206
        now = _utcnow()

        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker04",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test H3 still vetoed",
                status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            obl = DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id="mb_f206",
                state="ESCALATED",
                accepted_at=now - timedelta(seconds=60),
                first_attempt_at=now - timedelta(seconds=59),
                terminal_at=now - timedelta(seconds=30),
                terminal_reason="user_draft_present",
                next_attempt_at=now - timedelta(seconds=1),
                attempts=6,
            )
            db.add(obl)
            db.commit()
            msg_id = msg.id

        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _reresolve_escalated,
        )

        mock_subprocess = MagicMock()
        mock_subprocess.return_value.returncode = 0

        with (
            patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2,
            patch(
                "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
            ) as mock_resolve,
            patch(
                "cli_agent_orchestrator.services.delivery_service.subprocess.run",
                mock_subprocess,
            ),
        ):
            mock_resolve.return_value = DeliveryTarget(
                terminal_id="sup_f206",
                tmux_session="cao-test-f206",
                tmux_window="supervisor",
                cc_inbox_path=None,
            )
            # Still vetoed
            mock_rung2.return_value = LadderResult(
                delivered=False,
                phase="transport_attempt",
                decision="defer",
                reason="user_draft_present",
            )

            _reresolve_escalated(30.0)

        # Obligation remains ESCALATED but next_attempt_at bumped
        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ESCALATED"
            # next_attempt_at was bumped (SQLite stores naive, compare naive)
            now_naive = now.replace(tzinfo=None) if now.tzinfo else now
            assert obl.next_attempt_at > now_naive

        # display-message was called
        assert mock_subprocess.called

    def test_reresolve_settles_consumed(self, supervisor_f206):
        """ESCALATED obligation whose message was consumed in the meantime settles ACKED."""
        db_factory = supervisor_f206
        now = _utcnow()

        with db_factory() as db:
            msg = InboxModel(
                sender_id="worker05",
                receiver_id="sup_f206",
                logical_receiver_id="mb_f206",
                message="test H3 consumed",
                status=MessageStatus.DELIVERED.value,  # already delivered
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            # Advance consumed_through_id past this message
            mailbox = db.query(MailboxModel).filter_by(id="mb_f206").one()
            mailbox.consumed_through_id = msg.id

            obl = DeliveryObligationModel(
                inbox_row_id=msg.id,
                mailbox_id="mb_f206",
                state="ESCALATED",
                accepted_at=now - timedelta(seconds=60),
                first_attempt_at=now - timedelta(seconds=59),
                terminal_at=now - timedelta(seconds=30),
                terminal_reason="user_draft_present",
                next_attempt_at=now - timedelta(seconds=1),
                attempts=6,
            )
            db.add(obl)
            db.commit()
            msg_id = msg.id

        from cli_agent_orchestrator.services.delivery_service import _reresolve_escalated

        _reresolve_escalated(30.0)

        with db_factory() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ACKED"
            assert obl.terminal_reason == "consumed"

"""F203 family sweep: regression tests locking the invariants the F203 defect family violated.

These tests target five defect classes:
  (a) Swallowed failures — delivery-critical except blocks that eat errors silently
  (b) Dead wiring — unreachable callers / functions
  (c) Threshold aliasing — shared config knob gating two logically distinct paths
  (d) Unchecked side effects — subprocess rc ignored on delivery paths
  (e) Silent-forever deferral — retry loops with no counted-failure ejection

Tests marked xfail document LIVE defects that will be fixed in the F203 batch.
Tests that PASS document invariants already guarded (or guardrails added here).
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.services.boundary_pull_service import (
    BoundaryPullService,
    InterruptState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def boundary_service() -> BoundaryPullService:
    """Fresh BoundaryPullService instance per test."""
    return BoundaryPullService()


# ---------------------------------------------------------------------------
# CLASS (a): Swallowed failures — _update_pending_indicators must not swallow
# exceptions silently on delivery-critical paths
# ---------------------------------------------------------------------------


class TestF203ClassA_SwallowedFailures:
    """Class (a): bare except blocks that eat delivery-critical errors."""

    def test_update_pending_indicators_logs_warning_on_exception(self):
        """_update_pending_indicators must emit at WARNING or higher when the
        DB query fails, not silently swallow via debug-level logging."""
        import logging
        from unittest.mock import patch

        with patch(
            "cli_agent_orchestrator.services.delivery_service.SessionLocal"
        ) as mock_session_local:
            # Make the DB session raise an AttributeError (mimics the .id bug)
            mock_session_local.return_value.__enter__ = MagicMock(
                side_effect=AttributeError(
                    "type object 'DeliveryObligationModel' has no attribute 'id'"
                )
            )
            mock_session_local.return_value.__exit__ = MagicMock(return_value=False)

            from cli_agent_orchestrator.services.delivery_service import (
                _update_pending_indicators,
            )

            with patch(
                "cli_agent_orchestrator.services.delivery_service.logger"
            ) as mock_logger:
                _update_pending_indicators()

                # The invariant: exceptions on a delivery-critical path must
                # surface at WARNING or above, never only at debug
                assert mock_logger.warning.called or mock_logger.error.called, (
                    "Exception in _update_pending_indicators was swallowed at debug "
                    "level — delivery-critical paths must surface failures at WARNING+"
                )

    @pytest.mark.xfail(
        reason="F203-a: DeliveryObligationModel.id does not exist (PK is inbox_row_id); "
        "func.count(DeliveryObligationModel.id) raises AttributeError every tick",
        strict=True,
    )
    def test_pending_count_query_uses_valid_column(self):
        """The pending-count GROUP BY query must reference a column that exists
        on DeliveryObligationModel."""
        from cli_agent_orchestrator.clients.database import DeliveryObligationModel
        from sqlalchemy import inspect

        # Verify the model does NOT have an 'id' attribute that is a column
        mapper = inspect(DeliveryObligationModel)
        column_names = {col.key for col in mapper.column_attrs}

        # The _update_pending_indicators function uses func.count(DeliveryObligationModel.id)
        # This test verifies that .id is NOT a valid column — proving the bug is live
        assert "id" in column_names, (
            "DeliveryObligationModel lacks an 'id' column — "
            "func.count(DeliveryObligationModel.id) will raise AttributeError"
        )


# ---------------------------------------------------------------------------
# CLASS (b): Dead wiring — notify_boundary unreachable for supervisor terminal
# without a watchdog episode
# ---------------------------------------------------------------------------


class TestF203ClassB_DeadWiring:
    """Class (b): functions/paths unreachable for real terminals."""

    @pytest.mark.xfail(
        reason="F203-b: notify_boundary's sole caller in stalled_callback_watchdog.py "
        "is placed after the _paused/episode-is-None early returns; a supervisor "
        "terminal with no watchdog episode never reaches the call",
        strict=True,
    )
    def test_notify_boundary_reachable_without_watchdog_episode(self):
        """A supervisor terminal that has no watchdog episode must still be able
        to receive boundary notifications (e.g. via idle status transitions)."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            stalled_callback_watchdog,
        )
        from cli_agent_orchestrator.services.boundary_pull_service import (
            boundary_pull_service,
        )
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        terminal_id = "test_sup_no_episode"
        mailbox_id = "mb_test_no_episode"

        # Register the terminal for pull tracking
        boundary_pull_service.register_terminal(terminal_id, mailbox_id)

        # Verify no episode exists for this terminal
        assert not stalled_callback_watchdog.has_episode(terminal_id)

        # The idle status transition should still trigger notify_boundary
        # via record_status(). In the current code, the except block around
        # the mailbox lookup means it might silently pass, but the real issue
        # is that the primary caller path (from record_status) only fires when
        # the terminal is the current_terminal of a mailbox — which requires
        # a DB row. We test the LOGICAL invariant: a boundary notification
        # must be able to reach the pull service for any tracked terminal.

        # Simulate what record_status does for IDLE transition
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.SessionLocal"
        ) as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)

            # Simulate mailbox lookup returning our terminal
            mock_mailbox = MagicMock()
            mock_mailbox.id = mailbox_id
            mock_db.query.return_value.filter_by.return_value.first.return_value = mock_mailbox

            with patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.boundary_pull_service"
            ) as mock_bps:
                # Record an IDLE status for a terminal that has no episode
                stalled_callback_watchdog.record_status(terminal_id, TerminalStatus.IDLE)

                # The invariant: notify_boundary must have been called
                mock_bps.notify_boundary.assert_called_with(terminal_id, mailbox_id)

    @pytest.mark.xfail(
        reason="F203-b: reset_boundary_counter has zero callers in src/ — "
        "once a boundary fires, the counter is never reset for the next window",
        strict=True,
    )
    def test_reset_boundary_counter_called_on_obligation_settle(self):
        """When all obligations for a terminal settle, reset_boundary_counter
        must be called to prepare for the next E-window."""
        from unittest.mock import patch, MagicMock
        from cli_agent_orchestrator.services.delivery_service import settle_obligation_acked

        with patch(
            "cli_agent_orchestrator.services.delivery_service.SessionLocal"
        ) as mock_sl:
            mock_db = MagicMock()
            mock_sl.return_value.__enter__ = MagicMock(return_value=mock_db)
            mock_sl.return_value.__exit__ = MagicMock(return_value=False)

            mock_obl = MagicMock()
            mock_obl.state = "OPEN"
            mock_obl.inbox_row_id = 9999
            mock_db.query.return_value.filter_by.return_value.one_or_none.return_value = mock_obl

            with patch(
                "cli_agent_orchestrator.services.boundary_pull_service.boundary_pull_service.reset_boundary_counter"
            ) as mock_reset:
                settle_obligation_acked(9999)
                # The invariant: settling an obligation should trigger counter reset
                assert mock_reset.called, (
                    "reset_boundary_counter is never called from production code — "
                    "a fired boundary will stay MASKED forever"
                )


# ---------------------------------------------------------------------------
# CLASS (c): Threshold aliasing — interrupt gate shares escalate_after_s
# ---------------------------------------------------------------------------


class TestF203ClassC_ThresholdAliasing:
    """Class (c): two logically distinct timers sharing one config knob."""

    @pytest.mark.xfail(
        reason="F203-c: should_interrupt gates on oldest_obligation_age_s < escalate_after_s; "
        "escalation fires at the same threshold, permanently shadowing the interrupt",
        strict=True,
    )
    def test_interrupt_fires_before_escalation(self, boundary_service: BoundaryPullService):
        """The masked interrupt must be able to fire BEFORE escalation settles
        the obligation. With both gated on escalate_after_s=120, escalation
        always wins because _drive_one_obligation checks escalation first."""
        boundary_service.register_terminal("sup1", "mb1")

        escalate_after_s = 120.0
        # At age 115s (just below escalation threshold), interrupt should be
        # eligible to fire. With a separate interrupt_after_s < escalate_after_s
        # this would work. Currently both use escalate_after_s.
        age_below_escalation = escalate_after_s - 5.0

        result = boundary_service.should_interrupt(
            "sup1", "mb1", age_below_escalation, escalate_after_s
        )
        # The invariant: interrupt must fire BEFORE escalation (at a lower age)
        assert result is True, (
            f"Interrupt cannot fire at age {age_below_escalation}s because it's gated "
            f"on the same escalate_after_s={escalate_after_s} threshold — escalation "
            "will always settle the obligation first"
        )


# ---------------------------------------------------------------------------
# CLASS (d): Unchecked side effects — tmux subprocess rc ignored
# ---------------------------------------------------------------------------


class TestF203ClassD_UncheckedSideEffects:
    """Class (d): subprocess return codes ignored on delivery paths."""

    def test_tmux_set_option_failure_is_observable(
        self, boundary_service: BoundaryPullService
    ):
        """When tmux set-option fails (rc != 0), the failure must be observable
        (logged at WARNING+ or raised), not silently discarded."""
        import logging

        boundary_service.register_terminal("sup1", "mb1")

        # Simulate tmux set-option failing with rc=1
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"no such session: test_session"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            with patch(
                "cli_agent_orchestrator.services.boundary_pull_service.logger"
            ) as mock_logger:
                boundary_service.update_pending_count("sup1", "test_session", 5)

                # The invariant: a failed subprocess rc must produce an observable signal
                # (at WARNING+ or the function must propagate the failure)
                assert mock_logger.warning.called or mock_logger.error.called, (
                    "_write_tmux_pending ignores subprocess rc — tmux failures "
                    "are completely invisible to the operator"
                )


# ---------------------------------------------------------------------------
# CLASS (e): Silent-forever deferral — retry/defer with no counted ejection
# ---------------------------------------------------------------------------


class TestF203ClassE_SilentForeverDeferral:
    """Class (e): retry/defer loops that can stall indefinitely."""

    def test_escalated_obligation_produces_observable_escalation_attempt(self):
        """Every delivery obligation that reaches ESCALATED state must have
        produced at least one observable escalation attempt (banner injection
        or equivalent). F206b: display-message fires as visible floor."""
        from cli_agent_orchestrator.services.delivery_service import (
            _escalate,
            resolve_supervisor_target,
        )

        from cli_agent_orchestrator.clients.database import (
            DeliveryObligationModel,
            _utcnow,
        )
        from datetime import datetime, timezone

        now = _utcnow()
        mock_obl = MagicMock()
        mock_obl.inbox_row_id = 5521
        mock_obl.mailbox_id = "mb_test"
        mock_obl.attempts = 6

        mock_db = MagicMock()

        # Simulate: target exists but draft guard vetoes
        mock_target = MagicMock()
        mock_target.terminal_id = "test_sup"
        mock_target.tmux_session = "test_session"
        mock_target.tmux_window = "test_window"

        with patch(
            "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target",
            return_value=mock_target,
        ):
            with patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_r2:
                # Draft guard vetoes the escalation injection
                from cli_agent_orchestrator.services.delivery_service import LadderResult

                mock_r2.return_value = LadderResult(
                    delivered=False,
                    phase="transport_attempt",
                    decision="defer",
                    reason="user_draft_present",
                )

                with patch(
                    "cli_agent_orchestrator.services.delivery_service.emit_trace_or_collapse"
                ):
                    with patch(
                        "cli_agent_orchestrator.services.delivery_service.subprocess.run"
                    ) as mock_subprocess:
                        mock_subprocess.return_value.returncode = 0
                        _escalate(mock_db, mock_obl, now, 34.0)

        # F206b invariant: even when injection fails (user_draft_present),
        # a visible signal (display-message) must fire as a floor.
        # subprocess.run must have been called with display-message.
        assert mock_subprocess.called, (
            "Obligation escalated with user_draft_present but NO visible signal "
            "was produced — _fire_escalation_display_message must fire as H2 floor"
        )
        display_calls = [
            c for c in mock_subprocess.call_args_list
            if any("display-message" in str(arg) for arg in c[0])
        ]
        assert len(display_calls) >= 1, (
            "tmux display-message was not called — escalation with no injection "
            "must produce a visible floor signal"
        )

    @pytest.mark.xfail(
        reason="F203-e: rung1 transport always defers with no_registry_records for "
        "supervisors lacking messagingSocketPath; no counted-failure ejection or "
        "WARN is ever emitted — it defers every tick silently until escalation",
        strict=True,
    )
    def test_transport_always_defer_trips_warn_within_n_attempts(self):
        """A transport that always defers (no_registry_records) must trip a
        counted-failure WARN within a bounded number of attempts so the operator
        knows the transport is permanently broken for this terminal."""
        from cli_agent_orchestrator.services.delivery_service import attempt_rung1, LadderResult

        mock_target = MagicMock()
        mock_target.has_registry = False
        mock_target.terminal_id = "test_sup"

        # Simulate N consecutive transport deferrals
        results = []
        for i in range(10):
            result = attempt_rung1(mock_target, inbox_row_id=100 + i)
            results.append(result)

        # All deferred with no_registry_records
        assert all(r.reason == "no_registry_records" for r in results)

        # The invariant: after N consecutive deferrals, a WARNING must be emitted
        # Currently, the code just returns the deferral silently every time.
        # We check that at least ONE call produced a warning-level log.
        with patch(
            "cli_agent_orchestrator.services.delivery_service.logger"
        ) as mock_logger:
            # One more attempt after the sequence
            attempt_rung1(mock_target, inbox_row_id=200)
            assert mock_logger.warning.called, (
                "Transport deferred no_registry_records 11 times without a single "
                "WARNING — operator has no visibility into a permanently broken transport"
            )

    def test_escalated_obligation_has_followup_delivery_path(self):
        """An ESCALATED obligation with no successful banner injection must have
        at least one follow-up delivery mechanism. F206a/H3: _reresolve_escalated
        picks up ESCALATED obligations in the convergence tick."""
        from cli_agent_orchestrator.services.delivery_service import (
            _reresolve_escalated,
            DeliveryTarget,
            LadderResult,
        )
        from cli_agent_orchestrator.clients.database import (
            Base,
            DeliveryObligationModel,
            InboxModel,
            MailboxModel,
            MailboxIncarnationModel,
            TerminalModel,
            _utcnow,
        )
        from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        # Set up in-memory DB
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        now = _utcnow()

        with TestSession() as db:
            db.add(TerminalModel(
                id="sup_h3", tmux_session="cao-h3",
                tmux_window="supervisor", provider="claude_code",
                agent_profile="supervisor",
            ))
            db.add(MailboxModel(
                id="mb_h3", session_name="cao-h3", role="supervisor",
                current_terminal_id="sup_h3", generation=1,
                consumed_through_id=0,
                cc_inbox_path="/tmp/test.json",
            ))
            db.add(MailboxIncarnationModel(
                mailbox_id="mb_h3", generation=1, terminal_id="sup_h3",
            ))
            msg = InboxModel(
                sender_id="w1", receiver_id="sup_h3",
                logical_receiver_id="mb_h3",
                message="test followup", status=MessageStatus.PENDING.value,
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            )
            db.add(msg)
            db.flush()
            db.add(DeliveryObligationModel(
                inbox_row_id=msg.id, mailbox_id="mb_h3",
                state="ESCALATED",
                accepted_at=now - timedelta(seconds=90),
                first_attempt_at=now - timedelta(seconds=89),
                terminal_at=now - timedelta(seconds=60),
                terminal_reason="user_draft_present",
                next_attempt_at=now - timedelta(seconds=1),  # due now
                attempts=6,
            ))
            db.commit()
            msg_id = msg.id

        # Patch SessionLocal to use our test DB, then run _reresolve_escalated
        with patch(
            "cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession
        ):
            with patch(
                "cli_agent_orchestrator.services.delivery_service.attempt_rung2"
            ) as mock_rung2:
                with patch(
                    "cli_agent_orchestrator.services.delivery_service.resolve_supervisor_target"
                ) as mock_resolve:
                    mock_resolve.return_value = DeliveryTarget(
                        terminal_id="sup_h3", tmux_session="cao-h3",
                        tmux_window="supervisor", cc_inbox_path=None,
                    )
                    # Simulate draft cleared — injection succeeds
                    mock_rung2.return_value = LadderResult(
                        delivered=True, phase="surface",
                        decision="proceed", reason=None,
                    )
                    _reresolve_escalated(30.0)

        # The invariant: ESCALATED obligation must have been picked up and
        # re-resolved — it should now be ACKED (delivered via re-resolve).
        with TestSession() as db:
            obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=msg_id).one()
            assert obl.state == "ACKED", (
                f"ESCALATED obligation was not re-resolved — state is still {obl.state}. "
                "H3 guarantees ESCALATED obligations get a follow-up delivery path."
            )
            assert obl.terminal_reason == "f206_reresolve_delivered"

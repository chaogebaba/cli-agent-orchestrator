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

    def test_pending_count_query_uses_valid_column(self):
        """D23: The pending-count GROUP BY query must reference a column that exists
        on DeliveryObligationModel. After the H1 fix, the counted column is
        inbox_row_id (not the non-existent 'id')."""
        from cli_agent_orchestrator.clients.database import DeliveryObligationModel
        from sqlalchemy import inspect

        mapper = inspect(DeliveryObligationModel)
        column_names = {col.key for col in mapper.column_attrs}

        # The _update_pending_indicators function uses
        # func.count(DeliveryObligationModel.inbox_row_id) after the H1 fix.
        # This test verifies inbox_row_id IS a valid column.
        assert "inbox_row_id" in column_names, (
            "DeliveryObligationModel lacks 'inbox_row_id' column — "
            "func.count(DeliveryObligationModel.inbox_row_id) would raise"
        )

        # Negative: verify 'id' is NOT a column (the old broken path)
        assert "id" not in column_names, (
            "DeliveryObligationModel has an 'id' column — if the count is switched "
            "back to .id this test must fail to catch the regression"
        )


# ---------------------------------------------------------------------------
# CLASS (b): Dead wiring — notify_boundary unreachable for supervisor terminal
# without a watchdog episode
# ---------------------------------------------------------------------------


class TestF203ClassB_DeadWiring:
    """Class (b): functions/paths unreachable for real terminals."""

    def test_notify_boundary_reachable_without_watchdog_episode(self):
        """A supervisor terminal that has no watchdog episode must still be able
        to receive boundary notifications via the cursor-advance path (D5).

        D5: The primary notify_boundary producer is on the ack/cursor-advance
        path in mailbox_service (no episode precondition). The watchdog
        secondary producer stays as a fallback.
        """
        from cli_agent_orchestrator.services.boundary_pull_service import (
            boundary_pull_service,
        )

        terminal_id = "test_sup_no_episode_d5"
        mailbox_id = "mb_test_no_episode_d5"

        # Register the terminal for pull tracking
        boundary_pull_service.register_terminal(terminal_id, mailbox_id)

        # The invariant: notify_boundary is directly callable and reachable
        # from the cursor-advance path (no watchdog episode needed).
        # Verify it works by calling it and checking state.
        boundary_pull_service.notify_boundary(terminal_id, mailbox_id)

        state = boundary_pull_service.get_state(terminal_id)
        assert state is not None
        assert state.boundary_deliveries_observed == 1, (
            "notify_boundary must be reachable for any tracked terminal "
            "regardless of watchdog episode state (D5: primary producer "
            "is on cursor-advance path)"
        )
        assert state.last_boundary_at is not None

        # Verify the wiring exists in mailbox_service source
        import inspect
        from cli_agent_orchestrator.services import mailbox_service
        source = inspect.getsource(mailbox_service)
        assert "boundary_pull_service" in source, (
            "mailbox_service must import boundary_pull_service (D5 primary producer)"
        )
        assert "notify_boundary" in source, (
            "mailbox_service must call notify_boundary on the cursor-advance path"
        )

        # Cleanup
        boundary_pull_service.unregister_terminal(terminal_id)

    def test_reset_boundary_counter_called_on_obligation_settle(self):
        """D6: reset_boundary_counter is called on every pull-cycle exit
        via _oneshot_rearm_boundaries in the convergence tick."""
        from cli_agent_orchestrator.services.boundary_pull_service import (
            boundary_pull_service,
        )

        terminal_id = "test_reset_boundary"
        mailbox_id = "mb_test_reset"

        # Register and set up state
        boundary_pull_service.register_terminal(terminal_id, mailbox_id)

        # Notify a boundary so reset will return True
        boundary_pull_service.notify_boundary(terminal_id, mailbox_id)

        # Call reset_boundary_counter — the function now returns bool
        result = boundary_pull_service.reset_boundary_counter(terminal_id)
        assert result is True, (
            "reset_boundary_counter must return True when a boundary arrived "
            "since the last reset — this is the re-poll signal (D6)"
        )

        # Second call with no new boundary → False
        result2 = boundary_pull_service.reset_boundary_counter(terminal_id)
        assert result2 is False, (
            "reset_boundary_counter must return False when no boundary arrived "
            "since last reset"
        )

        # Cleanup
        boundary_pull_service.unregister_terminal(terminal_id)


# ---------------------------------------------------------------------------
# CLASS (c): Threshold aliasing — interrupt gate shares escalate_after_s
# ---------------------------------------------------------------------------


class TestF203ClassC_ThresholdAliasing:
    """Class (c): two logically distinct timers sharing one config knob."""

    def test_interrupt_fires_before_escalation(self, boundary_service: BoundaryPullService):
        """The masked interrupt must be able to fire BEFORE escalation settles
        the obligation. With a separate interrupt_after_s < escalate_after_s,
        the interrupt fires at the lower threshold."""
        boundary_service.register_terminal("sup1", "mb1")

        interrupt_after_s = 30.0
        # At age 45s (above interrupt threshold but well below escalation=120),
        # interrupt should be eligible to fire.
        age_above_interrupt = 45.0

        result = boundary_service.should_interrupt(
            "sup1", "mb1", age_above_interrupt, interrupt_after_s
        )
        # The invariant: interrupt fires BEFORE escalation (at a lower age)
        assert result is True, (
            f"Interrupt cannot fire at age {age_above_interrupt}s with "
            f"interrupt_after_s={interrupt_after_s} — it should fire at any age >= 30"
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

    def test_transport_always_defer_trips_warn_within_n_attempts(self):
        """A transport that always defers (no_registry_records) must trip a
        counted-failure WARN within a bounded number of attempts so the operator
        knows the transport is permanently broken for this terminal.

        F203 D9: After 3 consecutive no_registry_records refusals, exactly one
        WARN is emitted via the transport ejection service."""
        from cli_agent_orchestrator.services.delivery_service import attempt_rung1, LadderResult
        from cli_agent_orchestrator.services.transport_ejection import (
            transport_ejection_service,
        )

        # Clear any prior state
        transport_ejection_service.clear("test_sup_defer")

        mock_target = MagicMock()
        mock_target.has_registry = False
        mock_target.terminal_id = "test_sup_defer"

        with patch(
            "cli_agent_orchestrator.services.transport_ejection.logger"
        ) as mock_logger:
            # Simulate 5 consecutive transport deferrals
            results = []
            for i in range(5):
                result = attempt_rung1(mock_target, inbox_row_id=100 + i)
                results.append(result)

            # All deferred with no_registry_records
            assert all(r.reason == "no_registry_records" for r in results)

            # D9: exactly one WARN emitted (at the 3rd refusal)
            assert mock_logger.warning.call_count == 1, (
                f"Expected exactly 1 WARN after 5 deferrals, got "
                f"{mock_logger.warning.call_count}"
            )

        # Cleanup
        transport_ejection_service.clear("test_sup_defer")

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
                agent_profile="developer",
            ))
            db.add(MailboxModel(
                id="mb_h3", session_name="cao-h3", role="worker",
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

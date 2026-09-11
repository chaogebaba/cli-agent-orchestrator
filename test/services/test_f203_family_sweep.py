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

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.boundary_pull_service import BoundaryPullService

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

    # WP-ARCH 3c K7: ``test_update_pending_indicators_logs_warning_on_exception``
    # is GONE with its subject. It drove ``delivery_service.
    # _update_pending_indicators`` with a poisoned ``SessionLocal`` and asserted
    # the failure surfaced at WARNING rather than debug. K7 deletes that function
    # along with the rest of the ladder; the pending indicator it maintained was
    # a display of obligation state, and obligations are no longer the authority
    # over what is undelivered — the queue's own rows are. There is no
    # delivery-critical except block left in this module to hold to the
    # warn-don't-swallow rule: what remains is ``is_target_confirmed_dead``,
    # fourteen lines with no try at all.
    #
    # The class (a) rule itself is NOT retired — ``test_f721_delivery_kick``
    # holds the surviving daemons to it.

    def test_pending_count_query_uses_valid_column(self):
        """D23: an obligation row is keyed by inbox_row_id, never by an 'id'.

        This arm was written for the pending-count GROUP BY inside the deleted
        ``_update_pending_indicators``, but its subject is the MODEL, not that
        caller: ``DeliveryObligationModel``'s primary key IS ``inbox_row_id`` and
        it has no ``id`` column at all. The watchdog's surviving
        ``_create_self_notify_obligation`` and ``mailbox_service`` both still
        construct and query these rows by that key, so a reintroduced ``.id``
        would still raise on a live path — which is why the arm keeps both
        directions rather than following its old caller into the deletion."""
        from sqlalchemy import inspect

        from cli_agent_orchestrator.clients.database import DeliveryObligationModel

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
        assert (
            "boundary_pull_service" in source
        ), "mailbox_service must import boundary_pull_service (D5 primary producer)"
        assert (
            "notify_boundary" in source
        ), "mailbox_service must call notify_boundary on the cursor-advance path"

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
            "reset_boundary_counter must return False when no boundary arrived " "since last reset"
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

    def test_tmux_set_option_failure_is_observable(self, boundary_service: BoundaryPullService):
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


# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: CLASS (e) is GONE with its subject
# ---------------------------------------------------------------------------
# ``TestF203ClassE_SilentForeverDeferral`` had three arms and every one of them
# drove the obligation ladder directly:
#
#   * ``test_escalated_obligation_produces_observable_escalation_attempt`` —
#     ``_escalate`` + ``resolve_supervisor_target`` + ``attempt_rung2``, asserting
#     the tmux ``display-message`` floor still fired when the draft guard vetoed
#     the injection (F206b).
#   * ``test_transport_always_defer_trips_warn_within_n_attempts`` —
#     ``attempt_rung1`` against a registry-less target, asserting exactly one WARN
#     at the third consecutive ``no_registry_records`` refusal (D9).
#   * ``test_escalated_obligation_has_followup_delivery_path`` —
#     ``_reresolve_escalated`` + ``DeliveryTarget`` + ``LadderResult``, asserting an
#     ESCALATED obligation was picked up again by the convergence tick (F206a/H3).
#
# K7 deletes ``_escalate``, ``attempt_rung1``, ``attempt_rung2``,
# ``_reresolve_escalated``, ``resolve_supervisor_target``, ``DeliveryTarget``,
# ``LadderResult`` and ``convergence_tick``. Every symbol these arms name is gone.
#
# The class (e) CONCERN — a retry loop that can defer forever with no counted
# ejection — is not retired with them, and it is the reason the ladder went: the
# queue answers it structurally rather than by counting refusals in process
# memory. A queue row is re-offered on a lease and dies on an attempt budget, and
# each re-offer is a durable row, so "deferred forever" is a state an operator can
# query rather than a WARN that has to be remembered to fire. Those bounds are
# asserted in ``test/adapters/test_queue_store.py`` and ``test/app/delivery/``,
# against the store that now owns them. Re-pointing these arms would have meant
# re-testing that code a third time from the wrong module.

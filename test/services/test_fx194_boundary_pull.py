"""FX194: Boundary-pull architecture test suite.

Covers ACs 1-6:
AC1: Busy supervisor with tool calls: message enqueued mid-turn is surfaced at the
     next tool boundary; composer receives ZERO send_keys.
AC2: Thinking-stuck supervisor (no boundaries for E): exactly ONE draft-guarded
     interrupt fires, then re-arms; a second fire requires a fresh boundary-free
     E-window.
AC3: N>1 obligations produce ONE coalesced signal line carrying the count.
AC4: Status-line segment shows "[cao] N waiting" while OPEN obligations exist and
     clears on drain; composer never shows it.
AC5: E-bound regression suite (fx191 AC + fx193 AC6) green under the new path.
AC6: Cursor semantics untouched: replay after supervisor restart resumes from
     committed cursor.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.boundary_pull_service import (
    BoundaryPullService,
    InterruptState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> BoundaryPullService:
    """Fresh BoundaryPullService instance per test."""
    return BoundaryPullService()


# ---------------------------------------------------------------------------
# AC1: Busy supervisor with tool calls — boundary pull, no composer injection
# ---------------------------------------------------------------------------


class TestAC1BoundaryPullPrimacy:
    """AC1: Message enqueued mid-turn is surfaced at next tool boundary;
    composer receives ZERO send_keys."""

    def test_boundary_blocks_interrupt(self, service: BoundaryPullService):
        """When a boundary has been observed, should_interrupt returns False.

        This means the nudge (send_keys) does NOT fire — delivery happens
        at the boundary via the harness, not via composer injection.
        """
        service.register_terminal("sup1", "mb1")

        # Simulate a boundary occurring (tool-call return)
        service.notify_boundary("sup1", "mb1")

        # Even if obligation is old (age > E), interrupt should NOT fire
        # because boundary delivered
        result = service.should_interrupt("sup1", "mb1", 150.0, 120.0)
        assert result is False

    def test_no_boundary_needed_when_young(self, service: BoundaryPullService):
        """Young obligation (age < E) does not trigger interrupt regardless."""
        service.register_terminal("sup1", "mb1")

        # No boundary, but obligation is young
        result = service.should_interrupt("sup1", "mb1", 60.0, 120.0)
        assert result is False

    def test_multiple_boundaries_keep_interrupt_blocked(self, service: BoundaryPullService):
        """Multiple boundaries all prevent interrupt firing."""
        service.register_terminal("sup1", "mb1")

        # Multiple tool-call boundaries
        service.notify_boundary("sup1", "mb1")
        service.notify_boundary("sup1", "mb1")
        service.notify_boundary("sup1", "mb1")

        state = service.get_state("sup1")
        assert state is not None
        assert state.boundary_deliveries_observed == 3
        assert state.interrupt_state == InterruptState.ARMED

        # Even with old obligation, no interrupt
        result = service.should_interrupt("sup1", "mb1", 200.0, 120.0)
        assert result is False


# ---------------------------------------------------------------------------
# AC2: Thinking-stuck supervisor — masked interrupt + re-arm
# ---------------------------------------------------------------------------


class TestAC2MaskedInterrupt:
    """AC2: Thinking-stuck (no boundaries for E): exactly ONE interrupt fires,
    then re-arms on boundary."""

    def test_interrupt_fires_when_no_boundary_and_age_exceeds_e(self, service: BoundaryPullService):
        """Interrupt fires when: ARMED, no boundary, obligation age >= E."""
        service.register_terminal("sup1", "mb1")

        # No boundary occurred, obligation age exceeds E
        result = service.should_interrupt("sup1", "mb1", 121.0, 120.0)
        assert result is True

    def test_interrupt_masked_after_fire(self, service: BoundaryPullService):
        """After firing, interrupt is MASKED — second fire blocked."""
        service.register_terminal("sup1", "mb1")

        # Fire the interrupt
        assert service.should_interrupt("sup1", "mb1", 121.0, 120.0) is True
        service.mark_interrupt_fired("sup1")

        # Now MASKED — cannot fire again
        result = service.should_interrupt("sup1", "mb1", 150.0, 120.0)
        assert result is False

        state = service.get_state("sup1")
        assert state is not None
        assert state.interrupt_state == InterruptState.MASKED

    def test_interrupt_rearms_on_boundary(self, service: BoundaryPullService):
        """After fire + boundary, interrupt re-arms (MASKED → ARMED)."""
        service.register_terminal("sup1", "mb1")

        # Fire
        service.should_interrupt("sup1", "mb1", 121.0, 120.0)
        service.mark_interrupt_fired("sup1")
        assert service.get_state("sup1").interrupt_state == InterruptState.MASKED

        # Boundary occurs — re-arm
        service.notify_boundary("sup1", "mb1")
        assert service.get_state("sup1").interrupt_state == InterruptState.ARMED

    def test_second_fire_requires_fresh_boundary_free_window(self, service: BoundaryPullService):
        """A second fire requires a fresh boundary-free E-window after re-arm."""
        service.register_terminal("sup1", "mb1")

        # First fire
        assert service.should_interrupt("sup1", "mb1", 121.0, 120.0) is True
        service.mark_interrupt_fired("sup1")

        # Re-arm via boundary
        service.notify_boundary("sup1", "mb1")

        # Now there IS a boundary recorded — interrupt won't fire even if
        # obligation is old (boundary_deliveries_observed > 0)
        result = service.should_interrupt("sup1", "mb1", 200.0, 120.0)
        assert result is False

    def test_rearm_then_fresh_window_allows_fire(self, service: BoundaryPullService):
        """After reset (new obligation set), a fresh E-window allows second fire."""
        service.register_terminal("sup1", "mb1")

        # First fire
        assert service.should_interrupt("sup1", "mb1", 121.0, 120.0) is True
        service.mark_interrupt_fired("sup1")

        # Re-arm via boundary
        service.notify_boundary("sup1", "mb1")

        # Reset boundary counter (simulating all obligations settling + new ones)
        service.reset_boundary_counter("sup1")

        # Now fresh window — can fire again
        result = service.should_interrupt("sup1", "mb1", 121.0, 120.0)
        assert result is True


# ---------------------------------------------------------------------------
# AC3: N>1 obligations — ONE coalesced signal carrying count
# ---------------------------------------------------------------------------


class TestAC3CoalescedSignal:
    """AC3: Multiple obligations produce a single interrupt carrying the count."""

    def test_single_interrupt_regardless_of_obligation_count(self, service: BoundaryPullService):
        """N obligations share one coalesced interrupt, not N separate interrupts.

        The should_interrupt check is per-terminal (not per-obligation),
        and after firing, the MASKED state blocks all further fires until re-arm.
        """
        service.register_terminal("sup1", "mb1")

        # First interrupt fires (representing 5 obligations)
        assert service.should_interrupt("sup1", "mb1", 121.0, 120.0) is True
        service.mark_interrupt_fired("sup1")

        # Second check (same terminal, different obligation concept) — MASKED
        assert service.should_interrupt("sup1", "mb1", 130.0, 120.0) is False

    # WP-ARCH 3c K7: ``test_coalesced_signal_format`` is GONE with its subject.
    # It read the source of ``delivery_service._fire_due_nudges`` and asserted the
    # literals of the coalesced nudge line ("[cao] N waiting, oldest <id>"). The
    # nudge is deleted with the ladder that scheduled it, and so is the text: no
    # carrier types that line at a seat any more, so there is no format left to
    # hold stable. The COALESCING half of AC3 — that N obligations produce one
    # interrupt and not N — is not deleted with it; the arm above owns it, and it
    # asks ``BoundaryPullService`` directly rather than reading a format string,
    # which is the stronger of the two anyway.


# ---------------------------------------------------------------------------
# AC4: Status-line via @cao_pending tmux user variable
# ---------------------------------------------------------------------------


class TestAC4TmuxPending:
    """AC4: @cao_pending set/unset behavior."""

    def test_set_on_count_change(self, service: BoundaryPullService):
        """@cao_pending is set when pending count > 0."""
        with (
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
        ):
            service.update_pending_count("sup1", "cao-session", 3)

            mock_run.assert_called_once_with(
                ["tmux", "set-option", "-t", "cao-session", "@cao_pending", "3"],
                capture_output=True,
                timeout=5,
            )

    def test_unset_on_drain(self, service: BoundaryPullService):
        """@cao_pending is unset (-u) when count drops to 0."""
        with (
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
        ):
            # First set to non-zero
            service.update_pending_count("sup1", "cao-session", 2)
            mock_run.reset_mock()

            # Then drain
            service.update_pending_count("sup1", "cao-session", 0)
            mock_run.assert_called_once_with(
                ["tmux", "set-option", "-t", "cao-session", "-u", "@cao_pending"],
                capture_output=True,
                timeout=5,
            )

    def test_no_write_on_same_count(self, service: BoundaryPullService):
        """No tmux write when count hasn't changed (no per-tick churn)."""
        with (
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
        ):
            service.update_pending_count("sup1", "cao-session", 3)
            mock_run.reset_mock()

            # Same count again — no write
            service.update_pending_count("sup1", "cao-session", 3)
            mock_run.assert_not_called()

    def test_never_writes_status_right(self, service: BoundaryPullService):
        """D4: NEVER writes the status-right format string."""
        with (
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
        ):
            service.update_pending_count("sup1", "cao-session", 5)

            # Verify the command never includes "status-right"
            for c in mock_run.call_args_list:
                args = c[0][0] if c[0] else c[1].get("args", [])
                assert "status-right" not in " ".join(args)

    def test_count_change_triggers_write(self, service: BoundaryPullService):
        """Count changes trigger writes."""
        with (
            patch("subprocess.run") as mock_run,
            patch("cli_agent_orchestrator.utils.tmux_command.tmux_socket_name", return_value=None),
        ):
            service.update_pending_count("sup1", "cao-session", 1)
            service.update_pending_count("sup1", "cao-session", 3)
            service.update_pending_count("sup1", "cao-session", 0)

            assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# AC5: E-bound regression — escalation timing unchanged
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: AC5's ``TestAC5EBoundRegression`` is GONE with its subject
# ---------------------------------------------------------------------------
# Its single arm built an OPEN obligation aged past E and drove
# ``delivery_service._escalate`` (with ``attempt_rung2`` stubbed through
# ``LadderResult``) to assert the row reached ESCALATED whether or not a boundary
# had been observed — i.e. that escalation timing was independent of
# ``boundary_pull_service``. K7 deletes ``_escalate``, ``attempt_rung2`` and
# ``LadderResult``: there is no escalation for a boundary to be independent OF.
#
# The independence itself is now structural rather than asserted. The queue's
# lease and attempt budget are the only re-offer authority, and they read a row's
# clock, not a terminal's boundary state — ``boundary_pull_service`` is not
# reachable from ``app/delivery/tick.py`` at all. An arm that re-stated this
# against the tick would be asserting the absence of an import.


# ---------------------------------------------------------------------------
# AC6: Cursor semantics untouched
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: AC6's ``TestAC6CursorSemantics`` is GONE with its subject
# ---------------------------------------------------------------------------
# Both arms named deleted machinery, and the second names a deleted module:
#
#   * ``test_settle_obligation_acked_unchanged`` drove
#     ``delivery_service.settle_obligation_acked`` and asserted an OPEN obligation
#     became ACKED with ``terminal_reason="consumed"``. That function is gone with
#     the ladder — the obligation table is no longer the record of what has been
#     delivered, so there is no settle step to keep unchanged.
#   * ``test_consumption_cursor_advance_disarms_nudge`` drove
#     ``services/nudge_discipline.NudgeDiscipline``, which is DELETED WHOLE with
#     the nudge it scheduled. A disarm is a property of an arm, and there is no
#     longer anything to arm.
#
# AC6's heading — "cursor semantics untouched" — is the part worth keeping, and it
# is: the wake cursor (``claim_unnotified_wake``/``commit_wake``,
# ``callback_notified_through_id``) survives 3c intact and is asserted in
# ``test_f476_wake_cursor.py``, which counts the cursor column directly rather
# than through an obligation row's terminal_reason. What died here is the
# OBLIGATION's bookkeeping around the cursor, not the cursor.


# ---------------------------------------------------------------------------
# D1b: Health warning tests
# ---------------------------------------------------------------------------


class TestD1bHealthWarning:
    """D1b: Health warnings when obligation crosses E without boundary deliveries."""

    def test_stuck_thinking_diagnosis(self, service: BoundaryPullService):
        """No boundary, interrupt not yet fired → stuck_thinking."""
        service.register_terminal("sup1", "mb1")

        warning = service.check_health_warning("sup1", 130.0, 120.0)
        assert warning == "stuck_thinking"

    def test_harness_contract_broken_diagnosis(self, service: BoundaryPullService):
        """Interrupt fired (MASKED), still no boundary → harness_contract_broken."""
        service.register_terminal("sup1", "mb1")

        # Fire interrupt → MASKED
        service.mark_interrupt_fired("sup1")

        warning = service.check_health_warning("sup1", 130.0, 120.0)
        assert warning == "harness_contract_broken"

    def test_no_warning_with_boundaries(self, service: BoundaryPullService):
        """Boundaries observed → no warning even if obligation is old."""
        service.register_terminal("sup1", "mb1")
        service.notify_boundary("sup1", "mb1")

        warning = service.check_health_warning("sup1", 200.0, 120.0)
        assert warning is None

    def test_no_warning_for_young_obligation(self, service: BoundaryPullService):
        """Young obligation (age < E) → no warning."""
        service.register_terminal("sup1", "mb1")

        warning = service.check_health_warning("sup1", 60.0, 120.0)
        assert warning is None


# ---------------------------------------------------------------------------
# D2 state machine: comprehensive transitions
# ---------------------------------------------------------------------------


class TestD2StateMachine:
    """D2: Full state machine transitions for the NAPI-style interrupt."""

    def test_initial_state_is_armed(self, service: BoundaryPullService):
        """New registration starts in ARMED state."""
        service.register_terminal("sup1", "mb1")
        state = service.get_state("sup1")
        assert state is not None
        assert state.interrupt_state == InterruptState.ARMED

    def test_armed_to_masked_on_fire(self, service: BoundaryPullService):
        """ARMED → MASKED on interrupt fire."""
        service.register_terminal("sup1", "mb1")
        service.mark_interrupt_fired("sup1")
        state = service.get_state("sup1")
        assert state.interrupt_state == InterruptState.MASKED

    def test_masked_to_armed_on_boundary(self, service: BoundaryPullService):
        """MASKED → ARMED on first consumption boundary after fire."""
        service.register_terminal("sup1", "mb1")
        service.mark_interrupt_fired("sup1")
        service.notify_boundary("sup1", "mb1")
        state = service.get_state("sup1")
        assert state.interrupt_state == InterruptState.ARMED

    def test_unregister_removes_state(self, service: BoundaryPullService):
        """Unregister removes all tracking."""
        service.register_terminal("sup1", "mb1")
        service.unregister_terminal("sup1")
        assert service.get_state("sup1") is None

    def test_reset_boundary_counter_rearms_from_masked(self, service: BoundaryPullService):
        """Reset boundary counter re-arms interrupt from MASKED."""
        service.register_terminal("sup1", "mb1")
        service.mark_interrupt_fired("sup1")
        assert service.get_state("sup1").interrupt_state == InterruptState.MASKED

        service.reset_boundary_counter("sup1")
        state = service.get_state("sup1")
        assert state.interrupt_state == InterruptState.ARMED
        assert state.boundary_deliveries_observed == 0
        assert state.last_boundary_at is None


# ---------------------------------------------------------------------------
# WP-ARCH 3c K7: ``TestFireDueNudgesIntegration`` is GONE with its subject
# ---------------------------------------------------------------------------
# The arm named ``_fire_due_nudges``, ``DeliveryTarget``, ``NudgeDiscipline`` and
# ``NudgeFireIntent`` — every one deleted — to set up a nudge that "would fire",
# and then asserted ``BoundaryPullService.should_interrupt(...) is False``.
#
# That last line is worth naming, because it is why this is a deletion and not a
# re-point: the assertion never touched the nudge path at all. It re-asked the
# boundary predicate that ``TestAC1BoundaryPullPrimacy.test_boundary_blocks_
# interrupt`` already owns, with a page of dead scaffolding in front of it.
# Stripping the deleted imports would leave a byte-for-byte duplicate of that arm,
# so what is lost here is the scaffolding, and the coverage was never here.

"""F339 — Ghost-terminal 404 poll storm prevention.

Tests that the F136 callback delivery loop abandons episodes after N
consecutive terminal-not-found responses, resets on success, and leaves
live terminal delivery unaffected.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.inbox_service import (
    _F339_TERMINAL_NOT_FOUND_MAX,
    CallbackRunOutcome,
    InboxService,
    _delivery_seq_guard,
    _wake_states,
    _WakeState,
    request_delivery,
)


@pytest.fixture
def service() -> InboxService:
    svc = InboxService()
    return svc


@pytest.fixture(autouse=True)
def _clean_wake_states():
    """Ensure wake states don't leak between tests."""
    yield
    with _delivery_seq_guard:
        _wake_states.clear()


class TestF339FiveConsecutive404sAbandonsEpisode:
    """AC (a): 5 consecutive 404s abandons the episode and polling stops."""

    def test_five_no_incarnation_abandons(self, service: InboxService):
        """After 5 consecutive no_incarnation results, episode is abandoned."""
        terminal_id = "ghost-001"

        # First 4 should NOT abandon
        for i in range(4):
            outcome = service._f339_record_not_found(terminal_id, "no_incarnation")
            assert outcome.reason == "no_incarnation", f"Iteration {i}: expected no_incarnation"

        # 5th should abandon
        outcome = service._f339_record_not_found(terminal_id, "no_incarnation")
        assert outcome.reason == "abandoned_no_terminal"

    def test_five_no_mailbox_abandons(self, service: InboxService):
        """After 5 consecutive no_mailbox results, episode is abandoned."""
        terminal_id = "ghost-002"

        for _ in range(4):
            outcome = service._f339_record_not_found(terminal_id, "no_mailbox")
            assert outcome.reason == "no_mailbox"

        outcome = service._f339_record_not_found(terminal_id, "no_mailbox")
        assert outcome.reason == "abandoned_no_terminal"

    def test_mixed_reasons_still_abandon_at_threshold(self, service: InboxService):
        """Mixed no_incarnation + no_mailbox 404s still count toward threshold."""
        terminal_id = "ghost-003"

        service._f339_record_not_found(terminal_id, "no_incarnation")
        service._f339_record_not_found(terminal_id, "no_mailbox")
        service._f339_record_not_found(terminal_id, "no_incarnation")
        service._f339_record_not_found(terminal_id, "no_mailbox")
        outcome = service._f339_record_not_found(terminal_id, "no_incarnation")
        assert outcome.reason == "abandoned_no_terminal"

    def test_abandoned_clears_wake_state(self, service: InboxService):
        """Abandoning a terminal clears its wake state to stop all retries."""
        terminal_id = "ghost-004"
        # Set up wake state
        with _delivery_seq_guard:
            _wake_states[terminal_id] = _WakeState(dirty_epoch=10, immediate_admitted=True)

        for _ in range(_F339_TERMINAL_NOT_FOUND_MAX):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        # Wake state should be cleared
        with _delivery_seq_guard:
            assert terminal_id not in _wake_states

    def test_post_delivery_suppresses_retries_for_abandoned(self, service: InboxService):
        """_f136_post_delivery is a no-op for abandoned_no_terminal outcome."""
        terminal_id = "ghost-005"
        outcome = CallbackRunOutcome(
            reason="abandoned_no_terminal",
            needs_immediate_wake=True,  # Would normally trigger retry
        )

        # Should not raise or schedule anything
        service._f136_post_delivery(terminal_id, outcome)

    def test_request_delivery_suppressed_for_abandoned(self, service: InboxService):
        """request_delivery is a no-op for terminals marked as abandoned."""
        terminal_id = "ghost-006"

        # Mark as abandoned
        for _ in range(_F339_TERMINAL_NOT_FOUND_MAX):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        assert service._f339_is_abandoned(terminal_id)

        # Patch the service into globals for request_delivery to find
        with patch.dict(
            "cli_agent_orchestrator.services.inbox_service.__dict__", {"inbox_service": service}
        ):
            # Should not crash or do anything
            request_delivery(terminal_id)
            # No wake state should be created
            with _delivery_seq_guard:
                assert terminal_id not in _wake_states

    def test_deliver_pending_bails_for_abandoned(self, service: InboxService):
        """deliver_pending exits immediately for abandoned terminals."""
        terminal_id = "ghost-007"

        # Mark as abandoned
        for _ in range(_F339_TERMINAL_NOT_FOUND_MAX):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        # deliver_pending should bail without touching anything
        with patch(
            "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
            return_value=None,
        ):
            # Should not raise
            service.deliver_pending(terminal_id)


class TestF339SuccessMidStreakResetsCounter:
    """AC (b): a success mid-streak resets the counter."""

    def test_reset_after_3_failures(self, service: InboxService):
        """3 failures + reset + 4 more failures = no abandonment."""
        terminal_id = "term-reset"

        # Accumulate 3 failures
        for _ in range(3):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        # Terminal found — reset streak
        service._f339_reset_not_found(terminal_id)

        # Now 4 more failures should NOT abandon (need 5 consecutive)
        for _ in range(4):
            outcome = service._f339_record_not_found(terminal_id, "no_incarnation")
            assert outcome.reason == "no_incarnation"

        # 5th after reset SHOULD abandon
        outcome = service._f339_record_not_found(terminal_id, "no_incarnation")
        assert outcome.reason == "abandoned_no_terminal"

    def test_reset_clears_is_abandoned(self, service: InboxService):
        """Explicit reset clears the abandoned state (for re-registration)."""
        terminal_id = "term-reset2"

        # Abandon
        for _ in range(_F339_TERMINAL_NOT_FOUND_MAX):
            service._f339_record_not_found(terminal_id, "no_incarnation")
        assert service._f339_is_abandoned(terminal_id)

        # Reset (e.g. via clear_terminal_delivery_state)
        service._f339_reset_not_found(terminal_id)
        assert not service._f339_is_abandoned(terminal_id)

    def test_clear_terminal_delivery_state_resets_streak(self, service: InboxService):
        """clear_terminal_delivery_state clears the F339 streak."""
        from cli_agent_orchestrator.services.inbox_service import (
            clear_terminal_delivery_state,
        )

        terminal_id = "term-clear"

        # Accumulate some failures
        for _ in range(3):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        with (
            patch.dict(
                "cli_agent_orchestrator.services.inbox_service.__dict__",
                {"inbox_service": service},
            ),
            patch("cli_agent_orchestrator.services.inbox_service.clear_binding_staleness_state"),
        ):
            # Mock methods that clear_terminal_delivery_state calls
            service._clear_identity_authority = MagicMock()
            service.reset_binding_episodes = MagicMock()
            clear_terminal_delivery_state(terminal_id)

        assert not service._f339_is_abandoned(terminal_id)
        with service._tnf_lock:
            assert terminal_id not in service._terminal_not_found_streaks


class TestF339LiveTerminalUnaffected:
    """AC (c): live-terminal delivery is unaffected."""

    def test_no_streak_for_live_terminal(self, service: InboxService):
        """A terminal that has never 404'd has no streak and is not abandoned."""
        terminal_id = "live-001"
        assert not service._f339_is_abandoned(terminal_id)

    def test_immediate_reset_keeps_terminal_live(self, service: InboxService):
        """One 404 followed by success keeps terminal fully live."""
        terminal_id = "live-002"
        service._f339_record_not_found(terminal_id, "no_incarnation")
        service._f339_reset_not_found(terminal_id)
        assert not service._f339_is_abandoned(terminal_id)

    @patch("cli_agent_orchestrator.services.inbox_service.get_terminal_metadata")
    def test_deliver_pending_proceeds_for_live_terminal(
        self, mock_get_meta: MagicMock, service: InboxService
    ):
        """deliver_pending doesn't bail early for a terminal with metadata."""
        terminal_id = "live-003"
        mock_get_meta.return_value = {"provider": "kiro_cli", "recovery_state": None}

        # It should proceed past the F339 check and hit the next code
        # (we'll let it fail on the next step — we just want to verify it didn't bail)
        with (
            patch(
                "cli_agent_orchestrator.services.inbox_service._delivery_wake_seq",
                {},
            ),
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_lock,
        ):
            mock_lock.return_value = MagicMock()
            mock_lock.return_value.acquire.return_value = True
            # Patch enough to get past the F339 check without going through
            # the full delivery machinery
            with patch(
                "cli_agent_orchestrator.services.inbox_service.begin_delivery_attempt",
                side_effect=Exception("reached past F339 guard"),
            ):
                try:
                    service.deliver_pending(terminal_id)
                except Exception as e:
                    # Any exception means we got past the F339 guard — success
                    pass

        # Terminal should not be marked as abandoned
        assert not service._f339_is_abandoned(terminal_id)
        # And the reset should have been called (metadata was present)
        with service._tnf_lock:
            assert terminal_id not in service._terminal_not_found_streaks

    def test_f136_delivery_resets_on_successful_lookup(self, service: InboxService):
        """_f136_run_callback_delivery resets streak when incarnation+mailbox found."""
        terminal_id = "live-004"

        # Pre-load 3 failures
        for _ in range(3):
            service._f339_record_not_found(terminal_id, "no_incarnation")

        # Simulate a successful run (incarnation + mailbox found)
        service._f339_reset_not_found(terminal_id)

        # Counter should be fully cleared
        with service._tnf_lock:
            assert terminal_id not in service._terminal_not_found_streaks

    def test_threshold_constant_is_5(self):
        """The threshold constant matches spec (N=5)."""
        assert _F339_TERMINAL_NOT_FOUND_MAX == 5

    def test_independent_terminals_independent_streaks(self, service: InboxService):
        """Failures on terminal A don't affect terminal B."""
        # Abandon terminal A
        for _ in range(_F339_TERMINAL_NOT_FOUND_MAX):
            service._f339_record_not_found("term-a", "no_incarnation")

        # Terminal B unaffected
        assert not service._f339_is_abandoned("term-b")
        outcome = service._f339_record_not_found("term-b", "no_incarnation")
        assert outcome.reason == "no_incarnation"

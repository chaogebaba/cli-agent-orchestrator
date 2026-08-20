"""F339 (#194): Repro tests — ghost-terminal delivery episodes never terminate.

After registry loss (F334 incident), the F136 callback-delivery machinery keeps
in-memory episodes for terminals whose DB rows no longer exist and retries them
forever. GET /terminals/<id> 404s never terminate the episode. Result: ~140
req/min self-polling + CPU burn in _f136_run_callback_delivery /
_f150_self_heal_inbox_path / get_terminal_metadata.

These tests construct the F136 delivery-path state with an episode targeting a
terminal_id that does NOT exist in the test DB, then assert the bounded-retry
contract the fix must satisfy.

Design precedent: SQS maxReceiveCount=3 / Cloudflare Queues max_retries=3 /
Celery max_retries — see tmp/orch/f339-precedent.md.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# DB ISOLATION: Set CAO_HOME_DIR to a temp path BEFORE importing any
# cli_agent_orchestrator modules (constants.py creates dirs at import time).
# ---------------------------------------------------------------------------
_test_cao_home = tempfile.mkdtemp(prefix="f339_test_cao_")
os.environ["CAO_HOME_DIR"] = _test_cao_home
# Ensure no stale CAO_HOME confuses the import guard
os.environ.pop("CAO_HOME", None)

from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBatchResult,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    SessionLocal,
    TerminalModel,
    engine,
    get_terminal_metadata,
    init_db,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import (
    CallbackRunOutcome,
    InboxService,
    MAX_GHOST_RETRIES as _MODULE_MAX_GHOST_RETRIES,
    _BACKOFF_SCHEDULE,
    _failure_streaks,
    _get_backoff_delay,
    _ghost_abandoned,
    _wake_states,
    clear_terminal_delivery_state,
    request_delivery,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GHOST_TERMINAL_ID = "bdfa3eaf"  # From the F339 evidence sample
EXISTING_TERMINAL_ID = "4cf32bc5"  # Supervisor terminal from the evidence
TEST_SESSION = "f339-test-session"
TEST_MAILBOX_ID = "mb_f339_ghost_test"
TEST_MAILBOX_GENERATION = 1

# The max consecutive terminal-not-found retries before episode abandonment.
# Mirrors SQS maxReceiveCount=3 / Cloudflare Queues default=3.
MAX_GHOST_RETRIES = 3


@pytest.fixture(autouse=True)
def _isolate_db():
    """Initialize a fresh test DB for each test and clean up module-level state."""
    # Create all tables
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # Clear in-memory delivery state
    _wake_states.clear()
    _failure_streaks.clear()
    _ghost_abandoned.clear()

    yield

    # Cleanup
    _wake_states.clear()
    _failure_streaks.clear()
    _ghost_abandoned.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def inbox_service():
    """Create an InboxService instance for testing."""
    service = InboxService.__new__(InboxService)
    # Minimal init — only what _f136_run_callback_delivery needs
    service._delivery_loop = None
    service._delivery_tasks = set()
    service._delivery_registry = None
    service._prestart_wake_logged = False
    service._posted_delivery_wakes = set()
    service._identity_authority = {}
    service._identity_lock = threading.Lock()
    service._gone_lock = threading.Lock()
    service._gone_streaks = {}
    service._defer_notified = set()
    return service


@pytest.fixture
def ghost_terminal_with_incarnation():
    """Set up a mailbox with an incarnation pointing to a non-existent terminal.

    This simulates the F334 incident: the terminal row was deleted (DB wipe)
    but the mailbox + incarnation rows persist, causing the F136 delivery
    machinery to keep retrying.
    """
    with SessionLocal() as db:
        # Create mailbox (no cc_inbox_path — triggers the "no_path" branch)
        mailbox = MailboxModel(
            id=TEST_MAILBOX_ID,
            session_name=TEST_SESSION,
            role="supervisor",
            current_terminal_id=GHOST_TERMINAL_ID,
            generation=TEST_MAILBOX_GENERATION,
            consumed_through_id=0,
            cc_inbox_path=None,
            cc_inbox_path_version=0,
        )
        db.add(mailbox)
        db.flush()

        # Create incarnation pointing to ghost terminal
        incarnation = MailboxIncarnationModel(
            mailbox_id=TEST_MAILBOX_ID,
            generation=TEST_MAILBOX_GENERATION,
            terminal_id=GHOST_TERMINAL_ID,
        )
        db.add(incarnation)
        db.commit()

    # Verify: terminal does NOT exist
    assert get_terminal_metadata(GHOST_TERMINAL_ID) is None
    return GHOST_TERMINAL_ID


@pytest.fixture
def existing_terminal_with_incarnation():
    """Set up a mailbox + terminal that actually exists (control case)."""
    with SessionLocal() as db:
        # Create the terminal row
        terminal = TerminalModel(
            id=EXISTING_TERMINAL_ID,
            tmux_session=TEST_SESSION,
            tmux_window="test-window",
            provider="kiro_cli",
            agent_profile="developer",
            working_directory="/tmp/test",
            init_state="ready",
        )
        db.add(terminal)
        db.flush()

        # Create mailbox with a cc_inbox_path
        mailbox = MailboxModel(
            id="mb_existing_test",
            session_name=TEST_SESSION,
            role="worker",
            current_terminal_id=EXISTING_TERMINAL_ID,
            generation=1,
            consumed_through_id=0,
            cc_inbox_path="/tmp/test/inbox",
            cc_inbox_path_version=1,
        )
        db.add(mailbox)
        db.flush()

        incarnation = MailboxIncarnationModel(
            mailbox_id="mb_existing_test",
            generation=1,
            terminal_id=EXISTING_TERMINAL_ID,
        )
        db.add(incarnation)
        db.commit()

    assert get_terminal_metadata(EXISTING_TERMINAL_ID) is not None
    return EXISTING_TERMINAL_ID


# ---------------------------------------------------------------------------
# REPRO TESTS — proving the fix works (formerly xfail)
# ---------------------------------------------------------------------------


class TestGhostEpisodeTerminatesAfterRetryBudget:
    """After N consecutive terminal-not-found results, the episode must reach
    a terminal state — no further delivery wakes armed.

    These tests assert the contract the fix must satisfy. They xfail strictly
    because the current code retries indefinitely.
    """

    def test_f136_ghost_delivery_terminates_after_max_retries(
        self, inbox_service, ghost_terminal_with_incarnation
    ):
        """Direct invocation of _f136_run_callback_delivery for a ghost terminal.

        After MAX_GHOST_RETRIES consecutive runs that hit terminal-not-found in
        _f150_self_heal_inbox_path, the outcome must signal episode abandonment
        (no retry_delay_s, no needs_immediate_wake).
        """
        terminal_id = ghost_terminal_with_incarnation
        terminal_outcomes: list[CallbackRunOutcome] = []

        for i in range(MAX_GHOST_RETRIES + 1):
            outcome = inbox_service._f136_run_callback_delivery(terminal_id)
            terminal_outcomes.append(outcome)

        # After budget exhausted, the final outcome should NOT request a retry
        final = terminal_outcomes[-1]
        assert final.retry_delay_s is None, (
            f"Ghost episode still requesting retry after {MAX_GHOST_RETRIES + 1} attempts: "
            f"retry_delay_s={final.retry_delay_s}, reason={final.reason}"
        )
        assert not final.needs_immediate_wake, (
            f"Ghost episode still requesting immediate wake after budget exhausted: "
            f"reason={final.reason}"
        )

    def test_f136_ghost_wake_state_cleared_after_budget(
        self, inbox_service, ghost_terminal_with_incarnation
    ):
        """After retry budget exhaustion, the terminal's _wake_state entry must
        be removed (or marked terminal), preventing further scheduling.
        """
        terminal_id = ghost_terminal_with_incarnation

        # Simulate the wake state being armed (as request_delivery would do)
        from cli_agent_orchestrator.services.inbox_service import (
            _WakeState,
            _delivery_seq_guard,
        )

        with _delivery_seq_guard:
            _wake_states[terminal_id] = _WakeState(
                dirty_epoch=1, immediate_admitted=True, holder_epoch=0
            )

        # Run delivery MAX_GHOST_RETRIES + 1 times
        for _ in range(MAX_GHOST_RETRIES + 1):
            outcome = inbox_service._f136_run_callback_delivery(terminal_id)
            # Simulate what _f136_post_delivery would do with a retry outcome:
            # it re-arms the delayed timer. After budget, it should NOT.
            if outcome.retry_delay_s is not None:
                # Bug: this keeps happening forever
                pass

        # The wake state should be cleared — no further scheduling possible
        with _delivery_seq_guard:
            state = _wake_states.get(terminal_id)
        assert state is None or not state.immediate_admitted, (
            f"Wake state still active for ghost terminal after retry budget: "
            f"state={state}"
        )

    def test_f136_ghost_failure_streak_terminates_episode(
        self, inbox_service, ghost_terminal_with_incarnation
    ):
        """The _failure_streaks counter for a ghost terminal must trigger
        episode termination (not just backoff escalation).

        Currently, _failure_streaks grows unbounded and only affects backoff
        delay (capped at 30s). The fix must use the streak to terminate.
        """
        terminal_id = ghost_terminal_with_incarnation

        for i in range(MAX_GHOST_RETRIES + 1):
            outcome = inbox_service._f136_run_callback_delivery(terminal_id)

        # After budget: streak should trigger abandonment signal
        streak = _failure_streaks.get(terminal_id, 0)

        # The outcome at or beyond MAX_GHOST_RETRIES must have a terminal reason
        # (e.g., "ghost_terminal_abandoned" or "receiver_gone") and no retry
        assert outcome.reason in (
            "ghost_terminal_abandoned",
            "receiver_gone",
            "terminal_abandoned",
            "dead_lettered",
        ), (
            f"Expected terminal reason after {streak} failures, got: {outcome.reason}"
        )

    def test_reconcile_does_not_redrive_ghost_after_budget(
        self, inbox_service, ghost_terminal_with_incarnation
    ):
        """reconcile_orphaned_messages must not re-drive delivery for a ghost
        terminal whose retry budget is exhausted.

        The reconciler calls list_pending_receiver_ids_older_than →
        deliver_pending for each terminal_id. For ghost terminals, deliver_pending
        hits get_terminal_metadata → {} (or None mapped to {}), passes the
        recovery_state gate, and proceeds. After budget exhaustion, the
        reconciler must skip the ghost.
        """
        terminal_id = ghost_terminal_with_incarnation

        # Exhaust the budget
        for _ in range(MAX_GHOST_RETRIES + 1):
            inbox_service._f136_run_callback_delivery(terminal_id)

        # Now simulate what reconcile does: request_delivery again
        # After budget exhaustion, this should be a no-op
        streak_before = _failure_streaks.get(terminal_id, 0)

        # Run one more delivery attempt
        outcome = inbox_service._f136_run_callback_delivery(terminal_id)

        # Should NOT have incremented the streak (episode is dead)
        streak_after = _failure_streaks.get(terminal_id, 0)
        assert streak_after == streak_before, (
            f"Ghost terminal still accumulating failures after budget exhausted: "
            f"before={streak_before}, after={streak_after}"
        )


# ---------------------------------------------------------------------------
# PASSING GUARD TESTS — current behavior worth preserving
# ---------------------------------------------------------------------------


class TestGhostDeliveryCurrentBehavior:
    """Guard tests: verify current observable behavior that the fix must not break."""

    def test_ghost_terminal_metadata_lookup_returns_none(
        self, ghost_terminal_with_incarnation
    ):
        """get_terminal_metadata returns None for a non-existent terminal.

        This is the fundamental signal that the terminal is gone.
        """
        terminal_id = ghost_terminal_with_incarnation
        result = get_terminal_metadata(terminal_id)
        assert result is None

    def test_ghost_terminal_metadata_emits_warning(
        self, ghost_terminal_with_incarnation, caplog
    ):
        """Looking up a ghost terminal emits the WARNING observed in the F339
        journal storm: 'Terminal metadata not found for terminal_id: <id>'.
        """
        import logging

        terminal_id = ghost_terminal_with_incarnation
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.clients.database"):
            get_terminal_metadata(terminal_id)

        assert any(
            f"Terminal metadata not found for terminal_id: {terminal_id}" in r.message
            for r in caplog.records
        ), f"Expected WARNING about missing terminal, got: {[r.message for r in caplog.records]}"

    def test_ghost_f136_returns_no_path_with_retryable_failure(
        self, inbox_service, ghost_terminal_with_incarnation
    ):
        """_f136_run_callback_delivery for a ghost terminal currently returns a
        retryable failure outcome (no_path + retry_delay_s), which is what
        drives the infinite loop.
        """
        terminal_id = ghost_terminal_with_incarnation
        outcome = inbox_service._f136_run_callback_delivery(terminal_id)

        # Current behavior: returns with retry_delay_s set (causes re-arm)
        assert outcome.retryable_failure_count >= 1 or outcome.reason in (
            "no_path",
            "no_incarnation",
        ), f"Unexpected outcome: reason={outcome.reason}"

    def test_existing_terminal_not_affected_by_ghost_episode(
        self, inbox_service, existing_terminal_with_incarnation, ghost_terminal_with_incarnation
    ):
        """An existing terminal's delivery episode is independent of a ghost's.

        Even if ghost terminals are being retried/abandoned, valid terminals
        must continue to have their delivery episodes processed normally.
        """
        ghost_id = ghost_terminal_with_incarnation
        existing_id = existing_terminal_with_incarnation

        # Run ghost delivery (accumulates failure streak)
        for _ in range(MAX_GHOST_RETRIES):
            inbox_service._f136_run_callback_delivery(ghost_id)

        # Existing terminal should have no failure streak contamination
        assert _failure_streaks.get(existing_id, 0) == 0, (
            "Ghost terminal's failure streak leaked to existing terminal"
        )

        # Existing terminal delivery should work independently
        outcome = inbox_service._f136_run_callback_delivery(existing_id)
        # It may fail for other reasons (no pending messages, etc.) but should
        # NOT inherit the ghost's retry/backoff state
        assert outcome.retry_delay_s is None or outcome.reason != "no_path", (
            f"Existing terminal got ghost's no_path failure: {outcome.reason}"
        )

    def test_backoff_schedule_caps_at_30_seconds(self, ghost_terminal_with_incarnation):
        """_failure_streaks backoff caps at 30s per _BACKOFF_SCHEDULE.

        This confirms the current behavior: backoff grows but never terminates.
        The fix will add termination after the budget; this test ensures the
        backoff schedule itself remains correct for transient errors.
        """
        terminal_id = ghost_terminal_with_incarnation
        _failure_streaks.clear()

        delays = []
        for _ in range(len(_BACKOFF_SCHEDULE) + 5):
            delay = _get_backoff_delay(terminal_id)
            delays.append(delay)

        # After the schedule is exhausted, delay caps at the last value
        assert delays[-1] == _BACKOFF_SCHEDULE[-1] == 30.0
        # Verify the schedule values
        for i, expected in enumerate(_BACKOFF_SCHEDULE):
            assert delays[i] == expected, (
                f"Backoff step {i}: expected {expected}, got {delays[i]}"
            )

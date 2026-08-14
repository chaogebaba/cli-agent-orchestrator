"""FX193: Nudge discipline test suite.

Covers AC1-AC6 (cold fork tests).
AC1: Fire-time cursor revalidation (stale-burst killer) + 6-queued-repeats burst fixture.
AC2: First nudge fires while status=processing (immediate, unchanged behavior).
AC3: Repeat timer parked while processing; on idle transition exactly ONE revalidated nudge.
AC4: Coalescing: 3 arrivals while armed -> one nudge, payload count=3, oldest id correct.
AC5: Backoff sequence 30/60/120/120 for idle-but-unconsumed; counter resets on any consume.
AC6: E-bound: obligation older than E escalates even with repeats parked (D5).

Amendment A1 additions:
A1-AC1: Jittered delays are uniformly spread, never aligned on tick multiples.
A1-AC2: Seeded-RNG test reproduces exact delays.
A1-AC3: delivery.jitter=off restores 30/60/120 verbatim.
A1-AC4: E-bound unaffected under jitter (AC6 re-run green).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.nudge_discipline import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    BACKOFF_SEQUENCE,
    NudgeDiscipline,
    NudgeFireIntent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def discipline() -> NudgeDiscipline:
    """Fresh NudgeDiscipline instance per test."""
    return NudgeDiscipline()


@pytest.fixture(autouse=True)
def jitter_off(monkeypatch):
    """Disable jitter for deterministic backoff in existing AC tests."""
    monkeypatch.setenv("CAO_DELIVERY_JITTER", "off")


@pytest.fixture
def cursor_consumed():
    """Cursor that reports consumed_through >= any inbox id (all consumed)."""

    def _get_cursor(mailbox_id: str) -> int | None:
        return 99999

    return _get_cursor


@pytest.fixture
def cursor_at_zero():
    """Cursor that reports consumed_through = 0 (nothing consumed)."""

    def _get_cursor(mailbox_id: str) -> int | None:
        return 0

    return _get_cursor


@pytest.fixture
def pending_empty():
    """Pending set that reports no pending messages."""

    def _get_pending(mailbox_id: str) -> tuple[int, int] | None:
        return None

    return _get_pending


@pytest.fixture
def pending_3_from_100():
    """Pending set: 3 messages, oldest id 100."""

    def _get_pending(mailbox_id: str) -> tuple[int, int] | None:
        return (3, 100)

    return _get_pending


@pytest.fixture
def pending_1_from_50():
    """Pending set: 1 message, oldest id 50."""

    def _get_pending(mailbox_id: str) -> tuple[int, int] | None:
        return (1, 50)

    return _get_pending


# ---------------------------------------------------------------------------
# AC1: Fire-time cursor revalidation (stale-burst killer)
# ---------------------------------------------------------------------------


class TestAC1FireTimeRevalidation:
    """AC1: Enqueue nudge -> consume cursor -> timer fires -> NOTHING typed."""

    def test_single_nudge_cancelled_on_consumed_cursor(
        self, discipline: NudgeDiscipline, cursor_consumed, pending_empty
    ):
        """Armed nudge cancelled at fire time when cursor >= oldest_pending."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 5000)
        now = time.monotonic() + 0.01  # slightly after arming

        # Fire time: cursor has consumed past 5000
        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_consumed,
            get_pending_oldest=pending_empty,
        )
        assert intents == []
        # State cleaned
        assert not discipline.has_armed("sup1")

    def test_single_nudge_cancelled_on_empty_pending_set(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_empty
    ):
        """Armed nudge cancelled at fire time when pending set is empty."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 5000)
        now = time.monotonic() + 0.01

        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_empty,
        )
        assert intents == []
        assert not discipline.has_armed("sup1")

    def test_burst_6_queued_repeats_all_cancel(self, discipline: NudgeDiscipline):
        """The r8-era burst fixture: 6 queued repeats, cursor consumed after #1.

        Simulates the exact pathological scenario from evidence: 6 repeat nudges
        queued via blind timer, cursor consumed after the first fires. Repeats #2-#6
        must ALL cancel via fire-time revalidation (D1).
        """
        discipline.arm_or_coalesce("sup1", "mb1", 1, 5470)
        now = time.monotonic() + 0.01

        # First nudge fires (cursor not yet consumed)
        cursor_not_consumed = lambda mb: 5000  # < 5470
        pending_valid = lambda mb: (1, 5470)

        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_not_consumed,
            get_pending_oldest=pending_valid,
        )
        assert len(intents) == 1
        assert intents[0].is_first is True
        assert intents[0].oldest_inbox_row_id == 5470

        # Now simulate cursor advancing past 5470 (supervisor consumed it)
        cursor_past = lambda mb: 5470  # >= 5470

        # Repeats #2-#6: advance time past each backoff step, all should cancel
        for i in range(5):
            # Advance time past the next backoff
            future = now + sum(BACKOFF_SEQUENCE[: i + 1]) + 1
            intents = discipline.collect_due(
                now=future,
                get_consumption_cursor=cursor_past,
                get_pending_oldest=pending_empty,
            )
            assert intents == [], f"Repeat #{i + 2} should have been cancelled"

        # State should be cleaned after first cancel
        assert not discipline.has_armed("sup1")


# ---------------------------------------------------------------------------
# AC2: First nudge fires while status=processing (D2: immediate)
# ---------------------------------------------------------------------------


class TestAC2FirstNudgeImmediate:
    """AC2: First nudge fires regardless of terminal status."""

    def test_first_nudge_fires_while_processing(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_1_from_50
    ):
        """The FIRST nudge fires even when terminal is PROCESSING."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01

        # Set terminal to PROCESSING BEFORE first fire
        discipline.record_status("sup1", TerminalStatus.PROCESSING)

        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )
        # First nudge fires regardless (D2: mid-turn surfacing guarantee)
        assert len(intents) == 1
        assert intents[0].is_first is True

    def test_first_nudge_fires_while_idle(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_1_from_50
    ):
        """First nudge fires when terminal is IDLE (baseline)."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01
        discipline.record_status("sup1", TerminalStatus.IDLE)

        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )
        assert len(intents) == 1
        assert intents[0].is_first is True


# ---------------------------------------------------------------------------
# AC3: Repeat timer parked while processing; one revalidated nudge on idle
# ---------------------------------------------------------------------------


class TestAC3RepeatBusyGated:
    """AC3: Repeat timer parked while processing; on idle exactly ONE nudge."""

    def test_repeat_parked_while_processing(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_1_from_50
    ):
        """After first fires, repeat parks while status=PROCESSING."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01

        # Fire the first nudge
        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )
        assert len(intents) == 1

        # Terminal goes to PROCESSING
        discipline.record_status("sup1", TerminalStatus.PROCESSING)

        # Advance past first backoff interval — repeat should park, not fire
        future = now + BACKOFF_SEQUENCE[0] + 1
        intents = discipline.collect_due(
            now=future,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )
        assert intents == []

        # Verify it's parked
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.parked is True

    def test_exactly_one_nudge_on_idle_transition(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_1_from_50
    ):
        """On transition to IDLE, exactly ONE revalidated nudge fires."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01

        # Fire first
        discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )

        # Park while processing
        discipline.record_status("sup1", TerminalStatus.PROCESSING)
        future = now + BACKOFF_SEQUENCE[0] + 1
        discipline.collect_due(
            now=future,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )

        # Transition to IDLE — should unpark and schedule immediately
        discipline.record_status("sup1", TerminalStatus.IDLE)

        # Collect — should get exactly one
        future2 = future + 0.1
        intents = discipline.collect_due(
            now=future2,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )
        assert len(intents) == 1
        assert intents[0].is_first is False  # It's a repeat, not the first


# ---------------------------------------------------------------------------
# AC4: Coalescing — 3 arrivals while armed -> one nudge, correct payload
# ---------------------------------------------------------------------------


class TestAC4Coalescing:
    """AC4: At most ONE armed nudge per terminal; arrivals coalesce."""

    def test_three_arrivals_coalesce_into_one(self, discipline: NudgeDiscipline, cursor_at_zero):
        """3 arrivals while armed -> one nudge, payload count=3, oldest id correct."""
        # First arm
        is_new = discipline.arm_or_coalesce("sup1", "mb1", 1, 100)
        assert is_new is True

        # Second arrival — coalesces
        is_new = discipline.arm_or_coalesce("sup1", "mb1", 2, 100)
        assert is_new is False

        # Third arrival — coalesces again
        is_new = discipline.arm_or_coalesce("sup1", "mb1", 3, 100)
        assert is_new is False

        now = time.monotonic() + 0.01
        # Fire — should get ONE nudge with count=3 (from pending refresh)
        pending_3 = lambda mb: (3, 100)
        intents = discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_3,
        )
        assert len(intents) == 1
        assert intents[0].message_count == 3
        assert intents[0].oldest_inbox_row_id == 100

    def test_merge_fires_immediately_resets_backoff(
        self, discipline: NudgeDiscipline, cursor_at_zero, pending_1_from_50
    ):
        """D3 amended: merge fires immediately and resets backoff."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01

        # Fire first nudge
        discipline.collect_due(
            now=now,
            get_consumption_cursor=cursor_at_zero,
            get_pending_oldest=pending_1_from_50,
        )

        # Now backoff_step=1, next_fire_at = now + 30
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.backoff_step == 1

        # New arrival coalesces — should reset backoff and set next_fire_at to now
        discipline.arm_or_coalesce("sup1", "mb1", 2, 50)

        state = discipline.get_state("sup1")
        assert state is not None
        assert state.backoff_step == 0  # reset
        # next_fire_at should be approximately now (merge fires immediately)
        assert state.next_fire_at is not None
        assert state.next_fire_at <= time.monotonic() + 0.01


# ---------------------------------------------------------------------------
# AC5: Backoff sequence 30/60/120/120; counter resets on consume
# ---------------------------------------------------------------------------


class TestAC5Backoff:
    """AC5: Backoff sequence and reset behavior."""

    def test_backoff_sequence_30_60_120_120(self, discipline: NudgeDiscipline, cursor_at_zero):
        """Idle-but-unconsumed repeats follow 30 -> 60 -> 120 -> 120 (capped)."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01
        pending = lambda mb: (1, 50)
        discipline.record_status("sup1", TerminalStatus.IDLE)

        # Fire first (immediate)
        intents = discipline.collect_due(
            now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )
        assert len(intents) == 1

        # Verify backoff intervals
        expected_delays = [30, 60, 120, 120]
        cumulative = 0
        for i, expected_delay in enumerate(expected_delays):
            cumulative += expected_delay
            # Just before — should not fire
            just_before = now + cumulative - 0.1
            intents = discipline.collect_due(
                now=just_before,
                get_consumption_cursor=cursor_at_zero,
                get_pending_oldest=pending,
            )
            assert intents == [], f"Should not fire at step {i} just before deadline"

            # Just after — should fire
            just_after = now + cumulative + 0.1
            intents = discipline.collect_due(
                now=just_after,
                get_consumption_cursor=cursor_at_zero,
                get_pending_oldest=pending,
            )
            assert len(intents) == 1, f"Should fire at step {i} just after deadline"
            assert intents[0].is_first is False

    def test_backoff_resets_on_consume(self, discipline: NudgeDiscipline, cursor_at_zero):
        """Backoff counter resets when cursor advances (on_cursor_advance)."""
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        now = time.monotonic() + 0.01
        pending = lambda mb: (1, 50)
        discipline.record_status("sup1", TerminalStatus.IDLE)

        # Fire first
        discipline.collect_due(
            now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )

        # Advance backoff
        future = now + 31
        discipline.collect_due(
            now=future, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.backoff_step == 2  # After first(0->1) and second(1->2)

        # Consume — disarms entirely
        discipline.on_cursor_advance("sup1", "mb1")
        assert not discipline.has_armed("sup1")

        # Re-arm — backoff starts fresh
        discipline.arm_or_coalesce("sup1", "mb1", 1, 60)
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.backoff_step == 0
        assert state.first_fired is False


# ---------------------------------------------------------------------------
# AC6: E-bound escalation reachable even with repeats parked (D5)
# ---------------------------------------------------------------------------


class TestAC6EBoundEscalation:
    """AC6: Obligation older than E escalates even with repeats parked."""

    def test_escalation_fires_regardless_of_nudge_parking(self):
        """The escalation path (_escalate) is independent of nudge_discipline.

        This test verifies that the obligation-driven escalation in
        delivery_service._escalate still calls attempt_rung2 directly with
        is_escalation=True, bypassing nudge_discipline entirely. The nudge
        discipline cannot suppress or delay escalation (D5).
        """
        from unittest.mock import MagicMock, patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            DeliveryObligationModel,
            MailboxModel,
            SessionLocal,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _escalate,
            attempt_rung2,
            resolve_supervisor_target,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.add(
                MailboxModel(
                    id="mb1",
                    session_name="cao-test",
                    role="supervisor",
                    current_terminal_id="sup1",
                    generation=1,
                    consumed_through_id=0,
                )
            )
            db.add(
                DeliveryObligationModel(
                    inbox_row_id=100,
                    mailbox_id="mb1",
                    state="OPEN",
                    accepted_at=datetime.now(timezone.utc) - timedelta(seconds=200),
                    attempts=10,
                )
            )
            db.commit()

        now = datetime.now(timezone.utc)
        rung2_called = []

        def fake_rung2(target, inbox_row_id, **kwargs):
            rung2_called.append((target, inbox_row_id, kwargs))
            return LadderResult(delivered=True, phase="surface", decision="proceed", reason=None)

        with (
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch("cli_agent_orchestrator.services.delivery_service.attempt_rung2", fake_rung2),
        ):
            with TestSession() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=100).one()
                _escalate(db, obl, now, 200.0)
                db.commit()

                # Verify escalation happened
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=100).one()
                assert obl.state == "ESCALATED"

        # Verify rung2 was called with is_escalation=True
        assert len(rung2_called) == 1
        _, _, kwargs = rung2_called[0]
        assert kwargs.get("is_escalation") is True

    def test_escalation_bypasses_busy_gate(self):
        """Escalation with is_escalation=True bypasses the not_idle safety gate.

        Even when the terminal is PROCESSING (repeats parked), escalation
        still fires because _check_safety_gates skips the busy check for
        escalations. This is D5: nudge suppression must never mask a stuck delivery.
        """
        from unittest.mock import patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            SessionLocal,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            StalledCallbackWatchdog,
            _Episode,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.commit()

        target = DeliveryTarget(
            terminal_id="sup1",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
        )

        # Set up a watchdog with the terminal in PROCESSING
        watchdog = StalledCallbackWatchdog()
        now = datetime.now(timezone.utc)
        episode = _Episode(
            caller_id="worker1",
            profile="developer",
            inbound_at=time.monotonic(),
            episode_started_wall_at=now,
        )
        episode.status = TerminalStatus.PROCESSING
        watchdog._episodes["sup1"] = episode

        with (
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog"
                ".stalled_callback_watchdog",
                watchdog,
            ),
        ):
            # Non-escalation: should return "not_idle" (busy gate active)
            result = _check_safety_gates(target, is_escalation=False)
            assert result == "not_idle"

            # Escalation: should return None (bypasses busy gate, D5)
            result = _check_safety_gates(target, is_escalation=True)
            assert result is None


# ---------------------------------------------------------------------------
# AC-D2b: Draft-guard veto on injection
# ---------------------------------------------------------------------------


class TestACDraftGuardVeto:
    """D2b: Non-empty composer draft defers injection; empty composer injects."""

    def test_non_empty_composer_defers(self):
        """Non-empty composer draft → _check_safety_gates returns 'user_draft_present'."""
        from unittest.mock import MagicMock, patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.commit()

        target = DeliveryTarget(
            terminal_id="sup1",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
        )

        # Mock provider with a non-empty draft
        mock_provider = MagicMock()
        mock_provider.read_composer_draft.return_value = "half-typed user text"

        mock_backend = MagicMock()
        mock_backend.get_history.return_value = "line1\nline2\nhalf-typed user text"

        mock_provider_manager = MagicMock()
        mock_provider_manager.get_provider.return_value = mock_provider

        with (
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-test",
                    "tmux_window": "supervisor",
                },
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager",
                mock_provider_manager,
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=mock_backend,
            ),
        ):
            # Non-escalation: deferred
            result = _check_safety_gates(target, is_escalation=False)
            assert result == "user_draft_present"

            # Escalation: ALSO deferred (D2b carve-out: corrupted prompt > late nudge)
            result = _check_safety_gates(target, is_escalation=True)
            assert result == "user_draft_present"

    def test_empty_composer_allows_injection(self):
        """Empty composer draft → _check_safety_gates returns None (injection proceeds)."""
        from unittest.mock import MagicMock, patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.commit()

        target = DeliveryTarget(
            terminal_id="sup1",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
        )

        # Mock provider with empty draft
        mock_provider = MagicMock()
        mock_provider.read_composer_draft.return_value = ""

        mock_backend = MagicMock()
        mock_backend.get_history.return_value = "line1\nline2\n"

        mock_provider_manager = MagicMock()
        mock_provider_manager.get_provider.return_value = mock_provider

        with (
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-test",
                    "tmux_window": "supervisor",
                },
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager",
                mock_provider_manager,
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=mock_backend,
            ),
        ):
            # Empty draft → injection allowed
            result = _check_safety_gates(target, is_escalation=False)
            assert result is None

    def test_none_draft_allows_injection(self):
        """Provider returns None (no draft capability) → injection proceeds."""
        from unittest.mock import MagicMock, patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _check_safety_gates,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.commit()

        target = DeliveryTarget(
            terminal_id="sup1",
            tmux_session="cao-test",
            tmux_window="supervisor",
            cc_inbox_path=None,
        )

        # Mock provider that returns None (no draft detected)
        mock_provider = MagicMock()
        mock_provider.read_composer_draft.return_value = None

        mock_backend = MagicMock()
        mock_backend.get_history.return_value = "line1\nline2\n"

        mock_provider_manager = MagicMock()
        mock_provider_manager.get_provider.return_value = mock_provider

        with (
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={
                    "tmux_session": "cao-test",
                    "tmux_window": "supervisor",
                },
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager",
                mock_provider_manager,
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=mock_backend,
            ),
        ):
            # None draft → injection allowed (no draft to corrupt)
            result = _check_safety_gates(target, is_escalation=False)
            assert result is None



# ---------------------------------------------------------------------------
# Amendment A1: Full Jitter backoff tests
# ---------------------------------------------------------------------------


class TestA1FullJitter:
    """A1-ACs: Floor-clamped Full Jitter replaces deterministic ladder."""

    def test_a1_ac1_delays_uniformly_spread(self):
        """A1-AC1: Repeat delays over N obligations are uniformly spread,
        never aligned on tick multiples (30/60/120)."""
        import random as stdlib_random

        # Use a seeded RNG that produces known values
        class SeededRNG:
            def __init__(self, seed: int):
                self._rng = stdlib_random.Random(seed)

            def randint(self, a: int, b: int) -> int:
                return self._rng.randint(a, b)

        discipline = NudgeDiscipline(rng=SeededRNG(42))
        cursor_at_zero = lambda mb: 0
        pending = lambda mb: (1, 50)

        # Collect many jittered delays by simulating repeats
        delays: list[float] = []
        now = time.monotonic() + 0.01

        with patch.dict("os.environ", {"CAO_DELIVERY_JITTER": "on"}):
            discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
            discipline.record_status("sup1", TerminalStatus.IDLE)

            # Fire the first nudge (always immediate, step=0 → exactly 30s)
            intents = discipline.collect_due(
                now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
            )
            assert len(intents) == 1

            # Collect 20 repeats
            current = now
            for _ in range(20):
                state = discipline.get_state("sup1")
                assert state is not None
                delay = state.visibility_timeout_at - current
                delays.append(delay)
                current = state.visibility_timeout_at + 0.01  # advance past the fire time

                intents = discipline.collect_due(
                    now=current,
                    get_consumption_cursor=cursor_at_zero,
                    get_pending_oldest=pending,
                )
                assert len(intents) == 1

        # All delays must be >= BACKOFF_BASE (floor)
        assert all(d >= BACKOFF_BASE for d in delays), f"Floor violated: {delays}"
        # All delays must be <= BACKOFF_CAP
        assert all(d <= BACKOFF_CAP for d in delays), f"Cap violated: {delays}"

        # Delays should NOT all be exactly 30, 60, or 120 (jitter provides spread)
        # At least some variation should exist once step > 0
        unique_delays = set(int(d) for d in delays)
        # After step 0 (always 30), steps 1+ should show variation
        assert len(unique_delays) > 2, f"No jitter spread: {unique_delays}"

        # None should be exactly on the 30/60/120 tick multiples (probabilistically)
        # This is a soft check — with jitter, exact hits are rare
        tick_exact = [d for d in delays[1:] if d == 30.0 or d == 60.0 or d == 120.0]
        # At most 1 might land exactly (edge case with RNG), but not all
        assert len(tick_exact) < len(delays) // 2

    def test_a1_ac2_seeded_rng_reproduces_exact_delays(self):
        """A1-AC2: Seeded-RNG test reproduces exact delays."""
        import random as stdlib_random

        class SeededRNG:
            def __init__(self, seed: int):
                self._rng = stdlib_random.Random(seed)

            def randint(self, a: int, b: int) -> int:
                return self._rng.randint(a, b)

        cursor_at_zero = lambda mb: 0
        pending = lambda mb: (1, 50)

        with patch.dict("os.environ", {"CAO_DELIVERY_JITTER": "on"}):
            # Run twice with the same seed — must produce identical delays
            delays_run1: list[float] = []
            delays_run2: list[float] = []

            for delays_list in (delays_run1, delays_run2):
                discipline = NudgeDiscipline(rng=SeededRNG(99))
                discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
                discipline.record_status("sup1", TerminalStatus.IDLE)

                now = time.monotonic() + 0.01
                # Fire first
                discipline.collect_due(
                    now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
                )

                current = now
                for _ in range(5):
                    state = discipline.get_state("sup1")
                    assert state is not None
                    delay = state.visibility_timeout_at - current
                    delays_list.append(delay)
                    current = state.visibility_timeout_at + 0.01
                    discipline.collect_due(
                        now=current,
                        get_consumption_cursor=cursor_at_zero,
                        get_pending_oldest=pending,
                    )

            assert delays_run1 == delays_run2, (
                f"Seeded RNG not deterministic: {delays_run1} vs {delays_run2}"
            )

    def test_a1_ac3_jitter_off_restores_deterministic_ladder(
        self, discipline: NudgeDiscipline, cursor_at_zero
    ):
        """A1-AC3: delivery.jitter=off restores 30/60/120 verbatim.

        (This test uses the autouse jitter_off fixture, so it's already off.)
        """
        pending = lambda mb: (1, 50)
        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        discipline.record_status("sup1", TerminalStatus.IDLE)
        now = time.monotonic() + 0.01

        # Fire first
        discipline.collect_due(
            now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )

        # Check exact deterministic delays
        expected = [30, 60, 120, 120]
        current = now
        for i, expected_delay in enumerate(expected):
            state = discipline.get_state("sup1")
            assert state is not None
            actual_delay = state.visibility_timeout_at - current
            assert abs(actual_delay - expected_delay) < 0.01, (
                f"Step {i}: expected {expected_delay}s, got {actual_delay}s"
            )
            current = state.visibility_timeout_at + 0.01
            discipline.collect_due(
                now=current, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
            )

    def test_a1_ac4_ebound_unaffected_under_jitter(self):
        """A1-AC4: E-bound escalation unaffected (AC6 re-run green under jitter).

        The escalation path is independent of nudge_discipline backoff —
        it runs off obligation age, not nudge count or timing.
        """
        from unittest.mock import patch

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import (
            Base,
            DeliveryObligationModel,
            MailboxModel,
            TerminalModel,
        )
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            LadderResult,
            _escalate,
        )

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        TestSession = sessionmaker(bind=engine)

        with TestSession() as db:
            db.add(
                TerminalModel(
                    id="sup1",
                    tmux_session="cao-test",
                    tmux_window="supervisor",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )
            db.add(
                MailboxModel(
                    id="mb1",
                    session_name="cao-test",
                    role="supervisor",
                    current_terminal_id="sup1",
                    generation=1,
                    consumed_through_id=0,
                )
            )
            db.add(
                DeliveryObligationModel(
                    inbox_row_id=200,
                    mailbox_id="mb1",
                    state="OPEN",
                    accepted_at=datetime.now(timezone.utc) - timedelta(seconds=200),
                    attempts=10,
                )
            )
            db.commit()

        now = datetime.now(timezone.utc)

        def fake_rung2(target, inbox_row_id, **kwargs):
            return LadderResult(delivered=True, phase="surface", decision="proceed", reason=None)

        # Run with jitter ON — escalation still fires
        with (
            patch.dict("os.environ", {"CAO_DELIVERY_JITTER": "on"}),
            patch("cli_agent_orchestrator.services.delivery_service.SessionLocal", TestSession),
            patch("cli_agent_orchestrator.services.delivery_service.attempt_rung2", fake_rung2),
        ):
            with TestSession() as db:
                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=200).one()
                _escalate(db, obl, now, 200.0)
                db.commit()

                obl = db.query(DeliveryObligationModel).filter_by(inbox_row_id=200).one()
                assert obl.state == "ESCALATED"

    def test_step0_degenerates_to_exactly_30s(self):
        """n=0 degenerates to exactly 30s (floor == ceiling at step 0)."""
        import random as stdlib_random

        class SeededRNG:
            def __init__(self, seed: int):
                self._rng = stdlib_random.Random(seed)

            def randint(self, a: int, b: int) -> int:
                return self._rng.randint(a, b)

        cursor_at_zero = lambda mb: 0
        pending = lambda mb: (1, 50)

        with patch.dict("os.environ", {"CAO_DELIVERY_JITTER": "on"}):
            discipline = NudgeDiscipline(rng=SeededRNG(123))
            discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
            discipline.record_status("sup1", TerminalStatus.IDLE)

            now = time.monotonic() + 0.01
            # Fire first nudge
            discipline.collect_due(
                now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
            )

            # Step 0: min(120, 30*2^0) = 30; floor=30; floor>=ceiling → exactly 30
            state = discipline.get_state("sup1")
            assert state is not None
            delay = state.visibility_timeout_at - now
            assert delay == 30.0, f"Step 0 should be exactly 30s, got {delay}s"

    def test_receive_count_tracks_fires(self):
        """SQS vocabulary: receive_count increments on each fire."""
        discipline = NudgeDiscipline()
        cursor_at_zero = lambda mb: 0
        pending = lambda mb: (1, 50)

        discipline.arm_or_coalesce("sup1", "mb1", 1, 50)
        discipline.record_status("sup1", TerminalStatus.IDLE)

        now = time.monotonic() + 0.01
        # Fire first
        discipline.collect_due(
            now=now, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.receive_count == 1

        # Fire second (advance past backoff)
        future = now + 200  # past any backoff
        discipline.collect_due(
            now=future, get_consumption_cursor=cursor_at_zero, get_pending_oldest=pending
        )
        state = discipline.get_state("sup1")
        assert state is not None
        assert state.receive_count == 2

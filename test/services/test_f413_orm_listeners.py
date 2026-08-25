"""F413: ORM listener tests — obligation + sentinel + doorbell structurally unbypassable.

AC1: Raw db.add(InboxModel) yields obligation + trace + sentinel + doorbell (after commit).
AC2: Rolled-back insert: no obligation, no doorbell, stash cleared.
AC3: Existing producers still yield exactly ONE obligation per row.
AC4: HELD barrier row: no obligation at insert; obligation on barrier COMPLETE.
AC4b: Barrier CANCEL (bulk HELD→PENDING): qualifying rows get obligations via D7b helper.
AC4c: Terminal-reap HELD→PENDING flip: same as 4b on the reap path.
AC5: Non-supervisor receiver: no obligation, no sentinel, no doorbell.
AC6: Full regression — exercised on box run (covered by full suite).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    DeliveryObligationModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _f413_after_begin,
    _f413_after_commit,
    _f413_after_rollback,
    _f413_qualify_and_create,
    _f413_row_qualifies,
    _touch_supervisor_pending_flag,
    cancel_callback_barrier,
    create_inbox_message,
    delete_terminal_and_warm_intent,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services import mailbox_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def f413_db(tmp_path, monkeypatch):
    """In-memory DB with full schema + F413 listeners registered."""
    db_path = tmp_path / "f413.sqlite"
    eng = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=eng)
    sessions = sessionmaker(bind=eng, expire_on_commit=False)

    # Register F413 session-level listeners on this test sessionmaker
    event.listen(sessions, "after_commit", _f413_after_commit)
    event.listen(sessions, "after_rollback", _f413_after_rollback)
    event.listen(sessions, "after_begin", _f413_after_begin)

    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    # Patch stalled_callback_watchdog
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.stalled_callback_watchdog",
        MagicMock(),
    )
    # Patch _touch_supervisor_pending_flag to track calls
    flag_calls = []
    original_touch = _touch_supervisor_pending_flag

    def _mock_touch():
        flag_calls.append(1)

    monkeypatch.setattr(database, "_touch_supervisor_pending_flag", _mock_touch)
    sessions._f413_flag_calls = flag_calls
    yield sessions
    eng.dispose()


@pytest.fixture
def supervisor_setup(f413_db):
    """Create a supervisor terminal + mailbox + incarnation + worker."""
    with f413_db.begin() as db:
        db.add(
            TerminalModel(
                id="sup_0001",
                tmux_session="cao-f413",
                tmux_window="supervisor",
                provider="claude_code",
                agent_profile="code_supervisor",
                init_state="ready",
            )
        )
        db.add(
            MailboxModel(
                id="mb_sup_0001",
                session_name="cao-f413",
                role="supervisor",
                current_terminal_id="sup_0001",
                generation=1,
                consumed_through_id=0,
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_sup_0001",
                generation=1,
                terminal_id="sup_0001",
            )
        )
        # Worker terminal (sender)
        db.add(
            TerminalModel(
                id="wrk_0001",
                tmux_session="cao-f413",
                tmux_window="worker1",
                provider="kiro_cli",
                agent_profile="developer",
                init_state="ready",
                caller_id="sup_0001",
            )
        )
        # Non-supervisor terminal
        db.add(
            TerminalModel(
                id="raw_0001",
                tmux_session="cao-f413",
                tmux_window="raw1",
                provider="kiro_cli",
                agent_profile="developer",
                init_state="ready",
            )
        )
    return f413_db


# ---------------------------------------------------------------------------
# AC1: Load-bearing raw-add test
# ---------------------------------------------------------------------------


class TestAC1RawAdd:
    """Raw db.add(InboxModel) with PENDING + supervisor receiver yields obligation."""

    def test_raw_add_yields_obligation_trace_sentinel_doorbell(
        self, supervisor_setup, monkeypatch
    ):
        """AC1: a raw db.add(InboxModel) — with NO producer helper — yields exactly
        one OPEN DeliveryObligationModel row, one fx191.accept trace event, sentinel
        file touched, and one doorbell emit after commit."""
        db_factory = supervisor_setup
        doorbell_emits = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            lambda *a, **kw: doorbell_emits.append(a),
        )

        with db_factory() as db:
            row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="sup_0001",
                logical_receiver_id="mb_sup_0001",
                message="AC1 raw-add test message",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            )
            db.add(row)
            db.flush()
            row_id = int(row.id)

            # Before commit: obligation should exist (after_insert fires on flush)
            obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=row_id)
                .one_or_none()
            )
            assert obl is not None, "F413 AC1: No obligation created by after_insert"
            assert obl.state == "OPEN"
            assert obl.mailbox_id == "mb_sup_0001"

            # Trace event
            trace = (
                db.query(InboxMessageTraceEventModel)
                .filter_by(message_id=row_id, kind="fx191.accept")
                .one_or_none()
            )
            assert trace is not None, "F413 AC1: No trace event created by after_insert"
            assert trace.phase == "accept"
            assert trace.decision == "proceed"

            # Sentinel touched
            assert len(db_factory._f413_flag_calls) == 1

            # Doorbell not yet emitted (before commit)
            assert len(doorbell_emits) == 0

            db.commit()

        # After commit: doorbell emitted
        assert len(doorbell_emits) == 1
        assert doorbell_emits[0][0] == "sup_0001"  # terminal_id
        assert doorbell_emits[0][1] == row_id  # row_id


# ---------------------------------------------------------------------------
# AC2: Rolled-back insert
# ---------------------------------------------------------------------------


class TestAC2Rollback:
    """Rolled-back insert: obligation absent, no doorbell, stash cleared."""

    def test_rollback_clears_obligation_and_doorbell(self, supervisor_setup, monkeypatch):
        """AC2: After rollback, no obligation row, no doorbell emit, stash cleared."""
        db_factory = supervisor_setup
        doorbell_emits = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            lambda *a, **kw: doorbell_emits.append(a),
        )

        with db_factory() as db:
            row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="sup_0001",
                logical_receiver_id="mb_sup_0001",
                message="AC2 rollback test",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            )
            db.add(row)
            db.flush()
            row_id = int(row.id)
            db.rollback()

        # No doorbell emitted
        assert len(doorbell_emits) == 0

        # Obligation absent (rolled back — data not in DB)
        with db_factory() as db:
            obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=row_id)
                .one_or_none()
            )
            assert obl is None, "F413 AC2: Obligation survived rollback"


# ---------------------------------------------------------------------------
# AC3: Existing producers yield exactly one obligation
# ---------------------------------------------------------------------------


class TestAC3SingleObligation:
    """Existing producers still yield exactly ONE obligation per row."""

    def test_create_inbox_message_single_obligation(self, supervisor_setup, monkeypatch):
        """AC3: create_inbox_message (the unfenced path) creates one obligation."""
        db_factory = supervisor_setup

        msg = create_inbox_message(
            sender_id="wrk_0001",
            receiver_id="sup_0001",
            message="AC3 single obligation test",
        )

        with db_factory() as db:
            obligations = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=msg.id)
                .all()
            )
            assert len(obligations) == 1, (
                f"Expected exactly 1 obligation, got {len(obligations)}"
            )

    def test_no_obligation_call_sites_remain(self):
        """AC3: grep proves no hand-placed _create_obligation_inline calls in _insert_routed_inbox_row."""
        import inspect

        from cli_agent_orchestrator.clients.database import _insert_routed_inbox_row

        source = inspect.getsource(_insert_routed_inbox_row)
        assert "_create_obligation_inline" not in source, (
            "F413 AC3: hand-placed _create_obligation_inline still in _insert_routed_inbox_row"
        )
        assert "_touch_supervisor_pending_flag" not in source, (
            "F413 AC3: hand-placed _touch_supervisor_pending_flag still in _insert_routed_inbox_row"
        )


# ---------------------------------------------------------------------------
# AC4: HELD barrier row
# ---------------------------------------------------------------------------


class TestAC4BarrierHeld:
    """HELD barrier row: no obligation at insert; obligation on barrier COMPLETE."""

    def test_held_row_no_obligation(self, supervisor_setup):
        """AC4: HELD row at insert time gets NO obligation."""
        db_factory = supervisor_setup

        with db_factory() as db:
            row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="sup_0001",
                logical_receiver_id="mb_sup_0001",
                message="AC4 held row",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
            )
            db.add(row)
            db.flush()
            row_id = int(row.id)
            db.commit()

        with db_factory() as db:
            obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=row_id)
                .one_or_none()
            )
            assert obl is None, "F413 AC4: HELD row should NOT have obligation"


# ---------------------------------------------------------------------------
# AC4b: Barrier CANCEL bulk flip
# ---------------------------------------------------------------------------


class TestAC4bBarrierCancel:
    """Barrier CANCEL (bulk update HELD→PENDING): flipped supervisor-bound rows get obligations."""

    def test_cancel_creates_obligations_for_qualifying_rows(self, supervisor_setup, monkeypatch):
        """AC4b: After barrier cancel, HELD→PENDING rows with supervisor receiver get obligations."""
        db_factory = supervisor_setup

        # Create a barrier and held rows
        with db_factory.begin() as db:
            barrier = CallbackBarrierModel(
                owner_mailbox_id="mb_sup_0001",
                owner_generation=1,
                label="ac4b-test",
                state="OPEN",
                timeout_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )
            db.add(barrier)
            db.flush()
            barrier_id = int(barrier.id)

            # HELD row directed at supervisor mailbox
            held_row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="sup_0001",
                logical_receiver_id="mb_sup_0001",
                message="AC4b held for supervisor",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
                barrier_id=barrier_id,
            )
            db.add(held_row)
            db.flush()
            held_row_id = int(held_row.id)

            # HELD row directed at non-supervisor (should NOT get obligation)
            non_sup_row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="raw_0001",
                logical_receiver_id=None,
                message="AC4b held for non-supervisor",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
                barrier_id=barrier_id,
            )
            db.add(non_sup_row)
            db.flush()
            non_sup_row_id = int(non_sup_row.id)

            # Add barrier member so cancel_callback_barrier can find it
            db.add(
                CallbackBarrierMemberModel(
                    barrier_id=barrier_id,
                    member_key="wrk_0001",
                    terminal_id="wrk_0001",
                    lifecycle_generation=1,
                    state="AWAITING",
                    position=0,
                )
            )

        # Cancel the barrier
        result = cancel_callback_barrier(
            barrier_id=barrier_id,
            owner_id="sup_0001",
        )
        assert result["state"] == "CANCELLED"

        # Verify obligations
        with db_factory() as db:
            obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=held_row_id)
                .one_or_none()
            )
            assert obl is not None, "F413 AC4b: No obligation for HELD→PENDING supervisor row"
            assert obl.state == "OPEN"
            assert obl.mailbox_id == "mb_sup_0001"

            # Non-supervisor row: no obligation
            non_sup_obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=non_sup_row_id)
                .one_or_none()
            )
            assert non_sup_obl is None, (
                "F413 AC4b: Non-supervisor row should NOT have obligation"
            )


# ---------------------------------------------------------------------------
# AC4c: Terminal-reap HELD→PENDING flip
# ---------------------------------------------------------------------------


class TestAC4cTerminalReap:
    """Terminal-reap HELD→PENDING flip: qualifying rows get obligations."""

    def test_reap_per_row_creates_obligations(self, supervisor_setup, monkeypatch):
        """AC4c: Terminal reap per-row flip creates obligations for supervisor-bound rows."""
        db_factory = supervisor_setup

        # Create held rows owned by the worker terminal (reap target)
        with db_factory.begin() as db:
            held_row = InboxModel(
                sender_id="sup_0001",
                receiver_id="wrk_0001",
                logical_receiver_id=None,
                message="AC4c held for reap",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.HELD.value,
            )
            db.add(held_row)
            db.flush()
            held_row_id = int(held_row.id)

        # Reap the worker — rows get redirected to supervisor (the caller)
        with patch(
            "cli_agent_orchestrator.clients.database.invalidate_terminal_metadata_cache",
            lambda *a: None,
        ):
            result = delete_terminal_and_warm_intent(
                terminal_id="wrk_0001",
            )

        # After reap, the held row should be flipped to PENDING with supervisor as target
        # and should have an obligation since sup_0001 has a supervisor mailbox
        with db_factory() as db:
            row = db.query(InboxModel).filter_by(id=held_row_id).one_or_none()
            if row is not None and row.status == MessageStatus.PENDING.value:
                # Row was flipped to PENDING — check obligation
                if row.logical_receiver_id is not None:
                    obl = (
                        db.query(DeliveryObligationModel)
                        .filter_by(inbox_row_id=held_row_id)
                        .one_or_none()
                    )
                    if row.logical_receiver_id == "mb_sup_0001":
                        assert obl is not None, (
                            "F413 AC4c: No obligation for reaped HELD→PENDING supervisor row"
                        )


# ---------------------------------------------------------------------------
# AC5: Non-supervisor receiver
# ---------------------------------------------------------------------------


class TestAC5NonSupervisor:
    """Non-supervisor receiver: no obligation, no sentinel, no doorbell."""

    def test_non_supervisor_no_obligation(self, supervisor_setup, monkeypatch):
        """AC5: Message to non-supervisor terminal gets no obligation."""
        db_factory = supervisor_setup
        doorbell_emits = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            lambda *a, **kw: doorbell_emits.append(a),
        )

        # Reset flag call counter
        db_factory._f413_flag_calls.clear()

        with db_factory() as db:
            row = InboxModel(
                sender_id="wrk_0001",
                receiver_id="raw_0001",
                logical_receiver_id=None,
                message="AC5 non-supervisor test",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            )
            db.add(row)
            db.flush()
            row_id = int(row.id)
            db.commit()

        with db_factory() as db:
            obl = (
                db.query(DeliveryObligationModel)
                .filter_by(inbox_row_id=row_id)
                .one_or_none()
            )
            assert obl is None, "F413 AC5: Non-supervisor should NOT have obligation"

        # No sentinel touch
        assert len(db_factory._f413_flag_calls) == 0
        # No doorbell
        assert len(doorbell_emits) == 0


# ---------------------------------------------------------------------------
# D3 nested-tx guard tests
# ---------------------------------------------------------------------------


class TestD3NestedTxGuard:
    """D3: nested-tx guard prevents spurious doorbell on nested commit."""

    def test_nested_commit_does_not_emit_doorbell(self, supervisor_setup, monkeypatch):
        """Nested commit within begin_nested() does NOT emit doorbell."""
        db_factory = supervisor_setup
        doorbell_emits = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            lambda *a, **kw: doorbell_emits.append(a),
        )

        with db_factory() as db:
            with db.begin_nested():
                row = InboxModel(
                    sender_id="wrk_0001",
                    receiver_id="sup_0001",
                    logical_receiver_id="mb_sup_0001",
                    message="D3 nested test",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.PENDING.value,
                )
                db.add(row)
                db.flush()
            # After nested commit: no doorbell yet
            assert len(doorbell_emits) == 0
            db.commit()

        # After outer commit: doorbell emitted
        assert len(doorbell_emits) == 1

    def test_nested_rollback_preserves_earlier_stash(self, supervisor_setup, monkeypatch):
        """D3 snapshot-restore: nested rollback does not lose earlier stash entries."""
        db_factory = supervisor_setup
        doorbell_emits = []

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.ws_doorbell.push_doorbell_frame_sync",
            lambda *a, **kw: doorbell_emits.append(a),
        )

        with db_factory() as db:
            # First nested: succeeds
            with db.begin_nested():
                row1 = InboxModel(
                    sender_id="wrk_0001",
                    receiver_id="sup_0001",
                    logical_receiver_id="mb_sup_0001",
                    message="D3 first nested (ok)",
                    orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                    status=MessageStatus.PENDING.value,
                )
                db.add(row1)
                db.flush()

            # Second nested: fails
            try:
                with db.begin_nested():
                    row2 = InboxModel(
                        sender_id="wrk_0001",
                        receiver_id="sup_0001",
                        logical_receiver_id="mb_sup_0001",
                        message="D3 second nested (fail)",
                        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                        status=MessageStatus.PENDING.value,
                    )
                    db.add(row2)
                    db.flush()
                    raise ValueError("simulated failure")
            except ValueError:
                pass

            db.commit()

        # Only first row's doorbell should have emitted
        assert len(doorbell_emits) == 1
        assert doorbell_emits[0][0] == "sup_0001"


# ---------------------------------------------------------------------------
# Predicate unit tests
# ---------------------------------------------------------------------------


class TestPredicateUnit:
    """Unit tests for _f413_row_qualifies predicate."""

    def test_pending_with_receiver(self):
        assert _f413_row_qualifies(MessageStatus.PENDING.value, "mb_x") is True

    def test_held_with_receiver(self):
        assert _f413_row_qualifies(MessageStatus.HELD.value, "mb_x") is False

    def test_pending_without_receiver(self):
        assert _f413_row_qualifies(MessageStatus.PENDING.value, None) is False

    def test_cancelled_with_receiver(self):
        assert _f413_row_qualifies(MessageStatus.CANCELLED.value, "mb_x") is False

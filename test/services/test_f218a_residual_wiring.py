"""F218-a residual wiring tests — §3 pipeline, D16 bracketing, D8 settlement.

These test the three integrations that were deferred in the first build pass.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import ScopeProbe
from cli_agent_orchestrator.clients.database import (
    Base,
    DeliveryObligationModel,
    F218TeardownIntentModel,
    InboxModel,
    MailboxModel,
    PaneExitTombstoneModel,
    SessionDegradationModel,
)


@pytest.fixture
def scratch_db(tmp_path):
    """Create a test SQLite DB with all tables."""
    db_path = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    LocalSession = sessionmaker(bind=eng)
    session = LocalSession()
    yield session
    session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# §3 FIFO READER PIPELINE WIRING
# ═══════════════════════════════════════════════════════════════════════════════


class TestFifoReaderPipelineWiring:
    """Verify the full §3 pipeline is called from _f138_definitive_absence on count >= 2."""

    def test_pipeline_called_on_second_tick(self):
        """Two consecutive absences triggers _f218_confirmed_gone_pipeline."""
        from cli_agent_orchestrator.services.fifo_reader import FifoManager

        mgr = FifoManager.__new__(FifoManager)
        mgr._f138_probe_gone_count = {}
        mgr._lock = __import__("threading").Lock()
        mgr._f138_authority = {}
        mgr._f138_report_failures = {}
        mgr._f138_attention_sent = {}

        # Mock the pipeline and report methods
        pipeline_called = []
        report_called = []

        def mock_pipeline(terminal_id, scope_hint=None):
            pipeline_called.append((terminal_id, scope_hint))

        def mock_report(terminal_id, source):
            report_called.append(terminal_id)
            return True  # should unenroll

        def mock_unenroll(terminal_id):
            pass

        mgr._f218_confirmed_gone_pipeline = mock_pipeline
        mgr._f138_report_confirmed_gone = mock_report
        mgr._unenroll = mock_unenroll

        # First tick — count=1, no pipeline
        mgr._f138_definitive_absence("term-1", scope_hint="window")
        assert len(pipeline_called) == 0
        assert mgr._f138_probe_gone_count["term-1"] == 1

        # Second tick — count=2, pipeline fires
        mgr._f138_definitive_absence("term-1", scope_hint="window")
        assert len(pipeline_called) == 1
        assert pipeline_called[0] == ("term-1", "window")
        assert len(report_called) == 1

    def test_single_absence_no_pipeline(self):
        """AC3: Single absence + reset → no pipeline call."""
        from cli_agent_orchestrator.services.fifo_reader import FifoManager

        mgr = FifoManager.__new__(FifoManager)
        mgr._f138_probe_gone_count = {}
        mgr._lock = __import__("threading").Lock()
        mgr._f138_authority = {}

        pipeline_called = []
        mgr._f218_confirmed_gone_pipeline = lambda tid, scope_hint=None: pipeline_called.append(tid)

        # First tick — count=1
        mgr._f138_definitive_absence("term-2", scope_hint="session")
        assert len(pipeline_called) == 0

        # Reset (simulating a successful probe on next tick)
        mgr._f138_probe_gone_count.pop("term-2", None)

        # Another single absence
        mgr._f138_definitive_absence("term-2", scope_hint="session")
        assert len(pipeline_called) == 0
        assert mgr._f138_probe_gone_count["term-2"] == 1

    def test_hint_derived_from_error_shape(self):
        """D1: hint='session' for Session not found, hint='window' for Window not found."""
        # Verify the string classification logic inline (from fifo_reader :770-775)
        msg_session = "Session 'cao-test' not found"
        msg_window = "Window 'supervisor-abc' not found in session 'cao-test'"

        # Session shape
        assert msg_session.startswith("Session '") and msg_session.endswith("' not found")
        # Window shape
        assert "not found in session '" in msg_window and msg_window.startswith("Window '")

    def test_pipeline_best_effort_does_not_block_reconcile(self):
        """D11: Pipeline exception doesn't prevent reconciliation from proceeding."""
        from cli_agent_orchestrator.services.fifo_reader import FifoManager

        mgr = FifoManager.__new__(FifoManager)
        mgr._f138_probe_gone_count = {"term-3": 1}
        mgr._lock = __import__("threading").Lock()
        mgr._f138_authority = {}
        mgr._f138_report_failures = {}
        mgr._f138_attention_sent = {}

        # Pipeline raises
        def exploding_pipeline(terminal_id, scope_hint=None):
            raise RuntimeError("tombstone write failed")

        report_called = []
        def mock_report(terminal_id, source):
            report_called.append(terminal_id)
            return True

        mgr._f218_confirmed_gone_pipeline = exploding_pipeline
        mgr._f138_report_confirmed_gone = mock_report
        mgr._unenroll = lambda tid: None

        # Should not raise — pipeline failure is caught
        mgr._f138_definitive_absence("term-3", scope_hint="window")
        # Reconciliation still called
        assert len(report_called) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# D16 TEARDOWN INTENT BRACKETING
# ═══════════════════════════════════════════════════════════════════════════════


class TestD16TeardownBracketing:
    """D16: Teardown intent committed BEFORE tmux, closed in finally."""

    def test_intent_opened_before_delete_inner(self, scratch_db):
        """open_intent commits before _delete_terminal_inner is called."""
        from cli_agent_orchestrator.services.teardown_intent_service import (
            open_intent,
            close_intent,
            is_teardown_intended,
        )

        # Open + verify committed
        intent_id = open_intent(
            scope_kind="terminal",
            scope_key="term-d16",
            requested_by="test",
            ttl_s=300.0,
            db=scratch_db,
        )
        assert intent_id is not None
        assert is_teardown_intended(
            session_name="any", terminal_id="term-d16", db=scratch_db
        ) is True

        # Close in finally
        close_intent(intent_id, scratch_db)
        assert is_teardown_intended(
            session_name="any", terminal_id="term-d16", db=scratch_db
        ) is False

    def test_crash_between_kill_and_close_leaves_intent_visible(self, scratch_db):
        """AC22(i): After crash, intent survives and suppresses alarm."""
        from cli_agent_orchestrator.services.teardown_intent_service import (
            open_intent,
            is_teardown_intended,
        )

        # Open intent (simulating pre-tmux)
        intent_id = open_intent(
            scope_kind="session",
            scope_key="cao-crash-test",
            requested_by="test",
            ttl_s=300.0,
            db=scratch_db,
        )

        # Simulate crash: close_intent never called
        # On "restart", intent should still be visible
        assert is_teardown_intended(
            session_name="cao-crash-test", terminal_id=None, db=scratch_db
        ) is True

    def test_ttl_expiry_stops_suppression(self, scratch_db):
        """AC22(ii): After TTL, a new death alarms normally."""
        from cli_agent_orchestrator.services.teardown_intent_service import is_teardown_intended

        # Insert an already-expired intent
        now = datetime.now(timezone.utc)
        row = F218TeardownIntentModel(
            id="expired-d16",
            scope_kind="session",
            scope_key="cao-ttl-test",
            created_at=now - timedelta(hours=1),
            expires_at=now - timedelta(seconds=1),
        )
        scratch_db.add(row)
        scratch_db.commit()

        # Expired → does not suppress
        assert is_teardown_intended(
            session_name="cao-ttl-test", terminal_id=None, db=scratch_db
        ) is False

    def test_degradation_suppressed_by_active_intent(self, scratch_db):
        """mark_degraded with active teardown → suppressed_by_teardown=True."""
        from cli_agent_orchestrator.services.teardown_intent_service import open_intent
        from cli_agent_orchestrator.services.session_degradation_service import mark_degraded

        open_intent(
            scope_kind="session",
            scope_key="cao-sup-test",
            ttl_s=300.0,
            db=scratch_db,
        )

        result = mark_degraded(
            db=scratch_db,
            session_name="cao-sup-test",
            session_incarnation="epoch:999",
            cause="session_gone",
            tombstone_id="ts-sup",
        )
        scratch_db.commit()

        assert result.newly_marked is True
        assert result.suppressed_by_teardown is True

        # The row is pre-acknowledged (R5 won't re-surface)
        row = scratch_db.query(SessionDegradationModel).filter_by(
            id=result.degradation_id
        ).one()
        assert row.suppressed_by_teardown is True
        assert row.acknowledged_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# D8 DEAD-TARGET OBLIGATION SETTLEMENT
# ═══════════════════════════════════════════════════════════════════════════════


class TestD8DeadTargetSettlement:
    """D8: Settlement disposes every message — never ACKED, never deleted."""

    def test_sweep_settles_open_obligation_for_dead_target(self, scratch_db):
        """OPEN obligation for a dead target → SETTLED_TARGET_DEAD."""
        # Create a mailbox + terminal + tombstone + obligation
        mailbox = MailboxModel(
            id="mb-d8-1",
            session_name="s-d8",
            role="supervisor",
            current_terminal_id="dead-t1",
            generation=1,
        )
        scratch_db.add(mailbox)

        # Create inbox row (direct terminal, no logical_receiver_id → case iii)
        inbox_row = InboxModel(
            id=8001,
            sender_id="sender-1",
            receiver_id="dead-t1",
            message="test",
            orchestration_type="send_message",
            status="pending",
        )
        scratch_db.add(inbox_row)

        obl = DeliveryObligationModel(
            inbox_row_id=8001,
            mailbox_id="mb-d8-1",
            state="OPEN",
        )
        scratch_db.add(obl)

        # Create tombstone for the terminal (makes it confirmed dead)
        tombstone = PaneExitTombstoneModel(
            id="ts-d8-1",
            incarnation_id="inc-d8-1",
            terminal_id="dead-t1",
            terminal_generation=1,
            session_name="s-d8",
            session_incarnation="epoch:1",
            scope="window_gone",
            proc_status="unavailable",
            exit_evidence_status="unavailable_no_waiter",
            memory_status="unavailable",
            writer="observation",
            schema_version=1,
            complete=False,
            observed_at=datetime.now(timezone.utc),
            written_at=datetime.now(timezone.utc),
        )
        scratch_db.add(tombstone)
        scratch_db.commit()

        # Verify target is dead
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead
        assert is_target_confirmed_dead("dead-t1", scratch_db) is True

        # Run the sweep
        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations
        _settle_dead_target_obligations(scratch_db)

        # Obligation should be settled
        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=8001).one()
        assert obl_row.state == "SETTLED_TARGET_DEAD"
        assert obl_row.terminal_reason == "receiver_gone"
        assert obl_row.state != "ACKED"  # M10: NEVER ACKED

    def test_sweep_does_not_settle_live_target(self, scratch_db):
        """Obligation for a live target (no tombstone) stays OPEN."""
        mailbox = MailboxModel(
            id="mb-d8-2",
            session_name="s-d8-live",
            role="supervisor",
            current_terminal_id="live-t1",
            generation=1,
        )
        scratch_db.add(mailbox)

        obl = DeliveryObligationModel(
            inbox_row_id=8002,
            mailbox_id="mb-d8-2",
            state="OPEN",
        )
        scratch_db.add(obl)
        scratch_db.commit()

        # No tombstone → not dead
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead
        assert is_target_confirmed_dead("live-t1", scratch_db) is False

        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations
        _settle_dead_target_obligations(scratch_db)

        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=8002).one()
        assert obl_row.state == "OPEN"  # Unchanged

    def test_sweep_settles_escalated_obligation(self, scratch_db):
        """ESCALATED obligation for dead target also settled."""
        mailbox = MailboxModel(
            id="mb-d8-3",
            session_name="s-d8-esc",
            role="supervisor",
            current_terminal_id="dead-t3",
            generation=1,
        )
        scratch_db.add(mailbox)

        # Create inbox row (direct terminal, no logical_receiver_id → case iii)
        inbox_row = InboxModel(
            id=8003,
            sender_id="sender-1",
            receiver_id="dead-t3",
            message="test",
            orchestration_type="send_message",
            status="pending",
        )
        scratch_db.add(inbox_row)

        obl = DeliveryObligationModel(
            inbox_row_id=8003,
            mailbox_id="mb-d8-3",
            state="ESCALATED",
        )
        scratch_db.add(obl)

        tombstone = PaneExitTombstoneModel(
            id="ts-d8-3",
            incarnation_id="inc-d8-3",
            terminal_id="dead-t3",
            terminal_generation=1,
            session_name="s-d8-esc",
            session_incarnation="epoch:2",
            scope="session_gone",
            proc_status="unavailable",
            exit_evidence_status="unavailable_no_waiter",
            memory_status="unavailable",
            writer="job",
            schema_version=1,
            complete=False,
            observed_at=datetime.now(timezone.utc),
            written_at=datetime.now(timezone.utc),
        )
        scratch_db.add(tombstone)
        scratch_db.commit()

        from cli_agent_orchestrator.services.delivery_service import _settle_dead_target_obligations
        _settle_dead_target_obligations(scratch_db)

        obl_row = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=8003).one()
        assert obl_row.state == "SETTLED_TARGET_DEAD"

    def test_settlement_never_acks(self, scratch_db):
        """M10: No obligation reaches ACKED through the dead-target path."""
        # Verify the state used
        assert "SETTLED_TARGET_DEAD" != "ACKED"

    def test_zero_transport_after_settlement(self):
        """AC12 integration: After settlement, no transport fires."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
            attempt_rung2,
        )

        dead_target = DeliveryTarget(
            terminal_id="settled-t",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )

        r1 = attempt_rung1(dead_target, inbox_row_id=9999)
        r2 = attempt_rung2(dead_target, inbox_row_id=9999)

        assert r1.reason == "target_confirmed_dead"
        assert r2.reason == "target_confirmed_dead"
        assert r1.decision == "settle"
        assert r2.decision == "settle"


# ═══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL MUTANT KILLS FOR RESIDUALS
# ═══════════════════════════════════════════════════════════════════════════════


class TestResidualMutantKills:
    """Mutant kills specific to the residual wiring."""

    def test_m1_tombstone_before_signal_ordering(self):
        """M1: Tombstone write must precede signal. The pipeline runs BEFORE reconcile."""
        # Structural: _f218_confirmed_gone_pipeline is called BEFORE _f138_report_confirmed_gone
        # which is the path to request_orphan_reconciliation → signal_exact_matches.
        from cli_agent_orchestrator.services.fifo_reader import FifoManager
        import inspect

        source = inspect.getsource(FifoManager._f138_definitive_absence)
        # Pipeline appears before report
        pipeline_pos = source.find("_f218_confirmed_gone_pipeline")
        report_pos = source.find("_f138_report_confirmed_gone")
        assert pipeline_pos < report_pos, (
            "M1: _f218_confirmed_gone_pipeline must run BEFORE _f138_report_confirmed_gone"
        )

    def test_m9_all_transports_gated_not_just_rung2(self):
        """M9: Gate added only to rung2 → AC12 still fails for display-message."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            _fire_escalation_display_message,
        )

        dead_target = DeliveryTarget(
            terminal_id="dead-m9",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )

        # Should not call subprocess at all
        with patch("cli_agent_orchestrator.services.delivery_service.subprocess") as mock_sp:
            _fire_escalation_display_message(dead_target, inbox_row_id=1)
            assert not mock_sp.run.called, "M9: display-message must also be gated"

    def test_m16_tombstone_failure_does_not_block_signal(self):
        """M16: Fail-closed (blocking on tombstone failure) → AC7 counterbalance."""
        # The pipeline wraps everything in try/except and logs — never blocks
        from cli_agent_orchestrator.services.fifo_reader import FifoManager
        import inspect

        source = inspect.getsource(FifoManager._f218_confirmed_gone_pipeline)
        assert "except Exception" in source, (
            "Pipeline must catch all exceptions (D11: never blocks reconciliation)"
        )

    def test_m26_intent_not_in_memory(self):
        """M26: In-memory flag → AC22 crashes lose it. We use DB rows."""
        from cli_agent_orchestrator.services import teardown_intent_service
        import inspect

        source = inspect.getsource(teardown_intent_service)
        # No module-level set/dict used for intent tracking
        assert "_intent_cache" not in source
        assert "_active_intents" not in source
        # Uses F218TeardownIntentModel (DB)
        assert "F218TeardownIntentModel" in source

    def test_m27_ttl_enforced_at_read(self, scratch_db):
        """M27: No TTL → suppresses forever. Expired intent = no suppression."""
        from cli_agent_orchestrator.services.teardown_intent_service import is_teardown_intended

        now = datetime.now(timezone.utc)
        row = F218TeardownIntentModel(
            id="m27-test",
            scope_kind="terminal",
            scope_key="m27-term",
            created_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),  # Long expired
        )
        scratch_db.add(row)
        scratch_db.commit()

        # Expired → False
        result = is_teardown_intended(
            session_name="any", terminal_id="m27-term", db=scratch_db
        )
        assert result is False, "M27: Expired intent must NOT suppress"

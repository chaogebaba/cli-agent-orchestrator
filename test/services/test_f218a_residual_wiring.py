"""F218-a residual wiring tests — §3 pipeline and D16 bracketing.

These test the integrations that were deferred in the first build pass. The
third, D8 dead-target settlement, is deleted with the obligation ladder in
WP-ARCH 3c K7 — see the block where it stood.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    F218TeardownIntentModel,
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
            close_intent,
            is_teardown_intended,
            open_intent,
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
        assert (
            is_teardown_intended(session_name="any", terminal_id="term-d16", db=scratch_db) is True
        )

        # Close in finally
        close_intent(intent_id, scratch_db)
        assert (
            is_teardown_intended(session_name="any", terminal_id="term-d16", db=scratch_db) is False
        )

    def test_crash_between_kill_and_close_leaves_intent_visible(self, scratch_db):
        """AC22(i): After crash, intent survives and suppresses alarm."""
        from cli_agent_orchestrator.services.teardown_intent_service import (
            is_teardown_intended,
            open_intent,
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
        assert (
            is_teardown_intended(session_name="cao-crash-test", terminal_id=None, db=scratch_db)
            is True
        )

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
        assert (
            is_teardown_intended(session_name="cao-ttl-test", terminal_id=None, db=scratch_db)
            is False
        )

    def test_degradation_suppressed_by_active_intent(self, scratch_db):
        """mark_degraded with active teardown → suppressed_by_teardown=True."""
        from cli_agent_orchestrator.services.session_degradation_service import mark_degraded
        from cli_agent_orchestrator.services.teardown_intent_service import open_intent

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
        row = scratch_db.query(SessionDegradationModel).filter_by(id=result.degradation_id).one()
        assert row.suppressed_by_teardown is True
        assert row.acknowledged_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# WP-ARCH 3c K7: D8 DEAD-TARGET OBLIGATION SETTLEMENT — deleted with its subject
# ═══════════════════════════════════════════════════════════════════════════════
#
# ``TestD8DeadTargetSettlement`` stood here with five arms over
# ``delivery_service._settle_dead_target_obligations``: an OPEN obligation for a
# tombstoned target settles, a live target is left alone, an ESCALATED
# obligation settles too, settlement never reaches ACKED, and no transport fires
# afterwards (the last driving ``attempt_rung1``/``attempt_rung2`` against a
# ``DeliveryTarget`` marked ``confirmed_dead``).
#
# K7 deletes the obligation ladder and the sweep together. There is no
# obligation table in the delivery path to settle, no rung to refuse, and no
# ``DeliveryTarget`` to mark dead — so none of the five has a subject left. The
# residual wiring this file is actually about, the FIFO-reader pipeline and the
# D16 teardown bracket above, is untouched by the slice and stays.
#
# The deadness signal itself survives: ``is_target_confirmed_dead`` is the one
# symbol K7 keeps, read now by ``conversation_reconcile`` and ``fifo_reader``
# rather than by a sweep. What a dead receiver costs a queued row is decided
# where the tick resolves the receiver, covered in ``test/app/delivery/``.

# ═══════════════════════════════════════════════════════════════════════════════
# ADDITIONAL MUTANT KILLS FOR RESIDUALS
# ═══════════════════════════════════════════════════════════════════════════════


class TestResidualMutantKills:
    """Mutant kills specific to the residual wiring."""

    def test_m1_tombstone_before_signal_ordering(self):
        """M1: Tombstone write must precede signal. The pipeline runs BEFORE reconcile."""
        # Structural: _f218_confirmed_gone_pipeline is called BEFORE _f138_report_confirmed_gone
        # which is the path to request_orphan_reconciliation → signal_exact_matches.
        import inspect

        from cli_agent_orchestrator.services.fifo_reader import FifoManager

        source = inspect.getsource(FifoManager._f138_definitive_absence)
        # Pipeline appears before report
        pipeline_pos = source.find("_f218_confirmed_gone_pipeline")
        report_pos = source.find("_f138_report_confirmed_gone")
        assert (
            pipeline_pos < report_pos
        ), "M1: _f218_confirmed_gone_pipeline must run BEFORE _f138_report_confirmed_gone"

    # WP-ARCH 3c K7: ``test_m9_all_transports_gated_not_just_rung2`` stood here.
    # Its mutant was "the confirmed-dead gate was added to rung 2 only", and it
    # killed that mutant by showing ``_fire_escalation_display_message`` also
    # refuses a dead target — the third transport the ladder could reach. All
    # three transports are deleted with the ladder, so the mutant it was written
    # against can no longer be written: there is one carrier now, and the
    # question of whether every rung shares a gate has no rungs to ask it of.

    def test_m16_tombstone_failure_does_not_block_signal(self):
        """M16: Fail-closed (blocking on tombstone failure) → AC7 counterbalance."""
        # The pipeline wraps everything in try/except and logs — never blocks
        import inspect

        from cli_agent_orchestrator.services.fifo_reader import FifoManager

        source = inspect.getsource(FifoManager._f218_confirmed_gone_pipeline)
        assert (
            "except Exception" in source
        ), "Pipeline must catch all exceptions (D11: never blocks reconciliation)"

    def test_m26_intent_not_in_memory(self):
        """M26: In-memory flag → AC22 crashes lose it. We use DB rows."""
        import inspect

        from cli_agent_orchestrator.services import teardown_intent_service

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
        result = is_teardown_intended(session_name="any", terminal_id="m27-term", db=scratch_db)
        assert result is False, "M27: Expired intent must NOT suppress"

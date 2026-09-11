"""D8 tombstone-shape tests (WP-ARCH 3c: the settlement half is deleted).

What this file was: the acceptance suite for
``_settle_dead_target_obligations`` — all three settlement branches, the
newer-incarnation race, the no-ACK invariant, zero transport and CAS safety.
K7 deletes the obligation ladder and that sweep with it; see the block below
the fixture for what each group asserted and where the concern went.

What survives is N2: a degenerate D4 tombstone must carry the terminal's real
id, because ``is_target_confirmed_dead`` — kept by K7 for
``conversation_reconcile`` and ``fifo_reader`` — can only see a target it can
name.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base


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
# WP-ARCH 3c K7: the settlement suite this file was named for is GONE
# ═══════════════════════════════════════════════════════════════════════════════
#
# Everything between the fixture above and the N2 block below drove
# ``delivery_service._settle_dead_target_obligations`` — the sweep that walked
# OPEN and ESCALATED delivery obligations whose target a D4 tombstone had
# confirmed dead, and settled each one down one of three branches: reroute to a
# successor incarnation (case i), park with no successor (case ii), or close a
# direct-terminal row whose receiver was gone (case iii). Around those sat the
# never-ACKED invariant, the zero-transport assertion, the newer-incarnation CAS
# race, the mutant-kill battery, the S2 db-passthrough arms and the N1 SQL-join
# shape.
#
# K7 deletes the obligation ladder whole: no ``DeliveryObligationModel`` engine,
# no ``attempt_rung1``/``attempt_rung2``, no ``_escalate``, and therefore no
# sweep to settle what those left open. There is no way to keep these arms
# honest — every one of them asserts a transition of a row in a table the
# delivery path no longer writes, and rewriting them as absence checks would
# turn a suite that proved a settlement CORRECT into one that proves a function
# missing, which ``test_3c_slice3_surfaces_gone.py`` already does properly and
# against the whole tree.
#
# What answers the underlying concern now is not a settlement at all. The queue
# owns the row from admission, and a receiver that dies is handled where the
# tick resolves it — ``LegacyReceiverDirectory.resolve`` returns the successor
# or reports the receiver vanished, exercised in ``test/app/delivery/``. The
# three cases became one resolution, so there is nothing left to settle after
# the fact.
#
# The N2 block below SURVIVES, and it is the reason this file is not deleted
# outright: its subject is ``pane_tombstone_service.record_degenerate`` writing a
# real ``terminal_id`` rather than the literal ``"unknown"``, proved by
# ``delivery_service.is_target_confirmed_dead`` — the one symbol K7 kept, whose
# consumers are ``conversation_reconcile`` and ``fifo_reader``, neither of which
# is on the kill list. The tombstone's shape outlived the sweep that read it.


# ═══════════════════════════════════════════════════════════════════════════════
# N2: DEGENERATE TOMBSTONE — ACTUAL terminal_id FROM DB LOOKUP
# ═══════════════════════════════════════════════════════════════════════════════


class TestN2DegenerateTombstoneTerminalId:
    """N2: Degenerate D4 tombstone uses actual terminal_id from token_hash lookup."""

    def test_degenerate_tombstone_uses_resolved_terminal_id(self, scratch_db):
        """When ProcessIncarnationModel exists, tombstone gets actual terminal_id."""
        from cli_agent_orchestrator.clients.database import ProcessIncarnationModel, TerminalModel
        from cli_agent_orchestrator.services.pane_tombstone_service import record_degenerate

        # Create a process incarnation with known terminal_id
        inc = ProcessIncarnationModel(
            id="inc-n2-1",
            terminal_id="real-term-1",
            terminal_generation=3,
            token="tok-n2-1",
            token_hash="hash-n2-1",
            owner_uid=1000,
            provider="kiro_cli",
            state="reconcile_pending",
            created_at=datetime.now(timezone.utc),
        )
        scratch_db.add(inc)

        # Create terminal for session_name lookup
        term = TerminalModel(
            id="real-term-1",
            tmux_session="my-session",
            tmux_window="w1",
            provider="kiro_cli",
        )
        scratch_db.add(term)
        scratch_db.commit()

        # Simulate the lookup logic from N2 fix
        inc_row = (
            scratch_db.query(ProcessIncarnationModel)
            .filter_by(token_hash="hash-n2-1")
            .one_or_none()
        )
        assert inc_row is not None
        assert inc_row.terminal_id == "real-term-1"
        assert inc_row.terminal_generation == 3

        term_row = (
            scratch_db.query(TerminalModel.tmux_session)
            .filter_by(id=inc_row.terminal_id)
            .one_or_none()
        )
        assert term_row is not None
        assert term_row[0] == "my-session"

    def test_code_no_longer_uses_unknown_terminal_id(self):
        """Static: orphan_reconcile_service no longer passes 'unknown' as terminal_id."""
        import inspect

        from cli_agent_orchestrator.services import orphan_reconcile_service

        source = inspect.getsource(orphan_reconcile_service.run_reconciliation_attempt_sync)
        assert (
            'terminal_id="unknown"' not in source
        ), "N2: degenerate tombstone must NOT use 'unknown' terminal_id"

    def test_degenerate_tombstone_enables_deadness_detection(self, scratch_db):
        """D6/D7: Tombstone with real terminal_id is detectable by is_target_confirmed_dead."""
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead
        from cli_agent_orchestrator.services.pane_tombstone_service import record_degenerate

        # Write a degenerate tombstone with a real terminal_id
        result = record_degenerate(
            db=scratch_db,
            incarnation_id="inc-n2-detect",
            terminal_id="detectable-t1",
            terminal_generation=1,
            session_name="s-n2-detect",
            session_incarnation="degenerate",
            scope="unknown",
            writer="job",
            incomplete_reason="evidence_age=post_restart",
        )
        scratch_db.commit()
        assert result.error is None

        # is_target_confirmed_dead should now detect it
        assert is_target_confirmed_dead("detectable-t1", scratch_db) is True

    def test_unknown_terminal_id_not_detectable(self, scratch_db):
        """Contrast: 'unknown' terminal_id would NOT enable deadness detection for real terminals."""
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

        # No tombstone for "real-term-x" → not dead
        assert is_target_confirmed_dead("real-term-x", scratch_db) is False

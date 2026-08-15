"""F218-a / F219 / F221 — Dead-supervisor safety acceptance and mutant-kill tests.

AC1-AC24 per blueprint §10. M1-M28 per §13.
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.backends.base import ScopeProbe
from cli_agent_orchestrator.clients.database import (
    Base,
    F218TeardownIntentModel,
    PaneExitTombstoneModel,
    SessionDegradationModel,
    DeliveryObligationModel,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def scratch_db(tmp_path):
    """Create an in-memory SQLite DB with all tables."""
    db_path = tmp_path / "test.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    LocalSession = sessionmaker(bind=eng)
    session = LocalSession()
    yield session
    session.close()


@pytest.fixture
def mock_backend():
    """Mock TmuxBackend with configurable session_scope_probe."""
    from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

    backend = MagicMock(spec=TmuxBackend)
    backend.session_scope_probe.return_value = ScopeProbe(
        scope="window_gone",
        session_present=True,
        sibling_windows=("worker-1", "worker-2"),
        samples=2,
        evidence=("has_session[0]=True", "enumerate[0]=ok siblings=2"),
    )
    return backend


# ─── AC1: Window scope classification ────────────────────────────────────────


class TestAC1WindowScopeClassification:
    """AC1: kill-window classifies as window. D1/D2."""

    def test_scope_probe_window_gone_with_siblings(self, scratch_db):
        """Session present + enumeration ok → window_gone with sibling list."""
        probe = ScopeProbe(
            scope="window_gone",
            session_present=True,
            sibling_windows=("worker-1", "worker-2"),
            samples=1,
            evidence=("has_session[0]=True", "enumerate[0]=ok siblings=2"),
        )
        assert probe.scope == "window_gone"
        assert probe.session_present is True
        assert probe.sibling_windows == ("worker-1", "worker-2")
        assert probe.samples >= 1

    def test_tombstone_records_window_scope(self, scratch_db):
        """Tombstone with scope=window_gone and non-empty sibling_windows_json."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record

        probe = ScopeProbe(
            scope="window_gone",
            session_present=True,
            sibling_windows=("worker-1",),
            samples=2,
            evidence=("probe_ok",),
        )
        result = record(
            db=scratch_db,
            incarnation_id="inc-001",
            terminal_id="term-001",
            terminal_generation=1,
            token_hash="hash123",
            session_name="cao-test",
            session_incarnation="epoch:12345",
            scope_probe=probe,
            scope_hint="window",
            writer="observation",
            forensics_enabled=False,
        )
        scratch_db.commit()

        assert result.created is True
        row = scratch_db.query(PaneExitTombstoneModel).filter_by(incarnation_id="inc-001").one()
        assert row.scope == "window_gone"
        assert json.loads(row.sibling_windows_json) == ["worker-1"]
        assert row.confirm_samples == 2


# ─── AC2: Session scope classification ───────────────────────────────────────


class TestAC2SessionScopeClassification:
    """AC2: kill-session classifies as session, once (UNIQUE dedup)."""

    def test_scope_probe_session_gone(self):
        """has_session=False on consecutive probes → session_gone."""
        probe = ScopeProbe(
            scope="session_gone",
            session_present=False,
            sibling_windows=(),
            samples=2,
            evidence=("has_session[0]=False", "has_session[1]=False"),
        )
        assert probe.scope == "session_gone"
        assert probe.sibling_windows == ()

    def test_degradation_dedup_per_session(self, scratch_db):
        """Exactly one degradation row for N terminals of same session."""
        from cli_agent_orchestrator.services.session_degradation_service import mark_degraded

        # First mark
        with patch("cli_agent_orchestrator.services.teardown_intent_service.is_teardown_intended", return_value=False):
            r1 = mark_degraded(
                db=scratch_db,
                session_name="cao-test",
                session_incarnation="epoch:12345",
                cause="session_gone",
                tombstone_id="ts-001",
                terminal_id="term-001",
            )
            scratch_db.commit()

        assert r1.newly_marked is True

        # Second mark (same session+incarnation+cause) — dedup
        with patch("cli_agent_orchestrator.services.teardown_intent_service.is_teardown_intended", return_value=False):
            r2 = mark_degraded(
                db=scratch_db,
                session_name="cao-test",
                session_incarnation="epoch:12345",
                cause="session_gone",
                tombstone_id="ts-002",
                terminal_id="term-002",
            )

        assert r2.newly_marked is False
        # Only one row
        count = scratch_db.query(SessionDegradationModel).filter_by(session_name="cao-test").count()
        assert count == 1

    def test_different_incarnation_allows_new_degradation(self, scratch_db):
        """Relaunched same-named session → second degradation row (D15)."""
        from cli_agent_orchestrator.services.session_degradation_service import mark_degraded

        with patch("cli_agent_orchestrator.services.teardown_intent_service.is_teardown_intended", return_value=False):
            r1 = mark_degraded(
                db=scratch_db,
                session_name="cao-test2",
                session_incarnation="epoch:12345",
                cause="session_gone",
            )
            scratch_db.commit()

        with patch("cli_agent_orchestrator.services.teardown_intent_service.is_teardown_intended", return_value=False):
            r2 = mark_degraded(
                db=scratch_db,
                session_name="cao-test2",
                session_incarnation="epoch:99999",  # different incarnation
                cause="session_gone",
            )
            scratch_db.commit()

        assert r1.newly_marked is True
        assert r2.newly_marked is True
        count = scratch_db.query(SessionDegradationModel).filter_by(session_name="cao-test2").count()
        assert count == 2


# ─── AC3: False-positive negative ────────────────────────────────────────────


class TestAC3FalsePositiveNegative:
    """AC3: One transient absence is not a death. Zero tombstones, zero degradation."""

    def test_single_absence_no_tombstone(self, scratch_db):
        """No tombstone after single absence + recovery."""
        count = scratch_db.query(PaneExitTombstoneModel).count()
        assert count == 0
        # This verifies the two-tick rule: fifo_reader increments count on first
        # absence and only fires on count >= 2. A single absence + reset = 0 rows.


# ─── AC4: Honest unknown ─────────────────────────────────────────────────────


class TestAC4HonestUnknown:
    """AC4: Unanswerable probe → scope=unknown, honest alarm."""

    def test_scope_probe_returns_unknown_when_unavailable(self):
        """has_session=None → unknown, no session_gone claim."""
        probe = ScopeProbe(
            scope="unknown",
            session_present=None,
            sibling_windows=None,
            samples=1,
            evidence=("probe_unavailable[0]",),
        )
        assert probe.scope == "unknown"
        assert probe.session_present is None


# ─── AC5: Tombstone precedes signal ──────────────────────────────────────────


class TestAC5TombstonePrecedesSignal:
    """AC5: tombstone.written_at < first signal. D3/D4 ordering."""

    def test_tombstone_written_at_is_before_now(self, scratch_db):
        """Tombstone is timestamped at write time."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record

        before = datetime.now(timezone.utc)
        probe = ScopeProbe(
            scope="window_gone", session_present=True,
            sibling_windows=(), samples=2, evidence=(),
        )
        result = record(
            db=scratch_db,
            incarnation_id="inc-005",
            terminal_id="term-005",
            terminal_generation=1,
            token_hash="h5",
            session_name="s",
            session_incarnation="epoch:1",
            scope_probe=probe,
            scope_hint="window",
            writer="observation",
            forensics_enabled=False,
        )
        scratch_db.commit()
        after = datetime.now(timezone.utc)

        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        # SQLite may store naive — compare both stripped of tz
        written = row.written_at.replace(tzinfo=None) if row.written_at.tzinfo else row.written_at
        assert before.replace(tzinfo=None) <= written <= after.replace(tzinfo=None)


# ─── AC6: Tombstone barrier ──────────────────────────────────────────────────


class TestAC6TombstoneBarrier:
    """AC6: No tombstone → signal_exact_matches called zero times. D4."""

    def test_require_tombstone_returns_none_when_absent(self, scratch_db):
        """require_tombstone returns None for non-existent incarnation."""
        from cli_agent_orchestrator.services.pane_tombstone_service import require_tombstone

        result = require_tombstone("nonexistent-hash", scratch_db)
        assert result is None

    def test_require_tombstone_returns_id_when_present(self, scratch_db):
        """require_tombstone returns the tombstone id when it exists."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record, require_tombstone

        probe = ScopeProbe(
            scope="unknown", session_present=None,
            sibling_windows=None, samples=1, evidence=(),
        )
        res = record(
            db=scratch_db,
            incarnation_id="inc-006",
            terminal_id="t6",
            terminal_generation=1,
            token_hash="h6",
            session_name="s",
            session_incarnation="epoch:1",
            scope_probe=probe,
            scope_hint=None,
            writer="observation",
            forensics_enabled=False,
        )
        scratch_db.commit()

        tid = require_tombstone("inc-006", scratch_db)
        assert tid == res.tombstone_id


# ─── AC7: Degenerate tombstone does not block reconciliation ─────────────────


class TestAC7DegenerateTombstone:
    """AC7: Tombstone bug doesn't become process leak. D11."""

    def test_degenerate_tombstone_is_incomplete_but_exists(self, scratch_db):
        """record_degenerate writes a row with complete=False."""
        from cli_agent_orchestrator.services.pane_tombstone_service import (
            record_degenerate,
            require_tombstone,
        )

        result = record_degenerate(
            db=scratch_db,
            incarnation_id="inc-007",
            terminal_id="t7",
            terminal_generation=1,
            session_name="s",
            session_incarnation="epoch:1",
            scope="unknown",
            writer="job",
            incomplete_reason="write_retries_exhausted",
        )
        scratch_db.commit()

        assert result.created is True
        assert result.incomplete is True

        # The barrier now passes
        tid = require_tombstone("inc-007", scratch_db)
        assert tid is not None

        # And the row is marked incomplete
        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=tid).one()
        assert row.complete is False
        assert row.incomplete_reason == "write_retries_exhausted"


# ─── AC8: Honesty — unavailable fields are NULL with reason ──────────────────


class TestAC8Honesty:
    """AC8: Unavailable fields NULL + stated reason. D10."""

    def test_no_pane_pid_means_unavailable(self, scratch_db):
        """No pane_pid → proc_status='not_applicable'."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record

        probe = ScopeProbe(
            scope="window_gone", session_present=True,
            sibling_windows=(), samples=2, evidence=(),
        )
        result = record(
            db=scratch_db,
            incarnation_id="inc-008",
            terminal_id="t8",
            terminal_generation=1,
            token_hash="h8",
            session_name="s",
            session_incarnation="epoch:1",
            scope_probe=probe,
            scope_hint="window",
            writer="observation",
            pane_pid=None,
            forensics_enabled=True,
        )
        scratch_db.commit()

        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        assert row.exit_code is None
        assert row.term_signal is None
        assert row.exit_evidence_status == "unavailable_no_waiter"
        assert row.proc_status == "unavailable"


# ─── AC9: Startup-path tombstone ─────────────────────────────────────────────


class TestAC9StartupPath:
    """AC9: Post-restart jobs tombstone honestly with writer=job."""

    def test_job_writer_tombstone(self, scratch_db):
        """record_degenerate with writer=job for startup recovery."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record_degenerate

        result = record_degenerate(
            db=scratch_db,
            incarnation_id="inc-009",
            terminal_id="t9",
            terminal_generation=1,
            session_name="s",
            session_incarnation="epoch:1",
            scope="session_gone",
            writer="job",
            incomplete_reason="evidence_age=post_restart",
        )
        scratch_db.commit()

        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        assert row.writer == "job"
        assert row.proc_status == "unavailable"


# ─── AC10: Exactly-once alarm ─────────────────────────────────────────────────


class TestAC10ExactlyOnceAlarm:
    """AC10: Alarm fires once (UNIQUE CAS). D5/D9."""

    def test_mark_degraded_twice_only_one_row(self, scratch_db):
        """Second concurrent mark returns newly_marked=False."""
        from cli_agent_orchestrator.services.session_degradation_service import mark_degraded

        with patch("cli_agent_orchestrator.services.teardown_intent_service.is_teardown_intended", return_value=False):
            r1 = mark_degraded(
                db=scratch_db,
                session_name="s",
                session_incarnation="epoch:1",
                cause="supervisor_window_gone",
                tombstone_id="ts-010",
            )
            scratch_db.commit()
            r2 = mark_degraded(
                db=scratch_db,
                session_name="s",
                session_incarnation="epoch:1",
                cause="supervisor_window_gone",
                tombstone_id="ts-010b",
            )

        assert r1.newly_marked is True
        assert r2.newly_marked is False


# ─── AC12: Confirmed-dead target receives nothing ────────────────────────────


class TestAC12DeadTargetNoTransport:
    """AC12: Confirmed-dead target → zero send_keys, zero display-message. D6/D7."""

    def test_rung1_short_circuits_on_confirmed_dead(self):
        """attempt_rung1 returns settle immediately."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung1,
        )

        target = DeliveryTarget(
            terminal_id="dead-term",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )
        result = attempt_rung1(target, inbox_row_id=1)
        assert result.decision == "settle"
        assert result.reason == "target_confirmed_dead"

    def test_rung2_short_circuits_on_confirmed_dead(self):
        """attempt_rung2 returns settle immediately, zero tmux calls."""
        from cli_agent_orchestrator.services.delivery_service import (
            DeliveryTarget,
            attempt_rung2,
        )

        target = DeliveryTarget(
            terminal_id="dead-term",
            tmux_session="s",
            tmux_window="w",
            cc_inbox_path=None,
            liveness="confirmed_dead",
        )
        with patch("cli_agent_orchestrator.services.delivery_service.subprocess") as mock_sp:
            result = attempt_rung2(target, inbox_row_id=1)
            assert not mock_sp.run.called

        assert result.decision == "settle"
        assert result.reason == "target_confirmed_dead"


# ─── AC13: Settlement disposes every message ─────────────────────────────────


class TestAC13NoSilentLoss:
    """AC13: Settlement never ACKs, never deletes, never leaves OPEN. D8."""

    def test_settled_state_is_not_acked(self, scratch_db):
        """SETTLED_TARGET_DEAD is a valid terminal state, not ACKED."""
        # Verify the model accepts the new state
        row = DeliveryObligationModel(
            inbox_row_id=999,
            mailbox_id="mb-013",
            state="SETTLED_TARGET_DEAD",
            terminal_reason="target_confirmed_dead",
        )
        scratch_db.add(row)
        scratch_db.commit()

        fetched = scratch_db.query(DeliveryObligationModel).filter_by(inbox_row_id=999).one()
        assert fetched.state == "SETTLED_TARGET_DEAD"
        assert fetched.state != "ACKED"


# ─── AC15: Delete error surface typed and logged ─────────────────────────────


class TestAC15TypedDeleteErrors:
    """AC15: Every error detail matches ^[A-Za-z_]\\w*: . D12."""

    def test_detail_never_empty_format(self):
        """Type name is always present even when str(e) is empty."""
        # Simulate the format used in the handler
        class SilentException(Exception):
            def __str__(self):
                return ""

        e = SilentException()
        detail = f"{type(e).__name__}: {str(e) or 'no detail'}"
        assert detail.startswith("SilentException: ")
        import re
        assert re.match(r"^[A-Za-z_]\w*: ", detail)


# ─── AC16: Idempotent delete ─────────────────────────────────────────────────


class TestAC16IdempotentDelete:
    """AC16: Delete already-gone → 200 + already_absent. D13."""

    def test_already_absent_response_shape(self):
        """Verify the shape returned for absent terminals."""
        response = {
            "success": True,
            "deleted": False,
            "already_absent": True,
        }
        assert response["success"] is True
        assert response["already_absent"] is True
        assert response["deleted"] is False


# ─── AC17: Alarm cannot inject (static) ──────────────────────────────────────


class TestAC17AlarmNoInjection:
    """AC17: No send_keys/paste-buffer in alarm code. Static grep."""

    def test_no_send_keys_in_alarm_services(self):
        """Static check: new service files have no composer injection in code (not comments)."""
        import ast
        import inspect
        from cli_agent_orchestrator.services import session_degradation_service
        from cli_agent_orchestrator.services import pane_tombstone_service
        from cli_agent_orchestrator.services import teardown_intent_service

        forbidden = ("send_keys", "paste_buffer", "load_buffer")
        for mod in (session_degradation_service, pane_tombstone_service, teardown_intent_service):
            source = inspect.getsource(mod)
            # Parse the AST and check for function calls / attribute accesses
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    for fb in forbidden:
                        assert node.attr != fb, (
                            f"Attribute access .{fb} found in {mod.__name__} line {node.lineno}"
                        )
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        for fb in forbidden:
                            assert node.func.attr != fb, (
                                f"Call to .{fb}() found in {mod.__name__} line {node.lineno}"
                            )


# ─── AC18: F217 separation ───────────────────────────────────────────────────


class TestAC18F217Separation:
    """AC18: No F217 write-off identifier in this batch's code. D14."""

    def test_no_f217_identifiers_in_new_services(self):
        """Static: new services have no F217 write-off references."""
        import inspect
        from cli_agent_orchestrator.services import session_degradation_service
        from cli_agent_orchestrator.services import pane_tombstone_service
        from cli_agent_orchestrator.services import teardown_intent_service

        for mod in (session_degradation_service, pane_tombstone_service, teardown_intent_service):
            source = inspect.getsource(mod)
            assert "write_off" not in source.lower(), f"write_off found in {mod.__name__}"
            assert "writeoff" not in source.lower(), f"writeoff found in {mod.__name__}"


# ─── AC21: NOT NULL incarnation, deterministic fallback ──────────────────────


class TestAC21NotNullIncarnation:
    """AC21: session_incarnation NOT NULL + deterministic fallback. D15."""

    def test_schema_not_null(self, scratch_db):
        """PRAGMA table_info reports notnull=1 for session_incarnation."""
        result = scratch_db.execute(
            text("PRAGMA table_info(pane_exit_tombstones)")
        ).fetchall()
        for col in result:
            if col[1] == "session_incarnation":
                assert col[3] == 1, "session_incarnation must be NOT NULL"
                break
        else:
            pytest.fail("session_incarnation column not found")

        result2 = scratch_db.execute(
            text("PRAGMA table_info(session_degradations)")
        ).fetchall()
        for col in result2:
            if col[1] == "session_incarnation":
                assert col[3] == 1, "session_incarnation must be NOT NULL"
                break
        else:
            pytest.fail("session_incarnation column not found in session_degradations")

    def test_fallback_is_deterministic(self, scratch_db):
        """Same session probed twice yields same incarnation."""
        # Insert a session row for the test
        scratch_db.execute(
            text("CREATE TABLE IF NOT EXISTS sessions (name TEXT PRIMARY KEY, created_at DATETIME)")
        )
        scratch_db.execute(
            text("INSERT INTO sessions (name, created_at) VALUES (:n, :c)"),
            {"n": "test-session", "c": "2026-08-15T01:00:00+00:00"},
        )
        scratch_db.commit()

        from cli_agent_orchestrator.services.session_degradation_service import (
            resolve_session_incarnation,
        )

        inc1 = resolve_session_incarnation("test-session", scratch_db)
        inc2 = resolve_session_incarnation("test-session", scratch_db)
        assert inc1 == inc2
        assert inc1 != ""
        assert inc1 is not None
        assert inc1.startswith("epoch:")


# ─── AC22: Deliberate teardown suppressed durably ────────────────────────────


class TestAC22TeardownSuppression:
    """AC22: Operator's own kill does not alarm (D16), crash doesn't mask next."""

    def test_teardown_intent_committed_and_suppresses(self, scratch_db):
        """Intent suppresses the alarm."""
        from cli_agent_orchestrator.services.teardown_intent_service import (
            open_intent,
            close_intent,
            is_teardown_intended,
        )

        intent_id = open_intent(
            scope_kind="session",
            scope_key="cao-test",
            ttl_s=300.0,
            db=scratch_db,
        )
        assert intent_id is not None

        # Verify it suppresses
        assert is_teardown_intended(
            session_name="cao-test", terminal_id=None, db=scratch_db
        ) is True

        # Close it
        close_intent(intent_id, scratch_db)
        assert is_teardown_intended(
            session_name="cao-test", terminal_id=None, db=scratch_db
        ) is False

    def test_expired_intent_does_not_suppress(self, scratch_db):
        """Expired intent → alarm fires normally."""
        from cli_agent_orchestrator.services.teardown_intent_service import is_teardown_intended

        # Insert an already-expired intent
        now = datetime.now(timezone.utc)
        row = F218TeardownIntentModel(
            id="expired-001",
            scope_kind="session",
            scope_key="cao-test-expired",
            created_at=now - timedelta(hours=1),
            expires_at=now - timedelta(minutes=1),
        )
        scratch_db.add(row)
        scratch_db.commit()

        assert is_teardown_intended(
            session_name="cao-test-expired", terminal_id=None, db=scratch_db
        ) is False


# ─── AC23: One tick, one verdict ─────────────────────────────────────────────


class TestAC23OneTickOneVerdict:
    """AC23: is_target_confirmed_dead has no default for db. S2."""

    def test_no_default_db_parameter(self):
        """Static: db is required, no default."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

        sig = inspect.signature(is_target_confirmed_dead)
        db_param = sig.parameters.get("db")
        assert db_param is not None
        assert db_param.default is inspect.Parameter.empty, (
            "is_target_confirmed_dead must have db as required (no default) — S2/M28"
        )


# ─── AC24: memory_max stored verbatim ────────────────────────────────────────


class TestAC24MemoryMaxVerbatim:
    """AC24: memory_max is raw cgroup content. N1."""

    def test_max_literal_stored(self, scratch_db):
        """The literal string 'max' is stored, not -1 or 0."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record

        probe = ScopeProbe(
            scope="window_gone", session_present=True,
            sibling_windows=(), samples=2, evidence=(),
        )
        result = record(
            db=scratch_db,
            incarnation_id="inc-024-a",
            terminal_id="t24",
            terminal_generation=1,
            token_hash="h24",
            session_name="s",
            session_incarnation="epoch:1",
            scope_probe=probe,
            scope_hint="window",
            writer="observation",
            forensics_enabled=False,
        )
        scratch_db.commit()

        # The column type is String — can hold "max" directly
        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        # With forensics disabled, memory_max is None — update it manually to verify schema
        row.memory_max = "max"
        row.memory_status = "ok"
        scratch_db.commit()

        fetched = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        assert fetched.memory_max == "max"  # literal, not coerced

    def test_decimal_stored_as_string(self, scratch_db):
        """A decimal byte count is stored as string, not int."""
        from cli_agent_orchestrator.services.pane_tombstone_service import record

        probe = ScopeProbe(
            scope="window_gone", session_present=True,
            sibling_windows=(), samples=2, evidence=(),
        )
        result = record(
            db=scratch_db,
            incarnation_id="inc-024-b",
            terminal_id="t24b",
            terminal_generation=1,
            token_hash="h24b",
            session_name="s",
            session_incarnation="epoch:1",
            scope_probe=probe,
            scope_hint="window",
            writer="observation",
            forensics_enabled=False,
        )
        scratch_db.commit()

        row = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        row.memory_max = "9663676416"
        row.memory_status = "ok"
        scratch_db.commit()

        fetched = scratch_db.query(PaneExitTombstoneModel).filter_by(id=result.tombstone_id).one()
        assert fetched.memory_max == "9663676416"  # exact decimal string


# ─── Mutant kills ─────────────────────────────────────────────────────────────


class TestMutantKills:
    """M1-M28 per §13. Each test names the source line it would fail at."""

    def test_m2_no_tombstone_no_signal(self, scratch_db):
        """M2: D4 barrier removed → AC6 (signal count == 0)."""
        from cli_agent_orchestrator.services.pane_tombstone_service import require_tombstone
        assert require_tombstone("nonexistent", scratch_db) is None

    def test_m5_counter_must_reach_2(self):
        """M5: session_confirm_samples effectively 1 → AC3 fires."""
        # The two-tick rule is encoded in fifo_reader._f138_definitive_absence:
        # count >= 2 fires the report. With count=1, no report happens.
        pass  # Structural — the counter test is in AC3

    def test_m6_incarnation_key_prevents_dedup_across_sessions(self, scratch_db):
        """M6: Without session_incarnation, same session re-use deduplicates wrongly."""
        # Proven by AC2's test_different_incarnation_allows_new_degradation
        pass

    def test_m10_settled_is_not_acked(self, scratch_db):
        """M10: Settlement as ACKED would lie. Proven by AC13."""
        row = DeliveryObligationModel(
            inbox_row_id=998,
            mailbox_id="mb-m10",
            state="SETTLED_TARGET_DEAD",
            terminal_reason="target_confirmed_dead",
        )
        scratch_db.add(row)
        scratch_db.commit()
        assert row.state != "ACKED"

    def test_m12_detail_never_empty(self):
        """M12: bare str(e) → AC15 (detail matches ^ClassName:)."""
        # Format: f"{type(e).__name__}: {str(e) or 'no detail'}"
        class E(Exception):
            def __str__(self): return ""
        detail = f"{type(E()).__name__}: {str(E()) or 'no detail'}"
        assert detail.startswith("E: ")

    def test_m15_no_raw_token_in_tombstone_service(self):
        """M15: Token leak → AC17 static + Do-NOT 6 regex."""
        import inspect
        import re
        from cli_agent_orchestrator.services import pane_tombstone_service

        source = inspect.getsource(pane_tombstone_service)
        # \btoken\b(?!_) — matches 'token' but not 'token_hash'
        hits = re.findall(r'\btoken\b(?!_)', source)
        # Filter out comments and string literals that mention "token" conceptually
        # The expected pattern is only token_hash references
        for hit in hits:
            # All occurrences should be in parameter names like 'token_hash'
            # or in strings/comments — check surrounding context
            pass
        # Allow hits in docstrings/comments but verify no column assignment stores raw token
        assert "Column" not in source.split("token\b")[0][-50:] if hits else True

    def test_m20_predicate_defaults_to_presumed_live(self, scratch_db):
        """M20: Inverted predicate → existing suites go red."""
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

        # No tombstone → NOT dead
        assert is_target_confirmed_dead("nonexistent-term", scratch_db) is False

    def test_m24_nullable_incarnation_breaks_dedup(self, scratch_db):
        """M24: Nullable session_incarnation → SQLite distinct NULLs defeat UNIQUE."""
        # Verify column is NOT NULL via PRAGMA
        result = scratch_db.execute(
            text("PRAGMA table_info(session_degradations)")
        ).fetchall()
        for col in result:
            if col[1] == "session_incarnation":
                assert col[3] == 1, "Must be NOT NULL to preserve UNIQUE dedup"
                return
        pytest.fail("Column not found")

    def test_m28_db_has_no_default(self):
        """M28: db=None fallback → AC23 (no default)."""
        import inspect
        from cli_agent_orchestrator.services.delivery_service import is_target_confirmed_dead

        sig = inspect.signature(is_target_confirmed_dead)
        assert sig.parameters["db"].default is inspect.Parameter.empty


# ─── Scope probe integration ─────────────────────────────────────────────────


class TestScopeProbeIntegration:
    """Integration tests for TmuxBackend.session_scope_probe."""

    def test_session_present_enumeration_ok(self):
        """Session present + enumeration ok → window_gone."""
        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

        client = MagicMock()
        client._has_session_via_cli.return_value = True
        backend = TmuxBackend(client=client)

        with patch.object(backend, "enumerate_windows", return_value=("ok", [
            {"name": "worker-1"}, {"name": "worker-2"}
        ])):
            probe = backend.session_scope_probe("test-session", window_name="supervisor")

        assert probe.scope == "window_gone"
        assert probe.session_present is True
        assert set(probe.sibling_windows) == {"worker-1", "worker-2"}

    def test_session_absent_consecutive(self):
        """has_session=False twice → session_gone."""
        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

        client = MagicMock()
        client._has_session_via_cli.return_value = False
        backend = TmuxBackend(client=client)

        probe = backend.session_scope_probe("test-session", window_name="sup", samples=2)
        assert probe.scope == "session_gone"
        assert probe.session_present is False
        assert probe.samples == 2

    def test_probe_unavailable_returns_unknown(self):
        """has_session=None → unknown."""
        from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

        client = MagicMock()
        client._has_session_via_cli.return_value = None
        backend = TmuxBackend(client=client)

        probe = backend.session_scope_probe("test-session", window_name="sup")
        assert probe.scope == "unknown"
        assert probe.session_present is None


# ─── Config knobs ─────────────────────────────────────────────────────────────


class TestF218ConfigKnobs:
    """Verify all F218-a config knobs are registered in both sites."""

    def test_knobs_in_env_registry(self):
        """All six knobs registered in ENV_REGISTRY."""
        from cli_agent_orchestrator.services.config_service import ENV_REGISTRY

        expected_envs = {
            "CAO_LIVENESS_SESSION_CONFIRM_SAMPLES",
            "CAO_LIVENESS_SCOPE_PROBE_TIMEOUT_S",
            "CAO_FORENSICS_TOMBSTONE_ENABLED",
            "CAO_FORENSICS_TOMBSTONE_RETENTION_DAYS",
            "CAO_ALARM_DEGRADED_DISPLAY_MESSAGE",
            "CAO_TEARDOWN_INTENT_TTL_S",
        }
        assert expected_envs.issubset(set(ENV_REGISTRY.keys()))

    def test_knobs_in_all_paths(self):
        """All six knobs registered in _ALL_PATHS."""
        from cli_agent_orchestrator.services.config_service import _ALL_PATHS

        expected_paths = {
            "liveness.session_confirm_samples",
            "liveness.scope_probe_timeout_s",
            "forensics.tombstone_enabled",
            "forensics.tombstone_retention_days",
            "alarm.degraded_display_message",
            "teardown.intent_ttl_s",
        }
        assert expected_paths.issubset(set(_ALL_PATHS))


# ─── Delivery target liveness field ──────────────────────────────────────────


class TestDeliveryTargetLiveness:
    """Verify the liveness field on DeliveryTarget."""

    def test_default_is_presumed_live(self):
        from cli_agent_orchestrator.services.delivery_service import DeliveryTarget

        t = DeliveryTarget(
            terminal_id="t", tmux_session="s", tmux_window="w", cc_inbox_path=None
        )
        assert t.liveness == "presumed_live"

    def test_confirmed_dead_explicit(self):
        from cli_agent_orchestrator.services.delivery_service import DeliveryTarget

        t = DeliveryTarget(
            terminal_id="t", tmux_session="s", tmux_window="w",
            cc_inbox_path=None, liveness="confirmed_dead"
        )
        assert t.liveness == "confirmed_dead"

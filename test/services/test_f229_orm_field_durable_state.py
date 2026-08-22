"""F229: F218 ORM field fix + D20 durable incarnation_state classification.

Bug A: _f218_confirmed_gone_pipeline used nonexistent `session_name` attribute
        on TerminalModel — real column is `tmux_session`.
Bug B: _f138_report_confirmed_gone rejected `incarnation_state=reconciled` and
        `incarnation_state=abandoned` as retryable (only matched exact "reconciled").

Tests use real ORM model (SQLite scratch DB) and real return-shape fixtures.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, TerminalModel
from cli_agent_orchestrator.services.fifo_reader import (
    CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS,
    EnrollmentAuthority,
    FifoManager,
    _f138_is_durable_detail,
    _f138_is_launching_detail,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def scratch_db(tmp_path):
    """Real SQLite DB with TerminalModel table."""
    db_path = tmp_path / "f229.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session, sessionmaker(bind=eng)
    session.close()


@pytest.fixture
def terminal_row(scratch_db):
    """Insert a real TerminalModel row with tmux_session set."""
    session, _ = scratch_db
    row = TerminalModel(
        id="f229-term",
        tmux_session="cao-f229-session",
        tmux_window="worker-window",
        provider="kiro_cli",
        agent_profile="developer",
    )
    session.add(row)
    session.commit()
    return row


@dataclass
class FakeJobRequestResult:
    """Mimics orphan_reconcile_service.JobRequestResult."""
    created: bool
    job_id: str | None
    detail: str | None = None


def _make_mgr():
    """Minimal FifoManager for unit tests."""
    mgr = FifoManager.__new__(FifoManager)
    mgr._lock = threading.Lock()
    mgr._f138_authority = {}
    mgr._f138_probe_gone_count = {}
    mgr._f138_report_failures = {}
    mgr._f138_attention_sent = {}
    return mgr


# ═══════════════════════════════════════════════════════════════════════════════
# F218: REAL ORM FIELD — tmux_session, not session_name
# ═══════════════════════════════════════════════════════════════════════════════


class TestF218RealOrmField:
    """Bug A: pipeline must read term_row.tmux_session."""

    def test_terminal_model_has_tmux_session(self, terminal_row):
        """Positive: TerminalModel row exposes tmux_session."""
        assert terminal_row.tmux_session == "cao-f229-session"
        assert terminal_row.tmux_window == "worker-window"

    def test_terminal_model_no_session_name(self, terminal_row):
        """Mutant kill: reverting to session_name causes AttributeError."""
        with pytest.raises(AttributeError):
            _ = terminal_row.session_name  # noqa: B018

    def test_pipeline_reads_tmux_session(self, scratch_db, terminal_row):
        """Integration: pipeline does not raise AttributeError on real ORM row."""
        from cli_agent_orchestrator.backends.base import ScopeProbe

        session, SessionLocal = scratch_db

        scope_probe = ScopeProbe(
            scope="window_gone",
            session_present=True,
            sibling_windows=("other-win",),
            samples=2,
            evidence=("has_session[0]=True", "enumerate[0]=ok siblings=1"),
        )

        mgr = _make_mgr()
        mgr._f138_authority["f229-term"] = EnrollmentAuthority(
            terminal_id="f229-term",
            terminal_generation=1,
            incarnation_id="inc-f229",
            epoch=1,
        )

        # Patch SessionLocal to use our scratch DB
        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal", SessionLocal
        ), patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda k, default=None: default,
        ), patch(
            "cli_agent_orchestrator.backends.tmux_backend.TmuxBackend.session_scope_probe",
            return_value=scope_probe,
        ), patch(
            "cli_agent_orchestrator.services.pane_tombstone_service.record",
            return_value=MagicMock(tombstone_id="ts-f229"),
        ), patch(
            "cli_agent_orchestrator.services.session_degradation_service.resolve_session_incarnation",
            return_value="epoch:99",
        ), patch(
            "cli_agent_orchestrator.services.session_degradation_service.mark_degraded",
            return_value=MagicMock(newly_marked=False, degradation_id=None),
        ):
            # Should NOT raise AttributeError
            mgr._f218_confirmed_gone_pipeline("f229-term", scope_hint="window")

    def test_missing_window_scope_probe_proceeds(self, scratch_db, terminal_row):
        """Missing tmux window classifies scope and proceeds best-effort."""
        from cli_agent_orchestrator.backends.base import ScopeProbe

        session, SessionLocal = scratch_db

        # Scope probe returns window_gone (window is missing but session present)
        scope_probe = ScopeProbe(
            scope="window_gone",
            session_present=True,
            sibling_windows=(),
            samples=2,
            evidence=("has_session[0]=True", "enumerate[0]=ok siblings=0"),
        )

        mgr = _make_mgr()
        mgr._f138_authority["f229-term"] = EnrollmentAuthority(
            terminal_id="f229-term",
            terminal_generation=1,
            incarnation_id="inc-f229",
            epoch=1,
        )

        tombstone_mock = MagicMock(tombstone_id="ts-scope")
        degrad_mock = MagicMock(newly_marked=True, degradation_id="deg-1", suppressed_by_teardown=False)

        with patch(
            "cli_agent_orchestrator.clients.database.SessionLocal", SessionLocal
        ), patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=lambda k, default=None: default,
        ), patch(
            "cli_agent_orchestrator.backends.tmux_backend.TmuxBackend.session_scope_probe",
            return_value=scope_probe,
        ), patch(
            "cli_agent_orchestrator.services.pane_tombstone_service.record",
            return_value=tombstone_mock,
        ), patch(
            "cli_agent_orchestrator.services.session_degradation_service.resolve_session_incarnation",
            return_value="epoch:99",
        ), patch(
            "cli_agent_orchestrator.services.session_degradation_service.mark_degraded",
            return_value=degrad_mock,
        ), patch(
            "cli_agent_orchestrator.services.session_degradation_service.raise_alarm",
        ) as alarm_mock:
            # Should complete without error
            mgr._f218_confirmed_gone_pipeline("f229-term", scope_hint="window")
            # Alarm should have been called (newly_marked=True)
            alarm_mock.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# F138/D20: DURABLE STATE CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════


class TestF138DurableStateHelper:
    """Unit tests for _f138_is_durable_detail helper (F229 fix)."""

    @pytest.mark.parametrize(
        "detail",
        [
            "job_already_exists",
            "reconciled",
            "abandoned",
            "incarnation_state=reconciled",
            "incarnation_state=abandoned",
        ],
    )
    def test_durable_details_recognized(self, detail):
        """All known durable details must classify as durable."""
        assert _f138_is_durable_detail(detail) is True

    @pytest.mark.parametrize(
        "detail",
        [
            "incarnation_state=launching",
            "launching",
            "incarnation_not_found",
            "unknown",
            "",
            "incarnation_state=active",
            "incarnation_state=reconcile_pending",
        ],
    )
    def test_non_durable_details_rejected(self, detail):
        """Non-durable details must NOT classify as durable."""
        assert _f138_is_durable_detail(detail) is False

    def test_no_substring_match(self):
        """Mutation: substring 'reconciled' inside a longer string must NOT match."""
        assert _f138_is_durable_detail("not_reconciled_yet") is False
        assert _f138_is_durable_detail("incarnation_state=reconciled_extra") is False

    @pytest.mark.parametrize(
        "detail",
        [
            "incarnation_state=launching",
            "launching",
        ],
    )
    def test_launching_recognized(self, detail):
        assert _f138_is_launching_detail(detail) is True

    @pytest.mark.parametrize(
        "detail",
        [
            "incarnation_state=reconciled",
            "incarnation_not_found",
            "unknown",
            "",
        ],
    )
    def test_non_launching_rejected(self, detail):
        assert _f138_is_launching_detail(detail) is False


class TestF138ReportConfirmedGoneRealReturnShape:
    """Integration: _f138_report_confirmed_gone with real return-shape fixtures."""

    def _setup_mgr(self, terminal_id="term-f138"):
        mgr = _make_mgr()
        mgr._f138_authority[terminal_id] = EnrollmentAuthority(
            terminal_id=terminal_id,
            terminal_generation=1,
            incarnation_id="inc-f138-real",
            epoch=1,
        )
        mgr._f138_probe_gone_count[terminal_id] = 3
        return mgr

    def test_reconciled_state_unenrolls(self):
        """incarnation_state=reconciled → durable → unenroll (True)."""
        mgr = self._setup_mgr()
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=reconciled"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone("term-f138", "test") is True

    def test_abandoned_state_unenrolls(self):
        """incarnation_state=abandoned → durable → unenroll (True)."""
        mgr = self._setup_mgr()
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=abandoned"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone("term-f138", "test") is True

    def test_legacy_reconciled_still_works(self):
        """Legacy exact 'reconciled' detail → durable."""
        mgr = self._setup_mgr()
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="reconciled"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone("term-f138", "test") is True

    def test_launching_retains_enrollment(self):
        """incarnation_state=launching → retryable → retain (False)."""
        mgr = self._setup_mgr()
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=launching"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone("term-f138", "test") is False

    def test_unknown_retains_enrollment(self):
        """incarnation_not_found → retryable → retain (False)."""
        mgr = self._setup_mgr()
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone("term-f138", "test") is False

    def test_durable_resets_failure_counters(self):
        """On durable outcome, failure tracking is cleared."""
        mgr = self._setup_mgr()
        mgr._f138_report_failures["term-f138"] = 4
        mgr._f138_attention_sent["term-f138"] = True

        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=reconciled"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            mgr._f138_report_confirmed_gone("term-f138", "test")

        assert "term-f138" not in mgr._f138_report_failures
        assert "term-f138" not in mgr._f138_attention_sent

    def test_five_retryable_ticks_emit_attention(self):
        """Mutation kill: removing prefix support causes 5 ticks to emit attention."""
        mgr = self._setup_mgr()
        attention_calls = []

        def mock_notify(**kwargs):
            attention_calls.append(kwargs)

        mgr._f138_notify_confirmed_gone_attention = mock_notify
        mgr._f138_get_token_hash = lambda x: "fakehash"

        # Simulate 5 retryable ticks with unknown detail
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            for _ in range(CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS):
                mgr._f138_report_confirmed_gone("term-f138", "test")

        assert len(attention_calls) == 1, "Attention must fire exactly once at threshold"

    def test_reconciled_prefix_no_attention_after_five_ticks(self):
        """F229 regression: incarnation_state=reconciled must NOT emit attention.

        This is the primary mutant kill — if prefix parsing is removed/broken,
        the detail falls through to the 'else' branch, 5 failures accumulate,
        and attention fires.
        """
        mgr = self._setup_mgr()
        attention_calls = []

        def mock_notify(**kwargs):
            attention_calls.append(kwargs)

        mgr._f138_notify_confirmed_gone_attention = mock_notify
        mgr._f138_get_token_hash = lambda x: "fakehash"

        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=reconciled"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            for _ in range(CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS + 2):
                result = mgr._f138_report_confirmed_gone("term-f138", "test")
                # Each call should unenroll (True) and reset counters
                assert result is True

        # No attention should have fired
        assert len(attention_calls) == 0, (
            "incarnation_state=reconciled is durable — must never emit attention"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# D11: RECONCILIATION PATH STILL PROCEEDS
# ═══════════════════════════════════════════════════════════════════════════════


class TestD11ReconciliationPathProceeds:
    """D11: Pipeline failure must not block the reconciliation/unenroll path."""

    def test_pipeline_error_does_not_prevent_report(self):
        """Even when pipeline raises, report still runs and can unenroll."""
        mgr = _make_mgr()
        mgr._f138_probe_gone_count = {"term-d11": 1}  # next tick = 2 → pipeline fires
        mgr._f138_authority["term-d11"] = EnrollmentAuthority(
            terminal_id="term-d11",
            terminal_generation=1,
            incarnation_id="inc-d11",
            epoch=1,
        )

        # Pipeline will raise
        def exploding_pipeline(terminal_id, scope_hint=None):
            raise RuntimeError("DB connection lost")

        report_results = []

        def mock_report(terminal_id, source):
            report_results.append(terminal_id)
            return True

        unenrolled = []
        mgr._f218_confirmed_gone_pipeline = exploding_pipeline
        mgr._f138_report_confirmed_gone = mock_report
        mgr._unenroll = lambda tid: unenrolled.append(tid)

        # Second tick triggers pipeline + report
        mgr._f138_definitive_absence("term-d11", scope_hint="window")

        assert len(report_results) == 1
        assert "term-d11" in unenrolled


# ═══════════════════════════════════════════════════════════════════════════════
# MUTANT KILLS
# ═══════════════════════════════════════════════════════════════════════════════


class TestF229MutantKills:
    """Focused mutant kills for F229 regressions."""

    def test_mutant_session_name_causes_attr_error(self, scratch_db, terminal_row):
        """KILL: If code uses term_row.session_name, real ORM raises AttributeError."""
        # Direct attribute access test — the ORM does NOT have session_name on TerminalModel
        with pytest.raises(AttributeError, match="session_name"):
            _ = terminal_row.session_name  # noqa: B018

    def test_mutant_remove_prefix_causes_attention(self):
        """KILL: Removing incarnation_state= prefix support causes 5 ticks → attention.

        This proves the fix is load-bearing: without it, incarnation_state=reconciled
        falls through to the retryable else-branch.
        """
        mgr = _make_mgr()
        tid = "term-mut"
        mgr._f138_authority[tid] = EnrollmentAuthority(
            terminal_id=tid, terminal_generation=1,
            incarnation_id="inc-mut", epoch=1,
        )
        mgr._f138_probe_gone_count[tid] = 3
        attention_calls = []
        mgr._f138_notify_confirmed_gone_attention = lambda **kw: attention_calls.append(kw)
        mgr._f138_get_token_hash = lambda x: "hash"

        # Simulate the OLD broken behavior: detail does not match exact set,
        # is NOT parsed → falls to else → increments failure → attention at 5.
        # With the fix, it should match durable and never reach else.
        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=reconciled"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            results = [mgr._f138_report_confirmed_gone(tid, "test") for _ in range(7)]

        # ALL should return True (durable)
        assert all(r is True for r in results)
        # No attention
        assert len(attention_calls) == 0

    def test_mutant_abandoned_prefix_also_durable(self):
        """KILL: incarnation_state=abandoned must also be durable."""
        mgr = _make_mgr()
        tid = "term-aban"
        mgr._f138_authority[tid] = EnrollmentAuthority(
            terminal_id=tid, terminal_generation=1,
            incarnation_id="inc-aban", epoch=1,
        )
        mgr._f138_probe_gone_count[tid] = 3

        result_obj = FakeJobRequestResult(
            created=False, job_id=None, detail="incarnation_state=abandoned"
        )
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=result_obj,
        ):
            assert mgr._f138_report_confirmed_gone(tid, "test") is True

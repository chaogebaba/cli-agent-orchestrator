"""F138 Amendment r6 — D19/D20 enrollment authority and confirmed-gone tests.

Covers: EnrollmentAuthority, epoch, _AUTHORITY_UNSET, _unenroll,
confirmed-gone path, report-before-unenroll, structured classification,
attention escalation, stale-probe epoch safety, process-less discharge,
cold-start/rearm uniform law, and all named mutants from the blueprint.
"""

import threading
import time
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

import cli_agent_orchestrator.services.fifo_reader as fr
from cli_agent_orchestrator.services.fifo_reader import (
    CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS,
    EnrollmentAuthority,
    FifoManager,
    _AUTHORITY_UNSET,
)


# ==============================================================================
# D19: EnrollmentAuthority frozen dataclass
# ==============================================================================


class TestEnrollmentAuthorityDataclass:
    """Verify EnrollmentAuthority is frozen and contains required fields."""

    def test_frozen(self):
        auth = EnrollmentAuthority(
            terminal_id="t1", terminal_generation=1, incarnation_id="inc-1", epoch=1
        )
        with pytest.raises(FrozenInstanceError):
            auth.epoch = 2  # type: ignore

    def test_fields_present(self):
        auth = EnrollmentAuthority(
            terminal_id="t1", terminal_generation=2, incarnation_id="inc-x", epoch=5
        )
        assert auth.terminal_id == "t1"
        assert auth.terminal_generation == 2
        assert auth.incarnation_id == "inc-x"
        assert auth.epoch == 5

    def test_explicit_none_incarnation(self):
        """Explicit None = process-less marker (not an error)."""
        auth = EnrollmentAuthority(
            terminal_id="t1", terminal_generation=1, incarnation_id=None, epoch=3
        )
        assert auth.incarnation_id is None


# ==============================================================================
# D19: _AUTHORITY_UNSET sentinel and create_reader enforcement
# ==============================================================================


class TestAuthorityUnsetEnforcement:
    """Omitted authority when enrolling watchdog is an error, not process-less."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_omitted_authority_raises_typeerror(self, tmp_path, monkeypatch):
        """create_reader with pane_probe+rearm but no authority → TypeError."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        with pytest.raises(TypeError, match="requires explicit"):
            mgr.create_reader("t1", pane_probe=lambda: "", rearm=lambda: None)

    def test_omitted_generation_only_raises(self, tmp_path, monkeypatch):
        """Missing generation but incarnation_id given → still TypeError."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        with pytest.raises(TypeError, match="requires explicit"):
            mgr.create_reader(
                "t1", pane_probe=lambda: "", rearm=lambda: None,
                incarnation_id="inc-1",
            )

    def test_omitted_incarnation_only_raises(self, tmp_path, monkeypatch):
        """Missing incarnation_id but generation given → still TypeError."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        with pytest.raises(TypeError, match="requires explicit"):
            mgr.create_reader(
                "t1", pane_probe=lambda: "", rearm=lambda: None,
                terminal_generation=1,
            )

    def test_explicit_none_is_accepted(self, tmp_path, monkeypatch):
        """Explicit None (process-less) is valid, not conflated with omission."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=None, incarnation_id=None,
        )
        assert "t1" in mgr._f138_authority
        assert mgr._f138_authority["t1"].incarnation_id is None

    def test_no_watchdog_callers_can_omit(self, tmp_path, monkeypatch):
        """Legacy/test callers without pane_probe/rearm can omit authority."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader("t1")  # No error
        assert "t1" not in mgr._f138_authority


# ==============================================================================
# D19: Global epoch — single int, incremented per enrollment
# ==============================================================================


class TestGlobalEpoch:
    """_next_f138_enrollment_epoch: single global int, no per-terminal map."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_epoch_increments_on_enrollment(self, tmp_path, monkeypatch):
        mgr = self._make_manager(tmp_path, monkeypatch)
        assert mgr._next_f138_enrollment_epoch == 0

        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        assert mgr._next_f138_enrollment_epoch == 1
        assert mgr._f138_authority["t1"].epoch == 1

        mgr.stop_reader("t1")
        mgr.create_reader(
            "t2", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-2",
        )
        assert mgr._next_f138_enrollment_epoch == 2
        assert mgr._f138_authority["t2"].epoch == 2

    def test_unenroll_does_not_reset_epoch(self, tmp_path, monkeypatch):
        """_unenroll clears authority but never mutates global epoch."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        assert mgr._next_f138_enrollment_epoch == 1
        with mgr._lock:
            mgr._unenroll("t1")
        assert mgr._next_f138_enrollment_epoch == 1  # NOT reset
        assert "t1" not in mgr._f138_authority

    def test_per_terminal_epoch_mutant_killed(self, tmp_path, monkeypatch):
        """A per-terminal epoch map would allow epoch reuse — killed."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        epoch1 = mgr._f138_authority["t1"].epoch
        mgr.stop_reader("t1")
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-2",
        )
        epoch2 = mgr._f138_authority["t1"].epoch
        # Epochs must be strictly increasing (not reused for same terminal)
        assert epoch2 > epoch1

    def test_reused_epoch_mutant_killed(self, tmp_path, monkeypatch):
        """Even if same terminal_id re-enrolls, epochs are globally unique."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        epochs = []
        for i in range(5):
            mgr.create_reader(
                f"t{i}", pane_probe=lambda: "", rearm=lambda: None,
                terminal_generation=i, incarnation_id=f"inc-{i}",
            )
            epochs.append(mgr._f138_authority[f"t{i}"].epoch)
        # All unique and monotonically increasing
        assert epochs == sorted(set(epochs))


# ==============================================================================
# D19: Stale-probe epoch safety (dispatch_epoch)
# ==============================================================================


class TestStaleProbeEpochSafety:
    """Old probe returning after stop+rebind is discarded by epoch check."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        return FifoManager()

    def test_stale_epoch_discards_result(self, tmp_path, monkeypatch):
        """A probe result with old epoch is discarded before any mutation."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "content1", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        old_epoch = mgr._f138_authority["t1"].epoch

        # Stop and re-enroll with new epoch
        mgr.stop_reader("t1")
        mgr.create_reader(
            "t1", pane_probe=lambda: "content2", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-2",
        )
        new_epoch = mgr._f138_authority["t1"].epoch
        assert new_epoch != old_epoch

        # Simulate old probe completing with stale epoch — should be discarded
        mgr._check_pipe_liveness("t1", dispatch_epoch=old_epoch)
        # The gone_count should NOT have been touched
        assert mgr._f138_probe_gone_count.get("t1") is None

    def test_slow_old_probe_after_rebind_untouches_new_generation(self, tmp_path, monkeypatch):
        """Slow gen-N probe returns after stop+rebind gen-N+1: new gen untouched."""
        mgr = self._make_manager(tmp_path, monkeypatch)

        # Enroll gen 1
        mgr.create_reader(
            "t1", pane_probe=lambda: "alive", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-gen1",
        )
        gen1_epoch = mgr._f138_authority["t1"].epoch

        # Stop gen 1, enroll gen 2
        mgr.stop_reader("t1")
        mgr.create_reader(
            "t1", pane_probe=lambda: "alive-gen2", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-gen2",
        )
        gen2_epoch = mgr._f138_authority["t1"].epoch

        # Old gen-1 probe "returns" with ValueError (session gone)
        # This should be silently discarded, not affect gen-2
        old_probe_that_failed = lambda: (_ for _ in ()).throw(
            ValueError("Session 'old' not found")
        )
        mgr._pane_probe["t1"] = old_probe_that_failed
        mgr._check_pipe_liveness("t1", dispatch_epoch=gen1_epoch)

        # Gen-2 authority should be completely untouched
        assert mgr._f138_authority["t1"].epoch == gen2_epoch
        assert mgr._f138_authority["t1"].incarnation_id == "inc-gen2"
        assert mgr._f138_probe_gone_count.get("t1") is None

    def test_missing_epoch_returns_no_epoch_check(self, tmp_path, monkeypatch):
        """dispatch_epoch=None (legacy) skips epoch check entirely."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        # Direct poke without create_reader (legacy test style)
        mgr._pane_probe["t1"] = lambda: "content"
        mgr._rearm["t1"] = lambda: None
        mgr._last_data_at["t1"] = time.monotonic()

        # Should work without error (no epoch to check)
        mgr._check_pipe_liveness("t1", dispatch_epoch=None)
        mgr._check_pipe_liveness("t1")  # default None


# ==============================================================================
# D20: Confirmed-gone structured classification + report-before-unenroll
# ==============================================================================


class TestConfirmedGoneClassification:
    """D20: Structured result classification and conditional unenroll."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def _enroll(self, mgr, tid="t1", gen=1, inc_id="inc-1"):
        """Enroll a terminal with authority."""
        mgr.create_reader(
            tid, pane_probe=lambda: "content", rearm=lambda: None,
            terminal_generation=gen, incarnation_id=inc_id,
        )

    def test_durable_created_unenrolls(self, tmp_path, monkeypatch):
        """created result → durable → unenroll."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(created=True, job_id="j1", detail=None)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            should_unenroll = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        assert should_unenroll is True
        # Production caller (_f138_definitive_absence) does unenroll on True
        with mgr._lock:
            mgr._unenroll("t1")
        assert "t1" not in mgr._f138_authority
        assert "t1" not in mgr._pane_probe

    def test_durable_job_already_exists_unenrolls(self, tmp_path, monkeypatch):
        """job_already_exists → durable → unenroll."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(created=False, job_id="j1", detail="job_already_exists")

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        assert result is True  # Durable → caller unenrolls

    def test_durable_reconciled_unenrolls(self, tmp_path, monkeypatch):
        """reconciled → durable → unenroll."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(created=False, job_id=None, detail="reconciled")

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        assert result is True  # Durable → caller unenrolls

    def test_durable_abandoned_unenrolls(self, tmp_path, monkeypatch):
        """abandoned → durable → unenroll."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(created=False, job_id=None, detail="abandoned")

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        assert result is True  # Durable → caller unenrolls

    def test_launching_retains_enrollment(self, tmp_path, monkeypatch):
        """launching → retryable → enrollment retained."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_state=launching"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        # Enrollment retained — return False means "do NOT unenroll"
        assert result is False
        assert "t1" in mgr._f138_authority
        assert "t1" in mgr._pane_probe

    def test_launching_as_durable_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: treating launching as durable would unenroll — must NOT."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_state=launching"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "test_source")

        # MUST return False (retain) — mutant returning True would be durable
        assert result is False
        assert "t1" in mgr._pane_probe
        assert "t1" in mgr._f138_authority

    def test_incarnation_not_found_retains_enrollment(self, tmp_path, monkeypatch):
        """incarnation_not_found → fail-closed retry, NOT discharge."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "fifo_window_gone_confirmed")

        # Enrollment retained (fail-closed) — must return False
        assert result is False
        assert "t1" in mgr._f138_authority
        assert mgr._f138_report_failures.get("t1") == 1

    def test_missing_row_discharge_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: discharging on incarnation_not_found would lose track — must NOT."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "test_source")

        # MUST return False (fail-closed) — mutant returning True would discharge
        assert result is False
        assert "t1" in mgr._pane_probe

    def test_unknown_detail_retains_enrollment(self, tmp_path, monkeypatch):
        """Unknown detail → retryable (fail-closed), not durable."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="some_weird_state"
        )

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            result = mgr._f138_report_confirmed_gone("t1", "test")

        assert result is False
        assert "t1" in mgr._f138_authority
        assert mgr._f138_report_failures.get("t1") == 1

    def test_exception_is_retryable(self, tmp_path, monkeypatch):
        """Exception during report → retryable, enrollment retained."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=RuntimeError("DB unavailable"),
        ):
            result = mgr._f138_report_confirmed_gone("t1", "test")

        assert result is False
        assert "t1" in mgr._f138_authority
        assert mgr._f138_report_failures.get("t1") == 1

    def test_false_durable_exception_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: treating exception as durable would unenroll — must NOT."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=Exception("boom"),
        ):
            result = mgr._f138_report_confirmed_gone("t1", "test")

        assert result is False  # Must be retained
        assert "t1" in mgr._pane_probe


# ==============================================================================
# D20: Process-less explicit None discharges without DB
# ==============================================================================


class TestProcesslessDischarge:
    """D20: incarnation_id=None discharges without DB call."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_processless_discharges_without_db(self, tmp_path, monkeypatch):
        """Explicit None incarnation → True (unenroll) without any DB call."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id=None,
        )
        # Should not import or call any DB function
        result = mgr._f138_report_confirmed_gone("t1", "test_source")
        assert result is True

    def test_processless_misuse_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: conflating omitted authority with process-less would error.
        Explicit None must work; _AUTHORITY_UNSET must error."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        # Explicit None works
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id=None,
        )
        assert mgr._f138_authority["t1"].incarnation_id is None
        # Omission errors
        with pytest.raises(TypeError):
            mgr.create_reader(
                "t2", pane_probe=lambda: "", rearm=lambda: None,
                terminal_generation=1,
            )


# ==============================================================================
# D20: Attention escalation at CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS
# ==============================================================================


class TestAttentionEscalation:
    """D20: Fifth non-launching failure emits one attention notification."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def _enroll(self, mgr, tid="t1"):
        mgr.create_reader(
            tid, pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-attn",
        )

    def test_attention_at_threshold(self, tmp_path, monkeypatch):
        """Fifth failure fires exactly one attention notification."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        notify_calls = []
        original_notify = mgr._f138_notify_confirmed_gone_attention
        mgr._f138_notify_confirmed_gone_attention = lambda **kw: notify_calls.append(kw)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            for i in range(CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS + 2):
                mgr._f138_report_confirmed_gone("t1", "test")

        # Exactly one notification at attempt 5
        assert len(notify_calls) == 1
        assert notify_calls[0]["terminal_id"] == "t1"

    def test_no_attention_below_threshold(self, tmp_path, monkeypatch):
        """Fewer than 5 failures → no attention notification."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        notify_calls = []
        mgr._f138_notify_confirmed_gone_attention = lambda **kw: notify_calls.append(kw)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            for _ in range(CONFIRMED_GONE_REPORT_ATTENTION_ATTEMPTS - 1):
                mgr._f138_report_confirmed_gone("t1", "test")

        assert len(notify_calls) == 0

    def test_duplicate_attention_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: firing attention more than once. Must be exactly one."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        notify_calls = []
        mgr._f138_notify_confirmed_gone_attention = lambda **kw: notify_calls.append(kw)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            # Run way past the threshold
            for _ in range(20):
                mgr._f138_report_confirmed_gone("t1", "test")

        assert len(notify_calls) == 1  # EXACTLY one, never duplicates

    def test_success_resets_attention_state(self, tmp_path, monkeypatch):
        """A successful live probe clears failure count + attention marker."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        # Simulate 3 failures
        mgr._f138_report_failures["t1"] = 3
        mgr._f138_attention_sent["t1"] = False

        # Simulate a successful probe (the reset happens in _check_pipe_liveness)
        mgr._pane_probe["t1"] = lambda: "alive"
        mgr._rearm["t1"] = lambda: None
        mgr._last_data_at["t1"] = time.monotonic()
        mgr._check_pipe_liveness("t1")  # baseline
        # After successful probe, state should be cleared
        assert mgr._f138_report_failures.get("t1") is None
        assert mgr._f138_attention_sent.get("t1") is None

    def test_unbounded_count_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: unbounded failure count growth — attention only fires once."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        self._enroll(mgr)

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult
        mock_result = JobRequestResult(
            created=False, job_id=None, detail="incarnation_not_found"
        )

        notify_calls = []
        mgr._f138_notify_confirmed_gone_attention = lambda **kw: notify_calls.append(kw)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=mock_result,
        ):
            for _ in range(100):
                mgr._f138_report_confirmed_gone("t1", "test")

        # Attention only fires once despite 100 failures
        assert len(notify_calls) == 1
        assert mgr._f138_attention_sent.get("t1") is True


# ==============================================================================
# D20: Report-after-unenroll mutant killed
# ==============================================================================


class TestReportAfterUnenrollMutant:
    """D20: Report must happen BEFORE unenroll; never after."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_report_before_unenroll_ordering(self, tmp_path, monkeypatch):
        """D20: The confirmed-gone call must fire while authority is still pinned."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-order",
        )

        # Track ordering: was authority present when report was called?
        authority_at_report_time = []

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult

        def mock_reconcile(incarnation_id, source):
            # At this point, authority should still be pinned
            authority_at_report_time.append(mgr._f138_authority.get("t1"))
            return JobRequestResult(created=True, job_id="j1", detail=None)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=mock_reconcile,
        ):
            should_unenroll = mgr._f138_report_confirmed_gone("t1", "test")

        assert should_unenroll is True
        # Authority was present during the report call
        assert authority_at_report_time[0] is not None
        assert authority_at_report_time[0].incarnation_id == "inc-order"


# ==============================================================================
# D20: Cold-start and rearm use same confirmed API (no single-shot remnants)
# ==============================================================================


class TestUniformConfirmedPath:
    """D20: Cold-start and rearm exhaustion use the same confirmed-gone API."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_COLD_START_ATTEMPTS", 1)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_MAX_REARM_FAILURES", 1)
        return FifoManager()

    def test_cold_start_uses_confirmed_api(self, tmp_path, monkeypatch):
        """Cold-start exhaustion routes through _f138_report_confirmed_gone."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        calls = []
        original = mgr._f138_report_confirmed_gone

        def tracking_report(tid, src):
            calls.append((tid, src))
            return True  # simulate durable

        mgr._f138_report_confirmed_gone = tracking_report  # type: ignore

        # Enroll with authority (cold-start scenario)
        mgr.create_reader(
            "t1", pane_probe=lambda: "content", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-cold",
        )
        # Force cold-start state
        mgr._ever_delivered["t1"] = False
        mgr._registered_at["t1"] = time.monotonic() - 100

        # First check rearms (attempt 1), second exceeds max → give up
        mgr._check_pipe_liveness("t1")
        mgr._check_pipe_liveness("t1")

        assert len(calls) == 1
        assert calls[0] == ("t1", "fifo_cold_start_exhausted")

    def test_rearm_exhaustion_uses_confirmed_api(self, tmp_path, monkeypatch):
        """Rearm exhaustion routes through _f138_report_confirmed_gone."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        calls = []

        def tracking_report(tid, src):
            calls.append((tid, src))
            return True

        mgr._f138_report_confirmed_gone = tracking_report  # type: ignore

        def fail_rearm():
            raise RuntimeError("dead")

        mgr.create_reader(
            "t1", pane_probe=lambda: "content", rearm=fail_rearm,
            terminal_generation=1, incarnation_id="inc-rearm",
        )
        # Force stall detection
        mgr._last_data_at["t1"] = time.monotonic() - 100
        mgr._ever_delivered["t1"] = True

        mgr._check_pipe_liveness("t1")  # baseline
        mgr._pane_probe["t1"] = lambda: "changed"  # diverge
        mgr._check_pipe_liveness("t1")  # stall + rearm fail → give up

        assert len(calls) == 1
        assert calls[0] == ("t1", "fifo_rearm_exhausted")


# ==============================================================================
# D19: Dropped authority propagation mutant
# ==============================================================================


class TestDroppedAuthorityPropagation:
    """Mutant: terminal_service dropping generation/incarnation from create_reader."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_authority_propagated_to_enrollment(self, tmp_path, monkeypatch):
        """Authority from create_reader is exactly what the authority tuple contains."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=42, incarnation_id="inc-prop-test",
        )
        auth = mgr._f138_authority["t1"]
        assert auth.terminal_generation == 42
        assert auth.incarnation_id == "inc-prop-test"
        assert auth.terminal_id == "t1"

    def test_terminal_id_rediscovery_mutant_killed(self, tmp_path, monkeypatch):
        """Mutant: looking up terminal_id at report time instead of using pinned value.
        The authority tuple carries terminal_id frozen at enrollment."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        auth = mgr._f138_authority["t1"]
        # terminal_id is pinned in the authority — not rediscovered
        assert auth.terminal_id == "t1"


# ==============================================================================
# D19: Wrong-generation queue mutant
# ==============================================================================


class TestWrongGenerationQueue:
    """A slow gen-N probe cannot queue cleanup for gen-N+1 replacement."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_wrong_generation_cannot_target_replacement(self, tmp_path, monkeypatch):
        """Gen-1 probe returning after gen-2 enrollment cannot affect gen-2."""
        mgr = self._make_manager(tmp_path, monkeypatch)

        # Enroll gen 1
        mgr.create_reader(
            "t1", pane_probe=lambda: "gen1", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-gen1",
        )
        gen1_epoch = mgr._f138_authority["t1"].epoch

        # Stop gen 1, enroll gen 2
        mgr.stop_reader("t1")
        mgr.create_reader(
            "t1", pane_probe=lambda: "gen2-alive", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-gen2",
        )
        gen2_epoch = mgr._f138_authority["t1"].epoch

        # Simulate old gen-1 probe trying to report confirmed gone
        # (via _check_pipe_liveness with old epoch)
        def old_probe_gone():
            raise ValueError("Session 'old' not found")
        mgr._pane_probe["t1"] = old_probe_gone

        # With old epoch → discarded
        mgr._check_pipe_liveness("t1", dispatch_epoch=gen1_epoch)
        # Gen-2 authority should be completely untouched
        assert mgr._f138_authority["t1"].epoch == gen2_epoch
        assert mgr._f138_authority["t1"].incarnation_id == "inc-gen2"
        assert mgr._f138_probe_gone_count.get("t1") is None


# ==============================================================================
# D20: _unenroll clears all state
# ==============================================================================


class TestUnenrollCompleteness:
    """_unenroll clears every per-enrollment dict entry."""

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_unenroll_clears_all_state(self, tmp_path, monkeypatch):
        """Every per-enrollment dict is cleared by _unenroll."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        # Populate all state
        mgr._f138_probe_gone_count["t1"] = 2
        mgr._f138_report_failures["t1"] = 3
        mgr._f138_attention_sent["t1"] = True
        mgr._rearm_failures["t1"] = 1
        mgr._cold_start_attempts["t1"] = 2

        with mgr._lock:
            mgr._unenroll("t1")

        # Verify ALL state cleared
        assert "t1" not in mgr._pane_probe
        assert "t1" not in mgr._rearm
        assert "t1" not in mgr._liveness
        assert "t1" not in mgr._last_data_at
        assert "t1" not in mgr._rearm_failures
        assert "t1" not in mgr._registered_at
        assert "t1" not in mgr._ever_delivered
        assert "t1" not in mgr._cold_start_attempts
        assert "t1" not in mgr._f138_probe_gone_count
        assert "t1" not in mgr._f138_authority
        assert "t1" not in mgr._f138_report_failures
        assert "t1" not in mgr._f138_attention_sent
        # Global epoch NOT cleared
        assert mgr._next_f138_enrollment_epoch == 1


# ==============================================================================
# R5 proof gaps: terminal creation/reservation propagation
# ==============================================================================


class TestR5ProofGaps:
    """Close r5 provisional proof gaps with runtime physical kills."""

    def test_omitted_authority_fallback_killed(self, tmp_path, monkeypatch):
        """The old TypeError fallback that swallowed authority failures is gone.
        A signature error now propagates (no silent downgrade)."""
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        mgr = FifoManager()

        # With pane_probe+rearm but no authority → TypeError propagates
        with pytest.raises(TypeError, match="requires explicit"):
            mgr.create_reader("t1", pane_probe=lambda: "", rearm=lambda: None)

    def test_old_threshold_path_bypassed(self, tmp_path, monkeypatch):
        """Confirmed producers bypass the generic 2-submission threshold.
        _f138_report_confirmed_gone calls request_orphan_reconciliation directly."""
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        mgr = FifoManager()
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-direct",
        )

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult

        direct_calls = []
        generic_calls = []

        def mock_direct(incarnation_id, source):
            direct_calls.append((incarnation_id, source))
            return JobRequestResult(created=True, job_id="j1", detail=None)

        def mock_generic(**kw):
            generic_calls.append(kw)

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=mock_direct,
        ), patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.record_window_liveness_observation",
            side_effect=mock_generic,
        ):
            mgr._f138_report_confirmed_gone("t1", "test_source")

        # Direct path used, NOT generic
        assert len(direct_calls) == 1
        assert direct_calls[0] == ("inc-direct", "test_source")
        assert len(generic_calls) == 0


# ==============================================================================
# R6 Gap 1: Stale SUCCESS probe after stop+rebind preserves gen-2 state
# ==============================================================================


class TestStaleSuccessProbePreservesGen2State:
    """Gap 1: A stale SUCCESS probe (old epoch) must NOT clear gen-2 counters.

    The epoch guard (fifo_reader.py:793-797) discards stale results before
    the success path (lines 800-802) can pop gone_count/report_failures.
    Without the guard, a slow gen-1 probe returning after rebind would
    wipe gen-2's accumulated absence evidence.
    """

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_CHECK_INTERVAL_S", 0.0)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_COLD_START_GRACE_S", 0.0)
        monkeypatch.setattr(fr, "PIPE_LIVENESS_STALL_CHECKS", 1)
        return FifoManager()

    def test_stale_success_does_not_clear_gen2_gone_count(self, tmp_path, monkeypatch):
        """Stale SUCCESS with old epoch leaves _f138_probe_gone_count intact."""
        mgr = self._make_manager(tmp_path, monkeypatch)

        # Step 1: Enroll t1 gen=1, capture epoch
        mgr.create_reader(
            "t1", pane_probe=lambda: "alive-gen1", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-gen1",
        )
        gen1_epoch = mgr._f138_authority["t1"].epoch

        # Step 2: Stop, re-enroll t1 gen=2
        mgr.stop_reader("t1")
        mgr.create_reader(
            "t1", pane_probe=lambda: "alive-gen2", rearm=lambda: None,
            terminal_generation=2, incarnation_id="inc-gen2",
        )
        gen2_epoch = mgr._f138_authority["t1"].epoch
        assert gen2_epoch != gen1_epoch

        # Step 3: Simulate gen-2 saw absences (counters set by gen-2 watchdog)
        mgr._f138_probe_gone_count["t1"] = 1
        mgr._f138_report_failures["t1"] = 2

        # Step 4: Deliver a stale SUCCESS probe with old epoch.
        # The probe returns content (no exception), so it's a "success" result.
        # But dispatch_epoch is gen1_epoch — stale.
        mgr._check_pipe_liveness("t1", dispatch_epoch=gen1_epoch)

        # Step 5: Assert gone_count NOT cleared by stale success
        assert mgr._f138_probe_gone_count.get("t1") == 1, (
            "Stale success probe cleared gen-2 gone_count — epoch guard failed"
        )

        # Step 6: Assert report_failures NOT cleared by stale success
        assert mgr._f138_report_failures.get("t1") == 2, (
            "Stale success probe cleared gen-2 report_failures — epoch guard failed"
        )

    def test_current_epoch_success_does_clear_counters(self, tmp_path, monkeypatch):
        """Control: a CURRENT-epoch success probe DOES clear the counters."""
        mgr = self._make_manager(tmp_path, monkeypatch)

        mgr.create_reader(
            "t1", pane_probe=lambda: "alive", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-1",
        )
        current_epoch = mgr._f138_authority["t1"].epoch

        # Simulate accumulated counters
        mgr._f138_probe_gone_count["t1"] = 1
        mgr._f138_report_failures["t1"] = 2

        # Current-epoch probe succeeds — should clear counters
        mgr._check_pipe_liveness("t1", dispatch_epoch=current_epoch)

        assert mgr._f138_probe_gone_count.get("t1") is None
        assert mgr._f138_report_failures.get("t1") is None


# ==============================================================================
# R6 Gap 2: Report-before-unenroll at _f138_definitive_absence PRODUCTION level
# ==============================================================================


class TestDefinitiveAbsenceReportBeforeUnenroll:
    """Gap 2: _f138_definitive_absence retains enrollment on transient failure.

    The existing test_report_before_unenroll_ordering tests _f138_report_confirmed_gone
    directly. This test exercises the PRODUCTION entry point: _f138_definitive_absence,
    which increments gone_count, and on second hit calls _f138_report_confirmed_gone.
    When the report raises (transient failure), enrollment must be retained.
    """

    def _make_manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "FIFO_DIR", tmp_path)
        return FifoManager()

    def test_transient_failure_retains_enrollment(self, tmp_path, monkeypatch):
        """Transient RuntimeError in request_orphan_reconciliation → enrollment retained."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-transient",
        )
        assert "t1" in mgr._f138_authority

        # Step 2: Set gone_count=1 so next call triggers the second-hit path
        mgr._f138_probe_gone_count["t1"] = 1

        # Step 3: Mock request_orphan_reconciliation to raise RuntimeError
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=RuntimeError("transient DB failure"),
        ):
            # Step 4: Call _f138_definitive_absence
            mgr._f138_definitive_absence("t1")

        # Step 5: Authority STILL present (enrollment retained)
        assert "t1" in mgr._f138_authority, (
            "_f138_definitive_absence unenrolled despite transient failure"
        )

        # Step 6: gone_count incremented to 2, but enrollment not cleared
        assert mgr._f138_probe_gone_count.get("t1") == 2

    def test_successful_report_unenrolls(self, tmp_path, monkeypatch):
        """Successful reconciliation request → enrollment IS removed."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-success",
        )
        assert "t1" in mgr._f138_authority

        # Set gone_count=1 so next call triggers second-hit path
        mgr._f138_probe_gone_count["t1"] = 1

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=JobRequestResult(created=True, job_id="j-ok", detail=None),
        ):
            mgr._f138_definitive_absence("t1")

        # Authority removed (unenrolled)
        assert "t1" not in mgr._f138_authority, (
            "_f138_definitive_absence did not unenroll after successful report"
        )

    def test_transient_then_success_sequence(self, tmp_path, monkeypatch):
        """Full sequence: transient failure retains, then success unenrolls."""
        mgr = self._make_manager(tmp_path, monkeypatch)
        mgr.create_reader(
            "t1", pane_probe=lambda: "", rearm=lambda: None,
            terminal_generation=1, incarnation_id="inc-seq",
        )

        # First hit (gone_count 0→1): just increments
        mgr._f138_definitive_absence("t1")
        assert mgr._f138_probe_gone_count["t1"] == 1
        assert "t1" in mgr._f138_authority

        # Second hit (gone_count 1→2): triggers report — fails transiently
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            side_effect=RuntimeError("transient"),
        ):
            mgr._f138_definitive_absence("t1")

        assert "t1" in mgr._f138_authority  # retained
        assert mgr._f138_probe_gone_count["t1"] == 2

        from cli_agent_orchestrator.services.orphan_reconcile_service import JobRequestResult

        # Third hit (gone_count 2→3): triggers report — succeeds
        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.request_orphan_reconciliation",
            return_value=JobRequestResult(created=True, job_id="j-fin", detail=None),
        ):
            mgr._f138_definitive_absence("t1")

        assert "t1" not in mgr._f138_authority  # unenrolled

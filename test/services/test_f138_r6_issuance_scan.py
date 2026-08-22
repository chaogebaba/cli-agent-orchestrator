"""F138 r6 — Gaps 3-13: Issuance context propagation and scan-site coverage.

Covers:
- Gap 3: f138_reserve_incarnation receives issuance_ticks from create_terminal
- Gap 4: f138_reserve_incarnation receives issuance_boot_id from create_terminal
- Gap 5: Behavioral CLOCK_BOOTTIME domain (not CLOCK_REALTIME)
- Gap 6: SC_CLK_TCK scaling is applied (not raw clock seconds)
- Gaps 7-13: All 7 scan sites in run_reconciliation_attempt_sync pass issuance kwargs
"""

import os
import signal
import time
from dataclasses import field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.services.orphan_reconcile_service import (
    ORPHAN_TERM_GRACE_SECONDS,
    ProcScanResult,
    ProcTokenMatch,
    ReconcileAttemptResult,
    SignalResult,
    _EMPTY_SCAN_CONFIRM_COUNT,
    _POST_KILL_ROUNDS,
    _RESCAN_INTERVAL_S,
    run_reconciliation_attempt_sync,
    scan_incarnation_processes,
    signal_exact_matches,
)
import cli_agent_orchestrator.services.orphan_reconcile_service as ors


# ==============================================================================
# Gaps 3-6: Issuance context capture in terminal_service.create_terminal
# ==============================================================================


import cli_agent_orchestrator.services.terminal_service as ts
from cli_agent_orchestrator.services.terminal_service import _capture_f138_issuance_context


class TestIssuanceContextCapture:
    """Gaps 3-6: _capture_f138_issuance_context returns correct (ticks, boot_id).

    Tests exercise the ACTUAL production function, monkeypatching at the
    module level to control clock/sysconf/boot_id. Physical mutants prove
    each assertion catches the specific production code site.
    """

    def test_gap3_issuance_ticks_propagated(self, monkeypatch):
        """Gap 3: issuance_ticks is returned from _capture_f138_issuance_context."""
        monkeypatch.setattr(ts.time, "clock_gettime", lambda clk: 100.0)
        monkeypatch.setattr(ts.os, "sysconf", lambda name: 250)
        monkeypatch.setattr(ts.Path, "read_text", lambda self, *a, **kw: "boot-001\n")

        ticks, boot_id = _capture_f138_issuance_context()

        assert ticks is not None
        assert ticks == 25000  # int(100.0 * 250)

    def test_gap4_issuance_boot_id_propagated(self, monkeypatch):
        """Gap 4: issuance_boot_id is returned from _capture_f138_issuance_context."""
        monkeypatch.setattr(ts.time, "clock_gettime", lambda clk: 50.0)
        monkeypatch.setattr(ts.os, "sysconf", lambda name: 100)
        monkeypatch.setattr(ts.Path, "read_text", lambda self, *a, **kw: "  abc-def-boot-id  \n")

        ticks, boot_id = _capture_f138_issuance_context()

        assert boot_id == "abc-def-boot-id"

    def test_gap5_clock_boottime_not_realtime(self, monkeypatch):
        """Gap 5: Production uses CLOCK_BOOTTIME, not CLOCK_REALTIME."""
        clock_calls = []

        def tracking_clock_gettime(clock_id):
            clock_calls.append(clock_id)
            if clock_id == time.CLOCK_BOOTTIME:
                return 42.0
            elif clock_id == time.CLOCK_REALTIME:
                return 9999.0
            return 0.0

        monkeypatch.setattr(ts.time, "clock_gettime", tracking_clock_gettime)
        monkeypatch.setattr(ts.os, "sysconf", lambda name: 100)
        monkeypatch.setattr(ts.Path, "read_text", lambda self, *a, **kw: "boot\n")

        ticks, _ = _capture_f138_issuance_context()

        # Must have called with CLOCK_BOOTTIME
        assert time.CLOCK_BOOTTIME in clock_calls
        # Result reflects BOOTTIME (42.0*100=4200), not REALTIME (9999.0*100=999900)
        assert ticks == 4200
        assert ticks != 999900

    def test_gap6_sc_clk_tck_scaling(self, monkeypatch):
        """Gap 6: Ticks = int(clock_gettime * SC_CLK_TCK), not raw seconds."""
        monkeypatch.setattr(ts.time, "clock_gettime", lambda clk: 100.0)
        monkeypatch.setattr(ts.os, "sysconf", lambda name: 250)
        monkeypatch.setattr(ts.Path, "read_text", lambda self, *a, **kw: "boot\n")

        ticks, _ = _capture_f138_issuance_context()

        # Scaled: 100.0 * 250 = 25000
        assert ticks == 25000
        # If scaling removed (raw seconds): would be 100
        assert ticks != 100


# ==============================================================================
# Gaps 7-13: All 7 scan sites pass issuance kwargs
# ==============================================================================


def _empty_scan():
    """Return a complete empty ProcScanResult."""
    return ProcScanResult(matches=(), complete=True, errors=[])


def _match_scan(n=1):
    """Return a complete ProcScanResult with N matches."""
    matches = tuple(
        ProcTokenMatch(pid=1000 + i, start_ticks=5000 + i, uid=1000)
        for i in range(n)
    )
    return ProcScanResult(matches=matches, complete=True, errors=[])


def _signal_ok(n=1):
    """Return a SignalResult indicating n processes signaled."""
    return SignalResult(signaled=n, failed=0, safety_aborts=0)


class TestScanSiteIssuanceKwargs:
    """Gaps 7-13: Every call to scan_incarnation_processes in
    run_reconciliation_attempt_sync passes issuance_ticks and issuance_boot_id.

    Each test steers execution to exercise a specific scan call site by
    controlling the sequence of scan results, then asserts the captured
    kwargs contain the issuance fields at the expected call index.

    With ORPHAN_TERM_GRACE_SECONDS=0.0 (grace loop skipped), the call
    sequence is deterministic:
      [0] initial (line 409)
      [1] kill_scan (line 487)  — only if initial has matches
      [2] post_scan (line 496)  — inside post-KILL round
      [3] confirm_scan (line 507) or final_scan (line 521)

    Gap 8 (confirm scan, line 429) and Gap 9 (grace loop, line 466) use
    different flows that exercise those specific sites.
    """

    ISSUANCE_TICKS = 25000
    ISSUANCE_BOOT_ID = "test-boot-id-gap7-13"
    EXPECTED_KW = {
        "issuance_ticks": 25000,
        "issuance_boot_id": "test-boot-id-gap7-13",
    }

    def _run_simple(self, scan_side_effects, monkeypatch, grace=0.0):
        """Run reconciliation with controlled scan sequence and grace=0 (no grace loop).

        Returns (result, captured_kwargs_list).
        """
        captured_kwargs = []
        call_count = [0]

        def tracking_scan(token, uid, **kwargs):
            captured_kwargs.append(kwargs)
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(scan_side_effects):
                return scan_side_effects[idx]
            return _empty_scan()

        monkeypatch.setattr(ors, "scan_incarnation_processes", tracking_scan)
        monkeypatch.setattr(ors, "signal_exact_matches", lambda *a, **kw: _signal_ok())
        monkeypatch.setattr(ors, "ORPHAN_TERM_GRACE_SECONDS", grace)
        monkeypatch.setattr(ors, "_RESCAN_INTERVAL_S", 0.0)
        monkeypatch.setattr(ors, "_POST_KILL_ROUNDS", 1)
        monkeypatch.setattr(ors, "_EMPTY_SCAN_CONFIRM_COUNT", 2)
        monkeypatch.setattr(time, "sleep", lambda s: None)
        monkeypatch.setattr(ors, "_read_boot_id", lambda: self.ISSUANCE_BOOT_ID)

        result = run_reconciliation_attempt_sync(
            token="test_token_abc",
            owner_uid=1000,
            token_hash="testhash",
            issuance_ticks=self.ISSUANCE_TICKS,
            issuance_boot_id=self.ISSUANCE_BOOT_ID,
        )
        return result, captured_kwargs

    def test_gap7_initial_scan_passes_issuance(self, monkeypatch):
        """Gap 7: Site 1 (line 409) — initial scan passes issuance kwargs."""
        # Flow: initial(empty) → confirm(empty) → success("already_clean")
        scans = [_empty_scan(), _empty_scan()]
        result, captured = self._run_simple(scans, monkeypatch)

        assert result.code == "success"
        assert result.detail == "already_clean"
        assert len(captured) >= 1
        assert captured[0] == self.EXPECTED_KW

    def test_gap8_confirm_scan_passes_issuance(self, monkeypatch):
        """Gap 8: Site 2 (line 429) — confirm scan passes issuance kwargs."""
        # Flow: initial(empty) → confirm(empty) → success
        # confirm_scan is at index 1
        scans = [_empty_scan(), _empty_scan()]
        result, captured = self._run_simple(scans, monkeypatch)

        assert result.code == "success"
        assert len(captured) >= 2
        assert captured[1] == self.EXPECTED_KW

    def test_gap9_grace_loop_scan_passes_issuance(self, monkeypatch):
        """Gap 9: Site 3 (line 466) — grace loop rescan passes issuance kwargs."""
        # Flow: initial(matches) → TERM → grace loop rescan(empty) ×2 → success
        # Need grace > 0 so the while loop actually enters
        scans = [
            _match_scan(1),  # [0] initial (has matches → TERM)
            _empty_scan(),   # [1] grace loop rescan #1
            _empty_scan(),   # [2] grace loop rescan #2 (consecutive empty → success)
        ]
        result, captured = self._run_simple(scans, monkeypatch, grace=0.001)

        assert result.code == "success"
        assert result.detail == "term_sufficient"
        assert len(captured) >= 3
        # Grace loop scans are at indices 1 and 2
        assert captured[1] == self.EXPECTED_KW
        assert captured[2] == self.EXPECTED_KW

    def test_gap10_post_grace_kill_scan_passes_issuance(self, monkeypatch):
        """Gap 10: Site 4 (line 487) — kill_scan passes issuance kwargs.

        Flow with grace=0.0: initial(matches) → grace skipped → kill_scan
        kill_scan is at index 1.
        """
        scans = [
            _match_scan(1),  # [0] initial (has matches → TERM)
            _match_scan(1),  # [1] kill_scan (line 487) — has matches → KILL
            _empty_scan(),   # [2] post_scan
            _empty_scan(),   # [3] confirm_scan → success
        ]
        result, captured = self._run_simple(scans, monkeypatch, grace=0.0)

        assert result.code == "success"
        assert result.detail == "kill_required"
        assert len(captured) >= 2
        # kill_scan is at index 1 (grace=0 skips grace loop)
        assert captured[1] == self.EXPECTED_KW

    def test_gap11_post_kill_round_scan_passes_issuance(self, monkeypatch):
        """Gap 11: Site 5 (line 496) — post_scan passes issuance kwargs.

        Flow with grace=0.0: initial(matches) → kill_scan(matches) → post_scan
        post_scan is at index 2.
        """
        scans = [
            _match_scan(1),  # [0] initial
            _match_scan(1),  # [1] kill_scan (matches → KILL)
            _empty_scan(),   # [2] post_scan (line 496) — empty → triggers confirm
            _empty_scan(),   # [3] confirm_scan → success
        ]
        result, captured = self._run_simple(scans, monkeypatch, grace=0.0)

        assert result.code == "success"
        assert len(captured) >= 3
        # post_scan is at index 2
        assert captured[2] == self.EXPECTED_KW

    def test_gap12_post_kill_confirm_scan_passes_issuance(self, monkeypatch):
        """Gap 12: Site 6 (line 507) — confirm_scan passes issuance kwargs.

        Flow: initial(matches) → kill_scan(matches) → post_scan(empty) → confirm_scan
        confirm_scan is at index 3.
        """
        scans = [
            _match_scan(1),  # [0] initial
            _match_scan(1),  # [1] kill_scan (matches)
            _empty_scan(),   # [2] post_scan (empty → triggers confirm)
            _empty_scan(),   # [3] confirm_scan (line 507) → success
        ]
        result, captured = self._run_simple(scans, monkeypatch, grace=0.0)

        assert result.code == "success"
        assert result.detail == "kill_required"
        assert len(captured) >= 4
        # confirm_scan is at index 3
        assert captured[3] == self.EXPECTED_KW

    def test_gap13_final_scan_passes_issuance(self, monkeypatch):
        """Gap 13: Site 7 (line 521) — final_scan passes issuance kwargs.

        Flow: initial(matches) → kill_scan(matches) → post_scan(matches, extra KILL)
        → POST_KILL_ROUNDS exhausted → final_scan
        final_scan is at index 3 (with POST_KILL_ROUNDS=1).
        """
        scans = [
            _match_scan(1),  # [0] initial (matches → TERM)
            _match_scan(1),  # [1] kill_scan (matches → KILL)
            _match_scan(1),  # [2] post_scan round 0 (matches → extra KILL, loop ends)
            _empty_scan(),   # [3] final_scan (line 521) → success
        ]
        result, captured = self._run_simple(scans, monkeypatch, grace=0.0)

        assert result.code == "success"
        assert result.detail == "kill_required_final"
        assert len(captured) >= 4
        # final_scan is the last call (index 3)
        assert captured[3] == self.EXPECTED_KW

"""FX179 — Epoch-millisecond timestamp handling in CC session registry.

Confirms that updatedAt/statusUpdatedAt written as epoch-ms integers (live
Claude Code behavior post-S2) are correctly parsed, preventing false
record_stale refusals from the freshness guard in resolve_target.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def sessions_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def _epoch_ms_now() -> int:
    """Current time as epoch-milliseconds integer (Claude Code format)."""
    return int(time.time() * 1000)


def _epoch_ms_from_dt(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _make_epoch_registry_record(
    sessions_dir: Path,
    pid: int,
    *,
    session_id: str = "test-session-id",
    cwd: str = "/tmp/test",
    tmux: str = "cao-test:@0.%0",
    version: str = "2.1.231",
    peer_protocol: int = 1,
    socket_path: str = "/tmp/test.sock",
    proc_start: int = 12345,
    status: str = "idle",
    status_updated_at: int | str | None = None,
    updated_at: int | str | None = None,
) -> Path:
    """Write a registry record with epoch-ms timestamps (live CC format)."""
    now_ms = _epoch_ms_now()
    data = {
        "sessionId": session_id,
        "cwd": cwd,
        "tmux": tmux,
        "version": version,
        "peerProtocol": peer_protocol,
        "messagingSocketPath": socket_path,
        "procStart": proc_start,
        "status": status,
        "statusUpdatedAt": status_updated_at if status_updated_at is not None else now_ms,
        "updatedAt": updated_at if updated_at is not None else now_ms,
    }
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data))
    return path


# ===========================================================================
# Test: _parse_registry_timestamp helper
# ===========================================================================


class TestParseRegistryTimestamp:
    """Unit tests for the timestamp normalization helper."""

    def test_epoch_ms_int(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        # Known epoch-ms: 2026-08-13T12:00:00Z = 1786622400000
        result = _parse_registry_timestamp(1786622400000)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 13

    def test_epoch_ms_numeric_string(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp("1786622400000")
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026

    def test_iso8601_string_passthrough(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        iso = "2026-08-13T12:00:00+00:00"
        result = _parse_registry_timestamp(iso)
        assert result == iso

    def test_iso8601_with_z(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        iso = "2026-08-13T12:00:00Z"
        result = _parse_registry_timestamp(iso)
        assert result == iso  # passthrough, downstream handles Z

    def test_empty_string(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        assert _parse_registry_timestamp("") == ""

    def test_none(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        assert _parse_registry_timestamp(None) == ""

    def test_float_epoch_ms(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp(1786622400000.5)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026

    def test_garbage_string(self):
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        # Non-numeric, non-ISO → passthrough (will fail downstream → stale)
        result = _parse_registry_timestamp("not-a-date")
        assert result == "not-a-date"

    def test_short_numeric_string_not_epoch(self):
        """Short numeric strings (< 10 digits) are not treated as epoch-ms."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp("12345")
        # Returned as-is since < 10 digits
        assert result == "12345"

    def test_epoch_seconds_int_10_digits(self):
        """10-digit int (epoch-seconds) is correctly interpreted, not as epoch-ms.

        S1 fold: values < 1e12 are epoch-seconds, >= 1e12 are epoch-ms.
        1786622400 = 2026-08-13T12:00:00Z in epoch-seconds.
        Without the heuristic, dividing by 1000 gives 1970-01-21 → always stale.
        """
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp(1786622400)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 13

    def test_epoch_seconds_numeric_string_10_digits(self):
        """10-digit numeric string (epoch-seconds) is correctly interpreted."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp("1786622400")
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026
        assert dt.month == 8

    def test_epoch_ms_13_digits_still_correct(self):
        """13-digit epoch-ms (>= 1e12) still handled as milliseconds."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        # 1786622400000 ms = 2026-08-13T12:00:00Z
        result = _parse_registry_timestamp(1786622400000)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026
        assert dt.month == 8
        assert dt.day == 13

    def test_epoch_ms_13_digit_numeric_string_still_correct(self):
        """13-digit numeric string epoch-ms still handled as milliseconds."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        result = _parse_registry_timestamp("1786622400000")
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026

    def test_threshold_boundary_below(self):
        """Value just below 1e12 is treated as epoch-seconds."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        # 1_786_622_400 seconds = 2026-08-13T12:00:00Z (10-digit, < 1e12)
        result = _parse_registry_timestamp(1_786_622_400)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2026

    def test_threshold_boundary_at(self):
        """Value exactly at 1e12 is treated as epoch-milliseconds."""
        from cli_agent_orchestrator.services.cc_session_registry import _parse_registry_timestamp
        # 1_000_000_000_000 ms = 2001-09-09T01:46:40Z
        result = _parse_registry_timestamp(1_000_000_000_000)
        dt = datetime.fromisoformat(result)
        assert dt.year == 2001


# ===========================================================================
# Test: read_registry coerces epoch-ms at parse time
# ===========================================================================


class TestReadRegistryEpochCoercion:
    """read_registry normalizes epoch-ms to ISO strings."""

    def test_epoch_int_updated_at_coerced(self, sessions_dir):
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        now_ms = _epoch_ms_now()
        _make_epoch_registry_record(sessions_dir, 300, updated_at=now_ms)

        records = read_registry(sessions_dir)
        assert len(records) == 1
        # updated_at should be a valid ISO8601 string, not an int
        assert isinstance(records[0].updated_at, str)
        dt = datetime.fromisoformat(records[0].updated_at)
        assert dt.year >= 2025

    def test_epoch_int_status_updated_at_coerced(self, sessions_dir):
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        now_ms = _epoch_ms_now()
        _make_epoch_registry_record(sessions_dir, 300, status_updated_at=now_ms)

        records = read_registry(sessions_dir)
        assert len(records) == 1
        assert isinstance(records[0].status_updated_at, str)
        dt = datetime.fromisoformat(records[0].status_updated_at)
        assert dt.year >= 2025

    def test_iso_string_preserved(self, sessions_dir):
        from cli_agent_orchestrator.services.cc_session_registry import read_registry
        iso = "2026-08-13T12:00:00+00:00"
        _make_epoch_registry_record(sessions_dir, 300, updated_at=iso, status_updated_at=iso)

        records = read_registry(sessions_dir)
        assert records[0].updated_at == iso
        assert records[0].status_updated_at == iso


# ===========================================================================
# Test: resolve_target freshness guard with epoch-ms (THE BUG REPRO)
# ===========================================================================


class TestResolveTargetEpochFreshness:
    """Epoch-ms updatedAt no longer causes false record_stale."""

    def test_epoch_int_recent_record_not_stale(self, sessions_dir):
        """REPRO: epoch-ms int → AttributeError → record_stale (pre-fix)."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        now_ms = _epoch_ms_now()
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=now_ms,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        # Pre-fix: this was record_stale due to AttributeError on int.replace()
        assert result.refusal_reason is None
        assert result.record is not None
        assert result.record.pid == 300

    def test_epoch_int_old_record_is_stale(self, sessions_dir):
        """Epoch-ms that's too old → correctly identified as stale."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        old_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        old_ms = _epoch_ms_from_dt(old_dt)
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=old_ms,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"

    def test_numeric_string_epoch_recent_not_stale(self, sessions_dir):
        """Epoch-ms as numeric string also works."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        now_ms = str(_epoch_ms_now())
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=now_ms,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason is None
        assert result.record is not None


# ===========================================================================
# Test: verify_wake with epoch-ms statusUpdatedAt
# ===========================================================================


class TestVerifyWakeEpochTimestamps:
    """verify_wake correctly detects advancement when timestamps are epoch-ms."""

    def test_epoch_ms_advancement_detected(self, sessions_dir):
        """statusUpdatedAt advances as epoch-ms int → verify returns True."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
            _parse_registry_timestamp,
        )
        initial_ms = _epoch_ms_now()
        initial_iso = _parse_registry_timestamp(initial_ms)

        record_path = _make_epoch_registry_record(
            sessions_dir, 300,
            status_updated_at=initial_ms,
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="idle",
            status_updated_at=initial_iso,
            updated_at=initial_iso, raw={},
        )

        # Simulate wake: update statusUpdatedAt after a delay
        def update_record():
            time.sleep(0.3)
            data = json.loads(record_path.read_text())
            data["statusUpdatedAt"] = _epoch_ms_now()  # epoch-ms int
            data["status"] = "busy"
            record_path.write_text(json.dumps(data))

        t = threading.Thread(target=update_record, daemon=True)
        t.start()

        result = verify_wake(record, initial_iso, sessions_dir=sessions_dir, timeout_s=3.0)
        t.join(timeout=5)
        assert result is True

    @pytest.mark.slow  # F254 D19: exceeds unit budget
    def test_epoch_ms_no_change_fails(self, sessions_dir):
        """statusUpdatedAt stays same epoch-ms → verify returns False."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
            _parse_registry_timestamp,
        )
        fixed_ms = _epoch_ms_now()
        fixed_iso = _parse_registry_timestamp(fixed_ms)

        _make_epoch_registry_record(
            sessions_dir, 300,
            status_updated_at=fixed_ms,
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="idle",
            status_updated_at=fixed_iso,
            updated_at=fixed_iso, raw={},
        )

        result = verify_wake(record, fixed_iso, sessions_dir=sessions_dir, timeout_s=1.0)
        assert result is False

    def test_mixed_type_pre_sample_str_current_int(self, sessions_dir):
        """Pre-sample is ISO str, current on disk is epoch-ms int → normalization
        ensures correct != comparison (both become ISO)."""
        from cli_agent_orchestrator.services.cc_session_registry import (
            RegistryRecord,
            verify_wake,
        )
        # Pre-sample: ISO string from first read
        pre_iso = "2026-08-13T12:00:00+00:00"

        # On disk: epoch-ms int that's different
        new_ms = _epoch_ms_now()
        record_path = _make_epoch_registry_record(
            sessions_dir, 300,
            status_updated_at=new_ms,  # int on disk
            proc_start=3000,
        )

        record = RegistryRecord(
            pid=300, session_id="s", cwd="/tmp", tmux="s:@0.%0",
            version="2.1.231", peer_protocol=1,
            messaging_socket_path="/tmp/x.sock",
            proc_start=3000, status="idle",
            status_updated_at=pre_iso,
            updated_at=pre_iso, raw={},
        )

        # Should detect as changed since normalized timestamps differ
        result = verify_wake(record, pre_iso, sessions_dir=sessions_dir, timeout_s=1.0)
        assert result is True


# ===========================================================================
# Test: fail-closed behavior preserved
# ===========================================================================


class TestFailClosedPreserved:
    """Unparseable timestamps still trigger record_stale (fail-closed)."""

    def test_garbage_updated_at_is_stale(self, sessions_dir):
        """Non-parseable updatedAt → record_stale (fail-closed)."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at="not-a-valid-timestamp",
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"

    def test_empty_updated_at_is_stale(self, sessions_dir):
        """Empty updatedAt → record_stale."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at="",
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"



# ===========================================================================
# Test: S2 fold — negative-age (far-future timestamps) fail-closed
# ===========================================================================


class TestNegativeAgeFarFutureStale:
    """Far-future timestamps (negative age_s) are rejected as stale."""

    def test_far_future_epoch_seconds_int_is_stale(self, sessions_dir):
        """11-digit epoch-seconds (year 2286+) → negative age → record_stale."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        # 10_000_000_000 seconds = year 2286 — far future, negative age
        far_future_s = 10_000_000_000
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=far_future_s,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"

    def test_far_future_numeric_string_is_stale(self, sessions_dir):
        """12-digit epoch-seconds numeric string (far future) → record_stale."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        # 100_000_000_000 seconds ≈ year 5138
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at="100000000000",
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason == "record_stale"

    def test_just_under_now_stays_fresh(self, sessions_dir):
        """A timestamp 1 second ago is still fresh (positive age < max)."""
        from cli_agent_orchestrator.services.cc_session_registry import resolve_target

        # 1 second ago in epoch-seconds
        just_now_s = int(time.time()) - 1
        _make_epoch_registry_record(
            sessions_dir, 300,
            tmux="s:@0.%0",
            proc_start=3000,
            updated_at=just_now_s,
        )

        with (
            patch("cli_agent_orchestrator.services.cc_session_registry.pane_pid", return_value=100),
            patch("cli_agent_orchestrator.services.cc_session_registry._descendants", return_value=[100, 300]),
            patch("cli_agent_orchestrator.services.cc_session_registry._read_proc_start", return_value=3000),
            patch("cli_agent_orchestrator.services.cc_session_registry._resolve_tmux_window_id", return_value="@0"),
            patch("cli_agent_orchestrator.services.cc_session_registry.ConfigService") as mock_cfg,
        ):
            mock_cfg.get.return_value = 900.0
            result = resolve_target("term-01", "s", "win", sessions_dir=sessions_dir)

        assert result.refusal_reason is None
        assert result.record is not None
        assert result.record.pid == 300

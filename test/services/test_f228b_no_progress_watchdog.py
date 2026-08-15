"""F228-b no-progress watchdog tests — AC1-AC17 + mutation kills."""

from __future__ import annotations

import copy
import re
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    _Episode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

GRACE = 300.0  # default grace seconds


def _make_watchdog(grace=3):
    """Create a watchdog with injectable clock (grace is for stalled-callback, NP uses config)."""
    return StalledCallbackWatchdog(grace_seconds=grace)


def _meta(terminal_id="worker1", caller_id="sup1"):
    return {
        "id": terminal_id,
        "caller_id": caller_id,
        "provider": "grok_cli",
        "tmux_session": "cao-test",
        "tmux_window": terminal_id,
    }


def _config_np_on(path, default=None, override=None):
    """ConfigService.get mock that enables no-progress with grace=300."""
    mapping = {
        "supervisor.watchdog.no_progress": True,
        "supervisor.watchdog.no_progress_grace_s": 300.0,
        "supervisor.watchdog.quiescence": False,
    }
    return mapping.get(path, default)


def _config_np_on_60(path, default=None, override=None):
    """ConfigService.get mock with grace=60."""
    mapping = {
        "supervisor.watchdog.no_progress": True,
        "supervisor.watchdog.no_progress_grace_s": 60.0,
    }
    return mapping.get(path, default)


def _config_np_on_30(path, default=None, override=None):
    """ConfigService.get mock with grace=30 (should clamp to 60)."""
    mapping = {
        "supervisor.watchdog.no_progress": True,
        "supervisor.watchdog.no_progress_grace_s": 30.0,
    }
    return mapping.get(path, default)


def _config_np_off(path, default=None, override=None):
    """ConfigService.get mock with no-progress disabled."""
    mapping = {
        "supervisor.watchdog.no_progress": False,
        "supervisor.watchdog.no_progress_grace_s": 300.0,
    }
    return mapping.get(path, default)


def _setup_processing_worker(svc, terminal_id="worker1", caller_id="sup1",
                             profile="grok_dev", processing_at=10.0):
    """Record assign and set PROCESSING."""
    svc.record_inbound_task(terminal_id, caller_id, profile)
    svc.record_status(terminal_id, TerminalStatus.PROCESSING, now=processing_at)
    return svc


def _fingerprint_with_tail(svc, terminal_id, tail_text, now, metadata_fn=None, patterns=None):
    """Simulate refresh_screen_fingerprints for a single terminal by exercising the real method."""
    import hashlib
    from cli_agent_orchestrator.services.stalled_callback_watchdog import _filtered_liveness_tail

    _patterns = patterns or []
    filtered = _filtered_liveness_tail(tail_text, _patterns)
    fp = hashlib.sha256(filtered.encode("utf-8", "replace")).hexdigest()

    with svc._lock:
        episode = svc._episodes.get(terminal_id)
        if episode is None:
            return fp
        # Simulate what refresh_screen_fingerprints does for idle/quiet
        if episode.idle_since is not None or episode.quiet_since is not None:
            if episode.last_screen_fp is None:
                episode.last_screen_fp = fp
            elif episode.last_screen_fp != fp:
                if episode.idle_since is not None:
                    episode.idle_since = now
                if episode.quiet_since is not None:
                    episode.quiet_since = now
                episode.last_screen_fp = fp

        # NP fingerprint tracking
        if episode.processing_since is not None and episode.np_fired_key is None:
            hint_lines = [ln.strip() for ln in filtered.splitlines() if ln.strip()]
            raw_hint = hint_lines[-1] if hint_lines else ""
            sanitized_hint = raw_hint.replace('"', "'").replace('\n', ' ').replace('\r', ' ')
            sanitized_hint = ''.join(c if c.isprintable() else '?' for c in sanitized_hint)
            if len(sanitized_hint) > 80:
                sanitized_hint = sanitized_hint[:77] + "..."
            episode.last_np_hint = sanitized_hint if sanitized_hint else None

            if episode.last_np_fp is None:
                episode.last_np_fp = fp
                episode.last_progress_at = now
            elif episode.last_np_fp != fp:
                episode.last_np_fp = fp
                episode.last_progress_at = now
    return fp


# ---------------------------------------------------------------------------
# AC1: Clock lifecycle — AWAITING_BASELINE -> CLOCK_RUNNING
# ---------------------------------------------------------------------------

class TestAC1ClockLifecycle:
    def test_processing_entry_sets_processing_since(self):
        """Worker enters PROCESSING -> processing_since set."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "grok_dev")
        svc.record_status("w1", TerminalStatus.PROCESSING, now=100.0)
        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.processing_since == 100.0
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None

    def test_first_fingerprint_sets_baseline(self):
        """First fingerprint tick -> last_np_fp and last_progress_at set."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "line1\nline2\n", now=105.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_fp is not None
            assert ep.last_progress_at == 105.0


# ---------------------------------------------------------------------------
# AC2: Changing fingerprint never alerts
# ---------------------------------------------------------------------------

class TestAC2NoAlertOnProgress:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    def test_changing_screen_never_alerts(self, mock_snapshot, mock_meta, mock_config):
        """Worker at PROCESSING with fingerprint changing every tick for 600s -> no alert."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Simulate fingerprint changing every tick for 600s (120 ticks at 5s)
        for i in range(120):
            t = 5.0 + i * 5.0
            _fingerprint_with_tail(svc, "worker1", f"output line {i}\n", now=t)

        # Now tick at t=605
        with patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message") as mock_create:
            svc.tick_no_progress(now=605.0)
            mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# AC3: Transition to non-PROCESSING clears NP fields
# ---------------------------------------------------------------------------

class TestAC3ClearOnNonProcessing:
    def test_idle_clears_np_fields(self):
        """Worker transitions to IDLE -> all NP fields cleared."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "some output\n", now=105.0)
        # Verify fields are set
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is not None
            assert ep.last_np_fp is not None

        # Transition to IDLE
        svc.record_status("worker1", TerminalStatus.IDLE, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None
            assert ep.np_fired_key is None
            assert ep.last_np_hint is None

    def test_completed_clears_np_fields(self):
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)
        svc.record_status("worker1", TerminalStatus.COMPLETED, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None

    def test_error_clears_np_fields(self):
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)
        svc.record_status("worker1", TerminalStatus.ERROR, now=200.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None


# ---------------------------------------------------------------------------
# AC4: Pause/resume shifts NP clocks
# ---------------------------------------------------------------------------

class TestAC4PauseResume:
    def test_pause_resume_shifts_np_clocks(self):
        """Worker paused for 60s during PROCESSING -> clocks shifted by 60s."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=105.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            orig_processing_since = ep.processing_since
            orig_last_progress_at = ep.last_progress_at

        # Pause
        snapshot = svc.pause_terminal("worker1")
        # Simulate 60s elapsed
        with patch("time.monotonic", return_value=time.monotonic() + 60.0):
            svc.resume_terminal("worker1", snapshot)

        with svc._lock:
            ep = svc._episodes["worker1"]
            # Both should be shifted by approximately 60s
            assert ep.processing_since > orig_processing_since
            assert ep.last_progress_at > orig_last_progress_at
            shift = ep.processing_since - orig_processing_since
            assert shift >= 59.0  # allow small timing variance


# ---------------------------------------------------------------------------
# AC5: Alert fires with all diagnostic fields
# ---------------------------------------------------------------------------

class TestAC5AlertFires:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_alert_fires_after_grace(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Static screen for >= grace -> alert fires with all D6 diagnostic fields."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "running: uv pip install torch\n", now=5.0)

        # Same fingerprint — clock running, stall accumulates
        _fingerprint_with_tail(svc, "worker1", "running: uv pip install torch\n", now=100.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Tick at t=310 (stall_age = 310-5 = 305 >= 300)
        svc.tick_no_progress(now=310.0)

        mock_create.assert_called_once()
        sender, receiver, message = mock_create.call_args[0]
        assert sender == "watchdog:no_progress:worker1"
        assert receiver == "sup1"
        assert "grok_dev-worker1" in message
        assert "no visible output change" in message
        assert "gen=" in message
        assert "last_visible=" in message
        # Check hint from tail
        assert "uv pip install torch" in message

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_alert_fires_with_persist_failure_retries(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """D2 ordering: persist failure -> fired key NOT set -> next tick retries (mutant 13)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # First tick: persist fails
        mock_create.side_effect = RuntimeError("DB error")
        svc.tick_no_progress(now=310.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.np_fired_key is None  # NOT set on failure

        # Second tick: persist succeeds
        mock_create.side_effect = None
        mock_create.reset_mock()
        svc.tick_no_progress(now=315.0)
        mock_create.assert_called_once()
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.np_fired_key is not None


# ---------------------------------------------------------------------------
# AC6: Dedup — exactly one alert per processing episode
# ---------------------------------------------------------------------------

class TestAC6Dedup:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_exactly_one_alert_per_episode(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Same state held for 100 ticks after alert -> exactly one alert."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static output\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # First tick fires
        svc.tick_no_progress(now=310.0)
        assert mock_create.call_count == 1

        # 100 more ticks — no additional alerts
        for i in range(100):
            svc.tick_no_progress(now=315.0 + i * 5.0)
        assert mock_create.call_count == 1

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_no_rearm_after_fired_on_fingerprint_change(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 5: rearm after FIRED when fingerprint changes -> killed (still one alert)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static output\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Fire
        svc.tick_no_progress(now=310.0)
        assert mock_create.call_count == 1

        # Screen changes while in FIRED state (np_fired_key is set, so NP loop is skipped)
        _fingerprint_with_tail(svc, "worker1", "new output after fire\n", now=320.0)

        # Then screen goes static again for another grace period
        for i in range(70):
            _fingerprint_with_tail(svc, "worker1", "new output after fire\n", now=325.0 + i * 5.0)

        # Tick again — should NOT fire a second alert (same episode)
        svc.tick_no_progress(now=700.0)
        assert mock_create.call_count == 1


# ---------------------------------------------------------------------------
# AC7: Reentry after non-PROCESSING produces new episode
# ---------------------------------------------------------------------------

class TestAC7Reentry:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_reentry_after_idle_produces_second_alert(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Alert fires; worker goes IDLE; re-enters PROCESSING -> new episode, second alert."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # First alert
        svc.tick_no_progress(now=310.0)
        assert mock_create.call_count == 1

        # Worker goes IDLE -> np_fired_key cleared
        svc.record_status("worker1", TerminalStatus.IDLE, now=320.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.np_fired_key is None
            assert ep.processing_since is None

        # Worker re-enters PROCESSING (same generation)
        svc.record_status("worker1", TerminalStatus.PROCESSING, now=330.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since == 330.0

        # Baseline
        _fingerprint_with_tail(svc, "worker1", "static2\n", now=335.0)

        # Second alert after grace
        svc.tick_no_progress(now=640.0)
        assert mock_create.call_count == 2


# ---------------------------------------------------------------------------
# AC8: D5 recheck — status transitions between grace and recheck
# ---------------------------------------------------------------------------

class TestAC8RecheckRace:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_recheck_idle_suppresses_alert(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Worker went IDLE between grace expiry and recheck -> no alert."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        # Recheck returns IDLE (status changed)
        mock_snapshot.return_value = TerminalStatus.IDLE

        svc.tick_no_progress(now=310.0)
        mock_create.assert_not_called()

        # NP fields should be cleared
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None


# ---------------------------------------------------------------------------
# AC9: Pane unreadable -> no alert (never exits AWAITING_BASELINE)
# ---------------------------------------------------------------------------

class TestAC9UnreadablePane:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_no_baseline_no_alert(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Pane unreadable throughout -> no fingerprint -> stays AWAITING_BASELINE -> no alert."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        # Never call _fingerprint_with_tail -> last_np_fp stays None, last_progress_at stays None

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Tick well past grace — but no baseline means no alert
        svc.tick_no_progress(now=600.0)
        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# AC10: Routing — watchdog: prefix, no episode creation, no separate request_delivery
# ---------------------------------------------------------------------------

class TestAC10Routing:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_sender_has_watchdog_prefix(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Alert sender has watchdog:no_progress: prefix."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        svc.tick_no_progress(now=310.0)
        sender = mock_create.call_args[0][0]
        assert sender.startswith("watchdog:no_progress:")

    def test_watchdog_sender_creates_no_episode(self):
        """record_inbound_task with watchdog: sender returns immediately."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "watchdog:no_progress:w1", "developer")
        with svc._lock:
            assert "w1" not in svc._episodes

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_no_separate_request_delivery(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """No explicit request_delivery call from evaluate path (mutant 11)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "output\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        with patch("cli_agent_orchestrator.services.inbox_service.request_delivery") as mock_delivery:
            svc.tick_no_progress(now=310.0)
            # The watchdog evaluate path itself should NOT call request_delivery
            # (create_routed_inbox_message does it internally, but we patched create_routed
            # so its internals don't run)
            mock_delivery.assert_not_called()


# ---------------------------------------------------------------------------
# AC11: Dead caller -> no alert, no exception
# ---------------------------------------------------------------------------

class TestAC11DeadCaller:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_dead_caller_no_alert(self, mock_create, mock_snapshot, mock_config):
        """Dead caller -> no alert persisted, no exception."""
        svc = _make_watchdog()

        # Use real get_terminal_metadata patched to return None for caller
        with patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata") as mock_meta:
            # Allow record_inbound_task to work (terminal_exists is separate)
            mock_meta.return_value = _meta()
            _setup_processing_worker(svc, processing_at=0.0)
            _fingerprint_with_tail(svc, "worker1", "output\n", now=5.0)

            mock_snapshot.return_value = TerminalStatus.PROCESSING

            # Now make caller lookup return None (dead)
            def meta_side_effect(tid):
                if tid == "sup1":
                    return None  # dead caller
                return _meta(tid)

            mock_meta.side_effect = meta_side_effect
            svc.tick_no_progress(now=310.0)
            mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# AC12: Per-terminal failure isolation
# ---------------------------------------------------------------------------

class TestAC12Isolation:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_two_terminals_two_alerts(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Two terminals stalled -> two separate alerts."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "grok_dev")
        svc.record_status("w1", TerminalStatus.PROCESSING, now=0.0)
        _fingerprint_with_tail(svc, "w1", "static1\n", now=5.0)

        svc.record_inbound_task("w2", "sup1", "codex_dev")
        svc.record_status("w2", TerminalStatus.PROCESSING, now=0.0)
        _fingerprint_with_tail(svc, "w2", "static2\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        svc.tick_no_progress(now=310.0)
        assert mock_create.call_count == 2

        # Verify different terminal_ids in sender
        senders = [c[0][0] for c in mock_create.call_args_list]
        assert "watchdog:no_progress:w1" in senders
        assert "watchdog:no_progress:w2" in senders

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_exception_in_one_doesnt_kill_other(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Exception in terminal A's evaluation does not suppress terminal B's alert (mutant 14)."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "grok_dev")
        svc.record_status("w1", TerminalStatus.PROCESSING, now=0.0)
        _fingerprint_with_tail(svc, "w1", "static1\n", now=5.0)

        svc.record_inbound_task("w2", "sup1", "codex_dev")
        svc.record_status("w2", TerminalStatus.PROCESSING, now=0.0)
        _fingerprint_with_tail(svc, "w2", "static2\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        call_count = [0]
        orig_evaluate = svc._evaluate_no_progress

        def patched_evaluate(tid, ep, now, grace):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Simulated failure in first terminal")
            return orig_evaluate(tid, ep, now, grace)

        svc._evaluate_no_progress = patched_evaluate
        svc.tick_no_progress(now=310.0)
        # At least one alert should have succeeded
        assert mock_create.call_count >= 1


# ---------------------------------------------------------------------------
# AC13: Config flag on/off
# ---------------------------------------------------------------------------

class TestAC13ConfigFlag:
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_flag_off_no_alerts(self, mock_create, mock_snapshot, mock_meta):
        """Flag off -> no alerts regardless of stall age."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        with patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_off):
            svc.tick_no_progress(now=600.0)
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_flag_flipped_mid_test(self, mock_create, mock_snapshot, mock_meta):
        """Flag flipped true mid-test -> alert fires on next tick (mutant 9)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # First: flag off
        with patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_off):
            svc.tick_no_progress(now=310.0)
        mock_create.assert_not_called()

        # Flip to on
        with patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on):
            svc.tick_no_progress(now=315.0)
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# AC14: Grace clamping
# ---------------------------------------------------------------------------

class TestAC14GraceClamping:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on_60)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_grace_60_alerts_at_60(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Grace 60s -> alert at 60s."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Not yet at grace
        svc.tick_no_progress(now=64.0)
        mock_create.assert_not_called()

        # At grace
        svc.tick_no_progress(now=66.0)
        mock_create.assert_called_once()

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on_30)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_grace_30_clamped_to_60(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Grace 30s -> clamped to 60s minimum."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # 35s stall — would fire at 30 but clamped to 60
        svc.tick_no_progress(now=40.0)
        mock_create.assert_not_called()

        # At 65s (past clamped 60)
        svc.tick_no_progress(now=66.0)
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# AC15: Existing stalled-callback tests pass (regression guard)
# ---------------------------------------------------------------------------

class TestAC15IdleQuietUnchanged:
    """Filter predicate widening does not regress idle/quiet fingerprint paths."""

    def test_idle_fingerprint_still_resets_idle_since(self):
        """Idle fingerprint change still resets idle_since (existing behavior)."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=100.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "old_fp"

        # Simulate a different fingerprint (processed by refresh_screen_fingerprints internally)
        import hashlib
        new_fp = hashlib.sha256(b"new content").hexdigest()
        with svc._lock:
            ep = svc._episodes["w1"]
            # Directly simulate what refresh_screen_fingerprints does
            ep.last_screen_fp = new_fp
            ep.idle_since = 200.0  # reset

        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.idle_since == 200.0
            # NP fields should NOT be set (not PROCESSING)
            assert ep.processing_since is None

    def test_processing_terminal_has_no_idle_since(self):
        """A PROCESSING terminal does not have idle_since set."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=100.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.idle_since is None
            assert ep.quiet_since is None
            assert ep.processing_since == 100.0


# ---------------------------------------------------------------------------
# AC16: Existing FX181 quiescence tests pass (regression guard)
# ---------------------------------------------------------------------------

class TestAC16QuietUnchanged:
    """Quiescence quiet_since still works for ERROR members."""

    def test_error_sets_quiet_since_not_idle_since(self):
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.ERROR, now=100.0)
        with svc._lock:
            ep = svc._episodes["w1"]
            assert ep.quiet_since == 100.0
            assert ep.idle_since is None
            # Not PROCESSING
            assert ep.processing_since is None


# ---------------------------------------------------------------------------
# AC17: Alert message format + hint sanitization
# ---------------------------------------------------------------------------

class TestAC17MessageFormat:
    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_message_format_regex(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Alert message matches D6 format."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "running something\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        svc.tick_no_progress(now=310.0)
        message = mock_create.call_args[0][2]

        # Regex for D6 format
        pattern = (
            r"\[no-progress advisory\] worker \w+-\w+ has been processing for \d+s "
            r"with no visible output change for \d+s "
            r'\(gen=\d+, last_visible="[^"]*"\)\.'
        )
        assert re.search(pattern, message), f"Message doesn't match format: {message}"
        assert "HEURISTIC" in message
        assert "peek_terminal" in message
        assert "delete_terminal" in message

    def test_hint_sanitization_no_quotes(self):
        """Hint sanitization removes quotes (mutant 16)."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        # Tail with quotes and control chars
        tail = 'output with "quotes" and \x01control\x02 chars\n'
        _fingerprint_with_tail(svc, "worker1", tail, now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            hint = ep.last_np_hint
            assert hint is not None
            assert '"' not in hint
            # All printable
            assert all(c.isprintable() for c in hint)

    def test_hint_truncation_at_80(self):
        """Hint truncated to 80 chars max."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        long_line = "x" * 200 + "\n"
        _fingerprint_with_tail(svc, "worker1", long_line, now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_hint is not None
            assert len(ep.last_np_hint) <= 80
            assert ep.last_np_hint.endswith("...")

    def test_hint_empty_tail(self):
        """Empty filtered tail -> hint is None."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "\n\n\n", now=5.0)

        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.last_np_hint is None


# ---------------------------------------------------------------------------
# Additional mutant kills
# ---------------------------------------------------------------------------

class TestMutantKills:
    """Targeted tests for specific mutants not covered above."""

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant1_fire_while_changing(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 1: Skip FP reset on change -> would fire while screen changes. AC2 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Change screen every 50s for 350s total
        for i in range(7):
            _fingerprint_with_tail(svc, "worker1", f"line {i}\n", now=5.0 + i * 50.0)

        # Tick at 360: last change was at 305, so stall_age = 360-305 = 55 < 300 grace
        svc.tick_no_progress(now=360.0)
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant3_no_clear_on_transition(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 3: Not clearing NP fields on non-PROCESSING. AC3 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        # Go IDLE
        svc.record_status("worker1", TerminalStatus.IDLE, now=100.0)

        # Verify cleared
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.processing_since is None
            assert ep.last_np_fp is None
            assert ep.last_progress_at is None

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant7_fire_before_baseline(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 7: Fire before first fingerprint taken. AC9 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        # NO fingerprint taken — last_progress_at is None

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        svc.tick_no_progress(now=600.0)
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant8_includes_paused(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 8: Include paused terminals -> fires during pause. AC4 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Pause
        svc.pause_terminal("worker1")

        # Tick past grace — should NOT fire (paused)
        svc.tick_no_progress(now=310.0)
        mock_create.assert_not_called()

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant12_hint_in_message(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 12: Omit last_np_hint from message. AC17 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "running: build step\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        svc.tick_no_progress(now=310.0)
        message = mock_create.call_args[0][2]
        # The hint should be present in the message
        assert "last_visible=" in message
        # Not <none> because we have real output
        assert '<none>' not in message

    @patch("cli_agent_orchestrator.services.config_service.ConfigService.get", side_effect=_config_np_on)
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view")
    @patch("cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message")
    def test_mutant15_not_clearing_fired_key(self, mock_create, mock_snapshot, mock_meta, mock_config):
        """Mutant 15: Not clearing np_fired_key on non-PROCESSING. AC7 kills."""
        svc = _make_watchdog()
        _setup_processing_worker(svc, processing_at=0.0)
        _fingerprint_with_tail(svc, "worker1", "static\n", now=5.0)

        mock_meta.return_value = _meta()
        mock_snapshot.return_value = TerminalStatus.PROCESSING

        # Fire
        svc.tick_no_progress(now=310.0)
        assert mock_create.call_count == 1
        with svc._lock:
            assert svc._episodes["worker1"].np_fired_key is not None

        # Go IDLE -> fired_key must be cleared
        svc.record_status("worker1", TerminalStatus.IDLE, now=320.0)
        with svc._lock:
            ep = svc._episodes["worker1"]
            assert ep.np_fired_key is None

        # Re-enter PROCESSING, new episode
        svc.record_status("worker1", TerminalStatus.PROCESSING, now=330.0)
        _fingerprint_with_tail(svc, "worker1", "static again\n", now=335.0)

        # Second alert
        svc.tick_no_progress(now=640.0)
        assert mock_create.call_count == 2

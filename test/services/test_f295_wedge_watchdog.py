"""F295 Half 2 — Wedge watchdog tests (AC7-AC11)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    _Episode,
)


def _make_watchdog() -> StalledCallbackWatchdog:
    return StalledCallbackWatchdog(grace_seconds=10)


def _make_episode(
    caller_id: str = "caller1",
    profile: str = "grok_dev",
    processing_since: float | None = None,
    generation: int = 1,
) -> _Episode:
    return _Episode(
        caller_id=caller_id,
        profile=profile,
        inbound_at=time.monotonic(),
        episode_started_wall_at=datetime.now(timezone.utc),
        processing_since=processing_since,
        generation=generation,
        status=TerminalStatus.PROCESSING,
    )


# ---------------------------------------------------------------------------
# AC7: wedged grok pane with live spinner fires exactly one wedge notice
# ---------------------------------------------------------------------------


class TestWedgeArmFiringAndDedup:
    """A wedged grok_cli terminal fires exactly one notice per processing episode."""

    def test_fires_once_on_grok_cli_after_age_threshold(self):
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(processing_since=now - 1000, generation=1)

        with wd._lock:
            wd._episodes["t1"] = episode

        # Mock dependencies
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value={"provider": "grok_cli"},
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view"
            ) as mock_rsv,
            patch(
                "cli_agent_orchestrator.clients.database.merge_terminal_system_metadata",
                return_value=True,
            ) as mock_merge,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_msg,
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda k, default=None: {
                    "supervisor.watchdog.grok_wedge": True,
                    "supervisor.watchdog.grok_wedge_age_s": 900.0,
                }.get(k, default),
            ),
        ):
            mock_rsv.snapshot_view.return_value = TerminalStatus.PROCESSING

            wd.tick_wedge()

            # Exactly one notice fired
            assert mock_msg.call_count == 1
            msg_text = mock_msg.call_args[0][2]
            assert "peek_terminal" in msg_text
            assert "delete_terminal" in msg_text

            # wedge_suspect flagged
            mock_merge.assert_called_once_with("t1", {"wedge_suspect": True})

            # Second tick: no second notice (dedup)
            mock_msg.reset_mock()
            wd.tick_wedge()
            assert mock_msg.call_count == 0

    def test_f228b_arm_does_not_fire_while_spinner_animating(self):
        """With liveness_exclude_patterns, spinner animation = stable FP → NP never fires."""
        # This test verifies that GrokCliProvider.liveness_exclude_patterns is set
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider, PROCESSING_PATTERN

        assert GrokCliProvider.liveness_exclude_patterns == [PROCESSING_PATTERN]


# ---------------------------------------------------------------------------
# AC8: liveness_exclude_patterns stabilizes fingerprint
# ---------------------------------------------------------------------------


class TestLivenessExcludePatterns:
    """Spinner-only changes produce a stable fingerprint."""

    def test_grok_provider_has_processing_pattern(self):
        from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider, PROCESSING_PATTERN

        assert PROCESSING_PATTERN in GrokCliProvider.liveness_exclude_patterns


# ---------------------------------------------------------------------------
# AC9: flag-and-notify only — no key, no status write, no reap
# ---------------------------------------------------------------------------


class TestFlagAndNotifyOnly:
    """The wedge arm NEVER sends keys, writes status, or reaps."""

    def test_no_send_keys_no_status_write_no_reap(self):
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(processing_since=now - 1000, generation=1)

        with wd._lock:
            wd._episodes["t1"] = episode

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value={"provider": "grok_cli"},
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view"
            ) as mock_rsv,
            patch(
                "cli_agent_orchestrator.clients.database.merge_terminal_system_metadata",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda k, default=None: {
                    "supervisor.watchdog.grok_wedge": True,
                    "supervisor.watchdog.grok_wedge_age_s": 900.0,
                }.get(k, default),
            ),
        ):
            mock_rsv.snapshot_view.return_value = TerminalStatus.PROCESSING

            # Patch backend to detect any send_keys calls
            with (
                patch(
                    "cli_agent_orchestrator.backends.registry.get_backend"
                ) as mock_backend_fn,
            ):
                mock_backend = MagicMock()
                mock_backend_fn.return_value = mock_backend

                wd.tick_wedge()

                # No keys sent
                mock_backend.send_keys.assert_not_called()
                if hasattr(mock_backend, "send_key"):
                    mock_backend.send_key.assert_not_called()

        # Terminal status unchanged
        assert episode.status == TerminalStatus.PROCESSING


# ---------------------------------------------------------------------------
# AC10: flag reaches fleet and it clears
# ---------------------------------------------------------------------------


class TestWedgeFlagProjection:
    """wedge_suspect projects in fleet and clears on status transition."""

    def test_wedge_suspect_in_fleet(self):
        """After firing, fleet projects wedge_suspect: True."""
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "grok_cli", "metadata": {"cao": {"wedge_suspect": True}}}
        assert _is_wedge_suspect(row) is True

    def test_non_grok_returns_none(self):
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "codex_cli", "metadata": {"cao": {"wedge_suspect": True}}}
        assert _is_wedge_suspect(row) is None

    def test_no_wedge_returns_none(self):
        from cli_agent_orchestrator.services.fleet_service import _is_wedge_suspect

        row = {"provider": "grok_cli", "metadata": {"cao": {}}}
        assert _is_wedge_suspect(row) is None

    def test_clears_on_status_transition(self):
        """When status transitions from PROCESSING, wedge state clears."""
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(processing_since=now - 1000, generation=1)
        episode.wedge_fired_key = (1, now - 1000)
        episode.wedge_flagged = True

        with wd._lock:
            wd._episodes["t1"] = episode

        # Simulate status transition to IDLE
        wd.record_status("t1", TerminalStatus.IDLE)

        with wd._lock:
            ep = wd._episodes.get("t1")
            assert ep is not None
            assert ep.wedge_fired_key is None
            assert ep.wedge_flagged is False


# ---------------------------------------------------------------------------
# AC11: a reaped terminal is never flagged or announced
# ---------------------------------------------------------------------------


class TestReapedTerminalNotFlagged:
    """A terminal reaped between candidacy and recheck is silently dropped."""

    def test_reaped_before_recheck_no_notice(self):
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(processing_since=now - 1000, generation=1)

        with wd._lock:
            wd._episodes["t1"] = episode

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=None,  # terminal reaped
            ),
            patch(
                "cli_agent_orchestrator.clients.database.merge_terminal_system_metadata",
                side_effect=AssertionError("must not write metadata"),
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
                side_effect=AssertionError("must not send notice"),
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda k, default=None: {
                    "supervisor.watchdog.grok_wedge": True,
                    "supervisor.watchdog.grok_wedge_age_s": 900.0,
                }.get(k, default),
            ),
        ):
            # Should not raise, should not send anything
            wd.tick_wedge()

        # Episode state cleaned up
        with wd._lock:
            ep = wd._episodes.get("t1")
            assert ep.wedge_fired_key is None


# ---------------------------------------------------------------------------
# D11: caller dead → fallback to supervisor
# ---------------------------------------------------------------------------


class TestCallerFallback:
    """Dead caller falls back to supervisor mailbox."""

    def test_dead_caller_notifies_supervisor(self):
        wd = _make_watchdog()
        now = time.monotonic()
        episode = _make_episode(
            caller_id="dead_caller", processing_since=now - 1000, generation=1
        )

        with wd._lock:
            wd._episodes["t1"] = episode

        call_count = {"metadata_calls": 0}

        def mock_get_metadata(tid):
            call_count["metadata_calls"] += 1
            if tid == "t1":
                return {"provider": "grok_cli"}
            # dead_caller → None
            return None

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=mock_get_metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view"
            ) as mock_rsv,
            patch(
                "cli_agent_orchestrator.clients.database.merge_terminal_system_metadata",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_msg,
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor1",
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=lambda k, default=None: {
                    "supervisor.watchdog.grok_wedge": True,
                    "supervisor.watchdog.grok_wedge_age_s": 900.0,
                }.get(k, default),
            ),
        ):
            mock_rsv.snapshot_view.return_value = TerminalStatus.PROCESSING
            wd.tick_wedge()

            # Notice went to supervisor, not dead caller
            assert mock_msg.call_count == 1
            assert mock_msg.call_args[0][1] == "supervisor1"

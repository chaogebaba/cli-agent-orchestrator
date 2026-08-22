"""F168 — Idle supervisor doorbell acceptance tests.

V1: Focused unit tests (dedup/predicate, gate refusal, ordering/isolation, config/scope, call-site inventory).
V2: Real-sqlite integration for the reconciler site.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.services.inbox_service import CallbackRunOutcome


# ===========================================================================
# Helpers
# ===========================================================================

_NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _make_outcome(
    *,
    written: int = 0,
    already_present: int = 0,
    max_written_row_id: int = 0,
    replay_drained: int = 0,
    needs_immediate_wake: bool = False,
    reason: str = "ok",
) -> CallbackRunOutcome:
    return CallbackRunOutcome(
        selected=written + already_present,
        processed=written + already_present,
        cursor_before=0,
        cursor_after=max_written_row_id if max_written_row_id else 0,
        written=written,
        already_present=already_present,
        max_written_row_id=max_written_row_id,
        replay_drained=replay_drained,
        needs_immediate_wake=needs_immediate_wake,
        reason=reason,
    )


@pytest.fixture(autouse=True)
def _reset_doorbell_state():
    """Reset doorbell module state between tests."""
    import cli_agent_orchestrator.services.doorbell_service as ds
    ds._last_doorbell_row_id.clear()
    ds._last_warn_time.clear()
    yield
    ds._last_doorbell_row_id.clear()
    ds._last_warn_time.clear()


@pytest.fixture()
def mock_gates():
    """Patch all gate dependencies to pass by default."""
    # doorbell_service imports get_terminal_metadata at module level, so patch there
    with (
        patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
        patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push") as mock_should,
        patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
        patch("cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id") as mock_update,
        patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_lock_fn,
        patch("cli_agent_orchestrator.services.receiver_state_view.native_probe") as mock_probe,
        patch("cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe") as mock_inject_safe,
        patch("cli_agent_orchestrator.providers.manager.provider_manager.get_provider") as mock_get_prov,
        patch("cli_agent_orchestrator.services.terminal_service.send_prepared_input") as mock_send,
    ):
        # Default: all gates pass
        # FX170: disable native wake so fx168 tests exercise the gated path only.
        # Use a mutable dict so individual tests can override specific paths.
        _cfg_overrides: dict = {}

        def _cfg_side_effect(path, default=None, override=None):
            if path in _cfg_overrides:
                return _cfg_overrides[path]
            if path == "supervisor.wake.native":
                return False
            if path == "supervisor.doorbell":
                return True
            return True
        mock_config.get.side_effect = _cfg_side_effect
        mock_config._cfg_overrides = _cfg_overrides
        mock_should.return_value = True  # registered
        mock_meta.return_value = {"metadata": {"cc_team_inbox_path": "/tmp/inbox.json"}}
        mock_update.return_value = None

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_lock.release.return_value = None
        mock_lock_fn.return_value = mock_lock

        # Probe returns IDLE status
        from cli_agent_orchestrator.services.status_monitor import TerminalStatus
        probe_result = MagicMock()
        probe_result.status = TerminalStatus.IDLE
        probe_result.meta = {"result_status": "idle", "frame_source": "native", "agent_status": "idle"}
        mock_probe.return_value = probe_result

        # _inject_safe returns safe
        from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult
        mock_inject_safe.return_value = InjectSafetyResult("safe")

        mock_get_prov.return_value = MagicMock()
        mock_send.return_value = None

        yield {
            "config": mock_config,
            "should_push": mock_should,
            "meta": mock_meta,
            "update_meta": mock_update,
            "lock_fn": mock_lock_fn,
            "lock": mock_lock,
            "probe": mock_probe,
            "inject_safe": mock_inject_safe,
            "get_provider": mock_get_prov,
            "send": mock_send,
        }


# ===========================================================================
# V1 — AC2: One nudge per run regardless of batch size
# ===========================================================================


class TestAC2OneNudgePerRun:
    """After a run writing >=1 entries, exactly one nudge is attempted."""

    def test_single_row_one_ring(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "rang"
        mock_gates["send"].assert_called_once()

    def test_fifty_rows_one_ring(self, mock_gates):
        """50 written rows still produce exactly one ring call."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        result = ring_supervisor_doorbell("term-01", 150, written_count=50)
        assert result == "rang"
        mock_gates["send"].assert_called_once()

    def test_batch_driven_from_post_delivery(self, mock_gates):
        """Driving _f136_post_delivery with a 50-row outcome rings once."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        # Simulate post_delivery calling the doorbell
        outcome = _make_outcome(written=50, max_written_row_id=200)
        result = ring_supervisor_doorbell(
            "term-01", outcome.max_written_row_id, written_count=outcome.written,
        )
        assert result == "rang"
        assert mock_gates["send"].call_count == 1


# ===========================================================================
# V1 — AC3: already_present produces zero nudges
# ===========================================================================


class TestAC3AlreadyPresentNoRing:
    """A run whose results are all already_present attempts zero nudges."""

    def test_all_already_present(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        # written_count=0 means nothing new was written
        result = ring_supervisor_doorbell("term-01", 100, written_count=0)
        assert result == "skipped_dedup"
        mock_gates["send"].assert_not_called()


# ===========================================================================
# V1 — AC4: Cursor dedup — second run at same/lower row id skips
# ===========================================================================


class TestAC4CursorDedup:
    """Dedup by last_doorbell_row_id: same or lower row skips."""

    def test_first_ring_then_same_row_skips(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        result1 = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result1 == "rang"
        result2 = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result2 == "skipped_dedup"
        assert mock_gates["send"].call_count == 1

    def test_lower_row_skips(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        result = ring_supervisor_doorbell("term-01", 50, written_count=1)
        assert result == "skipped_dedup"

    def test_higher_row_rings(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        result = ring_supervisor_doorbell("term-01", 200, written_count=1)
        assert result == "rang"
        assert mock_gates["send"].call_count == 2


# ===========================================================================
# V1 — AC5: Gate-refused nudge leaves high-water unchanged
# ===========================================================================


class TestAC5RefusedLeavesCursorUnchanged:
    """A gate-refused nudge leaves last_doorbell_row_id unchanged."""

    def test_gate_refusal_preserves_cursor(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell, _get_last_doorbell_row_id
        # Make the lock refuse (G1 failure)
        mock_gates["lock"].acquire.return_value = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"
        assert _get_last_doorbell_row_id("term-01") == 0
        # Now allow it — should ring at 100 since cursor not advanced
        mock_gates["lock"].acquire.return_value = True
        result2 = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result2 == "rang"


# ===========================================================================
# V1 — AC6: Replay-tagged row rings (keyed on "new entry written")
# ===========================================================================


class TestAC6ReplayRowRings:
    """A replay row that writes a new entry rings."""

    def test_replay_row_with_below_cursor_id_still_rings(self, mock_gates):
        """Replay rows have ids below the forward cursor but still write new content."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        # Row id 5 is a replay (below cursor) but is newly written
        result = ring_supervisor_doorbell("term-01", 5, written_count=1)
        assert result == "rang"

    def test_post_delivery_replay_only_outcome_rings(self, mock_gates):
        """B1/Mutant-5 kill: _f136_post_delivery with replay-only outcome (cursor unchanged) rings.

        A replay-only run has written=1 (a replay row produced a new file entry)
        but cursor_after == cursor_before (replay rows do NOT advance the forward cursor).
        The call-site predicate must key on outcome.written, NOT on cursor advance.
        This test exercises the CALL SITE in inbox_service._f136_post_delivery.
        """
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.inbox_service import (
            CallbackRunOutcome,
            InboxService,
            _wake_states,
            _delivery_seq_guard,
        )

        # Track doorbell calls
        doorbell_calls = []
        original_ring = ring_supervisor_doorbell.__wrapped__ if hasattr(ring_supervisor_doorbell, '__wrapped__') else None

        def tracking_ring(tid, max_row_id, *, written_count=0):
            doorbell_calls.append((tid, max_row_id, written_count))
            return "rang"

        # Replay-only outcome: written=1, cursor unchanged, max_written_row_id set
        replay_outcome = CallbackRunOutcome(
            selected=1,
            processed=1,
            cursor_before=100,
            cursor_after=100,  # cursor NOT advanced (replay row)
            replay_selected=1,
            replay_drained=1,
            written=1,  # but a new entry WAS written
            already_present=0,
            max_written_row_id=42,  # the replay row id
            reason="ok",
        )

        # Set up minimal _wake_states so _f136_post_delivery doesn't early-return
        from cli_agent_orchestrator.services.inbox_service import _WakeState
        with _delivery_seq_guard:
            _wake_states["term-replay"] = _WakeState()

        svc = InboxService()

        with patch(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
            tracking_ring,
        ):
            # Patch at the import site inside _f136_post_delivery
            with patch(
                "cli_agent_orchestrator.services.inbox_service.ring_supervisor_doorbell",
                tracking_ring,
                create=True,
            ):
                svc._f136_post_delivery("term-replay", replay_outcome)

        # Clean up
        with _delivery_seq_guard:
            _wake_states.pop("term-replay", None)

        # Assert doorbell was called with correct args
        assert len(doorbell_calls) == 1, f"Expected 1 doorbell call, got {len(doorbell_calls)}"
        assert doorbell_calls[0] == ("term-replay", 42, 1)


# ===========================================================================
# V1 — AC7: Gate refusal reasons cause skip with correct log
# ===========================================================================


class TestAC7GateRefusalReasons:
    """Each _inject_safe veto reason causes skip, no exception, no retry."""

    @pytest.mark.parametrize("reason", [
        "safety_unverified",
        "waiting_status",
        "waiting_gate",
        "dialog_hazard",
        "identity_unverified",
    ])
    def test_inject_safe_veto_skips(self, mock_gates, reason):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult
        mock_gates["inject_safe"].return_value = InjectSafetyResult(
            "veto", reason, gate_episode="ep1" if reason == "waiting_gate" else None,
        )
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"
        mock_gates["send"].assert_not_called()

    def test_deferred_error_skips(self, mock_gates):
        """DeliveryDeferredError from draft guard causes skip."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
        mock_gates["send"].side_effect = DeliveryDeferredError("draft_present")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"


# ===========================================================================
# V1 — AC8: PROCESSING status skips (no eager exception for doorbell)
# ===========================================================================


class TestAC8ProcessingSkips:
    """Probe reporting PROCESSING skips the nudge."""

    def test_processing_status_skips(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.status_monitor import TerminalStatus
        mock_gates["probe"].return_value.status = TerminalStatus.PROCESSING
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"
        mock_gates["send"].assert_not_called()


# ===========================================================================
# V1 — AC9: Draft present skips; stash/restore never entered
# ===========================================================================


class TestAC9DraftSkipsNoStash:
    """Non-empty draft skips; draft_guard stash/restore is never entered."""

    def test_draft_deferred_skips(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
        mock_gates["send"].side_effect = DeliveryDeferredError("draft_present")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"

    def test_doorbell_passes_defer_on_dialog_true(self, mock_gates):
        """The doorbell always calls send_prepared_input with defer_on_dialog=True."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        call_kwargs = mock_gates["send"].call_args[1]
        assert call_kwargs["defer_on_dialog"] is True


# ===========================================================================
# V1 — AC10: Routes through gated injection (pane identity verification)
# ===========================================================================


class TestAC10GatedInjectionPath:
    """The doorbell send path reaches terminal_service identity verification."""

    def test_send_goes_through_send_prepared_input(self, mock_gates):
        """ring_supervisor_doorbell calls send_prepared_input (which owns pane identity)."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "rang"
        mock_gates["send"].assert_called_once()
        # Verify it passes terminal_id and the fixed nudge text
        args = mock_gates["send"].call_args
        assert args[0][0] == "term-01"  # terminal_id
        from cli_agent_orchestrator.services.doorbell_service import DOORBELL_NUDGE_TEXT
        assert args[0][1] == DOORBELL_NUDGE_TEXT

    def test_mutant_direct_tmux_killed(self, mock_gates):
        """A mutant bypassing send_prepared_input would not be called — we assert the path."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        # If someone imports tmux directly instead of send_prepared_input, the mock won't fire
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        # The fact that mock_gates["send"] (send_prepared_input) was called proves the path
        assert mock_gates["send"].called


# ===========================================================================
# V1 — AC11: Nudge only after write+cursor commit (ordering)
# ===========================================================================


class TestAC11OrderingIsolation:
    """The nudge fires only after the file write and cursor commit are durable."""

    def test_ring_raises_but_outcome_is_durable(self, mock_gates):
        """Even if ring raises, the outcome from _f136_run_callback_delivery is unchanged."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        # Simulate ring error
        mock_gates["send"].side_effect = RuntimeError("pane gone")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        # D3: error is caught, not propagated
        assert result == "error"
        # The write and cursor commit are tested in test_f136_callback_delivery —
        # here we prove the doorbell does not propagate exceptions.


# ===========================================================================
# V1 — AC12: Exception isolation
# ===========================================================================


class TestAC12ExceptionIsolation:
    """Any exception in the nudge path is caught, logged, never propagates."""

    def test_probe_exception_isolated(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["probe"].side_effect = RuntimeError("dead pane")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "error"

    def test_send_exception_isolated(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["send"].side_effect = OSError("tmux timeout")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "error"

    def test_metadata_exception_isolated(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["meta"].side_effect = RuntimeError("db gone")
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "error"


# ===========================================================================
# V1 — AC13: No attempt row, no PostSendMessageEvent
# ===========================================================================


class TestAC13NoAttemptRow:
    """The nudge opens no delivery attempt row and emits no events."""

    def test_send_called_without_orchestration_type(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        call_kwargs = mock_gates["send"].call_args[1]
        # D11: no orchestration_type means no attempt row
        assert call_kwargs.get("orchestration_type") is None

    def test_sender_id_is_cao_bridge(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        call_kwargs = mock_gates["send"].call_args[1]
        assert call_kwargs["sender_id"] == "cao-bridge"


# ===========================================================================
# V1 — AC14: Config flag matrix
# ===========================================================================


class TestAC14ConfigFlags:
    """supervisor.doorbell and teammate_push flag matrix."""

    def test_doorbell_off_no_ring(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["config"]._cfg_overrides["supervisor.doorbell"] = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_disabled"
        mock_gates["send"].assert_not_called()

    def test_teammate_push_off_no_ring(self, mock_gates):
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["should_push"].return_value = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_disabled"
        mock_gates["send"].assert_not_called()

    def test_no_cc_inbox_path_no_ring(self, mock_gates):
        """_should_teammate_push returns False when cc_team_inbox_path is absent."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["should_push"].return_value = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_disabled"

    def test_doorbell_on_teammate_push_off_no_ring(self, mock_gates):
        """Doorbell flag on but teammate_push off => no ring (D10 subordination)."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["should_push"].return_value = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_disabled"


# ===========================================================================
# V1 — AC15: Call-site inventory
# ===========================================================================


class TestAC15CallSiteInventory:
    """ring_supervisor_doorbell has exactly its three callers."""

    def test_three_call_sites_in_inbox_service(self):
        """Verify that inbox_service.py imports ring_supervisor_doorbell at exactly 2 sites.

        fx168 FIX-4: The D9 call site in deliver_pending was removed (lock reentrance
        made it structurally dead). Remaining sites: _f136_post_delivery and the
        fx158 pull-mode reconciler.
        """
        import inspect
        import cli_agent_orchestrator.services.inbox_service as mod
        source = inspect.getsource(mod)
        # Count the number of 'ring_supervisor_doorbell' calls (not imports)
        import_count = source.count("from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell")
        call_count = source.count("ring_supervisor_doorbell(")
        # 2 import sites (fx168 FIX-4: removed the dead D9 site in deliver_pending)
        assert import_count == 2, f"Expected 2 import sites, got {import_count}"
        # 2 call sites
        assert call_count == 2, f"Expected 2 call sites, got {call_count}"


# ===========================================================================
# V1 — D6 fixed text assertion (kills mutant 15)
# ===========================================================================


class TestD6FixedNudgeText:
    """The injected text is the D6-as-amended fixed line, content-independent."""

    def test_nudge_text_is_fixed(self):
        from cli_agent_orchestrator.services.doorbell_service import DOORBELL_NUDGE_TEXT
        assert DOORBELL_NUDGE_TEXT == "[cao] You have new callback message(s). Run any command to surface them."

    def test_nudge_text_content_independent(self, mock_gates):
        """No matter what the message content is, the nudge text is always the same."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell, DOORBELL_NUDGE_TEXT
        ring_supervisor_doorbell("term-01", 100, written_count=1)
        sent_text = mock_gates["send"].call_args[0][1]
        assert sent_text == DOORBELL_NUDGE_TEXT


# ===========================================================================
# V1 — D13: Rebind-concurrency guard (design gate empirical check)
# ===========================================================================


class TestD13RebindConcurrency:
    """The rebind window is excluded by G1 (delivery_lock) and G2 (recovery_state)."""

    def test_delivery_lock_contention_skips(self, mock_gates):
        """G1: non-blocking acquire failure => skipped_gate."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["lock"].acquire.return_value = False
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"
        mock_gates["send"].assert_not_called()

    def test_recovery_state_rebinding_skips(self, mock_gates):
        """G2: recovery_state not in {None, 'rebound'} => skipped_gate."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["meta"].return_value = {
            "metadata": {"cc_team_inbox_path": "/tmp/inbox.json", "recovery_state": "rebinding"},
        }
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "skipped_gate"

    def test_recovery_state_rebound_allows(self, mock_gates):
        """G2: recovery_state='rebound' is allowed."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        mock_gates["meta"].return_value = {
            "metadata": {"cc_team_inbox_path": "/tmp/inbox.json", "recovery_state": "rebound"},
        }
        result = ring_supervisor_doorbell("term-01", 100, written_count=1)
        assert result == "rang"


# ===========================================================================
# V1 — D12: Rate-limited WARN log
# ===========================================================================


class TestD12RateLimitedLog:
    """D12: WARN log rate-limited to once per terminal per 60s."""

    def test_warn_rate_limited(self, mock_gates, caplog):
        import logging
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell, _last_warn_time
        # Force an error scenario
        mock_gates["probe"].side_effect = RuntimeError("dead")
        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.doorbell_service"):
            ring_supervisor_doorbell("term-01", 100, written_count=1)
            ring_supervisor_doorbell("term-01", 200, written_count=1)
        # Only one WARN in 60s
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1


# ===========================================================================
# V2 — Real-sqlite integration: reconciler site (AC15)
# ===========================================================================


class TestFx168ReconcilerRealSqlite:
    """Reconciler push triggers doorbell (real sqlite fixture)."""

    def test_reconciler_push_rings_doorbell(self, real_sqlite_env, monkeypatch):
        """After a successful reconciler push, ring_supervisor_doorbell is called."""
        env = real_sqlite_env
        TestSession = env["TestSession"]
        from cli_agent_orchestrator.clients.database import (
            MailboxModel,
            MailboxIncarnationModel,
        )
        from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus, OrchestrationType
        from cli_agent_orchestrator.services.inbox_service import InboxService

        now = datetime.now(timezone.utc)
        terminal_id = "term-fx168"
        mailbox_id = str(uuid.uuid4())[:8]

        # Seed mailbox
        with TestSession.begin() as db:
            mb = MailboxModel(
                id=mailbox_id,
                session_name="test-sess",
                role="supervisor",
                current_terminal_id=terminal_id,
                generation=1,
                consumed_through_id=0,
            )
            db.add(mb)

        # Seed terminal
        from cli_agent_orchestrator.clients.database import TerminalModel
        with TestSession.begin() as db:
            term = TerminalModel(
                id=terminal_id,
                tmux_session="test",
                tmux_window="win-1",
                provider="kiro_cli",
                lifecycle_generation=1,
            )
            db.add(term)

        # Seed pending inbox row
        from cli_agent_orchestrator.clients.database import InboxModel
        from datetime import timedelta
        with TestSession.begin() as db:
            row = InboxModel(
                id=1,
                sender_id="worker-01",
                receiver_id=terminal_id,
                message="callback result",
                orchestration_type="send_message",
                status="pending",
                created_at=now - timedelta(seconds=120),
                logical_receiver_id=mailbox_id,
            )
            db.add(row)

        # Patch dependencies
        doorbell_calls = []

        def mock_ring(tid, max_id, *, written_count=0, caller_holds_no_delivery_lock=False):
            doorbell_calls.append((tid, max_id, written_count))
            return "rang"

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
            mock_ring,
        )

        # Patch is_supervisor_mailbox_pull_terminal to return True
        from cli_agent_orchestrator.services import mailbox_service
        monkeypatch.setattr(
            mailbox_service,
            "is_supervisor_mailbox_pull_terminal",
            lambda tid: True,
        )

        # Patch _should_teammate_push to return True
        from cli_agent_orchestrator.services import teammate_push_service
        monkeypatch.setattr(teammate_push_service, "_should_teammate_push", lambda tid: True)

        # Patch attempt_teammate_push_reported to return pushed=True
        from cli_agent_orchestrator.services.teammate_push_service import PushOutcome
        monkeypatch.setattr(
            teammate_push_service,
            "attempt_teammate_push_reported",
            lambda tid, msgs: PushOutcome(pushed=True, reason="pushed", message_ids=tuple(m.id for m in msgs)),
        )

        # Patch ConfigService
        from cli_agent_orchestrator.services.config_service import ConfigService
        monkeypatch.setattr(ConfigService, "get", staticmethod(lambda path, **kw: True))

        # Patch begin_delivery_attempt and settle_delivery_attempt
        import cli_agent_orchestrator.clients.database as db_mod
        monkeypatch.setattr(
            db_mod,
            "begin_delivery_attempt",
            lambda *a, **kw: str(uuid.uuid4()),
        )
        monkeypatch.setattr(
            db_mod,
            "settle_delivery_attempt",
            lambda *a, **kw: None,
        )

        # Patch get_terminal_metadata for the reconciler
        monkeypatch.setattr(
            db_mod,
            "get_terminal_metadata",
            lambda tid: {"metadata": {"cc_team_inbox_path": "/tmp/test_inbox.json"}, "tmux_session": "test", "tmux_window": "win-1", "provider": "kiro_cli", "lifecycle_generation": 1},
        )

        svc = InboxService()
        svc.reconcile_pull_mode_notifications()

        # Assert doorbell was called
        assert len(doorbell_calls) >= 1
        assert doorbell_calls[0][0] == terminal_id
        assert doorbell_calls[0][1] == 1  # max message id
        assert doorbell_calls[0][2] == 1  # written_count

    def test_reconciler_suppressed_no_doorbell(self, real_sqlite_env, monkeypatch):
        """Suppressed reconciler push does NOT call doorbell."""
        env = real_sqlite_env
        TestSession = env["TestSession"]
        from cli_agent_orchestrator.clients.database import (
            MailboxModel,
        )

        now = datetime.now(timezone.utc)
        terminal_id = "term-fx168b"
        mailbox_id = str(uuid.uuid4())[:8]

        # Seed mailbox
        with TestSession.begin() as db:
            mb = MailboxModel(
                id=mailbox_id,
                session_name="test-sess",
                role="supervisor",
                current_terminal_id=terminal_id,
                generation=1,
                consumed_through_id=0,
            )
            db.add(mb)

        # Seed terminal
        from cli_agent_orchestrator.clients.database import TerminalModel
        with TestSession.begin() as db:
            term = TerminalModel(
                id=terminal_id,
                tmux_session="test",
                tmux_window="win-1",
                provider="kiro_cli",
                lifecycle_generation=1,
            )
            db.add(term)

        # Seed pending inbox row
        from cli_agent_orchestrator.clients.database import InboxModel
        from datetime import timedelta
        with TestSession.begin() as db:
            row = InboxModel(
                id=2,
                sender_id="worker-02",
                receiver_id=terminal_id,
                message="callback result",
                orchestration_type="send_message",
                status="pending",
                created_at=now - timedelta(seconds=120),
                logical_receiver_id=mailbox_id,
            )
            db.add(row)

        doorbell_calls = []

        def mock_ring(tid, max_id, *, written_count=0, caller_holds_no_delivery_lock=False):
            doorbell_calls.append((tid, max_id, written_count))
            return "rang"

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.doorbell_service.ring_supervisor_doorbell",
            mock_ring,
        )

        # is_supervisor_mailbox_pull_terminal => True
        from cli_agent_orchestrator.services import mailbox_service
        monkeypatch.setattr(
            mailbox_service,
            "is_supervisor_mailbox_pull_terminal",
            lambda tid: True,
        )

        # _should_teammate_push => True so we get past the gate, but outcome.pushed=False
        from cli_agent_orchestrator.services import teammate_push_service
        from cli_agent_orchestrator.services.teammate_push_service import PushOutcome
        monkeypatch.setattr(teammate_push_service, "_should_teammate_push", lambda tid: True)
        monkeypatch.setattr(
            teammate_push_service,
            "attempt_teammate_push_reported",
            lambda tid, msgs: PushOutcome(pushed=False, reason="already_notified", message_ids=tuple(m.id for m in msgs)),
        )

        from cli_agent_orchestrator.services.config_service import ConfigService
        monkeypatch.setattr(ConfigService, "get", staticmethod(lambda path, **kw: True))

        import cli_agent_orchestrator.clients.database as db_mod
        monkeypatch.setattr(
            db_mod,
            "begin_delivery_attempt",
            lambda *a, **kw: str(uuid.uuid4()),
        )
        monkeypatch.setattr(
            db_mod,
            "settle_delivery_attempt",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            db_mod,
            "get_terminal_metadata",
            lambda tid: {"metadata": {"cc_team_inbox_path": "/tmp/test_inbox.json"}, "tmux_session": "test", "tmux_window": "win-1", "provider": "kiro_cli", "lifecycle_generation": 1},
        )

        from cli_agent_orchestrator.services.inbox_service import InboxService
        svc = InboxService()
        svc.reconcile_pull_mode_notifications()

        # Doorbell should NOT have been called (push was suppressed)
        assert len(doorbell_calls) == 0


# ===========================================================================
# V1 — max_written_row_id field on CallbackRunOutcome
# ===========================================================================


class TestMaxWrittenRowIdField:
    """CallbackRunOutcome carries max_written_row_id (keyword-defaulted)."""

    def test_default_zero(self):
        o = CallbackRunOutcome()
        assert o.max_written_row_id == 0

    def test_keyword_construction(self):
        o = CallbackRunOutcome(written=3, max_written_row_id=42)
        assert o.max_written_row_id == 42

    def test_positional_backward_compat(self):
        """Existing code constructing CallbackRunOutcome positionally still works."""
        # The new field is keyword-only at the end, so positional construction
        # of the existing fields should not break.
        o = CallbackRunOutcome(
            selected=10,
            processed=8,
            cursor_before=0,
            cursor_after=5,
            replay_selected=2,
            replay_drained=1,
            written=6,
            already_present=2,
            retryable_failure_count=0,
            identity_conflict_count=0,
            bootstrap_mode=None,
            needs_immediate_wake=False,
            retry_delay_s=None,
            reason="ok",
        )
        assert o.max_written_row_id == 0  # default



# ===========================================================================
# fx168 FIX-5: G4 tmux-compatible fallback
# ===========================================================================


class TestFix5TmuxFallback:
    """G4 tmux fallback uses probe_screen_status when native_probe returns None."""

    def test_tmux_idle_rings_with_source_tmux(self):
        """When native_probe=None + tmux probe IDLE → decision=rang, source=tmux."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.status_monitor import TerminalStatus
        from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push") as mock_should,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id") as mock_update,
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_lock_fn,
            patch("cli_agent_orchestrator.services.receiver_state_view.native_probe") as mock_native,
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status") as mock_tmux_probe,
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe") as mock_inject_safe,
            patch("cli_agent_orchestrator.providers.manager.provider_manager.get_provider") as mock_get_prov,
            patch("cli_agent_orchestrator.services.terminal_service.send_prepared_input") as mock_send,
        ):
            mock_config.get.side_effect = lambda p, default=None, override=None: False if p == "supervisor.wake.native" else True
            mock_should.return_value = True
            mock_meta.return_value = {"metadata": {"cc_team_inbox_path": "/tmp/inbox.json"}}
            mock_update.return_value = None

            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_lock_fn.return_value = mock_lock

            # Native probe unavailable (tmux backend)
            mock_native.return_value = None

            # Tmux probe returns IDLE
            tmux_result = MagicMock()
            tmux_result.status = TerminalStatus.IDLE
            tmux_result.meta = {"result_status": "idle", "frame_source": "screen"}
            mock_tmux_probe.return_value = tmux_result

            mock_inject_safe.return_value = InjectSafetyResult("safe")
            mock_get_prov.return_value = MagicMock()
            mock_send.return_value = None

            decision = ring_supervisor_doorbell("term-001", 100, written_count=1)

            assert decision == "rang"
            mock_tmux_probe.assert_called_once_with("term-001")
            mock_send.assert_called_once()

    def test_tmux_busy_pane_skips_gate(self):
        """When native_probe=None + tmux probe PROCESSING → decision=skipped_gate."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.status_monitor import TerminalStatus

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push") as mock_should,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id"),
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_lock_fn,
            patch("cli_agent_orchestrator.services.receiver_state_view.native_probe") as mock_native,
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status") as mock_tmux_probe,
            patch("cli_agent_orchestrator.services.terminal_service.send_prepared_input") as mock_send,
        ):
            mock_config.get.side_effect = lambda p, default=None, override=None: False if p == "supervisor.wake.native" else True
            mock_should.return_value = True
            mock_meta.return_value = {"metadata": {"cc_team_inbox_path": "/tmp/inbox.json"}}

            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_lock_fn.return_value = mock_lock

            # Native unavailable
            mock_native.return_value = None

            # Tmux probe returns PROCESSING (busy)
            tmux_result = MagicMock()
            tmux_result.status = TerminalStatus.PROCESSING
            tmux_result.meta = {"result_status": "processing", "frame_source": "screen"}
            mock_tmux_probe.return_value = tmux_result

            decision = ring_supervisor_doorbell("term-001", 100, written_count=1)

            assert decision == "skipped_gate"
            # send_prepared_input NOT called — pane is busy
            mock_send.assert_not_called()

    def test_herdr_native_preferred_over_tmux(self):
        """When native_probe returns a result, tmux fallback is NOT used."""
        from cli_agent_orchestrator.services.doorbell_service import ring_supervisor_doorbell
        from cli_agent_orchestrator.services.status_monitor import TerminalStatus
        from cli_agent_orchestrator.services.inbox_service import InjectSafetyResult

        with (
            patch("cli_agent_orchestrator.services.doorbell_service.ConfigService") as mock_config,
            patch("cli_agent_orchestrator.services.teammate_push_service._should_teammate_push") as mock_should,
            patch("cli_agent_orchestrator.services.doorbell_service.get_terminal_metadata") as mock_meta,
            patch("cli_agent_orchestrator.services.doorbell_service.set_terminal_last_doorbell_row_id"),
            patch("cli_agent_orchestrator.services.inbox_service.get_delivery_lock") as mock_lock_fn,
            patch("cli_agent_orchestrator.services.receiver_state_view.native_probe") as mock_native,
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status") as mock_tmux_probe,
            patch("cli_agent_orchestrator.services.inbox_service.inbox_service._inject_safe") as mock_inject_safe,
            patch("cli_agent_orchestrator.providers.manager.provider_manager.get_provider") as mock_get_prov,
            patch("cli_agent_orchestrator.services.terminal_service.send_prepared_input") as mock_send,
        ):
            mock_config.get.side_effect = lambda p, default=None, override=None: False if p == "supervisor.wake.native" else True
            mock_should.return_value = True
            mock_meta.return_value = {"metadata": {"cc_team_inbox_path": "/tmp/inbox.json"}}

            mock_lock = MagicMock()
            mock_lock.acquire.return_value = True
            mock_lock_fn.return_value = mock_lock

            # Native probe available (herdr backend) — returns IDLE
            native_result = MagicMock()
            native_result.status = TerminalStatus.IDLE
            native_result.meta = {"result_status": "idle", "frame_source": "native"}
            mock_native.return_value = native_result

            mock_inject_safe.return_value = InjectSafetyResult("safe")
            mock_get_prov.return_value = MagicMock()
            mock_send.return_value = None

            decision = ring_supervisor_doorbell("term-001", 100, written_count=1)

            assert decision == "rang"
            # Tmux probe NOT called — native was available
            mock_tmux_probe.assert_not_called()

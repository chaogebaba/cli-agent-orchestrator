"""FX181 quiescence watchdog tests — V1 (unit) and V2 (delivery-seam)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.stalled_callback_watchdog import (
    StalledCallbackWatchdog,
    _RetiredMember,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_watchdog(grace=3):
    return StalledCallbackWatchdog(grace_seconds=grace)


def _meta(terminal_id="worker1", caller_id="sup1"):
    return {
        "id": terminal_id,
        "caller_id": caller_id,
        "provider": "grok_cli",
        "tmux_session": "cao-test",
        "tmux_window": terminal_id,
    }


def _setup_owed_idle(svc, terminal_id="worker1", caller_id="sup1", profile="grok_dev", idle_at=10.0):
    """Record an assign and mark the worker idle with a screen fingerprint."""
    svc.record_inbound_task(terminal_id, caller_id, profile)
    svc.record_status(terminal_id, TerminalStatus.IDLE, now=idle_at)
    with svc._lock:
        svc._episodes[terminal_id].last_screen_fp = "sampled"


def _config_on(path, default=None, override=None):
    """ConfigService.get mock that enables quiescence with grace=3."""
    mapping = {
        "supervisor.watchdog.quiescence": True,
        "supervisor.watchdog.quiescence_grace_s": 3.0,
    }
    return mapping.get(path, default)


def _config_off(path, default=None, override=None):
    """ConfigService.get mock with quiescence disabled."""
    mapping = {
        "supervisor.watchdog.quiescence": False,
        "supervisor.watchdog.quiescence_grace_s": 3.0,
    }
    return mapping.get(path, default)


# ---------------------------------------------------------------------------
# V1 — Set membership (AC1, AC2, AC3)
# ---------------------------------------------------------------------------

class TestSetMembership:
    """AC1-AC3: owed set creation, settlement, retirement."""

    def test_ac1_assign_creates_owed_entry(self):
        """A non-park_warm assign creates an owed entry."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        with svc._lock:
            assert "w1" in svc._episodes
            assert svc._episodes["w1"].caller_id == "sup1"

    def test_ac1_watchdog_sender_excluded(self):
        """A watchdog: sender creates no episode."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "watchdog:stall:x", "developer")
        with svc._lock:
            assert "w1" not in svc._episodes

    def test_ac2_callback_removes_member(self):
        """A delivered callback removes the member (callback_seen=True)."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value={"caller_id": "sup1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "sup1")
        with svc._lock:
            assert svc._episodes["w1"].callback_seen is True

    def test_ac2_clear_terminal_removes_from_both_stores(self):
        """delete_terminal (clear_terminal) removes from _episodes AND _dead_owed."""
        svc = _make_watchdog()
        svc.record_inbound_task("w1", "sup1", "developer")
        # Simulate retirement into _dead_owed
        with svc._lock:
            svc._dead_owed["sup1"] = {"w1": _RetiredMember(
                terminal_id="w1", caller_id="sup1", generation=1,
                profile="developer", retired_at=100.0,
            )}
        svc.clear_terminal("w1")
        with svc._lock:
            assert "w1" not in svc._episodes
            assert "w1" not in svc._dead_owed.get("sup1", {})

    def test_ac3_metadata_none_retires_to_dead_owed(self):
        """Metadata-None retirement: episode moves to _dead_owed with identity intact."""
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"

        # metadata returns None -> retirement
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=None,
        ):
            notices = svc.collect_due_notifications(now=14.0)

        # No per-worker notice (can't describe dead terminal)
        assert notices == []
        # But _dead_owed has it
        with svc._lock:
            assert "w1" not in svc._episodes
            assert "w1" in svc._dead_owed.get("sup1", {})
            retired = svc._dead_owed["sup1"]["w1"]
            assert retired.generation == 1
            assert retired.profile == "developer"
            assert retired.last_status == "dead"

    def test_ac3_mutant_bare_pop_killed(self):
        """A mutant that restores the bare pop loses the debt — this test kills it."""
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=None,
        ):
            svc.collect_due_notifications(now=14.0)

        # The debt MUST survive somewhere
        with svc._lock:
            has_debt = (
                "w1" in svc._episodes
                or "w1" in svc._dead_owed.get("sup1", {})
            )
        assert has_debt, "Mutant: bare pop loses the debt"


# ---------------------------------------------------------------------------
# V1 — RING predicate (AC4-AC7)
# ---------------------------------------------------------------------------

class TestRingPredicate:
    """AC4-AC7: ring fires / no-ring conditions / grace boundary."""

    def _tick_with_mocks(self, svc, now, *, status_return=TerminalStatus.IDLE,
                         sup_meta=None, callback_status=None):
        """Run tick_quiescence with standard mocks."""
        if sup_meta is None:
            sup_meta = _meta("sup1")

        def meta_side_effect(tid):
            if tid == "sup1":
                return sup_meta
            return None  # workers are dead by default in this helper

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=status_return,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=callback_status,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ) as mock_deliver,
        ):
            svc.tick_quiescence(now=now)
            return mock_persist, mock_deliver

    def test_ac4_ring_fires_mixed_live_dead(self):
        """Ring fires: owed set of 2 (one idle live, one dead) both >= grace."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "grok_dev", idle_at=10.0)
        # Retire w2 as dead
        with svc._lock:
            svc._dead_owed.setdefault("sup1", {})["w2"] = _RetiredMember(
                terminal_id="w2", caller_id="sup1", generation=2,
                profile="codex_dev", retired_at=10.0,
            )

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            if tid == "w1":
                return _meta("w1", "sup1")
            return None

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            mock_persist.assert_called_once()
            msg = mock_persist.call_args[0][2]
            # D6: message lists both lanes
            assert "grok_dev-w1" in msg
            assert "codex_dev-w2" in msg
            assert "last=idle" in msg
            assert "last=dead" in msg
            assert "2 lane(s)" in msg
            # D6: no worker-authored content (fixed text)
            assert "[quiescence watchdog]" in msg

    def test_ac5_no_ring_empty_set(self):
        """No ring when owed set is empty."""
        svc = _make_watchdog(grace=3)
        mock_persist, _ = self._tick_with_mocks(svc, now=100.0)
        mock_persist.assert_not_called()

    def test_ac5_no_ring_member_processing(self):
        """No ring when one member is PROCESSING."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, status_return=TerminalStatus.PROCESSING
        )
        mock_persist.assert_not_called()

    def test_ac5_no_ring_member_waiting_user_answer(self):
        """No ring when member is WAITING_USER_ANSWER."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, status_return=TerminalStatus.WAITING_USER_ANSWER
        )
        mock_persist.assert_not_called()

    def test_ac5_no_ring_member_unknown(self):
        """No ring when member status is UNKNOWN (indeterminate)."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, status_return=TerminalStatus.UNKNOWN
        )
        mock_persist.assert_not_called()

    def test_ac5_no_ring_snapshot_none(self):
        """No ring when snapshot returns None (indeterminate)."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(svc, now=14.0, status_return=None)
        mock_persist.assert_not_called()

    def test_ac5_no_ring_inside_grace(self):
        """No ring when member quiet < grace."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(svc, now=12.9)
        mock_persist.assert_not_called()

    def test_ac5_no_ring_flag_off(self):
        """No ring when supervisor.watchdog.quiescence is false."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=_meta("sup1"),
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_off,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            mock_persist.assert_not_called()

    def test_ac5_no_ring_supervisor_unresolvable(self):
        """No ring when supervisor terminal is dead (D9)."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        # Supervisor meta is None (dead supervisor)
        def meta_side_effect(tid):
            if tid == "sup1":
                return None  # dead supervisor
            return _meta(tid, "sup1")

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            mock_persist.assert_not_called()

    def test_ac6_queued_callback_suppresses_ring(self):
        """A PENDING callback from any member suppresses the ring entirely."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, callback_status=MessageStatus.PENDING
        )
        mock_persist.assert_not_called()

    def test_ac6_held_callback_suppresses_ring(self):
        """A HELD callback suppresses."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, callback_status=MessageStatus.HELD
        )
        mock_persist.assert_not_called()

    def test_ac6_delivered_callback_settles(self):
        """A DELIVERED callback settles the member — set shrinks, re-evaluate next tick."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        mock_persist, _ = self._tick_with_mocks(
            svc, now=14.0, callback_status=MessageStatus.DELIVERED
        )
        mock_persist.assert_not_called()
        # callback_seen should be set
        with svc._lock:
            assert svc._episodes["w1"].callback_seen is True

    def test_ac7_grace_boundary_exactly_grace_rings(self):
        """Quiet exactly == grace triggers the ring."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        # now=13.0 -> quiet=3.0 == grace -> should ring
        mock_persist, _ = self._tick_with_mocks(svc, now=13.0)
        mock_persist.assert_called_once()

    def test_ac7_fingerprint_reset_restarts_clock(self):
        """Screen fingerprint change resets idle_since — the quiet clock restarts."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)
        # Simulate fingerprint change at t=12 (resets idle_since to 12)
        with svc._lock:
            svc._episodes["w1"].idle_since = 12.0
            svc._episodes["w1"].last_screen_fp = "new_fp"
        # At t=14, quiet = 2s < 3 grace -> no ring
        mock_persist, _ = self._tick_with_mocks(svc, now=14.0)
        mock_persist.assert_not_called()
        # At t=15, quiet = 3s == grace -> ring
        mock_persist, _ = self._tick_with_mocks(svc, now=15.0)
        mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# V1 — At-most-once and re-arm (AC8, AC9)
# ---------------------------------------------------------------------------

class TestDedupAndRearm:
    """AC8-AC9: at-most-once per quiescence episode, re-arm rules."""

    def _tick_ringing(self, svc, now):
        """Tick that should ring — returns (mock_persist, mock_deliver)."""
        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ) as mock_deliver,
        ):
            svc.tick_quiescence(now=now)
            return mock_persist, mock_deliver

    def test_ac8_100_ticks_produce_one_ring(self):
        """With a held quiescence state, 100 consecutive ticks produce exactly one row."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        total_calls = 0
        for i in range(100):
            mock_persist, _ = self._tick_ringing(svc, now=14.0 + i)
            total_calls += mock_persist.call_count

        assert total_calls == 1

    def test_ac8_rearm_on_processing_then_restall(self):
        """Member returning to PROCESSING re-arms; re-stall >= grace rings again."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        # First ring
        mock_persist, _ = self._tick_ringing(svc, now=14.0)
        assert mock_persist.call_count == 1

        # Worker goes back to PROCESSING (re-arm)
        svc.record_status("w1", TerminalStatus.PROCESSING, now=15.0)
        # Worker goes idle again
        svc.record_status("w1", TerminalStatus.IDLE, now=16.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp2"

        # Second ring after grace
        mock_persist, _ = self._tick_ringing(svc, now=19.0)
        assert mock_persist.call_count == 1

    def test_ac9_composition_change_rearms(self):
        """After ring #1, a new assign adds a member; that worker's death + grace rings again."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        # First ring
        mock_persist, _ = self._tick_ringing(svc, now=14.0)
        assert mock_persist.call_count == 1

        # New assign to same caller
        svc.record_inbound_task("w2", "sup1", "grok_dev")
        svc.record_status("w2", TerminalStatus.IDLE, now=15.0)
        with svc._lock:
            svc._episodes["w2"].last_screen_fp = "fp"

        # Second ring
        mock_persist, _ = self._tick_ringing(svc, now=19.0)
        assert mock_persist.call_count == 1
        msg = mock_persist.call_args[0][2]
        assert "w2" in msg


# ---------------------------------------------------------------------------
# V1/V2 — Delivery and isolation (AC10, AC11)
# ---------------------------------------------------------------------------

class TestDeliveryAndIsolation:
    """AC10-AC11: per-caller isolation, sender prefix, request_delivery."""

    def test_ac10_two_callers_independent_messages(self):
        """Two callers get independent messages naming only their own lanes."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        _setup_owed_idle(svc, "w2", "sup2", "dev_b", idle_at=10.0)

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            if tid == "sup2":
                return _meta("sup2")
            if tid == "w1":
                return _meta("w1", "sup1")
            if tid == "w2":
                return _meta("w2", "sup2")
            return None

        persist_calls = []
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
                side_effect=lambda s, r, m, **kw: persist_calls.append((s, r, m)),
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)

        assert len(persist_calls) == 2
        # Each message names only its own lane
        sup1_msgs = [m for s, r, m in persist_calls if r == "sup1"]
        sup2_msgs = [m for s, r, m in persist_calls if r == "sup2"]
        assert len(sup1_msgs) == 1
        assert len(sup2_msgs) == 1
        assert "dev_a-w1" in sup1_msgs[0]
        assert "dev_b-w2" not in sup1_msgs[0]
        assert "dev_b-w2" in sup2_msgs[0]
        assert "dev_a-w1" not in sup2_msgs[0]

    def test_ac10_exception_in_one_caller_doesnt_suppress_other(self):
        """An exception in caller A's evaluation does not suppress caller B's ring."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup_bad", "dev_a", idle_at=10.0)
        _setup_owed_idle(svc, "w2", "sup_good", "dev_b", idle_at=10.0)

        call_count = [0]

        def meta_side_effect(tid):
            if tid == "sup_bad":
                # Force exception in sup_bad's evaluation
                raise RuntimeError("injected fault for sup_bad")
            if tid == "sup_good":
                return _meta("sup_good")
            return _meta(tid, "sup_good")

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            # Should not raise
            svc.tick_quiescence(now=14.0)
            # sup_good's ring should still fire
            assert mock_persist.call_count == 1
            assert "sup_good" in mock_persist.call_args[0][0]

    def test_ac10_no_exception_escapes_tick_quiescence(self):
        """No exception escapes tick_quiescence (D8 fail-silence)."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        with (
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=RuntimeError("config boom"),
            ),
        ):
            # Must not raise
            svc.tick_quiescence(now=14.0)

    def test_ac11_sender_prefix_and_no_episode(self):
        """Persisted row has sender watchdog:quiescence:<caller> — creates no watchdog episode."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ) as mock_deliver,
        ):
            svc.tick_quiescence(now=14.0)
            # Sender prefix
            sender = mock_persist.call_args[0][0]
            assert sender == "watchdog:quiescence:sup1"
            # Delivery requested (not synchronous)
            mock_deliver.assert_called_once_with("sup1")

        # The watchdog: sender guard prevents episode creation
        svc.record_inbound_task("sup1", "watchdog:quiescence:sup1", "dev")
        with svc._lock:
            assert "sup1" not in svc._episodes or svc._episodes.get("sup1") is None or \
                svc._episodes.get("sup1", object()).caller_id != "watchdog:quiescence:sup1"

    def test_ac5_flag_flipped_mid_tick(self):
        """Mutant 13: flag evaluated per-tick, not once at startup."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")

        # First tick: flag off -> no ring
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_off,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            mock_persist.assert_not_called()

        # Second tick: flag on -> ring fires
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# V2 — Delivery-seam: persist failure does not set dedup key (D5 ordering)
# ---------------------------------------------------------------------------

class TestDeliverySeam:
    """V2: create_routed_inbox_message and request_delivery stubbed with recorders."""

    def test_persist_failure_does_not_set_dedup_key(self):
        """D5 ordering: if persist fails, dedup key stays unset, next tick retries."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")

        # First tick: persist raises -> no dedup key
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
                side_effect=RuntimeError("DB down"),
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)

        # Key not set
        with svc._lock:
            assert "sup1" not in svc._quiescence_last_fired

        # Second tick: persist succeeds -> key set, ring delivered
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ) as mock_deliver,
        ):
            svc.tick_quiescence(now=15.0)
            mock_persist.assert_called_once()
            mock_deliver.assert_called_once_with("sup1")

        # Key now set
        with svc._lock:
            assert "sup1" in svc._quiescence_last_fired

    def test_dedup_key_uses_generation(self):
        """Mutant 7: dedup on terminal_ids only (ignore generation) — killed."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")

        # First ring
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.IDLE,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ) as mock_persist,
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=14.0)
            assert mock_persist.call_count == 1

        # Key includes generation
        with svc._lock:
            key = svc._quiescence_last_fired["sup1"]
            # Key is sorted tuple of (terminal_id, generation)
            assert any(gen > 0 for _, gen in key)


# ---------------------------------------------------------------------------
# V1 — Existing per-worker notice tests still pass (AC3 regression)
# ---------------------------------------------------------------------------

class TestExistingBehaviorUnchanged:
    """AC3: the per-worker notice path and existing semantics remain."""

    def test_per_worker_stall_notice_unchanged(self):
        """The existing per-worker notice still fires for a live stalled worker."""
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=_meta("w1", "sup1"),
        ):
            notices = svc.collect_due_notifications(now=13.0)

        assert len(notices) == 1
        assert "idle 3s without callback" in notices[0].message

    def test_callback_seen_prevents_stall_notice(self):
        """callback_seen still prevents the per-worker notice."""
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value={"caller_id": "sup1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "sup1")

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=_meta("w1", "sup1"),
        ):
            notices = svc.collect_due_notifications(now=13.0)
        assert notices == []

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


def _setup_owed_error(svc, terminal_id="worker1", caller_id="sup1", profile="grok_dev", error_at=10.0):
    """Record an assign and drive the worker into ERROR (B1 / D2 row 1)."""
    svc.record_inbound_task(terminal_id, caller_id, profile)
    svc.record_status(terminal_id, TerminalStatus.ERROR, now=error_at)


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
        """A delivered callback removes the member (callback_seen=True) — and no ring follows.

        S3 mutant kill: dropping `if episode.callback_seen: continue` from the owed-set
        build would leave the settled member in the set and ring on a fully settled
        supervisor. The second half of this test is what notices.
        """
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value={"caller_id": "sup1", "caller_mailbox_id": None},
        ):
            svc.record_callback_if_to_caller("w1", "sup1")
        with svc._lock:
            assert svc._episodes["w1"].callback_seen is True

        # Full settlement: the owed set is empty, so no tick may ever ring.
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
            svc.tick_quiescence(now=100.0)
        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()

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
                         sup_meta=None, callback_status=None, workers_dead=False):
        """Run tick_quiescence with standard mocks."""
        if sup_meta is None:
            sup_meta = _meta("sup1")

        def meta_side_effect(tid):
            if tid == "sup1":
                return sup_meta
            if workers_dead:
                return None
            return _meta(tid, "sup1")  # workers are alive by default

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
        """Screen fingerprint change resets the quiet clock — driven through the real path.

        Mutant 6 (compare quiet-age against a clock the fingerprint reset does not
        touch) is killed here: the reset is produced by refresh_screen_fingerprints,
        not by poking the episode fields.
        """
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev", idle_at=10.0)

        backend = MagicMock()
        backend.get_history.return_value = "pane changed since last sample"
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=_meta("w1", "sup1"),
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=None,
            ),
        ):
            # Fingerprint differs from the "sampled" placeholder -> clock restarts at 12.0
            svc.refresh_screen_fingerprints(now=12.0)

        with svc._lock:
            assert svc._episodes["w1"].idle_since == 12.0
            assert svc._episodes["w1"].quiet_since == 12.0

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

    def test_error_status_does_not_arm_per_worker_notice(self):
        """B1 regression: ERROR feeds the quiescence clock only — notify_due is untouched.

        idle_since stays None on ERROR (so no per-worker stall notice), while the
        quiescence-scoped quiet_since runs.
        """
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "developer", error_at=10.0)
        with svc._lock:
            assert svc._episodes["w1"].idle_since is None
            assert svc._episodes["w1"].quiet_since == 10.0

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=_meta("w1", "sup1"),
        ):
            notices = svc.collect_due_notifications(now=100.0)
        assert notices == []


# ---------------------------------------------------------------------------
# B1 — ERROR is a TERMINAL state whose quiet clock runs (D2 row 1)
# ---------------------------------------------------------------------------

class TestErrorMemberQuietClock:
    """B1: a live ERROR member must contribute a running quiet clock, not veto the caller."""

    def _tick(self, svc, now, *, status_return=TerminalStatus.ERROR, callback_status=None):
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

    def test_single_error_member_rings_after_grace(self):
        """One live ERROR member, quiet >= G: the caller rings (pre-fix: never rang)."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)

        # Inside grace: silent
        mock_persist, _ = self._tick(svc, now=12.0)
        mock_persist.assert_not_called()

        # At grace: rings, labelled last=error
        mock_persist, _ = self._tick(svc, now=13.0)
        mock_persist.assert_called_once()
        msg = mock_persist.call_args[0][2]
        assert "grok_dev-w1 last=error quiet=3s gen=1" in msg
        assert "1 lane(s)" in msg

    def test_error_member_does_not_veto_whole_caller(self):
        """Mixed ERROR + dead members ring together — one ERROR must not zero the caller."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)
        with svc._lock:
            svc._dead_owed.setdefault("sup1", {})["w2"] = _RetiredMember(
                terminal_id="w2", caller_id="sup1", generation=2,
                profile="codex_dev", retired_at=10.0,
            )

        mock_persist, mock_deliver = self._tick(svc, now=14.0)
        mock_persist.assert_called_once()
        msg = mock_persist.call_args[0][2]
        assert "2 lane(s)" in msg
        assert "grok_dev-w1 last=error" in msg
        assert "codex_dev-w2 last=dead" in msg
        mock_deliver.assert_called_once_with("sup1")

    def test_error_then_processing_rearms(self):
        """ERROR -> PROCESSING clears the quiet clock and the dedup key; re-erroring rings again."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)

        mock_persist, _ = self._tick(svc, now=14.0)
        assert mock_persist.call_count == 1

        # Same state held: no second ring
        mock_persist, _ = self._tick(svc, now=15.0)
        mock_persist.assert_not_called()

        # Worker resumes...
        svc.record_status("w1", TerminalStatus.PROCESSING, now=16.0)
        with svc._lock:
            assert svc._episodes["w1"].quiet_since is None
            assert "sup1" not in svc._quiescence_last_fired
        # ...and errors again
        svc.record_status("w1", TerminalStatus.ERROR, now=17.0)

        # Inside the new grace window: silent
        mock_persist, _ = self._tick(svc, now=19.0)
        mock_persist.assert_not_called()
        # Past it: second ring
        mock_persist, _ = self._tick(svc, now=20.0)
        assert mock_persist.call_count == 1


# ---------------------------------------------------------------------------
# S2 — clear_terminal's dedup clear is caller-scoped
# ---------------------------------------------------------------------------

class TestDedupClearIsCallerScoped:
    """S2: an unrelated delete must not re-arm an unchanged set (AC8)."""

    def _tick(self, svc, now):
        def meta_side_effect(tid):
            if tid.startswith("sup"):
                return _meta(tid)
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
            svc.tick_quiescence(now=now)
            return mock_persist

    def test_unrelated_delete_does_not_rearm(self):
        """sup1's set is unchanged by deleting sup2's worker — sup1 must stay silent."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        _setup_owed_idle(svc, "w2", "sup2", "dev_b", idle_at=10.0)

        # Both ring once
        mock_persist = self._tick(svc, now=14.0)
        assert mock_persist.call_count == 2

        # Unrelated delete: sup2's worker is reaped
        svc.clear_terminal("w2")
        with svc._lock:
            assert "sup1" in svc._quiescence_last_fired
            assert "sup2" not in svc._quiescence_last_fired

        # sup1's set never changed -> no second ring for anyone
        mock_persist = self._tick(svc, now=15.0)
        mock_persist.assert_not_called()

    def test_unknown_terminal_delete_touches_nothing(self):
        """A delete of a terminal no caller owed leaves every dedup key intact."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        mock_persist = self._tick(svc, now=14.0)
        assert mock_persist.call_count == 1

        svc.clear_terminal("stranger")
        with svc._lock:
            assert "sup1" in svc._quiescence_last_fired

        mock_persist = self._tick(svc, now=15.0)
        mock_persist.assert_not_called()

    def test_owning_caller_delete_does_rearm(self):
        """The affected caller IS re-armed — scoping must not become inertness."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        _setup_owed_idle(svc, "w2", "sup1", "dev_b", idle_at=10.0)
        mock_persist = self._tick(svc, now=14.0)
        assert mock_persist.call_count == 1

        svc.clear_terminal("w2")
        with svc._lock:
            assert "sup1" not in svc._quiescence_last_fired

        mock_persist = self._tick(svc, now=15.0)
        assert mock_persist.call_count == 1
        assert "1 lane(s)" in mock_persist.call_args[0][2]

    def test_dead_owed_delete_rearms_its_caller_only(self):
        """The _dead_owed removal path also scopes its re-arm to the owning caller."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        with svc._lock:
            svc._quiescence_last_fired["sup1"] = (("w1", 1),)
            svc._quiescence_last_fired["sup2"] = (("wX", 1),)
            svc._dead_owed["sup2"] = {"wX": _RetiredMember(
                terminal_id="wX", caller_id="sup2", generation=1,
                profile="dev_b", retired_at=10.0,
            )}

        svc.clear_terminal("wX")
        with svc._lock:
            assert "sup2" not in svc._quiescence_last_fired
            assert "sup1" in svc._quiescence_last_fired


# ---------------------------------------------------------------------------
# S4 — D9 needs BOTH halves before dropping a caller's stores
# ---------------------------------------------------------------------------

class TestD9MailboxHalf:
    """S4: metadata-None alone is not death; a mailbox may route to a live successor."""

    def _tick(self, svc, now, *, successor):
        def meta_side_effect(tid):
            if tid == "sup1":
                return None  # the old supervisor pane is gone
            if tid == "sup1b":
                return _meta("sup1b")
            return _meta(tid, "sup1")

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "_supervisor_mailbox_live_terminal",
                return_value=successor,
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

    def _seed_dead_lane(self, svc):
        with svc._lock:
            svc._dead_owed.setdefault("sup1", {})["w2"] = _RetiredMember(
                terminal_id="w2", caller_id="sup1", generation=2,
                profile="codex_dev", retired_at=10.0,
            )

    def test_mailbox_successor_retains_stores_and_rings_successor(self):
        """metadata None + mailbox routing to a live successor: keep stores, ring successor."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "grok_dev", idle_at=10.0)
        self._seed_dead_lane(svc)

        mock_persist, mock_deliver = self._tick(svc, now=14.0, successor="sup1b")

        mock_persist.assert_called_once()
        sender, receiver, msg = mock_persist.call_args[0][:3]
        # Sender still names the debt owner; the row is addressed to the successor
        assert sender == "watchdog:quiescence:sup1"
        assert receiver == "sup1b"
        mock_deliver.assert_called_once_with("sup1b")
        assert "grok_dev-w1" in msg
        assert "codex_dev-w2" in msg

        # Stores retained — the dead-lane ledger survives the rebind
        with svc._lock:
            assert "w2" in svc._dead_owed.get("sup1", {})
            assert "w1" in svc._episodes

    def test_no_successor_drops_stores_and_stays_silent(self):
        """Both halves fail: no ring, and the caller's FX181 stores are dropped."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "grok_dev", idle_at=10.0)
        self._seed_dead_lane(svc)
        with svc._lock:
            svc._quiescence_last_fired["sup1"] = (("w1", 1),)

        mock_persist, mock_deliver = self._tick(svc, now=14.0, successor=None)

        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()
        with svc._lock:
            assert "sup1" not in svc._dead_owed
            assert "sup1" not in svc._quiescence_last_fired

    def test_helper_returns_none_without_mailbox(self):
        """The resolver itself is conservative: no supervisor mailbox -> no successor."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        assert _supervisor_mailbox_live_terminal("no-such-supervisor-terminal") is None


# ---------------------------------------------------------------------------
# N5/N7 — same-tick re-evaluation and predicate-time status
# ---------------------------------------------------------------------------

class TestSameTickReevalAndStatusCarry:
    """AC6/D4: the shrunken set is re-evaluated in the SAME tick; status is sampled once."""

    def _tick(self, svc, now, *, callback_side_effect=None, snapshot):
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
                snapshot,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                side_effect=callback_side_effect,
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

    def test_settled_member_shrinks_set_and_rings_same_tick(self):
        """N5: w1 settles via the DB probe; the remaining w2 rings in the SAME tick."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)
        _setup_owed_idle(svc, "w2", "sup1", "dev_b", idle_at=10.0)

        def cb(terminal_id, caller_id, since):
            return MessageStatus.DELIVERED if terminal_id == "w1" else None

        mock_persist, _ = self._tick(
            svc, now=14.0,
            callback_side_effect=cb,
            snapshot=MagicMock(return_value=TerminalStatus.IDLE),
        )

        mock_persist.assert_called_once()
        msg = mock_persist.call_args[0][2]
        assert "1 lane(s)" in msg
        assert "dev_b-w2" in msg
        assert "dev_a-w1" not in msg
        with svc._lock:
            assert svc._episodes["w1"].callback_seen is True

    def test_last_member_settling_leaves_empty_set_silent(self):
        """The same-tick re-eval of an emptied set still never rings."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "dev_a", idle_at=10.0)

        mock_persist, mock_deliver = self._tick(
            svc, now=14.0,
            callback_side_effect=lambda *a: MessageStatus.DELIVERED,
            snapshot=MagicMock(return_value=TerminalStatus.IDLE),
        )
        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()

    def test_status_sampled_once_per_member(self):
        """N7: the predicate-time status is carried into the message, not re-sampled."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "dev_a", error_at=10.0)
        snapshot = MagicMock(return_value=TerminalStatus.ERROR)

        mock_persist, _ = self._tick(
            svc, now=14.0,
            callback_side_effect=lambda *a: None,
            snapshot=snapshot,
        )
        mock_persist.assert_called_once()
        assert "last=error" in mock_persist.call_args[0][2]
        # One member, one tick, one status observation
        assert snapshot.call_count == 1

    def test_completed_status_labelled_completed(self):
        """N7 regression: a COMPLETED member is labelled `completed`, never defaulted to `idle`."""
        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "dev_a")
        svc.record_status("w1", TerminalStatus.COMPLETED, now=10.0)

        mock_persist, _ = self._tick(
            svc, now=14.0,
            callback_side_effect=lambda *a: None,
            snapshot=MagicMock(return_value=TerminalStatus.COMPLETED),
        )
        mock_persist.assert_called_once()
        assert "last=completed" in mock_persist.call_args[0][2]


# ---------------------------------------------------------------------------
# B1r2 — every anti-false-idle reset is inherited by the quiet clock (AC7)
# ---------------------------------------------------------------------------

class TestLivenessResetInheritance:
    """B1 round 2: idle_since is reset at three liveness proofs; quiet_since must
    follow at all three, or the aggregate rings off a clock the same tick just
    disproved."""

    def _tick(self, svc, now, *, status_return=TerminalStatus.IDLE):
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
                return_value=status_return,
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

    def test_fresh_frame_suppress_leaves_quiet_clock_running(self):
        """The suppress path proves the pane is running — the quiet clock restarts with it.

        Pre-fix: collect_due_notifications suppressed its own per-worker notice off a
        fresh RUNNING frame, then tick_quiescence rang in the same second off the stale
        quiet_since.
        """
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "grok_dev", idle_at=10.0)

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=_meta("w1", "sup1"),
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch.object(svc, "_fresh_frame_decides_running", return_value=(True, None)),
        ):
            assert svc.collect_due_notifications(now=14.0) == []

        with svc._lock:
            assert svc._episodes["w1"].idle_since == 14.0
            assert svc._episodes["w1"].quiet_since == 14.0

        # The very tick that suppressed the per-worker notice must not ring aggregate.
        mock_persist, mock_deliver = self._tick(svc, now=14.0)
        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()
        # Still inside the restarted grace window
        mock_persist, _ = self._tick(svc, now=16.0)
        mock_persist.assert_not_called()
        # The clock was restarted, not disabled: a genuinely quiet pane still rings.
        mock_persist, _ = self._tick(svc, now=17.0)
        mock_persist.assert_called_once()

    def test_auto_resume_leaves_quiet_clock_running(self):
        """An accepted auto-resume nudge restarts the quiet clock alongside idle_since."""
        import threading as _threading

        from cli_agent_orchestrator.clients.database import WatchdogInsertResult

        svc = _make_watchdog(grace=3)
        svc.record_inbound_task("w1", "sup1", "codex_dev")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "stable"

        metadata = dict(_meta("w1", "sup1"), provider="codex")
        delivery_lock = _threading.Lock()

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_callback_status_since",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.status_monitor.status_monitor.probe_screen_status",
                return_value=(TerminalStatus.IDLE, {"transient_api_error": True}),
            ),
            patch(
                "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
                return_value=delivery_lock,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog."
                "insert_watchdog_auto_resume_message",
                return_value=WatchdogInsertResult("inserted", 46),
            ),
            patch("cli_agent_orchestrator.services.inbox_service.request_delivery"),
        ):
            assert svc.collect_due_notifications(now=14.0) == []

        with svc._lock:
            episode = svc._episodes["w1"]
            assert episode.auto_resumed is True
            assert episode.idle_since == 14.0
            assert episode.quiet_since == 14.0

        # Pre-fix quiet_since stayed at 10.0 and this tick rang.
        mock_persist, mock_deliver = self._tick(svc, now=14.0)
        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()
        # The nudge buys exactly one grace window, not silence.
        mock_persist, _ = self._tick(svc, now=17.0)
        mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# S1 — ERROR members are fingerprint-tracked (anti-false-idle for the quiet clock)
# ---------------------------------------------------------------------------

class TestErrorMemberFingerprintTracking:
    """S1: an ERROR pane whose screen still churns is not quiet, and must not ring."""

    def _refresh(self, svc, now, frames):
        backend = MagicMock()
        backend.get_history.side_effect = frames
        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                return_value=_meta("w1", "sup1"),
            ),
            patch(
                "cli_agent_orchestrator.backends.registry.get_backend",
                return_value=backend,
            ),
            patch(
                "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
                return_value=None,
            ),
        ):
            svc.refresh_screen_fingerprints(now=now)
        return backend

    def _tick(self, svc, now):
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
                return_value=TerminalStatus.ERROR,
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

    def test_churning_error_pane_accrues_no_quiet_age(self):
        """A changing screen resets the ERROR member's quiet clock — no ring."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)

        self._refresh(svc, 11.0, ["frame 1"])
        self._refresh(svc, 13.0, ["frame 2"])

        with svc._lock:
            episode = svc._episodes["w1"]
            assert episode.quiet_since == 13.0
            # AC3: an ERROR pane must never become idle-notifiable
            assert episode.idle_since is None

        mock_persist, _ = self._tick(svc, now=14.0)
        mock_persist.assert_not_called()

    def test_static_error_pane_still_rings_at_grace(self):
        """Fingerprint tracking must not silence a genuinely frozen ERROR pane."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)

        self._refresh(svc, 11.0, ["frozen"])
        self._refresh(svc, 12.0, ["frozen"])

        with svc._lock:
            assert svc._episodes["w1"].quiet_since == 10.0

        mock_persist, _ = self._tick(svc, now=13.0)
        mock_persist.assert_called_once()
        assert "grok_dev-w1 last=error quiet=3s" in mock_persist.call_args[0][2]

    def test_idle_member_fingerprint_reset_still_moves_both_clocks(self):
        """The pre-existing IDLE path keeps resetting idle_since AND quiet_since."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup1", "grok_dev", idle_at=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = None

        self._refresh(svc, 11.0, ["frame 1"])
        self._refresh(svc, 13.0, ["frame 2"])

        with svc._lock:
            episode = svc._episodes["w1"]
            assert episode.idle_since == 13.0
            assert episode.quiet_since == 13.0


# ---------------------------------------------------------------------------
# S2 — _supervisor_mailbox_live_terminal against real SQLite
# ---------------------------------------------------------------------------

@pytest.fixture
def mailbox_db(tmp_path, monkeypatch):
    """Real SQLite for the D9 mailbox resolver (it queries the ORM directly)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database

    engine = create_engine(
        f"sqlite:///{tmp_path / 'fx181-mailbox.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _add_terminal(db, terminal_id):
    from cli_agent_orchestrator.clients.database import TerminalModel

    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="cao-orch",
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="chao_supervisor",
            init_state="ready",
        )
    )


def _add_mailbox(db, *, mailbox_id, role="supervisor", current_terminal_id=None, role_suffix=""):
    from cli_agent_orchestrator.clients.database import MailboxModel

    db.add(
        MailboxModel(
            id=mailbox_id,
            session_name=f"cao-orch{role_suffix}",
            role=role,
            current_terminal_id=current_terminal_id,
            generation=1,
        )
    )


class TestSupervisorMailboxResolver:
    """S2: the D9 second half is exercised against real rows, not a mock.

    A `return None` stub for `_supervisor_mailbox_live_terminal` survived the whole
    suite before these tests: every prior D9 test patched the resolver out.
    """

    def test_mailbox_id_caller_resolves_to_current_terminal(self, mailbox_db):
        """caller_id IS the supervisor mailbox id: route to its current terminal."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        with mailbox_db() as db:
            _add_terminal(db, "sup-live")
            _add_mailbox(db, mailbox_id="mbx-sup", current_terminal_id="sup-live")
            db.commit()

        assert _supervisor_mailbox_live_terminal("mbx-sup") == "sup-live"

    def test_terminal_id_caller_resolves_via_incarnation(self, mailbox_db):
        """caller_id is a retired incarnation: its mailbox names the live successor."""
        from cli_agent_orchestrator.clients.database import MailboxIncarnationModel
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        with mailbox_db() as db:
            _add_terminal(db, "sup-new")
            _add_mailbox(db, mailbox_id="mbx-sup", current_terminal_id="sup-new")
            db.add(
                MailboxIncarnationModel(
                    mailbox_id="mbx-sup", generation=1, terminal_id="sup-old"
                )
            )
            db.commit()

        assert _supervisor_mailbox_live_terminal("sup-old") == "sup-new"

    def test_dead_successor_yields_none(self, mailbox_db):
        """The mailbox points at a terminal whose row is gone: genuinely unreachable."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        with mailbox_db() as db:
            _add_mailbox(db, mailbox_id="mbx-sup", current_terminal_id="sup-gone")
            db.commit()

        assert _supervisor_mailbox_live_terminal("mbx-sup") is None

    def test_non_supervisor_role_yields_none(self, mailbox_db):
        """Only supervisor mailboxes carry the debt — a worker mailbox is not a successor."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        with mailbox_db() as db:
            _add_terminal(db, "wkr-live")
            _add_mailbox(
                db, mailbox_id="mbx-wkr", role="worker", current_terminal_id="wkr-live"
            )
            db.commit()

        assert _supervisor_mailbox_live_terminal("mbx-wkr") is None

    def test_self_reference_yields_none(self, mailbox_db):
        """A mailbox still pointing at the dead caller itself is no successor."""
        from cli_agent_orchestrator.services.stalled_callback_watchdog import (
            _supervisor_mailbox_live_terminal,
        )

        with mailbox_db() as db:
            _add_mailbox(db, mailbox_id="sup1", current_terminal_id="sup1")
            db.commit()

        assert _supervisor_mailbox_live_terminal("sup1") is None

    def test_live_resolver_keeps_stores_and_rings_successor(self, mailbox_db):
        """End to end through the real resolver: the ring follows the mailbox."""
        svc = _make_watchdog(grace=3)
        _setup_owed_idle(svc, "w1", "sup-old", "grok_dev", idle_at=10.0)

        with mailbox_db() as db:
            _add_terminal(db, "sup-new")
            _add_mailbox(db, mailbox_id="sup-old", current_terminal_id="sup-new")
            db.commit()

        def meta_side_effect(tid):
            if tid == "sup-old":
                return None
            if tid == "sup-new":
                return _meta("sup-new")
            return _meta(tid, "sup-old")

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

        mock_persist.assert_called_once()
        assert mock_persist.call_args[0][1] == "sup-new"
        mock_deliver.assert_called_once_with("sup-new")
        with svc._lock:
            assert "w1" in svc._episodes


# ---------------------------------------------------------------------------
# S3 — the pause span shifts the quiet clock
# ---------------------------------------------------------------------------

class TestPauseResumeQuietShift:
    """S3: a quarantine window is frozen time, not quiet time."""

    def _tick(self, svc, now, *, caller="sup1"):
        def meta_side_effect(tid):
            if tid == caller:
                return _meta(caller)
            return _meta(tid, caller)

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.receiver_state_view.snapshot_view",
                return_value=TerminalStatus.ERROR,
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

    def test_paused_member_is_excluded_from_owed_set(self):
        """N6: while paused, the member cannot contribute a ring however old its clock."""
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)
        svc.pause_terminal("w1")

        mock_persist, mock_deliver = self._tick(svc, now=500.0)
        mock_persist.assert_not_called()
        mock_deliver.assert_not_called()

    def test_pause_span_shifts_quiet_clock_past_the_old_deadline(self):
        """A 100s quarantine moves the ring deadline by 100s — it does not arrive early.

        Kills the mutant that drops `episode.quiet_since += elapsed` from
        resume_terminal: without the shift the resumed member rings immediately.
        """
        svc = _make_watchdog(grace=3)
        _setup_owed_error(svc, "w1", "sup1", "grok_dev", error_at=10.0)

        episode, started = svc.pause_terminal("w1")
        # Resume as if 100s of quarantine had elapsed.
        svc.resume_terminal("w1", (episode, started - 100.0))

        with svc._lock:
            shifted = svc._episodes["w1"].quiet_since
        assert shifted == pytest.approx(110.0, abs=1.0)

        # The pre-pause deadline (13.0) and everything short of the shifted one stay silent.
        mock_persist, _ = self._tick(svc, now=13.0)
        mock_persist.assert_not_called()
        mock_persist, _ = self._tick(svc, now=111.0)
        mock_persist.assert_not_called()

        # Past the shifted deadline it rings, and the reported quiet age is measured
        # from the shifted clock (~5s), never from the pre-pause one (~105s).
        mock_persist, _ = self._tick(svc, now=115.0)
        mock_persist.assert_called_once()
        message = mock_persist.call_args[0][2]
        assert "grok_dev-w1 last=error" in message
        quiet_reported = int(
            message.split("quiet=")[1].split("s")[0]
        )
        assert 3 <= quiet_reported <= 6



# ---------------------------------------------------------------------------
# F185 — dead-lane ring latency pinned to quiescence grace (not notifier grace)
# ---------------------------------------------------------------------------

class TestF185DeadLaneRingLatency:
    """F185: a dead worker enters the quiescence-quiet set on the quiescence grace
    path immediately — death is already a terminal signal; the notifier grace
    exists for live-but-quiet workers only.

    These tests prove that a worker whose terminal row disappears (death) rings
    within quiescence_grace + tick, NOT notifier_grace + quiescence_grace.
    """

    def _tick(self, svc, now, *, sup_meta=None):
        """Run tick_quiescence with mocks suitable for a dead-worker scenario."""
        if sup_meta is None:
            sup_meta = _meta("sup1")

        def meta_side_effect(tid):
            if tid == "sup1":
                return sup_meta
            return None  # all workers are dead

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

    def test_dead_lane_rings_within_quiescence_grace(self):
        """A dead worker rings after quiescence grace, NOT after the notifier grace.

        Setup: notifier grace = 120s, quiescence grace = 3s.
        Worker goes idle at t=10, dies (metadata=None) at the next tick.
        The ring MUST fire at t <= death_time + quiescence_grace (i.e. ~16s or less),
        and MUST NOT require waiting until t=10+120=130 (notifier grace).
        """
        # Large notifier grace to prove we bypass it.
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "sampled"

        # At t=13 the worker has been idle 3s (well below notifier grace=120).
        # Under the old code, collect_due_notifications would NOT have reached
        # the metadata check yet. With F185, tick_quiescence probes death first.
        death_time = 13.0
        mock_persist, _ = self._tick(svc, now=death_time)
        # At death_time itself, the worker just entered _dead_owed with retired_at=13.
        # quiescence grace=3 means we need now >= retired_at + 3 = 16.
        mock_persist.assert_not_called()

        # At t=15.9, still inside quiescence grace (< 3s from retired_at=13)
        mock_persist, _ = self._tick(svc, now=15.9)
        mock_persist.assert_not_called()

        # At t=16.0, exactly at grace boundary — ring fires.
        mock_persist, _ = self._tick(svc, now=16.0)
        mock_persist.assert_called_once()
        msg = mock_persist.call_args[0][2]
        assert "developer-w1" in msg
        assert "last=dead" in msg
        assert "[quiescence watchdog]" in msg

    def test_dead_lane_does_not_wait_for_notifier_grace(self):
        """The ring fires WELL BEFORE the notifier grace would have elapsed.

        Explicitly tests that at t = idle_start + notifier_grace - 1 the ring
        has ALREADY fired (it fired at idle_start + 3 + quiescence_grace).
        """
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "sampled"

        # Tick at t=11 — worker dies immediately after idle.
        mock_persist, _ = self._tick(svc, now=11.0)
        mock_persist.assert_not_called()

        # The worker is now in _dead_owed with retired_at=11.
        with svc._lock:
            assert "w1" in svc._dead_owed.get("sup1", {})
            assert "w1" not in svc._episodes

        # t=14 = retired_at(11) + quiescence_grace(3) — ring fires.
        mock_persist, _ = self._tick(svc, now=14.0)
        mock_persist.assert_called_once()

        # Confirm this is well below the notifier grace boundary (10 + 120 = 130).
        assert 14.0 < 10.0 + 120.0

    def test_dead_lane_retirement_preserves_identity(self):
        """Fast-path retirement preserves profile, generation, caller_id."""
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "kiro_dev")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"
            svc._episodes["w1"].generation = 7

        # Worker dead, supervisor alive — retirement happens without the ring
        # consuming the stores (ring won't fire yet, quiescence grace=3).
        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return None  # worker is dead

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            side_effect=meta_side_effect,
        ), patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=_config_on,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
        ), patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery",
        ):
            svc.tick_quiescence(now=11.0)

        with svc._lock:
            member = svc._dead_owed["sup1"]["w1"]
            assert member.profile == "kiro_dev"
            assert member.generation == 7
            assert member.caller_id == "sup1"
            assert member.last_status == "dead"
            assert member.retired_at == 11.0

    def test_dead_lane_callback_seen_not_retired(self):
        """A worker that has already delivered a callback is NOT retired to _dead_owed."""
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "fp"
            svc._episodes["w1"].callback_seen = True

        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            return_value=None,
        ), patch(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            side_effect=_config_on,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
        ) as mock_persist, patch(
            "cli_agent_orchestrator.services.inbox_service.request_delivery",
        ):
            svc.tick_quiescence(now=200.0)

        with svc._lock:
            assert "w1" not in svc._dead_owed.get("sup1", {})
        mock_persist.assert_not_called()

    def test_dead_lane_anti_spam_no_double_ring(self):
        """After the ring fires once for a dead lane, it does not fire again (D5 dedup)."""
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "sampled"

        # First tick: death + retirement.
        mock_persist, _ = self._tick(svc, now=11.0)
        mock_persist.assert_not_called()

        # Second tick: ring fires.
        mock_persist, _ = self._tick(svc, now=14.0)
        mock_persist.assert_called_once()

        # Third tick: no second ring (D5).
        mock_persist, _ = self._tick(svc, now=100.0)
        mock_persist.assert_not_called()

    def test_live_worker_still_waits_notifier_grace(self):
        """Live workers are NOT affected by the F185 fast-path — they still need
        the notifier grace to elapse before per-worker notice fires."""
        svc = _make_watchdog(grace=120)
        svc.record_inbound_task("w1", "sup1", "developer")
        svc.record_status("w1", TerminalStatus.IDLE, now=10.0)
        with svc._lock:
            svc._episodes["w1"].last_screen_fp = "sampled"

        # Worker is alive (metadata != None), so no retirement should happen.
        def meta_side_effect(tid):
            if tid == "sup1":
                return _meta("sup1")
            return _meta(tid, "sup1")  # worker is alive

        with (
            patch(
                "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
                side_effect=meta_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.config_service.ConfigService.get",
                side_effect=_config_on,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.create_routed_inbox_message",
            ),
            patch(
                "cli_agent_orchestrator.services.inbox_service.request_delivery",
            ),
        ):
            svc.tick_quiescence(now=15.0)

        # Worker must still be in _episodes, NOT retired.
        with svc._lock:
            assert "w1" in svc._episodes
            assert "w1" not in svc._dead_owed.get("sup1", {})

        # The per-worker notifier hasn't fired either (idle_seconds=5 < grace=120).
        with patch(
            "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
            side_effect=meta_side_effect,
        ):
            notices = svc.collect_due_notifications(now=15.0)
        assert notices == []

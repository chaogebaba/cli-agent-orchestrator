import asyncio
import json
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends.herdr_backend import NativeFetch, map_native_status
from cli_agent_orchestrator.kernel.receiver_state import (
    FreshnessProof,
    NativeEvidence,
    ReceiverState,
    ReceiverStateStore,
)
from cli_agent_orchestrator.models.native_publish import NativePublishRequest
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import NativeResolution
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services import receiver_state_view, terminal_service
from cli_agent_orchestrator.services.status_monitor import (
    IdentityProof,
    SettlementEntry,
    StatusMonitor,
)


def _native_state(**overrides):
    values = dict(
        terminal_id="t1",
        lifecycle_generation=1,
        window_identity="w1",
        observation_epoch="e1",
        observation_sequence=1,
        provider="kiro_cli",
        frame_source="native",
        captured_at_mono=10.0,
        frame_hash=None,
        latched_status=TerminalStatus.PROCESSING,
        pass_outcome="native",
        freshness_proof=FreshnessProof("identity_ok"),
        origin="native_poll",
        native_evidence=NativeEvidence("working", TerminalStatus.PROCESSING, 3, 10.0),
    )
    values.update(overrides)
    return ReceiverState(**values)


def test_native_evidence_is_frozen():
    evidence = NativeEvidence("working", TerminalStatus.PROCESSING, 1, 2.0)
    with pytest.raises(FrozenInstanceError):
        evidence.agent_status = "done"


@pytest.mark.parametrize(
    "frame_source,pass_outcome",
    [("native", "probe"), ("fresh_capture", "native"), ("incremental", "native")],
)
def test_illegal_native_pairings_rejected(frame_source, pass_outcome):
    with pytest.raises(ValueError):
        _native_state(frame_source=frame_source, pass_outcome=pass_outcome)


@pytest.mark.parametrize(
    "field,value",
    [
        ("terminal_id", "other"),
        ("observation_epoch", "other"),
        ("observation_sequence", 2),
        ("freshness_proof", FreshnessProof("identity_failed", "x")),
    ],
)
def test_native_pair_coherence_rejects_without_partial_commit(field, value):
    store = ReceiverStateStore()
    fresh = _native_state()
    incremental = _native_state(**{field: value})
    with pytest.raises(ValueError):
        store.publish_native_pair(fresh, incremental, ("e1", 11.0))
    assert (
        store.snapshot_view(fresh.key, require_fresh=False, max_age_s=100.0, now_mono=10.0) is None
    )


def test_native_pair_commits_both_slots_with_token():
    store = ReceiverStateStore()
    observation = _native_state()
    token = ("e1", 11.0)
    store.publish_native_pair(observation, observation, token)
    assert store.snapshot_view(observation.key, require_fresh=False, max_age_s=1.0, now_mono=10.0)
    assert store.snapshot_view(
        observation.key, require_fresh=True, max_age_s=1.0, now_mono=10.0, token=token
    )


@pytest.mark.parametrize(
    "wire,expected",
    [
        ("working", TerminalStatus.PROCESSING),
        ("blocked", TerminalStatus.WAITING_USER_ANSWER),
        ("done", TerminalStatus.COMPLETED),
        ("idle", TerminalStatus.IDLE),
        ("unknown", None),
        ("novel", None),
    ],
)
def test_pure_native_wire_mapping(wire, expected):
    assert map_native_status(wire) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_name", ["pane.agent_status_changed", "pane_agent_status_changed"])
async def test_both_real_wire_spellings_publish(wire_name):
    published = []
    service = HerdrInboxService(native_publish_callback=published.append)
    service._publisher_enabled = True
    service._register_terminal_locked("t1", "p1", False)
    event = {
        "event": wire_name,
        "data": {"pane_id": "p1", "agent_status": "blocked", "agent": "kiro"},
    }
    service._reader = asyncio.StreamReader()
    service._reader.feed_data((json.dumps(event) + "\n").encode())
    service._reader.feed_eof()
    with pytest.raises(ConnectionError):
        await service._event_loop()
    assert len(published) == 1
    assert published[0].agent_status == "blocked"


class _Provider:
    def __init__(self):
        self._flush_lock = threading.RLock()
        self._task_dispatched = False
        self._last_dispatch_time = 0.0
        self._done_first_detected = 0.0
        self._idle_first_detected = 0.0
        self.blocks_orchestrated_input_while_waiting_user_answer = False
        self.composer_stash_keys = None
        self.paste_enter_count = 1
        self.paste_submit_delay = 0.0

    def resolve_native_status(self, native):
        return NativeResolution(native, None, None)

    def _arm_dispatch_locked(self, begun):
        snapshot = {
            "task_dispatched": self._task_dispatched,
            "last_dispatch_time": self._last_dispatch_time,
            "done_first_detected": self._done_first_detected,
            "idle_first_detected": self._idle_first_detected,
        }
        self._task_dispatched = True
        self._last_dispatch_time = begun
        self._done_first_detected = self._idle_first_detected = 0.0
        return snapshot

    def _commit_dispatch_locked(self, submitted):
        self._task_dispatched = True
        self._last_dispatch_time = submitted

    def _restore_dispatch_locked(self, snapshot):
        self._task_dispatched = snapshot["task_dispatched"]
        self._last_dispatch_time = snapshot["last_dispatch_time"]
        self._done_first_detected = snapshot["done_first_detected"]
        self._idle_first_detected = snapshot["idle_first_detected"]


@pytest.fixture
def native_monitor(monkeypatch):
    monitor = StatusMonitor()
    provider = _Provider()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda terminal_id: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: {
            "lifecycle_generation": 1,
            "tmux_window": "w1",
            "tmux_session": "s1",
            "provider": "kiro_cli",
        },
    )
    monitor.set_native_event_gen_accessor(lambda terminal_id, pane_id: 4)
    return monitor, provider


def test_push_and_poll_proof_directions_and_token_ownership(native_monitor):
    monitor, _provider = native_monitor
    received = time.monotonic()
    request = NativePublishRequest("t1", "p1", 4, "working", received)
    monitor.publish_native_observation(
        request, IdentityProof("t1", "native", received + 0.01, None), received
    )
    key = ("t1", 1, "w1")
    assert monitor.receiver_state_store.snapshot_view(
        key, require_fresh=False, max_age_s=1.0, now_mono=received
    )
    assert (
        monitor.receiver_state_store.snapshot_view(
            key, require_fresh=True, max_age_s=1.0, now_mono=received, token=("x", 1.0)
        )
        is None
    )

    proof = IdentityProof("t1", "native", received + 0.02, None)
    fetched = proof.proven_at_mono + 0.01
    token = monitor.publish_native_poll(
        "t1", "p1", NativeFetch("working", TerminalStatus.PROCESSING, None), fetched, proof
    )
    assert token is not None
    assert monitor.receiver_state_store.snapshot_view(
        key, require_fresh=True, max_age_s=1.0, now_mono=fetched, token=token
    )


@pytest.mark.parametrize(
    "fetch",
    [
        NativeFetch(None, None, "command_error"),
        NativeFetch("unknown", None, None),
    ],
)
def test_poll_failure_and_unknown_return_unmatched_token(native_monitor, fetch):
    monitor, _provider = native_monitor
    proof = IdentityProof("t1", "native", 10.0, None)
    token = monitor.publish_native_poll("t1", "p1", fetch, 10.1, proof)
    assert token is not None
    assert (
        monitor.receiver_state_store.snapshot_view(
            ("t1", 1, "w1"), require_fresh=True, max_age_s=1.0, now_mono=10.1, token=token
        )
        is None
    )


def test_dispatch_abort_restores_provider_flush_state(native_monitor):
    monitor, provider = native_monitor
    provider._task_dispatched = False
    provider._last_dispatch_time = 7.0
    txn = monitor.begin_dispatch("t1")
    assert provider._task_dispatched is True
    monitor.abort_dispatch(txn)
    assert provider._task_dispatched is False
    assert provider._last_dispatch_time == 7.0


@pytest.mark.parametrize("send_seam", ["send_input", "send_prepared_input"])
def test_send_failure_aborts_dispatch_and_restores_all_provider_flush_fields(
    native_monitor, monkeypatch, send_seam
):
    monitor, provider = native_monitor
    provider._task_dispatched = False
    provider._last_dispatch_time = 7.0
    provider._done_first_detected = 8.0
    provider._idle_first_detected = 9.0
    before = (
        provider._task_dispatched,
        provider._last_dispatch_time,
        provider._done_first_detected,
        provider._idle_first_detected,
    )
    metadata = {
        "tmux_session": "session",
        "tmux_window": "window",
        "provider": "kiro_cli",
        "caller_id": None,
    }
    backend = MagicMock(supports_identity_readback=False)
    backend.read_native_identity.return_value = SimpleNamespace(verdict="match")
    backend.send_keys.side_effect = RuntimeError("backend send failed")
    monkeypatch.setattr(terminal_service, "status_monitor", monitor)
    monkeypatch.setattr(terminal_service, "get_terminal_metadata", lambda _terminal: metadata)
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service.provider_manager, "get_provider", lambda _terminal: provider
    )
    monkeypatch.setattr(terminal_service, "preserve_draft_before_send", lambda *_args: None)
    monkeypatch.setattr(terminal_service, "inject_memory_context", lambda message, *_a, **_k: message)
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _terminal: None)
    monkeypatch.setattr(monitor, "get_status", lambda _terminal: TerminalStatus.IDLE)
    monkeypatch.setattr(monitor, "notify_input_sent", lambda _terminal: None)
    monkeypatch.setattr(monitor, "clear_rolling_buffer", lambda _terminal: None)

    try:
        with pytest.raises(RuntimeError, match="backend send failed"):
            if send_seam == "send_input":
                terminal_service.send_input("t1", "payload")
            else:
                terminal_service.send_prepared_input("t1", "payload")
    finally:
        terminal_service._memory_injected_terminals.discard("t1")

    assert backend.send_keys.call_count == 1
    assert (
        provider._task_dispatched,
        provider._last_dispatch_time,
        provider._done_first_detected,
        provider._idle_first_detected,
    ) == before
    dispatch_key = ("t1", 1)
    assert dispatch_key in monitor._dispatch_consumed
    assert dispatch_key not in monitor._dispatch_states
    assert dispatch_key not in monitor._dispatch_providers
    assert monitor.active_dispatch_epoch("t1") == 0
    assert monitor._dispatch_mutexes["t1"].acquire(blocking=False)
    monitor._dispatch_mutexes["t1"].release()


@pytest.mark.asyncio
async def test_settlement_timer_removes_matching_entry_and_preserves_replacement(
    native_monitor, monkeypatch
):
    monitor, _provider = native_monitor
    monitor._loop = asyncio.get_running_loop()
    key = ("t1", "p1")
    request = NativePublishRequest("t1", "p1", 4, "working", time.monotonic())
    monkeypatch.setattr(monitor, "_run_settlement", lambda *_args: None)

    monitor._arm_settlement_locked(request, time.monotonic() + 60.0)
    matching = monitor._settlements[key]
    matching.handle._run()
    matching.handle.cancel()
    await asyncio.gather(*tuple(monitor._settlement_tasks))
    assert key not in monitor._settlements

    started = threading.Event()
    release = threading.Event()

    def blocked_settlement(*_args):
        started.set()
        assert release.wait(2.0)

    monkeypatch.setattr(monitor, "_run_settlement", blocked_settlement)
    monitor._arm_settlement_locked(request, time.monotonic() + 60.0)
    superseded = monitor._settlements[key]
    superseded.handle._run()
    superseded.handle.cancel()
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    replacement_request = NativePublishRequest(
        "t1", "p1", request.generation + 1, "working", time.monotonic()
    )
    replacement = SettlementEntry(MagicMock(), replacement_request, request.generation + 1, 0)
    with monitor._lock:
        monitor._settlements[key] = replacement
    release.set()
    await asyncio.gather(*tuple(monitor._settlement_tasks))

    assert monitor._settlements[key] is replacement
    replacement.handle.cancel.assert_not_called()


def test_native_poll_cooldown_suppresses_second_poll(monkeypatch):
    receiver_state_view._native_poll_last.clear()
    backend = MagicMock()
    backend.get_pane_id.return_value = "p1"
    backend.fetch_native_status.return_value = NativeFetch(
        "working", TerminalStatus.PROCESSING, None
    )
    provider = SimpleNamespace(
        capabilities=SimpleNamespace(native_status_source="herdr")
    )
    monitor = MagicMock()
    monitor.prove_terminal_identity.return_value = IdentityProof("t1", "native", 10.0, None)
    monitor.publish_native_poll.return_value = ("epoch", 10.1)
    monkeypatch.setattr(receiver_state_view, "get_backend", lambda: backend)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _terminal: {"tmux_session": "session", "tmux_window": "window"},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _terminal: provider,
    )
    monkeypatch.setattr(
        receiver_state_view.time, "monotonic", MagicMock(side_effect=[10.0, 10.1, 11.0])
    )

    assert receiver_state_view._poll_native_once("t1", monitor) is not None
    assert receiver_state_view._poll_native_once("t1", monitor) is None
    backend.fetch_native_status.assert_called_once_with("session", "window")
    monitor.publish_native_poll.assert_called_once()

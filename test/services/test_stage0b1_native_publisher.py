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
from cli_agent_orchestrator.services.status_monitor import IdentityProof, StatusMonitor


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

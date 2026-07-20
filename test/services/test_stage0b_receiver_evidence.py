from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.kernel.receiver_state import (
    AnchorSpec,
    FreshnessProof,
    ProbeEvidence,
    ReceiverState,
    ReceiverStateStore,
    ScreenSignal,
    classify_screen_signals,
    screen_classification_result,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider, ProviderCapabilities
from cli_agent_orchestrator.services import auto_responder as ar
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
from cli_agent_orchestrator.services.status_monitor import (
    PROOF_MAX_AGE_S,
    IdentityProof,
    ProbeResult,
    StatusMonitor,
)

META = {
    "tmux_session": "session",
    "tmux_window": "window",
    "lifecycle_generation": 2,
    "provider": "fake",
}
KEY = ("terminal", 2, "window")


def _legacy_meta() -> dict[str, object]:
    return {
        "probed_at": "2026-07-20T00:00:00Z",
        "geometry": {"columns": 80, "rows": 24},
        "frame_rows_hash": "abc",
        "frame_source": "fresh_capture",
        "result_status": "idle",
        "law_signal": {"class": "chrome", "provider_signal": "prompt", "row_index": 3},
        "identity_proof_failure": "wrong-pane",
        "probe_failure": "empty_capture",
        "temporal_demotion": {"frames": 2, "multiset_sha256": "def"},
        "injection_hazard": "dialog",
        "transient_api_error": True,
        "idle_reason": "transient_api_error",
    }


def _observation(
    *,
    source: str = "incremental",
    status: TerminalStatus = TerminalStatus.IDLE,
    proof: FreshnessProof | None = None,
    raw=None,
    evidence: ProbeEvidence | None = None,
) -> ReceiverState:
    return ReceiverState(
        terminal_id="terminal",
        lifecycle_generation=2,
        window_identity="window",
        observation_epoch="epoch",
        observation_sequence=1,
        provider="fake",
        frame_source=source,  # type: ignore[arg-type]
        captured_at_mono=10.0,
        frame_hash=None,
        latched_status=status,
        pass_outcome="probe" if source == "fresh_capture" else "accepted",
        freshness_proof=proof
        or FreshnessProof("identity_ok" if source == "fresh_capture" else "not_probed"),
        origin="probe" if source == "fresh_capture" else "incremental",
        raw_classification=raw,
        probe_evidence=evidence,
    )


def test_d1_probe_evidence_is_deeply_frozen_and_serializes_byte_identically() -> None:
    meta = _legacy_meta()
    evidence = ProbeEvidence.from_legacy_dict(meta)
    assert evidence.to_legacy_dict() == meta
    assert list(evidence.to_legacy_dict()) == list(meta)
    with pytest.raises(FrozenInstanceError):
        evidence.geometry.columns = 99  # type: ignore[misc]
    meta["geometry"]["columns"] = 1  # type: ignore[index]
    assert evidence.geometry.columns == 80


def test_d1_signal_and_result_repr_redact_raw_rows() -> None:
    secret = "private frame text"
    signal = ScreenSignal("progress", "spinner", 4, secret, "corroborable")
    result = screen_classification_result([signal])
    assert secret not in repr(signal)
    assert secret not in repr(result)
    assert "sha256:" in repr(result)


def test_pin1_frozen_clock_mints_strictly_increasing_tokens(monkeypatch) -> None:
    store = ReceiverStateStore()
    monkeypatch.setattr(
        "cli_agent_orchestrator.kernel.receiver_state.store.time.monotonic", lambda: 10.0
    )
    first = store.mint_token("terminal", "epoch")
    second = store.mint_token("terminal", "epoch")
    assert second[1] > first[1]
    assert second != first


def test_d3_fresh_selection_is_exclusive_token_owned_and_invalidated() -> None:
    store = ReceiverStateStore()
    store.publish_observation(_observation(status=TerminalStatus.PROCESSING))
    token = store.mint_token("terminal", "epoch")
    store.publish_observation(_observation(source="fresh_capture"), fresh_token=token)
    assert store.snapshot_view(KEY, require_fresh=True, max_age_s=5, now_mono=11) is None
    assert (
        store.snapshot_view(
            KEY,
            require_fresh=True,
            max_age_s=5,
            now_mono=11,
            token=("epoch", token[1] + 1),
        )
        is None
    )
    view = store.snapshot_view(KEY, require_fresh=True, max_age_s=5, now_mono=11, token=token)
    assert view is not None and view.frame_source == "fresh_capture"
    assert store.invalidate_terminal("terminal") == 1
    assert store.snapshot_view(KEY, require_fresh=True, max_age_s=5, token=token) is None


def test_d3_identity_failed_fresh_observation_is_effect_ineligible() -> None:
    store = ReceiverStateStore()
    token = store.mint_token("terminal", "epoch")
    store.publish_observation(
        _observation(source="fresh_capture", proof=FreshnessProof("identity_failed", "wrong-pane")),
        fresh_token=token,
    )
    assert (
        store.snapshot_view(KEY, require_fresh=True, max_age_s=5, now_mono=11, token=token) is None
    )


@pytest.mark.parametrize(
    ("prior", "current", "anchor", "status"),
    [
        (
            [ScreenSignal("progress", "spin", 1, "same", "corroborable")],
            [ScreenSignal("progress", "spin", 9, "same", "corroborable")],
            AnchorSpec("spin", "corroborable"),
            TerminalStatus.UNKNOWN,
        ),
        (
            [],
            [ScreenSignal("progress", "spin", 1, "new", "corroborable")],
            AnchorSpec("spin", "corroborable"),
            TerminalStatus.PROCESSING,
        ),
        (
            [ScreenSignal("progress", "old", 1, "same", "corroborable")],
            [ScreenSignal("progress", "new", 1, "same", "corroborable")],
            AnchorSpec("new", "corroborable"),
            TerminalStatus.PROCESSING,
        ),
        (
            [ScreenSignal("progress", "spin", 1, "same", "corroborable")],
            [ScreenSignal("progress", "spin", 1, "same", "corroborable")],
            None,
            TerminalStatus.UNKNOWN,
        ),
    ],
)
def test_d4_anchor_and_fallback_comparator_laws(prior, current, anchor, status) -> None:
    assert classify_screen_signals(current, prior, anchor).status is status


def test_d4_multiset_subtraction_corroborates_duplicate_growth_deterministically() -> None:
    prior = [ScreenSignal("progress", "spin", 8, "same", "corroborable")]
    current = [
        ScreenSignal("progress", "spin", 2, "same", "corroborable"),
        ScreenSignal("progress", "spin", 5, "same", "corroborable"),
    ]
    result = classify_screen_signals(current, prior, AnchorSpec("spin", "corroborable"))
    assert result.status is TerminalStatus.PROCESSING
    assert result.row_index == 5


def test_d4_exempt_running_is_never_demoted() -> None:
    prior = [ScreenSignal("progress", "RUNNING_PATTERN", 1, "same", "exempt")]
    current = [ScreenSignal("progress", "RUNNING_PATTERN", 1, "same", "exempt")]
    result = classify_screen_signals(current, prior)
    assert result.status is TerminalStatus.PROCESSING
    assert result.provider_signal == "RUNNING_PATTERN"


def test_d4_reducer_is_stateless_across_identical_calls() -> None:
    prior = [ScreenSignal("progress", "spin", 1, "same", "corroborable")]
    current = [ScreenSignal("progress", "spin", 2, "same", "corroborable")]
    assert classify_screen_signals(current, prior) == classify_screen_signals(current, prior)


class _Emitter:
    capabilities = ProviderCapabilities(
        supports_screen_detection=True,
        signal_kinds=frozenset({"progress", "chrome"}),
    )

    def emit_screen_signals(self, rows):
        row = rows[0]
        if row.startswith("spin"):
            return (ScreenSignal("progress", "spin", 0, row, "corroborable"),)
        return (ScreenSignal("chrome", "prompt", 0),)

    def get_status_from_screen(self, rows):
        return screen_classification_result(self.emit_screen_signals(rows)).status

    def classify_injection_hazard(self, _rows):
        return None

    def transient_error_detected(self, _rows, _classification):
        return False

    def classify_idle_reason(self, _rows, _classification):
        return None


class _StatusOnlyProvider(BaseProvider):
    supports_screen_detection = True

    def __init__(self, status: TerminalStatus) -> None:
        super().__init__("terminal", "session", "window")
        self._test_status = status

    async def initialize(self) -> bool:
        return True

    def get_status(self, _buffer: str) -> TerminalStatus:
        return self._test_status

    def extract_last_message_from_script(self, script_output: str) -> str:
        return script_output

    def exit_cli(self) -> str:
        return "exit"

    def cleanup(self) -> None:
        return None


class _CorroboratingRunningEmitter:
    capabilities = ProviderCapabilities(
        supports_screen_detection=True,
        signal_kinds=frozenset({"progress"}),
        liveness_anchor=AnchorSpec("RUNNING_PATTERN", "corroborable"),
    )

    def __init__(self) -> None:
        self.emitter_calls = 0

    def emit_screen_signals(self, rows):
        self.emitter_calls += 1
        return (
            ScreenSignal(
                "progress", "RUNNING_PATTERN", 0, rows[0], "corroborable"
            ),
        )

    def get_status_from_screen(self, _rows):
        return TerminalStatus.PROCESSING

    def classify_idle_reason(self, _rows, _classification):
        return None


def test_d3_probe_proves_before_every_temporal_capture_and_returns_typed_abi(monkeypatch) -> None:
    monitor = StatusMonitor()
    monitor._screens["terminal"] = (
        SimpleNamespace(display=["spin-a"], columns=80, lines=1),
        object(),
    )
    backend = MagicMock(supports_identity_readback=True)
    backend.capture_viewport.side_effect = ["spin-a", "spin-a", "spin-b"]
    backend.get_pane_size.return_value = (80, 1)
    order: list[str] = []

    def prove(_terminal_id):
        order.append("prove")
        return IdentityProof("terminal", "pane_readback", 10.0, None)

    backend.capture_viewport.side_effect = lambda *_args: (
        order.append("capture") or ["spin-a", "spin-a", "spin-b"][order.count("capture") - 1]
    )
    monkeypatch.setattr(monitor, "prove_terminal_identity", prove)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.time.sleep", lambda _s: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.time.monotonic", lambda: 10.1
    )
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_Emitter(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.clients.database.get_terminal_metadata", return_value=META),
    ):
        result = monitor.probe_screen_status("terminal")

    assert isinstance(result, ProbeResult)
    assert result.status is TerminalStatus.PROCESSING
    assert order == ["prove", "capture", "prove", "capture", "prove", "capture"]
    with pytest.raises(TypeError):
        _status, _meta = result  # type: ignore[misc]


def test_d6_publish_validates_proof_age_before_containment(monkeypatch) -> None:
    monitor = StatusMonitor()
    classification = screen_classification_result([ScreenSignal("chrome", "prompt", 0)])
    future = IdentityProof("terminal", "pane_readback", 11.0, None)
    old = IdentityProof("terminal", "pane_readback", 10.0 - PROOF_MAX_AGE_S - 0.1, None)
    with pytest.raises(ValueError):
        monitor.publish_fresh_observation(
            "terminal", ["prompt"], 10.0, classification, "fresh_capture", future
        )
    with pytest.raises(ValueError):
        monitor.publish_fresh_observation(
            "terminal", ["prompt"], 10.0, classification, "fresh_capture", old
        )


def test_d6_publish_accepts_zero_age_proof_and_token_owned_read(monkeypatch) -> None:
    monitor = StatusMonitor()
    provider = _Emitter()
    classification = screen_classification_result([ScreenSignal("chrome", "prompt", 0)])
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata", lambda _id: META
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _id: provider,
    )

    token = monitor.publish_fresh_observation(
        "terminal",
        ["prompt"],
        10.0,
        classification,
        "fresh_capture",
        IdentityProof("terminal", "pane_readback", 10.0, None),
    )

    view = monitor.receiver_state_store.snapshot_view(
        KEY, require_fresh=True, max_age_s=2.0, now_mono=10.0, token=token
    )
    assert view is not None
    assert view.raw_classification is classification


def test_d1_raw_classification_domain_for_forced_status_only_and_emitters(
    monkeypatch,
) -> None:
    monitor = StatusMonitor()
    emitter = _Emitter()
    status_only = _StatusOnlyProvider(TerminalStatus.PROCESSING)
    classification = screen_classification_result(
        [ScreenSignal("progress", "RUNNING_PATTERN", 0, "running", "exempt")]
    )
    metadata = {
        "terminal": META,
        "status": {**META, "tmux_window": "status-window", "provider": "status-only"},
    }
    providers = {"terminal": emitter, "status": status_only}
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda terminal_id: metadata[terminal_id],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda terminal_id: providers[terminal_id],
    )

    with pytest.raises(ValueError, match="forced observations"):
        replace(_observation(), origin="forced", raw_classification=classification)

    monitor.receiver_state_store.publish_observation(_observation(raw=classification))
    incremental = monitor.receiver_state_store.snapshot_view(
        KEY, require_fresh=False, max_age_s=2.0, now_mono=10.0
    )
    assert incremental is not None and incremental.raw_classification is classification

    emitter_token = monitor.publish_fresh_observation(
        "terminal",
        ["running"],
        10.0,
        classification,
        "fresh_capture",
        IdentityProof("terminal", "pane_readback", 10.0, None),
    )
    emitter_view = monitor.receiver_state_store.snapshot_view(
        KEY, require_fresh=True, max_age_s=2.0, now_mono=10.0, token=emitter_token
    )
    assert emitter_view is not None and emitter_view.raw_classification is classification

    status_token = monitor.publish_fresh_observation(
        "status",
        ["running"],
        10.0,
        classification,
        "fresh_capture",
        IdentityProof("status", "pane_readback", 10.0, None),
    )
    status_view = monitor.receiver_state_store.snapshot_view(
        ("status", 2, "status-window"),
        require_fresh=True,
        max_age_s=2.0,
        now_mono=10.0,
        token=status_token,
    )
    assert status_view is not None
    assert status_view.latched_status is TerminalStatus.PROCESSING
    assert status_view.raw_classification is None


def test_d6_status_only_processing_watchdog_never_suppresses(monkeypatch) -> None:
    monitor = StatusMonitor()
    provider = _StatusOnlyProvider(TerminalStatus.PROCESSING)
    metadata = {
        "id": "terminal",
        "provider": "status-only",
        "tmux_session": "session",
        "tmux_window": "window",
        "lifecycle_generation": 2,
    }
    backend = MagicMock()
    backend.capture_viewport.return_value = "processing"
    monkeypatch.setattr(
        monitor,
        "prove_terminal_identity",
        lambda terminal_id: IdentityProof(terminal_id, "pane_readback", 10.0, None),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor", monitor
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _id: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _id: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _id: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
        lambda op: op == "watchdog.pane_classify",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.time.monotonic",
        lambda: 10.0,
    )

    result = StalledCallbackWatchdog()._fresh_frame_decides_running("terminal")

    assert result == (False, None)
    view = monitor.receiver_state_store.prior_classification(KEY, prefer_fresh=True)
    assert view is None


def test_d6_status_only_auto_responder_uses_latched_status_before_enter(
    monkeypatch,
) -> None:
    monitor = StatusMonitor()
    provider = _StatusOnlyProvider(TerminalStatus.WAITING_USER_ANSWER)
    engine = ar.AutoResponder()
    rule = ar.Rule("status-only", True, "contains", "Proceed?", [], ["Enter"])
    metadata = {
        "id": "terminal",
        "provider": "status-only",
        "tmux_session": "session",
        "tmux_window": "window",
        "lifecycle_generation": 2,
    }
    order: list[str] = []
    published: dict[str, object] = {}
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    backend.capture_viewport.side_effect = lambda *_args: order.append("capture") or "Proceed?"
    backend.send_special_key.side_effect = lambda *_args: order.append("effect")
    real_publish = monitor.publish_fresh_observation
    real_snapshot = monitor.receiver_state_store.snapshot_view

    def prove(terminal_id):
        order.append("prove")
        return IdentityProof(terminal_id, "pane_readback", 10.0, None)

    def publish(*args, **kwargs):
        order.append("publish")
        token = real_publish(*args, **kwargs)
        published["token"] = token
        return token

    def snapshot(*args, **kwargs):
        order.append("read")
        assert kwargs["token"] == published["token"]
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(monitor, "prove_terminal_identity", prove)
    monkeypatch.setattr(monitor, "publish_fresh_observation", publish)
    monkeypatch.setattr(monitor.receiver_state_store, "snapshot_view", snapshot)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor", monitor
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _id: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _id: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
        lambda op: op == "auto_responder.frame_classify",
    )
    monkeypatch.setattr(ar.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(ar.threading, "Thread", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(engine, "_log", lambda *_args: None)

    fired = engine._fire(
        "terminal",
        metadata,
        provider,
        rule,
        "Proceed?",
        engine._state_for("terminal", rule.name),
    )

    assert fired
    backend.send_special_key.assert_called_once_with("session", "window", "Enter")
    assert order == ["prove", "capture", "publish", "read", "effect"]


def test_d6_signal_emitting_watchdog_plumbs_priors_into_running_decision(
    monkeypatch,
) -> None:
    monitor = StatusMonitor()
    provider = _CorroboratingRunningEmitter()
    metadata = {
        "id": "terminal",
        "provider": "emitter",
        "tmux_session": "session",
        "tmux_window": "window",
        "lifecycle_generation": 2,
    }
    backend = MagicMock()
    backend.capture_viewport.return_value = "same"
    monkeypatch.setattr(
        monitor,
        "prove_terminal_identity",
        lambda terminal_id: IdentityProof(terminal_id, "pane_readback", 10.0, None),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.status_monitor", monitor
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.get_terminal_metadata",
        lambda _id: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _id: metadata,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _id: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.seam_activation.receiver_state_active",
        lambda op: op == "watchdog.pane_classify",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.time.monotonic",
        lambda: 10.0,
    )
    prior_same = screen_classification_result(
        [ScreenSignal("progress", "RUNNING_PATTERN", 0, "same", "corroborable")]
    )
    monitor.receiver_state_store.publish_observation(
        _observation(status=TerminalStatus.PROCESSING, raw=prior_same)
    )
    svc = StalledCallbackWatchdog()

    with patch(
        "cli_agent_orchestrator.providers.screen_classification.screen_classification_result",
        wraps=screen_classification_result,
    ) as reducer:
        assert svc._fresh_frame_decides_running("terminal") == (False, None)
        monitor.receiver_state_store.invalidate_terminal("terminal")
        prior_changed = screen_classification_result(
            [ScreenSignal("progress", "RUNNING_PATTERN", 0, "old", "corroborable")]
        )
        monitor.receiver_state_store.publish_observation(
            _observation(status=TerminalStatus.PROCESSING, raw=prior_changed)
        )
        assert svc._fresh_frame_decides_running("terminal") == (True, None)

    assert provider.emitter_calls == 2
    assert reducer.call_args_list[0].args[1] == prior_same.signals
    assert reducer.call_args_list[1].args[1] == prior_changed.signals


def test_pin1_publish_fault_returns_unmatched_token(monkeypatch) -> None:
    monitor = StatusMonitor()
    classification = screen_classification_result([ScreenSignal("chrome", "prompt", 0)])
    proof = IdentityProof("terminal", "pane_readback", 9.9, None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata", lambda _id: META
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _id: _Emitter(),
    )
    monkeypatch.setattr(
        monitor.receiver_state_store,
        "publish_observation",
        MagicMock(side_effect=RuntimeError("x")),
    )
    token = monitor.publish_fresh_observation(
        "terminal", ["prompt"], 10.0, classification, "fresh_capture", proof
    )
    assert isinstance(token, tuple) and len(token) == 2
    assert (
        monitor.receiver_state_store.snapshot_view(
            KEY, require_fresh=True, max_age_s=5, now_mono=10.1, token=token
        )
        is None
    )


def test_d6_all_auto_responder_effect_sites_call_the_fresh_barrier() -> None:
    source = (
        Path(__file__).parents[2] / "src/cli_agent_orchestrator/services/auto_responder.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_fire", "_verify_and_retry", "_surface_retry_exhausted"):
        calls = {
            node.func.attr
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_effect_barrier" in calls


def test_d4_provider_bodies_emit_raw_signals_without_reducing() -> None:
    root = Path(__file__).parents[2] / "src/cli_agent_orchestrator/providers"
    for filename in ("codex.py", "claude_code.py", "grok_cli.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        emitter = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "emit_screen_signals"
        )
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(emitter)
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        assert "classify_screen_signals" not in calls
        assert "screen_classification_result" not in calls

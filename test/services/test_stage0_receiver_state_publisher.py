"""Stage-0a receiver-state publisher gates for StatusMonitor."""

from __future__ import annotations

import ast
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.kernel.receiver_state import FreshnessProof, ReceiverState
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.kernel.receiver_state.classification import (
    ScreenClassification,
    ScreenClassificationResult,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def _metadata(_terminal_id: str) -> dict[str, object]:
    return {
        "tmux_session": "worker-session",
        "tmux_window": "worker-window",
        "lifecycle_generation": 4,
        "provider": "codex",
    }


def _install_capture(monkeypatch, monitor: StatusMonitor, records: list[dict[str, object]]) -> None:
    def capture(_terminal_id: str, **kwargs: object) -> None:
        records.append(kwargs)
        assert monitor._lock._is_owned()  # type: ignore[attr-defined]

    monkeypatch.setattr(monitor, "_publish_observation", capture)


def _patch_external(monkeypatch, monitor: StatusMonitor, published: list[str]) -> None:
    bus = MagicMock()

    def publish(_topic: str, data: dict[str, str]) -> None:
        assert not monitor._lock._is_owned()  # type: ignore[attr-defined]
        published.append(data["status"])

    bus.publish.side_effect = publish
    responder = MagicMock()
    responder.record_published_status.side_effect = lambda _terminal_id, _status: assert_unlocked(
        monitor
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.bus", bus)
    monkeypatch.setattr("cli_agent_orchestrator.services.auto_responder.auto_responder", responder)


def assert_unlocked(monitor: StatusMonitor) -> None:
    assert not monitor._lock._is_owned()  # type: ignore[attr-defined]


def test_single_exit_publishes_accepted_and_keeps_external_bus_out_of_lock(
    monkeypatch,
) -> None:
    monitor = StatusMonitor()
    records: list[dict[str, object]] = []
    published: list[str] = []
    _install_capture(monkeypatch, monitor, records)
    _patch_external(monkeypatch, monitor, published)

    monitor._apply_detection("t1", TerminalStatus.IDLE)

    assert records == [
        {
            "latched_status": TerminalStatus.IDLE,
            "pass_outcome": "accepted",
            "frame_source": "incremental",
            "metadata": None,
        }
    ]
    assert published == ["idle"]


def test_truth_table_rejects_and_force_status_all_publish_observations(monkeypatch) -> None:
    monitor = StatusMonitor()
    records: list[dict[str, object]] = []
    published: list[str] = []
    _install_capture(monkeypatch, monitor, records)
    _patch_external(monkeypatch, monitor, published)

    monitor._apply_detection("t1", TerminalStatus.IDLE)
    monitor._apply_detection("t1", TerminalStatus.IDLE)
    monitor._apply_detection("t1", TerminalStatus.PROCESSING)
    monitor._apply_detection("t1", TerminalStatus.UNKNOWN)
    monitor._chunk_seq["t1"] = 2
    monitor._apply_detection("t1", TerminalStatus.COMPLETED, expected_seq=1)
    monitor.force_status("t1", TerminalStatus.WAITING_USER_ANSWER)

    assert [record["pass_outcome"] for record in records] == [
        "accepted",
        "no_change",
        "sticky_rejected",
        "unknown_suppressed",
        "stale_seq",
        "forced",
    ]
    assert [record["latched_status"] for record in records] == [
        TerminalStatus.IDLE,
        TerminalStatus.IDLE,
        TerminalStatus.IDLE,
        TerminalStatus.IDLE,
        TerminalStatus.IDLE,
        TerminalStatus.WAITING_USER_ANSWER,
    ]
    assert published == ["idle", "waiting_user_answer"]


def test_stale_expected_sequence_does_not_mutate_latch(monkeypatch) -> None:
    monitor = StatusMonitor()
    monitor._last_status["t1"] = TerminalStatus.IDLE
    monitor._processing_gen["t1"] = 7
    monitor._chunk_seq["t1"] = 2
    records: list[dict[str, object]] = []
    _install_capture(monkeypatch, monitor, records)
    _patch_external(monkeypatch, monitor, [])

    monitor._apply_detection("t1", TerminalStatus.PROCESSING, expected_seq=1)

    assert monitor._last_status["t1"] is TerminalStatus.IDLE
    assert monitor._processing_gen["t1"] == 7
    assert records[0]["pass_outcome"] == "stale_seq"


def test_hook_failure_is_swallowed_without_masking_body_or_external_publish(monkeypatch) -> None:
    monitor = StatusMonitor()
    published: list[str] = []
    _patch_external(monkeypatch, monitor, published)
    monkeypatch.setattr(monitor, "_publish_observation", MagicMock(side_effect=ValueError("hook")))
    log_failure = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.logger.exception", log_failure
    )

    monitor._apply_detection("t1", TerminalStatus.IDLE)

    assert monitor._last_status["t1"] is TerminalStatus.IDLE
    assert published == ["idle"]
    log_failure.assert_called_once()


def test_hook_failure_never_replaces_in_lock_body_exception(monkeypatch) -> None:
    monitor = StatusMonitor()
    monkeypatch.setattr(monitor, "_observe_locked", MagicMock(side_effect=RuntimeError("body")))
    monkeypatch.setattr(monitor, "_publish_observation", MagicMock(side_effect=ValueError("hook")))
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.logger.exception", MagicMock()
    )

    with pytest.raises(RuntimeError, match="body"):
        monitor._apply_detection("t1", TerminalStatus.IDLE)


@pytest.mark.parametrize("failure_point", ["before_latch", "after_latch"])
def test_in_lock_exception_publishes_aborted_latch_at_exception_time(
    monkeypatch, failure_point: str
) -> None:
    monitor = StatusMonitor()
    records: list[dict[str, object]] = []
    _install_capture(monkeypatch, monitor, records)
    _patch_external(monkeypatch, monitor, [])

    if failure_point == "before_latch":
        monkeypatch.setattr(
            monitor, "_observe_locked", MagicMock(side_effect=RuntimeError("before"))
        )
        with pytest.raises(RuntimeError, match="before"):
            monitor._apply_detection("t1", TerminalStatus.IDLE)
    else:

        class FailingMapping(dict):
            def __setitem__(self, _key, _value):
                raise RuntimeError("after")

        monitor._last_status["t1"] = TerminalStatus.UNKNOWN
        monitor._processing_gen = FailingMapping()
        with pytest.raises(RuntimeError, match="after"):
            monitor._apply_detection("t1", TerminalStatus.PROCESSING)

    assert records[0]["pass_outcome"] == "aborted"
    assert records[0]["latched_status"] is (
        TerminalStatus.UNKNOWN if failure_point == "before_latch" else TerminalStatus.PROCESSING
    )


def test_out_of_lock_bus_failure_is_post_pass_and_not_aborted(monkeypatch) -> None:
    monitor = StatusMonitor()
    records: list[dict[str, object]] = []
    _install_capture(monkeypatch, monitor, records)
    bus = MagicMock()
    bus.publish.side_effect = RuntimeError("bus")
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.bus", bus)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder", MagicMock()
    )

    with pytest.raises(RuntimeError, match="bus"):
        monitor._apply_detection("t1", TerminalStatus.IDLE)

    assert records[0]["pass_outcome"] == "accepted"


def test_real_kernel_publish_uses_terminal_key_and_latched_projection(monkeypatch) -> None:
    monitor = StatusMonitor()

    def metadata_outside_monitor_lock(terminal_id: str):
        assert not monitor._lock._is_owned()  # type: ignore[attr-defined]
        return _metadata(terminal_id)

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        metadata_outside_monitor_lock,
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.status_monitor.bus", MagicMock())
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder", MagicMock()
    )

    monitor._apply_detection("t1", TerminalStatus.IDLE)
    view = monitor.receiver_state_store.snapshot_view(
        ("t1", 4, "worker-window"),
        require_fresh=False,
        max_age_s=30.0,
        now_mono=time.monotonic(),
    )

    assert view is not None
    assert view.latched_status is TerminalStatus.IDLE
    assert view.pass_outcome == "accepted"
    assert view.provider == "codex"
    assert view.observation_sequence == 1


def test_probe_publishes_settled_result_to_fresh_slot_with_proof(monkeypatch) -> None:
    monitor = StatusMonitor()

    class Provider:
        def classify_screen(self, _rows):
            return ScreenClassificationResult(
                ScreenClassification(TerminalStatus.IDLE, "chrome", None, None), ()
            )

        def classify_injection_hazard(self, _rows):
            return None

        def transient_error_detected(self, _rows, _classification):
            return False

        def classify_idle_reason(self, _rows, _classification):
            return None

    backend = MagicMock()
    backend.supports_identity_readback = True
    backend.capture_viewport.return_value = "prompt"
    backend.get_pane_size.return_value = (80, 24)
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.manager.provider_manager.get_provider",
        lambda _terminal_id: Provider(),
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.pane_identity_service.pane_identity_failure",
        lambda *_args: None,
    )
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", _metadata)

    status, _meta = monitor.probe_screen_status("t1")

    assert status is TerminalStatus.IDLE
    slots = monitor.receiver_state_store._entries[("t1", 4, "worker-window")]  # type: ignore[attr-defined]
    observation = slots.latest_fresh.latest
    assert observation is not None
    assert observation.frame_source == "fresh_capture"
    assert observation.pass_outcome == "probe"
    assert observation.latched_status is TerminalStatus.IDLE
    assert observation.freshness_proof.identity_ok


def test_reset_buffer_invalidates_pre_reset_receiver_observation(monkeypatch) -> None:
    monitor = StatusMonitor()
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", _metadata)
    monitor.receiver_state_store.publish_observation(
        ReceiverState(
            terminal_id="t1",
            lifecycle_generation=4,
            window_identity="worker-window",
            observation_epoch="11111111-1111-1111-1111-111111111111",
            observation_sequence=1,
            provider="codex",
            frame_source="incremental",
            captured_at_mono=time.monotonic(),
            frame_hash=None,
            latched_status=TerminalStatus.IDLE,
            pass_outcome="accepted",
            freshness_proof=FreshnessProof("not_probed"),
        )
    )

    monitor.reset_buffer("t1")

    assert (
        monitor.receiver_state_store.snapshot_view(
            ("t1", 4, "worker-window"),
            require_fresh=False,
            max_age_s=30.0,
        )
        is None
    )


def test_receiver_store_publisher_is_closed_to_other_modules() -> None:
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator"
    violations: list[str] = []
    for source_path in root.rglob("*.py"):
        relative_path = source_path.relative_to(root).as_posix()
        if relative_path in {"kernel/receiver_state/store.py", "services/status_monitor.py"}:
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "publish_observation":
                    violations.append(f"{source_path}:{node.lineno}")
    assert violations == []

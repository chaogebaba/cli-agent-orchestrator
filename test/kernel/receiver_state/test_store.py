from __future__ import annotations

import ast
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from cli_agent_orchestrator.kernel.receiver_state import (
    FreshnessProof,
    ReceiverState,
    ReceiverStateStore,
    ScreenSignal,
    apply_recovery_overlay,
    pass_outcome_for_source,
    screen_classification_result,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.screen_classification import (
    ScreenSignal as CompatibilityScreenSignal,
)


def _state(
    *,
    captured_at_mono: float = 10.0,
    status: TerminalStatus = TerminalStatus.IDLE,
    outcome: str = "accepted",
    frame_source: str = "incremental",
    sequence: int = 1,
) -> ReceiverState:
    proof = "identity_ok" if frame_source == "fresh_capture" else "not_probed"
    return ReceiverState(
        terminal_id="deadbeef",
        lifecycle_generation=3,
        window_identity="worker-1",
        observation_epoch="11111111-1111-1111-1111-111111111111",
        observation_sequence=sequence,
        provider="codex",
        frame_source=frame_source,  # type: ignore[arg-type]
        captured_at_mono=captured_at_mono,
        frame_hash=None,
        latched_status=status,
        pass_outcome=outcome,  # type: ignore[arg-type]
        freshness_proof=FreshnessProof(proof),  # type: ignore[arg-type]
    )


def test_d1_compatibility_module_reexports_kernel_types() -> None:
    assert CompatibilityScreenSignal is ScreenSignal
    result = screen_classification_result([ScreenSignal("chrome", "composer", 2)])
    assert result.status is TerminalStatus.IDLE


def test_d1_kernel_import_direction_is_stdlib_models_or_local_only() -> None:
    package = (
        Path(__file__).parents[3] / "src" / "cli_agent_orchestrator" / "kernel" / "receiver_state"
    )
    violations: list[str] = []
    for source_path in sorted(package.glob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                root = module.partition(".")[0]
                allowed = root in sys.stdlib_module_names or module.startswith(
                    "cli_agent_orchestrator.models"
                )
                if not allowed:
                    violations.append(f"{source_path.name}:{node.lineno}:{module}")
    assert violations == []


def test_d2_slot_merge_and_fresh_slot_isolation() -> None:
    store = ReceiverStateStore()
    incremental = _state(status=TerminalStatus.PROCESSING, captured_at_mono=10.0)
    fresh = _state(
        status=TerminalStatus.IDLE,
        outcome="probe",
        frame_source="fresh_capture",
        captured_at_mono=20.0,
        sequence=2,
    )

    store.publish_observation(incremental)
    store.publish_observation(fresh)

    view = store.snapshot_view(incremental.key, require_fresh=False, max_age_s=30.0, now_mono=21.0)
    assert view is not None
    assert view.latched_status is TerminalStatus.PROCESSING
    assert view.frame_source == "incremental"
    assert view.observation_sequence == 1
    assert (
        store.snapshot_view(incremental.key, require_fresh=True, max_age_s=30.0, now_mono=21.0)
        is None
    )


@pytest.mark.parametrize(
    ("outcome", "eligible"),
    [
        ("accepted", True),
        ("no_change", True),
        ("stale_seq", False),
        ("unknown_suppressed", True),
        ("sticky_rejected", True),
        ("forced", True),
        ("aborted", False),
    ],
)
def test_d2_latch_truth_table_outcomes(outcome: str, eligible: bool) -> None:
    store = ReceiverStateStore()
    seed = _state(captured_at_mono=9.0)
    observation = _state(outcome=outcome, captured_at_mono=10.0, sequence=2)
    store.publish_observation(seed)
    store.publish_observation(observation)

    view = store.snapshot_view(observation.key, require_fresh=False, max_age_s=30.0, now_mono=11.0)
    assert view is not None
    assert view.latched_status is TerminalStatus.IDLE
    assert view.pass_outcome == outcome
    assert view.freshness_eligible is eligible


def test_d2_probe_truth_table_and_slot_shape_are_closed() -> None:
    probe = _state(outcome="probe", frame_source="fresh_capture")
    assert probe.pass_outcome == "probe"
    assert probe.freshness_eligible is True
    with pytest.raises(ValueError, match="fresh_capture"):
        replace(probe, pass_outcome="accepted")
    with pytest.raises(ValueError, match="incremental"):
        replace(_state(), pass_outcome="probe")
    with pytest.raises(ValueError, match="pass outcome"):
        _state(outcome="filtered")
    with pytest.raises(ValueError, match="freshness proof"):
        FreshnessProof("unknown")  # type: ignore[arg-type]


def test_d2_pass_source_forces_only_settled_non_aborted_passes() -> None:
    assert pass_outcome_for_source("inline", "accepted") == "accepted"
    assert pass_outcome_for_source("forced", "accepted") == "forced"
    assert pass_outcome_for_source("forced", "no_change") == "forced"
    assert pass_outcome_for_source("forced", "aborted") == "aborted"


def test_d2_stale_reject_does_not_renew_freshness() -> None:
    store = ReceiverStateStore()
    accepted = _state(captured_at_mono=10.0, outcome="accepted")
    store.publish_observation(accepted)
    store.publish_observation(
        replace(
            accepted,
            observation_sequence=2,
            captured_at_mono=20.0,
            pass_outcome="stale_seq",
        )
    )

    assert (
        store.snapshot_view(accepted.key, require_fresh=False, max_age_s=5.0, now_mono=21.0) is None
    )
    visible = store.snapshot_view(accepted.key, require_fresh=False, max_age_s=30.0, now_mono=21.0)
    assert visible is not None
    assert visible.pass_outcome == "stale_seq"
    assert visible.captured_at_mono == 20.0
    assert visible.freshness_eligible is False


@pytest.mark.parametrize(
    "recovery_state",
    [
        "rebind_starting",
        "rebind_exiting",
        "rebind_failed",
        "fallback_starting",
        "fallback_ready",
    ],
)
def test_d2_recovery_overlay_projects_error(recovery_state: str) -> None:
    assert (
        apply_recovery_overlay(TerminalStatus.PROCESSING, recovery_state)  # type: ignore[arg-type]
        is TerminalStatus.ERROR
    )


@pytest.mark.parametrize("recovery_state", [None, "rebound"])
def test_d2_recovery_overlay_preserves_raw_status(recovery_state: str | None) -> None:
    assert (
        apply_recovery_overlay(TerminalStatus.PROCESSING, recovery_state)  # type: ignore[arg-type]
        is TerminalStatus.PROCESSING
    )


def test_d2_snapshot_applies_recovery_overlay_after_slot_read() -> None:
    store = ReceiverStateStore()
    observation = _state(status=TerminalStatus.COMPLETED)
    store.publish_observation(observation)

    view = store.snapshot_view(
        observation.key,
        require_fresh=False,
        max_age_s=30.0,
        recovery_state="rebind_starting",
        now_mono=11.0,
    )
    assert view is not None
    assert view.latched_status is TerminalStatus.ERROR


def test_d2_invalidation_is_exact_or_terminal_wide() -> None:
    store = ReceiverStateStore()
    first = _state()
    second = replace(first, lifecycle_generation=4, observation_sequence=2)
    third = replace(first, terminal_id="cafebabe")
    for observation in (first, second, third):
        store.publish_observation(observation)

    assert store.invalidate(first.key) is True
    assert store.invalidate(first.key) is False
    assert store.invalidate_terminal("deadbeef") == 1
    assert (
        store.snapshot_view(third.key, require_fresh=False, max_age_s=30.0, now_mono=11.0)
        is not None
    )

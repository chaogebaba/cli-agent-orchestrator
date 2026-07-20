"""D5-D7 activation, view-shim, capability, and trace-manifest gates."""

from __future__ import annotations

import concurrent.futures
import ast
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.kernel.receiver_state import (
    FreshnessProof,
    ReceiverState,
    ReceiverStateStore,
)
from cli_agent_orchestrator.kernel.receiver_state.trace_manifest import (
    CONSUMER_MODULES,
    generate_manifest,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services import receiver_state_view, seam_activation


@pytest.fixture
def seam_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'seam.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(seam_activation, "SessionLocal", sessions)
    with sessions() as db:
        for consumer_op in database.SEAM_ACTIVATION_CONSUMER_OPS:
            db.add(
                database.SeamActivationModel(
                    consumer_op=consumer_op,
                    active_authority="legacy",
                    updated_at="2026-07-19T00:00:00+00:00",
                )
            )
        db.commit()
    yield sessions
    engine.dispose()


def test_activation_bootstrap_rows_are_legacy_by_default(seam_db) -> None:
    with seam_db() as db:
        rows = db.query(database.SeamActivationModel).all()
    assert len(rows) == 9
    assert {row.active_authority for row in rows} == {"legacy"}
    assert {row.active_version for row in rows} == {0}
    assert {row.acceptance_token for row in rows} == {None}


def test_accept_promote_rollback_reaccept_and_duplicate_history(seam_db) -> None:
    accepted = seam_activation.accept("watchdog.cached_status", "e1")
    assert isinstance(accepted, seam_activation.Accepted)
    assert isinstance(
        seam_activation.promote("watchdog.cached_status", accepted.acceptance_token),
        seam_activation.Promoted,
    )
    assert isinstance(
        seam_activation.rollback("watchdog.cached_status", 1), seam_activation.RolledBack
    )

    accepted_e2 = seam_activation.accept("watchdog.cached_status", "e2")
    assert isinstance(accepted_e2, seam_activation.Accepted)
    assert isinstance(
        seam_activation.promote("watchdog.cached_status", accepted_e2.acceptance_token),
        seam_activation.Promoted,
    )
    assert isinstance(
        seam_activation.rollback("watchdog.cached_status", 2), seam_activation.RolledBack
    )

    replay = seam_activation.accept("watchdog.cached_status", "e1")
    assert isinstance(replay, seam_activation.DuplicateEvidence)

    with seam_db() as db:
        evidence = db.query(database.SeamActivationEvidenceModel).all()
        row = db.get(database.SeamActivationModel, "watchdog.cached_status")
    assert len(evidence) == 2
    assert row.active_authority == "legacy"
    assert row.rollback_version == 2


def test_stale_token_aba_promotion_is_rejected(seam_db) -> None:
    first = seam_activation.accept("agent_step.status_reads", "first")
    assert isinstance(first, seam_activation.Accepted)
    assert isinstance(
        seam_activation.rollback("agent_step.status_reads", 0), seam_activation.RollbackConflict
    )
    assert isinstance(
        seam_activation.promote("agent_step.status_reads", first.acceptance_token),
        seam_activation.Promoted,
    )
    assert isinstance(
        seam_activation.rollback("agent_step.status_reads", 1), seam_activation.RolledBack
    )
    second = seam_activation.accept("agent_step.status_reads", "second")
    assert isinstance(second, seam_activation.Accepted)
    assert isinstance(
        seam_activation.promote("agent_step.status_reads", first.acceptance_token),
        seam_activation.PromotionConflict,
    )
    assert isinstance(
        seam_activation.promote("agent_step.status_reads", second.acceptance_token),
        seam_activation.Promoted,
    )


def test_concurrent_accept_and_promote_are_single_winner(seam_db) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda ref: seam_activation.accept("delivery.admission_status", ref),
                ("race-a", "race-b"),
            )
        )
    assert sum(isinstance(result, seam_activation.Accepted) for result in results) == 1
    accepted = next(result for result in results if isinstance(result, seam_activation.Accepted))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        promoted = list(
            pool.map(
                lambda _index: seam_activation.promote(
                    "delivery.admission_status", accepted.acceptance_token
                ),
                (1, 2),
            )
        )
    assert sum(isinstance(result, seam_activation.Promoted) for result in promoted) == 1


def test_schema_rejects_illegal_activation_states(seam_db) -> None:
    with seam_db() as db:
        db.add(
            database.SeamActivationModel(
                consumer_op="illegal-receiver-zero",
                active_authority="receiver_state",
                active_version=0,
                accepted_version=0,
                updated_at="now",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(
            database.SeamActivationModel(
                consumer_op="illegal-null-token",
                active_authority="legacy",
                active_version=0,
                accepted_version=1,
                acceptance_token=None,
                updated_at="now",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_activation_read_outage_fails_closed_and_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(seam_activation, "SessionLocal", MagicMock(side_effect=OSError("db")))
    caplog.set_level("WARNING", logger="cli_agent_orchestrator.services.seam_activation")
    assert seam_activation.receiver_state_active("watchdog.cached_status") is False
    assert seam_activation.receiver_state_active("watchdog.cached_status") is False
    assert caplog.text.count("using legacy authority") == 1


def _fake_monitor(store: object) -> SimpleNamespace:
    return SimpleNamespace(
        receiver_state_store=store,
        get_status=MagicMock(return_value=TerminalStatus.COMPLETED),
        get_raw_status=MagicMock(return_value=TerminalStatus.PROCESSING),
        probe_screen_status=MagicMock(return_value=(TerminalStatus.IDLE, {})),
    )


def test_event_inbox_bypasses_activation_and_store(monkeypatch) -> None:
    store = MagicMock()
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: True),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", MagicMock(return_value=True))
    assert (
        receiver_state_view.snapshot_view(
            "watchdog.cached_status", "t1", max_age_s=30.0, none_behavior="none", monitor=monitor
        )
        is TerminalStatus.COMPLETED
    )
    store.snapshot_view.assert_not_called()
    receiver_state_view.receiver_state_active.assert_not_called()


def test_legacy_default_does_not_touch_store(monkeypatch) -> None:
    store = MagicMock()
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: False)
    assert (
        receiver_state_view.snapshot_view(
            "agent_step.status_reads", "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
        )
        is TerminalStatus.COMPLETED
    )
    store.snapshot_view.assert_not_called()


def test_recovery_metadata_exception_falls_back_to_raw(monkeypatch) -> None:
    store = MagicMock()
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view, "get_terminal_metadata", MagicMock(side_effect=OSError("db"))
    )
    assert (
        receiver_state_view.snapshot_view(
            "agent_step.status_reads", "t1", max_age_s=10.0, none_behavior="none", monitor=monitor
        )
        is TerminalStatus.PROCESSING
    )
    store.snapshot_view.assert_not_called()


def test_recovery_metadata_is_applied_by_view_adapter(monkeypatch) -> None:
    store = ReceiverStateStore()
    store.publish_observation(
        ReceiverState(
            terminal_id="t1",
            lifecycle_generation=1,
            window_identity="w",
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
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _id: {
            "tmux_window": "w",
            "lifecycle_generation": 1,
            "recovery_state": "rebind_starting",
        },
    )
    assert (
        receiver_state_view.snapshot_view(
            "agent_step.status_reads", "t1", max_age_s=10.0, none_behavior="none", monitor=monitor
        )
        is TerminalStatus.ERROR
    )


def test_watchdog_none_path_probes_then_uses_legacy_status(monkeypatch) -> None:
    store = MagicMock()
    store.snapshot_view.return_value = None
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _id: {"tmux_window": "w", "lifecycle_generation": 1, "recovery_state": None},
    )
    assert (
        receiver_state_view.snapshot_view(
            "watchdog.cached_status",
            "t1",
            max_age_s=30.0,
            none_behavior="watchdog",
            monitor=monitor,
        )
        is TerminalStatus.COMPLETED
    )
    monitor.probe_screen_status.assert_called_once_with("t1")
    monitor.get_status.assert_called_once_with("t1")


def test_delivery_none_path_defers_without_legacy_fallback(monkeypatch) -> None:
    store = MagicMock()
    store.snapshot_view.return_value = None
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _id: {"tmux_window": "w", "lifecycle_generation": 1, "recovery_state": None},
    )
    assert (
        receiver_state_view.snapshot_view(
            "delivery.admission_status", "t1", max_age_s=5.0, none_behavior="none", monitor=monitor
        )
        is None
    )
    monitor.get_status.assert_not_called()


def test_flipped_processing_read_rechecks_legacy_unstick_side_effect(monkeypatch) -> None:
    store = ReceiverStateStore()
    now_mono = time.monotonic()
    common = dict(
        terminal_id="t1",
        lifecycle_generation=1,
        window_identity="w",
        observation_epoch="11111111-1111-1111-1111-111111111111",
        provider="codex",
        frame_hash=None,
    )
    store.publish_observation(
        ReceiverState(
            **common,
            observation_sequence=1,
            frame_source="incremental",
            captured_at_mono=now_mono,
            latched_status=TerminalStatus.PROCESSING,
            pass_outcome="accepted",
            freshness_proof=FreshnessProof("not_probed"),
        )
    )
    monitor = _fake_monitor(store)

    def unstick(_terminal_id: str):
        store.publish_observation(
            ReceiverState(
                **common,
                observation_sequence=2,
                frame_source="incremental",
                captured_at_mono=time.monotonic(),
                latched_status=TerminalStatus.IDLE,
                pass_outcome="accepted",
                freshness_proof=FreshnessProof("not_probed"),
            )
        )
        return TerminalStatus.IDLE

    monitor.get_raw_status.side_effect = unstick
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _id: {"tmux_window": "w", "lifecycle_generation": 1, "recovery_state": None},
    )

    assert (
        receiver_state_view.snapshot_view(
            "agent_step.status_reads", "t1", max_age_s=10.0, none_behavior="none", monitor=monitor
        )
        is TerminalStatus.IDLE
    )
    monitor.get_raw_status.assert_called_once_with("t1")


def test_backend_failure_warning_is_rate_limited_per_terminal(monkeypatch, caplog) -> None:
    receiver_state_view._backend_failure_last_logged.clear()
    clock = iter((0.0, 10.0, 61.0))
    monkeypatch.setattr(receiver_state_view.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        receiver_state_view, "get_backend", MagicMock(side_effect=OSError("backend"))
    )
    monitor = _fake_monitor(MagicMock())
    caplog.set_level("WARNING", logger="cli_agent_orchestrator.services.receiver_state_view")

    for _ in range(3):
        receiver_state_view.snapshot_view(
            "agent_step.status_reads", "t1", max_age_s=10.0, none_behavior="none", monitor=monitor
        )

    assert caplog.text.count("backend check failed") == 2


def test_named_capability_sites_read_provider_descriptor() -> None:
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator" / "services"
    for filename, owner in (
        ("auto_responder.py", "on_screen"),
        ("inbox_service.py", "deliver_pending"),
    ):
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        function_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == owner
        ]
        assert function_nodes, filename
        attrs = [
            node
            for node in ast.walk(function_nodes[0])
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "provider"
            and node.attr == "capabilities"
        ]
        assert attrs, filename


def test_view_reads_incremental_slot_only(monkeypatch) -> None:
    store = ReceiverStateStore()
    now_mono = time.monotonic()
    common = dict(
        terminal_id="t1",
        lifecycle_generation=1,
        window_identity="w",
        observation_epoch="11111111-1111-1111-1111-111111111111",
        provider="codex",
        frame_hash=None,
        freshness_proof=FreshnessProof("not_probed"),
    )
    store.publish_observation(
        ReceiverState(
            **common,
            observation_sequence=1,
            frame_source="incremental",
            captured_at_mono=now_mono,
            latched_status=TerminalStatus.IDLE,
            pass_outcome="accepted",
        )
    )
    store.publish_observation(
        ReceiverState(
            **common,
            observation_sequence=2,
            frame_source="fresh_capture",
            captured_at_mono=now_mono + 1.0,
            latched_status=TerminalStatus.PROCESSING,
            pass_outcome="probe",
        )
    )
    monitor = _fake_monitor(store)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(receiver_state_view, "receiver_state_active", lambda _op: True)
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _id: {"tmux_window": "w", "lifecycle_generation": 1, "recovery_state": None},
    )
    assert (
        receiver_state_view.snapshot_view(
            "watchdog.cached_status", "t1", max_age_s=30.0, none_behavior="none", monitor=monitor
        )
        is TerminalStatus.IDLE
    )


def test_trace_manifest_is_byte_exact_and_has_37_hits() -> None:
    manifest_path = (
        Path(__file__).parents[2]
        / "src/cli_agent_orchestrator/kernel/receiver_state/trace_manifest.txt"
    )
    expected = manifest_path.read_text(encoding="utf-8")
    assert generate_manifest(Path(__file__).parents[2]) == expected
    assert len([line for line in expected.splitlines() if line]) == 37


def test_trace_manifest_matches_bare_name_calls(tmp_path) -> None:
    for index, relative_path in enumerate(CONSUMER_MODULES):
        source_path = tmp_path / relative_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            'get_status("t1")\n' if index == 0 else "pass\n",
            encoding="utf-8",
        )

    assert generate_manifest(tmp_path) == (
        "src/cli_agent_orchestrator/services/agent_step.py:1:get_status\n"
    )


def test_provider_capabilities_descriptor_is_closed() -> None:
    capabilities = ProviderCapabilities(
        supports_screen_detection=True,
        accepts_input_while_processing=True,
        paste_enter_count=2,
    )
    assert capabilities == ProviderCapabilities(True, True, 2)
    assert capabilities.paste_enter_count == 2

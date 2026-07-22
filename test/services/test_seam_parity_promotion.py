"""WP-SEAM-PARITY T1-T6 acceptance coverage."""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import receiver_state_view, seam_activation, seam_parity
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def parity_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'parity.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(seam_activation, "SessionLocal", sessions)
    monkeypatch.setattr(seam_parity, "SessionLocal", sessions)
    monkeypatch.setattr(seam_parity, "SEAM_PARITY_POISON_DIR", tmp_path / "poison")
    seam_parity._clean_buffer.clear()
    seam_parity._clean_buffer_started.clear()
    seam_parity._promotion_inhibited.clear()
    seam_parity._build_id_cache = "build-a"
    now = datetime.now(timezone.utc).isoformat()
    with sessions() as db:
        for op in database.SEAM_ACTIVATION_CONSUMER_OPS:
            db.add(
                database.SeamActivationModel(
                    consumer_op=op,
                    active_authority="legacy",
                    updated_at=now,
                )
            )
        for op in seam_parity.PARITY_CONSUMER_OPS:
            db.add(
                database.SeamParityModel(
                    consumer_op=op,
                    build_id="build-a",
                    phase="collecting",
                    window_started_at=now,
                    window_nonce=f"nonce-{op}",
                )
            )
        db.commit()
    yield sessions
    engine.dispose()


def _monitor(rs_status: TerminalStatus | None, legacy: TerminalStatus):
    store = MagicMock()
    store.snapshot_view.return_value = (
        None
        if rs_status is None
        else SimpleNamespace(latched_status=rs_status, origin="incremental")
    )
    return SimpleNamespace(
        receiver_state_store=store,
        get_status=MagicMock(return_value=legacy),
        get_raw_status=MagicMock(return_value=legacy),
        probe_screen_status=MagicMock(),
    )


def _receiver_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: False),
    )
    monkeypatch.setattr(
        receiver_state_view,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "tmux_window": "w",
            "lifecycle_generation": 1,
            "recovery_state": None,
        },
    )


def _set_phase(sessions, op: str, authority: str, phase: str, build_id: str = "build-a"):
    with sessions() as db:
        activation = db.get(database.SeamActivationModel, op)
        activation.active_authority = authority
        if authority == "receiver_state":
            activation.active_version = 1
            activation.accepted_version = 1
        else:
            activation.active_version = 0
            activation.accepted_version = 0
        parity = db.get(database.SeamParityModel, op)
        parity.phase = phase
        parity.build_id = build_id
        parity.window_nonce = f"{authority}-{phase}-{build_id}"
        parity.clean_samples = 0
        db.commit()


def test_t1_collecting_match_mismatch_unavailable_and_resolver_provenance(
    parity_db, monkeypatch
) -> None:
    _receiver_patches(monkeypatch)
    op = "agent_step.status_reads"
    monitor = _monitor(TerminalStatus.COMPLETED, TerminalStatus.COMPLETED)

    resolved = receiver_state_view.resolve_rs_answer(
        "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
    )
    assert resolved == receiver_state_view.ResolvedRSAnswer(TerminalStatus.COMPLETED, True)
    assert (
        receiver_state_view.snapshot_view(
            op, "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
        )
        is TerminalStatus.COMPLETED
    )
    seam_parity.flush_clean_samples()
    with parity_db() as db:
        assert db.get(database.SeamParityModel, op).clean_samples == 1

    monitor.receiver_state_store.snapshot_view.return_value = None
    unavailable = receiver_state_view.resolve_rs_answer(
        "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
    )
    assert unavailable == receiver_state_view.ResolvedRSAnswer(TerminalStatus.COMPLETED, False)
    assert (
        receiver_state_view.snapshot_view(
            op, "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
        )
        is TerminalStatus.COMPLETED
    )
    seam_parity.flush_clean_samples()
    with parity_db() as db:
        assert db.get(database.SeamParityModel, op).clean_samples == 1

    monitor.receiver_state_store.snapshot_view.return_value = SimpleNamespace(
        latched_status=TerminalStatus.IDLE,
        origin="incremental",
    )
    old_nonce = seam_parity.parity_state(op).window_nonce
    assert (
        receiver_state_view.snapshot_view(
            op, "t1", max_age_s=10.0, none_behavior="legacy", monitor=monitor
        )
        is TerminalStatus.COMPLETED
    )
    with parity_db() as db:
        row = db.get(database.SeamParityModel, op)
        history = db.query(database.SeamParityMismatchModel).all()
    assert row.window_nonce != old_nonce
    assert row.clean_samples == 0
    assert len(history) == 1
    assert not (seam_parity.SEAM_PARITY_POISON_DIR / op).exists()


def test_t1_comparator_exception_does_not_perturb_legacy(parity_db, monkeypatch) -> None:
    _receiver_patches(monkeypatch)
    monitor = _monitor(TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    monkeypatch.setattr(
        receiver_state_view,
        "resolve_rs_answer",
        MagicMock(side_effect=RuntimeError("shadow failed")),
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


def test_t1_confirmation_roles_swap_and_mismatch_rolls_back(parity_db, monkeypatch) -> None:
    _receiver_patches(monkeypatch)
    op = "watchdog.cached_status"
    _set_phase(parity_db, op, "receiver_state", "confirming")
    monitor = _monitor(TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    assert (
        receiver_state_view.snapshot_view(
            op, "t1", max_age_s=30.0, none_behavior="watchdog", monitor=monitor
        )
        is TerminalStatus.IDLE
    )
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        parity = db.get(database.SeamParityModel, op)
        mismatch = db.query(database.SeamParityMismatchModel).one()
    assert activation.active_authority == "legacy"
    assert parity.phase == "collecting"
    assert mismatch.acted_answer == "idle"
    assert mismatch.shadow_answer == "completed"


@pytest.mark.parametrize(
    ("authority", "phase", "expected"),
    [
        ("legacy", "collecting", "collecting"),
        ("legacy", "confirming", "collecting"),
        ("legacy", "done", "collecting"),
        ("receiver_state", "collecting", "confirming"),
        ("receiver_state", "confirming", "confirming"),
        ("receiver_state", "done", "done"),
    ],
)
def test_t2_startup_repair_full_authority_phase_matrix(
    parity_db, authority, phase, expected
) -> None:
    op = "watchdog.cached_status"
    _set_phase(parity_db, op, authority, phase)
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        before = (
            activation.active_authority,
            activation.accepted_version,
            activation.active_version,
            activation.rollback_version,
            activation.acceptance_token,
        )
    seam_parity.startup_repair()
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        parity = db.get(database.SeamParityModel, op)
        after = (
            activation.active_authority,
            activation.accepted_version,
            activation.active_version,
            activation.rollback_version,
            activation.acceptance_token,
        )
    assert parity.phase == expected
    assert before == after


def test_t2_missing_row_and_build_change_done_reopen_confirmation(parity_db) -> None:
    missing = "delivery.admission_status"
    with parity_db() as db:
        db.delete(db.get(database.SeamParityModel, missing))
        db.commit()
    op = "watchdog.cached_status"
    _set_phase(parity_db, op, "receiver_state", "done", build_id="old-build")
    seam_parity.startup_repair()
    with parity_db() as db:
        inserted = db.get(database.SeamParityModel, missing)
        reopened = db.get(database.SeamParityModel, op)
        activation = db.get(database.SeamActivationModel, op)
    assert inserted.phase == "collecting"
    assert reopened.phase == "confirming"
    assert reopened.build_id == "build-a"
    assert activation.active_authority == "receiver_state"


def test_t2_epoch_fence_drops_buffered_clean_after_mismatch(parity_db) -> None:
    op = "watchdog.cached_status"
    assert (
        seam_parity.record_comparison(
            op,
            "collecting",
            TerminalStatus.IDLE,
            TerminalStatus.IDLE,
            rs_sourced=True,
        )
        == "match"
    )
    old_key = next(iter(seam_parity._clean_buffer))
    assert (
        seam_parity.record_comparison(
            op,
            "collecting",
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
            rs_sourced=True,
        )
        == "mismatch"
    )
    seam_parity._clean_buffer[old_key] = 100
    seam_parity.flush_clean_samples()
    with parity_db() as db:
        row = db.get(database.SeamParityModel, op)
    assert row.clean_samples == 0


def test_t3_sweep_atomically_promotes_and_records_unique_evidence(parity_db, monkeypatch) -> None:
    op = "watchdog.cached_status"
    monkeypatch.setattr(seam_parity, "_thresholds", lambda: seam_parity.ParityThresholds(1, 0))
    with parity_db() as db:
        row = db.get(database.SeamParityModel, op)
        row.clean_samples = 1
        row.window_started_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        nonce = row.window_nonce
        db.commit()
    seam_parity.sweep()
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        parity = db.get(database.SeamParityModel, op)
        evidence = db.query(database.SeamActivationEvidenceModel).one()
    assert activation.active_authority == "receiver_state"
    assert parity.phase == "confirming"
    assert evidence.evidence_ref == f"parity:build-a:1:{nonce}"


def test_t3_composite_normalizes_orphan_and_rolls_back_on_injected_transaction(
    parity_db,
) -> None:
    op = "agent_step.status_reads"
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        activation.accepted_version = 1
        activation.acceptance_token = "orphan"
        db.commit()
    assert isinstance(
        seam_activation.promote_with_evidence(op, "orphan-evidence"), seam_activation.Promoted
    )

    other = "watchdog.ready_backlog_gate"
    with parity_db() as db:
        result = seam_activation._promote_with_evidence_in(db, other, "rolled-back")
        assert isinstance(result, seam_activation.Promoted)
        db.rollback()
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, other)
        parity = db.get(database.SeamParityModel, other)
    assert activation.active_authority == "legacy"
    assert activation.accepted_version == 0
    assert parity.phase == "collecting"


def test_t4_clean_confirmation_stops_dual_read(parity_db, monkeypatch) -> None:
    op = "watchdog.cached_status"
    _set_phase(parity_db, op, "receiver_state", "confirming")
    monkeypatch.setattr(seam_parity, "_thresholds", lambda: seam_parity.ParityThresholds(1, 0))
    with parity_db() as db:
        row = db.get(database.SeamParityModel, op)
        row.clean_samples = 1
        db.commit()
    seam_parity.sweep()
    with parity_db() as db:
        assert db.get(database.SeamParityModel, op).phase == "done"

    _receiver_patches(monkeypatch)
    monitor = _monitor(TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    assert (
        receiver_state_view.snapshot_view(
            op, "t1", max_age_s=30.0, none_behavior="watchdog", monitor=monitor
        )
        is TerminalStatus.IDLE
    )
    monitor.get_status.assert_not_called()


def test_t5_event_inbox_and_out_of_scope_bypass_comparator(parity_db, monkeypatch) -> None:
    monitor = _monitor(TerminalStatus.IDLE, TerminalStatus.COMPLETED)
    monkeypatch.setattr(
        receiver_state_view,
        "get_backend",
        lambda: SimpleNamespace(supports_event_inbox=lambda: True),
    )
    sample = MagicMock()
    monkeypatch.setattr(seam_parity, "record_comparison", sample)
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
    assert (
        receiver_state_view.snapshot_view(
            "watchdog.pane_classify",
            "t1",
            max_age_s=30.0,
            none_behavior="watchdog",
            monitor=monitor,
        )
        is TerminalStatus.COMPLETED
    )
    sample.assert_not_called()


def test_t5_direct_production_admission_site_uses_phase_answer(parity_db, monkeypatch) -> None:
    database.create_terminal("sender", "session", "sender", "codex")
    database.create_terminal("receiver", "session", "receiver", "grok_cli")
    database.create_inbox_message("sender", "receiver", "work")
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    phase_view = MagicMock(return_value=TerminalStatus.PROCESSING)
    provider = MagicMock()
    provider.capabilities.accepts_input_while_processing = False
    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch.object(receiver_state_view, "view_from_legacy", phase_view),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as paste,
    ):
        InboxService().deliver_pending("receiver")
    phase_view.assert_called_once_with(
        "delivery.admission_status",
        "receiver",
        TerminalStatus.IDLE,
        max_age_s=5.0,
        none_behavior="none",
        monitor=monitor,
    )
    paste.assert_not_called()


def test_t5b_poison_recovery_is_idempotent_and_inhibits(parity_db) -> None:
    op = "watchdog.cached_status"
    record = seam_parity.MismatchRecord(
        consumer_op=op,
        build_id="build-a",
        window_nonce=seam_parity.parity_state(op).window_nonce,
        phase="collecting",
        acted_answer="idle",
        shadow_answer="processing",
        detail="acted=idle shadow=processing",
        created_at="2026-07-22T00:00:00+00:00",
    )
    seam_parity._write_poison(record)
    seam_parity.startup_repair()
    seam_parity._write_poison(record)
    seam_parity.startup_repair()
    with parity_db() as db:
        rows = db.query(database.SeamParityMismatchModel).all()
        parity = db.get(database.SeamParityModel, op)
    assert len(rows) == 1
    assert rows[0].source == "poison_recovery"
    assert parity.phase == "collecting"
    assert op in seam_parity._promotion_inhibited
    assert not (seam_parity.SEAM_PARITY_POISON_DIR / op).exists()


def test_t5b_db_failure_leaves_marker_and_latch(parity_db, monkeypatch) -> None:
    op = "watchdog.cached_status"
    monkeypatch.setattr(seam_parity, "_persist_collecting_mismatch", lambda _record: False)
    assert (
        seam_parity.record_comparison(
            op,
            "collecting",
            TerminalStatus.IDLE,
            TerminalStatus.PROCESSING,
            rs_sourced=True,
        )
        == "mismatch"
    )
    assert (seam_parity.SEAM_PARITY_POISON_DIR / op).exists()
    assert op in seam_parity._promotion_inhibited


def test_t5b_marker_failure_latches_but_still_persists(parity_db, monkeypatch) -> None:
    op = "watchdog.cached_status"
    monkeypatch.setattr(seam_parity, "_write_poison", MagicMock(side_effect=OSError("disk")))
    seam_parity.record_comparison(
        op,
        "collecting",
        TerminalStatus.IDLE,
        TerminalStatus.PROCESSING,
        rs_sourced=True,
    )
    with parity_db() as db:
        assert db.query(database.SeamParityMismatchModel).count() == 1
    assert op in seam_parity._promotion_inhibited


def test_t5c_unknown_build_confirming_clean_holds_but_mismatch_demotes(
    parity_db, monkeypatch
) -> None:
    op = "watchdog.cached_status"
    seam_parity._build_id_cache = "unknown"
    _set_phase(parity_db, op, "receiver_state", "confirming", build_id="unknown")
    assert (
        seam_parity.record_comparison(
            op,
            "confirming",
            TerminalStatus.IDLE,
            TerminalStatus.IDLE,
            rs_sourced=True,
        )
        == "match"
    )
    seam_parity.flush_clean_samples()
    seam_parity.sweep()
    with parity_db() as db:
        assert db.get(database.SeamParityModel, op).clean_samples == 0
        assert db.get(database.SeamParityModel, op).phase == "confirming"
    seam_parity.record_comparison(
        op,
        "confirming",
        TerminalStatus.IDLE,
        TerminalStatus.PROCESSING,
        rs_sourced=True,
    )
    with parity_db() as db:
        activation = db.get(database.SeamActivationModel, op)
        mismatch = db.query(database.SeamParityMismatchModel).one()
    assert activation.active_authority == "legacy"
    assert mismatch.build_id == "unknown"


def test_t5c_build_identity_module_surface_is_exact() -> None:
    assert set(seam_parity.BUILD_IDENTITY_MODULES) == {
        "services/receiver_state_view.py",
        "services/seam_activation.py",
        "services/status_monitor.py",
        "services/agent_step.py",
        "services/inbox_service.py",
        "services/stalled_callback_watchdog.py",
        "clients/database.py",
        "api/main.py",
        "services/seam_parity.py",
    }


@pytest.mark.asyncio
async def test_t5d_watchdog_sweep_first_deferred_and_event_storm_capped(monkeypatch) -> None:
    watchdog = StalledCallbackWatchdog()
    watchdog._parity_clock = MagicMock(side_effect=[0.0, 59.0, 60.0, 60.1, 119.9, 120.0])
    event = {"topic": "terminal.t.status", "data": {"status": "idle"}}
    queue = SimpleNamespace(
        get=AsyncMock(side_effect=[event, event, event, event, asyncio.CancelledError()])
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.stalled_callback_watchdog.bus.subscribe",
        lambda _topic: queue,
    )
    for name in (
        "record_status",
        "poll_unarmed_statuses",
        "refresh_screen_fingerprints",
        "notify_due",
        "tick_waiting_inbox",
        "tick_ready_backlog",
    ):
        monkeypatch.setattr(watchdog, name, MagicMock())
    sweep = MagicMock()
    monkeypatch.setattr(seam_parity, "sweep", sweep)
    with pytest.raises(asyncio.CancelledError):
        await watchdog.run()
    assert sweep.call_count == 1


@pytest.mark.asyncio
async def test_t5e_startup_repair_failure_aborts_before_consumer_tasks(monkeypatch) -> None:
    from cli_agent_orchestrator.api import main

    monkeypatch.setattr(main, "setup_logging", MagicMock())
    monkeypatch.setattr(main, "install_access_log_redaction", MagicMock())
    monkeypatch.setattr(main, "init_telemetry", MagicMock())
    monkeypatch.setattr(main, "is_sandbox", lambda: False)
    monkeypatch.setattr(main, "init_db", MagicMock())
    monkeypatch.setattr(main.seam_parity, "startup_repair", MagicMock(side_effect=OSError("db")))
    status_run = MagicMock()
    monkeypatch.setattr(main.status_monitor, "run", status_run)
    with pytest.raises(OSError, match="db"):
        async with main.lifespan(main.app):
            pass
    status_run.assert_not_called()


def test_t6_cli_status_rollback_reset_and_no_promote(parity_db) -> None:
    runner = CliRunner()
    status = runner.invoke(cli, ["seam", "status", "--json"])
    assert status.exit_code == 0
    assert len(json.loads(status.output)) == 5
    help_result = runner.invoke(cli, ["seam", "--help"])
    assert help_result.exit_code == 0
    assert "promote" not in help_result.output
    reset = runner.invoke(cli, ["seam", "reset", "watchdog.cached_status"])
    assert reset.exit_code == 0
    conflict = runner.invoke(cli, ["seam", "rollback", "watchdog.cached_status"])
    assert conflict.exit_code == 1


def test_t5_source_wiring_calls_direct_comparator_and_startup_before_tasks() -> None:
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator"
    inbox_tree = ast.parse((root / "services/inbox_service.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(inbox_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "view_from_legacy"
    ]
    assert calls
    main_text = (root / "api/main.py").read_text(encoding="utf-8")
    assert main_text.index("seam_parity.startup_repair()") < main_text.index(
        "asyncio.create_task(status_monitor.run())"
    )

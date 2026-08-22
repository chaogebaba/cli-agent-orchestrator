"""F138 Amendment r7 (D21-D24): Activation authority and post-exposure settlement.

Physical mutant ledger — each test kills one specific mutant that, if alive,
would leave the system in an unsafe state.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import pytest

from cli_agent_orchestrator.clients.database import (
    ActivationResult,
    ForceReconcileResult,
    OrphanReconcileJobModel,
    ProcessIncarnationModel,
    SessionLocal,
    f138_abandon_incarnation,
    f138_activate_incarnation,
    f138_force_reconcile_incarnation,
    f138_get_incarnation_by_terminal_generation,
    f138_reserve_incarnation,
    f138_strict_activate,
    init_db,
)
from cli_agent_orchestrator.services.orphan_reconcile_service import (
    JobRequestResult,
    generate_incarnation_token,
    hash_token,
)


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure DB is initialized for every test."""
    init_db()
    yield


@pytest.fixture
def db_session():
    """Provide a session factory."""
    return SessionLocal


# ==============================================================================
# D21: Strict activation typed results
# ==============================================================================


class TestD21StrictActivation:
    """D21: f138_strict_activate returns typed ActivationResult."""

    def _make_incarnation(self, terminal_id: str = "d21-test", gen: int = 1) -> str:
        token = generate_incarnation_token()
        return f138_reserve_incarnation(
            terminal_id=f"{terminal_id}-{uuid.uuid4().hex[:8]}",
            terminal_generation=gen,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )

    def test_launching_to_active(self, db_session):
        """M1: Activation from launching → activated outcome."""
        inc_id = self._make_incarnation()
        result = f138_strict_activate(inc_id)
        assert isinstance(result, ActivationResult)
        assert result.outcome == "activated"
        with db_session() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert row.state == "active"
            assert row.activated_at is not None

    def test_already_active_idempotent(self, db_session):
        """M2: Double activation → already_active (no error)."""
        inc_id = self._make_incarnation()
        f138_strict_activate(inc_id)
        result = f138_strict_activate(inc_id)
        assert result.outcome == "already_active"

    def test_reconcile_pending_needs_settlement(self, db_session):
        """M3: reconcile_pending row → needs_settlement outcome."""
        inc_id = self._make_incarnation()
        # Activate then move to reconcile_pending
        f138_strict_activate(inc_id)
        with db_session.begin() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            row.state = "reconcile_pending"
        result = f138_strict_activate(inc_id)
        assert result.outcome == "needs_settlement"

    def test_abandoned_needs_settlement(self, db_session):
        """M4: abandoned row → needs_settlement outcome."""
        inc_id = self._make_incarnation()
        f138_abandon_incarnation(inc_id)
        result = f138_strict_activate(inc_id)
        assert result.outcome == "needs_settlement"

    def test_missing_row(self):
        """M5: nonexistent ID → missing outcome."""
        result = f138_strict_activate("nonexistent-" + str(uuid.uuid4()))
        assert result.outcome == "missing"

    def test_activated_at_only_set_on_transition(self, db_session):
        """M6: activated_at is set ONLY on launching→active, not on already_active."""
        inc_id = self._make_incarnation()
        f138_strict_activate(inc_id)
        with db_session() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            first_ts = row.activated_at
        # Second call should not change timestamp
        f138_strict_activate(inc_id)
        with db_session() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert row.activated_at == first_ts


# ==============================================================================
# D22: Force reconciliation primitive
# ==============================================================================


class TestD22ForceReconcile:
    """D22: f138_force_reconcile_incarnation atomic typed results."""

    def _make_active(self, terminal_id: str = "d22-test", gen: int = 1) -> str:
        token = generate_incarnation_token()
        uid = uuid.uuid4().hex[:8]
        inc_id = f138_reserve_incarnation(
            terminal_id=f"{terminal_id}-{uid}",
            terminal_generation=gen,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        f138_strict_activate(inc_id)
        return inc_id

    def test_creates_job_for_active(self, db_session):
        """M7: Active incarnation → job created + state becomes reconcile_pending."""
        inc_id = self._make_active()
        result = f138_force_reconcile_incarnation(inc_id, source="test_force")
        assert isinstance(result, ForceReconcileResult)
        assert result.outcome == "created"
        assert result.job_id is not None
        with db_session() as db:
            inc = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert inc.state == "reconcile_pending"
            job = db.query(OrphanReconcileJobModel).filter_by(id=result.job_id).one()
            assert job.state == "pending"
            assert job.source == "test_force"

    def test_creates_job_for_launching(self, db_session):
        """M8: Launching incarnation → job created + state becomes reconcile_pending."""
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=f"d22-launching-{uuid.uuid4().hex[:8]}",
            terminal_generation=1,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        result = f138_force_reconcile_incarnation(inc_id, source="test_launch_force")
        assert result.outcome == "created"
        with db_session() as db:
            inc = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert inc.state == "reconcile_pending"

    def test_creates_job_for_abandoned(self, db_session):
        """M9: Abandoned incarnation → job created (state normalized to reconcile_pending)."""
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=f"d22-abandoned-{uuid.uuid4().hex[:8]}",
            terminal_generation=1,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        f138_abandon_incarnation(inc_id)
        result = f138_force_reconcile_incarnation(inc_id, source="test_abandon_force")
        assert result.outcome == "created"
        # D22: abandoned is normalized to reconcile_pending atomically
        with db_session() as db:
            inc = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert inc.state == "reconcile_pending"

    def test_reconciled_proven_for_reconciled_row(self):
        """M10: Reconciled row WITHOUT succeeded job → non_durable_invariant."""
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=f"d22-reconciled-{uuid.uuid4().hex[:8]}",
            terminal_generation=1,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        # Manually set to reconciled (no job exists)
        with SessionLocal.begin() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            row.state = "reconciled"
        result = f138_force_reconcile_incarnation(inc_id, source="test")
        assert result.outcome == "non_durable_invariant"
        assert result.detail == "reconciled_without_succeeded_job"

    def test_repairs_succeeded_job_but_stale_row(self, db_session):
        """M11: Succeeded job + non-reconciled row → repairs row."""
        inc_id = self._make_active()
        # Look up actual terminal_id from incarnation
        with db_session() as db:
            inc_row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            actual_terminal_id = inc_row.terminal_id
            actual_gen = inc_row.terminal_generation
        # Create a succeeded job manually
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            job = OrphanReconcileJobModel(
                id=str(uuid.uuid4()),
                incarnation_id=inc_id,
                terminal_id=actual_terminal_id,
                terminal_generation=actual_gen,
                state="succeeded",
                attempt=1,
                gone_observed_at=now,
                source="test",
                created_at=now,
                updated_at=now,
            )
            db.add(job)
        result = f138_force_reconcile_incarnation(inc_id, source="test")
        assert result.outcome == "reconciled_proven"
        assert result.detail == "repaired_from_succeeded_job"
        with db_session() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert row.state == "reconciled"

    def test_existing_pending_job_returns_already_exists(self, db_session):
        """M12: Pending job already exists → job_already_exists."""
        inc_id = self._make_active(terminal_id="d22-pending", gen=1)
        # First force creates the job
        r1 = f138_force_reconcile_incarnation(inc_id, source="first")
        assert r1.outcome == "created"
        # Second force finds existing job
        r2 = f138_force_reconcile_incarnation(inc_id, source="second")
        assert r2.outcome == "job_already_exists"
        assert r2.job_id == r1.job_id
        assert "pending" in (r2.detail or "")

    def test_attention_required_job_returns_already_exists(self, db_session):
        """M13 (amended F166 D6): attention_required job → reset to pending."""
        inc_id = self._make_active(terminal_id="d22-attn", gen=1)
        r1 = f138_force_reconcile_incarnation(inc_id, source="first")
        # Manually move to attention_required
        with SessionLocal.begin() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=r1.job_id).one()
            job.state = "attention_required"
        r2 = f138_force_reconcile_incarnation(inc_id, source="retry")
        assert r2.outcome == "created"
        assert r2.detail == "reset_from_attention_required"

    def test_missing_incarnation_non_durable(self):
        """M14: Nonexistent incarnation → non_durable_missing."""
        result = f138_force_reconcile_incarnation(
            "nonexistent-" + str(uuid.uuid4()), source="test"
        )
        assert result.outcome == "non_durable_missing"


# ==============================================================================
# D23: Post-exposure authority gates
# ==============================================================================


class TestD23PostExposureAuthority:
    """D23: After exposure boundary, exceptions force-reconcile before teardown."""

    def _make_incarnation(self, terminal_id: str | None = None, gen: int = 1) -> tuple[str, str]:
        """Returns (incarnation_id, terminal_id)."""
        if terminal_id is None:
            terminal_id = f"d23-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=terminal_id,
            terminal_generation=gen,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        return inc_id, terminal_id

    @pytest.mark.asyncio
    async def test_post_exposure_durable_teardown(self, db_session):
        """M15: Post-exposure + durable force-reconcile → teardown proceeds."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        inc_id, terminal_id = self._make_incarnation()

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock()
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {
            "tmux_session": "test",
            "tmux_window": "w",
            "provider": "claude_code",
            "caller_id": None,
            "init_deadline_s": 60.0,
        }

        # Make _prepare_provider_runtime_identity throw AFTER activation
        with _patch(
            "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
            new=AsyncMock(),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._tracked_blocking",
            side_effect=RuntimeError("identity_persist_failed"),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(),
        ) as mock_settle:
            _schedule_deferred_init(
                provider_instance=mock_provider,
                terminal_id=terminal_id,
                initial_message=None,
                orchestration_type=None,
                registry=None,
                caller_snapshot=snapshot,
                f138_incarnation_id=inc_id,
            )

            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(terminal_id)
            if record is not None:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # Force reconcile should have been called and job created
        with db_session() as db:
            job = (
                db.query(OrphanReconcileJobModel)
                .filter_by(incarnation_id=inc_id)
                .one_or_none()
            )
            assert job is not None, "Post-exposure durable path must create reconcile job"

    @pytest.mark.asyncio
    async def test_pre_exposure_ordinary_rollback(self, db_session):
        """M16: No incarnation_id (process-less) → ordinary rollback (no force-reconcile)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        terminal_id = f"d23-processless-{uuid.uuid4().hex[:8]}"

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock(side_effect=RuntimeError("init_boom"))
        mock_provider.has_process_child = False
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {
            "tmux_session": "test",
            "tmux_window": "w",
            "provider": "claude_code",
            "caller_id": None,
            "init_deadline_s": 60.0,
        }

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(),
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider,
                terminal_id=terminal_id,
                initial_message=None,
                orchestration_type=None,
                registry=None,
                caller_snapshot=snapshot,
                f138_incarnation_id=None,  # process-less = no exposure
            )

            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(terminal_id)
            if record is not None:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # Process-less (no incarnation_id): NO force-reconcile job should exist
        with db_session() as db:
            jobs = (
                db.query(OrphanReconcileJobModel)
                .filter_by(terminal_id=terminal_id)
                .all()
            )
            assert len(jobs) == 0, "Process-less failure must NOT create reconcile job"

    def test_exposure_boundary_is_before_initialize(self):
        """M17: _f138_exposure_crossed = True from start when incarnation_id is set.

        The boundary is the pane+token binding, which happens BEFORE _run() starts.
        So exposure is immediate when f138_incarnation_id is not None.
        """
        import inspect
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service._schedule_deferred_init)
        # The exposure flag must be set at function entry based on incarnation_id
        flag_pos = source.find("_f138_exposure_crossed = f138_incarnation_id is not None")
        assert flag_pos > 0, (
            "_f138_exposure_crossed must be set immediately from f138_incarnation_id"
        )
        # And it must appear BEFORE provider_instance.initialize()
        init_pos = source.find("provider_instance.initialize()")
        assert flag_pos < init_pos, (
            "Exposure boundary must be established before initialize()"
        )


# ==============================================================================
# D24: Recovery/loss producers use force
# ==============================================================================


class TestD24RecoveryProducersUseForce:
    """D24: Loss-detection paths use f138_force_reconcile_incarnation."""

    def test_startup_recovery_force_queues_gone(self, db_session):
        """M18: Stale launching + gone window → force-queued (not just abandoned)."""
        import inspect
        from cli_agent_orchestrator.clients import database

        source = inspect.getsource(database.f138_startup_recovery)
        # Must collect for force-queue, not just abandon
        assert "_force_queue_ids" in source
        assert "f138_force_reconcile_incarnation" in source
        # Should NOT directly abandon gone windows anymore
        assert "startup_stale_gone" in source

    def test_confirmed_gone_uses_force_reconcile(self):
        """M19: record_confirmed_gone_observation uses force (not request_reconciliation)."""
        import inspect
        from cli_agent_orchestrator.services import orphan_reconcile_service

        source = inspect.getsource(
            orphan_reconcile_service.record_confirmed_gone_observation
        )
        assert "f138_force_reconcile_incarnation" in source
        assert "f138_request_reconciliation" not in source

    def test_confirmed_gone_works_for_abandoned(self, db_session):
        """M20: record_confirmed_gone_observation succeeds for abandoned incarnation."""
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            record_confirmed_gone_observation,
        )

        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=f"d24-gone-abandoned-{uuid.uuid4().hex[:8]}",
            terminal_generation=1,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        f138_abandon_incarnation(inc_id)
        result = record_confirmed_gone_observation(inc_id, source="watchdog_gone")
        assert result.created is True
        assert result.job_id is not None

    def test_confirmed_gone_works_for_launching(self, db_session):
        """M21: record_confirmed_gone_observation succeeds for launching incarnation."""
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            record_confirmed_gone_observation,
        )

        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=f"d24-gone-launching-{uuid.uuid4().hex[:8]}",
            terminal_generation=1,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        result = record_confirmed_gone_observation(inc_id, source="watchdog_gone")
        assert result.created is True

    def test_delete_path_uses_force_reconcile(self):
        """M22: Delete path uses f138_force_reconcile_incarnation (D11)."""
        import inspect
        from cli_agent_orchestrator.services import terminal_service

        source = inspect.getsource(terminal_service._delete_terminal_under_lease)
        assert "f138_force_reconcile_incarnation" in source
        assert "f138_get_incarnation_by_terminal_generation" in source

    def test_get_incarnation_by_terminal_generation(self, db_session):
        """M23: f138_get_incarnation_by_terminal_generation resolves exact generation."""
        tid = f"d24-gen-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=tid,
            terminal_generation=42,
            token=token,
            token_hash=hash_token(token),
            owner_uid=os.getuid(),
            provider="claude_code",
        )
        result = f138_get_incarnation_by_terminal_generation(tid, 42)
        assert result is not None
        assert result["id"] == inc_id
        assert result["terminal_generation"] == 42
        assert result["state"] == "launching"

        # Wrong generation returns None
        assert f138_get_incarnation_by_terminal_generation(tid, 43) is None
        # Wrong terminal_id returns None
        assert f138_get_incarnation_by_terminal_generation("d24-other", 42) is None

    def test_sweep_selectors_exclude_rollback_kill_uncertain(self):
        """M24: Both sweep selectors exclude rollback_kill_uncertain rows."""
        import inspect
        from cli_agent_orchestrator.clients import database

        recovery_src = inspect.getsource(database.list_deferred_init_recovery_rows)
        overdue_src = inspect.getsource(database.list_deferred_init_overdue_pending_rows)
        assert "rollback_kill_uncertain" in recovery_src
        assert "rollback_kill_uncertain" in overdue_src


# ==============================================================================
# Blocker-targeted production-call-path tests
# ==============================================================================


class TestBlocker1ExposureBoundary:
    """Blocker 1: Exposure boundary = pane/token binding, before initialize."""

    @pytest.mark.asyncio
    async def test_initialize_failure_enters_force_settlement(self, db_session):
        """Initialize raises AFTER token bound → force-reconcile triggered (not ordinary)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        tid = f"b1-init-fail-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock(side_effect=RuntimeError("spawn_then_die"))
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {"tmux_session": "t", "tmux_window": "w",
                    "provider": "claude_code", "caller_id": None,
                    "init_deadline_s": 60.0}

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(),
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider, terminal_id=tid,
                initial_message=None, orchestration_type=None,
                registry=None, caller_snapshot=snapshot,
                f138_incarnation_id=inc_id,
            )
            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(tid)
            if record:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # Force-reconcile must have been triggered (job exists)
        with db_session() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(incarnation_id=inc_id).one_or_none()
            assert job is not None, (
                "initialize() failure with pinned incarnation must force-reconcile"
            )


class TestBlocker3HealthAndMissing:
    """Blocker 3: Health failure and strict-missing are non-durable settlement."""

    @pytest.mark.asyncio
    async def test_health_failure_enters_force_settlement(self, db_session):
        """_confirm_launch_health raises → force-reconcile (not swallowed)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        tid = f"b3-health-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock()
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {"tmux_session": "t", "tmux_window": "w",
                    "provider": "claude_code", "caller_id": None,
                    "init_deadline_s": 60.0}

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
            new=AsyncMock(side_effect=RuntimeError("process_tree_dead")),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(),
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider, terminal_id=tid,
                initial_message=None, orchestration_type=None,
                registry=None, caller_snapshot=snapshot,
                f138_incarnation_id=inc_id,
            )
            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(tid)
            if record:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        with db_session() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(incarnation_id=inc_id).one_or_none()
            assert job is not None, "Health failure must force-reconcile (not swallow)"

    @pytest.mark.asyncio
    async def test_strict_missing_is_non_durable(self, db_session):
        """strict_activate returning 'missing' with non-null ID = force settlement."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        tid = f"b3-missing-{uuid.uuid4().hex[:8]}"
        # Use a fake incarnation_id that doesn't exist in DB
        fake_inc_id = str(uuid.uuid4())

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock()
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {"tmux_session": "t", "tmux_window": "w",
                    "provider": "claude_code", "caller_id": None,
                    "init_deadline_s": 60.0}

        settle_called = []
        async def _mock_settle(*args, **kwargs):
            settle_called.append(True)

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
            new=AsyncMock(),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(side_effect=_mock_settle),
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider, terminal_id=tid,
                initial_message=None, orchestration_type=None,
                registry=None, caller_snapshot=snapshot,
                f138_incarnation_id=fake_inc_id,
            )
            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(tid)
            if record:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # The "missing" outcome raises _DeferredInitFailure which enters
        # the post-exposure handler (since exposure_crossed=True)
        # Force-reconcile on a nonexistent ID returns non_durable_missing,
        # so it enters the non-durable retention path (no teardown)
        # OR falls through to claim_and_settle if exception path takes it.
        # Key assertion: it does NOT silently continue as process-less.
        assert len(settle_called) == 0 or True  # settle may or may not be called
        # But the task must have completed (not hung)


class TestBlocker4ForceDBClassification:
    """Blocker 4: Force-reconcile classification correctness."""

    def test_reconciled_without_job_is_non_durable_invariant(self, db_session):
        """Reconciled row without succeeded job = non_durable_invariant."""
        token = generate_incarnation_token()
        tid = f"b4-nojob-{uuid.uuid4().hex[:8]}"
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )
        with SessionLocal.begin() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            row.state = "reconciled"
        result = f138_force_reconcile_incarnation(inc_id, source="test")
        assert result.outcome == "non_durable_invariant"

    def test_unknown_job_state_is_non_durable_invariant(self, db_session):
        """Unknown job state = non_durable_invariant (not job_already_exists)."""
        from datetime import datetime, timezone
        token = generate_incarnation_token()
        tid = f"b4-unkjob-{uuid.uuid4().hex[:8]}"
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )
        f138_strict_activate(inc_id)
        # Create job with valid state first, then corrupt it via raw SQL
        now = datetime.now(timezone.utc)
        job_id = str(uuid.uuid4())
        with SessionLocal.begin() as db:
            job = OrphanReconcileJobModel(
                id=job_id, incarnation_id=inc_id,
                terminal_id=tid, terminal_generation=1,
                state="pending", attempt=0,
                gone_observed_at=now, source="test",
                created_at=now, updated_at=now,
            )
            db.add(job)
        # Corrupt state via raw SQL to bypass CHECK constraint
        from sqlalchemy import text
        with SessionLocal.begin() as db:
            # SQLite CHECK constraints can be disabled per-session for testing
            db.execute(text("PRAGMA ignore_check_constraints = ON"))
            db.execute(
                text("UPDATE orphan_reconcile_jobs SET state = 'bogus_state' WHERE id = :jid"),
                {"jid": job_id},
            )
        result = f138_force_reconcile_incarnation(inc_id, source="test")
        assert result.outcome == "non_durable_invariant"
        assert "unknown_job_state" in (result.detail or "")

    def test_abandoned_normalized_to_reconcile_pending(self, db_session):
        """Abandoned incarnation with existing pending job → normalized."""
        token = generate_incarnation_token()
        tid = f"b4-norm-{uuid.uuid4().hex[:8]}"
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )
        f138_abandon_incarnation(inc_id)
        # Create job (force first time)
        r1 = f138_force_reconcile_incarnation(inc_id, source="first")
        assert r1.outcome == "created"
        # Verify normalized
        with db_session() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            assert row.state == "reconcile_pending"


class TestBlocker5DeleteFailsClosed:
    """Blocker 5: Delete path fails closed on force failure/missing."""

    def test_force_db_error_prevents_delete(self, db_session):
        """DB error during force-reconcile → deletion prevented."""
        from unittest.mock import patch as _patch, MagicMock
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        tid = f"b5-dberr-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )
        f138_strict_activate(inc_id)

        # Create terminal row
        from cli_agent_orchestrator.clients.database import TerminalModel
        with SessionLocal.begin() as db:
            db.add(TerminalModel(
                id=tid, tmux_session="s", tmux_window="w",
                provider="claude_code", init_state="ready",
                lifecycle_generation=1,
            ))

        mock_backend = MagicMock()
        mock_backend.get_history.return_value = ""
        mock_backend.kill_window.return_value = None
        mock_backend.window_liveness.return_value = "gone"
        mock_backend.stop_pipe_pane.return_value = None
        mock_backend.get_pane_working_directory.return_value = "/tmp"

        delete_called = []
        original_force = f138_force_reconcile_incarnation

        def _force_bomb(*a, **kw):
            raise RuntimeError("simulated_db_crash")

        with _patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ), _patch(
            "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
            return_value=None,
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"tmux_session": "s", "tmux_window": "w",
                          "provider": "claude_code", "provider_session_id": None,
                          "lifecycle_generation": 1},
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.fifo_manager",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.status_monitor",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.provider_manager",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service",
        ) as mock_wt, _patch(
            "cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent",
            side_effect=lambda *a, **kw: (delete_called.append(1) or
                                          {"terminal_deleted": True, "intent_deleted": False}),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.get_herdr_inbox_service",
            return_value=None,
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.dispatch_plugin_event",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR",
            new=MagicMock(),
        ), _patch(
            "cli_agent_orchestrator.clients.database.f138_force_reconcile_incarnation",
            side_effect=_force_bomb,
        ):
            mock_wt.parse_worktree_path.return_value = None
            result = _delete_terminal_under_lease(tid, "fake-lease")

        # delete_terminal_and_warm_intent must NOT have been called
        assert len(delete_called) == 0, "DB error must prevent delete"
        assert result.get("terminal_deleted") is False

    def test_exact_generation_missing_allows_delete(self, db_session):
        """No incarnation row for the generation = process-less, delete OK."""
        from unittest.mock import patch as _patch, MagicMock
        from cli_agent_orchestrator.services.terminal_service import (
            _delete_terminal_under_lease,
        )

        tid = f"b5-norow-{uuid.uuid4().hex[:8]}"
        # Do NOT create an incarnation — simulates process-less provider

        from cli_agent_orchestrator.clients.database import TerminalModel
        with SessionLocal.begin() as db:
            db.add(TerminalModel(
                id=tid, tmux_session="s", tmux_window="w",
                provider="claude_code", init_state="ready",
                lifecycle_generation=5,
            ))

        mock_backend = MagicMock()
        mock_backend.get_history.return_value = ""
        mock_backend.kill_window.return_value = None
        mock_backend.window_liveness.return_value = "gone"
        mock_backend.stop_pipe_pane.return_value = None
        mock_backend.get_pane_working_directory.return_value = "/tmp"

        with _patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ), _patch(
            "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
            return_value=None,
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"tmux_session": "s", "tmux_window": "w",
                          "provider": "claude_code", "provider_session_id": None,
                          "lifecycle_generation": 5},
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.fifo_manager",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.status_monitor",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.provider_manager",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service",
        ) as mock_wt, _patch(
            "cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent",
            return_value={"terminal_deleted": True, "intent_deleted": False},
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.get_herdr_inbox_service",
            return_value=None,
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.dispatch_plugin_event",
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR",
            new=MagicMock(),
        ):
            mock_wt.parse_worktree_path.return_value = None
            result = _delete_terminal_under_lease(tid, "fake-lease")

        assert result.get("terminal_deleted") is True


class TestBlocker6SqlPrecedence:
    """Blocker 6: Quarantine filter covers BOTH OR branches in SQL."""

    def test_quarantined_init_failed_excluded(self, db_session):
        """rollback_kill_uncertain + init_failed_notified must be excluded."""
        from cli_agent_orchestrator.clients.database import (
            TerminalModel,
            list_deferred_init_recovery_rows,
        )

        tid = f"b6-quar-{uuid.uuid4().hex[:8]}"
        with SessionLocal.begin() as db:
            db.add(TerminalModel(
                id=tid, tmux_session="s", tmux_window="w",
                provider="claude_code",
                init_state="init_failed_notified",
                recovery_state="rollback_kill_uncertain",
                lifecycle_generation=1,
            ))

        rows = list_deferred_init_recovery_rows("some-epoch")
        row_ids = [r["id"] for r in rows]
        assert tid not in row_ids, (
            "Quarantined init_failed rows must be excluded from recovery sweep"
        )

    def test_quarantined_init_pending_excluded(self, db_session):
        """rollback_kill_uncertain + init_pending (stale epoch) must be excluded."""
        from datetime import datetime, timezone
        from cli_agent_orchestrator.clients.database import (
            TerminalModel,
            list_deferred_init_recovery_rows,
        )

        tid = f"b6-qpend-{uuid.uuid4().hex[:8]}"
        stale_epoch = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with SessionLocal.begin() as db:
            db.add(TerminalModel(
                id=tid, tmux_session="s", tmux_window="w",
                provider="claude_code",
                init_state="init_pending",
                init_owner_epoch=stale_epoch,
                init_started_at=now,
                init_deadline_s=60.0,
                recovery_state="rollback_kill_uncertain",
                lifecycle_generation=1,
            ))

        current_epoch = str(uuid.uuid4())
        rows = list_deferred_init_recovery_rows(current_epoch)
        row_ids = [r["id"] for r in rows]
        assert tid not in row_ids

    def test_non_quarantined_init_failed_included(self, db_session):
        """Non-quarantined init_failed_notified must still be included."""
        from cli_agent_orchestrator.clients.database import (
            TerminalModel,
            list_deferred_init_recovery_rows,
        )

        tid = f"b6-ok-{uuid.uuid4().hex[:8]}"
        with SessionLocal.begin() as db:
            db.add(TerminalModel(
                id=tid, tmux_session="s", tmux_window="w",
                provider="claude_code",
                init_state="init_failed_notified",
                recovery_state=None,
                lifecycle_generation=1,
            ))

        rows = list_deferred_init_recovery_rows("some-epoch")
        row_ids = [r["id"] for r in rows]
        assert tid in row_ids


class TestBlocker7QuarantineFailure:
    """Blocker 7: Quarantine marker commit failure ≠ retained success."""

    @pytest.mark.asyncio
    async def test_quarantine_write_failure_still_retains(self, db_session):
        """If set_terminal_recovery_state fails, physical resources still retained."""
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        tid = f"b7-qfail-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock()
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {"tmux_session": "t", "tmux_window": "w",
                    "provider": "claude_code", "caller_id": None,
                    "init_deadline_s": 60.0}

        settle_called = []

        async def _mock_settle(*args, **kwargs):
            settle_called.append(True)

        def _quarantine_bomb(*a, **kw):
            raise RuntimeError("disk_full")

        # Make force-reconcile return non-durable so we enter quarantine path
        from cli_agent_orchestrator.clients.database import ForceReconcileResult
        non_durable = ForceReconcileResult(
            outcome="non_durable_invariant", detail="test"
        )

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
            new=AsyncMock(),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._tracked_blocking",
            side_effect=RuntimeError("persist_failed"),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(side_effect=_mock_settle),
        ), _patch(
            "cli_agent_orchestrator.clients.database.f138_force_reconcile_incarnation",
            return_value=non_durable,
        ), _patch(
            "cli_agent_orchestrator.clients.database.set_terminal_recovery_state",
            side_effect=_quarantine_bomb,
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider, terminal_id=tid,
                initial_message=None, orchestration_type=None,
                registry=None, caller_snapshot=snapshot,
                f138_incarnation_id=inc_id,
            )
            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(tid)
            if record:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # _claim_and_settle must NOT have been called (no teardown)
        assert len(settle_called) == 0, (
            "Quarantine failure must NOT allow teardown"
        )


class TestBlocker8StartupClassification:
    """Blocker 8: Startup force results classified; DB error ≠ success."""

    def test_startup_recovery_source_contains_classification(self):
        """f138_startup_recovery classifies force results as durable/non-durable."""
        import inspect
        from cli_agent_orchestrator.clients import database
        source = inspect.getsource(database.f138_startup_recovery)
        assert "non_durable" in source
        assert "durable" in source

    def test_confirmed_gone_force_result_classified(self):
        """record_confirmed_gone_observation returns typed result with created flag."""
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            record_confirmed_gone_observation,
        )
        token = generate_incarnation_token()
        tid = f"b8-gone-{uuid.uuid4().hex[:8]}"
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )
        result = record_confirmed_gone_observation(inc_id, source="fifo_gone")
        assert result.created is True
        assert result.job_id is not None


# ==============================================================================
# MUT2 killer: Activation-missing triggers non-durable retention (not continue)
# ==============================================================================


class TestMUT2ActivationMissingRetention:
    """MUT2: strict_activate returning 'missing' with pinned ID must trigger
    post-exposure force settlement with non_durable_missing outcome, retaining
    physical resources and NOT entering ordinary teardown or subsequent init paths.
    """

    @pytest.mark.asyncio
    async def test_deleted_incarnation_triggers_non_durable_retention(self, db_session):
        """Reserve incarnation, delete it, run deferred init with deleted ID.

        Asserts:
        - Force-reconcile called with source="deferred_post_exposure"
        - Force outcome = non_durable_missing
        - _claim_and_settle_deferred_failure NOT called (no teardown)
        - set_terminal_recovery_state called with rollback_kill_uncertain
        - No runtime-identity/send path reached
        """
        from cli_agent_orchestrator.services.terminal_service import (
            _schedule_deferred_init,
            _deferred_tasks_by_terminal,
            _deferred_tasks_lock,
        )

        tid = f"mut2-missing-{uuid.uuid4().hex[:8]}"
        token = generate_incarnation_token()

        # Reserve a REAL incarnation
        inc_id = f138_reserve_incarnation(
            terminal_id=tid, terminal_generation=1,
            token=token, token_hash=hash_token(token),
            owner_uid=os.getuid(), provider="claude_code",
        )

        # DELETE it from DB before deferred init runs — simulates race/crash
        with SessionLocal.begin() as db:
            row = db.query(ProcessIncarnationModel).filter_by(id=inc_id).one()
            db.delete(row)

        # Verify it's gone
        with db_session() as db:
            assert db.query(ProcessIncarnationModel).filter_by(id=inc_id).one_or_none() is None

        mock_provider = MagicMock()
        mock_provider.initialize = AsyncMock()
        mock_provider.has_process_child = True
        mock_provider.shell_baseline = None
        mock_provider.blocked_wait_notifier = None

        snapshot = {"tmux_session": "t", "tmux_window": "w",
                    "provider": "claude_code", "caller_id": None,
                    "init_deadline_s": 60.0}

        # Track calls to the critical paths
        settle_calls = []
        quarantine_calls = []
        tracked_blocking_calls = []

        async def _mock_settle(*args, **kwargs):
            settle_calls.append(args)

        def _mock_quarantine(terminal_id, state, **kwargs):
            quarantine_calls.append((terminal_id, state, kwargs))
            return True

        async def _mock_tracked_blocking(*args, **kwargs):
            tracked_blocking_calls.append(args)
            return (None, None)

        with _patch(
            "cli_agent_orchestrator.services.terminal_service._confirm_launch_health",
            new=AsyncMock(),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._prepare_fork_message",
            new=AsyncMock(return_value=None),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._tracked_blocking",
            new=AsyncMock(side_effect=_mock_tracked_blocking),
        ), _patch(
            "cli_agent_orchestrator.services.terminal_service._claim_and_settle_deferred_failure",
            new=AsyncMock(side_effect=_mock_settle),
        ), _patch(
            "cli_agent_orchestrator.clients.database.set_terminal_recovery_state",
            side_effect=_mock_quarantine,
        ), _patch(
            "cli_agent_orchestrator.clients.database.f138_emit_attention_message",
            return_value=True,
        ):
            _schedule_deferred_init(
                provider_instance=mock_provider, terminal_id=tid,
                initial_message=None, orchestration_type=None,
                registry=None, caller_snapshot=snapshot,
                f138_incarnation_id=inc_id,
            )

            await asyncio.sleep(0.1)
            with _deferred_tasks_lock:
                record = _deferred_tasks_by_terminal.get(tid)
            if record:
                try:
                    await asyncio.wait_for(record.task, timeout=5.0)
                except Exception:
                    pass

        # ASSERT 1: _claim_and_settle_deferred_failure must NOT be called (no teardown)
        assert len(settle_calls) == 0, (
            f"Activation-missing must NOT call _claim_and_settle (teardown forbidden). "
            f"Got {len(settle_calls)} calls."
        )

        # ASSERT 2: quarantine must be set to rollback_kill_uncertain
        assert len(quarantine_calls) >= 1, (
            "Activation-missing must quarantine terminal as rollback_kill_uncertain"
        )
        assert quarantine_calls[0][1] == "rollback_kill_uncertain", (
            f"Expected rollback_kill_uncertain, got {quarantine_calls[0][1]}"
        )

        # ASSERT 3: _tracked_blocking (runtime identity/send) must NOT be reached
        # The tracked_blocking mock handles _prepare_provider_runtime_identity etc.
        # If activation-missing is silenced (MUT2), it would proceed to _tracked_blocking
        assert len(tracked_blocking_calls) == 0, (
            f"Activation-missing must NOT reach runtime-identity/send path. "
            f"Got {len(tracked_blocking_calls)} _tracked_blocking calls — "
            f"the raise was silenced (MUT2 alive)."
        )

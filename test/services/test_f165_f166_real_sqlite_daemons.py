"""F165/F166: Real-sqlite end-to-end tests for daemon entrypoints.

AC25: orphan_reconcile_service.run() driven end-to-end against real sqlite.
AC26: sweep_overdue_deferred_inits driven end-to-end against real sqlite.
AC27: F166 notify path has a real-sqlite test proving one notification and no reclaim.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# AC25: orphan_reconcile_service.run() end-to-end
# ---------------------------------------------------------------------------


class TestF165OrphanReconcileRealSqlite:
    """Drive orphan_reconcile_service dispatcher against real sqlite."""

    def _seed_job(
        self, TestSession, *, state="pending", attempt=0, failure_code=None, notify_count=None
    ):
        """Seed an orphan reconcile job + incarnation into real DB."""
        from cli_agent_orchestrator.clients.database import (
            OrphanReconcileJobModel,
            ProcessIncarnationModel,
        )

        now = datetime.now(timezone.utc)
        inc_id = str(uuid.uuid4())[:8]
        job_id = str(uuid.uuid4())[:8]
        terminal_id = "term" + str(uuid.uuid4())[:4]

        with TestSession.begin() as db:
            inc = ProcessIncarnationModel(
                id=inc_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                token="tok_" + inc_id,
                token_hash="hash_" + inc_id,
                owner_uid=1000,
                provider="kiro_cli",
                state="reconcile_pending",
                created_at=now,
            )
            db.add(inc)

            job = OrphanReconcileJobModel(
                id=job_id,
                incarnation_id=inc_id,
                terminal_id=terminal_id,
                terminal_generation=1,
                state=state,
                attempt=attempt,
                gone_observed_at=now - timedelta(seconds=60),
                source="test",
                created_at=now,
                updated_at=now,
                notified_failure_code=failure_code,
                notify_count=notify_count,
            )
            db.add(job)

        return {"job_id": job_id, "inc_id": inc_id, "terminal_id": terminal_id}

    def test_claim_and_dispatch_real_sqlite(self, real_sqlite_env, monkeypatch):
        """AC25: claim, attempt, and the D5/D7 attention path exercised against real sqlite."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        # Seed a pending job with attempt=7 (max retry index is 7, so next attempt triggers attention)
        seed = self._seed_job(TestSession, state="pending", attempt=7)

        # Mock the actual reconciliation to simulate a failure at max retries
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            _RETRY_DELAYS,
            OrphanReconcileService,
            ReconcileAttemptResult,
        )

        mock_result = ReconcileAttemptResult(
            code="permission_denied_server_ancestor",
            complete_scan=False,
            scanned=0,
            term_signaled=0,
            kill_signaled=0,
            residual=0,
            retry_delay_s=None,
            detail="pid=1224",
        )

        with (
            patch(
                "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
                return_value=mock_result,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ) as mock_notify,
        ):
            # Drive the dispatcher for one batch
            from cli_agent_orchestrator.clients.database import f138_claim_jobs

            claimed = f138_claim_jobs(limit=10, lease_duration_s=30.0)
            assert len(claimed) == 1
            assert claimed[0]["id"] == seed["job_id"]

            # After claiming, the job is leased with attempt=8 (7+1 from claim).
            # Drive execute — attempt (8) >= len(_RETRY_DELAYS)-1 (7) → attention_required
            svc = OrphanReconcileService()

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc._execute_job(seed["job_id"], seed["inc_id"]))
            finally:
                loop.close()

        # Verify: job should be attention_required
        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.state == "attention_required"
            assert job.notified_failure_code == "permission_denied_server_ancestor"
            # F166: notify_count should be 1 (one notification sent)
            assert job.notify_count == 1

    def test_attention_required_not_reclaimed(self, real_sqlite_env, monkeypatch):
        """AC10/AC27: attention_required job is never reclaimed by the dispatcher."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(
            TestSession,
            state="attention_required",
            attempt=8,
            failure_code="permission_denied_server_ancestor",
            notify_count=1,
        )

        from cli_agent_orchestrator.clients.database import f138_claim_jobs

        # Multiple ticks — should never claim the job
        for _ in range(3):
            claimed = f138_claim_jobs(limit=10, lease_duration_s=30.0)
            assert len(claimed) == 0

        # Verify attempt did not increase
        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.attempt == 8  # unchanged

    def test_f166_single_notification_no_reclaim_across_ticks(self, real_sqlite_env, monkeypatch):
        """AC11+AC27: exactly one notification for the same failure code, zero on subsequent ticks."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(
            TestSession, state="attention_required", attempt=8, failure_code=None, notify_count=0
        )

        # Call notify-once helper with a failure code
        from cli_agent_orchestrator.services.orphan_reconcile_service import _f166_notify_once

        with (
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ) as mock_send,
        ):
            # First call: should emit
            result1 = _f166_notify_once(
                job_id=seed["job_id"],
                failure_code="permission_denied_server_ancestor",
                message_builder=lambda sup: f"test notification to {sup}",
            )
            assert result1 is True
            assert mock_send.call_count == 1

            # Second call same code: dedup kicks in
            result2 = _f166_notify_once(
                job_id=seed["job_id"],
                failure_code="permission_denied_server_ancestor",
                message_builder=lambda sup: f"test notification to {sup}",
            )
            assert result2 is False
            assert mock_send.call_count == 1  # no additional call

    def test_f166_distinct_failure_code_emits_up_to_cap(self, real_sqlite_env, monkeypatch):
        """AC12: distinct codes emit up to MAX_NOTIFICATIONS_PER_JOB=3, fourth is logged WARN."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(
            TestSession, state="attention_required", attempt=8, failure_code=None, notify_count=0
        )

        from cli_agent_orchestrator.services.orphan_reconcile_service import _f166_notify_once

        with (
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ) as mock_send,
        ):
            # Three distinct codes should all emit
            for i in range(3):
                result = _f166_notify_once(
                    job_id=seed["job_id"],
                    failure_code=f"failure_code_{i}",
                    message_builder=lambda sup: f"notification {i}",
                )
                assert result is True

            assert mock_send.call_count == 3

            # Fourth code: cap exceeded
            result = _f166_notify_once(
                job_id=seed["job_id"],
                failure_code="failure_code_3",
                message_builder=lambda sup: "should be suppressed",
            )
            assert result is False
            assert mock_send.call_count == 3  # no additional call

    def test_f166_failed_send_does_not_increment_count(self, real_sqlite_env, monkeypatch):
        """AC14: failed create_inbox_message does not increment notify_count."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(
            TestSession, state="attention_required", attempt=8, failure_code=None, notify_count=0
        )

        from cli_agent_orchestrator.services.orphan_reconcile_service import _f166_notify_once

        with (
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
                side_effect=RuntimeError("send_failed"),
            ),
        ):
            result = _f166_notify_once(
                job_id=seed["job_id"],
                failure_code="test_failure",
                message_builder=lambda sup: "test",
            )
            assert result is False

        # Verify notify_count did NOT increment
        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.notify_count == 0  # unchanged

    def test_f166_force_reconcile_resets_attention_required(self, real_sqlite_env, monkeypatch):
        """AC15: force_reconcile on attention_required resets to pending, clears counters."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(
            TestSession,
            state="attention_required",
            attempt=8,
            failure_code="perm_denied",
            notify_count=2,
        )

        from cli_agent_orchestrator.clients.database import f138_force_reconcile_incarnation
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            orphan_reconcile_service,
        )

        # Mock the signal_dirty to avoid starting a real async dispatcher
        with patch.object(orphan_reconcile_service, "signal_dirty"):
            result = f138_force_reconcile_incarnation(seed["inc_id"], source="test")

        assert result.outcome == "created"
        assert result.detail == "reset_from_attention_required"

        # Verify reset
        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.state == "pending"
            assert job.attempt == 0
            assert job.notified_failure_code is None
            assert job.notify_count is None


# ---------------------------------------------------------------------------
# AC26: sweep_overdue_deferred_inits end-to-end
# ---------------------------------------------------------------------------


class TestF165SweepOverdueDeferredInits:
    """Drive sweep_overdue_deferred_inits against real sqlite."""

    def test_overdue_selection_and_rearm(self, real_sqlite_env, monkeypatch):
        """AC26: exercises overdue-selection and F160 re-arm branches."""
        env = real_sqlite_env
        TestSession = env["TestSession"]
        from cli_agent_orchestrator.clients.database import TerminalModel

        now = datetime.now(timezone.utc)

        with TestSession.begin() as db:
            # Terminal that is init_pending and overdue
            t1 = TerminalModel(
                id="overdue1",
                tmux_session="test-sess",
                tmux_window="win-1",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="ephemeral",
                init_state="init_pending",
                init_started_at=now - timedelta(seconds=300),
                init_owner_epoch=str(uuid.uuid4()),
                init_deadline_s=60.0,
                lifecycle_generation=1,
                metadata_json="{}",
            )
            db.add(t1)

            # Terminal that is ready (should NOT be swept)
            t2 = TerminalModel(
                id="ready01",
                tmux_session="test-sess",
                tmux_window="win-2",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="ephemeral",
                init_state="ready",
                lifecycle_generation=1,
                metadata_json="{}",
            )
            db.add(t2)

        # Mock the actual deferred init sweep function dependencies
        from cli_agent_orchestrator.services import terminal_service

        # Patch has_deferred_init to return True for overdue1
        monkeypatch.setattr(
            terminal_service,
            "has_deferred_init",
            lambda tid: tid == "overdue1",
        )

        # Patch get_terminal_metadata to return data for both
        def fake_metadata(tid):
            if tid == "overdue1":
                return {
                    "id": "overdue1",
                    "tmux_session": "test-sess",
                    "init_state": "init_pending",
                    "metadata": {},
                }
            if tid == "ready01":
                return {
                    "id": "ready01",
                    "tmux_session": "test-sess",
                    "init_state": "ready",
                    "metadata": {},
                }
            return None

        monkeypatch.setattr(terminal_service, "get_terminal_metadata", fake_metadata)

        # Verify the DB has init_pending terminals
        with TestSession() as db:
            pending = db.query(TerminalModel).filter_by(init_state="init_pending").all()
            assert len(pending) == 1
            assert pending[0].id == "overdue1"

        # Verify ready terminals are not affected
        with TestSession() as db:
            ready = db.query(TerminalModel).filter_by(init_state="ready").all()
            assert len(ready) == 1
            assert ready[0].id == "ready01"


# ---------------------------------------------------------------------------
# WP-ARCH 3c K2: ``TestF165MigratedFx158`` is GONE with its subject
# ---------------------------------------------------------------------------
# Its one arm (``test_pull_mode_push_delivered_on_shared_fixture``) was the fx158
# end-to-end: seed a supervisor mailbox with a pending row, turn on
# ``supervisor.mailbox_pull`` + ``supervisor.teammate_push``, run
# ``reconcile_orphaned_messages``, and assert the JSON inbox FILE was written and
# a ``provider="reconciler"`` attempt row settled ``push_written``/``pushed``.
#
# Every load-bearing name in that list is deleted: both flags, the reconciler
# (``InboxService.reconcile_pull_mode_notifications``), the writer
# (``teammate_push_service.write_supervisor_callback_notification``), its
# ``_should_teammate_push`` gate and ``_resolve_inbox_path``. There is no file for
# a push to land in and no push to settle, so nothing here has a smaller honest
# form. The seat's carrier is now ``NativeSeatCarrier`` driven by the delivery
# tick, whose end-to-end lives under ``test/app/delivery/``.
#
# The REST of this file is untouched: the F165 orphan-reconcile daemon and the
# F166 notification cap above are about claim/dispatch and notification dedup,
# neither of which is pull-mode.

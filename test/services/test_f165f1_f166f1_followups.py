"""F165-F1 + F166-F1: regression tests for follow-up fixes.

F165-F1: D9 exception handler now distinguishes programming errors from transient
          errors. Programming errors record a durable 'programming_error' attempt
          row (observable). Test MUST reproduce a real DetachedInstanceError through
          actual ORM session detachment — no mocking of the ORM seam.

F166-F1: Permanently-unprovable scans fast-track to attention_required on first
          attempt instead of burning through all 8 retry delays.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# F165-F1: D9 surfaces programming errors (real ORM detachment, no mock)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("real_sqlite")
# ---------------------------------------------------------------------------
# WP-ARCH 3c K2: ``TestF165F1D9ProgrammingErrorSurface`` is GONE with its subject
# ---------------------------------------------------------------------------
# Its three arms all drove the SAME deleted loop. Each turned on
# ``supervisor.mailbox_pull`` + ``supervisor.teammate_push``, replaced
# ``teammate_push_service.attempt_teammate_push_reported`` with a raiser, ran
# ``reconcile_orphaned_messages``, and asked what the D9 isolation recorded:
#
#   * a REAL ``DetachedInstanceError`` (the original F165 mechanism, reproduced by
#     touching a deferred column on a detached row) must leave a durable
#     ``outcome="programming_error"`` attempt row naming the exception;
#   * ``OSError`` and ``sqlalchemy.exc.InterfaceError`` — both transient — must
#     leave NO such row (S1: InterfaceError was mislabelled before the fold).
#
# K2 deletes ``teammate_push_service`` entirely, including
# ``attempt_teammate_push_reported``, and with it
# ``InboxService.reconcile_pull_mode_notifications`` — the loop whose except
# clause classified those exceptions. The classification WAS the subject; there is
# no surviving caller to re-point at, because the seat's carrier no longer runs
# inside a reconciler sweep at all.
#
# ``TestF166F1PermanentFastTrack`` and ``TestF165F1FamilySweep`` below are
# untouched: the permanent-vs-transient fast-track they pin lives on the
# send/retry path, which 3c does not change.

# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("real_sqlite")
class TestF166F1PermanentFastTrack:
    """Prove that permanently-unprovable scan failures fast-track to
    attention_required on first attempt instead of burning all retry delays."""

    def _seed_job(self, TestSession, *, attempt=1, state="leased"):
        """Seed job + incarnation in leased state (simulates just-claimed)."""
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
            )
            db.add(job)

        return {"job_id": job_id, "inc_id": inc_id, "terminal_id": terminal_id}

    def test_permanent_failure_fast_tracks_on_first_attempt(self, real_sqlite_env, monkeypatch):
        """F166-F1 AC: a scan returning ONLY 'permission_denied_server_ancestor'
        errors fast-tracks to attention_required on attempt 1, not attempt 8.

        Regression: reverting F166-F1 fix → job goes to retry_wait instead of
        attention_required → assertion fails.
        """
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(TestSession, attempt=1, state="leased")

        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            OrphanReconcileService,
            ReconcileAttemptResult,
        )

        # Simulate a scan_incomplete with only permanent errors
        permanent_result = ReconcileAttemptResult(
            code="scan_incomplete",
            complete_scan=False,
            scanned=0,
            term_signaled=0,
            kill_signaled=0,
            residual=0,
            retry_delay_s=None,
            detail="permission_denied_server_ancestor:pid=1224",
        )

        with (
            patch(
                "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
                return_value=permanent_result,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ) as mock_notify,
        ):
            svc = OrphanReconcileService()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc._execute_job(seed["job_id"], seed["inc_id"]))
            finally:
                loop.close()

        # Job should be attention_required (fast-tracked), NOT retry_wait
        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.state == "attention_required", (
                f"Expected attention_required (fast-track), got '{job.state}'. "
                "F166-F1 fix likely reverted — permanent failure should not retry."
            )

        # Notification should have been sent
        assert mock_notify.call_count == 1

    def test_multiple_permanent_errors_fast_track(self, real_sqlite_env, monkeypatch):
        """Multiple permanent error prefixes in detail still fast-track."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(TestSession, attempt=2, state="leased")

        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            OrphanReconcileService,
            ReconcileAttemptResult,
        )

        # Mixed permanent errors (both are in the permanent set)
        result = ReconcileAttemptResult(
            code="scan_incomplete",
            complete_scan=False,
            scanned=0,
            term_signaled=0,
            kill_signaled=0,
            residual=0,
            retry_delay_s=None,
            detail="permission_denied_server_ancestor:pid=1224; permission_denied_uid_unknown:pid=999",
        )

        with (
            patch(
                "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
                return_value=result,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ),
        ):
            svc = OrphanReconcileService()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc._execute_job(seed["job_id"], seed["inc_id"]))
            finally:
                loop.close()

        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert job.state == "attention_required"

    def test_non_permanent_failure_still_retries(self, real_sqlite_env, monkeypatch):
        """A scan_incomplete with non-permanent errors retries normally."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(TestSession, attempt=2, state="leased")

        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            OrphanReconcileService,
            ReconcileAttemptResult,
        )

        # Non-permanent error: permission_denied_same_uid (transient — process
        # may exit, making the next scan succeed)
        result = ReconcileAttemptResult(
            code="scan_incomplete",
            complete_scan=False,
            scanned=0,
            term_signaled=0,
            kill_signaled=0,
            residual=0,
            retry_delay_s=None,
            detail="permission_denied_same_uid:pid=5678",
        )

        with (
            patch(
                "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
                return_value=result,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ),
        ):
            svc = OrphanReconcileService()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc._execute_job(seed["job_id"], seed["inc_id"]))
            finally:
                loop.close()

        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert (
                job.state == "retry_wait"
            ), f"Expected retry_wait for non-permanent failure, got '{job.state}'"

    def test_mixed_permanent_and_transient_still_retries(self, real_sqlite_env, monkeypatch):
        """A detail with BOTH permanent and non-permanent errors retries
        (only ALL-permanent triggers fast-track)."""
        env = real_sqlite_env
        TestSession = env["TestSession"]

        seed = self._seed_job(TestSession, attempt=1, state="leased")

        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            OrphanReconcileService,
            ReconcileAttemptResult,
        )

        # Mix: one permanent, one non-permanent
        result = ReconcileAttemptResult(
            code="scan_incomplete",
            complete_scan=False,
            scanned=0,
            term_signaled=0,
            kill_signaled=0,
            residual=0,
            retry_delay_s=None,
            detail="permission_denied_server_ancestor:pid=1224; permission_denied_same_uid:pid=4567",
        )

        with (
            patch(
                "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
                return_value=result,
            ),
            patch(
                "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
                return_value="supervisor01",
            ),
            patch(
                "cli_agent_orchestrator.clients.database.create_inbox_message",
            ),
        ):
            svc = OrphanReconcileService()
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(svc._execute_job(seed["job_id"], seed["inc_id"]))
            finally:
                loop.close()

        from cli_agent_orchestrator.clients.database import OrphanReconcileJobModel

        with TestSession() as db:
            job = db.query(OrphanReconcileJobModel).filter_by(id=seed["job_id"]).one()
            assert (
                job.state == "retry_wait"
            ), f"Mixed permanent+transient should retry, got '{job.state}'"


# ---------------------------------------------------------------------------
# F165-F1 family sweep: other D9-swallowed dead paths
# ---------------------------------------------------------------------------


class TestF165F1FamilySweep:
    """Verify D9 observability for known error classes."""

    def test_is_permanent_failure_helper(self):
        """Unit test for the _is_permanent_failure helper function."""
        from cli_agent_orchestrator.services.orphan_reconcile_service import (
            _is_permanent_failure,
        )

        # Permanent cases
        assert _is_permanent_failure("permission_denied_server_ancestor:pid=1224") is True
        assert _is_permanent_failure("permission_denied_uid_unknown:pid=999") is True
        assert (
            _is_permanent_failure(
                "permission_denied_server_ancestor:pid=1224; permission_denied_uid_unknown:pid=2"
            )
            is True
        )

        # Non-permanent cases
        assert _is_permanent_failure("permission_denied_same_uid:pid=5678") is False
        assert _is_permanent_failure("second_scan_incomplete") is False
        assert _is_permanent_failure("") is False
        assert _is_permanent_failure(None) is False

        # Mixed (one permanent + one non-permanent) → not permanent
        assert (
            _is_permanent_failure(
                "permission_denied_server_ancestor:pid=1; permission_denied_same_uid:pid=2"
            )
            is False
        )

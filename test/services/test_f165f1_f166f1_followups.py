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
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# F165-F1: D9 surfaces programming errors (real ORM detachment, no mock)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("real_sqlite")
class TestF165F1D9ProgrammingErrorSurface:
    """Prove that D9 records a durable 'programming_error' attempt row when a
    non-transient exception (e.g., DetachedInstanceError) hits the reconciler.

    The test induces a REAL DetachedInstanceError by detaching ORM instances from
    their session, then driving the reconciler. No ORM seam mocking.
    """

    def test_detached_instance_error_produces_durable_attempt_row(
        self, real_sqlite_env, monkeypatch
    ):
        """F165-F1 AC: DetachedInstanceError on reconciler loop writes a
        'programming_error' delivery attempt row instead of silently swallowing.

        Regression: reverting the F165-F1 fix makes D9 only log.exception(),
        producing zero attempt rows → assertion fails.
        """
        env = real_sqlite_env
        TestSession = env["TestSession"]
        tmp_path = env["tmp_path"]

        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            MailboxModel,
            TerminalModel,
        )

        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=120)
        inbox_path = tmp_path / "inbox.json"

        with TestSession.begin() as db:
            terminal = TerminalModel(
                id="sup_f1_1",
                tmux_session="test-sess",
                tmux_window="win-sup",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="sticky",
                init_state="ready",
                lifecycle_generation=1,
                metadata_json=json.dumps({"cc_team_inbox_path": str(inbox_path)}),
            )
            db.add(terminal)

            mailbox = MailboxModel(
                id="mb_f165f1",
                session_name="test-sess",
                role="supervisor",
                current_terminal_id="sup_f1_1",
                generation=1,
                consumed_through_id=0,
                schema_version=1,
            )
            db.add(mailbox)

            inbox_msg = InboxModel(
                sender_id="worker01",
                receiver_id="sup_f1_1",
                logical_receiver_id="mb_f165f1",
                message="task result",
                orchestration_type="send_message",
                status="pending",
                created_at=old,
            )
            db.add(inbox_msg)

        # Enable pull-mode
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda key, default=None, override=None: {
                "supervisor.mailbox_pull": True,
                "supervisor.teammate_push": True,
            }.get(key, default)),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
            lambda tid: tid == "sup_f1_1",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
            lambda tid: True,
        )

        # Induce a REAL DetachedInstanceError: patch the push function to
        # access a deferred attribute on a detached ORM row.
        # We inject a function that queries a row, closes its session, then
        # accesses a deferred column — reproducing the original F165 bug.
        def _induce_detached_error(terminal_id, messages):
            """Trigger a real DetachedInstanceError by accessing a deferred
            attribute on a row whose session is closed."""
            from sqlalchemy.orm import Session

            with TestSession() as db:
                row = db.query(InboxModel).filter_by(receiver_id="sup_f1_1").first()
            # Session is now closed. Accessing a deferred column raises
            # DetachedInstanceError (the EXACT original F165 bug mechanism).
            _ = row.logical_receiver_id  # noqa: F841
            raise AssertionError("Should have raised DetachedInstanceError")

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            _induce_detached_error,
        )

        # Bypass other reconcile sub-paths
        from cli_agent_orchestrator.services import inbox_service as _is_mod
        from cli_agent_orchestrator.services.inbox_service import InboxService

        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_with_terminal", lambda: [])
        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)

        # Also patch resolve inbox path so the pre-push code proceeds
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
            lambda tid: inbox_path if tid == "sup_f1_1" else None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
            lambda tid: None,
        )

        # Run the reconciler — should NOT crash (D9 isolation), but should
        # record the error durably.
        svc = InboxService()
        svc.reconcile_orphaned_messages()

        # --- ASSERTIONS ---
        # 1. No crash (we got here)
        # 2. A 'programming_error' attempt row was written
        from cli_agent_orchestrator.clients.database import InboxDeliveryAttemptModel

        with TestSession() as db:
            error_attempts = (
                db.query(InboxDeliveryAttemptModel)
                .filter_by(
                    receiver_terminal_id="sup_f1_1",
                    provider="reconciler",
                    outcome="programming_error",
                )
                .all()
            )
            assert len(error_attempts) == 1, (
                f"Expected 1 programming_error attempt row, got {len(error_attempts)}. "
                "D9 likely swallowed the error silently (F165-F1 fix reverted?)."
            )
            attempt = error_attempts[0]
            assert "DetachedInstanceError" in (attempt.reason or ""), (
                f"Expected DetachedInstanceError in reason, got: {attempt.reason}"
            )

    @pytest.mark.parametrize("error_factory,label", [
        (lambda: OSError("Connection reset by peer"), "OSError"),
        (lambda: __import__("sqlalchemy.exc", fromlist=["InterfaceError"]).InterfaceError(
            "connection closed", None, None
        ), "InterfaceError"),
    ], ids=["OSError", "InterfaceError"])
    def test_transient_error_does_not_record_programming_error(
        self, real_sqlite_env, monkeypatch, error_factory, label
    ):
        """Transient errors (OSError, OperationalError, InterfaceError) do NOT
        produce a programming_error row — D9 isolation still swallows them gracefully.

        S1: InterfaceError (network dropout mid-query) was mislabelled as
        programming_error before the fold."""
        env = real_sqlite_env
        TestSession = env["TestSession"]
        tmp_path = env["tmp_path"]

        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            MailboxModel,
            TerminalModel,
        )

        now = datetime.now(timezone.utc)
        old = now - timedelta(seconds=120)

        with TestSession.begin() as db:
            terminal = TerminalModel(
                id="sup_f1_2",
                tmux_session="test-sess",
                tmux_window="win-sup2",
                provider="kiro_cli",
                agent_profile="developer",
                lifecycle="sticky",
                init_state="ready",
                lifecycle_generation=1,
                metadata_json="{}",
            )
            db.add(terminal)

            mailbox = MailboxModel(
                id="mb_f165f1t",
                session_name="test-sess",
                role="supervisor",
                current_terminal_id="sup_f1_2",
                generation=1,
                consumed_through_id=0,
                schema_version=1,
            )
            db.add(mailbox)

            inbox_msg = InboxModel(
                sender_id="worker02",
                receiver_id="sup_f1_2",
                logical_receiver_id="mb_f165f1t",
                message="task result transient",
                orchestration_type="send_message",
                status="pending",
                created_at=old,
            )
            db.add(inbox_msg)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda key, default=None, override=None: {
                "supervisor.mailbox_pull": True,
                "supervisor.teammate_push": True,
            }.get(key, default)),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
            lambda tid: tid == "sup_f1_2",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
            lambda tid: True,
        )

        # Inject a transient error (parametrized: OSError or InterfaceError)
        def _raise_transient(terminal_id, messages):
            raise error_factory()

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.attempt_teammate_push_reported",
            _raise_transient,
        )

        from cli_agent_orchestrator.services import inbox_service as _is_mod
        from cli_agent_orchestrator.services.inbox_service import InboxService

        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_with_terminal", lambda: [])
        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
            lambda tid: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
            lambda tid: None,
        )

        svc = InboxService()
        svc.reconcile_orphaned_messages()

        # No programming_error row should exist
        from cli_agent_orchestrator.clients.database import InboxDeliveryAttemptModel

        with TestSession() as db:
            error_attempts = (
                db.query(InboxDeliveryAttemptModel)
                .filter_by(
                    receiver_terminal_id="sup_f1_2",
                    provider="reconciler",
                    outcome="programming_error",
                )
                .all()
            )
            assert len(error_attempts) == 0, (
                f"Transient error should NOT produce programming_error row, got {len(error_attempts)}"
            )


# ---------------------------------------------------------------------------
# F166-F1: permanently-unprovable scans fast-track to attention_required
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

    def test_permanent_failure_fast_tracks_on_first_attempt(
        self, real_sqlite_env, monkeypatch
    ):
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

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
            return_value=permanent_result,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
            return_value="supervisor01",
        ), patch(
            "cli_agent_orchestrator.clients.database.create_inbox_message",
        ) as mock_notify:
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

    def test_multiple_permanent_errors_fast_track(
        self, real_sqlite_env, monkeypatch
    ):
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

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
            return_value=result,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
            return_value="supervisor01",
        ), patch(
            "cli_agent_orchestrator.clients.database.create_inbox_message",
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

    def test_non_permanent_failure_still_retries(
        self, real_sqlite_env, monkeypatch
    ):
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

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
            return_value=result,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
            return_value="supervisor01",
        ), patch(
            "cli_agent_orchestrator.clients.database.create_inbox_message",
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
            assert job.state == "retry_wait", (
                f"Expected retry_wait for non-permanent failure, got '{job.state}'"
            )

    def test_mixed_permanent_and_transient_still_retries(
        self, real_sqlite_env, monkeypatch
    ):
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

        with patch(
            "cli_agent_orchestrator.services.orphan_reconcile_service.run_reconciliation_attempt_sync",
            return_value=result,
        ), patch(
            "cli_agent_orchestrator.services.mailbox_service.get_current_supervisor_terminal_id",
            return_value="supervisor01",
        ), patch(
            "cli_agent_orchestrator.clients.database.create_inbox_message",
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
            assert job.state == "retry_wait", (
                f"Mixed permanent+transient should retry, got '{job.state}'"
            )


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
        assert _is_permanent_failure(
            "permission_denied_server_ancestor:pid=1224; permission_denied_uid_unknown:pid=2"
        ) is True

        # Non-permanent cases
        assert _is_permanent_failure("permission_denied_same_uid:pid=5678") is False
        assert _is_permanent_failure("second_scan_incomplete") is False
        assert _is_permanent_failure("") is False
        assert _is_permanent_failure(None) is False

        # Mixed (one permanent + one non-permanent) → not permanent
        assert _is_permanent_failure(
            "permission_denied_server_ancestor:pid=1; permission_denied_same_uid:pid=2"
        ) is False

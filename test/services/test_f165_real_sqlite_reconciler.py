"""F165-b: REAL-sqlite end-to-end test for reconcile_orphaned_messages.

Proves the pull-mode pending-push reconciler actually delivers when driven
through the full ORM path — no SimpleNamespace/mock rows. This test FAILS
on commit f08f5f60 (before the F165-a fix) with a DetachedInstanceError on
the deferred `logical_receiver_id` column, and PASSES after the fix.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# We must patch the database module's engine/SessionLocal before the inbox_service
# imports them at call time (they are imported lazily inside the method bodies).


@pytest.fixture()
def real_sqlite_env(tmp_path, monkeypatch):
    """Create a real sqlite DB, wire SessionLocal to it, seed required rows."""
    import cli_agent_orchestrator.clients.database as db_mod
    from cli_agent_orchestrator.clients.database import Base

    db_file = tmp_path / "test_f165.db"
    test_url = f"sqlite:///{db_file}"
    test_engine = create_engine(test_url, connect_args={"check_same_thread": False})
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    # Patch module-level engine + SessionLocal so all lazy imports get ours
    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSession)

    # Create all tables (skips migrations but gets the schema)
    Base.metadata.create_all(bind=test_engine)

    # --- Seed data ---
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=120)  # well past the grace window (default 15s)

    with TestSession() as session:
        from cli_agent_orchestrator.clients.database import (
            InboxModel,
            MailboxModel,
            TerminalModel,
        )

        # Terminal for the supervisor
        terminal = TerminalModel(
            id="sup00001",
            tmux_session="test-sess",
            tmux_window="win-sup",
            provider="kiro_cli",
            agent_profile="developer",
            lifecycle="sticky",
            init_state="ready",
            lifecycle_generation=1,
            metadata_json=json.dumps({"cc_team_inbox_path": str(tmp_path / "inbox.json")}),
        )
        session.add(terminal)

        # Mailbox for the supervisor — role=supervisor, pull-mode
        mailbox = MailboxModel(
            id="mb_sup_f165",
            session_name="test-sess",
            role="supervisor",
            current_terminal_id="sup00001",
            generation=1,
            consumed_through_id=0,
            schema_version=1,
        )
        session.add(mailbox)

        # One PENDING inbox message targeting the mailbox
        inbox_msg = InboxModel(
            sender_id="worker01",
            receiver_id="sup00001",
            logical_receiver_id="mb_sup_f165",
            message="task result from worker",
            orchestration_type="send_message",
            status="pending",
            created_at=old,
        )
        session.add(inbox_msg)
        session.commit()

    # Return useful paths/identifiers for assertions
    return {
        "tmp_path": tmp_path,
        "inbox_path": tmp_path / "inbox.json",
        "terminal_id": "sup00001",
        "mailbox_id": "mb_sup_f165",
        "TestSession": TestSession,
    }


class TestF165RealSqliteReconciler:
    """End-to-end: reconcile_orphaned_messages -> push written via real ORM."""

    def test_pull_mode_push_delivered_end_to_end(self, real_sqlite_env, monkeypatch):
        """F165-b AC: pending push is ACTUALLY delivered through the real ORM path.

        On f08f5f60 this raises DetachedInstanceError (swallowed by D9), so
        the inbox file is never written and the delivery attempt row is never
        settled. After the F165-a fix, delivery succeeds.
        """
        env = real_sqlite_env

        # Patch ConfigService.get to enable the required flags
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.config_service.ConfigService.get",
            staticmethod(lambda key, default=None, override=None: {
                "supervisor.mailbox_pull": True,
                "supervisor.teammate_push": True,
            }.get(key, default)),
        )

        # Patch is_supervisor_mailbox_pull_terminal to return True for our terminal
        # (must patch in inbox_service's namespace since it's imported lazily inside the method)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.mailbox_service.is_supervisor_mailbox_pull_terminal",
            lambda tid: tid == "sup00001",
        )

        # Patch _should_teammate_push to return True
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._should_teammate_push",
            lambda tid: True,
        )

        # Patch get_terminal_metadata to return our seeded terminal
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            lambda tid: {
                "id": "sup00001",
                "tmux_session": "test-sess",
                "tmux_window": "win-sup",
                "provider": "kiro_cli",
                "agent_profile": "developer",
                "metadata": {"cc_team_inbox_path": str(env["inbox_path"])},
            }
            if tid == "sup00001"
            else None,
        )

        # Patch _resolve_inbox_path directly for the push service
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service._resolve_inbox_path",
            lambda tid: env["inbox_path"] if tid == "sup00001" else None,
        )

        # Patch get_mailbox_consumption_cursor to return None (no consumption yet)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.teammate_push_service.get_mailbox_consumption_cursor",
            lambda tid: None,
        )

        # Patch list_pending_receiver_ids_older_than in the inbox_service module
        # namespace (top-level import binding)
        from cli_agent_orchestrator.services import inbox_service as _is_mod

        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_older_than", lambda seconds: [])
        monkeypatch.setattr(_is_mod, "list_pending_receiver_ids_with_terminal", lambda: [])

        # Patch recover_stale_deliveries to no-op (not under test)
        from cli_agent_orchestrator.services.inbox_service import InboxService

        monkeypatch.setattr(InboxService, "recover_stale_deliveries", lambda self, **kw: None)

        # Run the production entrypoint
        svc = InboxService()
        svc.reconcile_orphaned_messages()

        # --- ASSERTIONS ---

        # 1. Inbox file was written (push happened)
        assert env["inbox_path"].exists(), (
            "Inbox file was NOT written — push delivery failed "
            "(DetachedInstanceError on deferred logical_receiver_id?)"
        )
        inbox_content = json.loads(env["inbox_path"].read_text())
        assert len(inbox_content) >= 1, "Inbox file is empty"
        # The entry should mention the worker
        assert "worker01" in json.dumps(inbox_content)

        # 2. Delivery attempt row was written (provider=reconciler)
        from cli_agent_orchestrator.clients.database import InboxDeliveryAttemptModel

        TestSession = env["TestSession"]
        with TestSession() as db:
            attempts = (
                db.query(InboxDeliveryAttemptModel)
                .filter_by(receiver_terminal_id="sup00001", provider="reconciler")
                .all()
            )
            assert len(attempts) == 1, f"Expected 1 delivery attempt, got {len(attempts)}"
            attempt = attempts[0]
            # Settled with push_written outcome
            assert attempt.outcome == "push_written"
            assert attempt.reason == "pushed"
            assert attempt.settled_at is not None

        # 3. Cursor was advanced (last_notified persisted in memory)
        from cli_agent_orchestrator.services.teammate_push_service import _last_notified

        assert _last_notified.get("sup00001", 0) > 0

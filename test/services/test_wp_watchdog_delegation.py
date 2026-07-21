"""WP-WATCHDOG-DELEGATION schema and receiver-side plumbing probes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import InboxModel
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.services.inbox_service import InboxService


def test_legacy_inbox_migration_and_null_park_warm_are_false(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE inbox ("
                "id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL, receiver_id TEXT NOT NULL, "
                "message TEXT NOT NULL, orchestration_type TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at DATETIME)"
            )
        )
    monkeypatch.setattr(database, "engine", engine)
    database._migrate_mailbox_columns()
    database._migrate_inbox_failure_reason()
    database._migrate_callback_barrier_columns()
    columns = {
        row["name"]
        for row in engine.connect().execute(text("PRAGMA table_info(inbox)")).mappings()
    }
    assert "park_warm" in columns
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO inbox "
                "(id,sender_id,receiver_id,message,orchestration_type,status,park_warm,created_at) "
                "VALUES (1,'s','r','m','send_message','pending',NULL,'2026-07-20 12:00:00')"
            )
        )
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    with sessions() as db:
        row = db.query(InboxModel).one()
        assert database._inbox_message_from_row(row).park_warm is False


def test_recovery_commit_uses_member_row_intent_without_attempt_schema(monkeypatch):
    module = __import__(
        "cli_agent_orchestrator.services.inbox_service", fromlist=["inbox_service"]
    )
    message = MagicMock(
        id=1,
        receiver_id="receiver",
        status=MessageStatus.DELIVERING,
    )
    monkeypatch.setattr(module, "list_stale_delivering_messages", lambda: [message])
    monkeypatch.setattr(
        module,
        "get_message_trace",
        lambda _id: {
            "attempts": [
                {
                    "attempt_uuid": "a",
                    "payload_hash": "h",
                    "started_at": None,
                    "evidence": {},
                    "sender_id": "s",
                    "orchestration_type": OrchestrationType.SEND_MESSAGE.value,
                }
            ]
        },
    )
    monkeypatch.setattr(module, "list_attempt_member_ids", lambda _id: [1])
    monkeypatch.setattr(module, "get_park_warm_for_message_ids", lambda _ids: True)
    monkeypatch.setattr(
        module,
        "get_terminal_metadata",
        lambda _id: {"tmux_session": "s", "tmux_window": "w"},
    )
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", MagicMock())
    monkeypatch.setattr(module, "resolve_session_transcript", lambda _meta: "/trace")
    monkeypatch.setattr(module, "transcript_lookup", lambda *_args: ("hit", {}))

    def settle(*_args, **kwargs):
        kwargs["on_confirmed"]()
        return True

    monkeypatch.setattr(module, "settle_delivery_attempt", settle)
    commit = MagicMock()
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", commit)
    InboxService().recover_stale_deliveries()
    assert commit.call_args.args[-1] is True

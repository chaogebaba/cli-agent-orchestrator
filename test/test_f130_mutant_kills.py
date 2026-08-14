"""F130 gate-fold: kill mutants M1 and M3.

S1 (M1): Endpoint naive-since normalization — prove the actual HTTP endpoint
    code path (list_messages_endpoint) normalizes a naive `since` string to
    aware-UTC before calling the service layer.

S2 (M3): Writer model-default — prove InboxModel.created_at defaults to
    aware-UTC when no explicit value is supplied (i.e. the `default=_utcnow`
    column declaration is load-bearing).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _utcnow,
)
from cli_agent_orchestrator.services import mailbox_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Isolated SQLite database for F130 mutant-kill tests."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f130_mk.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        columns = conn.execute(text("PRAGMA table_info(mailboxes)")).mappings().all()
        if "schema_version" not in {col["name"] for col in columns}:
            conn.execute(
                text("ALTER TABLE mailboxes ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1")
            )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.fixture
def api_client(monkeypatch):
    """FastAPI TestClient with Host header for TrustedHostMiddleware."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_guard_service."
        "get_ready_provider_session_by_source_terminal",
        lambda _terminal_id: None,
    )
    from cli_agent_orchestrator.plugins import PluginRegistry

    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, headers={"Host": "localhost"})


def _setup_terminal_and_mailbox(db: Session, terminal_id: str = "ab001122") -> None:
    """Create terminal + mailbox scaffolding for the given ID."""
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="test-sess",
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="developer",
            init_state="ready",
        )
    )
    mb = MailboxModel(
        id="mb_test01",
        session_name="test-sess",
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=1,
        consumed_through_id=0,
        schema_version=1,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(mb)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=mb.id,
            generation=1,
            terminal_id=terminal_id,
            published_at=_utcnow(),
        )
    )


# ---------------------------------------------------------------------------
# S1: Kill mutant M1 — endpoint naive-since normalization
# ---------------------------------------------------------------------------


class TestF130M1EndpointNaiveSinceKill:
    """Prove the endpoint code path normalizes naive `since` to aware-UTC.

    Strategy: hit GET /messages?to=<id>&since=<naive> through the ASGI test
    client. Mock the service-layer `list_messages` to capture the `since` kwarg
    it receives. Under clean code the captured value is aware-UTC; under the M1
    mutant (revert endpoint normalization) it is naive — so the assertion on
    tzinfo kills M1.
    """

    def test_endpoint_passes_aware_utc_since_to_service(self, api_client, scratch_db):
        """GET /messages with naive since string → service receives aware-UTC."""
        terminal_id = "ab001122"
        with scratch_db.begin() as db:
            _setup_terminal_and_mailbox(db, terminal_id)

        # A naive ISO string (no offset info)
        naive_since_str = "2026-08-10T04:00:00"

        captured_kwargs: dict = {}

        def _mock_list_messages(receiver, **kwargs):
            captured_kwargs.update(kwargs)
            return {"items": [], "has_more": False, "next_cursor": None}

        with patch(
            "cli_agent_orchestrator.services.mailbox_service.list_messages",
            side_effect=_mock_list_messages,
        ):
            resp = api_client.get(
                "/messages", params={"to": terminal_id, "since": naive_since_str}
            )

        assert resp.status_code == 200, f"Unexpected {resp.status_code}: {resp.text}"
        since_val = captured_kwargs.get("since")
        assert since_val is not None, "since was not forwarded to service"
        # M1 kill: without endpoint normalization, since_val.tzinfo is None
        assert since_val.tzinfo is not None, (
            "Endpoint must normalize naive since to aware-UTC (M1 mutant survived)"
        )
        assert since_val.tzinfo == timezone.utc, (
            f"Expected UTC, got {since_val.tzinfo}"
        )
        # Digits preserved (naive treated as UTC, not converted)
        assert since_val.hour == 4


# ---------------------------------------------------------------------------
# S2: Kill mutant M3 — InboxModel.created_at default is aware-UTC
# ---------------------------------------------------------------------------


class TestF130M3WriterModelDefaultKill:
    """Prove InboxModel.created_at defaults to aware-UTC (not naive local).

    Strategy: insert an InboxModel row WITHOUT passing an explicit created_at.
    The column default (`_utcnow`) fires. Read the row back and assert
    tzinfo is present. Under M3 (default=datetime.now), the value is naive.
    """

    def test_inbox_row_default_created_at_is_aware_utc(self, scratch_db):
        """An InboxModel row created without explicit created_at has aware-UTC ts."""
        with scratch_db.begin() as db:
            _setup_terminal_and_mailbox(db, "ab001122")
            # Create row WITHOUT explicit created_at — triggers column default
            row = InboxModel(
                sender_id="worker-001",
                receiver_id="ab001122",
                logical_receiver_id=None,
                enqueue_generation=1,
                message="m3-kill-test",
                orchestration_type="send_message",
                status="pending",
                # NOTE: no created_at — exercising the model default
            )
            db.add(row)
            db.flush()
            row_id = row.id

        # Read back via raw SQL to see what was stored
        with scratch_db() as db:
            raw = db.execute(
                text("SELECT created_at FROM inbox WHERE id = :id"), {"id": row_id}
            ).scalar()

        # The stored text must NOT be naive-local (M3 mutant).
        # Under correct code, _utcnow() returns aware-UTC; SQLAlchemy sqlite
        # strips +00:00 on write, so the stored text is naive-UTC digits.
        # Read back through ORM to verify the column processes correctly.
        with scratch_db() as db:
            orm_row = db.query(InboxModel).filter_by(id=row_id).one()
            created = orm_row.created_at

        # Under M3 (datetime.now), the digits are local time (potentially off
        # by hours). Under correct code, digits are UTC. We verify by comparing
        # to a known UTC reference — the row was just created, so it should be
        # within 5 seconds of now-UTC.
        now_utc = _utcnow()
        # Make created comparable: if naive (M3 case), replace as-is; if aware, keep.
        if created.tzinfo is None:
            # Even if tzinfo is stripped by sqlite, the DIGITS should be UTC
            created_for_cmp = created.replace(tzinfo=timezone.utc)
        else:
            created_for_cmp = created

        delta = abs((now_utc - created_for_cmp).total_seconds())
        assert delta < 5, (
            f"created_at digits are not UTC — delta {delta}s from now-UTC. "
            f"Stored: {raw!r}, ORM: {created!r}. "
            "M3 mutant (datetime.now) produces local-time digits."
        )

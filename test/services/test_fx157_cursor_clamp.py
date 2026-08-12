"""fx157 AC1–AC6: cursor clamp on list_messages default page."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
)
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.mailbox_service import list_messages


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fx157.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _terminal(db, tid: str) -> None:
    db.add(
        TerminalModel(
            id=tid,
            tmux_session="s",
            tmux_window=tid,
            provider="codex",
            agent_profile="code_supervisor",
            init_state="ready",
        )
    )


def _mailbox(db, consumed_through: int = 0) -> MailboxModel:
    row = MailboxModel(
        id="mb_fx157aaa",
        session_name="cao-fx157",
        role="supervisor",
        current_terminal_id="sup00001",
        generation=1,
        consumed_through_id=consumed_through,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=1,
            terminal_id="sup00001",
            published_at=datetime.now(timezone.utc),
        )
    )
    return row


def _inbox_row(db, receiver: str, status: str = "pending", *, logical: str | None = None) -> InboxModel:
    row = InboxModel(
        sender_id="worker01",
        receiver_id=receiver,
        logical_receiver_id=logical,
        message="hello",
        orchestration_type="send_message",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


class TestAC1CursorClamp:
    """Bare mailbox list returns only rows with id > consumed_through_id."""

    def test_clamp_excludes_sub_cursor_rows(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "delivered", logical="mb_fx157aaa")
            r2 = _inbox_row(db, "sup00001", "delivered", logical="mb_fx157aaa")
            r3 = _inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa")
        # Advance cursor to r2
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = r2.id
        page = list_messages("mb_fx157aaa")
        ids = [item["id"] for item in page["items"]]
        assert r1.id not in ids
        assert r2.id not in ids
        assert r3.id in ids
        assert len(ids) == 1


class TestAC2AuditPaths:
    """Both audit paths still reach consumed rows."""

    def test_audit_browse_reaches_consumed(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "delivered", logical="mb_fx157aaa")
            r2 = _inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa")
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = r1.id
        page = list_messages("mb_fx157aaa", audit_browse=True)
        ids = {item["id"] for item in page["items"]}
        assert r1.id in ids
        assert r2.id in ids

    def test_explicit_after_id_zero_reaches_consumed(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "delivered", logical="mb_fx157aaa")
            r2 = _inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa")
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = r1.id
        page = list_messages("mb_fx157aaa", after_id=0)
        ids = {item["id"] for item in page["items"]}
        assert r1.id in ids
        assert r2.id in ids


class TestAC3Pagination:
    """Walking after_id=next_after_id from a clamped page yields correct order."""

    def test_paginated_walk_stays_above_cursor(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            rows = []
            for _ in range(5):
                rows.append(_inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa"))
        # Consume first 2
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = rows[1].id
        # Walk with limit=2
        page1 = list_messages("mb_fx157aaa", limit=2)
        assert len(page1["items"]) == 2
        assert page1["has_more"] is True
        assert all(item["id"] > rows[1].id for item in page1["items"])
        # Page 2
        page2 = list_messages("mb_fx157aaa", after_id=page1["next_after_id"], limit=2)
        assert len(page2["items"]) == 1
        assert page2["has_more"] is False
        assert page2["items"][0]["id"] == rows[4].id


class TestAC4HighWaterCursor:
    """Cursor at high-water yields empty page, not error."""

    def test_cursor_at_max_returns_empty(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "delivered", logical="mb_fx157aaa")
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = r1.id
        page = list_messages("mb_fx157aaa")
        assert page["items"] == []
        assert page["has_more"] is False
        assert page["next_after_id"] is None


class TestAC5TerminalReceiver:
    """Clamp does not apply to terminal-id receiver."""

    def test_terminal_receiver_unbounded(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "delivered")
            r2 = _inbox_row(db, "sup00001", "pending")
        # Advance cursor
        with scratch_db.begin() as db:
            mb = db.get(MailboxModel, "mb_fx157aaa")
            mb.consumed_through_id = r1.id
        # Terminal-id receiver — no clamp
        page = list_messages("sup00001")
        ids = {item["id"] for item in page["items"]}
        assert r1.id in ids
        assert r2.id in ids


class TestAC6FreshMailbox:
    """Fresh mailbox (consumed_through_id=0) is byte-identical to pre-fix."""

    def test_fresh_mailbox_returns_all(self, scratch_db):
        with scratch_db.begin() as db:
            _terminal(db, "sup00001")
            _mailbox(db, consumed_through=0)
            r1 = _inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa")
            r2 = _inbox_row(db, "sup00001", "pending", logical="mb_fx157aaa")
        page = list_messages("mb_fx157aaa")
        ids = [item["id"] for item in page["items"]]
        assert ids == [r1.id, r2.id]
        assert page["has_more"] is False

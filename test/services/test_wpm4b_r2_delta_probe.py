"""Promoted reviewer delta probe for WPM4-B Wave 1 design gate r2.

Claim attacked: the orphan anti-join may directly settle DELIVERING inbox rows
while open attempts remain owned by WPM2 recovery.

Expected amended semantics: the orphan helper settles PENDING only; every
DELIVERING row is settled through its owning attempt's existing WPM2 CAS.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    begin_delivery_attempt,
    recover_wpm2_stale_attempt,
    settle_pending_orphan_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm4b-r2.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def test_pending_orphan_helper_never_settles_delivering_attempt(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "payload")
    attempt_uuid = begin_delivery_attempt([message], "receiver", "claude_code", "wire-hash", 7)
    with scratch_db.begin() as db:
        attempt = db.get(InboxDeliveryAttemptModel, attempt_uuid)
        attempt.started_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)

    result = settle_pending_orphan_messages()
    assert result.settled_count == 0
    with scratch_db() as db:
        attempt = db.get(InboxDeliveryAttemptModel, attempt_uuid)
        assert attempt.settled_at is None
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERING.value


def test_wpm2_receiver_gone_settles_attempt_reason_and_notice_atomically(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "payload")
    attempt_uuid = begin_delivery_attempt([message], "receiver", "claude_code", "wire-hash", 7)
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)

    assert (
        recover_wpm2_stale_attempt(
            attempt_uuid,
            [message.id],
            MessageStatus.DELIVERY_FAILED,
            "failed",
            "receiver_gone",
            {},
        )
        == "settled"
    )
    assert (
        recover_wpm2_stale_attempt(
            attempt_uuid,
            [message.id],
            MessageStatus.DELIVERY_FAILED,
            "failed",
            "receiver_gone",
            {},
        )
        == "stale"
    )
    with scratch_db() as db:
        attempt = db.get(InboxDeliveryAttemptModel, attempt_uuid)
        row = db.get(InboxModel, message.id)
        notices = (
            db.query(InboxModel)
            .filter(
                InboxModel.message.startswith(f"p5-orphan receiver=receiver batch={message.id}\n")
            )
            .all()
        )
        assert attempt.settled_at is not None
        assert row.status == MessageStatus.DELIVERY_FAILED.value
        assert row.failure_reason == "receiver_gone"
        assert len(notices) == 1
        assert notices[0].receiver_id == "sender"

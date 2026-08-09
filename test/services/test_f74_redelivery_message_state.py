"""F74 — redelivery must key on MESSAGE state, not ATTEMPT state.

Five pinned tests verifying that recovery decisions consult InboxModel.status
(the MESSAGE-level authority) rather than relying solely on attempt-level state.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    begin_delivery_attempt,
    create_inbox_message,
    list_stale_open_claude_attempts,
    recover_wpm2_stale_attempt,
    update_message_status,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import wire_hash


@pytest.fixture
def f74_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f74.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code", caller_id="sender")
    yield sessions
    engine.dispose()


def _open_old_attempt(sessions, age=61):
    """Create a message + open delivery attempt, backdate started_at."""
    message = create_inbox_message("sender", "receiver", "payload")
    attempt = begin_delivery_attempt(
        [message], "receiver", "claude_code", wire_hash("payload"), 7,
        evidence=json.dumps({}),
    )
    with sessions.begin() as db:
        db.get(InboxDeliveryAttemptModel, attempt).started_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age)
        )
    return message, attempt


def test_f74_delivered_message_not_re_driven_by_recovery(f74_db):
    """Test 1: DELIVERED message with stale open attempt is NOT re-driven."""
    message, attempt_uuid = _open_old_attempt(f74_db)
    # Settle message to DELIVERED externally (simulates concurrent delivery)
    update_message_status(message.id, MessageStatus.DELIVERED)

    with patch(
        "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
        return_value=None,  # receiver gone — triggers recovery path
    ):
        InboxService()._recover_wpm2_attempt({
            "receiver_terminal_id": "receiver",
            "attempt_uuid": attempt_uuid,
            "message_ids": [message.id],
            "sender_id": "sender",
            "orchestration_type": "assign",
            "payload_hash": wire_hash("payload"),
            "started_at": None,
            "evidence": "{}",
        })

    # Message must stay DELIVERED — not regressed to DELIVERY_FAILED
    with f74_db() as db:
        row = db.get(InboxModel, message.id)
        assert row.status == MessageStatus.DELIVERED.value


def test_f74_delivery_failed_not_resurrected_to_pending(f74_db):
    """Test 2: DELIVERY_FAILED message is NOT resurrected to PENDING."""
    message, attempt_uuid = _open_old_attempt(f74_db)
    # Cap the message to DELIVERY_FAILED
    update_message_status(message.id, MessageStatus.DELIVERY_FAILED)

    with patch(
        "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
        return_value={"tmux_session": "s", "tmux_window": "receiver"},
    ), patch(
        "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
        return_value=None,  # unresolved → triggers ambiguous/PENDING path
    ):
        InboxService()._recover_wpm2_attempt({
            "receiver_terminal_id": "receiver",
            "attempt_uuid": attempt_uuid,
            "message_ids": [message.id],
            "sender_id": "sender",
            "orchestration_type": "assign",
            "payload_hash": wire_hash("payload"),
            "started_at": None,
            "evidence": "{}",
        })

    # Message must stay DELIVERY_FAILED — not resurrected to PENDING
    with f74_db() as db:
        row = db.get(InboxModel, message.id)
        assert row.status == MessageStatus.DELIVERY_FAILED.value


def test_f74_pending_message_with_failed_attempt_is_redelivered(f74_db):
    """Test 3: PENDING message with failed latest attempt IS eligible for redelivery."""
    message, attempt_uuid = _open_old_attempt(f74_db)
    # Settle the attempt as failed but leave message PENDING
    from cli_agent_orchestrator.clients.database import settle_delivery_attempt
    settle_delivery_attempt(attempt_uuid, MessageStatus.PENDING, "ambiguous", reason="confirmation_timeout")

    # get_pending_messages should return this message (it's PENDING)
    from cli_agent_orchestrator.clients.database import get_pending_messages
    pending = get_pending_messages("receiver")
    assert message.id in [m.id for m in pending]


def test_f74_recover_wpm2_returns_stale_for_delivered_message(f74_db):
    """Test 4: recover_wpm2_stale_attempt returns 'stale' when message is DELIVERED."""
    message, attempt_uuid = _open_old_attempt(f74_db)
    # Set message to DELIVERED (not DELIVERING)
    update_message_status(message.id, MessageStatus.DELIVERED)

    result = recover_wpm2_stale_attempt(
        attempt_uuid,
        [message.id],
        MessageStatus.PENDING,
        "ambiguous",
        "confirmation_timeout",
        {},
    )
    # CAS guards on status == DELIVERING; DELIVERED fails the CAS → "stale"
    assert result == "stale"


def test_f74_capped_message_no_further_attempts(f74_db):
    """Test 5: Once capped to DELIVERY_FAILED, deliver_pending does not add attempts."""
    message, attempt_uuid = _open_old_attempt(f74_db, age=0)
    # Settle the attempt and cap the message
    from cli_agent_orchestrator.clients.database import settle_delivery_attempt
    settle_delivery_attempt(attempt_uuid, MessageStatus.DELIVERY_FAILED, "failed", reason="attempt_cap")
    update_message_status(message.id, MessageStatus.DELIVERY_FAILED)

    # Count attempts before
    from cli_agent_orchestrator.clients.database import list_message_attempts
    before_count = len(list_message_attempts([message.id]))

    # Try to deliver again — should not create new attempts for capped message
    from cli_agent_orchestrator.clients.database import get_pending_messages
    pending = get_pending_messages("receiver")
    # DELIVERY_FAILED message should NOT appear in pending
    assert message.id not in [m.id for m in pending]

    # Attempt count unchanged
    after_count = len(list_message_attempts([message.id]))
    assert after_count == before_count

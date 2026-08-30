"""F578 D23 — opt-in message expire_after_s / supersede_key row-state transitions.

AC10: expiry is a row-state transition applied by an unconditional sweep pass
ahead of the receiver loop's grace floor; supersede transitions earlier
undelivered peers at enqueue; both leave EVERY pending-row query at once; rows
without the fields behave byte-identically to today.
"""

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    _utcnow,
    create_inbox_message,
    create_terminal,
    expire_pending_rows,
    get_pending_messages,
    list_expired_pending_rows,
    list_pending_receiver_ids,
    list_pending_receiver_ids_older_than,
    list_pending_receiver_ids_with_terminal,
    list_stalled_direct_pending_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def db_env(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.clear_terminal_metadata_cache()
    # send-side callback guard needs a no-op context for a bare unit DB.
    create_terminal("sup", "cao-t", "w-sup", "claude_code")
    create_terminal("wrk", "cao-t", "w-wrk", "claude_code")
    return sessions


def _status(db_env, msg_id):
    from cli_agent_orchestrator.clients.database import InboxModel

    with db_env() as db:
        return db.query(InboxModel).filter(InboxModel.id == msg_id).one().status


# ---- byte-identical default (no fields) -----------------------------------


def test_row_without_fields_is_plain_pending(db_env):
    msg = create_inbox_message("sup", "wrk", "hello")
    assert msg.status == MessageStatus.PENDING
    assert msg.expire_after_s is None
    assert msg.supersede_key is None
    # No expiry candidate.
    assert list_expired_pending_rows() == []


# ---- expire_after_s -------------------------------------------------------


def test_expiry_candidate_only_after_deadline(db_env):
    msg = create_inbox_message("sup", "wrk", "ephemeral", expire_after_s=5)
    # Not yet elapsed.
    assert list_expired_pending_rows(now=_utcnow()) == []
    # Past the deadline.
    later = _utcnow() + timedelta(seconds=6)
    assert list_expired_pending_rows(now=later) == [msg.id]


def test_expire_pending_rows_transitions_to_expired(db_env):
    msg = create_inbox_message("sup", "wrk", "ephemeral", expire_after_s=5)
    later = _utcnow() + timedelta(seconds=6)
    ids = list_expired_pending_rows(now=later)
    assert expire_pending_rows(ids) == 1
    assert _status(db_env, msg.id) == MessageStatus.EXPIRED.value


def test_expired_row_leaves_every_pending_query(db_env):
    """AC10: an expired row stops being returned by each pending-row query."""
    msg = create_inbox_message("sup", "wrk", "ephemeral", expire_after_s=1)
    # Before expiry it is pending on the doorbell / older-than / with-terminal feeds.
    assert "wrk" in list_pending_receiver_ids()
    later = _utcnow() + timedelta(seconds=2)
    expire_pending_rows(list_expired_pending_rows(now=later))
    assert "wrk" not in list_pending_receiver_ids()
    assert "wrk" not in list_pending_receiver_ids_with_terminal()
    assert "wrk" not in list_pending_receiver_ids_older_than(0)
    assert [m.id for m in get_pending_messages("wrk")] == []
    assert [m.id for m in list_stalled_direct_pending_messages(0) if m.id == msg.id] == []


def test_expire_pending_rows_skips_already_terminal(db_env):
    msg = create_inbox_message("sup", "wrk", "ephemeral", expire_after_s=1)
    later = _utcnow() + timedelta(seconds=2)
    assert expire_pending_rows([msg.id]) == 1
    # A second pass is a no-op (already expired, not pending).
    assert expire_pending_rows([msg.id]) == 0


# ---- supersede_key --------------------------------------------------------


def test_supersede_at_enqueue_transitions_earlier_peer(db_env):
    first = create_inbox_message("sup", "wrk", "v1", supersede_key="status")
    assert _status(db_env, first.id) == MessageStatus.PENDING.value
    second = create_inbox_message("sup", "wrk", "v2", supersede_key="status")
    # Only the latest remains pending; the earlier is superseded.
    assert _status(db_env, first.id) == MessageStatus.SUPERSEDED.value
    assert _status(db_env, second.id) == MessageStatus.PENDING.value


def test_supersede_only_within_same_key(db_env):
    a = create_inbox_message("sup", "wrk", "a", supersede_key="keyA")
    b = create_inbox_message("sup", "wrk", "b", supersede_key="keyB")
    c = create_inbox_message("sup", "wrk", "c", supersede_key="keyA")
    # keyB is untouched; only the earlier keyA row is superseded.
    assert _status(db_env, a.id) == MessageStatus.SUPERSEDED.value
    assert _status(db_env, b.id) == MessageStatus.PENDING.value
    assert _status(db_env, c.id) == MessageStatus.PENDING.value


def test_supersede_leaves_only_latest_deliverable(db_env):
    create_inbox_message("sup", "wrk", "old", supersede_key="k")
    latest = create_inbox_message("sup", "wrk", "new", supersede_key="k")
    pending = get_pending_messages("wrk")
    assert [m.id for m in pending] == [latest.id]


def test_no_supersede_key_never_supersedes(db_env):
    a = create_inbox_message("sup", "wrk", "a")
    b = create_inbox_message("sup", "wrk", "b")
    assert _status(db_env, a.id) == MessageStatus.PENDING.value
    assert _status(db_env, b.id) == MessageStatus.PENDING.value

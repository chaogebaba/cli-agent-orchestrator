"""F44-T6 — recovery sweep classifies execution evidence before resurrection.

The stale-open recovery sweep (`_recover_wpm2_attempt`) previously wrote
PENDING/ambiguous/confirmation_timeout for a stale submission without consulting
the execution evidence F44 proved is the correct signal. These tests pin the new
classify-then-settle step: a stale submission whose opener evidence shows the
payload executed now settles inferred-delivered instead of being re-driven.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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
    create_terminal,
    get_message_trace,
    get_pending_messages,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import wire_hash


@pytest.fixture
def f44t6_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f44t6.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    create_terminal("sender", "s", "sender", "codex")
    create_terminal("receiver", "s", "receiver", "claude_code", caller_id="sender")
    yield sessions
    engine.dispose()


def _open_old_attempt(sessions, payload="payload", age=61):
    """Create a message + stale open delivery attempt."""
    message = create_inbox_message("sender", "receiver", payload)
    attempt = begin_delivery_attempt(
        [message],
        "receiver",
        "claude_code",
        wire_hash(payload),
        7,
        evidence=json.dumps({}),
    )
    with sessions.begin() as db:
        db.get(InboxDeliveryAttemptModel, attempt).started_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=age)
    return message, attempt


def _opener_evidence(path) -> dict:
    """Opener evidence shaped like `begin_delivery_attempt` persists."""
    stat = os.stat(path)
    return {
        "path": str(path),
        "inode": stat.st_ino,
        "size": stat.st_size,
        "resolution_kind": "exact_id",
    }


def _attempt_dict(attempt_uuid, message_ids, payload, evidence):
    return {
        "receiver_terminal_id": "receiver",
        "attempt_uuid": attempt_uuid,
        "message_ids": message_ids,
        "sender_id": "sender",
        "orchestration_type": OrchestrationType.ASSIGN.value,
        "payload_hash": wire_hash(payload),
        "started_at": None,
        "evidence": json.dumps(evidence),
    }


def _patch_unresolved_lookup(monkeypatch, metadata):
    """Metadata present, lookup non-hit → reaches F74 check + evidence step."""
    monkeypatch.setattr(
        inbox_module,
        "get_terminal_metadata",
        lambda _term: metadata,
    )
    monkeypatch.setattr(
        inbox_module,
        "resolve_session_transcript",
        lambda _meta: None,
    )
    monkeypatch.setattr(inbox_module, "_wpm2_lookup", lambda *_a, **_k: ("unresolved", {}))


def test_t1_evidence_positive_recovers_settles_inferred_delivered(f44t6_db, tmp_path, monkeypatch):
    """A stale submission whose transcript grew at PROCESSING settles DELIVERED."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    with open(transcript, "ab") as stream:
        stream.write(b"y" * 500)  # grow in place, same inode

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service._recover_wpm2_attempt(_attempt_dict(attempt_uuid, [message.id], "payload", evidence))

    expected = {**evidence, "kind": "execution_evidence"}
    expected["transcript_growth"] = {
        "size_at_open": 1000,
        "size_at_settle": 1500,
        "inode_stable": True,
    }
    expected["receiver_status_at_settle"] = "processing"
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert (trace["attempts"][0]["outcome"], trace["attempts"][0]["reason"]) == (
        "confirmed",
        "inferred_by_execution",
    )
    assert trace["attempts"][0]["evidence"] == expected
    service._commit_watchdog_ops.assert_called_once()
    assert message.id not in [m.id for m in get_pending_messages("receiver")]


def test_t2_flat_transcript_fail_closes_to_pending(f44t6_db, tmp_path, monkeypatch):
    """No transcript growth → predicate False → legacy PENDING resurrection."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "flat.jsonl"
    transcript.write_bytes(b"x" * 1000)  # not grown
    evidence = _opener_evidence(transcript)

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["outcome"] == "ambiguous"
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"
    assert message.id in [m.id for m in get_pending_messages("receiver")]


def test_t3_terminal_message_untouched_f74_pin(f44t6_db, tmp_path, monkeypatch):
    """A member already DELIVERED → no settle at all (neither path)."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    with open(transcript, "ab") as stream:
        stream.write(b"y" * 500)
    from cli_agent_orchestrator.clients.database import update_message_status

    update_message_status(message.id, MessageStatus.DELIVERED)

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert trace["attempts"][0]["outcome"] is None  # attempt never settled


def test_t4_cas_race_no_double_settle(f44t6_db, tmp_path, monkeypatch):
    """Resurrection already settled → inferred-delivered CAS fails → no double settle."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    with open(transcript, "ab") as stream:
        stream.write(b"y" * 500)

    # First settle wins: resurrection write to PENDING (message no longer DELIVERING).
    from cli_agent_orchestrator.clients.database import recover_wpm2_stale_attempt

    assert (
        recover_wpm2_stale_attempt(
            attempt_uuid,
            [message.id],
            MessageStatus.PENDING,
            "ambiguous",
            "confirmation_timeout",
            {},
        )
        == "settled"
    )

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    # Exactly one settle won: message stays PENDING, attempt stays ambiguous.
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"


def test_t5_receiver_gone_f74_first_line_intact(f44t6_db, tmp_path, monkeypatch):
    """Metadata absent → receiver_gone branch fires before evidence classification."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    with open(transcript, "ab") as stream:
        stream.write(b"y" * 500)

    monkeypatch.setattr(
        inbox_module,
        "get_terminal_metadata",
        lambda _term: None,
    )
    classify = MagicMock(wraps=inbox_module._classify_probable_delivery)
    monkeypatch.setattr(inbox_module, "_classify_probable_delivery", classify)

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    classify.assert_not_called()
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERY_FAILED.value
    assert trace["attempts"][0]["reason"] == "receiver_gone"


@pytest.mark.parametrize(
    "status",
    [
        "unknown",  # sampler failure
        "idle",  # receiver went idle after submit
        "error",
    ],
)
def test_t6_weak_evidence_fail_closed(f44t6_db, tmp_path, monkeypatch, status):
    """Non-executing receiver status → predicate False → resurrection."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    with open(transcript, "ab") as stream:
        stream.write(b"y" * 500)

    monitor = MagicMock()
    if status == "unknown":
        monitor.get_status.side_effect = RuntimeError("sampler failure")
    else:
        monitor.get_status.return_value = TerminalStatus(status)
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"


def test_t6_inode_changed_fail_closed(f44t6_db, tmp_path, monkeypatch):
    """Inode rotated → growth inode_stable False → resurrection."""
    message, attempt_uuid = _open_old_attempt(f44t6_db)
    transcript = tmp_path / "grown.jsonl"
    transcript.write_bytes(b"x" * 1000)
    evidence = _opener_evidence(transcript)
    # Rotate the inode: rewrite via a new file so st_ino differs from the opener ref.
    rotated = tmp_path / "rotated.jsonl"
    rotated.write_bytes(b"x" * 1000 + b"y" * 500)
    evidence["inode"] = os.stat(rotated).st_ino

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    _patch_unresolved_lookup(monkeypatch, {"tmux_session": "s", "tmux_window": "receiver"})

    InboxService()._recover_wpm2_attempt(
        _attempt_dict(attempt_uuid, [message.id], "payload", evidence)
    )

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"

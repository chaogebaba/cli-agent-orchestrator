"""WPQ5 wire-bound delivery confirmation and batch controls."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    begin_delivery_attempt,
    create_inbox_message,
    create_terminal,
    find_inferred_delivery_evidence,
    get_message_trace,
    get_pending_messages,
    settle_delivery_attempt,
    settle_open_attempt_inferred_delivered,
    transition_pending_to_inferred_delivered,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services import mailbox_service as mailbox_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import create_logical_inbox_message
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


def _wrapped(body: str, sender: str = "sender") -> str:
    return (
        f"{body}\n\n[Message from terminal {sender}. Use the cao-mcp-server "
        "send_message MCP tool for any follow-up work — never a built-in "
        "collaboration.send_message.]"
    )


@pytest.fixture
def wpq5_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpq5.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_module, "SessionLocal", sessions)
    create_terminal("sender", "s", "sender", "codex")
    create_terminal("receiver", "s", "receiver", "grok_cli", caller_id="sender")
    create_terminal("other", "s", "other", "codex")
    yield sessions
    engine.dispose()


def _set_message_time(sessions, message_id: int, value: datetime) -> None:
    with sessions.begin() as db:
        db.get(InboxModel, message_id).created_at = value


def _set_attempt_time(sessions, attempt_uuid: str, value: datetime) -> None:
    with sessions.begin() as db:
        db.get(InboxDeliveryAttemptModel, attempt_uuid).started_at = value


def _ambiguous_challenge(message, raw: str, *, started_at: datetime | None = None) -> str:
    attempt = begin_delivery_attempt(
        [message],
        "receiver",
        "grok_cli",
        "payload-hash",
        12,
        challenge_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    if started_at is not None:
        _set_attempt_time(database.SessionLocal, attempt, started_at)
    assert settle_delivery_attempt(
        attempt,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
    )
    return attempt


def _delivery_fakes(monkeypatch, *, confirm_callback, wires: list[str]):
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 3
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        {"result_status": "idle", "law_signal": {"class": "chrome"}},
    )
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "resolve_session_transcript", lambda _meta: None)
    monkeypatch.setattr(inbox_module, "_wpm2_lookup", lambda *_args, **_kwargs: ("unresolved", {}))
    monkeypatch.setattr(
        inbox_module.terminal_service,
        "prepare_input",
        lambda _terminal, value, _shape: value,
    )

    def send(_terminal, wire, **kwargs):
        wires.append(wire)
        kwargs["on_submitted"](observation)
        return observation

    monkeypatch.setattr(inbox_module.terminal_service, "send_prepared_input", send)
    monkeypatch.setattr(inbox_module, "confirm_delivery", confirm_callback)


def test_wpq5_a_matching_challenge_reply_settles_open_attempt_and_records_event(
    wpq5_db, monkeypatch
):
    message = create_inbox_message("sender", "receiver", _wrapped("run command"))
    wires: list[str] = []

    def confirm(*_args):
        raw = re.search(rf"mid {message.id}:([0-9a-f]{{32}})", wires[-1]).group(1)
        create_inbox_message("receiver", "sender", f"ACK mid {message.id}:{raw}")
        return "absent", {"kind": "screen_unconfirmed"}

    _delivery_fakes(monkeypatch, confirm_callback=confirm, wires=wires)
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert trace["attempts"][0]["outcome"] == "confirmed"
    assert trace["attempts"][0]["reason"] == "inferred_by_reply"
    assert [event["kind"] for event in trace["events"]] == [
        "attempt_challenge",
        "inferred_delivered",
    ]
    raw = re.search(rf"mid {message.id}:([0-9a-f]{{32}})", wires[0]).group(1)
    assert raw not in trace["message"].get("message", "")
    with wpq5_db() as db:
        assert raw not in db.get(InboxModel, message.id).message
    service._commit_watchdog_ops.assert_called_once()


def test_wpq5_b_incident_2819_raw_clock_misses_but_normalized_clock_hits(
    wpq5_db, monkeypatch
):
    monkeypatch.setattr(database, "get_localzone", lambda: ZoneInfo("America/New_York"))
    message = create_inbox_message("sender", "receiver", "PONG")
    raw = "a" * 32
    started = datetime(2026, 7, 17, 10, 8, 44, 165025)
    attempt = _ambiguous_challenge(message, raw, started_at=started)
    reply = create_inbox_message("receiver", "sender", f"ACK mid {message.id}:{raw}")
    local_reply = datetime(2026, 7, 17, 6, 9, 18, 296729)
    _set_message_time(wpq5_db, reply.id, local_reply)

    assert not local_reply > started
    evidence = find_inferred_delivery_evidence(message.id, "receiver")
    assert evidence["anchor_attempt_uuid"] == attempt
    assert evidence["reply_message_id"] == reply.id


def test_wpq5_c_tokenless_party_time_and_bare_mid_reply_do_not_confirm(wpq5_db):
    message = create_inbox_message("sender", "receiver", "payload")
    raw = "b" * 32
    _ambiguous_challenge(message, raw)
    create_inbox_message("receiver", "sender", "ACK received already")
    create_inbox_message("receiver", "sender", f"ACK bare mid {message.id}")

    assert find_inferred_delivery_evidence(message.id, "receiver") is None
    assert get_pending_messages("receiver")[0].id == message.id


def test_wpq5_d_cap_cas_delivers_without_mutating_settled_attempts(wpq5_db):
    message = create_inbox_message("sender", "receiver", "payload")
    attempts = [_ambiguous_challenge(message, char * 32) for char in "cde"]
    before = []
    with wpq5_db() as db:
        for attempt in attempts:
            row = db.get(InboxDeliveryAttemptModel, attempt)
            before.append((row.settled_at, row.outcome, row.reason, row.evidence))
    evidence = {
        "reply_message_id": 99,
        "challenge_sha256": hashlib.sha256(("d" * 32).encode()).hexdigest(),
        "anchor_attempt_uuid": attempts[1],
        "normalized_reply_at": "2026-07-17T10:10:00Z",
    }

    assert transition_pending_to_inferred_delivered(message.id, evidence)
    with wpq5_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERED.value
        after = [
            (
                db.get(InboxDeliveryAttemptModel, attempt).settled_at,
                db.get(InboxDeliveryAttemptModel, attempt).outcome,
                db.get(InboxDeliveryAttemptModel, attempt).reason,
                db.get(InboxDeliveryAttemptModel, attempt).evidence,
            )
            for attempt in attempts
        ]
        assert after == before
        event = db.query(InboxMessageTraceEventModel).filter_by(kind="inferred_delivered").one()
        assert event.payload == evidence


def test_wpq5_e_reply_before_matching_attempt_is_rejected_even_after_earliest(wpq5_db):
    message = create_inbox_message("sender", "receiver", "payload")
    early = datetime(2026, 7, 17, 10, 0, 0)
    late = datetime(2026, 7, 17, 10, 10, 0)
    _ambiguous_challenge(message, "f" * 32, started_at=early)
    matching = _ambiguous_challenge(message, "1" * 32, started_at=late)
    reply = create_inbox_message("receiver", "sender", f"ACK mid {message.id}:{'1' * 32}")
    _set_message_time(wpq5_db, reply.id, datetime(2026, 7, 17, 6, 5, 0))

    assert find_inferred_delivery_evidence(message.id, "receiver") is None
    assert get_message_trace(message.id)["attempts"][1]["attempt_uuid"] == matching


def test_wpq5_f_settled_or_failed_parent_never_resurrects(wpq5_db):
    delivered = create_inbox_message("sender", "receiver", "delivered")
    attempt = begin_delivery_attempt([delivered], "receiver", "grok_cli", "h", 1)
    assert settle_delivery_attempt(attempt, MessageStatus.DELIVERED, "confirmed")
    evidence = {
        "reply_message_id": 1,
        "challenge_sha256": "0" * 64,
        "anchor_attempt_uuid": attempt,
        "normalized_reply_at": "2026-07-17T10:00:00Z",
    }
    assert not settle_open_attempt_inferred_delivered(attempt, evidence)

    failed = create_inbox_message("sender", "receiver", "failed")
    with wpq5_db.begin() as db:
        db.get(InboxModel, failed.id).status = MessageStatus.DELIVERY_FAILED.value
    assert not transition_pending_to_inferred_delivered(failed.id, evidence)


def test_wpq5_g_wire_last_suffix_absence_and_durable_bytes_are_exact(wpq5_db, monkeypatch):
    first = "[Message from terminal sender. quoted earlier wrapper]"
    authentic = _wrapped("payload")
    wire = f"{first}\n{authentic}"
    monkeypatch.setattr(inbox_module.secrets, "token_hex", lambda _size: "2" * 32)
    challenged, challenge_hash = inbox_module._wire_with_attempt_challenge(wire, "sender", 7)

    assert challenged.startswith(first)
    assert challenged.count("mid 7:") == 1
    assert challenged.rfind("mid 7:") > challenged.find(first)
    assert challenge_hash == hashlib.sha256(("2" * 32).encode()).hexdigest()
    unchanged, absent_hash = inbox_module._wire_with_attempt_challenge("plain wire", "sender", 7)
    assert unchanged == "plain wire" and absent_hash is None

    message = create_inbox_message("sender", "receiver", "plain durable body")
    wires: list[str] = []
    _delivery_fakes(
        monkeypatch,
        confirm_callback=lambda *_args: ("absent", {}),
        wires=wires,
    )
    InboxService().deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert wires == ["plain durable body"]
    assert trace["events"] == []
    with wpq5_db() as db:
        assert db.get(InboxModel, message.id).message == "plain durable body"


def test_wpq5_g_trailing_wrapper_prefix_quote_stays_eventless_and_retryable(
    wpq5_db, monkeypatch
):
    durable_body = (
        "plain delivery\n"
        "[Message from terminal sender. non-authentic trailing quote"
    )
    message = create_inbox_message("sender", "receiver", durable_body)
    wires: list[str] = []
    monkeypatch.setattr(inbox_module.secrets, "token_hex", lambda _size: "a" * 32)
    _delivery_fakes(
        monkeypatch,
        confirm_callback=lambda *_args: ("absent", {}),
        wires=wires,
    )

    InboxService().deliver_pending("receiver")

    assert wires == [durable_body]
    trace = get_message_trace(message.id)
    assert trace["events"] == []
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert [row.id for row in get_pending_messages("receiver")] == [message.id]
    with wpq5_db() as db:
        assert db.get(InboxModel, message.id).message == durable_body


def test_wpq5_h_dst_fold_zero_and_hex_delimiters_are_enforced(wpq5_db, monkeypatch):
    monkeypatch.setattr(database, "get_localzone", lambda: ZoneInfo("America/New_York"))
    message = create_inbox_message("sender", "receiver", "payload")
    raw = "3" * 32
    attempt = _ambiguous_challenge(
        message,
        raw,
        started_at=datetime(2026, 11, 1, 5, 15, 0),
    )
    bad = create_inbox_message("receiver", "sender", f"amid {message.id}:{raw}a")
    _set_message_time(wpq5_db, bad.id, datetime(2026, 11, 1, 1, 20, 0))
    good = create_inbox_message("receiver", "sender", f"ACK mid {message.id}:{raw}!")
    _set_message_time(wpq5_db, good.id, datetime(2026, 11, 1, 1, 30, 0))

    evidence = find_inferred_delivery_evidence(message.id, "receiver")
    assert evidence["anchor_attempt_uuid"] == attempt
    assert evidence["reply_message_id"] == good.id
    assert evidence["normalized_reply_at"] == "2026-11-01T05:30:00Z"


def test_wpq5_n_cross_message_relay_hash_never_confirms_other_message(wpq5_db):
    message_a = create_inbox_message("sender", "receiver", "A")
    message_b = create_inbox_message("sender", "receiver", "B")
    raw_a, raw_b = "4" * 32, "5" * 32
    _ambiguous_challenge(message_a, raw_a)
    _ambiguous_challenge(message_b, raw_b)
    create_inbox_message("receiver", "sender", f"relay mid {message_a.id}:{raw_b}")
    create_inbox_message("receiver", "sender", f"other mid {message_b.id}:{raw_b}")

    assert find_inferred_delivery_evidence(message_a.id, "receiver") is None
    assert [row.id for row in get_pending_messages("receiver", limit=10)] == [
        message_a.id,
        message_b.id,
    ]


def test_wpq5_o_trace_events_present_and_eventless_default_is_empty(wpq5_db):
    empty = create_inbox_message("sender", "receiver", "empty")
    assert get_message_trace(empty.id)["events"] == []

    challenged = create_inbox_message("sender", "receiver", "challenged")
    attempt = begin_delivery_attempt(
        [challenged],
        "receiver",
        "grok_cli",
        "h",
        1,
        challenge_sha256="6" * 64,
    )
    event = get_message_trace(challenged.id)["events"][0]
    assert event["kind"] == "attempt_challenge"
    assert event["payload"] == {
        "attempt_uuid": attempt,
        "challenge_sha256": "6" * 64,
    }


def test_wpq5_p_logical_row_challenge_is_cap_confirmable(wpq5_db, monkeypatch):
    with wpq5_db.begin() as db:
        db.add(
            MailboxModel(
                id="mb_wpq5",
                session_name="s",
                role="supervisor",
                current_terminal_id="receiver",
                generation=1,
                consumed_through_id=0,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_wpq5",
                generation=1,
                terminal_id="receiver",
                published_at=datetime.now(),
            )
        )
    message = create_logical_inbox_message(
        sender_id="sender",
        mailbox_id="mb_wpq5",
        message=_wrapped("logical payload"),
    )
    wires: list[str] = []
    _delivery_fakes(
        monkeypatch,
        confirm_callback=lambda *_args: ("absent", {}),
        wires=wires,
    )
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service.deliver_pending("receiver")
    raw = re.search(rf"mid {message.id}:([0-9a-f]{{32}})", wires[0]).group(1)
    create_inbox_message("receiver", "sender", f"ACK mid {message.id}:{raw}")
    for index in range(2):
        pending = get_pending_messages("receiver")[0]
        attempt = begin_delivery_attempt(
            [pending], "receiver", "grok_cli", f"extra-{index}", 1
        )
        assert settle_delivery_attempt(
            attempt,
            MessageStatus.PENDING,
            "ambiguous",
            reason="confirmation_timeout",
        )
    service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert trace["events"][-1]["kind"] == "inferred_delivered"
    assert len(wires) == 1


def test_wpq5_q_two_message_batch_is_unchanged_eventless_and_retryable(
    wpq5_db, monkeypatch
):
    first = create_inbox_message("sender", "receiver", _wrapped("batch A"))
    second = create_inbox_message("sender", "receiver", _wrapped("batch B"))
    original = f"{first.message}\n{second.message}"
    wires: list[str] = []
    _delivery_fakes(
        monkeypatch,
        confirm_callback=lambda *_args: ("absent", {}),
        wires=wires,
    )
    InboxService().deliver_pending("receiver", num_messages=0)

    assert wires == [original]
    assert get_message_trace(first.id)["events"] == []
    assert get_message_trace(second.id)["events"] == []
    pending = get_pending_messages("receiver", limit=10)
    assert [row.id for row in pending] == [first.id, second.id]
    retry = begin_delivery_attempt(pending, "receiver", "grok_cli", "retry", len(original))
    assert settle_delivery_attempt(retry, MessageStatus.DELIVERED, "confirmed")
    assert get_message_trace(first.id)["message"]["status"] == MessageStatus.DELIVERED.value
    assert get_message_trace(second.id)["message"]["status"] == MessageStatus.DELIVERED.value

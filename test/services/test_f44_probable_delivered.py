"""WP-F44 probable-delivered acceptance coverage."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    begin_delivery_attempt,
    create_inbox_message,
    create_terminal,
    get_message_trace,
    settle_attempt_inferred_delivered_batch,
    settle_delivery_attempt,
    settle_delivery_attempt_proof_safe,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import TranscriptResolution
from cli_agent_orchestrator.services.stalled_callback_watchdog import stalled_callback_watchdog
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def f44_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f44.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    create_terminal("caller", "s", "caller", "codex")
    create_terminal("sender", "s", "sender", "codex")
    create_terminal("receiver", "s", "receiver", "grok_cli", caller_id="caller")
    yield sessions
    engine.dispose()


def _qualifying_evidence(*, busy: bool = True) -> dict:
    evidence = {
        "receiver_status_at_settle": "processing",
        "transcript_growth": {
            "size_at_open": 600000,
            "size_at_settle": 668174,
            "inode_stable": True,
        },
    }
    if busy:
        evidence["busy_initial_submit"] = {"status_at_submit": "processing"}
    return evidence


def _attempt_rows(sessions, message_ids: list[int], *, text: str = "payload"):
    messages = [
        create_inbox_message("sender", "receiver", f"{text}-{index}") for index in message_ids
    ]
    attempt = begin_delivery_attempt(messages, "receiver", "grok_cli", "hash", 10)
    return messages, attempt


def _delivery_fakes(monkeypatch, transcript, *, send_error=None, settle_status=None):
    admission = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, 1, 1)
    submitted = BoundaryObservation("epoch", TerminalStatus.PROCESSING, 2, 2, 2, 2, 2)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = admission
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 2
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        {"result_status": "idle", "law_signal": {"class": "chrome"}},
    )
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    monkeypatch.setattr(
        inbox_module,
        "resolve_session_transcript",
        lambda _meta: TranscriptResolution(transcript, "exact_id"),
    )
    monkeypatch.setattr(inbox_module, "_wpm2_lookup", lambda *_a, **_k: ("unresolved", {}))
    monkeypatch.setattr(inbox_module.terminal_service, "prepare_input", lambda _t, value, _s: value)

    def send(_terminal, _wire, **kwargs):
        kwargs["on_submitted"](submitted)
        with transcript.open("ab") as stream:
            stream.write(b"y" * 68174)
        monitor.get_status.return_value = settle_status or TerminalStatus.PROCESSING
        if send_error is not None:
            raise send_error
        return submitted

    monkeypatch.setattr(inbox_module.terminal_service, "send_prepared_input", send)
    return monitor


@pytest.mark.parametrize(
    "evidence, expected",
    [
        (_qualifying_evidence(), True),
        (_qualifying_evidence(busy=False), True),
        ({**_qualifying_evidence(), "receiver_status_at_settle": "idle"}, False),
        (
            {
                **_qualifying_evidence(),
                "transcript_growth": {
                    "size_at_open": 600000,
                    "size_at_settle": 668174,
                    "inode_stable": False,
                },
            },
            False,
        ),
        (
            {
                **_qualifying_evidence(),
                "transcript_growth": {
                    "size_at_open": 600000,
                    "size_at_settle": 600000,
                    "inode_stable": True,
                },
            },
            False,
        ),
        (
            {
                **_qualifying_evidence(),
                "transcript_growth": {
                    "size_at_open": 600000,
                    "size_at_settle": 500000,
                    "inode_stable": True,
                },
            },
            False,
        ),
        ({}, False),
        ({**_qualifying_evidence(), "transcript_growth": None}, False),
        (
            {
                **_qualifying_evidence(),
                "busy_initial_submit": {"status_at_submit": "idle"},
            },
            False,
        ),
    ],
)
def test_t1_probable_delivered_truth_table(evidence, expected):
    assert inbox_module.is_probable_delivered(evidence) is expected


def test_t2_main_path_execution_evidence_settles_once_without_redelivery(
    f44_db, tmp_path, monkeypatch
):
    transcript = tmp_path / "fixture.jsonl"
    transcript.write_bytes(b"x" * 600000)
    message = create_inbox_message("sender", "receiver", "fixture-3529-attempt-1")
    monitor = _delivery_fakes(monkeypatch, transcript)
    timeouts: list[float] = []

    def confirm(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return "ambiguous", {
            "kind": "transcript_absent",
            "path": str(transcript),
            "inode": transcript.stat().st_ino,
            "size": 668174,
        }

    monkeypatch.setattr(inbox_module, "confirm_delivery", confirm)
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    attempt = trace["attempts"][0]
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert (attempt["outcome"], attempt["reason"]) == (
        "confirmed",
        "inferred_by_execution",
    )
    assert attempt["evidence"]["kind"] == "execution_evidence"
    assert attempt["evidence"]["path"] == str(transcript)
    assert attempt["evidence"]["inode"] == transcript.stat().st_ino
    assert attempt["evidence"]["receiver_status_at_settle"] == "processing"
    assert attempt["evidence"]["busy_initial_submit"]["status_at_submit"] == "processing"
    assert attempt["evidence"]["transcript_growth"] == {
        "size_at_open": 600000,
        "size_at_settle": 668174,
        "inode_stable": True,
    }
    assert [event["kind"] for event in trace["events"]] == ["inferred_delivered"]
    service._commit_watchdog_ops.assert_called_once()
    assert timeouts == [10.0]
    service.deliver_pending("receiver")
    assert len(get_message_trace(message.id)["attempts"]) == 1
    assert monitor.get_status.call_count > 0

    late = create_inbox_message("receiver", "caller", "late challenge reply")
    late_attempt = begin_delivery_attempt([late], "caller", "codex", "late", 4)
    assert settle_delivery_attempt(late_attempt, MessageStatus.DELIVERED, "confirmed")
    assert get_message_trace(late.id)["message"]["status"] == MessageStatus.DELIVERED.value
    unchanged = get_message_trace(message.id)
    assert unchanged["message"]["status"] == MessageStatus.DELIVERED.value
    assert len(unchanged["attempts"]) == 1


def test_t2_false_main_path_retains_legacy_pending_flow(f44_db, tmp_path, monkeypatch):
    transcript = tmp_path / "no-growth.jsonl"
    transcript.write_bytes(b"x" * 600000)
    message = create_inbox_message("sender", "receiver", "lost injection")
    _delivery_fakes(monkeypatch, transcript, settle_status=TerminalStatus.IDLE)
    monkeypatch.setattr(
        inbox_module,
        "confirm_delivery",
        lambda *_a, **_k: ("ambiguous", {"kind": "transcript_absent"}),
    )
    InboxService().deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["outcome"] == "ambiguous"
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"


def test_t2b_batch_helper_commits_all_members_events_and_callback_post_commit(f44_db):
    messages, attempt = _attempt_rows(f44_db, [1, 2], text="batch")
    observed: list[list[str]] = []

    def confirmed():
        with f44_db() as db:
            observed.append([db.get(InboxModel, item.id).status for item in messages])

    evidence = {**_qualifying_evidence(), "kind": "execution_evidence"}
    assert settle_attempt_inferred_delivered_batch(
        attempt, [item.id for item in messages], evidence, on_confirmed=confirmed
    )
    assert observed == [[MessageStatus.DELIVERED.value, MessageStatus.DELIVERED.value]]
    with f44_db() as db:
        row = db.get(InboxDeliveryAttemptModel, attempt)
        assert (row.outcome, row.reason) == ("confirmed", "inferred_by_execution")
        assert json.loads(row.evidence) == evidence
        events = (
            db.query(InboxMessageTraceEventModel).order_by(InboxMessageTraceEventModel.id).all()
        )
        assert [event.message_id for event in events] == [item.id for item in messages]
        assert all(event.kind == "inferred_delivered" for event in events)


def test_t2b_exception_arm_settles_two_member_batch(f44_db, tmp_path, monkeypatch):
    transcript = tmp_path / "exception.jsonl"
    transcript.write_bytes(b"x" * 600000)
    first = create_inbox_message("sender", "receiver", "batch-a")
    second = create_inbox_message("sender", "receiver", "batch-b")
    _delivery_fakes(monkeypatch, transcript, send_error=RuntimeError("after submit"))
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service.deliver_pending("receiver", num_messages=0)
    traces = [get_message_trace(first.id), get_message_trace(second.id)]
    assert all(trace["message"]["status"] == MessageStatus.DELIVERED.value for trace in traces)
    assert all(trace["attempts"][0]["reason"] == "inferred_by_execution" for trace in traces)
    assert all(trace["events"][0]["kind"] == "inferred_delivered" for trace in traces)
    service._commit_watchdog_ops.assert_called_once()


def test_t2b_stale_and_rowcount_mismatch_are_atomic_then_legacy_requeues(f44_db, monkeypatch):
    messages, stale_attempt = _attempt_rows(f44_db, [1, 2], text="stale")
    with f44_db.begin() as db:
        db.get(InboxModel, messages[0].id).status = MessageStatus.PENDING.value
    evidence = {**_qualifying_evidence(), "kind": "execution_evidence"}
    assert not settle_attempt_inferred_delivered_batch(
        stale_attempt, [item.id for item in messages], evidence
    )
    assert settle_delivery_attempt_proof_safe(stale_attempt, {}) == "stale"
    with f44_db() as db:
        assert db.get(InboxDeliveryAttemptModel, stale_attempt).settled_at is None
        assert [db.get(InboxModel, item.id).status for item in messages] == [
            MessageStatus.PENDING.value,
            MessageStatus.DELIVERING.value,
        ]

    intact, attempt = _attempt_rows(f44_db, [3, 4], text="rowcount")
    real_update = Query.update

    def mismatched_update(query, *args, **kwargs):
        return real_update(query, *args, **kwargs) - 1

    monkeypatch.setattr(Query, "update", mismatched_update)
    assert not settle_attempt_inferred_delivered_batch(
        attempt, [item.id for item in intact], evidence
    )
    monkeypatch.setattr(Query, "update", real_update)
    with f44_db() as db:
        assert all(
            db.get(InboxModel, item.id).status == MessageStatus.DELIVERING.value for item in intact
        )
        assert db.get(InboxDeliveryAttemptModel, attempt).settled_at is None
    assert settle_delivery_attempt_proof_safe(attempt, {}) == "settled"
    with f44_db() as db:
        assert all(
            db.get(InboxModel, item.id).status == MessageStatus.PENDING.value for item in intact
        )


def test_t2b_unknown_classifier_falls_through_to_legacy(f44_db, tmp_path, monkeypatch):
    transcript = tmp_path / "unknown.jsonl"
    transcript.write_bytes(b"x" * 600000)
    message = create_inbox_message("sender", "receiver", "unknown status")
    monitor = _delivery_fakes(monkeypatch, transcript, send_error=RuntimeError("after submit"))
    monitor.get_status.side_effect = RuntimeError("status unavailable")
    InboxService().deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["evidence"]["receiver_status_at_settle"] == "unknown"


def test_t2c_callback_insert_serializes_before_probable_settlement(f44_db, monkeypatch):
    messages, attempt = _attempt_rows(f44_db, [1], text="lock")
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    monkeypatch.setattr(
        inbox_module,
        "_classify_probable_delivery",
        lambda *_a, **_k: _qualifying_evidence(),
    )
    callback_holds_lock = threading.Event()
    release_callback = threading.Event()
    result: list[bool] = []

    def callback_insert():
        with stalled_callback_watchdog.callback_insert_guard("receiver"):
            callback_holds_lock.set()
            assert release_callback.wait(5)

    def settle():
        won, _ = service._settle_probable_delivered(
            attempt,
            [messages[0].id],
            messages,
            {},
            None,
            "receiver",
            "sender",
            OrchestrationType.ASSIGN,
            {"caller_id": "caller", "agent_profile": "worker"},
            False,
        )
        result.append(won)

    callback_thread = threading.Thread(target=callback_insert)
    settle_thread = threading.Thread(target=settle)
    callback_thread.start()
    assert callback_holds_lock.wait(5)
    settle_thread.start()
    with f44_db() as db:
        assert db.get(InboxModel, messages[0].id).status == MessageStatus.DELIVERING.value
        assert db.get(InboxDeliveryAttemptModel, attempt).settled_at is None
    release_callback.set()
    callback_thread.join(5)
    settle_thread.join(5)
    assert not callback_thread.is_alive() and not settle_thread.is_alive()
    assert result == [True]


def test_t3_stat_failure_is_null_and_never_blocks_legacy(f44_db, monkeypatch):
    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.PROCESSING
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    evidence = inbox_module._classify_probable_delivery(
        {"busy_initial_submit": {"status_at_submit": "processing"}},
        "receiver",
        {"path": "/definitely/missing/f44", "inode": 1, "size": 10},
    )
    assert evidence["transcript_growth"] is None
    assert not inbox_module.is_probable_delivered(evidence)


def test_t4_delivery_failed_is_not_selected_for_delivery_or_recovery(f44_db, monkeypatch):
    message = create_inbox_message("sender", "receiver", "terminal")
    attempt = begin_delivery_attempt([message], "receiver", "grok_cli", "hash", 3)
    assert settle_delivery_attempt(attempt, MessageStatus.DELIVERY_FAILED, "failed")
    send = MagicMock()
    recover = MagicMock()
    monkeypatch.setattr(inbox_module.terminal_service, "send_prepared_input", send)
    service = InboxService()
    monkeypatch.setattr(service, "_recover_wpm2_attempt", recover)
    service.deliver_pending("receiver")
    service.recover_stale_deliveries(recurring=True)
    send.assert_not_called()
    recover.assert_not_called()


def test_t6_wpm2_recovery_residual_still_requeues_confirmation_timeout(f44_db, monkeypatch):
    message = create_inbox_message("sender", "receiver", "stale claude")
    attempt = begin_delivery_attempt([message], "receiver", "claude_code", "hash", 4)
    with f44_db.begin() as db:
        db.get(InboxDeliveryAttemptModel, attempt).started_at = datetime.now() - timedelta(
            minutes=5
        )
    monkeypatch.setattr(inbox_module, "resolve_session_transcript", lambda _meta: None)
    InboxService()._recover_wpm2_attempt(
        {
            "attempt_uuid": attempt,
            "receiver_terminal_id": "receiver",
            "message_ids": [message.id],
            "payload_hash": "hash",
            "started_at": datetime.now() - timedelta(minutes=5),
            "evidence": "{}",
            "sender_id": "sender",
            "orchestration_type": OrchestrationType.ASSIGN.value,
        }
    )
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert trace["attempts"][0]["outcome"] == "ambiguous"
    assert trace["attempts"][0]["reason"] == "confirmation_timeout"


@pytest.mark.parametrize(
    "raw",
    ["0.1", True, 0, -1, float("nan"), float("inf"), 10**400],
)
def test_t5a_present_invalid_timeout_warns_and_defaults(monkeypatch, caplog, raw):
    monkeypatch.setattr(
        inbox_module.settings_service,
        "get_provider_defaults",
        lambda _section: {"confirmation_timeout_seconds": raw},
    )
    caplog.set_level("WARNING", logger=inbox_module.__name__)
    assert inbox_module._confirmation_timeout_seconds() == 10.0
    assert caplog.text.count("Invalid [inbox] confirmation_timeout_seconds") == 1


def test_t5a_valid_missing_and_malformed_timeout_settings(monkeypatch, tmp_path, caplog):
    with monkeypatch.context() as context:
        context.setattr(
            inbox_module.settings_service,
            "get_provider_defaults",
            lambda _section: {"confirmation_timeout_seconds": 0.125},
        )
        assert inbox_module._confirmation_timeout_seconds() == 0.125

    with monkeypatch.context() as context:
        context.setattr(
            inbox_module.settings_service,
            "get_provider_defaults",
            lambda _section: {},
        )
        caplog.clear()
        assert inbox_module._confirmation_timeout_seconds() == 10.0
        assert not caplog.text

    malformed = tmp_path / "providers.toml"
    malformed.write_text("[inbox\n", encoding="utf-8")
    monkeypatch.setattr(inbox_module.settings_service, "PROVIDER_DEFAULTS_FILE", malformed)
    caplog.clear()
    assert inbox_module._confirmation_timeout_seconds() == 10.0
    assert not caplog.text


def test_t5a_outer_template_documents_commented_inbox_default():
    template = Path(__file__).resolve().parents[3] / "providers.toml.default"
    content = template.read_text(encoding="utf-8")
    assert "# [inbox]" in content
    assert "# confirmation_timeout_seconds = 10.0" in content

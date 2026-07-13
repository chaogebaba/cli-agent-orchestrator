"""Frozen-r9 evidence tests for WPM1 inbox delivery deduplication."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    begin_delivery_attempt,
    create_inbox_message,
    create_terminal,
    get_pending_messages,
    merge_wpm1_attempt_evidence,
    record_wpm1_stalled_notice,
    settle_delivery_attempt,
    settle_wpm1_terminal_batch,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.inbox import InboxMessage, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference, TranscriptResolution, transcript_ref,
)
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


@pytest.fixture
def wpm1_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm1.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    create_terminal("caller", "s", "caller", "codex")
    create_terminal("sender", "s", "sender", "codex")
    create_terminal("receiver", "s", "receiver", "claude_code", caller_id="caller")
    yield sessions
    engine.dispose()


def _ambiguous(evidence=None, *, sender="sender", receiver="receiver"):
    create_inbox_message(sender, receiver, "wire")
    message = get_pending_messages(receiver)[0]
    attempt = begin_delivery_attempt(
        [message], receiver, "claude_code", "digest", 4,
        evidence=json.dumps(evidence or {"resolution_kind": "binding"}),
    )
    settle_delivery_attempt(
        attempt, MessageStatus.PENDING, "ambiguous", reason="confirmation_timeout",
        evidence=json.dumps(evidence or {"resolution_kind": "binding"}),
    )
    return message, attempt


@pytest.mark.parametrize("padding", [1900, 2400])
def test_wpm1_no_slice_real_settlement_preserves_json(wpm1_db, padding):
    prior = {
        "resolution_kind": "binding", "padding": "x" * padding,
        "boundary_authorized": "2026-07-13T00:00:00Z",
    }
    message, attempt = _ambiguous(prior)
    assert merge_wpm1_attempt_evidence(
        attempt, [message.id], {"terminal_settled_at": "2026-07-13T01:02:03Z"})
    with wpm1_db() as db:
        evidence = json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)
    assert evidence["padding"] == prior["padding"]
    assert evidence["boundary_authorized"].endswith("Z")
    assert evidence["terminal_settled_at"].endswith("Z")


def test_wpm1_merge_before_cas_rolls_back_on_member_mismatch(wpm1_db):
    message, attempt = _ambiguous()
    assert settle_wpm1_terminal_batch(
        [message.id, 999], MessageStatus.DELIVERED, "receiver") == "stale"
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value
        assert "terminal_settled_at" not in json.loads(
            db.get(InboxDeliveryAttemptModel, attempt).evidence)


def test_wpm1_merge_before_cas_rolls_back_on_post_merge_cas_failure(wpm1_db):
    message, attempt = _ambiguous()
    engine = wpm1_db.kw["bind"]
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER lose_wpm1_cas BEFORE UPDATE OF status ON inbox "
            "WHEN NEW.status = 'delivered' BEGIN SELECT RAISE(ABORT, 'cas lost'); END"
        )
    with pytest.raises(Exception, match="cas lost"):
        settle_wpm1_terminal_batch([message.id], MessageStatus.DELIVERED, "receiver")
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value
        assert "terminal_settled_at" not in json.loads(
            db.get(InboxDeliveryAttemptModel, attempt).evidence)


def test_wpm1_stalled_notice_is_atomic_and_external_retry_deduplicates(wpm1_db):
    message, attempt = _ambiguous()
    stamp = "2026-07-13T01:02:03Z"
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", stamp) == "recorded"
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", stamp) == "already_recorded"
    with wpm1_db() as db:
        notices = db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=stalled ")).all()
        assert len(notices) == 1
        assert notices[0].receiver_id == "sender"
        assert json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)[
            "stalled_notified_at"] == stamp


def test_wpm1_notice_header_lf_prevents_one_ten_collision(wpm1_db):
    one, attempt = _ambiguous()
    with wpm1_db.begin() as db:
        db.add(InboxModel(
            id=10, sender_id="sender", receiver_id="receiver", message="other",
            orchestration_type="send_message", status="pending"))
    assert record_wpm1_stalled_notice(
        attempt, [one.id], "receiver", "2026-07-13T01:02:03Z") == "recorded"
    with wpm1_db() as db:
        bodies = [row.message for row in db.query(InboxModel).filter(
            InboxModel.sender_id == "message-trace:receiver").all()]
    assert any(body.startswith(f"wpm1-notice kind=stalled batch={one.id}\n") for body in bodies)
    assert not any(body.startswith("wpm1-notice kind=stalled batch=10\n") for body in bodies)


def test_wpm1_concurrent_independent_stalled_writers_insert_once(wpm1_db):
    message, attempt = _ambiguous()
    barrier = threading.Barrier(2)
    outcomes = []

    def worker():
        barrier.wait()
        outcomes.append(record_wpm1_stalled_notice(
            attempt, [message.id], "receiver", "2026-07-13T01:02:03Z"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=stalled ")).count() == 1
    assert sorted(outcomes) == ["already_recorded", "recorded"]


def test_wpm1_atomic_helper_acquires_immediate_write_lock(wpm1_db):
    message, attempt = _ambiguous()
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    engine = wpm1_db.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        record_wpm1_stalled_notice(
            attempt, [message.id], "receiver", "2026-07-13T01:02:03Z")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert "BEGIN IMMEDIATE" in statements
    assert "BEGIN" not in statements


def test_wpm1_busy_exhaustion_is_closed_and_next_wake_retries_pair(wpm1_db):
    message, attempt = _ambiguous()
    lock = wpm1_db.kw["bind"].raw_connection()
    lock.execute("BEGIN IMMEDIATE")
    try:
        assert record_wpm1_stalled_notice(
            attempt, [message.id], "receiver", "2026-07-13T01:02:03Z") == "busy_aborted"
    finally:
        lock.rollback()
        lock.close()
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value
        assert "stalled_notified_at" not in json.loads(
            db.get(InboxDeliveryAttemptModel, attempt).evidence)
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z") == "recorded"


def test_wpm1_service_merge_busy_aborted_never_flips_pending_failed(wpm1_db):
    message, _attempt = _ambiguous()
    lock = wpm1_db.kw["bind"].raw_connection()
    lock.execute("BEGIN IMMEDIATE")
    svc = InboxService()
    try:
        with (
            patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
                  return_value=_binding()),
            patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
                  return_value=("absent", {})),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        ):
            monitor.get_status.return_value = TerminalStatus.PROCESSING
            svc.deliver_pending("receiver")
    finally:
        lock.rollback()
        lock.close()
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value


def test_wpm1_late_confirm_writes_clock_and_corrective_to_durable_recipient(wpm1_db):
    message, attempt = _ambiguous()
    record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z")
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERED.value
        evidence = json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)
        assert evidence["terminal_settled_at"].endswith("Z")
        corrective = db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).one()
        assert corrective.receiver_id == "sender"


def test_wpm1_log_only_stall_late_confirm_logs_without_orphan(wpm1_db, caplog):
    message, attempt = _ambiguous()
    with wpm1_db.begin() as db:
        db.query(database.TerminalModel).filter(
            database.TerminalModel.id.in_(("sender", "caller"))).delete(
            synchronize_session=False)
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z") == "logged_only"
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).count() == 0
    assert "no longer available" in caplog.text


@pytest.mark.parametrize("hook,statement", [
    ("before_cursor_execute", "UPDATE inbox_delivery_attempt"),
    ("after_cursor_execute", "UPDATE inbox_delivery_attempt"),
    ("before_cursor_execute", "INSERT INTO inbox"),
    ("after_cursor_execute", "INSERT INTO inbox"),
])
def test_wpm1_stalled_pair_crash_rolls_back_after_every_write(
    wpm1_db, hook, statement
):
    message, attempt = _ambiguous()
    engine = wpm1_db.kw["bind"]
    fired = False

    def crash(_conn, _cursor, sql, _params, _context, _many):
        nonlocal fired
        if not fired and sql.lstrip().lower().startswith(statement.lower()):
            fired = True
            raise RuntimeError("crash-stalled-write")

    event.listen(engine, hook, crash)
    try:
        with pytest.raises(RuntimeError, match="crash-stalled-write"):
            record_wpm1_stalled_notice(
                attempt, [message.id], "receiver", "2026-07-13T01:02:03Z")
    finally:
        event.remove(engine, hook, crash)
    with wpm1_db() as db:
        assert "stalled_notified_at" not in json.loads(
            db.get(InboxDeliveryAttemptModel, attempt).evidence)
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z") == "recorded"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=stalled ")).count() == 1


@pytest.mark.parametrize("hook,statement", [
    ("before_cursor_execute", "UPDATE inbox_delivery_attempt"),
    ("after_cursor_execute", "UPDATE inbox_delivery_attempt"),
    ("before_cursor_execute", "UPDATE inbox SET"),
    ("after_cursor_execute", "UPDATE inbox SET"),
    ("before_cursor_execute", "INSERT INTO inbox"),
    ("after_cursor_execute", "INSERT INTO inbox"),
])
def test_wpm1_corrective_pair_crash_rolls_back_after_every_write(
    wpm1_db, hook, statement
):
    message, attempt = _ambiguous()
    record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z")
    engine = wpm1_db.kw["bind"]
    fired = False

    def crash(_conn, _cursor, sql, _parameters, _context, _many):
        nonlocal fired
        if not fired and sql.lstrip().lower().startswith(statement.lower()):
            fired = True
            raise RuntimeError("crash-corrective-write")

    event.listen(engine, hook, crash)
    try:
        with pytest.raises(RuntimeError, match="crash-corrective-write"):
            settle_wpm1_terminal_batch([message.id], MessageStatus.DELIVERED, "receiver")
    finally:
        event.remove(engine, hook, crash)
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value
        assert "terminal_settled_at" not in json.loads(
            db.get(InboxDeliveryAttemptModel, attempt).evidence)
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).count() == 1


def test_wpm1_cleanup_day15_retains_unsettled_batch_and_notice(
    wpm1_db, monkeypatch, tmp_path, caplog
):
    from cli_agent_orchestrator.services import cleanup_service

    message, attempt = _ambiguous()
    record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-06-01T01:02:03Z")
    with wpm1_db.begin() as db:
        for row in db.query(InboxModel).all():
            row.created_at = datetime.now() - timedelta(days=15)
    monkeypatch.setattr(cleanup_service, "SessionLocal", wpm1_db)
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "terminal")
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    with caplog.at_level("INFO"):
        cleanup_service.cleanup_old_data()
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id) is not None
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=stalled ")).count() == 1
    assert "Exempted 1 gated WPM1 batch" in caplog.text
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERED.value
        corrective = db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).one()
        assert corrective.receiver_id == "sender"


def test_wpm1_cleanup_day15_external_retry_keeps_dedup_key(
    wpm1_db, monkeypatch, tmp_path
):
    from cli_agent_orchestrator.services import cleanup_service

    message, attempt = _ambiguous()
    stamp = "2026-06-01T01:02:03Z"
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", stamp) == "recorded"
    with wpm1_db.begin() as db:
        for row in db.query(InboxModel).all():
            row.created_at = datetime.now() - timedelta(days=15)
    monkeypatch.setattr(cleanup_service, "SessionLocal", wpm1_db)
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "terminal")
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    cleanup_service.cleanup_old_data()
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", stamp) == "already_recorded"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=stalled ")).count() == 1


def test_wpm1_cleanup_reaps_terminal_batch_attempt_and_notice_together(
    wpm1_db, monkeypatch, tmp_path
):
    from cli_agent_orchestrator.services import cleanup_service

    message, attempt = _ambiguous()
    assert record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-01-01T01:02:03Z") == "recorded"
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db.begin() as db:
        for row in db.query(InboxModel).all():
            row.created_at = datetime.now() - timedelta(days=20)
        evidence = json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)
        evidence["terminal_settled_at"] = "2026-01-01T00:00:00Z"
        db.get(InboxDeliveryAttemptModel, attempt).evidence = json.dumps(evidence)
    monkeypatch.setattr(cleanup_service, "SessionLocal", wpm1_db)
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "terminal")
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    cleanup_service.cleanup_old_data()
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id) is None
        assert db.get(InboxDeliveryAttemptModel, attempt) is None
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice ")).count() == 0


@pytest.mark.parametrize("clock,should_exist,warning", [
    (None, True, "Absent WPM1 terminal_settled_at"),
    ("not-a-clock", True, "Malformed WPM1 terminal_settled_at"),
    ("2026-01-01T00:00:00Z", False, None),
])
def test_wpm1_cleanup_settled_clock_matrix(
    wpm1_db, monkeypatch, tmp_path, caplog, clock, should_exist, warning
):
    from cli_agent_orchestrator.services import cleanup_service

    message, attempt = _ambiguous()
    with wpm1_db.begin() as db:
        db.get(InboxModel, message.id).status = MessageStatus.DELIVERED.value
        db.get(InboxModel, message.id).created_at = datetime.now() - timedelta(days=20)
        evidence = {"resolution_kind": "binding"}
        if clock is not None:
            evidence["terminal_settled_at"] = clock
        db.get(InboxDeliveryAttemptModel, attempt).evidence = json.dumps(evidence)
    monkeypatch.setattr(cleanup_service, "SessionLocal", wpm1_db)
    monkeypatch.setattr(cleanup_service, "TERMINAL_LOG_DIR", tmp_path / "terminal")
    monkeypatch.setattr(cleanup_service, "LOG_DIR", tmp_path / "logs")
    with caplog.at_level("WARNING"):
        cleanup_service.cleanup_old_data()
    with wpm1_db() as db:
        assert (db.get(InboxModel, message.id) is not None) is should_exist
    if warning:
        assert warning in caplog.text


@pytest.mark.parametrize("status,reason", [
    (MessageStatus.DELIVERED, None),
    (MessageStatus.DELIVERY_FAILED, None),
    (MessageStatus.DELIVERY_FAILED, "receiver_gone"),
])
def test_wpm1_all_terminal_arms_write_canonical_clock(wpm1_db, status, reason):
    message, attempt = _ambiguous()
    assert settle_wpm1_terminal_batch(
        [message.id], status, "receiver", reason=reason) == "settled"
    with wpm1_db() as db:
        evidence = json.loads(db.get(InboxDeliveryAttemptModel, attempt).evidence)
        assert evidence["terminal_settled_at"].endswith("Z")


def test_wpm1_watchdog_suppression_query_episode_matrix(wpm1_db):
    message, attempt = _ambiguous()
    assert database.has_inflight_callback_since(
        "sender", "receiver", datetime.now() - timedelta(minutes=1))
    assert not database.has_inflight_callback_since(
        "sender", "caller", datetime.now() - timedelta(minutes=1))
    settle_wpm1_terminal_batch([message.id], MessageStatus.DELIVERED, "receiver")
    assert not database.has_inflight_callback_since(
        "sender", "receiver", datetime.now() - timedelta(minutes=1))


def test_wpm1_watchdog_collect_due_uses_episode_scoped_inflight_query(wpm1_db):
    watchdog = StalledCallbackWatchdog(grace_seconds=3)
    watchdog.record_inbound_task("sender", "receiver", "developer")
    _ambiguous(sender="sender", receiver="receiver")
    watchdog.record_status("sender", TerminalStatus.IDLE, now=10)
    watchdog._episodes["sender"].last_screen_fp = "stable"
    assert watchdog.collect_due_notifications(now=13) == []
    assert not watchdog._episodes["sender"].fired


def _gate_message() -> InboxMessage:
    return InboxMessage(
        id=1, sender_id="sender", receiver_id="receiver", message="wire",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        status=MessageStatus.PENDING, created_at=datetime.now())


def _gate_attempt(number=0, *, exhausted=False, age_minutes=1):
    evidence = {"resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10}
    if exhausted:
        evidence["boundary_exhausted_at"] = "2026-07-13T00:00:00Z"
    return {
        "attempt_uuid": f"a{number}", "provider": "claude_code",
        "payload_hash": "digest", "outcome": "ambiguous",
        "reason": "confirmation_timeout",
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes + 1),
        "settled_at": datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        "prior_attempt_uuid": f"a{number - 1}" if number else None,
        "evidence": json.dumps(evidence),
    }


def _binding():
    return TranscriptResolution(
        path=__import__("pathlib").Path("/trace"), resolution_kind="binding",
        live_reference=TranscriptLiveReference(
            __import__("pathlib").Path("/trace"), 1, 20))


def test_incident2_replay_growth_injects_once_then_d2_confirms(wpm1_db):
    svc = InboxService()
    create_inbox_message("sender", "receiver", "wire")
    message = get_pending_messages("receiver")[0]
    resolution = _binding()
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=resolution),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              side_effect=[("absent", {"size": 999}), ("absent", {"size": 1999}),
                           ("hit", {})]),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="prepared"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input")
        as send,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", transcript_ref(resolution))),
        patch.object(svc, "_commit_watchdog_ops"),
    ):
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 1
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc.deliver_pending("receiver")
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        svc.deliver_pending("receiver")
        svc.deliver_pending("receiver")
        svc.deliver_pending("receiver")
    assert send.call_count == 1
    with wpm1_db() as db:
        attempts = db.query(InboxDeliveryAttemptModel).all()
        assert len(attempts) == 1
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERED.value


def test_loss_boundary_marks_exhaustion_before_authorizing_successor():
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    attempt = _gate_attempt()
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True) as merge,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        state, evidence = svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)
    assert state == "inject"
    assert "boundary_authorized" in evidence
    assert any("boundary_exhausted_at" in call.args[2] for call in merge.call_args_list)


def test_busy_wake_after_third_injection_cannot_cap():
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    attempts = [_gate_attempt(0, exhausted=True), _gate_attempt(1, exhausted=True),
                _gate_attempt(2)]
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=attempts),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch") as settle,
    ):
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)[0] == "stop"
    settle.assert_not_called()


def test_third_exhaustion_proof_caps_without_fourth_injection():
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    attempts = [_gate_attempt(0, exhausted=True), _gate_attempt(1, exhausted=True),
                _gate_attempt(2)]
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=attempts),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
              return_value="settled") as settle,
        patch.object(svc, "_notify_delivery_failed"),
    ):
        monitor.get_status.return_value = TerminalStatus.COMPLETED
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)[0] == "stop"
    settle.assert_called_once_with([1], MessageStatus.DELIVERY_FAILED, "receiver")


def test_threshold_plus_idle_proof_sends_no_stalled_notice():
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    attempt = _gate_attempt(age_minutes=241)
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.record_wpm1_stalled_notice") as notice,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)[0] == "inject"
    notice.assert_not_called()


@pytest.mark.parametrize("age_minutes,activity_age", [(31, 31), (241, 1)])
def test_stalled_notice_fires_at_30min_idle_or_4h_absolute(age_minutes, activity_age):
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "unresolved"
    attempt = _gate_attempt(age_minutes=age_minutes)
    evidence = json.loads(attempt["evidence"])
    evidence.update({
        "last_activity_at": (
            datetime.now(timezone.utc) - timedelta(minutes=activity_age)
        ).isoformat().replace("+00:00", "Z"),
        "last_observed_status": TerminalStatus.PROCESSING.value,
        "last_observed_ref": transcript_ref(_binding()),
    })
    attempt["evidence"] = json.dumps(evidence)
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("unresolved", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.record_wpm1_stalled_notice",
              return_value="recorded") as notice,
    ):
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)[0] == "stop"
    notice.assert_called_once()


def test_wpm1_rowcount_zero_merge_aborts_wake_before_gate_or_settlement():
    svc = InboxService()
    provider = MagicMock()
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[_gate_attempt()]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=False),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch") as settle,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)[0] == "stop"
    provider.read_composer_draft_state.assert_not_called()
    settle.assert_not_called()


@pytest.mark.parametrize("draft,expected", [("", "empty"), ("typed", "nonempty")])
def test_wpm1_claude_composer_read_only_tri_state(draft, expected):
    provider = ClaudeCodeProvider("receiver", "s", "w")
    screen = f"────────\n❯ {draft}\n────────"
    backend = MagicMock()
    backend.get_history.return_value = screen
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        assert provider.read_composer_draft_state() == expected
    assert backend.send_keys.call_count == 0
    assert backend.send_special_key.call_count == 0


def test_wpm1_claude_composer_capture_failure_and_changing_content_fail_closed():
    provider = ClaudeCodeProvider("receiver", "s", "w")
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("capture failed")
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        assert provider.read_composer_draft_state() == "unresolved"
    backend.get_history.side_effect = [
        "────────\n❯ first\n────────", "────────\n❯ second\n────────"]
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        assert provider.read_composer_draft_state() == "unresolved"


@pytest.mark.parametrize("fixture,expected", [
    ("multiline-draft-sgr.txt", "nonempty"),
    ("dim-placeholder-sgr.txt", "empty"),
])
def test_wpm1_claude_composer_frozen_fixture_matrix(fixture, expected):
    provider = ClaudeCodeProvider("receiver", "s", "w")
    screen = (Path(__file__).parents[1] / "fixtures" / "fx2" / fixture).read_text()
    backend = MagicMock()
    backend.get_history.return_value = screen
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        assert provider.read_composer_draft_state() == expected


def test_wpm1_claude_composer_parser_failure_is_unresolved():
    provider = ClaudeCodeProvider("receiver", "s", "w")
    backend = MagicMock()
    backend.get_history.return_value = "────────\n❯ typed\n────────"
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch.object(provider, "read_composer_draft", return_value=None),
    ):
        assert provider.read_composer_draft_state() == "unresolved"


def test_wpm1_paste_without_submit_blocks_reinject_then_consumption_d2_confirms(wpm1_db):
    message, _attempt = _ambiguous()
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "nonempty"
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              side_effect=[("absent", {}), ("hit", {})]),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input")
        as send,
        patch.object(svc, "_commit_watchdog_ops"),
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc.deliver_pending("receiver")
        assert send.call_count == 0
        svc.deliver_pending("receiver")
    with wpm1_db() as db:
        assert db.get(InboxModel, message.id).status == MessageStatus.DELIVERED.value
    send.assert_not_called()


def test_draft_guard_deferred_attempt_creates_no_wpm1_gate_state():
    svc = InboxService()
    deferred = _gate_attempt()
    deferred.update(outcome="deferred", reason="delivery_deferred")
    with patch.object(svc, "_exact_batch_attempts", return_value=[deferred]):
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, MagicMock(),
            "sender", OrchestrationType.SEND_MESSAGE) == ("normal", None)


def test_exact_batch_attempt_lookup_rejects_overlapping_member_set():
    svc = InboxService()
    attempts = [_gate_attempt(0), _gate_attempt(1)]
    with (
        patch("cli_agent_orchestrator.services.inbox_service.list_message_attempts",
              return_value=attempts),
        patch("cli_agent_orchestrator.services.inbox_service.list_attempt_member_ids",
              side_effect=[[1], [1, 2]]),
    ):
        assert svc._exact_batch_attempts([1]) == [attempts[0]]


def test_successor_writer_pins_exact_exhausted_source_despite_newer_overlap(wpm1_db):
    first, exhausted = _ambiguous({
        "resolution_kind": "binding", "boundary_exhausted_at": "2026-07-13T00:00:00Z"
    })
    create_inbox_message("sender", "receiver", "overlap")
    both = get_pending_messages("receiver", limit=10)
    overlap = begin_delivery_attempt(both, "receiver", "claude_code", "overlap", 7)
    settle_delivery_attempt(overlap, MessageStatus.PENDING, "interrupted")
    first = next(message for message in get_pending_messages("receiver", limit=10)
                 if message.id == first.id)
    successor = begin_delivery_attempt(
        [first], "receiver", "claude_code", "successor", 9,
        evidence=json.dumps({"boundary_authorized": "2026-07-13T01:00:00Z"}),
        prior_attempt_uuid=exhausted,
    )
    with wpm1_db() as db:
        assert db.get(InboxDeliveryAttemptModel, successor).prior_attempt_uuid == exhausted
    settle_delivery_attempt(successor, MessageStatus.PENDING, "interrupted")
    with pytest.raises(ValueError, match="exact batch"):
        begin_delivery_attempt(
            [first], "receiver", "claude_code", "bad", 3,
            evidence=json.dumps({"boundary_authorized": "2026-07-13T02:00:00Z"}),
            prior_attempt_uuid=overlap,
        )


def test_successor_restart_after_exhaustion_merge_injects_once(wpm1_db):
    message, exhausted = _ambiguous({
        "resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10,
        "boundary_exhausted_at": "2026-07-13T00:00:00Z",
    })
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="prepared"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input")
        as send,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", {"resolution_kind": "binding"})),
    ):
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 1
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc.deliver_pending("receiver")
    assert send.call_count == 1
    with wpm1_db() as db:
        attempts = db.query(InboxDeliveryAttemptModel).order_by(
            InboxDeliveryAttemptModel.started_at).all()
        assert len(attempts) == 2
        assert attempts[-1].prior_attempt_uuid == exhausted
        assert db.get(InboxModel, message.id).status == MessageStatus.PENDING.value


def test_successor_restart_after_begin_commit_recovers_and_never_respawns(wpm1_db):
    message, exhausted = _ambiguous({
        "resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10,
        "boundary_exhausted_at": "2026-07-13T00:00:00Z",
    })
    successor = begin_delivery_attempt(
        [message], "receiver", "claude_code", "successor", 9,
        evidence=json.dumps({"resolution_kind": "binding", "boundary_authorized": True}),
        prior_attempt_uuid=exhausted,
    )
    svc = InboxService()
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("process crashed")
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        svc.recover_stale_deliveries()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input")
        as send,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc.deliver_pending("receiver")
    send.assert_not_called()
    with wpm1_db() as db:
        attempts = db.query(InboxDeliveryAttemptModel).all()
        assert len(attempts) == 2
        assert db.get(InboxDeliveryAttemptModel, successor).outcome == "interrupted"


def test_successor_restart_after_paste_return_recovers_and_never_respawns(wpm1_db):
    message, exhausted = _ambiguous({
        "resolution_kind": "binding", "path": "/trace", "inode": 1, "size": 10,
        "boundary_exhausted_at": "2026-07-13T00:00:00Z",
    })
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    pasted = MagicMock(side_effect=KeyboardInterrupt("crash after paste"))
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="prepared"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              pasted),
    ):
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 1
        monitor.get_status.return_value = TerminalStatus.IDLE
        with pytest.raises(KeyboardInterrupt, match="crash after paste"):
            svc.deliver_pending("receiver")
    pasted.assert_called_once()
    backend = MagicMock()
    backend.get_history.side_effect = RuntimeError("process crashed")
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        svc.recover_stale_deliveries()
    with (
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input")
        as send,
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc.deliver_pending("receiver")
    send.assert_not_called()
    with wpm1_db() as db:
        attempts = db.query(InboxDeliveryAttemptModel).all()
        assert len(attempts) == 2
        successor = next(row for row in attempts if row.attempt_uuid != exhausted)
        assert successor.outcome == "interrupted"
        assert successor.prior_attempt_uuid == exhausted


def test_interrupted_successor_blocks_duplicate_respawn():
    svc = InboxService()
    exhausted = _gate_attempt(0, exhausted=True)
    interrupted = _gate_attempt(1)
    interrupted.update(outcome="interrupted", reason="pane_unresolvable",
                       prior_attempt_uuid=exhausted["attempt_uuid"])
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[exhausted, interrupted]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              return_value=("absent", {})),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        state, _ = svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)
    assert state == "stop"


def test_cap_barrier_late_payload_confirmation_wins():
    svc = InboxService()
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    attempts = [_gate_attempt(0, exhausted=True), _gate_attempt(1, exhausted=True),
                _gate_attempt(2)]
    # Three initial newest-first checks, boundary check, then pre-settlement barrier.
    lookups = [("absent", {})] * 4 + [("hit", {})]
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=attempts),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              side_effect=lookups),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
              return_value="settled") as settle,
        patch.object(svc, "_commit_watchdog_ops"),
    ):
        monitor.get_status.return_value = TerminalStatus.IDLE
        svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)
    settle.assert_called_once()
    assert settle.call_args.args == ([1], MessageStatus.DELIVERED, "receiver")
    assert callable(settle.call_args.kwargs["on_confirmed"])


def test_ambiguous_attempt_queued_command_confirmation_stops_reinjection(tmp_path):
    from cli_agent_orchestrator.services.message_trace_service import (
        transcript_lookup, wire_hash,
    )

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "attachment",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "attachment": {"type": "queued_command", "prompt": "wire"},
    }) + "\n", encoding="utf-8")
    attempt = _gate_attempt(0)
    attempt["payload_hash"] = wire_hash("wire")
    svc = InboxService()

    def lookup(_metadata, payload_hash, started_at, _expected_ref):
        return transcript_lookup(
            transcript, payload_hash, started_at, scan_from_start=True)

    provider = MagicMock()
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=_binding()),
        patch("cli_agent_orchestrator.services.inbox_service.continuity_aware_lookup",
              side_effect=lookup),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
              return_value="settled") as settle,
        patch.object(svc, "_commit_watchdog_ops"),
    ):
        state, evidence = svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {"provider": "claude_code"}, provider,
            "sender", OrchestrationType.SEND_MESSAGE)

    assert (state, evidence) == ("stop", None)
    settle.assert_called_once()
    assert settle.call_args.args == ([1], MessageStatus.DELIVERED, "receiver")
    provider.read_composer_draft_state.assert_not_called()


def test_late_confirm_without_prior_stall_creates_no_corrective(wpm1_db, caplog):
    message, _attempt = _ambiguous()
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).count() == 0
    assert "corrective notice" not in caplog.text


def test_corrective_never_recomputes_deleted_durable_recipient(wpm1_db, caplog):
    message, attempt = _ambiguous()
    record_wpm1_stalled_notice(
        attempt, [message.id], "receiver", "2026-07-13T01:02:03Z")
    with wpm1_db.begin() as db:
        db.query(InboxModel).filter(InboxModel.receiver_id == "sender").delete()
        db.query(database.TerminalModel).filter_by(id="sender").delete()
    assert settle_wpm1_terminal_batch(
        [message.id], MessageStatus.DELIVERED, "receiver") == "settled"
    with wpm1_db() as db:
        assert db.query(InboxModel).filter(
            InboxModel.message.startswith("wpm1-notice kind=corrective ")).count() == 0
    assert "no longer available" in caplog.text


@pytest.mark.parametrize("evidence", ["{}", "{bad-json", json.dumps({
    "resolution_kind": "binding", "stale_note": "binding_stale:missing"
})])
def test_dead_receiver_wins_before_absent_malformed_unresolved_continuity(evidence):
    svc = InboxService()
    attempt = _gate_attempt()
    attempt["evidence"] = evidence
    with (
        patch.object(svc, "_exact_batch_attempts", return_value=[attempt]),
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch",
              return_value="settled") as settle,
        patch.object(svc, "_notify_delivery_failed") as notice,
    ):
        assert svc._handle_wpm1_gate(
            "receiver", [_gate_message()], {}, MagicMock(), "sender",
            OrchestrationType.SEND_MESSAGE)[0] == "stop"
    settle.assert_called_once_with(
        [1], MessageStatus.DELIVERY_FAILED, "receiver", reason="receiver_gone")
    notice.assert_called_once_with("receiver", [1], reason="receiver_gone")

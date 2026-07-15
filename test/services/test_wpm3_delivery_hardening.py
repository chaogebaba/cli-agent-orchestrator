"""Frozen WPM3 r2 production-path hardening evidence."""

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import TerminalNotFoundError
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base, InboxDeliveryAttemptModel, InboxModel,
    begin_delivery_attempt, begin_delivery_attempt_if_no_other_delivering,
    create_inbox_message, create_terminal, create_transcript_binding,
    get_message_trace, get_pending_messages, make_admission_proof,
    settle_delivery_attempt,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service, message_trace_service
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference, TranscriptResolution, transcript_ref, wire_hash,
)
from cli_agent_orchestrator.services.status_monitor import (
    BoundaryObservation, StatusMonitor,
)
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError


@pytest.fixture
def wpm3_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm3.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    create_terminal("caller", "s", "caller", "codex")
    create_terminal("sender", "s", "sender", "codex")
    create_terminal("sender2", "s", "sender2", "codex")
    create_terminal("receiver", "s", "receiver", "claude_code", caller_id="caller")
    yield sessions
    engine.dispose()


def _observation(status, *, epoch="epoch", seq=4, anchor=1):
    ready = status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
    return BoundaryObservation(
        epoch, status, 3, 1, seq, anchor + 1, seq if ready else None)


def _submit(status=TerminalStatus.PROCESSING, *, epoch="epoch", seq=2):
    return BoundaryObservation(epoch, status, 2, 1, seq, seq, None)


def _deliver_busy(wpm3_db, monkeypatch, *, provider_name="claude_code",
                  draft="empty", accepts=False, prior_outcome=None):
    if provider_name != "claude_code":
        create_terminal("native", "s", "native", provider_name, caller_id="caller")
    receiver = "receiver" if provider_name == "claude_code" else "native"
    message = create_inbox_message("sender", receiver, "payload")
    if prior_outcome is not None:
        prior = begin_delivery_attempt(
            [message], receiver, provider_name, "prior", 5)
        settle_delivery_attempt(
            prior, MessageStatus.PENDING, prior_outcome,
            reason="send_failed", evidence="{}")
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = draft
    provider.accepts_input_while_processing = accepts
    submitted = _submit()

    def send(*_args, **kwargs):
        callback = kwargs.get("on_submitted")
        if callback is not None:
            callback(submitted)
        return submitted

    monkeypatch.setattr(inbox_service, "EAGER_INBOX_DELIVERY", True)
    with (
        patch("cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
              return_value=provider),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=None),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              return_value="payload"),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("absent", {"kind": "transcript_absent"})),
    ):
        monitor.get_boundary_observation.return_value = _observation(TerminalStatus.PROCESSING)
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        InboxService().deliver_pending(receiver)
    return message, paste


def test_wpm3_eager_flag_cannot_disable_s4_eligibility(wpm3_db, monkeypatch):
    message, paste = _deliver_busy(wpm3_db, monkeypatch, accepts=False)
    assert paste.call_count == 1
    assert len(get_message_trace(message.id)["attempts"]) == 1


def test_wpm3_eager_flag_cannot_open_busy_paste_s4_refused(wpm3_db, monkeypatch):
    message, paste = _deliver_busy(
        wpm3_db, monkeypatch, accepts=True, prior_outcome="failed")
    assert paste.call_count == 0
    assert get_message_trace(message.id)["message"]["status"] == "pending"


def test_wpm3_eager_native_provider_unaffected(wpm3_db, monkeypatch):
    message, paste = _deliver_busy(
        wpm3_db, monkeypatch, provider_name="opencode_cli", accepts=True)
    assert paste.call_count == 1
    assert len(get_message_trace(message.id)["attempts"]) == 1


def _cursor(path: Path, size=None):
    return {"path": str(path), "inode": path.stat().st_ino,
            "size": path.stat().st_size if size is None else size,
            "resolution_kind": "binding", "cursor_version": 1}


def _settled_with_evidence(evidence, *, text="payload", outcome="ambiguous",
                           reason="confirmation_timeout"):
    message = create_inbox_message("sender", "receiver", text)
    attempt = begin_delivery_attempt(
        [message], "receiver", "claude_code", wire_hash(text), len(text),
        evidence=json.dumps(evidence))
    settle_delivery_attempt(
        attempt, MessageStatus.PENDING, outcome, reason=reason,
        evidence=json.dumps(evidence))
    return message, attempt


def test_wpm3_recovery_and_gate_share_lookup_authority(wpm3_db, tmp_path):
    assert inbox_service._wpm2_lookup is message_trace_service.wpm2_lookup
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    ref = _cursor(path)
    message, attempt = _settled_with_evidence(
        {"resolution_kind": "binding", "last_observed_ref": ref})
    calls = []

    def lookup(*args):
        calls.append(args)
        return "absent", {"kind": "transcript_absent"}

    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              side_effect=lookup),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.return_value = SimpleNamespace(status="bad")
        monitor.get_status.return_value = TerminalStatus.PROCESSING
        InboxService()._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, None,
            "sender", OrchestrationType.SEND_MESSAGE)
        recovery_message = create_inbox_message("sender", "receiver", "recovery")
        recovery_attempt = begin_delivery_attempt(
            [recovery_message], "receiver", "claude_code", wire_hash("recovery"), 8,
            evidence=json.dumps({"last_observed_ref": ref}))
        InboxService()._recover_wpm2_attempt({
            "attempt_uuid": recovery_attempt, "receiver_terminal_id": "receiver",
            "message_ids": [recovery_message.id], "payload_hash": wire_hash("recovery"),
            "started_at": datetime.now(timezone.utc), "evidence": json.dumps({
                "last_observed_ref": ref}), "sender_id": "sender",
            "orchestration_type": OrchestrationType.SEND_MESSAGE.value,
        })
    assert len(calls) == 2
    assert all(call[3]["last_observed_ref"] == ref for call in calls)


def test_wpm3_recovery_rejects_evidence_bag_as_expected_ref(wpm3_db, tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    cursor = _cursor(path)
    bag = {"path": "/legacy", "size": 999, "inode": 99,
           "resolution_kind": "binding", "last_observed_ref": cursor}
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    message = create_inbox_message("sender", "receiver", "payload")
    attempt = begin_delivery_attempt(
        [message], "receiver", "claude_code", wire_hash("payload"), 7,
        evidence=json.dumps(bag))
    observed_refs = []

    def continuity(_metadata, _hash, _started, expected_ref):
        observed_refs.append(expected_ref)
        return "absent", {**cursor, "kind": "transcript_absent"}

    with patch.object(message_trace_service, "continuity_aware_lookup", side_effect=continuity):
        InboxService()._recover_wpm2_attempt({
            "attempt_uuid": attempt, "receiver_terminal_id": "receiver",
            "message_ids": [message.id], "payload_hash": wire_hash("payload"),
            "started_at": datetime.now(timezone.utc), "evidence": json.dumps(bag),
            "sender_id": "sender",
            "orchestration_type": OrchestrationType.SEND_MESSAGE.value,
        })
    assert observed_refs == [cursor]
    assert observed_refs[0] is not bag


def test_wpm3_preopen_dedup_uses_canonical_lookup(wpm3_db, tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    message, attempt = _settled_with_evidence(
        {"last_observed_ref": _cursor(path)}, outcome="interrupted",
        reason="terminal_not_found")
    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("hit", {"kind": "transcript_queued_command"})) as lookup,
        patch("cli_agent_orchestrator.services.inbox_service.transcript_lookup",
              side_effect=AssertionError("direct lookup forbidden")),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service."
              "send_prepared_input") as paste,
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=TranscriptResolution(path, "binding", path.stat().st_ino)),
    ):
        monitor.get_boundary_observation.return_value = _observation(TerminalStatus.IDLE)
        InboxService().deliver_pending("receiver")
    lookup.assert_called()
    paste.assert_not_called()
    assert get_message_trace(message.id)["message"]["status"] == "delivered"


def _full_corrective(path: Path):
    cursor = _cursor(path)
    return {
        "resolution_kind": "binding", "last_observed_ref": cursor,
        "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1},
        "boundary_exhausted_at": "2030-01-01T00:00:00Z",
        "boundary_snapshot": {
            "observation_epoch": "epoch", "status": "completed",
            "status_gen": 3, "input_gen": 1, "seq": 4,
            "last_non_ready_seq": 2, "last_ready_seq": 4,
        },
    }


def _corrective_result(
    wpm3_db, tmp_path, mutate=None, add_member=False,
    outcome="ambiguous", reason="confirmation_timeout",
):
    path = tmp_path / f"corrective-{len(list(tmp_path.iterdir()))}.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    evidence = _full_corrective(path)
    if mutate is not None:
        mutate(evidence)
    message, prior = _settled_with_evidence(evidence, outcome=outcome, reason=reason)
    messages = [message]
    if add_member:
        messages.append(create_inbox_message("sender", "receiver", "extra"))
    proof = make_admission_proof("corrective", [item.id for item in messages], prior)
    return begin_delivery_attempt_if_no_other_delivering(
        messages, "receiver", "claude_code", "next", 4,
        prior_attempt_uuid=prior, admission_proof=proof), message, prior


def test_wpm3_corrective_refused_without_persisted_anchor(wpm3_db, tmp_path):
    result, _, _ = _corrective_result(
        wpm3_db, tmp_path, lambda value: value.pop("injection_completed_seq"))
    assert result.kind == "stale_admission"
    no_cursor, _, _ = _corrective_result(
        wpm3_db, tmp_path, lambda value: value.pop("last_observed_ref"))
    assert no_cursor.kind == "stale_admission"
    wrong_outcome, _, _ = _corrective_result(
        wpm3_db, tmp_path, outcome="interrupted", reason="terminal_not_found")
    assert wrong_outcome.kind == "stale_admission"


def test_wpm3_corrective_refused_snapshot_only_row(wpm3_db, tmp_path):
    result, _, _ = _corrective_result(
        wpm3_db, tmp_path, lambda value: value.pop("boundary_exhausted_at"))
    assert result.kind == "stale_admission"
    malformed, _, _ = _corrective_result(
        wpm3_db, tmp_path, lambda value: value["boundary_snapshot"].pop("status_gen"))
    assert malformed.kind == "stale_admission"
    invalid_cycle, _, _ = _corrective_result(
        wpm3_db, tmp_path,
        lambda value: value["boundary_snapshot"].update(last_ready_seq=1))
    assert invalid_cycle.kind == "stale_admission"


def test_wpm3_corrective_refused_exhaustion_only_row(wpm3_db, tmp_path):
    result, _, _ = _corrective_result(
        wpm3_db, tmp_path, lambda value: value.pop("boundary_snapshot"))
    assert result.kind == "stale_admission"


def test_wpm3_corrective_refused_on_member_set_mismatch(wpm3_db, tmp_path):
    result, _, _ = _corrective_result(wpm3_db, tmp_path, add_member=True)
    assert result.kind == "stale_admission"


def test_wpm3_corrective_refused_on_fingerprint_mismatch(wpm3_db, tmp_path):
    path = tmp_path / "fingerprint.jsonl"
    path.write_text(json.dumps({"type": "user", "message": "other"}) + "\n")
    create_transcript_binding("receiver", "session", str(path), path.stat().st_ino, "test")
    message, prior = _settled_with_evidence(_full_corrective(path))
    proof = make_admission_proof("corrective", [message.id], prior)
    with wpm3_db.begin() as db:
        row = db.get(InboxDeliveryAttemptModel, prior)
        changed = json.loads(row.evidence)
        changed["boundary_exhausted_at"] = "2030-01-02T00:00:00Z"
        row.evidence = json.dumps(changed)
    stale = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "next2", 5,
        prior_attempt_uuid=prior, admission_proof=proof)
    assert stale.kind == "stale_admission"


def test_wpm3_corrective_opens_with_full_fingerprint(wpm3_db, tmp_path):
    result, message, prior = _corrective_result(wpm3_db, tmp_path)
    assert result.kind == "opened" and result.attempt_uuid
    settle_delivery_attempt(
        result.attempt_uuid, MessageStatus.PENDING, "interrupted",
        reason="pane_unresolvable", evidence="{}")
    proof = make_admission_proof("corrective", [message.id], prior)
    successor = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "duplicate", 9,
        prior_attempt_uuid=prior, admission_proof=proof)
    assert successor.kind == "stale_admission"


def test_wpm3_corrective_admission_ignores_proven_pre_paste_successor(
    wpm3_db: Any, tmp_path: Path,
) -> None:
    result, message, prior = _corrective_result(  # type: ignore[no-untyped-call]
        wpm3_db, tmp_path)
    assert result.kind == "opened" and result.attempt_uuid
    settle_delivery_attempt(
        result.attempt_uuid, MessageStatus.PENDING, "interrupted",
        reason="terminal_not_found", evidence="{}")
    proof = make_admission_proof("corrective", [message.id], prior)
    successor = begin_delivery_attempt_if_no_other_delivering(
        [message], "receiver", "claude_code", "retry", 5,
        prior_attempt_uuid=prior, admission_proof=proof)
    assert successor.kind == "opened"


def _invalid_snapshot_gate(attempts, *, notice=None):
    service = InboxService()
    message = SimpleNamespace(
        id=1, sender_id="sender", receiver_id="receiver", message="payload",
        orchestration_type=OrchestrationType.SEND_MESSAGE)
    with (
        patch.object(service, "_exact_batch_attempts", return_value=attempts),
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent"})),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=TranscriptResolution(Path("/trace"), "binding", 1)),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.merge_wpm1_attempt_evidence",
              return_value=True) as merge,
        patch("cli_agent_orchestrator.services.inbox_service.record_wpm1_stalled_notice",
              return_value="recorded") as notice_mock,
        patch("cli_agent_orchestrator.services.inbox_service.settle_wpm1_terminal_batch") as settle,
    ):
        monitor.get_boundary_observation.return_value = SimpleNamespace(status="invalid")
        monitor.get_status.return_value = TerminalStatus.COMPLETED
        state, detail = service._handle_wpm1_gate(
            "receiver", [message], {"provider": "claude_code"}, None,
            "sender", OrchestrationType.SEND_MESSAGE)
    return state, detail, merge, notice_mock, settle


def _raw_attempt(index=0, *, age_hours=0):
    evidence = {
        "resolution_kind": "binding",
        "injection_completed_seq": {"observation_epoch": "epoch", "seq": 1},
    }
    if age_hours:
        evidence["last_activity_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return {
        "attempt_uuid": f"a{index}", "provider": "claude_code",
        "payload_hash": "hash", "outcome": "ambiguous",
        "reason": "confirmation_timeout", "evidence": json.dumps(evidence),
        "started_at": datetime.now(timezone.utc),
        "settled_at": datetime.now(timezone.utc) - timedelta(hours=age_hours),
        "prior_attempt_uuid": None,
    }


def test_wpm3_invalid_snapshot_transient_no_loss_increment():
    state, detail, merge, _, settle = _invalid_snapshot_gate([_raw_attempt()])
    assert state == "skip_d2_only"
    assert detail["protection_reason"] == "transient_snapshot_unavailable"
    assert all("boundary_exhausted_at" not in call.args[2] for call in merge.call_args_list)
    settle.assert_not_called()


def test_wpm3_invalid_snapshot_cannot_exhaust_cap():
    state, _, merge, _, settle = _invalid_snapshot_gate(
        [_raw_attempt(0), _raw_attempt(1), _raw_attempt(2)])
    assert state == "skip_d2_only"
    assert all("boundary_exhausted_at" not in call.args[2] for call in merge.call_args_list)
    settle.assert_not_called()


def test_wpm3_invalid_snapshot_notices_before_skip():
    state, _, _, notice, _ = _invalid_snapshot_gate([_raw_attempt(age_hours=5)])
    assert state == "skip_d2_only"
    notice.assert_called_once()


def test_wpm3_invalid_snapshot_releases_disjoint_work(wpm3_db):
    protected = create_inbox_message("sender", "receiver", "protected")
    evidence = {"resolution_kind": "binding", "injection_completed_seq": {
        "observation_epoch": "epoch", "seq": 1}}
    prior = begin_delivery_attempt(
        [protected], "receiver", "claude_code", "protected", 9,
        evidence=json.dumps(evidence))
    settle_delivery_attempt(
        prior, MessageStatus.PENDING, "ambiguous", reason="confirmation_timeout",
        evidence=json.dumps(evidence))
    later = create_inbox_message("sender2", "receiver", "later")
    submitted = _submit(TerminalStatus.COMPLETED)

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    with (
        patch("cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
              return_value=("absent", {"kind": "transcript_absent"})),
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=TranscriptResolution(Path("/trace"), "binding", 1)),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              side_effect=lambda _id, payload, *_args: payload),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send) as paste,
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              return_value=("hit", {"kind": "transcript_user_turn"})),
    ):
        monitor.get_boundary_observation.return_value = SimpleNamespace(status="invalid")
        monitor.get_status.return_value = TerminalStatus.COMPLETED
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        InboxService().deliver_pending("receiver")
    assert paste.call_count == 1
    assert get_message_trace(protected.id)["message"]["status"] == "pending"
    assert get_message_trace(later.id)["message"]["status"] == "delivered"


def test_wpm3_no_production_cycle_bypass_flag():
    source = inspect.getsource(InboxService._handle_wpm1_gate)
    assert "legacy_snapshot_seam" not in source


def _post_submit_multi_group(wpm3_db, error, proof_result="settled"):
    first = create_inbox_message("sender", "receiver", "one")
    second = create_inbox_message("sender2", "receiver", "two")
    submitted = _submit(TerminalStatus.COMPLETED)
    prepared = []

    def prepare(_terminal, payload, *_args):
        prepared.append(payload)
        return payload

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch("cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
              return_value=None),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
              side_effect=prepare),
        patch("cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
              side_effect=send),
        patch("cli_agent_orchestrator.services.inbox_service.confirm_delivery",
              side_effect=error),
        patch("cli_agent_orchestrator.services.inbox_service.settle_delivery_attempt_proof_safe",
              return_value=proof_result) as proof_safe,
    ):
        monitor.get_boundary_observation.return_value = _observation(TerminalStatus.IDLE)
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        InboxService().deliver_pending("receiver", num_messages=0)
    assert prepared == ["one"]
    proof_safe.assert_called_once()
    assert get_message_trace(second.id)["attempts"] == []
    return first, second


def test_wpm3_post_submit_settled_stops_wake_multi_group(wpm3_db):
    _post_submit_multi_group(wpm3_db, RuntimeError("tail"), "settled")


def test_wpm3_post_submit_recovery_stops_wake_multi_group(wpm3_db):
    _post_submit_multi_group(
        wpm3_db, RuntimeError("tail"), "settlement_pending_recovery")


def test_wpm3_post_submit_terminal_not_found_stops_wake_multi_group(wpm3_db):
    _post_submit_multi_group(wpm3_db, TerminalNotFoundError("gone"), "settled")


@pytest.mark.parametrize("error", [
    RuntimeError("generic"), TerminalNotFoundError("gone"),
    DeliveryDeferredError("deferred"), TerminalInputBlockedError("blocked"),
])
def test_wpm3_outer_arms_post_submit_table(wpm3_db, error):
    _post_submit_multi_group(wpm3_db, error, "settled")


def _seed_epoch_maps(monitor, terminal_id="term"):
    with monitor._lock:
        monitor._observation_epoch[terminal_id] = "old"
        monitor._observation_seq[terminal_id] = 9
        monitor._last_non_ready_seq[terminal_id] = 7
        monitor._last_ready_seq[terminal_id] = 8


def test_wpm3_epoch_maps_popped_on_terminal_free():
    monitor = StatusMonitor()
    _seed_epoch_maps(monitor)
    monitor.clear_terminal("term")
    for mapping in (monitor._observation_epoch, monitor._observation_seq,
                    monitor._last_non_ready_seq, monitor._last_ready_seq):
        assert "term" not in mapping


def test_wpm3_reset_buffer_keeps_entries_opens_fresh_epoch():
    monitor = StatusMonitor()
    _seed_epoch_maps(monitor)
    monitor.reset_buffer("term")
    assert monitor._observation_epoch["term"] != "old"
    assert monitor._observation_seq["term"] == 0
    assert "term" not in monitor._last_non_ready_seq
    assert "term" not in monitor._last_ready_seq

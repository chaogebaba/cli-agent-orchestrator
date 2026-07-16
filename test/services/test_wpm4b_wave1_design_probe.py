"""Promoted reviewer probe for WPM4-B Wave 1 design gate r1.

Claims attacked:
- A redelivery marker can remain the first pasted line while transcript
  confirmation hashes the exact tagged wire bytes.
- A parked PENDING row becomes unreachable after hard terminal deletion.

Expected post-fix semantics:
- Shape the original body first, prefix the shaped bytes, hash/send/confirm those
  exact bytes, and retain explicit tag evidence on the linked attempt.
- Orphan settlement atomically marks receiver-gone rows and enqueues at most one
  notice to each live original sender.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    begin_delivery_attempt,
    get_message_trace,
    get_pending_messages,
    settle_delivery_attempt,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
    confirm_delivery,
    wire_hash,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm4b.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _binding() -> TranscriptResolution:
    path = __import__("pathlib").Path("/trace")
    return TranscriptResolution(
        path,
        "binding",
        TranscriptLiveReference(path, 1, 20),
    )


def _observation(seq: int, *, non_ready: int | None, ready: int | None) -> BoundaryObservation:
    return BoundaryObservation(
        "wpm4b-epoch",
        TerminalStatus.IDLE,
        3,
        1,
        seq,
        non_ready,
        ready,
    )


def test_tagged_shaped_wire_confirms_and_original_hash_does_not(tmp_path):
    marker = (
        "[redelivery of attempt 990f103f - prior delivery unconfirmed; "
        "ignore if already received]"
    )
    shaped_original = "<cao-memory>restart context</cao-memory>\n\noriginal callback"
    tagged_wire = f"{marker}\n{shaped_original}"
    assert tagged_wire.startswith(marker)
    assert wire_hash(tagged_wire) != wire_hash(shaped_original)

    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-07-15T12:00:01Z",
                "message": {"role": "user", "content": tagged_wire},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    resolution = TranscriptResolution(transcript, "binding")
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
        return_value=resolution,
    ):
        tagged_outcome, _ = confirm_delivery(
            {}, wire_hash(tagged_wire), "2026-07-15T12:00:00Z", timeout=0.05
        )
        original_outcome, _ = confirm_delivery(
            {}, wire_hash(shaped_original), "2026-07-15T12:00:00Z", timeout=0.05
        )
    assert tagged_outcome == "hit"
    assert original_outcome == "ambiguous"


def test_tagged_queued_command_confirms_with_exact_wire_hash(tmp_path):
    tagged_wire = "[redelivery of attempt 990f103f]\ncallback"
    transcript = tmp_path / "queued.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-07-15T12:00:01Z",
                "attachment": {"type": "queued_command", "prompt": tagged_wire},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    resolution = TranscriptResolution(transcript, "binding")
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
        return_value=resolution,
    ):
        outcome, evidence = confirm_delivery(
            {}, wire_hash(tagged_wire), "2026-07-15T12:00:00Z", timeout=0.05
        )
    assert outcome == "hit"
    assert evidence["kind"] == "transcript_queued_command"


def test_hard_delete_settles_pending_and_notifies_original_sender_once(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code", caller_id="sender")
    message = database.create_inbox_message("sender", "receiver", "callback")
    with scratch_db.begin() as db:
        db.get(InboxModel, message.id).created_at = datetime.now() - timedelta(minutes=5)

    assert database.list_pending_receiver_ids_older_than(1) == ["receiver"]
    assert database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)[
        "terminal_deleted"
    ]
    assert database.list_pending_receiver_ids_older_than(1) == []

    service = InboxService()
    first = service.reconcile_pending_orphans()
    second = service.reconcile_pending_orphans()
    trace = get_message_trace(message.id)
    assert first.settled_count == 1 and first.notification_count == 1
    assert second.settled_count == 0 and second.notification_count == 0
    assert trace["message"]["status"] == MessageStatus.DELIVERY_FAILED.value
    assert trace["message"]["failure_reason"] == "receiver_gone"
    with scratch_db() as db:
        notices = (
            db.query(InboxModel)
            .filter(
                InboxModel.message.startswith(f"p5-orphan receiver=receiver batch={message.id}\n")
            )
            .all()
        )
    assert len(notices) == 1
    assert notices[0].receiver_id == "sender"


def test_orphan_notice_failure_rolls_back_pending_settlement(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "callback")
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)
    engine = scratch_db.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_p5_notice BEFORE INSERT ON inbox "
                "WHEN NEW.message LIKE 'p5-orphan %' "
                "BEGIN SELECT RAISE(ABORT, 'notice rejected'); END"
            )
        )

    with pytest.raises(Exception, match="notice rejected"):
        InboxService().reconcile_pending_orphans()
    with scratch_db() as db:
        row = db.get(InboxModel, message.id)
        assert row.status == MessageStatus.PENDING.value
        assert row.failure_reason is None


def test_lawful_row_present_states_are_not_orphans(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("old", "s", "old", "claude_code", caller_id="sender")
    database.create_terminal(
        "replacement",
        "s",
        "replacement",
        "claude_code",
        caller_id="sender",
        provider_session_id="session-uuid",
    )
    pending = database.create_inbox_message("sender", "old", "callback")
    database.set_terminal_recovery_state("old", "fallback_starting")
    assert database.settle_terminal_fallback("old", "replacement") == 1
    assert database.get_terminal_metadata("old") is not None
    with scratch_db() as db:
        assert db.get(InboxModel, pending.id).receiver_id == "replacement"

    owner = "12345678-1234-1234-1234-123456789abc"
    database.create_terminal(
        "initializing",
        "s",
        "initializing",
        "claude_code",
        caller_id="sender",
        init_state="init_pending",
        init_started_at=datetime.now(timezone.utc),
        init_owner_epoch=owner,
        init_deadline_s=10.0,
    )
    init_message = database.create_inbox_message("sender", "initializing", "queued")
    with scratch_db.begin() as db:
        db.get(InboxModel, init_message.id).created_at = datetime.now() - timedelta(minutes=5)
    assert "initializing" in database.list_pending_receiver_ids_older_than(1)
    assert InboxService().reconcile_pending_orphans().settled_count == 0


def test_orphan_reconciliation_drains_oldest_hundred_then_remainder(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    messages = [
        database.create_inbox_message("sender", "receiver", f"payload-{index}")
        for index in range(105)
    ]
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)

    first = InboxService().reconcile_pending_orphans()
    assert first.settled_count == 100 and first.notification_count == 1
    with scratch_db() as db:
        statuses = {
            row.id: row.status
            for row in db.query(InboxModel)
            .filter(InboxModel.id.in_([message.id for message in messages]))
            .all()
        }
    assert all(
        statuses[message.id] == MessageStatus.DELIVERY_FAILED.value for message in messages[:100]
    )
    assert all(statuses[message.id] == MessageStatus.PENDING.value for message in messages[100:])

    second = InboxService().reconcile_pending_orphans()
    assert second.settled_count == 5 and second.notification_count == 1
    assert InboxService().reconcile_pending_orphans().settled_count == 0
    with scratch_db() as db:
        assert (
            db.query(InboxModel)
            .filter(
                InboxModel.id.in_([message.id for message in messages]),
                InboxModel.status != MessageStatus.DELIVERY_FAILED.value,
            )
            .count()
            == 0
        )
        assert db.query(InboxModel).filter(InboxModel.message.startswith("p5-orphan ")).count() == 2


def test_orphan_sender_absent_is_logged_only_without_notice(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "payload")
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)
    database.delete_terminal_and_warm_intent("sender", preserve_warm_intent=False)

    result = InboxService().reconcile_pending_orphans()
    assert result.settled_count == 1
    assert result.notification_count == 0
    assert result.logged_only_count == 1
    with scratch_db() as db:
        row = db.get(InboxModel, message.id)
        assert row.status == MessageStatus.DELIVERY_FAILED.value
        assert row.failure_reason == "receiver_gone"
        assert db.query(InboxModel).filter(InboxModel.message.startswith("p5-orphan ")).count() == 0


def test_real_deliver_pending_replay_tags_exact_wire_and_trace(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "callback")
    shaped = "<cao-memory>restart context</cao-memory>\n\ncallback"
    initial = _observation(0, non_ready=None, ready=0)
    submitted = _observation(1, non_ready=None, ready=1)
    boundary = _observation(4, non_ready=2, ready=4)
    wires: list[str] = []
    confirmed_digests: list[str] = []

    def send(_terminal_id, wire, **kwargs):
        wires.append(wire)
        callback = kwargs.get("on_submitted")
        if callback is not None:
            callback(submitted)
        return submitted

    def confirm(_metadata, digest, _started_at, _evidence=None):
        confirmed_digests.append(digest)
        if len(confirmed_digests) == 1:
            return (
                "ambiguous",
                {
                    "path": "/trace",
                    "inode": 1,
                    "size": 20,
                    "resolution_kind": "binding",
                },
            )
        return "hit", {"kind": "transcript_user_turn", "padding": "x" * 3000}

    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    service = InboxService()
    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=_binding(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service._wpm2_lookup",
            return_value=("absent", {}),
        ),
        patch(
            "cli_agent_orchestrator.services.message_trace_service."
            "bounded_transcript_suffix_lookup",
            return_value=("absent", {}),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            return_value=shaped,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            side_effect=confirm,
        ),
        patch.object(service, "_commit_watchdog_ops"),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        observations = iter([initial, boundary])
        monitor.get_boundary_observation.side_effect = lambda _terminal_id: next(
            observations, boundary
        )
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 3
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        service.deliver_pending("receiver")
        service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    prior, corrective = trace["attempts"]
    marker = (
        f"[redelivery of attempt {prior['attempt_uuid'][:8]} - prior delivery unconfirmed; "
        "ignore if already received]"
    )
    tagged_wire = f"{marker}\n{shaped}"
    assert wires == [shaped, tagged_wire]
    assert corrective["prior_attempt_uuid"] == prior["attempt_uuid"]
    assert corrective["payload_hash"] == wire_hash(tagged_wire)
    assert corrective["payload_length"] == len(tagged_wire.encode())
    assert confirmed_digests == [wire_hash(shaped), wire_hash(tagged_wire)]
    assert corrective["outcome"] == "confirmed"
    assert corrective["evidence"]["redelivery_tag"] == {
        "version": 1,
        "prior_attempt_uuid": prior["attempt_uuid"],
    }
    assert corrective["evidence"]["padding"] == "x" * 3000
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value


def test_failed_pre_paste_attempt_retries_without_tag(scratch_db):
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "callback")
    prior = begin_delivery_attempt(
        [message], "receiver", "claude_code", wire_hash("callback"), len("callback")
    )
    settle_delivery_attempt(
        prior,
        MessageStatus.PENDING,
        "failed",
        reason="send_failed_before_paste",
    )
    sent = MagicMock(return_value=_observation(1, non_ready=None, ready=1))
    service = InboxService()
    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=_binding(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            return_value="callback",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            sent,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {}),
        ),
        patch.object(service, "_commit_watchdog_ops"),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        monitor.get_boundary_observation.return_value = _observation(0, non_ready=None, ready=0)
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 3
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE, {"result_status": "idle"}
        )
        service.deliver_pending("receiver")

    sent.assert_called_once()
    assert sent.call_args.args[1] == "callback"
    attempts = get_message_trace(message.id)["attempts"]
    assert attempts[-1]["prior_attempt_uuid"] is None
    assert "redelivery_tag" not in attempts[-1]["evidence"]

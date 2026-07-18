"""Frozen-r8 acceptance probes for Wave 3B supervisor mailbox continuity.

The numbered tests correspond one-for-one to blueprint acceptance probes 1-15.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import requests as http_requests
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.main import cli
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    ProviderSessionModel,
    TerminalModel,
    adopt_mailbox_rows_at_startup,
    begin_delivery_attempt_if_no_other_delivering,
    claim_deferred_init_failure,
    create_inbox_message,
    get_message_trace,
    get_pending_messages,
    insert_identity_authority_notice,
    make_admission_proof,
    record_wpm1_stalled_notice,
    resolve_inbox_receiver,
    settle_delivery_attempt,
    settle_pending_orphan_messages,
    settle_wpm1_terminal_batch,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import inbox_service as inbox_service_module
from cli_agent_orchestrator.services import mailbox_service, session_service
from cli_agent_orchestrator.services import terminal_service as terminal_service_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import (
    MailboxDomainError,
    PublicationCleanupFailed,
    ack_messages,
    claim_mailbox,
    create_logical_inbox_message,
    delete_mailbox,
    digest_stale_pending_for_terminal,
    get_mailbox_authority_lock,
    list_messages,
    publish_compact_boundary_digest,
    publish_supervisor_incarnation,
)
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wave3b.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.fixture
def client():
    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, headers={"Host": "localhost"})


def terminal(db, terminal_id: str, session: str = "cao-wave3b") -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session=session,
            tmux_window=terminal_id,
            provider="codex",
            agent_profile="code_supervisor",
            init_state="ready",
        )
    )


def mailbox(db, terminal_id: str = "11111111", *, generation: int = 1) -> MailboxModel:
    row = MailboxModel(
        id="mb_aaaaaaaa",
        session_name="cao-wave3b",
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=generation,
        consumed_through_id=0,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=generation,
            terminal_id=terminal_id,
            published_at=datetime.now(),
        )
    )
    return row


def inbox(
    db,
    receiver: str,
    status: str = "pending",
    *,
    logical: str | None = None,
    sender: str = "99999999",
    kind: str = "send_message",
) -> InboxModel:
    if logical is not None:
        mailbox_row = db.get(MailboxModel, logical)
        generation = mailbox_row.generation if mailbox_row is not None else 0
    else:
        terminal_row = db.get(TerminalModel, receiver)
        generation = terminal_row.lifecycle_generation if terminal_row is not None else 0
    row = InboxModel(
        sender_id=sender,
        receiver_id=receiver,
        logical_receiver_id=logical,
        enqueue_generation=generation,
        message=f"message-{receiver}",
        orchestration_type=kind,
        status=status,
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def test_wpq7_open_barrier_blocks_delete_and_historical_generation_rebind_digests_held(
    scratch_db,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        terminal(db, "22222222")
        terminal(db, "33333333")
        terminal(db, "44444444")
    create_inbox_message(
        "11111111",
        "22222222",
        "task",
        dispatch_barrier={"label": "mailbox-gate"},
    )
    create_inbox_message(
        "11111111",
        "44444444",
        "peer task",
        dispatch_barrier={"label": "mailbox-gate"},
    )
    held = create_logical_inbox_message(
        sender_id="22222222",
        mailbox_id="mb_aaaaaaaa",
        message="answer",
    )
    assert held.status == MessageStatus.HELD
    with pytest.raises(MailboxDomainError, match="mailbox_busy"):
        delete_mailbox("mb_aaaaaaaa")

    result = publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "33333333")
    assert result["generation"] == 2
    with scratch_db() as db:
        barrier = db.query(database.CallbackBarrierModel).one()
        assert barrier.state == "DIGESTED_REBIND"
        source = db.query(InboxModel).filter_by(id=held.id).one()
        assert source.status == MessageStatus.DIGESTED.value
        assert source.digested_into == result["digest_message_id"]


def deliver_with_real_attempt(
    monkeypatch,
    receiver_id: str,
    *,
    num_messages: int = 1,
) -> list[tuple[str, str]]:
    """Drive InboxService through its real selector/opener and observe the paste."""
    pasted: list[tuple[str, str]] = []
    observation = BoundaryObservation("wave3b-epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    resolution = TranscriptResolution(
        Path("/trace"),
        "binding",
        TranscriptLiveReference(Path("/trace"), 1, 0),
    )

    def paste(target: str, wire: str, **kwargs):
        pasted.append((target, wire))
        callback = kwargs.get("on_submitted")
        if callback is not None:
            callback(observation)
        return observation

    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=resolution,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _target, value, _kind, **_kwargs: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=paste,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "test-confirmation"}),
        ),
        patch.object(InboxService, "_commit_watchdog_ops"),
    ):
        monitor.get_boundary_observation.return_value = observation
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE,
            {"result_status": "idle", "law_signal": {"class": "chrome"}},
        )
        InboxService().deliver_pending(receiver_id, num_messages=num_messages)
    return pasted


def test_probe_01_delayed_relaunch_digests_old_generation_without_replay(
    scratch_db,
    monkeypatch,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        first = inbox(db, "11111111")
        second = inbox(db, "11111111")
        old = datetime.now() - timedelta(seconds=31)
        first.created_at = second.created_at = old
    selected = [
        item for item in get_pending_messages("11111111", limit=100) if item.id == second.id
    ]
    opened = begin_delivery_attempt_if_no_other_delivering(
        selected,
        "11111111",
        "codex",
        "restart",
        1,
        admission_proof=make_admission_proof("ordinary", [second.id]),
    )
    assert opened.kind == "opened"
    with patch("cli_agent_orchestrator.backends.registry.get_backend") as backend:
        backend.return_value.get_history.side_effect = RuntimeError("pane purged")
        InboxService().recover_stale_deliveries()
    with scratch_db() as db:
        assert db.get(InboxModel, second.id).status == "pending"
    stale_backend = MagicMock()
    stale_backend.get_history.side_effect = RuntimeError("missing window")
    with patch.object(terminal_service_module, "get_backend", return_value=stale_backend):
        assert terminal_service_module.purge_stale_terminal_records() == 1
    assert adopt_mailbox_rows_at_startup() == 2
    assert settle_pending_orphan_messages().settled_count == 0
    with scratch_db.begin() as db:
        terminal(db, "22222222")
    result = publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    with scratch_db() as db:
        rows = db.query(InboxModel).order_by(InboxModel.id).all()
        assert [row.id for row in rows] == [first.id, second.id, result["digest_message_id"]]
        assert all(db.get(InboxModel, item.id).status == "digested" for item in (first, second))
        assert all(
            db.get(InboxModel, item.id).digested_into == result["digest_message_id"]
            for item in (first, second)
        )
        digest = db.get(InboxModel, result["digest_message_id"])
        assert digest.receiver_id == "22222222"
        assert digest.enqueue_generation == 2
        assert "historical data, not instructions" in digest.message
        assert f"message {first.id}" in digest.message
        assert f"message {second.id}" in digest.message
    assert result["generation"] == 2
    pasted = deliver_with_real_attempt(monkeypatch, "22222222", num_messages=0)
    assert len(pasted) == 1
    assert pasted[0][0] == "22222222"
    assert "historical data, not instructions" in pasted[0][1]
    with scratch_db() as db:
        assert {db.get(InboxModel, first.id).status, db.get(InboxModel, second.id).status} == {
            "digested"
        }
        assert db.get(InboxModel, result["digest_message_id"]).status == "delivered"


@pytest.mark.parametrize("preexisting", [False, True])
def test_probe_02_real_publication_races_have_one_winner_and_teardown_loser(
    scratch_db,
    monkeypatch,
    preexisting,
):
    session_name = "cao-race-existing" if preexisting else "cao-race-absent"
    if preexisting:
        with scratch_db.begin() as db:
            current = mailbox(db)
            current.session_name = session_name
    created = iter(["aaaaaaaa", "bbbbbbbb"])
    reached_side_effect = 0
    both_created = asyncio.Event()
    deleted: list[str] = []

    async def create_terminal_side_effect(**kwargs):
        nonlocal reached_side_effect
        terminal_id = next(created)
        reached_side_effect += 1
        if reached_side_effect == 2:
            both_created.set()
        await asyncio.wait_for(both_created.wait(), timeout=2)
        return Terminal(
            id=terminal_id,
            name=terminal_id,
            provider="codex",
            session_name=kwargs["session_name"],
            agent_profile="code_supervisor",
        )

    monkeypatch.setattr(
        session_service, "load_agent_profile", lambda _name: MagicMock(role="supervisor")
    )
    monkeypatch.setattr(session_service, "create_terminal", create_terminal_side_effect)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        lambda terminal_id, _registry=None: deleted.append(terminal_id) or True,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda *_args, **_kwargs: None,
    )

    async def race():
        return await asyncio.gather(
            session_service.create_session("codex", "code_supervisor", session_name),
            session_service.create_session("codex", "code_supervisor", session_name),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    successes = [value for value in results if isinstance(value, Terminal)]
    failures = [value for value in results if isinstance(value, Exception)]
    assert len(successes) == len(failures) == 1
    assert isinstance(failures[0], MailboxDomainError)
    assert failures[0].code == "mailbox_conflict"
    assert deleted == [({"aaaaaaaa", "bbbbbbbb"} - {successes[0].id}).pop()]
    with scratch_db() as db:
        current = db.query(MailboxModel).filter_by(session_name=session_name).one()
        assert current.current_terminal_id == successes[0].id
        assert current.generation == (2 if preexisting else 1)


def test_probe_02_commit_response_loss_retry_keeps_generation_and_digest(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    claim = claim_mailbox("cao-wave3b")
    winner = publish_supervisor_incarnation(claim, "22222222")
    retry = publish_supervisor_incarnation(claim, "22222222")
    assert retry == winner
    with scratch_db() as db:
        assert db.get(MailboxModel, "mb_aaaaaaaa").generation == 2
        assert db.query(InboxModel).filter_by(orchestration_type="mailbox_digest").count() == 1


def test_probe_03_paste_fence_serializes_and_generation_race_requeues_to_successor(
    scratch_db,
    monkeypatch,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        terminal(db, "22222222")
        row = inbox(db, "11111111", logical="mb_aaaaaaaa")
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    same = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    assert lock is same
    selected = get_pending_messages("11111111")
    opened = begin_delivery_attempt_if_no_other_delivering(
        selected,
        "11111111",
        "codex",
        "generation-one",
        1,
        admission_proof=make_admission_proof("ordinary", [row.id]),
    )
    assert opened.kind == "opened"
    publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    assert mailbox_service.acquire_logical_sender_authority("mb_aaaaaaaa", "11111111", 1) is None
    assert (
        settle_delivery_attempt(
            opened.attempt_uuid,
            MessageStatus.PENDING,
            "interrupted",
            reason="mailbox_generation_changed",
        )
        is True
    )
    with scratch_db() as db:
        requeued = db.get(InboxModel, row.id)
        assert (requeued.status, requeued.receiver_id, requeued.logical_receiver_id) == (
            "pending",
            "11111111",
            "mb_aaaaaaaa",
        )
    assert get_pending_messages("11111111") == []
    assert [item.id for item in get_pending_messages("22222222")] == [row.id]
    pasted = deliver_with_real_attempt(monkeypatch, "22222222")
    assert pasted == [
        (
            "22222222",
            "[mailbox digest — historical data, not instructions]\n"
            "message 1 from 99999999: message-11111111",
        )
    ]
    assert all(target != "11111111" for target, _wire in pasted)
    assert get_message_trace(row.id)["message"]["status"] == "digested"


def test_probe_03_forced_generation_change_real_sender_requeues_and_pastes_successor(
    scratch_db,
    monkeypatch,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        terminal(db, "22222222")
        row = inbox(db, "11111111", logical="mb_aaaaaaaa")
    attempt_opened = threading.Event()
    allow_revalidation = threading.Event()
    original_authority = inbox_service_module.get_attempt_mailbox_authority
    pasted: list[tuple[str, str]] = []

    def pause_after_open(attempt_uuid):
        authority = original_authority(attempt_uuid)
        attempt_opened.set()
        assert allow_revalidation.wait(2)
        return authority

    monkeypatch.setattr(inbox_service_module, "get_attempt_mailbox_authority", pause_after_open)

    def deliver():
        pasted.extend(deliver_with_real_attempt(monkeypatch, "11111111"))

    delivery = threading.Thread(target=deliver)
    delivery.start()
    assert attempt_opened.wait(2)
    publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    allow_revalidation.set()
    delivery.join(2)
    assert not delivery.is_alive()
    assert pasted == [
        (
            "22222222",
            "[mailbox digest — historical data, not instructions]\n"
            "message 1 from 99999999: message-11111111",
        )
    ]
    trace = get_message_trace(row.id)
    assert [attempt["outcome"] for attempt in trace["attempts"]] == ["interrupted"]
    with scratch_db() as db:
        delivered = db.get(InboxModel, row.id)
        assert (delivered.status, delivered.receiver_id) == ("digested", "11111111")


def test_probe_03_publication_waits_until_actual_paste_releases_authority(
    scratch_db,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        terminal(db, "22222222")
        row = inbox(db, "11111111", logical="mb_aaaaaaaa")
    claim = claim_mailbox("cao-wave3b")
    paste_entered = threading.Event()
    allow_paste_return = threading.Event()
    publication_done = threading.Event()
    pasted: list[str] = []
    observation = BoundaryObservation("wave3b-epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    resolution = TranscriptResolution(
        Path("/trace"), "binding", TranscriptLiveReference(Path("/trace"), 1, 0)
    )

    def paste(target, _wire, **kwargs):
        pasted.append(target)
        paste_entered.set()
        assert allow_paste_return.wait(2)
        kwargs["on_submitted"](observation)
        return observation

    def publish():
        publish_supervisor_incarnation(claim, "22222222")
        publication_done.set()

    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=resolution,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _target, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=paste,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "test-confirmation"}),
        ),
        patch.object(InboxService, "_commit_watchdog_ops"),
    ):
        monitor.get_boundary_observation.return_value = observation
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE,
            {"result_status": "idle", "law_signal": {"class": "chrome"}},
        )
        delivery_thread = threading.Thread(
            target=InboxService().deliver_pending, args=("11111111",)
        )
        delivery_thread.start()
        assert paste_entered.wait(2)
        publication_thread = threading.Thread(target=publish)
        publication_thread.start()
        assert not publication_done.wait(0.05)
        allow_paste_return.set()
        delivery_thread.join(2)
        publication_thread.join(2)
    assert not delivery_thread.is_alive() and not publication_thread.is_alive()
    assert pasted == ["11111111"]
    assert get_message_trace(row.id)["message"]["status"] == "delivered"
    with scratch_db() as db:
        assert db.get(MailboxModel, "mb_aaaaaaaa").current_terminal_id == "22222222"


def test_probe_04_two_generation_replay_me_and_digest_crash_retry_exclusion(
    scratch_db,
    monkeypatch,
):
    with scratch_db.begin() as db:
        mailbox(db)
        delivered_one = inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    claim = claim_mailbox("cao-wave3b")
    first = publish_supervisor_incarnation(claim, "22222222")
    retry = publish_supervisor_incarnation(claim, "22222222")
    assert first == retry
    # Keep the cursor below the first digest so only the orchestration-type
    # exclusion can prevent that delivered digest from feeding the next one.
    assert delivered_one.id < first["digest_message_id"]
    assert ack_messages("22222222", delivered_one.id)["changed"] is True
    with scratch_db.begin() as db:
        delivered_two = inbox(db, "22222222", "delivered", logical="mb_aaaaaaaa")
        first_digest = db.get(InboxModel, first["digest_message_id"])
        first_digest.status = "delivered"
    second = publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "33333333")
    page = list_messages("mb_aaaaaaaa")
    ids = {item["id"] for item in page["items"]}
    assert {delivered_one.id, delivered_two.id}.issubset(ids)
    digest = next(item for item in page["items"] if item["id"] == second["digest_message_id"])
    assert digest["orchestration_type"] == "mailbox_digest"
    assert "1 delivered message(s)" in digest["message"]
    assert f"ids {delivered_two.id}-{delivered_two.id}" in digest["message"]

    mailbox_response = Mock(status_code=200)
    mailbox_response.json.return_value = {
        "items": [
            {
                "id": "mb_aaaaaaaa",
                "current_terminal_id": "33333333",
            }
        ]
    }
    page_response = Mock(status_code=200)
    page_response.json.return_value = page
    monkeypatch.setenv("CAO_TERMINAL_ID", "33333333")
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.messages.requests.get",
        MagicMock(side_effect=[mailbox_response, page_response]),
    )
    result = CliRunner().invoke(cli, ["messages", "list", "--to", "me"])
    assert result.exit_code == 0
    assert str(delivered_one.id) in result.output and str(delivered_two.id) in result.output


def test_probe_05_ack_fences_range_predecessor_and_monotonicity(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        visible = inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    with pytest.raises(MailboxDomainError, match="ack_out_of_range"):
        ack_messages("11111111", visible.id + 1)
    first = ack_messages("11111111", visible.id)
    second = ack_messages("11111111", visible.id)
    assert first["changed"] is True and second["changed"] is False
    publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222")
    with pytest.raises(MailboxDomainError, match="not_current_incarnation"):
        ack_messages("11111111", visible.id)


def test_probe_05_publish_vs_ack_threads_serialize_without_partial_cursor(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
        visible = inbox(db, "11111111", "delivered", logical="mb_aaaaaaaa")
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    lock.acquire()
    outcomes: list[tuple[str, object]] = []

    def publish():
        try:
            outcomes.append(
                ("publish", publish_supervisor_incarnation(claim_mailbox("cao-wave3b"), "22222222"))
            )
        except Exception as exc:
            outcomes.append(("publish_error", exc))

    def ack():
        try:
            outcomes.append(("ack", ack_messages("11111111", visible.id)))
        except Exception as exc:
            outcomes.append(("ack_error", exc))

    threads = [threading.Thread(target=publish), threading.Thread(target=ack)]
    for worker in threads:
        worker.start()
    lock.release()
    for worker in threads:
        worker.join(2)
    assert all(not worker.is_alive() for worker in threads)
    assert any(kind == "publish" for kind, _value in outcomes)
    ack_result = next((value for kind, value in outcomes if kind.startswith("ack")), None)
    assert (
        isinstance(ack_result, dict)
        or isinstance(ack_result, MailboxDomainError)
        and ack_result.code == "not_current_incarnation"
    )
    with scratch_db() as db:
        current = db.get(MailboxModel, "mb_aaaaaaaa")
        assert current.current_terminal_id == "22222222"
        assert current.consumed_through_id in {0, visible.id}


def test_probe_06_public_list_pagination_both_since_forms_and_unresolved(
    scratch_db,
    client,
):
    with scratch_db.begin() as db:
        terminal(db, "11111111")
        one = inbox(db, "11111111")
        two = inbox(db, "11111111")
        attempt = InboxDeliveryAttemptModel(
            attempt_uuid="attempt-unresolved",
            receiver_terminal_id="11111111",
            provider="codex",
            outcome="unresolved",
            reason="continuity_uncertain",
            payload_hash="x",
            payload_length=1,
            evidence="{}",
            sender_id="99999999",
            orchestration_type="send_message",
            started_at=datetime.now(),
            last_at=datetime.now(),
            settled_at=datetime.now(),
        )
        db.add(attempt)
        db.add(
            InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt.attempt_uuid, message_id=two.id, position=0
            )
        )
    since = datetime.now() - timedelta(days=1)
    page1 = client.get("/messages", params={"to": "11111111", "limit": 1})
    assert page1.status_code == 200
    assert page1.json()["next_after_id"] == one.id
    assert page1.json()["has_more"] is True
    naive = client.get(
        "/messages",
        params={
            "to": "11111111",
            "after_id": one.id,
            "since": since.isoformat(),
        },
    )
    aware = client.get(
        "/messages",
        params={
            "to": "11111111",
            "after_id": one.id,
            "since": since.replace(tzinfo=timezone.utc).isoformat(),
        },
    )
    assert naive.status_code == aware.status_code == 200
    assert naive.json() == aware.json()
    assert naive.json()["items"][0]["id"] == two.id
    assert naive.json()["items"][0]["last_attempt_outcome"] == "unresolved"


def test_probe_07_mcp_http_twins_are_blocked_without_bearer_when_auth_enabled(
    client,
    monkeypatch,
):
    from cli_agent_orchestrator.mcp_server import server as mcp_server

    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.delenv("CAO_API_TOKEN", raising=False)
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")

    def requests_response(response, method: str, url: str):
        converted = http_requests.Response()
        converted.status_code = response.status_code
        converted._content = response.content
        converted.headers.update(response.headers)
        converted.url = url
        converted.request = http_requests.Request(method, url).prepare()
        return converted

    def get(url, **kwargs):
        response = client.get("/messages", params=kwargs.get("params"))
        return requests_response(response, "GET", url)

    def post(url, **kwargs):
        response = client.post("/messages/ack", json=kwargs.get("json"))
        return requests_response(response, "POST", url)

    monkeypatch.setattr(mcp_server.requests, "get", get)
    monkeypatch.setattr(mcp_server.requests, "post", post)
    listed = mcp_server._list_messages_impl("11111111")
    acked = mcp_server._ack_messages_impl(1)
    assert listed["detail"] and acked["detail"]
    assert "success" not in listed and "success" not in acked


def test_probe_07_scope_enforcement_is_401_and_403(client, monkeypatch):
    from cli_agent_orchestrator.security import auth

    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    requests = [
        ("get", "/messages?to=11111111"),
        ("get", "/terminals/11111111/inbox/messages"),
        ("post", "/messages/ack"),
        ("get", "/mailboxes"),
        ("delete", "/mailboxes/mb_aaaaaaaa"),
    ]
    for method, path in requests:
        response = (
            client.post(
                path,
                json={"terminal_id": "11111111", "up_to_id": 1},
            )
            if method == "post"
            else getattr(client, method)(path)
        )
        assert response.status_code == 401

    async def wrong_scope():
        return [auth.SCOPE_WRITE]

    app.dependency_overrides[auth.get_current_scopes] = wrong_scope
    try:
        assert client.get("/messages?to=11111111").status_code == 403
        assert client.get("/terminals/11111111/inbox/messages").status_code == 403
        assert client.get("/mailboxes").status_code == 403
        # write is insufficient for operator deletion.
        assert client.delete("/mailboxes/mb_aaaaaaaa").status_code == 403
    finally:
        app.dependency_overrides.pop(auth.get_current_scopes, None)

    async def read_only_scope():
        return [auth.SCOPE_READ]

    app.dependency_overrides[auth.get_current_scopes] = read_only_scope
    try:
        assert (
            client.post(
                "/messages/ack",
                json={
                    "terminal_id": "11111111",
                    "up_to_id": 1,
                },
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.pop(auth.get_current_scopes, None)


def test_probe_08_each_direct_writer_resolves_dead_incarnation_to_mailbox(scratch_db):
    with scratch_db.begin() as db:
        current = mailbox(db)
        current.current_terminal_id = "22222222"
        current.generation = 2
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_aaaaaaaa",
                generation=2,
                terminal_id="22222222",
                published_at=datetime.now(),
            )
        )
        db.add(
            TerminalModel(
                id="33333333",
                tmux_session="cao-worker",
                tmux_window="worker",
                provider="codex",
                agent_profile="code_worker",
                init_state="init_pending",
                init_started_at=datetime.now(timezone.utc),
                init_owner_epoch="11111111-1111-1111-1111-111111111111",
                init_deadline_s=30.0,
            )
        )
        orphan = inbox(db, "44444444", sender="11111111")
        wpm = inbox(db, "33333333", sender="11111111")
        attempt = InboxDeliveryAttemptModel(
            attempt_uuid="wave3b-wpm1",
            receiver_terminal_id="33333333",
            provider="codex",
            outcome="ambiguous",
            reason="confirmation_timeout",
            payload_hash="wpm",
            payload_length=1,
            evidence="{}",
            sender_id="11111111",
            orchestration_type="send_message",
            started_at=datetime.now(),
            last_at=datetime.now(),
            settled_at=datetime.now(),
        )
        db.add(attempt)
        db.add(
            InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt.attempt_uuid,
                message_id=wpm.id,
                position=0,
            )
        )
    direct = create_inbox_message("writer-1", "11111111", "writer-1-rollover")
    logical = create_logical_inbox_message(
        sender_id="writer-2", mailbox_id="mb_aaaaaaaa", message="writer-2-rollover"
    )
    assert (
        insert_identity_authority_notice("writer-10", "11111111", "writer-10-rollover").value
        == "inserted"
    )
    deferred = claim_deferred_init_failure(
        "33333333",
        caller_id="11111111",
        failure_token="22222222-2222-2222-2222-222222222222",
        notice="deferred-init-notice",
    )
    assert deferred["status"] == "claimed_notified"
    assert settle_pending_orphan_messages().settled_count == 1
    assert (
        record_wpm1_stalled_notice("wave3b-wpm1", [wpm.id], "33333333", "2030-01-01T00:00:00Z")
        == "recorded"
    )
    assert settle_wpm1_terminal_batch([wpm.id], MessageStatus.DELIVERED, "33333333") == "settled"
    with scratch_db() as db:
        notices = db.query(InboxModel).filter(InboxModel.id.notin_([orphan.id, wpm.id])).all()
        matched = [
            row
            for row in notices
            if (
                row.message == "deferred-init-notice"
                or row.message.startswith("p5-orphan")
                or row.message.startswith("wpm1-notice kind=stalled")
                or row.message.startswith("wpm1-notice kind=corrective")
            )
        ]
        assert len(matched) == 4
        assert {(row.receiver_id, row.logical_receiver_id) for row in matched} == {
            ("11111111", "mb_aaaaaaaa")
        }
        assert {row.enqueue_generation for row in matched} == {2}
        rollover_rows = (
            db.query(InboxModel)
            .filter(
                InboxModel.message.in_(
                    ["writer-1-rollover", "writer-2-rollover", "writer-10-rollover"]
                )
            )
            .all()
        )
        assert {row.message for row in rollover_rows} == {
            "writer-1-rollover",
            "writer-2-rollover",
            "writer-10-rollover",
        }
        assert {row.enqueue_generation for row in rollover_rows} == {2}
        assert direct.enqueue_generation == logical.enqueue_generation == 2


def test_probe_09_raw_addressed_output_bytes_match_parent_33aad1c(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    parent = tmp_path / "parent"
    parent.mkdir()
    archive = subprocess.check_output(
        [
            "git",
            "archive",
            "--format=tar",
            "33aad1c",
        ],
        cwd=repo,
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(parent, filter="data")
    script = """
import json, sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from cli_agent_orchestrator.clients import database
engine = create_engine('sqlite:///' + sys.argv[1])
database.Base.metadata.create_all(engine)
sessions = sessionmaker(bind=engine, expire_on_commit=False)
database.SessionLocal = sessions
with sessions.begin() as db:
    db.add(database.TerminalModel(id='11111111', tmux_session='cao-raw',
        tmux_window='raw', provider='codex', agent_profile='code_worker',
        init_state='ready'))
row = database.create_inbox_message('99999999', '11111111', 'raw body')
payload = {'id': row.id, 'sender_id': row.sender_id, 'receiver_id': row.receiver_id,
    'message': row.message, 'orchestration_type': row.orchestration_type.value,
    'status': row.status.value, 'failure_reason': row.failure_reason}
sys.stdout.buffer.write(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode())
"""

    def run(tree: Path, name: str) -> bytes:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(tree / "src")
        return subprocess.check_output(
            [sys.executable, "-c", script, str(tmp_path / f"{name}.sqlite")],
            cwd=tree,
            env=env,
        )

    parent_bytes = run(parent, "parent")
    target_bytes = run(repo, "target")
    assert target_bytes == parent_bytes
    assert json.loads(target_bytes)["receiver_id"] == "11111111"


def test_probe_10_mailbox_delete_settles_refuses_and_p5_straggler(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "99999999")
        mailbox(db)
        terminal(db, "11111111")
        pending = inbox(db, "11111111", logical="mb_aaaaaaaa")
    with pytest.raises(MailboxDomainError, match="mailbox_in_use"):
        delete_mailbox("mb_aaaaaaaa")
    with scratch_db.begin() as db:
        db.query(TerminalModel).filter_by(id="11111111").delete()
        db.get(InboxModel, pending.id).status = "delivering"
    with pytest.raises(MailboxDomainError, match="mailbox_busy"):
        delete_mailbox("mb_aaaaaaaa")
    with scratch_db.begin() as db:
        db.get(InboxModel, pending.id).status = "pending"
    result = delete_mailbox("mb_aaaaaaaa")
    assert result == {"settled_pending": 1, "notices_sent": 1}
    with scratch_db.begin() as db:
        settled = db.query(InboxModel).filter_by(id=pending.id).one()
        assert (settled.status, settled.failure_reason) == ("delivery_failed", "mailbox_deleted")
        straggler = inbox(db, "11111111", logical="mb_aaaaaaaa", sender="99999999")
    p5 = settle_pending_orphan_messages()
    assert p5.settled_count == 1 and p5.notification_count == 1
    with scratch_db() as db:
        recovered = db.get(InboxModel, straggler.id)
        assert (recovered.status, recovered.failure_reason) == (
            "delivery_failed",
            "mailbox_deleted",
        )
    with pytest.raises(MailboxDomainError, match="unknown_mailbox"):
        delete_mailbox("mb_aaaaaaaa")


def test_wpq1_writer5_mailbox_delete_notice_stamps_sender_rollover_generation(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "sender-current")
        db.add(
            MailboxModel(
                id="mb_sender",
                session_name="cao-sender",
                role="supervisor",
                current_terminal_id="sender-current",
                generation=4,
                consumed_through_id=0,
            )
        )
        db.add_all(
            [
                MailboxIncarnationModel(
                    mailbox_id="mb_sender", generation=3, terminal_id="sender-old"
                ),
                MailboxIncarnationModel(
                    mailbox_id="mb_sender", generation=4, terminal_id="sender-current"
                ),
            ]
        )
        db.add(
            MailboxModel(
                id="mb_target",
                session_name="cao-target",
                role="supervisor",
                current_terminal_id=None,
                generation=2,
                consumed_through_id=0,
            )
        )
        db.add(
            MailboxIncarnationModel(mailbox_id="mb_target", generation=2, terminal_id="target-old")
        )
        pending = InboxModel(
            sender_id="sender-old",
            receiver_id="target-old",
            logical_receiver_id="mb_target",
            enqueue_generation=2,
            message="writer-5-target",
            orchestration_type="send_message",
            status="pending",
        )
        db.add(pending)

    assert delete_mailbox("mb_target") == {"settled_pending": 1, "notices_sent": 1}
    with scratch_db() as db:
        notice = db.query(InboxModel).filter(InboxModel.message.like("mailbox-delete %")).one()
        assert notice.receiver_id == "sender-old"
        assert notice.logical_receiver_id == "mb_sender"
        assert notice.enqueue_generation == 4


def test_probe_11_incarnation_mapper_pk_and_global_uniqueness(scratch_db):
    assert [column.name for column in MailboxIncarnationModel.__table__.primary_key] == [
        "mailbox_id",
        "generation",
    ]
    with scratch_db.begin() as db:
        mailbox(db)
        db.add(
            MailboxModel(
                id="mb_bbbbbbbb",
                session_name="cao-other",
                role="supervisor",
                current_terminal_id="11111111",
                generation=1,
                consumed_through_id=0,
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_bbbbbbbb",
                generation=1,
                terminal_id="11111111",
                published_at=datetime.now(),
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()


def test_probe_12_superseded_and_misscoped_retries_conflict(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db)
    old_claim = claim_mailbox("cao-wave3b")
    publish_supervisor_incarnation(old_claim, "22222222")
    with pytest.raises(MailboxDomainError, match="mailbox_conflict"):
        publish_supervisor_incarnation(old_claim, "11111111")
    mis_scoped = mailbox_service.MailboxClaim("cao-other", "supervisor", "mb_aaaaaaaa", 1)
    with pytest.raises(MailboxDomainError, match="mailbox_conflict"):
        publish_supervisor_incarnation(mis_scoped, "22222222")


def test_probe_12_route_dead_mailbox_unknown_and_raw_paths(
    scratch_db,
    client,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "33333333")
    dead = client.post(
        "/terminals/mb_aaaaaaaa/inbox/messages",
        params={"sender_id": "99999999", "message": "queue while dead"},
    )
    unknown = client.post(
        "/terminals/mb_bbbbbbbb/inbox/messages",
        params={"sender_id": "99999999", "message": "unknown"},
    )
    backend = MagicMock()
    backend.session_exists.return_value = True
    with patch("cli_agent_orchestrator.api.main.get_backend", return_value=backend):
        raw = client.post(
            "/terminals/33333333/inbox/messages",
            params={"sender_id": "99999999", "message": "raw"},
        )
    assert dead.status_code == 200
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["code"] == "unknown_mailbox"
    assert raw.status_code == 200 and raw.json()["receiver_id"] == "33333333"
    with scratch_db() as db:
        logical = db.get(InboxModel, dead.json()["message_id"])
        raw_row = db.get(InboxModel, raw.json()["message_id"])
        assert (logical.status, logical.receiver_id, logical.logical_receiver_id) == (
            "pending",
            "11111111",
            "mb_aaaaaaaa",
        )
        assert raw_row.logical_receiver_id is None


def test_probe_12_publication_cleanup_failure_keeps_typed_original_cause(
    scratch_db,
    monkeypatch,
):
    terminal_result = Terminal(
        id="aaaaaaaa",
        name="supervisor",
        provider="codex",
        session_name="cao-cleanup",
        agent_profile="code_supervisor",
    )

    async def create_terminal_side_effect(**_kwargs):
        return terminal_result

    monkeypatch.setattr(
        session_service, "load_agent_profile", lambda _name: MagicMock(role="supervisor")
    )
    monkeypatch.setattr(session_service, "create_terminal", create_terminal_side_effect)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.seed_resume_bootstrap",
        lambda *_args, **_kwargs: None,
    )
    cause = MailboxDomainError("mailbox_conflict", "original conflict")
    monkeypatch.setattr(
        mailbox_service, "publish_supervisor_incarnation", MagicMock(side_effect=cause)
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.delete_terminal",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(database, "get_terminal_metadata", lambda _id: {"id": "aaaaaaaa"})
    with pytest.raises(PublicationCleanupFailed) as caught:
        asyncio.run(session_service.create_session("codex", "code_supervisor", "cao-cleanup"))
    assert caught.value.cause_code == "mailbox_conflict"
    assert caught.value.cause_message == "original conflict"


def test_probe_12_sender_lock_timeout_interrupts_and_requeues(scratch_db, monkeypatch):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        row = inbox(db, "11111111", logical="mb_aaaaaaaa")
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    lock.acquire()
    monkeypatch.setattr(mailbox_service, "MAILBOX_AUTHORITY_TIMEOUT_SECONDS", 0.01)
    try:
        assert deliver_with_real_attempt(monkeypatch, "11111111") == []
    finally:
        lock.release()
    trace = get_message_trace(row.id)
    assert trace["message"]["status"] == "pending"
    assert trace["attempts"][-1]["outcome"] == "interrupted"
    assert trace["attempts"][-1]["reason"] == "mailbox_authority_timeout"


def test_probe_12_attempt_open_racing_delete_serializes_behind_begin_immediate(
    scratch_db,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "99999999")
        row = inbox(db, "11111111", logical="mb_aaaaaaaa")
    candidate = get_pending_messages("11111111")
    proof = make_admission_proof("ordinary", [row.id])
    engine = scratch_db.kw["bind"]
    delete_inside = threading.Event()
    release_delete = threading.Event()
    mailbox_selects = 0

    def pause_second_mailbox_read(_conn, _cursor, statement, _params, _context, _many):
        nonlocal mailbox_selects
        if "FROM mailboxes" in statement:
            mailbox_selects += 1
            if mailbox_selects == 2:
                delete_inside.set()
                assert release_delete.wait(2)

    event.listen(engine, "before_cursor_execute", pause_second_mailbox_read)
    outcomes: dict[str, object] = {}
    try:
        delete_thread = threading.Thread(
            target=lambda: outcomes.setdefault("delete", delete_mailbox("mb_aaaaaaaa"))
        )
        delete_thread.start()
        assert delete_inside.wait(2)
        open_thread = threading.Thread(
            target=lambda: outcomes.setdefault(
                "open",
                begin_delivery_attempt_if_no_other_delivering(
                    candidate,
                    "11111111",
                    "codex",
                    "race",
                    1,
                    admission_proof=proof,
                ),
            )
        )
        open_thread.start()
        release_delete.set()
        delete_thread.join(2)
        open_thread.join(2)
    finally:
        event.remove(engine, "before_cursor_execute", pause_second_mailbox_read)
    assert not delete_thread.is_alive() and not open_thread.is_alive()
    assert outcomes["delete"] == {"settled_pending": 1, "notices_sent": 1}
    assert outcomes["open"].kind in {"stale_candidate", "busy_aborted"}


@pytest.mark.parametrize(
    "error,expected_status,expected_code",
    [
        (MailboxDomainError("mailbox_conflict", "conflict"), 409, "mailbox_conflict"),
        (
            MailboxDomainError("mailbox_authority_timeout", "timeout"),
            409,
            "mailbox_authority_timeout",
        ),
        (
            PublicationCleanupFailed(MailboxDomainError("mailbox_conflict", "cause")),
            500,
            "publication_cleanup_failed",
        ),
    ],
)
@pytest.mark.parametrize(
    "path,seam",
    [
        ("/sessions", "cli_agent_orchestrator.api.main.session_service.create_session"),
        ("/sessions/start", "cli_agent_orchestrator.api.main.session_service.start_session"),
    ],
)
def test_probe_13_http_projections_guard_delivery_and_cold_registry(
    client,
    monkeypatch,
    error,
    expected_status,
    expected_code,
    path,
    seam,
):
    assert get_mailbox_authority_lock("cold", "supervisor") is get_mailbox_authority_lock(
        "cold", "supervisor"
    )
    with patch(seam, side_effect=error):
        response = client.post(path, params={"agent_profile": "code_supervisor"})
    assert response.status_code == expected_status
    assert response.json()["detail"]["code"] == expected_code
    if expected_code == "publication_cleanup_failed":
        assert response.json()["detail"]["cause"]["code"] == "mailbox_conflict"


def test_probe_13_cold_registry_two_threads_receive_one_lock_object():
    barrier = threading.Barrier(2)
    locks: list[threading.Lock] = []

    def lookup():
        barrier.wait()
        locks.append(get_mailbox_authority_lock("brand-new", "supervisor"))

    callers = [threading.Thread(target=lookup) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(2)
    assert len(locks) == 2 and locks[0] is locks[1]


def test_probe_13_ready_base_guard_raw_and_mailbox_parity_with_refresh_override(
    scratch_db,
    client,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        db.add(
            ProviderSessionModel(
                name="protected-base",
                provider="codex",
                session_uuid="base-uuid",
                cwd="/tmp",
                agent_profile="code_supervisor",
                status="ready",
                kind="base",
                source_terminal_id="11111111",
                session_name="cao-wave3b",
            )
        )
    base_params = {"sender_id": "99999999", "message": "guarded"}
    blocked_raw = client.post("/terminals/11111111/inbox/messages", params=base_params)
    blocked_logical = client.post("/terminals/mb_aaaaaaaa/inbox/messages", params=base_params)
    assert blocked_raw.status_code == blocked_logical.status_code == 409
    backend = MagicMock()
    backend.session_exists.return_value = True
    with (
        patch("cli_agent_orchestrator.api.main.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"),
    ):
        allowed_raw = client.post(
            "/terminals/11111111/inbox/messages",
            params={**base_params, "refresh_ingest": True},
        )
        allowed_logical = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages",
            params={**base_params, "refresh_ingest": True},
        )
    assert allowed_raw.status_code == allowed_logical.status_code == 200


def test_probe_13_logical_insert_immediately_pastes_to_resolved_live_incarnation(
    scratch_db,
    client,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
    observation = BoundaryObservation("wave3b-epoch", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
    provider = MagicMock()
    provider.read_composer_draft_state.return_value = "empty"
    pasted: list[str] = []

    def paste(target, _wire, **kwargs):
        pasted.append(target)
        kwargs["on_submitted"](observation)
        return observation

    resolution = TranscriptResolution(
        Path("/trace"), "binding", TranscriptLiveReference(Path("/trace"), 1, 0)
    )
    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=resolution,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _target, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=paste,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "test-confirmation"}),
        ),
        patch.object(InboxService, "_commit_watchdog_ops"),
    ):
        monitor.get_boundary_observation.return_value = observation
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            TerminalStatus.IDLE,
            {"result_status": "idle", "law_signal": {"class": "chrome"}},
        )
        response = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages",
            params={"sender_id": "99999999", "message": "inline"},
        )
    assert response.status_code == 200
    assert response.json()["receiver_id"] == "11111111"
    assert pasted == ["11111111"]
    with scratch_db() as db:
        assert db.get(InboxModel, response.json()["message_id"]).status == "delivered"


@pytest.mark.parametrize(
    "code,status,cause",
    [
        ("mailbox_conflict", 409, None),
        ("mailbox_authority_timeout", 409, None),
        ("publication_cleanup_failed", 500, {"code": "mailbox_conflict", "message": "conflict"}),
    ],
)
def test_probe_14_both_session_start_cli_clients_decode_typed_errors(
    monkeypatch,
    code,
    status,
    cause,
):
    response = Mock(status_code=status)
    detail = {"code": code, "message": "typed failure"}
    if cause:
        detail["cause"] = cause
    response.json.return_value = {"detail": detail}
    response.raise_for_status.side_effect = RuntimeError("must not flatten")
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: response)
    runner = CliRunner()
    canonical = runner.invoke(cli, ["session", "start", "demo", "--agents", "code_supervisor"])
    deprecated = runner.invoke(
        cli,
        [
            "launch",
            "--agents",
            "code_supervisor",
            "--session-name",
            "demo",
            "--headless",
            "--auto-approve",
        ],
    )
    for result in (canonical, deprecated):
        assert result.exit_code == 1
        assert code in result.output
        if cause:
            assert "cause=mailbox_conflict" in result.output


def test_probe_14_seed_failure_exit_two_is_retained_for_both_clients(monkeypatch):
    response = Mock(status_code=422)
    response.json.return_value = {
        "bootstrap": {"status": "seed_failed", "error_code": "seed_timeout"}
    }
    monkeypatch.setattr("requests.post", lambda *args, **kwargs: response)
    runner = CliRunner()
    results = [
        runner.invoke(cli, ["session", "start", "demo", "--agents", "code_supervisor"]),
        runner.invoke(
            cli,
            [
                "launch",
                "--agents",
                "code_supervisor",
                "--session-name",
                "demo",
                "--headless",
                "--auto-approve",
            ],
        ),
    ]
    assert all(result.exit_code == 2 for result in results)
    assert all("seed_timeout" in result.output for result in results)


def test_probe_14_publication_cannot_enter_resolution_to_insert_window(
    scratch_db,
    client,
    monkeypatch,
):
    with scratch_db.begin() as db:
        mailbox(db)
        terminal(db, "11111111")
        terminal(db, "22222222")
        db.add(
            ProviderSessionModel(
                name="successor-base",
                provider="codex",
                session_uuid="successor-uuid",
                cwd="/tmp",
                agent_profile="code_supervisor",
                status="ready",
                kind="base",
                source_terminal_id="22222222",
                session_name="cao-wave3b",
            )
        )
    claim = claim_mailbox("cao-wave3b")
    original_resolve = mailbox_service.resolve_inbox_receiver
    resolution_entered = threading.Event()
    allow_insert = threading.Event()
    publication_done = threading.Event()
    resolved: list[tuple[str, str | None, int | None]] = []
    responses: dict[str, object] = {}

    def resolve_and_pause(db, receiver_id):
        value = original_resolve(db, receiver_id)
        resolved.append(value)
        resolution_entered.set()
        assert allow_insert.wait(2)
        return value

    def send():
        responses["send"] = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages",
            params={"sender_id": "99999999", "message": "window"},
        )

    def publish():
        responses["publish"] = publish_supervisor_incarnation(claim, "22222222")
        publication_done.set()

    monkeypatch.setattr(mailbox_service, "resolve_inbox_receiver", resolve_and_pause)
    with patch("cli_agent_orchestrator.api.main.inbox_service.deliver_pending"):
        sender = threading.Thread(target=send)
        sender.start()
        assert resolution_entered.wait(2)
        publisher = threading.Thread(target=publish)
        publisher.start()
        assert not publication_done.wait(0.05)
        allow_insert.set()
        sender.join(2)
        publisher.join(2)
    assert not sender.is_alive() and not publisher.is_alive()
    assert resolved == [("11111111", "mb_aaaaaaaa", 1)]
    assert responses["send"].status_code == 200
    assert responses["publish"]["generation"] == 2
    with scratch_db() as db:
        stored = db.query(InboxModel).filter_by(message="window").one()
        assert stored.receiver_id == "11111111"
        assert stored.logical_receiver_id == "mb_aaaaaaaa"
        assert stored.enqueue_generation == 1
        assert stored.status == "digested"
        assert stored.digested_into == responses["publish"]["digest_message_id"]


def test_probe_15_send_timeout_is_409_no_insert_and_mcp_structured(
    scratch_db,
    monkeypatch,
    client,
):
    with scratch_db.begin() as db:
        mailbox(db)
    lock = get_mailbox_authority_lock("cao-wave3b", "supervisor")
    lock.acquire()
    monkeypatch.setattr(mailbox_service, "MAILBOX_AUTHORITY_TIMEOUT_SECONDS", 0.01)
    try:
        response = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages",
            params={"sender_id": "99999999", "message": "blocked"},
        )
    finally:
        lock.release()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "mailbox_authority_timeout"
    from cli_agent_orchestrator.mcp_server import server as mcp_server

    monkeypatch.setenv("CAO_TERMINAL_ID", "99999999")

    def post(url, **kwargs):
        api_response = client.post(
            "/terminals/mb_aaaaaaaa/inbox/messages", params=kwargs.get("params")
        )
        converted = http_requests.Response()
        converted.status_code = api_response.status_code
        converted._content = api_response.content
        converted.headers.update(api_response.headers)
        converted.url = url
        converted.request = http_requests.Request("POST", url).prepare()
        return converted

    monkeypatch.setattr(mcp_server.requests, "post", post)
    lock.acquire()
    try:
        mcp_result = mcp_server._send_message_impl("mb_aaaaaaaa", "blocked through MCP")
    finally:
        lock.release()
    assert mcp_result == {
        "success": False,
        "error": {
            "code": "mailbox_authority_timeout",
            "message": "mailbox authority lock timed out",
        },
    }
    with scratch_db() as db:
        assert db.query(InboxModel).count() == 0


def test_wpq1_drain_routes_only_stale_generation_to_digest(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222", generation=2)
        terminal(db, "22222222")
        stale = inbox(db, "22222222", logical="mb_aaaaaaaa")
        stale.enqueue_generation = 1
        current = inbox(db, "22222222", logical="mb_aaaaaaaa")
        current.enqueue_generation = 2

    assert digest_stale_pending_for_terminal("22222222") == 1

    with scratch_db() as db:
        stale_row = db.get(InboxModel, stale.id)
        current_row = db.get(InboxModel, current.id)
        digest = db.get(InboxModel, stale_row.digested_into)
        assert stale_row.status == "digested"
        assert current_row.status == "pending"
        assert current_row.digested_into is None
        assert digest.orchestration_type == "mailbox_digest"
        assert digest.enqueue_generation == 2
        assert f"message {stale.id}" in digest.message


def test_wpq1_digest_summary_strips_c1_and_format_controls_with_caps(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222", generation=2)
        terminal(db, "22222222")
        stale = inbox(db, "22222222", logical="mb_aaaaaaaa")
        stale.enqueue_generation = 1
        stale.message = "left\u009fright\u200b " + ("é" * 2000)

    assert digest_stale_pending_for_terminal("22222222") == 1

    with scratch_db() as db:
        digest = db.get(InboxModel, db.get(InboxModel, stale.id).digested_into)
        summary = digest.message.splitlines()[1]
        assert "\u009f" not in summary
        assert "\u200b" not in summary
        assert "leftright" in summary
        assert len(summary.encode("utf-8")) <= 120
        assert len(digest.message.encode("utf-8")) <= 2000


def test_wpq5_i_compact_digest_selects_confirmed_and_inferred_deliveries_with_120b_lines(
    scratch_db,
):
    now = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222", generation=2)
        terminal(db, "22222222")
        confirmed = inbox(db, "22222222", status="delivered", logical="mb_aaaaaaaa")
        confirmed.message = "confirmed\u009f " + ("é" * 200)
        inferred = inbox(db, "22222222", status="delivered", logical="mb_aaaaaaaa")
        inferred.message = "inferred callback"
        db.flush()
        attempt = InboxDeliveryAttemptModel(
            attempt_uuid="compact-confirmed",
            receiver_terminal_id="22222222",
            provider="codex",
            started_at=now - timedelta(minutes=4),
            settled_at=now - timedelta(minutes=3),
            outcome="confirmed",
            payload_hash="h",
            payload_length=1,
            sender_id=confirmed.sender_id,
            orchestration_type=confirmed.orchestration_type,
        )
        db.add(attempt)
        db.add(
            InboxDeliveryAttemptMemberModel(
                attempt_uuid=attempt.attempt_uuid,
                message_id=confirmed.id,
                position=0,
            )
        )
        db.add(
            InboxMessageTraceEventModel(
                message_id=inferred.id,
                kind="inferred_delivered",
                payload={"reply_message_id": 9},
                created_at=now - timedelta(minutes=2),
            )
        )

    notice_id = publish_compact_boundary_digest("22222222", window_min=15, now_utc=now)
    assert notice_id is not None
    with scratch_db() as db:
        notice = db.get(InboxModel, notice_id)
        assert notice.sender_id == "compact-digest"
        assert "cao messages list --to 22222222 --since" in notice.message
        assert "MCP `list_messages`" in notice.message
        summaries = notice.message.splitlines()[2:]
        assert len(summaries) == 2
        assert all(len(line.encode("utf-8")) <= 120 for line in summaries)
        assert any(len(line.encode("utf-8")) > 80 for line in summaries)
        assert "\u009f" not in notice.message


def test_wpq5_j_compact_retry_ten_seconds_later_is_fenced(scratch_db):
    now = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222")
        terminal(db, "22222222")
        row = inbox(db, "22222222", status="delivered", logical="mb_aaaaaaaa")
        db.flush()
        db.add(
            InboxMessageTraceEventModel(
                message_id=row.id,
                kind="inferred_delivered",
                payload={"reply_message_id": 1},
                created_at=now - timedelta(minutes=1),
            )
        )

    first = publish_compact_boundary_digest("22222222", window_min=15, now_utc=now)
    retry = publish_compact_boundary_digest(
        "22222222", window_min=15, now_utc=now + timedelta(seconds=10)
    )
    assert first is not None and retry is None
    with scratch_db() as db:
        assert db.query(InboxModel).filter_by(sender_id="compact-digest").count() == 1


def test_wpq5_k_real_compact_after_fence_window_publishes_new_digest(scratch_db):
    now = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222")
        terminal(db, "22222222")
        row = inbox(db, "22222222", status="delivered", logical="mb_aaaaaaaa")
        db.flush()
        db.add(
            InboxMessageTraceEventModel(
                message_id=row.id,
                kind="inferred_delivered",
                payload={"reply_message_id": 1},
                created_at=now - timedelta(minutes=1),
            )
        )

    first = publish_compact_boundary_digest("22222222", window_min=15, now_utc=now)
    second = publish_compact_boundary_digest(
        "22222222", window_min=15, now_utc=now + timedelta(minutes=6)
    )
    assert first is not None and second is not None and second != first


def test_wpq5_l_empty_zero_window_and_non_mailbox_terminal_publish_nothing(scratch_db):
    now = datetime(2026, 7, 17, 16, 0, tzinfo=timezone.utc)
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222")
        terminal(db, "22222222")
        terminal(db, "33333333")

    assert publish_compact_boundary_digest("22222222", window_min=15, now_utc=now) is None
    assert publish_compact_boundary_digest("22222222", window_min=0, now_utc=now) is None
    assert publish_compact_boundary_digest("33333333", window_min=15, now_utc=now) is None


def test_wpq1_superseded_digest_chain_uses_direct_structural_counts(scratch_db):
    with scratch_db.begin() as db:
        mailbox(db, terminal_id="22222222", generation=2)
        terminal(db, "22222222")
        digest_a = inbox(
            db,
            "22222222",
            logical="mb_aaaaaaaa",
            kind="mailbox_digest",
        )
        digest_a.enqueue_generation = 1
        direct = inbox(db, "22222222", logical="mb_aaaaaaaa")
        direct.enqueue_generation = 1

    assert digest_stale_pending_for_terminal("22222222") == 2
    with scratch_db() as db:
        digest_b_id = db.get(InboxModel, digest_a.id).digested_into
        assert db.get(InboxModel, direct.id).digested_into == digest_b_id

    with scratch_db.begin() as db:
        db.get(MailboxModel, "mb_aaaaaaaa").generation = 3

    assert digest_stale_pending_for_terminal("22222222") == 1
    with scratch_db() as db:
        digest_a_row = db.get(InboxModel, digest_a.id)
        digest_b = db.get(InboxModel, digest_b_id)
        digest_c = db.get(InboxModel, digest_b.digested_into)
        assert digest_a_row.digested_into == digest_b.id
        assert digest_b.status == "digested"
        assert digest_c.enqueue_generation == 3
        assert (
            f"superseded digest {digest_b.id} (gen 2, 1 items, ids {direct.id}-{direct.id})"
            in digest_c.message
        )
        assert f"superseded digest {digest_a.id}" not in digest_c.message


def test_wpq1_purge_uses_shared_p5_transaction_and_notice(scratch_db):
    with scratch_db.begin() as db:
        terminal(db, "sender")
        terminal(db, "stale")
        row = inbox(db, "stale", sender="sender")
    backend = MagicMock()
    backend.get_history.side_effect = lambda _session, window, **_kwargs: (
        "alive" if window == "sender" else (_ for _ in ()).throw(RuntimeError("gone"))
    )

    with patch.object(terminal_service_module, "get_backend", return_value=backend):
        assert terminal_service_module.purge_stale_terminal_records() == 1

    with scratch_db() as db:
        assert db.get(InboxModel, row.id).status == "delivery_failed"
        notices = db.query(InboxModel).filter_by(receiver_id="sender").all()
        assert len(notices) == 1
        assert notices[0].message.startswith("p5-orphan receiver=stale")

"""WPQ7 callback barrier acceptance and mutation-killing controls."""

from __future__ import annotations

import ast
import inspect
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _fire_open_barrier_in_db,
    _maybe_fire_completed_barrier,
    callback_barrier_dispatch_allowed,
    callback_barrier_status,
    cancel_callback_barrier,
    create_inbox_message,
    delete_terminal_and_warm_intent,
    fire_due_barriers,
    get_callback_status_since,
    get_pending_messages,
    insert_barrier_escalation_message,
    settle_terminal_rebound,
    transition_pending_to_delivery_failed,
)
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import callback_barrier_service
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services import mailbox_service as mailbox_module
from cli_agent_orchestrator.services import stalled_callback_watchdog as watchdog_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import create_logical_inbox_message
from cli_agent_orchestrator.services.stalled_callback_watchdog import StalledCallbackWatchdog
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


def _barrier_sessions(tmp_path, monkeypatch, *, autoflush: bool, filename: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(autocommit=False, autoflush=autoflush, bind=engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.cleanup_service.SessionLocal", sessions)
    return sessions


@pytest.fixture
def barrier_db(tmp_path, monkeypatch):
    return _barrier_sessions(tmp_path, monkeypatch, autoflush=False, filename="wpq7.db")


@pytest.fixture(params=[True, False], ids=["autoflush-true", "autoflush-false"])
def completion_barrier_db(tmp_path, monkeypatch, request):
    return _barrier_sessions(
        tmp_path,
        monkeypatch,
        autoflush=request.param,
        filename=f"wpq7-completion-{request.param}.db",
    )


def _terminal(db, terminal_id: str, *, caller: str | None = None, profile: str = "reviewer"):
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="cao-wpq7",
            tmux_window=terminal_id,
            provider="codex",
            agent_profile=profile,
            caller_id=caller,
            lifecycle_generation=1,
        )
    )


def _seed_raw(sessions, workers=("worker-a", "worker-b")):
    with sessions.begin() as db:
        _terminal(db, "owner", profile="supervisor")
        for worker in workers:
            _terminal(db, worker, caller="owner")


def _dispatch_pair(label: str = "gate"):
    first = create_inbox_message("owner", "worker-a", "task a", dispatch_barrier={"label": label})
    second = create_inbox_message("owner", "worker-b", "task b", dispatch_barrier={"label": label})
    return first, second


def _seed_mailbox_owner(sessions):
    with sessions.begin() as db:
        db.add(
            MailboxModel(
                id="mb_aaaaaaaa",
                session_name="cao-wpq7",
                role="supervisor",
                current_terminal_id="owner",
                generation=1,
                consumed_through_id=0,
            )
        )
        db.add(
            MailboxIncarnationModel(
                mailbox_id="mb_aaaaaaaa",
                generation=1,
                terminal_id="owner",
                published_at=datetime.now(),
            )
        )


def _install_f92_watchdog(monkeypatch, *, caller_mailbox_id: str | None = None):
    service = StalledCallbackWatchdog(grace_seconds=3)
    monkeypatch.setattr(watchdog_module, "stalled_callback_watchdog", service)
    monkeypatch.setattr(
        watchdog_module,
        "get_terminal_metadata",
        lambda terminal_id: {
            "id": terminal_id,
            "caller_id": "owner",
            "caller_mailbox_id": caller_mailbox_id,
            "provider": "grok_cli",
            "tmux_session": "cao-wpq7",
            "tmux_window": terminal_id,
        },
    )
    return service


def _orphan_owner(sessions, owner_id: str = "owner"):
    """Delete the owner terminal, leaving its OPEN barrier behind.

    Reproduces the LIVE shape from the 2026-07-25 crash-loop rather than an
    invented one: the barrier row, its members and its held callbacks all
    survive; only the owner terminal row is gone. Nothing in the public API can
    produce this state, which is precisely why no existing test reached it —
    the orphan is made by the DELETE path, not by the barrier path.

    Raw evidence:
    probes/error-pane-samples/2026-07-25-cao-server-crashloop-barrier-owner-gone.txt
    """
    with sessions.begin() as db:
        deleted = db.query(TerminalModel).filter_by(id=owner_id).delete()
    assert deleted == 1, "fixture must actually orphan the barrier"


@pytest.fixture
def orphaned_barrier_db(barrier_db):
    """A DB holding exactly the row shape that crash-looped cao-server 634 times.

    OPEN barrier, overdue timeout, every member ARRIVED, callback still held,
    owner terminal deleted. Any sweep-side change should be run against this.

    Note the members are stamped ARRIVED *raw*. Arriving through the public API
    would fire the barrier COMPLETE on the last arrival — yet the live barrier
    had all three members ARRIVED and was still OPEN. That combination is the
    separate never-fires defect (F71, GOLDEN-TIPS 2026-07-25); it is what left
    an owner-gone barrier sitting in the sweep set for hours. The fixture
    reproduces the state as found, not the state the API can reach.
    """
    _seed_raw(barrier_db)
    _dispatch_pair("drill-r2-brainstorm")
    create_inbox_message("worker-a", "owner", "answer a")
    with barrier_db.begin() as db:
        # Stamp the remaining member ARRIVED without firing, as F71 left it.
        for member in db.query(CallbackBarrierMemberModel).all():
            member.state = "ARRIVED"
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "OPEN", "precondition: barrier must still be open"
        assert barrier.combined_message_id is None
        members = db.query(CallbackBarrierMemberModel).all()
        assert [m.state for m in members] == ["ARRIVED", "ARRIVED"]
        assert db.query(InboxModel).filter_by(status=MessageStatus.HELD.value).count() == 1
    _orphan_owner(barrier_db)
    return barrier_db


@pytest.mark.parametrize("member_count", [1, 3])
@pytest.mark.parametrize("mailbox_owner", [False, True])
def test_last_arrival_fires_immediately_in_both_autoflush_modes(
    completion_barrier_db, member_count, mailbox_owner
):
    workers = tuple(f"worker-{index}" for index in range(member_count))
    _seed_raw(completion_barrier_db, workers=workers)
    if mailbox_owner:
        _seed_mailbox_owner(completion_barrier_db)

    for worker in workers:
        create_inbox_message(
            "owner",
            worker,
            f"task for {worker}",
            dispatch_barrier={"label": "live-shape"},
        )
    for worker in workers[:-1]:
        create_inbox_message(worker, "owner", f"answer from {worker}")

    with completion_barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "OPEN"
        assert barrier.combined_message_id is None

    final_worker = workers[-1]
    create_inbox_message(final_worker, "owner", f"answer from {final_worker}")

    with completion_barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "FIRED_COMPLETE"
        combined_rows = db.query(InboxModel).filter_by(sender_id=f"barrier:{barrier.id}").all()
        assert len(combined_rows) == 1
        assert barrier.combined_message_id == combined_rows[0].id
        assert combined_rows[0].message.startswith(
            f"[callback barrier COMPLETE] live-shape — {member_count}/{member_count} in "
        )


def test_two_member_happy_path_holds_then_fires_one_combined(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair()
    first = create_inbox_message("worker-a", "owner", "answer a")
    assert first.status == MessageStatus.HELD
    second = create_inbox_message("worker-b", "owner", "answer b")
    assert second.status == MessageStatus.DIGESTED

    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "FIRED_COMPLETE"
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert combined.status == MessageStatus.PENDING.value
        assert combined.sender_id == f"barrier:{barrier.id}"
        assert combined.message.startswith("[callback barrier COMPLETE] gate — 2/2 in ")
        assert "answer a" in combined.message and "answer b" in combined.message
        source = db.query(InboxModel).filter_by(barrier_id=barrier.id).all()
        assert {row.status for row in source} == {MessageStatus.DIGESTED.value}
        assert (
            db.query(InboxDeliveryAttemptMemberModel)
            .filter(InboxDeliveryAttemptMemberModel.message_id.in_([row.id for row in source]))
            .count()
            == 0
        )
        assert (
            db.query(InboxMessageTraceEventModel)
            .filter(InboxMessageTraceEventModel.message_id.in_([row.id for row in source]))
            .count()
            == 0
        )


def test_held_is_durable_callback_proof_and_not_delivery_pending(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair()
    before = datetime.now() - timedelta(seconds=1)
    held = create_inbox_message("worker-a", "owner", "answer")
    assert held.status == MessageStatus.HELD
    assert get_callback_status_since("worker-a", "owner", before) == MessageStatus.HELD
    assert held.id not in {row.id for row in get_pending_messages("owner")}


@pytest.mark.parametrize("wrapper", ["raw-terminal", "logical-mailbox"])
def test_f92_one_member_barrier_records_callback_through_creation_wrapper(
    barrier_db, monkeypatch, wrapper
):
    _seed_raw(barrier_db, workers=("worker-a",))
    caller_mailbox_id = None
    callback_receiver = "owner"
    if wrapper == "logical-mailbox":
        _seed_mailbox_owner(barrier_db)
        caller_mailbox_id = "mb_aaaaaaaa"
        callback_receiver = caller_mailbox_id

    watchdog = _install_f92_watchdog(
        monkeypatch,
        caller_mailbox_id=caller_mailbox_id,
    )
    watchdog.record_inbound_task("worker-a", "owner", "developer")
    first_episode = watchdog._episodes["worker-a"]
    create_inbox_message(
        "owner",
        "worker-a",
        "task",
        dispatch_barrier={"label": f"f92-{wrapper}"},
    )

    if wrapper == "raw-terminal":
        callback = create_inbox_message("worker-a", callback_receiver, "answer")
    else:
        callback = create_logical_inbox_message(
            sender_id="worker-a",
            mailbox_id=callback_receiver,
            message="answer",
        )

    assert callback.status == MessageStatus.DIGESTED
    assert callback.barrier_id is not None
    assert callback.barrier_member_key is not None
    assert first_episode.callback_seen
    with barrier_db() as db:
        assert db.query(CallbackBarrierModel).one().state == "FIRED_COMPLETE"
        assert db.query(CallbackBarrierMemberModel).one().state == "ARRIVED"

    watchdog.record_status("worker-a", TerminalStatus.IDLE, now=10.0)
    first_episode.last_screen_fp = "sample"
    assert watchdog.collect_due_notifications(now=13.0) == []


@pytest.mark.parametrize("wrapper", ["raw-terminal", "logical-mailbox"])
def test_f92_creation_wrapper_ignores_row_without_complete_barrier_membership(
    barrier_db, monkeypatch, wrapper
):
    _seed_raw(barrier_db, workers=("worker-a",))
    callback_receiver = "owner"
    insert_module = dbmod
    create_callback = lambda: create_inbox_message("worker-a", callback_receiver, "half-populated")
    if wrapper == "logical-mailbox":
        _seed_mailbox_owner(barrier_db)
        callback_receiver = "mb_aaaaaaaa"
        insert_module = mailbox_module
        create_callback = lambda: create_logical_inbox_message(
            sender_id="worker-a",
            mailbox_id=callback_receiver,
            message="half-populated",
        )

    watchdog = _install_f92_watchdog(monkeypatch)
    recorder = MagicMock()
    monkeypatch.setattr(watchdog, "record_callback_if_to_caller", recorder)
    original_insert = insert_module._insert_routed_inbox_row

    def insert_half_populated_membership(db, *args, **kwargs):
        row = original_insert(db, *args, **kwargs)
        row.barrier_id = 8675309
        row.barrier_member_key = None
        return row

    monkeypatch.setattr(
        insert_module,
        "_insert_routed_inbox_row",
        insert_half_populated_membership,
    )

    callback = create_callback()

    assert callback.barrier_id == 8675309
    assert callback.barrier_member_key is None
    recorder.assert_not_called()
    with barrier_db() as db:
        committed = db.query(InboxModel).filter_by(id=callback.id).one()
        assert committed.barrier_id == 8675309
        assert committed.barrier_member_key is None


def test_f92_intermediate_mailbox_callback_records_before_barrier_fires(barrier_db, monkeypatch):
    _seed_raw(barrier_db)
    _seed_mailbox_owner(barrier_db)
    watchdog = _install_f92_watchdog(monkeypatch, caller_mailbox_id="mb_aaaaaaaa")
    watchdog.record_inbound_task("worker-a", "owner", "developer")
    episode = watchdog._episodes["worker-a"]
    _dispatch_pair("f92-intermediate")

    callback = create_logical_inbox_message(
        sender_id="worker-a",
        mailbox_id="mb_aaaaaaaa",
        message="answer",
    )

    assert callback.status == MessageStatus.HELD
    assert episode.callback_seen
    with barrier_db() as db:
        assert db.query(CallbackBarrierModel).one().state == "OPEN"
        states = {
            member.terminal_id: member.state
            for member in db.query(CallbackBarrierMemberModel).all()
        }
    assert states == {"worker-a": "ARRIVED", "worker-b": "AWAITING"}


def test_f92_new_task_after_one_member_callback_starts_new_generation(barrier_db, monkeypatch):
    _seed_raw(barrier_db, workers=("worker-a",))
    watchdog = _install_f92_watchdog(monkeypatch)
    watchdog.record_inbound_task("worker-a", "owner", "developer")
    first_episode = watchdog._episodes["worker-a"]
    create_inbox_message(
        "owner",
        "worker-a",
        "task one",
        dispatch_barrier={"label": "f92-generation"},
    )
    callback = create_inbox_message("worker-a", "owner", "answer one")
    assert callback.status == MessageStatus.DIGESTED

    # This arrives before any watchdog poll. Without the post-commit recorder,
    # record_inbound_task joins the unanswered prior episode instead.
    watchdog.record_inbound_task("worker-a", "owner", "developer")

    replacement = watchdog._episodes["worker-a"]
    assert replacement is not first_episode
    assert replacement.generation == first_episode.generation + 1


def test_f92_digested_callback_durably_suppresses_when_recorder_missed(barrier_db, monkeypatch):
    _seed_raw(barrier_db, workers=("worker-a",))
    watchdog = _install_f92_watchdog(monkeypatch)
    watchdog.record_inbound_task("worker-a", "owner", "developer")
    recorder = MagicMock()
    monkeypatch.setattr(watchdog, "record_callback_if_to_caller", recorder)
    create_inbox_message(
        "owner",
        "worker-a",
        "task",
        dispatch_barrier={"label": "f92-durable"},
    )

    callback = create_inbox_message("worker-a", "owner", "answer")

    assert callback.status == MessageStatus.DIGESTED
    recorder.assert_called_once_with("worker-a", "owner")
    episode = watchdog._episodes["worker-a"]
    assert not episode.callback_seen
    watchdog.record_status("worker-a", TerminalStatus.IDLE, now=10.0)
    episode.last_screen_fp = "sample"
    assert watchdog.collect_due_notifications(now=13.0) == []
    assert episode.callback_seen
    assert not episode.fired


def test_f92_combined_partial_barrier_row_does_not_clear_missing_worker(barrier_db, monkeypatch):
    _seed_raw(barrier_db)
    watchdog = _install_f92_watchdog(monkeypatch)
    watchdog.record_inbound_task("worker-b", "owner", "developer")
    _dispatch_pair("f92-partial")
    create_inbox_message("worker-a", "owner", "answer")
    fired = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(fired) == 1
    with barrier_db() as db:
        combined = db.query(InboxModel).filter_by(id=fired[0]).one()
        assert combined.sender_id.startswith("barrier:")
        assert "1/2" in combined.message

    episode = watchdog._episodes["worker-b"]
    watchdog.record_status("worker-b", TerminalStatus.IDLE, now=10.0)
    episode.last_screen_fp = "sample"
    monkeypatch.setattr(
        watchdog,
        "_fresh_frame_decides_running",
        lambda _terminal_id: (False, None),
    )

    notices = watchdog.collect_due_notifications(now=13.0)

    assert len(notices) == 1
    assert notices[0].terminal_id == "worker-b"
    assert episode.fired
    assert not episode.callback_seen


def test_terminal_settlement_selector_never_admits_held(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("selector")
    held = create_inbox_message("worker-a", "owner", "answer")
    assert transition_pending_to_delivery_failed([held.id]) is False
    with barrier_db() as db:
        assert db.query(InboxModel).filter_by(id=held.id).one().status == MessageStatus.HELD.value


@pytest.mark.parametrize(
    ("states", "expected_header", "expected_count"),
    [
        (("ARRIVED", "ARRIVED"), "COMPLETE", "2/2"),
        (("ARRIVED", "GONE"), "PARTIAL", "1/2"),
        (("GONE", "GONE"), "PARTIAL", "0/2"),
        (("ARRIVED", "FAILED"), "PARTIAL", "1/2"),
        (("ARRIVED", "AWAITING"), "PARTIAL", "1/2"),
    ],
)
def test_completion_render_matrix(barrier_db, states, expected_header, expected_count):
    _seed_raw(barrier_db)
    _dispatch_pair("matrix")
    if states[0] == "ARRIVED":
        create_inbox_message("worker-a", "owner", "answer a")
    with barrier_db.begin() as db:
        members = (
            db.query(CallbackBarrierMemberModel).order_by(CallbackBarrierMemberModel.position).all()
        )
        for member, state in zip(members, states):
            member.state = state
            if state == "FAILED":
                member.failure_class = "quota_or_auth"
    fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert combined.message.startswith(
            f"[callback barrier {expected_header}] matrix — {expected_count} in "
        )
        if states[0] == "ARRIVED":
            assert "answer a" in combined.message


def test_timeout_zero_arrivals_and_cancel_release_are_lossless(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("timeout")
    fired = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(fired) == 1
    with barrier_db() as db:
        combined = db.query(InboxModel).filter_by(id=fired[0]).one()
        assert "0/2" in combined.message
        # FAM-3: members are now terminalized as FAILED before render
        assert combined.message.count("[FAILED: barrier_closed_timeout]") == 2

    _dispatch_pair("cancel")
    held = create_inbox_message("worker-a", "owner", "held before cancel")
    assert held.status == MessageStatus.HELD
    result = cancel_callback_barrier(barrier_label="cancel", owner_id="owner")
    assert result["released"] == 1
    create_inbox_message("worker-b", "owner", "late after cancel")
    with barrier_db() as db:
        assert (
            db.query(InboxModel).filter_by(id=held.id).one().status == MessageStatus.PENDING.value
        )
        late = (
            db.query(InboxModel)
            .filter_by(sender_id="worker-b")
            .order_by(InboxModel.id.desc())
            .first()
        )
        assert late.status == MessageStatus.PENDING.value
        assert late.message.startswith("[late callback after barrier cancel]")


def test_duplicate_callback_appends_but_counts_member_once(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("dup")
    create_inbox_message("worker-a", "owner", "first")
    create_inbox_message("worker-a", "owner", "second")
    create_inbox_message("worker-b", "owner", "peer")
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        members = db.query(CallbackBarrierMemberModel).filter_by(barrier_id=barrier.id).all()
        assert sum(member.state == "ARRIVED" for member in members) == 2
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert "first" in combined.message and "second" in combined.message


def test_arrival_timeout_race_has_exactly_one_fire_winner(barrier_db):
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message("owner", "worker-a", "task", dispatch_barrier={"label": "race"})
    with barrier_db.begin() as db:
        db.query(CallbackBarrierModel).update(
            {CallbackBarrierModel.timeout_at: datetime.now() - timedelta(seconds=1)}
        )
    start = threading.Barrier(2)
    errors = []

    def arrive():
        try:
            start.wait()
            create_inbox_message("worker-a", "owner", "answer")
        except Exception as exc:  # pragma: no cover - assertion reports the race
            errors.append(exc)

    def timeout():
        try:
            start.wait()
            fire_due_barriers(datetime.now(timezone.utc))
        except Exception as exc:  # pragma: no cover - assertion reports the race
            errors.append(exc)

    threads = [threading.Thread(target=arrive), threading.Thread(target=timeout)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert errors == []
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state in {"FIRED_COMPLETE", "FIRED_TIMEOUT"}
        assert db.query(InboxModel).filter(InboxModel.sender_id.like("barrier:%")).count() == 1


def test_fire_cas_rejects_a_second_winner(barrier_db):
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message("owner", "worker-a", "task", dispatch_barrier={"label": "cas"})
    with barrier_db.begin() as db:
        barrier = db.query(CallbackBarrierModel).one()
        first = _fire_open_barrier_in_db(
            db,
            barrier,
            state="FIRED_TIMEOUT",
            close_reason="timeout",
        )
        assert first is not None
    with barrier_db.begin() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert (
            _fire_open_barrier_in_db(
                db,
                barrier,
                state="FIRED_TIMEOUT",
                close_reason="timeout",
            )
            is None
        )
    with barrier_db() as db:
        assert db.query(InboxModel).filter(InboxModel.sender_id.like("barrier:%")).count() == 1


def test_failure_notice_quota_is_single_alert_transient_preserves_watchdog(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("failure")
    first = insert_barrier_escalation_message("worker-a", "owner", "quota notice", "quota_or_auth")
    second = insert_barrier_escalation_message("worker-a", "owner", "quota notice", "quota_or_auth")
    transient = insert_barrier_escalation_message(
        "worker-b", "owner", "transient notice", "transient_api_error"
    )
    assert first is not None and second is not None and transient is not None
    with barrier_db() as db:
        alerts = db.query(InboxModel).filter(InboxModel.sender_id.like("barrier-alert:%")).all()
        watchdog = db.query(InboxModel).filter_by(sender_id="watchdog:worker-b").all()
        assert len(alerts) == 1 and len(watchdog) == 1
        members = {m.terminal_id: m for m in db.query(CallbackBarrierMemberModel).all()}
        assert members["worker-a"].state == "FAILED"
        assert members["worker-b"].state == "AWAITING"
        assert members["worker-b"].failure_class == "transient_api_error"


def test_supervisor_only_dispatch_and_barrier_control_are_owner_scoped(barrier_db):
    _seed_raw(barrier_db)
    assert callback_barrier_dispatch_allowed("owner", "worker-a") is True
    assert callback_barrier_dispatch_allowed("worker-b", "worker-a") is False
    create_inbox_message("owner", "worker-a", "task", dispatch_barrier={"label": "owned"})
    with barrier_db() as db:
        barrier_id = int(db.query(CallbackBarrierModel.id).scalar())
    with pytest.raises(ValueError, match="barrier_not_found"):
        callback_barrier_status(barrier_id=barrier_id, owner_id="worker-a")


def test_mcp_supervisor_barrier_path_remains_functional_end_to_end(barrier_db, monkeypatch):
    _seed_raw(barrier_db, workers=("worker-a",))
    monkeypatch.setenv("CAO_TERMINAL_ID", "owner")
    monkeypatch.setattr(mcp_server, "_current_terminal_id", lambda: "owner")
    post = MagicMock()
    deliver = MagicMock()
    monkeypatch.setattr(mcp_server.cao_http, "post", post)
    monkeypatch.setattr(inbox_module, "request_delivery", deliver)

    result = mcp_server._send_message_impl(
        "worker-a",
        "task",
        barrier="mcp-only",
        barrier_timeout_seconds=90,
        barrier_member_key="lane-a",
    )

    assert result["success"] is True, result
    post.assert_not_called()
    deliver.assert_called_once_with("worker-a")
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        member = db.query(CallbackBarrierMemberModel).one()
        task = db.query(InboxModel).one()
        assert (barrier.label, barrier.state) == ("mcp-only", "OPEN")
        assert (member.member_key, member.terminal_id, member.state) == (
            "lane-a",
            "worker-a",
            "AWAITING",
        )
        assert (task.sender_id, task.receiver_id, task.barrier_id, task.status) == (
            "owner",
            "worker-a",
            None,
            MessageStatus.PENDING.value,
        )


def test_direct_barrier_service_derives_process_principal_and_rejects_caller_selected_owner(
    barrier_db, monkeypatch
):
    _seed_raw(barrier_db, workers=("worker-a", "worker-process"))
    create_inbox_message(
        "owner",
        "worker-a",
        "task",
        dispatch_barrier={"label": "principal-bound", "member_key": "lane-a"},
    )
    with barrier_db() as db:
        barrier_id = int(db.query(CallbackBarrierModel.id).scalar())

    assert "sender_id" not in inspect.signature(callback_barrier_service.dispatch).parameters
    assert (
        "sender_id" not in inspect.signature(callback_barrier_service.dispatch_allowed).parameters
    )
    assert "owner_id" not in inspect.signature(callback_barrier_service.status).parameters
    assert "owner_id" not in inspect.signature(callback_barrier_service.cancel).parameters

    with pytest.raises(TypeError, match="sender_id"):
        callback_barrier_service.dispatch(
            sender_id="owner",
            receiver_id="worker-a",
            message="caller-selected task",
            refresh_ingest=False,
            barrier="principal-bound",
            barrier_timeout_seconds=None,
            barrier_member_key=None,
        )
    with pytest.raises(TypeError, match="owner_id"):
        callback_barrier_service.status(barrier_id=barrier_id, owner_id="owner")
    with pytest.raises(TypeError, match="owner_id"):
        callback_barrier_service.cancel(barrier_id=barrier_id, owner_id="owner")

    monkeypatch.setenv("CAO_TERMINAL_ID", "worker-process")
    allowed = MagicMock(wraps=callback_barrier_dispatch_allowed)
    status = MagicMock(wraps=callback_barrier_status)
    cancel = MagicMock(wraps=cancel_callback_barrier)
    monkeypatch.setattr(callback_barrier_service, "_dispatch_allowed", allowed)
    monkeypatch.setattr(callback_barrier_service, "callback_barrier_status", status)
    monkeypatch.setattr(callback_barrier_service, "cancel_callback_barrier", cancel)

    with pytest.raises(ValueError, match="supervisor ownership"):
        callback_barrier_service.dispatch(
            receiver_id="worker-a",
            message="derived worker task",
            refresh_ingest=False,
            barrier="principal-bound",
            barrier_timeout_seconds=None,
            barrier_member_key=None,
        )
    with pytest.raises(ValueError, match="barrier_not_found"):
        callback_barrier_service.status(barrier_id=barrier_id)
    with pytest.raises(ValueError, match="barrier_not_found"):
        callback_barrier_service.cancel(barrier_id=barrier_id)

    allowed.assert_called_once_with("worker-process", "worker-a")
    status.assert_called_once_with(
        barrier_id=barrier_id,
        barrier_label=None,
        owner_id="worker-process",
    )
    cancel.assert_called_once_with(
        barrier_id=barrier_id,
        barrier_label=None,
        owner_id="worker-process",
    )
    with barrier_db() as db:
        assert db.get(CallbackBarrierModel, barrier_id).state == "OPEN"
        assert db.query(InboxModel).count() == 1


def test_stale_worker_generation_cannot_fill_unrearmed_member(barrier_db):
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message(
        "owner",
        "worker-a",
        "task",
        dispatch_barrier={"label": "generation-fence", "member_key": "lane-a"},
    )
    assert settle_terminal_rebound("worker-a", "session", "zsh") == 2
    callback = create_inbox_message("worker-a", "owner", "stale generation callback")
    assert callback.status == MessageStatus.PENDING
    assert callback.barrier_id is None
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        member = db.query(CallbackBarrierMemberModel).one()
        assert barrier.state == "OPEN" and barrier.combined_message_id is None
        assert member.lifecycle_generation == 1 and member.state == "AWAITING"


def test_rebind_increments_generation_and_explicit_rearm_reuses_member(barrier_db):
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message(
        "owner",
        "worker-a",
        "task",
        dispatch_barrier={"label": "rearm", "member_key": "reviewer"},
    )
    insert_barrier_escalation_message("worker-a", "owner", "quota", "quota_or_auth")
    assert settle_terminal_rebound("worker-a", "session", "zsh") == 2
    create_inbox_message(
        "owner",
        "worker-a",
        "retry",
        dispatch_barrier={"label": "rearm", "member_key": "reviewer"},
    )
    with barrier_db() as db:
        members = db.query(CallbackBarrierMemberModel).all()
        assert len(members) == 1
        assert members[0].state == "AWAITING"
        assert members[0].lifecycle_generation == 2


def test_delete_marks_gone_and_fires_partial_immediately(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("gone")
    create_inbox_message("worker-a", "owner", "answer")
    delete_terminal_and_warm_intent("worker-b")
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "FIRED_COMPLETE"
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert combined.message.startswith("[callback barrier PARTIAL] gone — 1/2")


def test_label_reuse_after_close_creates_new_barrier(barrier_db):
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message("owner", "worker-a", "task", dispatch_barrier={"label": "reuse"})
    create_inbox_message("worker-a", "owner", "answer")
    create_inbox_message("owner", "worker-a", "task 2", dispatch_barrier={"label": "reuse"})
    with barrier_db() as db:
        rows = db.query(CallbackBarrierModel).order_by(CallbackBarrierModel.id).all()
        assert len(rows) == 2 and rows[0].state == "FIRED_COMPLETE" and rows[1].state == "OPEN"


@pytest.mark.parametrize(
    "dispatch",
    [
        {"label": ""},
        {"label": "   "},
        {"label": "x", "timeout_seconds": True},
        {"label": "x", "timeout_seconds": 0},
        {"label": "x", "timeout_seconds": 86401},
        {"label": "x", "member_key": ""},
    ],
)
def test_dispatch_validation_rejects_invalid_values(barrier_db, dispatch):
    _seed_raw(barrier_db, workers=("worker-a",))
    with pytest.raises(ValueError):
        create_inbox_message("owner", "worker-a", "task", dispatch_barrier=dispatch)


def test_utf8_cap_preserves_codepoint_and_points_to_durable_sources(barrier_db):
    _seed_raw(barrier_db)
    _dispatch_pair("bytes")
    create_inbox_message("worker-a", "owner", "😀" * 5000)
    create_inbox_message("worker-b", "owner", "done")
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert len(combined.message.encode("utf-8")) <= 16 * 1024
        combined.message.encode("utf-8").decode("utf-8")
        assert "list_messages/message trace" in combined.message


@pytest.mark.slow  # F254 D19: exceeds unit budget
def test_composed_pending_writer_count_is_ten_after_digest_seats_retire(barrier_db):
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator"
    expected = {
        "clients/database.py::claim_deferred_init_failure",
        "clients/database.py::_fire_open_barrier_in_db",
        "clients/database.py::_insert_routed_inbox_row",
        "clients/database.py::insert_barrier_escalation_message",
        "clients/database.py::insert_watchdog_auto_resume_message",
        "clients/database.py::insert_identity_authority_notice",
        "clients/database.py::_record_p5_orphan_notices",
        "clients/database.py::record_wpm1_stalled_notice.operation",
        "clients/database.py::settle_wpm1_terminal_batch.operation",
        "services/mailbox_service.py::delete_mailbox",
    }
    seats: dict[str, bool] = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        stack: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append((node.name, node))
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "InboxModel":
                    qualified = f"{path.relative_to(root).as_posix()}::" + ".".join(
                        name for name, _ in stack
                    )
                    expansions = [keyword.value for keyword in node.keywords if keyword.arg is None]
                    assert len(expansions) == 1
                    expansion = expansions[0]
                    direct = (
                        isinstance(expansion, ast.Call)
                        and isinstance(expansion.func, ast.Name)
                        and expansion.func.id == "_stamp_enqueue_generation"
                    )
                    stamped_names: set[str] = set()
                    if stack:
                        for candidate in ast.walk(stack[-1][1]):
                            if (
                                not isinstance(candidate, ast.Assign)
                                or candidate.lineno >= node.lineno
                            ):
                                continue
                            if (
                                isinstance(candidate.value, ast.Call)
                                and isinstance(candidate.value.func, ast.Name)
                                and candidate.value.func.id == "_stamp_enqueue_generation"
                            ):
                                stamped_names.update(
                                    target.id
                                    for target in candidate.targets
                                    if isinstance(target, ast.Name)
                                )
                    seats[qualified] = direct or (
                        isinstance(expansion, ast.Name) and expansion.id in stamped_names
                    )
                self.generic_visit(node)

        Visitor().visit(tree)
    assert seats == {qualified: True for qualified in expected}

    _seed_raw(barrier_db)
    with barrier_db.begin() as db:
        db.get(TerminalModel, "owner").lifecycle_generation = 7
    _dispatch_pair("stamp-composed")
    create_inbox_message("worker-a", "owner", "answer a")
    create_inbox_message("worker-b", "owner", "answer b")
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        combined = db.get(InboxModel, barrier.combined_message_id)
        assert combined.enqueue_generation == 7


def test_quota_rearm_completion_delivers_two_groups_and_only_combined_is_challenged(
    barrier_db, monkeypatch
):
    _seed_raw(barrier_db)
    create_inbox_message(
        "owner",
        "worker-a",
        "task a",
        dispatch_barrier={"label": "dual-lane", "member_key": "lane-a"},
    )
    create_inbox_message(
        "owner",
        "worker-b",
        "task b",
        dispatch_barrier={"label": "dual-lane", "member_key": "lane-b"},
    )
    alert = insert_barrier_escalation_message("worker-a", "owner", "quota notice", "quota_or_auth")
    assert alert is not None and alert.message_id is not None
    assert settle_terminal_rebound("worker-a", "session", "zsh") == 2
    create_inbox_message(
        "owner",
        "worker-a",
        "retry a",
        dispatch_barrier={"label": "dual-lane", "member_key": "lane-a"},
    )
    create_inbox_message("worker-a", "owner", "answer a")
    create_inbox_message("worker-b", "owner", "answer b")

    with barrier_db.begin() as db:
        barrier = db.query(CallbackBarrierModel).one()
        combined = db.get(InboxModel, barrier.combined_message_id)
        combined_id = int(combined.id)
        captured: dict[str, str] = {}

        def capture(_receiver_id, message, **_kwargs):
            captured["message"] = message
            return {"success": True}

        monkeypatch.setenv("CAO_TERMINAL_ID", combined.sender_id)
        monkeypatch.setattr(
            mcp_server,
            "_current_terminal_id",
            lambda: combined.sender_id,
        )
        monkeypatch.setattr(mcp_server, "ENABLE_SENDER_ID_INJECTION", True)
        monkeypatch.setattr(mcp_server, "_send_to_inbox", capture)
        assert mcp_server._send_message_impl("owner", combined.message) == {"success": True}
        combined.message = captured["message"]

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
    monkeypatch.setattr(
        inbox_module,
        "_wpm2_lookup",
        lambda *_args, **_kwargs: ("unresolved", {}),
    )
    monkeypatch.setattr(
        inbox_module.terminal_service,
        "prepare_input",
        lambda _terminal, value, _shape: value,
    )

    def send(_terminal, _wire, **kwargs):
        kwargs["on_submitted"](observation)
        return observation

    monkeypatch.setattr(inbox_module.terminal_service, "send_prepared_input", send)
    monkeypatch.setattr(
        inbox_module,
        "confirm_delivery",
        lambda *_args, **_kwargs: ("hit", {"kind": "screen_confirmed"}),
    )
    service = InboxService()
    service._commit_watchdog_ops = MagicMock()
    service.deliver_pending("owner", num_messages=0)

    with barrier_db() as db:
        attempts = db.query(InboxDeliveryAttemptModel).all()
        assert len(attempts) == 2
        membership = {
            tuple(
                message_id
                for message_id, in db.query(InboxDeliveryAttemptMemberModel.message_id)
                .filter_by(attempt_uuid=attempt.attempt_uuid)
                .order_by(InboxDeliveryAttemptMemberModel.position)
                .all()
            )
            for attempt in attempts
        }
        assert membership == {(int(alert.message_id),), (combined_id,)}
        events = db.query(InboxMessageTraceEventModel).all()
        assert [(event.message_id, event.kind) for event in events] == [
            (combined_id, "attempt_challenge")
        ]


@pytest.mark.parametrize("mailbox_owner", [False, True])
def test_concurrent_first_tag_creates_one_open_barrier_for_each_owner_form(
    barrier_db, mailbox_owner
):
    _seed_raw(barrier_db)
    if mailbox_owner:
        with barrier_db.begin() as db:
            db.add(
                MailboxModel(
                    id="mb_aaaaaaaa",
                    session_name="cao-wpq7",
                    role="supervisor",
                    current_terminal_id="owner",
                    generation=1,
                    consumed_through_id=0,
                )
            )
            db.add(
                MailboxIncarnationModel(
                    mailbox_id="mb_aaaaaaaa",
                    generation=1,
                    terminal_id="owner",
                    published_at=datetime.now(),
                )
            )
    errors = []

    def dispatch(worker):
        try:
            create_inbox_message("owner", worker, "task", dispatch_barrier={"label": "concurrent"})
        except Exception as exc:  # pragma: no cover - assertion reports the exact race
            errors.append(exc)

    threads = [
        threading.Thread(target=dispatch, args=(worker,)) for worker in ("worker-a", "worker-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert errors == []
    with barrier_db() as db:
        assert db.query(CallbackBarrierModel).count() == 1
        assert db.query(CallbackBarrierMemberModel).count() == 2


def test_owner_gone_barrier_closes_instead_of_bricking_the_sweep(orphaned_barrier_db):
    """The exact live crash: sweeping an owner-gone barrier must close, not raise.

    Incident 2026-07-25: barrier 5 (`drill-r2-brainstorm`) outlived owner
    `8494ddf1`. The startup sweep tried to enqueue its combined callback to the
    deleted receiver, `_stamp_enqueue_generation` raised
    `pending_receiver_generation_unavailable`, and cao-server crash-looped 634
    times over two hours — `cao launch` failed `Connection refused` on :9889 and
    the fleet could not start at all.

    Raw evidence:
    probes/error-pane-samples/2026-07-25-cao-server-crashloop-barrier-owner-gone.txt
    """
    fired = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))

    assert fired == []
    with orphaned_barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"
        assert barrier.combined_message_id is None
        # No PENDING row may be minted for the receiver that does not exist.
        assert (
            db.query(InboxModel)
            .filter_by(status=MessageStatus.PENDING.value, receiver_id="owner")
            .count()
            == 0
        )
        # Held callbacks are closed with a named reason, never left dangling.
        held = db.query(InboxModel).filter_by(barrier_id=barrier.id).one()
        assert held.status == MessageStatus.CANCELLED.value
        assert held.failure_reason == "barrier_owner_gone"


def test_owner_gone_sweep_is_idempotent_across_reboots(orphaned_barrier_db):
    """Repeated sweeps must stay quiet — the crash-loop swept the same row 634x.

    A fix that closes the barrier but re-raises (or re-fires) on the next boot
    would still brick the server, just more slowly.
    """
    first = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))
    second = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=2))
    third = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=3))

    assert first == second == third == []
    with orphaned_barrier_db() as db:
        assert db.query(CallbackBarrierModel).one().state == "CANCELLED"


def test_owner_gone_mailbox_barrier_also_closes(barrier_db):
    """Same defect through the mailbox-owner branch, not just the terminal branch.

    `_fire_open_barrier_in_db` resolved a mailbox owner with `.one()`, which
    raises `NoResultFound` on a deleted mailbox — a different exception, the same
    unbootable outcome. Bug-family sweep, not a second incident.
    """
    _seed_raw(barrier_db)
    _seed_mailbox_owner(barrier_db)
    _dispatch_pair("mailbox-orphan")
    with barrier_db.begin() as db:
        assert db.query(MailboxModel).filter_by(id="mb_aaaaaaaa").delete() == 1

    fired = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))

    assert fired == []
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"


def test_deleting_owner_closes_its_open_barriers(barrier_db):
    """Delete-side sweep: an owner's OPEN barriers close when the owner is deleted.

    `_mark_barrier_member_gone_in_db` only ever handled barriers the terminal was
    a MEMBER of. A supervisor is an OWNER, never a member, so deleting it left an
    orphaned OPEN barrier behind — the state that bricked the server.
    """
    _seed_raw(barrier_db)
    _dispatch_pair("owned")
    with barrier_db() as db:
        assert db.query(CallbackBarrierModel).one().state == "OPEN"

    delete_terminal_and_warm_intent("owner")

    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"
    # And the sweep over the now-clean table is a no-op rather than a raise.
    assert fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1)) == []


def test_one_unfireable_barrier_does_not_wedge_the_others(barrier_db, monkeypatch):
    """Per-barrier savepoint: a raise on one barrier must not abort the sweep.

    Without isolation a single bad row aborts the whole transaction, and since
    the sweep is retried unchanged every poll, NO barrier would ever fire again.
    """
    _seed_raw(barrier_db)
    _dispatch_pair("poison")
    _dispatch_pair("healthy")

    real = dbmod._fire_open_barrier_in_db

    def explode(db, barrier, **kwargs):
        if barrier.label == "poison":
            raise ValueError("pending_receiver_generation_unavailable")
        return real(db, barrier, **kwargs)

    monkeypatch.setattr(dbmod, "_fire_open_barrier_in_db", explode)
    fired = fire_due_barriers(datetime.now(timezone.utc) + timedelta(hours=1))

    assert len(fired) == 1
    with barrier_db() as db:
        states = {row.label: row.state for row in db.query(CallbackBarrierModel).all()}
    assert states["healthy"] == "FIRED_TIMEOUT"
    assert states["poison"] == "OPEN"


# ---------------------------------------------------------------------------
# F71 — sweep closing branch: completion-outranks-timeout (blueprint §1)
# ---------------------------------------------------------------------------


def _seed_all_arrived_barrier(sessions, *, timeout_at: datetime):
    """Seed an all-ARRIVED barrier holding callback messages, never fired.

    Reproduces the F71 live shape (barrier 5 `drill-r2-brainstorm`): every
    member delivered its callback (held), all members stamped ARRIVED, but the
    last-arrival `_maybe_fire_completed_barrier` call was skipped by a crash, so
    the barrier sits OPEN. The sweep must close it FIRED_COMPLETE.
    """
    _seed_raw(sessions)
    _dispatch_pair("f71")
    create_inbox_message("worker-a", "owner", "answer a")
    with sessions.begin() as db:
        # Stamp every member ARRIVED without firing the last one, as F71 left it.
        # (The second real callback would fire COMPLETE through the arrival path —
        # the crash is exactly that `_maybe_fire_completed_barrier` call being
        # skipped, so we reproduce the state as found, not as the API can reach.)
        for member in db.query(CallbackBarrierMemberModel).all():
            member.state = "ARRIVED"
        db.query(CallbackBarrierModel).update(
            {CallbackBarrierModel.timeout_at: timeout_at},
            synchronize_session=False,
        )
    with sessions() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "OPEN", "precondition: barrier must still be open"
        assert barrier.combined_message_id is None
        # worker-a holds a callback; worker-b is stamped ARRIVED raw with none.
        assert db.query(InboxModel).filter_by(status=MessageStatus.HELD.value).count() == 1


@pytest.mark.parametrize("timeout_in_future", [False, True], ids=["expired", "not-yet-due"])
def test_f71_sweep_closes_all_arrived_barrier_fired_complete(barrier_db, timeout_in_future):
    """AC#1 + AC#2 — an all-ARRIVED barrier closes FIRED_COMPLETE on the sweep.

    AC#1: timeout in the past (would previously have fired FIRED_TIMEOUT).
    AC#2: timeout in the future — the exact live shape (barrier 5 all-ARRIVED at
    06:28, never a timeout). Completion runs on every OPEN pass, not just
    timed-out ones.
    """
    now = datetime.now(timezone.utc)
    timeout_at = now + timedelta(hours=1) if timeout_in_future else now - timedelta(hours=1)
    _seed_all_arrived_barrier(barrier_db, timeout_at=timeout_at)

    fired = fire_due_barriers(now)

    assert len(fired) == 1
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "FIRED_COMPLETE"
        assert barrier.close_reason == "complete"
        combined = db.query(InboxModel).filter_by(id=barrier.combined_message_id).one()
        assert combined.status == MessageStatus.PENDING.value
        assert combined.message.startswith("[callback barrier COMPLETE] f71 — 2/2 in ")
        assert "answer a" in combined.message
        assert db.query(InboxModel).filter(InboxModel.sender_id.like("barrier:%")).count() == 1


def test_f71_sweep_leaves_incomplete_untimed_out_barrier_open(barrier_db):
    """AC#3 — an incomplete, not-yet-timed-out barrier is untouched by the sweep."""
    _seed_raw(barrier_db)
    _dispatch_pair("f71-open")
    create_inbox_message("worker-a", "owner", "answer a")  # worker-b still AWAITING

    fired = fire_due_barriers(datetime.now(timezone.utc))

    assert fired == []
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "OPEN"
        assert barrier.combined_message_id is None


def test_f71_completion_vs_sweep_race_has_exactly_one_winner(barrier_db):
    """AC#4 — a concurrent completion-fire and sweep-fire yield one combined row.

    The CAS in `_fire_open_barrier_in_db` is the single serialization point: the
    winner fires, the loser no-ops. Completion-vs-sweep is structurally the same
    interleaving as the existing arrival-vs-timeout race.
    """
    _seed_raw(barrier_db, workers=("worker-a",))
    create_inbox_message("owner", "worker-a", "task", dispatch_barrier={"label": "f71-race"})
    with barrier_db.begin() as db:
        db.query(CallbackBarrierModel).update(
            {CallbackBarrierModel.timeout_at: datetime.now() - timedelta(seconds=1)}
        )
    start = threading.Barrier(2)
    errors = []

    def complete():
        try:
            start.wait()
            with barrier_db.begin() as db:
                barrier = db.query(CallbackBarrierModel).one()
                _maybe_fire_completed_barrier(db, barrier)
        except Exception as exc:  # pragma: no cover - assertion reports the race
            errors.append(exc)

    def sweep():
        try:
            start.wait()
            fire_due_barriers(datetime.now(timezone.utc))
        except Exception as exc:  # pragma: no cover - assertion reports the race
            errors.append(exc)

    threads = [threading.Thread(target=complete), threading.Thread(target=sweep)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert errors == []
    with barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state in {"FIRED_COMPLETE", "FIRED_TIMEOUT"}
        assert db.query(InboxModel).filter(InboxModel.sender_id.like("barrier:%")).count() == 1


@pytest.fixture
def completed_orphaned_barrier_db(barrier_db):
    """All-ARRIVED barrier WITH held messages, then owner orphaned.

    AC#5 sub-case: the existing `orphaned_barrier_db` holds 0 callback messages,
    so it cannot prove that an owner-gone close wins over a COMPLETE fire when
    held content is present. This fixture seeds the all-ARRIVED barrier with held
    messages first, then orphans the owner AFTER seeding.
    """
    _seed_all_arrived_barrier(
        barrier_db, timeout_at=datetime.now(timezone.utc) - timedelta(hours=1)
    )
    _orphan_owner(barrier_db)
    return barrier_db


def test_f71_owner_gone_wins_over_complete(completed_orphaned_barrier_db):
    """AC#5 — an all-ARRIVED barrier whose owner is gone closes CANCELLED, not COMPLETE.

    The owner-gone check in `_fire_open_barrier_in_db` runs before the CAS and
    must win even for a complete barrier — delivering a combined callback to a
    dead receiver would raise.
    """
    fired = fire_due_barriers(datetime.now(timezone.utc))

    assert fired == []
    with completed_orphaned_barrier_db() as db:
        barrier = db.query(CallbackBarrierModel).one()
        assert barrier.state == "CANCELLED"
        assert barrier.close_reason == "owner_gone"
        assert barrier.combined_message_id is None
        assert (
            db.query(InboxModel)
            .filter_by(status=MessageStatus.PENDING.value, receiver_id="owner")
            .count()
            == 0
        )
        held = db.query(InboxModel).filter_by(barrier_id=barrier.id).all()
        assert len(held) == 1
        assert {row.status for row in held} == {MessageStatus.CANCELLED.value}

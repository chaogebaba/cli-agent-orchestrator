"""WPQ7 callback barrier acceptance and mutation-killing controls."""

from __future__ import annotations

import ast
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as dbmod
from cli_agent_orchestrator.clients.database import (
    Base,
    CallbackBarrierMemberModel,
    CallbackBarrierModel,
    InboxDeliveryAttemptMemberModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    _fire_open_barrier_in_db,
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
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def barrier_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpq7.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.mailbox_service.SessionLocal", sessions)
    monkeypatch.setattr("cli_agent_orchestrator.services.cleanup_service.SessionLocal", sessions)
    return sessions


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
        assert combined.message.count("[MISSING") == 2

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


def test_composed_pending_writer_count_is_twelve_and_each_seat_stamps():
    root = Path(__file__).parents[2] / "src" / "cli_agent_orchestrator"
    seats = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "InboxModel":
                    seats.append((path.name, ".".join(stack)))
                self.generic_visit(node)

        Visitor().visit(tree)
    assert len(seats) == 12
    assert ("database.py", "_insert_routed_inbox_row") in seats
    assert ("database.py", "_fire_open_barrier_in_db") in seats
    assert ("database.py", "insert_barrier_escalation_message") in seats


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

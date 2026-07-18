"""WPQ7 callback barrier acceptance and mutation-killing controls."""

from __future__ import annotations

import ast
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
from cli_agent_orchestrator.services import inbox_service as inbox_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


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
    post = MagicMock()
    deliver = MagicMock()
    monkeypatch.setattr(mcp_server.cao_http, "post", post)
    monkeypatch.setattr(inbox_module.inbox_service, "deliver_pending", deliver)

    result = mcp_server._send_message_impl(
        "worker-a",
        "task",
        barrier="mcp-only",
        barrier_timeout_seconds=90,
        barrier_member_key="lane-a",
    )

    assert result["success"] is True
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


def test_composed_pending_writer_count_is_twelve_and_each_seat_stamps(barrier_db):
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
        "services/mailbox_service.py::publish_supervisor_incarnation",
        "services/mailbox_service.py::digest_stale_pending_for_terminal",
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

"""WPQ11 incarnation-owned parked-mail controls."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    get_pending_messages,
    settle_terminal_fallback,
    settle_terminal_rebound,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import mailbox_service
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import (
    MailboxClaim,
    MailboxDomainError,
    digest_stale_pending_for_terminal,
    list_messages,
    publish_supervisor_incarnation,
)
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def park_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpq11.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    database._migrate_mailbox_columns()
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _terminal(db, terminal_id: str, generation: int, **values) -> TerminalModel:
    row = TerminalModel(
        id=terminal_id,
        tmux_session="wpq11",
        tmux_window=terminal_id,
        provider="codex",
        lifecycle_generation=generation,
        init_state="ready",
        **values,
    )
    db.add(row)
    return row


def _mailbox(db, terminal_id: str = "11111111", generation: int = 1) -> MailboxModel:
    now = datetime.now()
    row = MailboxModel(
        id="mb_aaaaaaaa",
        session_name="cao-wpq11",
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=generation,
        consumed_through_id=0,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=row.id,
            generation=generation,
            terminal_id=terminal_id,
            published_at=now,
        )
    )
    return row


def _message(
    db,
    *,
    receiver_id: str,
    status: MessageStatus = MessageStatus.PENDING,
    logical_receiver_id: str | None = None,
    enqueue_generation: int | None = None,
    body: str = "callback",
) -> InboxModel:
    row = InboxModel(
        sender_id="99999999",
        receiver_id=receiver_id,
        logical_receiver_id=logical_receiver_id,
        enqueue_generation=enqueue_generation,
        message=body,
        orchestration_type=OrchestrationType.SEND_MESSAGE.value,
        status=status.value,
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def _deliver_and_capture(terminal_id: str) -> list[tuple[str, str]]:
    pasted: list[tuple[str, str]] = []
    observation = BoundaryObservation("wpq11", TerminalStatus.IDLE, 3, 1, 4, 2, 4)
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
            return_value=("unverified", {"kind": "send_returned_unverified"}),
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
        InboxService().deliver_pending(terminal_id, num_messages=0)
    return pasted


def test_fresh_publication_advances_cursor_and_parks_mixed_backlog_without_push(park_db):
    with park_db.begin() as db:
        _mailbox(db)
        _terminal(db, "11111111", 3)
        _terminal(db, "22222222", 0)
        delivered = [
            _message(
                db,
                receiver_id="11111111",
                status=MessageStatus.DELIVERED,
                logical_receiver_id="mb_aaaaaaaa",
                enqueue_generation=1,
                body=f"history-{index}",
            )
            for index in range(101)
        ]
        stale_logical = _message(
            db,
            receiver_id="11111111",
            logical_receiver_id="mb_aaaaaaaa",
            enqueue_generation=1,
        )
        stale_raw = _message(db, receiver_id="11111111", enqueue_generation=3)
        current_row = _message(db, receiver_id="22222222", enqueue_generation=0)

    result = publish_supervisor_incarnation(
        MailboxClaim("cao-wpq11", "supervisor", "mb_aaaaaaaa", 1), "22222222"
    )

    assert result["digest_message_id"] is None
    assert get_pending_messages("22222222", limit=200) == []
    with park_db() as db:
        mailbox = db.get(MailboxModel, "mb_aaaaaaaa")
        assert mailbox.consumed_through_id == delivered[-1].id
        parked = (
            db.query(InboxModel)
            .filter(InboxModel.status == MessageStatus.PARKED.value)
            .order_by(InboxModel.id)
            .all()
        )
        assert [row.id for row in parked] == [stale_logical.id, stale_raw.id, current_row.id]
        assert [(row.owner_receiver_id, row.owner_generation) for row in parked] == [
            ("11111111", 1),
            ("11111111", 3),
            ("22222222", 0),
        ]
        assert all(row.digested_into is None for row in parked)
        assert db.query(InboxModel).filter_by(sender_id="mailbox-digest").count() == 0
        assert (
            db.query(InboxMessageTraceEventModel).filter_by(kind="digest_high_water").count() == 0
        )
        incarnation = db.query(MailboxIncarnationModel).filter_by(terminal_id="22222222").one()
        assert incarnation.digest_message_id is None


def test_parked_query_requires_incarnation_and_audit_marks_dead_successor(park_db):
    with park_db.begin() as db:
        _mailbox(db)
        _terminal(db, "11111111", 1)
        _terminal(db, "22222222", 0)
        parked = _message(
            db,
            receiver_id="11111111",
            logical_receiver_id="mb_aaaaaaaa",
            enqueue_generation=1,
        )
    publish_supervisor_incarnation(
        MailboxClaim("cao-wpq11", "supervisor", "mb_aaaaaaaa", 1), "22222222"
    )

    assert all(item["id"] != parked.id for item in list_messages("mb_aaaaaaaa")["items"])
    with pytest.raises(MailboxDomainError, match="parked_query_requires_incarnation"):
        list_messages("mb_aaaaaaaa", status=MessageStatus.PARKED)
    page = list_messages("mb_aaaaaaaa", status=MessageStatus.PARKED, generation=1)
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == parked.id
    assert page["items"][0]["owner_receiver_id"] == "11111111"
    assert page["items"][0]["owner_generation"] == 1
    assert page["items"][0]["dead_to_successor"] is True
    audit = list_messages("mb_aaaaaaaa", audit_browse=True)
    parked_item = next(item for item in audit["items"] if item["id"] == parked.id)
    assert parked_item["dead_to_successor"] is True


def test_in_place_resume_reactivates_exact_owner_and_survives_real_preflight(park_db):
    with park_db.begin() as db:
        _mailbox(db)
        _terminal(db, "11111111", 3)
        logical = _message(
            db,
            receiver_id="11111111",
            status=MessageStatus.PARKED,
            logical_receiver_id="mb_aaaaaaaa",
            enqueue_generation=1,
        )
        logical.owner_receiver_id, logical.owner_generation = "11111111", 1
        raw = _message(
            db,
            receiver_id="11111111",
            status=MessageStatus.PARKED,
            enqueue_generation=3,
        )
        raw.owner_receiver_id, raw.owner_generation = "11111111", 3
        wrong = _message(
            db,
            receiver_id="11111111",
            status=MessageStatus.PARKED,
            enqueue_generation=2,
        )
        wrong.owner_receiver_id, wrong.owner_generation = "11111111", 2

    assert settle_terminal_rebound("11111111", "session-uuid", "codex resume") == 4
    assert digest_stale_pending_for_terminal("11111111") == 0
    with park_db() as db:
        logical_row, raw_row, wrong_row = [
            db.get(InboxModel, row.id) for row in (logical, raw, wrong)
        ]
        assert (logical_row.status, logical_row.enqueue_generation) == ("pending", 1)
        assert (raw_row.status, raw_row.enqueue_generation) == ("pending", 4)
        assert wrong_row.status == "parked"
        assert (raw_row.owner_receiver_id, raw_row.owner_generation) == ("11111111", 3)


def test_fallback_resume_routes_exact_owner_and_trigger_keeps_owner_immutable(park_db):
    with park_db.begin() as db:
        _mailbox(db, generation=2)
        _terminal(db, "11111111", 5, recovery_state="fallback_starting")
        _terminal(db, "22222222", 7, provider_session_id="provider-session")
        logical = _message(
            db,
            receiver_id="11111111",
            status=MessageStatus.PARKED,
            logical_receiver_id="mb_aaaaaaaa",
            enqueue_generation=2,
        )
        logical.owner_receiver_id, logical.owner_generation = "11111111", 2
        raw = _message(
            db,
            receiver_id="11111111",
            status=MessageStatus.PARKED,
            enqueue_generation=5,
        )
        raw.owner_receiver_id, raw.owner_generation = "11111111", 5

    assert settle_terminal_fallback("11111111", "22222222") == 2
    assert digest_stale_pending_for_terminal("22222222") == 0
    with park_db() as db:
        mailbox = db.get(MailboxModel, "mb_aaaaaaaa")
        logical_row, raw_row = [db.get(InboxModel, row.id) for row in (logical, raw)]
        assert mailbox.current_terminal_id == "22222222"
        assert (logical_row.receiver_id, logical_row.enqueue_generation) == ("22222222", 2)
        assert (raw_row.receiver_id, raw_row.enqueue_generation) == ("22222222", 7)
        assert (logical_row.owner_receiver_id, logical_row.owner_generation) == ("11111111", 2)
        assert (raw_row.owner_receiver_id, raw_row.owner_generation) == ("11111111", 5)

    with pytest.raises(IntegrityError, match="parked_owner_immutable"):
        with park_db.begin() as db:
            db.execute(
                text("UPDATE inbox SET owner_receiver_id='22222222' WHERE id=:id"),
                {"id": raw.id},
            )


@pytest.mark.parametrize("variant", ["raw_in_place", "raw_fallback", "logical_fallback"])
def test_reactivated_row_reaches_ordinary_delivery(park_db, variant):
    source, replacement = "11111111", "22222222"
    with park_db.begin() as db:
        if variant == "raw_in_place":
            _terminal(db, source, 3)
            target = source
            owner_generation = 3
            logical_receiver_id = None
        elif variant == "raw_fallback":
            _terminal(db, source, 5, recovery_state="fallback_starting")
            _terminal(db, replacement, 7, provider_session_id="provider-session")
            target = replacement
            owner_generation = 5
            logical_receiver_id = None
        else:
            _terminal(db, source, 5, recovery_state="fallback_starting")
            _terminal(db, replacement, 7, provider_session_id="provider-session")
            _mailbox(db, source, 2)
            target = replacement
            owner_generation = 2
            logical_receiver_id = "mb_aaaaaaaa"
        row = _message(
            db,
            receiver_id=source,
            status=MessageStatus.PARKED,
            logical_receiver_id=logical_receiver_id,
            enqueue_generation=owner_generation,
            body=variant,
        )
        row.owner_receiver_id = source
        row.owner_generation = owner_generation
        message_id = int(row.id)

    if variant == "raw_in_place":
        assert settle_terminal_rebound(source, "provider-session", "codex resume") == 4
    else:
        assert settle_terminal_fallback(source, replacement) == 1
    assert digest_stale_pending_for_terminal(target) == 0

    pasted = _deliver_and_capture(target)

    with park_db() as db:
        delivered = db.get(InboxModel, message_id)
        assert delivered.status == MessageStatus.DELIVERED.value
        assert (delivered.owner_receiver_id, delivered.owner_generation) == (
            source,
            owner_generation,
        )
    assert len(pasted) == 1
    assert pasted[0][0] == target
    assert variant in pasted[0][1]


def test_fresh_successor_reparks_fallback_commit_without_rewriting_owner(park_db):
    source, replacement, successor = "11111111", "22222222", "33333333"
    with park_db.begin() as db:
        _terminal(db, source, 5, recovery_state="fallback_starting")
        _terminal(db, replacement, 7, provider_session_id="provider-session")
        _terminal(db, successor, 0)
        _mailbox(db, source, 2)
        row = _message(
            db,
            receiver_id=source,
            status=MessageStatus.PARKED,
            logical_receiver_id="mb_aaaaaaaa",
            enqueue_generation=2,
            body="callback survives crash-before-wake",
        )
        row.owner_receiver_id, row.owner_generation = source, 2
        message_id = int(row.id)

    assert settle_terminal_fallback(source, replacement) == 1
    publication = publish_supervisor_incarnation(
        MailboxClaim("cao-wpq11", "supervisor", "mb_aaaaaaaa", 2), successor
    )

    assert publication["digest_message_id"] is None
    with park_db() as db:
        parked = db.get(InboxModel, message_id)
        assert parked.status == MessageStatus.PARKED.value
        assert (parked.owner_receiver_id, parked.owner_generation) == (source, 2)
        assert parked.receiver_id == replacement

"""Runtime controls for publication-digest cursors and native composer admission."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptMemberModel,
    InboxDeliveryAttemptModel,
    InboxMessageTraceEventModel,
    InboxModel,
    MailboxIncarnationModel,
    MailboxModel,
    TerminalModel,
    begin_delivery_attempt,
    confirm_batch_from_prior_attempt,
    get_pending_messages,
    settle_delivery_attempt,
    settle_open_attempt_inferred_delivered,
    settle_wpm1_terminal_batch,
    transition_pending_to_inferred_delivered,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.services import mailbox_service, terminal_service
from cli_agent_orchestrator.services import inbox_service as inbox_service_module
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.mailbox_service import (
    ack_messages,
    claim_mailbox,
    publish_supervisor_incarnation,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
)


@pytest.fixture
def wpq10_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpq10.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(mailbox_service, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _terminal(db, terminal_id: str) -> None:
    db.add(
        TerminalModel(
            id=terminal_id,
            tmux_session="wpq10",
            tmux_window=terminal_id,
            provider="claude_code",
            agent_profile="code_supervisor",
            init_state="ready",
        )
    )


def _mailbox(
    db,
    *,
    mailbox_id: str = "mb_wpq10aa",
    session_name: str = "wpq10-session",
    terminal_id: str = "old",
) -> MailboxModel:
    now = datetime.now()
    row = MailboxModel(
        id=mailbox_id,
        session_name=session_name,
        role="supervisor",
        current_terminal_id=terminal_id,
        generation=1,
        consumed_through_id=0,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.add(
        MailboxIncarnationModel(
            mailbox_id=mailbox_id,
            generation=1,
            terminal_id=terminal_id,
            published_at=now,
        )
    )
    return row


def _message(
    db,
    receiver_id: str,
    *,
    mailbox_id: str = "mb_wpq10aa",
    status: MessageStatus = MessageStatus.PENDING,
    kind: OrchestrationType = OrchestrationType.SEND_MESSAGE,
) -> InboxModel:
    mailbox = db.get(MailboxModel, mailbox_id)
    row = InboxModel(
        sender_id="peer",
        receiver_id=receiver_id,
        logical_receiver_id=mailbox_id,
        enqueue_generation=mailbox.generation,
        message=f"message-{receiver_id}",
        orchestration_type=kind.value,
        status=status.value,
        created_at=datetime.now(),
    )
    db.add(row)
    db.flush()
    return row


def _direct_digest(db, *, receiver_id: str = "receiver") -> tuple[int, int]:
    history = _message(db, receiver_id, status=MessageStatus.DELIVERED)
    digest = _message(
        db,
        receiver_id,
        status=MessageStatus.PENDING,
        kind=OrchestrationType.MAILBOX_DIGEST,
    )
    db.add(
        InboxMessageTraceEventModel(
            message_id=digest.id,
            kind="digest_high_water",
            payload={"high_water": history.id},
        )
    )
    db.flush()
    return int(digest.id), int(history.id)


def _attempt(db, message_id: int, *, outcome: str | None = None) -> str:
    attempt_uuid = str(uuid.uuid4())
    db.add(
        InboxDeliveryAttemptModel(
            attempt_uuid=attempt_uuid,
            receiver_terminal_id="receiver",
            provider="claude_code",
            payload_hash=attempt_uuid,
            payload_length=1,
            sender_id="mailbox-digest",
            orchestration_type=OrchestrationType.MAILBOX_DIGEST.value,
            outcome=outcome,
            reason="confirmation_timeout" if outcome == "ambiguous" else None,
        )
    )
    db.add(
        InboxDeliveryAttemptMemberModel(
            attempt_uuid=attempt_uuid,
            message_id=message_id,
            position=0,
        )
    )
    return attempt_uuid


def _confirm_family(wpq10_db, family: str, digest_id: int) -> None:
    if family == "normal":
        with wpq10_db() as db:
            receiver_id = db.get(InboxModel, digest_id).receiver_id
        selected = [
            row for row in get_pending_messages(receiver_id) if row.id == digest_id
        ]
        attempt_uuid = begin_delivery_attempt(
            selected, receiver_id, "claude_code", "h", 1
        )
        assert settle_delivery_attempt(
            attempt_uuid, MessageStatus.DELIVERED, "confirmed"
        )
    elif family == "prior":
        with wpq10_db.begin() as db:
            attempt_uuid = _attempt(db, digest_id)
        assert confirm_batch_from_prior_attempt([digest_id], attempt_uuid)
    elif family == "wpm1":
        with wpq10_db.begin() as db:
            _attempt(db, digest_id, outcome="ambiguous")
        assert (
            settle_wpm1_terminal_batch(
                [digest_id], MessageStatus.DELIVERED, "receiver"
            )
            == "settled"
        )
    elif family == "inferred_open":
        with wpq10_db.begin() as db:
            db.get(InboxModel, digest_id).status = MessageStatus.DELIVERING.value
            attempt_uuid = _attempt(db, digest_id)
        assert settle_open_attempt_inferred_delivered(attempt_uuid, {"proof": "reply"})
    elif family == "inferred_cap":
        assert transition_pending_to_inferred_delivered(digest_id, {"proof": "reply"})
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(family)


@pytest.mark.parametrize(
    "family", ["normal", "prior", "wpm1", "inferred_open", "inferred_cap"]
)
def test_wpq10_all_confirmation_families_advance_build_cursor(wpq10_db, family):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        digest_id, high_water = _direct_digest(db)
    _confirm_family(wpq10_db, family, digest_id)
    with wpq10_db() as db:
        assert db.get(InboxModel, digest_id).status == MessageStatus.DELIVERED.value
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == high_water


def test_wpq10_digest_delivery_and_cursor_share_one_transaction(
    wpq10_db, monkeypatch
):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        digest_id, _ = _direct_digest(db)
    selected = get_pending_messages("receiver")
    attempt_uuid = begin_delivery_attempt(selected, "receiver", "claude_code", "h", 1)

    def interrupt(_db, _ids):
        raise RuntimeError("cursor write interrupted")

    monkeypatch.setattr(database, "_advance_digest_cursor_in_db", interrupt)
    with pytest.raises(RuntimeError, match="cursor write interrupted"):
        settle_delivery_attempt(attempt_uuid, MessageStatus.DELIVERED, "confirmed")
    with wpq10_db() as db:
        assert db.get(InboxModel, digest_id).status == MessageStatus.DELIVERING.value
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == 0
        assert db.get(InboxDeliveryAttemptModel, attempt_uuid).settled_at is None


def test_wpq10_relaunch_consumes_only_persisted_publication_history(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db)
        _terminal(db, "old")
        _terminal(db, "next")
        _terminal(db, "later")
        history = _message(db, "old", status=MessageStatus.DELIVERED)
    first = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "next")
    digest_id = first["digest_message_id"]
    assert digest_id is not None
    _confirm_family(wpq10_db, "normal", digest_id)
    second = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "later")
    assert second["digest_message_id"] is None
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == history.id


def test_wpq10_failed_publication_delivery_repeats_without_cursor_loss(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db)
        for terminal_id in ("old", "next", "later"):
            _terminal(db, terminal_id)
        _message(db, "old", status=MessageStatus.DELIVERED)
    first = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "next")
    selected = get_pending_messages("next")
    attempt_uuid = begin_delivery_attempt(selected, "next", "claude_code", "h", 1)
    assert settle_delivery_attempt(
        attempt_uuid, MessageStatus.DELIVERY_FAILED, "failed", reason="not_confirmed"
    )
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == 0
    second = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "later")
    assert second["digest_message_id"] is not None


def test_wpq10_late_delivery_below_digest_id_is_not_consumed(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db)
        _terminal(db, "old")
        _terminal(db, "next")
        summarized = _message(db, "old", status=MessageStatus.DELIVERED)
        late = _message(db, "old", status=MessageStatus.PENDING)
    publication = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "next")
    digest_id = publication["digest_message_id"]
    assert summarized.id < late.id < digest_id
    with wpq10_db.begin() as db:
        db.get(InboxModel, late.id).status = MessageStatus.DELIVERED.value
    _confirm_family(wpq10_db, "normal", digest_id)
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == summarized.id


def test_wpq10_lower_auto_cursor_cannot_overwrite_higher_explicit_ack(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        digest_id, low = _direct_digest(db)
        high = _message(db, "receiver", status=MessageStatus.DELIVERED)
    selected = [row for row in get_pending_messages("receiver") if row.id == digest_id]
    attempt_uuid = begin_delivery_attempt(selected, "receiver", "claude_code", "h", 1)
    gate = threading.Barrier(2)
    errors: list[Exception] = []

    def confirm() -> None:
        try:
            gate.wait()
            settle_delivery_attempt(attempt_uuid, MessageStatus.DELIVERED, "confirmed")
        except Exception as exc:  # pragma: no cover - asserted empty
            errors.append(exc)

    def acknowledge() -> None:
        try:
            gate.wait()
            ack_messages("receiver", high.id)
        except Exception as exc:  # pragma: no cover - asserted empty
            errors.append(exc)

    workers = [threading.Thread(target=confirm), threading.Thread(target=acknowledge)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(3)
    assert not errors and all(not worker.is_alive() for worker in workers)
    with wpq10_db() as db:
        assert low < high.id
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == high.id


def test_wpq10_digest_cursor_isolated_by_mailbox(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _mailbox(
            db,
            mailbox_id="mb_wpq10bb",
            session_name="other-session",
            terminal_id="other",
        )
        _terminal(db, "receiver")
        _terminal(db, "other")
        digest_id, high_water = _direct_digest(db)
    _confirm_family(wpq10_db, "inferred_cap", digest_id)
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == high_water
        assert db.get(MailboxModel, "mb_wpq10bb").consumed_through_id == 0


def test_wpq10_stale_only_publication_records_null_and_does_not_advance(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db)
        _terminal(db, "old")
        _terminal(db, "next")
        _message(db, "old", status=MessageStatus.PENDING)
    publication = publish_supervisor_incarnation(claim_mailbox("wpq10-session"), "next")
    digest_id = publication["digest_message_id"]
    with wpq10_db() as db:
        event = db.query(InboxMessageTraceEventModel).filter_by(
            message_id=digest_id, kind="digest_high_water"
        ).one()
        assert event.payload == {"high_water": None}
    _confirm_family(wpq10_db, "normal", digest_id)
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == 0


def test_wpq10_digest_without_high_water_event_is_cursor_noop(wpq10_db):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        digest = _message(
            db,
            "receiver",
            status=MessageStatus.PENDING,
            kind=OrchestrationType.MAILBOX_DIGEST,
        )
        digest_id = int(digest.id)
    _confirm_family(wpq10_db, "normal", digest_id)
    with wpq10_db() as db:
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == 0


def _configure_prepared_send(monkeypatch, state: str, events: list[str]):
    backend = MagicMock(supports_identity_readback=False)
    backend.read_native_identity.return_value = SimpleNamespace(verdict="match")
    backend.send_keys.side_effect = lambda *_a, **_k: events.append("paste_submit")
    provider = MagicMock()
    provider.composer_stash_keys = ["C-s"]
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.3
    provider.read_composer_draft_state.side_effect = (
        lambda **_kwargs: events.append("classify") or state
    )
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _terminal: {
            "tmux_session": "session",
            "tmux_window": "window",
            "provider": "claude_code",
        },
    )
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        terminal_service.provider_manager, "get_provider", lambda _terminal: provider
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "get_status",
        lambda _terminal: TerminalStatus.IDLE,
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "notify_input_sent",
        lambda _terminal: events.append("notify"),
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "clear_rolling_buffer",
        lambda _terminal: events.append("clear"),
    )
    observation = BoundaryObservation(
        "wpq10", TerminalStatus.IDLE, 1, 1, 1, 1, 1
    )
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "mark_injection_completed",
        lambda _terminal: observation,
    )
    monkeypatch.setattr(terminal_service, "update_last_active", lambda _terminal: None)
    original_apply = terminal_service.apply_prepared_native_stash
    monkeypatch.setattr(
        terminal_service,
        "apply_prepared_native_stash",
        lambda prepared: events.append("stash") or original_apply(prepared),
    )
    terminal_service._memory_injected_terminals.discard("receiver")
    return backend, provider


@pytest.mark.parametrize("state", ["nonempty", "unresolved", "dialog"])
def test_wpq10_nonempty_or_unresolved_native_composer_has_zero_mutation(
    monkeypatch, state
):
    events: list[str] = []
    backend, _provider = _configure_prepared_send(monkeypatch, state, events)
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input(
            "receiver", "payload", defer_on_dialog=True
        )
    assert events == ["classify"]
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()
    assert "receiver" not in terminal_service._memory_injected_terminals


def test_wpq10_empty_native_composer_preserves_send_sequence(monkeypatch):
    events: list[str] = []
    backend, _provider = _configure_prepared_send(monkeypatch, "empty", events)
    terminal_service.send_prepared_input("receiver", "payload", defer_on_dialog=True)
    assert events == ["classify", "notify", "clear", "stash", "paste_submit"]
    backend.send_keys.assert_called_once()
    backend.send_special_key.assert_not_called()
    assert "receiver" in terminal_service._memory_injected_terminals
    terminal_service._memory_injected_terminals.discard("receiver")


def test_wpq10_changed_authority_snapshots_defer_before_mutation(monkeypatch):
    events: list[str] = []
    backend, _provider = _configure_prepared_send(monkeypatch, "empty", events)
    real_provider = ClaudeCodeProvider("receiver", "session", "window")
    backend.get_history.side_effect = [
        "❯ \n────────────────────",
        "❯ human text\n────────────────────",
    ]
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        terminal_service.provider_manager,
        "get_provider",
        lambda _terminal: real_provider,
    )
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input("receiver", "payload")
    assert events == []
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()
    assert "receiver" not in terminal_service._memory_injected_terminals


def test_wpq10_authority_capture_failure_defers_before_mutation(monkeypatch):
    events: list[str] = []
    backend, _provider = _configure_prepared_send(monkeypatch, "empty", events)
    real_provider = ClaudeCodeProvider("receiver", "session", "window")
    backend.get_history.side_effect = RuntimeError("capture unavailable")
    monkeypatch.setattr(
        "cli_agent_orchestrator.backends.registry.get_backend", lambda: backend
    )
    monkeypatch.setattr(
        terminal_service.provider_manager,
        "get_provider",
        lambda _terminal: real_provider,
    )
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input("receiver", "payload")
    assert events == []
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()
    assert "receiver" not in terminal_service._memory_injected_terminals


def test_wpq10_waiting_gate_precedes_composer_authority(monkeypatch):
    backend = MagicMock()
    provider_lookup = MagicMock()
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _terminal: {"tmux_session": "session", "tmux_window": "window"},
    )
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(terminal_service.provider_manager, "get_provider", provider_lookup)
    monkeypatch.setattr(
        terminal_service.status_monitor,
        "get_status",
        lambda _terminal: TerminalStatus.WAITING_USER_ANSWER,
    )
    with pytest.raises(terminal_service.TerminalInputBlockedError):
        terminal_service.send_prepared_input("receiver", "payload")
    provider_lookup.assert_not_called()
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()


def test_wpq10_deferred_composer_attempt_stays_pending(wpq10_db, monkeypatch):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        row = _message(db, "receiver")
    selected = [item for item in get_pending_messages("receiver") if item.id == row.id]
    attempt_uuid = begin_delivery_attempt(selected, "receiver", "claude_code", "h", 1)
    events: list[str] = []
    _configure_prepared_send(monkeypatch, "nonempty", events)
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input("receiver", "payload")
    assert settle_delivery_attempt(
        attempt_uuid,
        MessageStatus.PENDING,
        "deferred",
        reason="delivery_deferred",
    )
    with wpq10_db() as db:
        attempt = db.get(InboxDeliveryAttemptModel, attempt_uuid)
        assert db.get(InboxModel, row.id).status == MessageStatus.PENDING.value
        assert (attempt.outcome, attempt.reason) == ("deferred", "delivery_deferred")


def test_wpq10_delivery_engine_uses_native_composer_defer_path(
    wpq10_db, monkeypatch
):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        row = _message(db, "receiver")
    events: list[str] = []
    backend, provider = _configure_prepared_send(monkeypatch, "nonempty", events)
    observation = BoundaryObservation(
        "wpq10-delivery", TerminalStatus.IDLE, 1, 1, 1, 1, 1
    )
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = 1
    monitor.get_status_gen.return_value = 1
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        {"result_status": "idle", "law_signal": {"class": "chrome"}},
    )
    monkeypatch.setattr(inbox_service_module, "status_monitor", monitor)
    monkeypatch.setattr(
        inbox_service_module.provider_manager,
        "get_provider",
        lambda _terminal: provider,
    )
    monkeypatch.setattr(
        inbox_service_module,
        "resolve_session_transcript",
        lambda *_args, **_kwargs: TranscriptResolution(
            Path("/trace"),
            "binding",
            TranscriptLiveReference(Path("/trace"), 1, 0),
        ),
    )
    monkeypatch.setattr(
        terminal_service,
        "prepare_input",
        lambda _target, value, _kind, **_kwargs: value,
    )
    monkeypatch.setattr(InboxService, "_commit_watchdog_ops", MagicMock())

    InboxService().deliver_pending("receiver")

    assert events == ["classify"]
    backend.send_keys.assert_not_called()
    backend.send_special_key.assert_not_called()
    with wpq10_db() as db:
        assert db.get(InboxModel, row.id).status == MessageStatus.PENDING.value
        attempt = (
            db.query(InboxDeliveryAttemptModel)
            .join(
                InboxDeliveryAttemptMemberModel,
                InboxDeliveryAttemptMemberModel.attempt_uuid
                == InboxDeliveryAttemptModel.attempt_uuid,
            )
            .filter(InboxDeliveryAttemptMemberModel.message_id == row.id)
            .one()
        )
        assert (attempt.outcome, attempt.reason) == (
            "deferred",
            "delivery_deferred",
        )


def test_wpq10_digest_defer_then_empty_delivery_advances_once(wpq10_db, monkeypatch):
    with wpq10_db.begin() as db:
        _mailbox(db, terminal_id="receiver")
        _terminal(db, "receiver")
        digest_id, high_water = _direct_digest(db)
    selected = [item for item in get_pending_messages("receiver") if item.id == digest_id]
    first_attempt = begin_delivery_attempt(selected, "receiver", "claude_code", "h1", 1)
    events: list[str] = []
    _backend, provider = _configure_prepared_send(monkeypatch, "nonempty", events)
    with pytest.raises(DeliveryDeferredError):
        terminal_service.send_prepared_input("receiver", "digest")
    assert settle_delivery_attempt(
        first_attempt,
        MessageStatus.PENDING,
        "deferred",
        reason="delivery_deferred",
    )
    with wpq10_db() as db:
        assert db.get(InboxModel, digest_id).status == MessageStatus.PENDING.value
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == 0

    provider.read_composer_draft_state.side_effect = lambda **_kwargs: "empty"
    selected = [item for item in get_pending_messages("receiver") if item.id == digest_id]
    second_attempt = begin_delivery_attempt(selected, "receiver", "claude_code", "h2", 1)
    terminal_service.send_prepared_input("receiver", "digest")
    assert settle_delivery_attempt(
        second_attempt, MessageStatus.DELIVERED, "confirmed"
    )
    with wpq10_db() as db:
        assert db.get(InboxModel, digest_id).status == MessageStatus.DELIVERED.value
        assert db.get(MailboxModel, "mb_wpq10aa").consumed_through_id == high_water
        assert (
            db.query(InboxDeliveryAttemptMemberModel)
            .filter_by(message_id=digest_id)
            .count()
            == 2
        )

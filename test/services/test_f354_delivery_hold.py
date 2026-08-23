"""F354: inbox delivery must hold while a blocking dialog is on the terminal.

Defect 2 of the F354 sample (auto-responder no-fire + delivery retries into a
blocked TUI): while a terminal is dialog-blocked (WAITING_USER_ANSWER admission
status, or the auto-responder's blocking-dialog gate), ``deliver_pending`` must
suppress the attempt — no paste, no burned attempt rows — and leave the message
cleanly PENDING. Once the dialog clears, the next wake delivers normally.

These tests run the production delivery seam (real sqlite attempt tables, not
the legacy test seam), mirroring test_wpm2_delivery_soundness.py.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    create_inbox_message,
    get_message_trace,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import ProviderCapabilities
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation


@pytest.fixture
def f354_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f354.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    database.create_terminal("caller", "s", "caller", "claude_code")
    database.create_terminal("sender", "s", "sender", "claude_code")
    database.create_terminal("receiver", "s", "receiver", "grok_cli", caller_id="caller")
    yield sessions
    engine.dispose()


def _observation(status, epoch="epoch", seq=1):
    return BoundaryObservation(
        epoch,
        status,
        1,
        1,
        seq,
        seq if status == TerminalStatus.PROCESSING else None,
        seq if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED} else None,
    )


def _run_delivery(boundary_status, probe_status, gate_value):
    """One deliver_pending pass through the production seam; returns the paste mock."""
    provider = MagicMock()
    provider.capabilities = ProviderCapabilities(accepts_input_while_processing=True)
    submitted = _observation(TerminalStatus.PROCESSING, seq=2)

    def send(*_args, **kwargs):
        kwargs["on_submitted"](submitted)
        return submitted

    with (
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            return_value="payload",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ) as paste,
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("absent", {"kind": "transcript_absent"}),
        ),
        patch("cli_agent_orchestrator.services.inbox_service.EAGER_INBOX_DELIVERY", True),
        patch(
            "cli_agent_orchestrator.services.auto_responder.auto_responder.waiting_gate",
            return_value=gate_value,
        ),
    ):
        monitor.get_boundary_observation.return_value = _observation(boundary_status)
        monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
        monitor.probe_screen_status.return_value = (
            probe_status,
            {"result_status": probe_status.value},
        )
        InboxService().deliver_pending("receiver")
    return paste


def _make_pending_message():
    message = create_inbox_message("sender", "receiver", "f354 payload")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    return message


def test_waiting_user_answer_holds_delivery_even_on_eager_path(f354_db):
    """Dialog-blocked admission status: no paste, no attempt row, stays PENDING."""
    message = _make_pending_message()

    paste = _run_delivery(
        TerminalStatus.WAITING_USER_ANSWER, TerminalStatus.WAITING_USER_ANSWER, None
    )

    paste.assert_not_called()
    trace = get_message_trace(message.id)
    assert trace["attempts"] == []
    assert trace["message"]["status"] == MessageStatus.PENDING.value


def test_responder_dialog_gate_holds_eager_processing_delivery(f354_db):
    """Status flapped to PROCESSING mid-dialog: the responder gate still holds."""
    message = _make_pending_message()

    paste = _run_delivery(
        TerminalStatus.PROCESSING, TerminalStatus.PROCESSING, "unknown_dialog"
    )

    paste.assert_not_called()
    trace = get_message_trace(message.id)
    assert trace["attempts"] == []
    assert trace["message"]["status"] == MessageStatus.PENDING.value


def test_delivery_resumes_after_dialog_clears(f354_db):
    """Held while gated, then delivers exactly once once the gate clears."""
    message = _make_pending_message()

    held = _run_delivery(
        TerminalStatus.PROCESSING, TerminalStatus.PROCESSING, "unknown_dialog"
    )
    held.assert_not_called()

    resumed = _run_delivery(TerminalStatus.IDLE, TerminalStatus.IDLE, None)

    resumed.assert_called_once()
    trace = get_message_trace(message.id)
    assert len(trace["attempts"]) == 1

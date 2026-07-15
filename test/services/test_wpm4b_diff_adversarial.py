"""Promoted empirical probes for the WPM4-B Wave 1 diff gate."""

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
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
    recover_wpm2_stale_attempt,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.draft_guard import DeliveryDeferredError
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import (
    TranscriptLiveReference,
    TranscriptResolution,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation
from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError


@pytest.fixture  # type: ignore[untyped-decorator]
def scratch_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'wpm4b-diff.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _observation(seq: int, *, non_ready: int | None, ready: int | None) -> BoundaryObservation:
    return BoundaryObservation("wpm4b-epoch", TerminalStatus.IDLE, 3, 1, seq, non_ready, ready)


def _binding() -> TranscriptResolution:
    path = Path("/trace")
    return TranscriptResolution(path, "binding", TranscriptLiveReference(path, 1, 20))


def _assert_corrective_pre_paste_retries_untagged(scratch_db: Any, error: Exception) -> None:
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "callback")
    initial = _observation(0, non_ready=None, ready=0)
    submitted = _observation(1, non_ready=None, ready=1)
    boundary = _observation(4, non_ready=2, ready=4)
    send_calls: list[str] = []

    def send(_terminal_id: str, wire: str, **kwargs: Any) -> BoundaryObservation:
        send_calls.append(wire)
        if len(send_calls) == 2:
            # The corrective opened, but the no-submit error prevents any paste.
            raise error
        callback = kwargs.get("on_submitted")
        if callback is not None:
            callback(submitted)
        return submitted

    confirms = iter(
        [
            (
                "ambiguous",
                {
                    "path": "/trace",
                    "inode": 1,
                    "size": 20,
                    "resolution_kind": "binding",
                },
            ),
            ("hit", {"kind": "transcript_user_turn"}),
        ]
    )
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
            return_value="callback",
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            side_effect=lambda *_a, **_k: next(confirms),
        ),
        patch.object(service, "_commit_watchdog_ops"),
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
    ):
        observations = iter([initial, boundary, boundary])
        monitor.get_boundary_observation.side_effect = lambda _terminal_id: next(
            observations, boundary
        )
        monitor.get_status.return_value = TerminalStatus.IDLE
        monitor.get_input_gen.return_value = 1
        monitor.get_status_gen.return_value = 3
        service.deliver_pending("receiver")
        service.deliver_pending("receiver")
        service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert trace is not None
    assert len(send_calls) == 3
    assert send_calls[0] == "callback"
    assert send_calls[1].startswith("[redelivery of attempt ")
    assert send_calls[2] == "callback"
    assert trace["attempts"][-1]["prior_attempt_uuid"] is None


def test_corrective_pre_paste_deferral_retries_untagged(scratch_db: Any) -> None:
    _assert_corrective_pre_paste_retries_untagged(
        scratch_db, DeliveryDeferredError("pre-paste defer")
    )


def test_corrective_pre_paste_input_blocked_retries_untagged(scratch_db: Any) -> None:
    _assert_corrective_pre_paste_retries_untagged(
        scratch_db, TerminalInputBlockedError("pre-paste blocked")
    )


def test_wpm2_notice_failure_rolls_back_attempt_and_member(scratch_db: Any) -> None:
    database.create_terminal("sender", "s", "sender", "codex")
    database.create_terminal("receiver", "s", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "payload")
    attempt_uuid = begin_delivery_attempt([message], "receiver", "claude_code", "wire-hash", 7)
    database.delete_terminal_and_warm_intent("receiver", preserve_warm_intent=False)
    engine = scratch_db.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_wpm2_notice BEFORE INSERT ON inbox "
                "WHEN NEW.message LIKE 'p5-orphan %' "
                "BEGIN SELECT RAISE(ABORT, 'notice rejected'); END"
            )
        )

    with pytest.raises(Exception, match="notice rejected"):
        recover_wpm2_stale_attempt(
            attempt_uuid,
            [message.id],
            MessageStatus.DELIVERY_FAILED,
            "failed",
            "receiver_gone",
            {},
        )
    with scratch_db() as db:
        attempt = db.get(InboxDeliveryAttemptModel, attempt_uuid)
        member = db.get(InboxModel, message.id)
        assert attempt.settled_at is None
        assert attempt.outcome is None
        assert member.status == MessageStatus.DELIVERING.value
        assert member.failure_reason is None

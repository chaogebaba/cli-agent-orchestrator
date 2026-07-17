"""Discriminating acceptance probes for frozen Wave 4 D1-D6."""

from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    NoticeInsertOutcome,
    create_inbox_message,
    create_terminal,
    get_message_trace,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import claude_code, codex, grok_cli
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services import inbox_service as inbox_service_module
from cli_agent_orchestrator.services import mailbox_service as mailbox_service_module
from cli_agent_orchestrator.services import terminal_service as terminal_service_module
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import (
    confirm_delivery,
    normalized_confirmation_fingerprint,
    transcript_lookup,
    wire_hash,
)
from cli_agent_orchestrator.services.pane_identity_service import PaneIdentityMismatchError
from cli_agent_orchestrator.services.replay_policy import (
    CAP_TABLE,
    AuthorizationFacts,
    ObservedFact,
    ReplayPolicy,
    run_post_auth_engine,
)
from cli_agent_orchestrator.services.status_monitor import (
    BoundaryObservation,
    ScreenProbeFrameSource,
    StatusMonitor,
)


def _grok() -> GrokCliProvider:
    return object.__new__(GrokCliProvider)


def _claude() -> ClaudeCodeProvider:
    return ClaudeCodeProvider("claude", "session", "window")


def _codex() -> CodexProvider:
    return CodexProvider("codex", "session", "window")


def _probe_meta(
    status: TerminalStatus,
    provider_signal: str | None = None,
    frame_source: str | None = None,
) -> dict:
    signal_class = (
        "chrome" if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED} else "progress"
    )
    return {
        "probed_at": "2026-07-16T00:00:00Z",
        "geometry": {"columns": 220, "rows": 50},
        "frame_rows_hash": "0" * 64,
        "frame_source": frame_source
        or (
            "fresh_capture"
            if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
            else "incremental"
        ),
        "result_status": status.value,
        "law_signal": {
            "class": signal_class,
            "provider_signal": provider_signal,
            "row_index": 0,
        },
    }


@pytest.fixture
def wave4_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave4.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    create_terminal("sender", "session", "sender", "codex")
    create_terminal("receiver", "session", "receiver", "grok_cli")
    yield sessions
    engine.dispose()


def test_probe_01_grok_above_fold_spinner_defers_without_attempt(wave4_db):
    message = create_inbox_message("sender", "receiver", "do the queued task")
    screen = [
        "Waiting for response…",
        *[f"flow row {index}" for index in range(13)],
        "❯",
        "Grok 4.5 · always-approve · ctrl+o transcript",
    ]
    status = _grok().get_status_from_screen(screen)
    assert status == TerminalStatus.PROCESSING
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
    monitor.probe_screen_status.return_value = (
        status,
        _probe_meta(status, "PROCESSING_PATTERN"),
    )
    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as paste,
    ):
        InboxService().deliver_pending("receiver")
    assert paste.call_count == 0
    assert get_message_trace(message.id)["attempts"] == []

    ready_meta = _probe_meta(TerminalStatus.IDLE, "IDLE_FOOTER_PATTERN")
    monitor.probe_screen_status.return_value = (TerminalStatus.IDLE, ready_meta)

    def send(_terminal_id, _wire, **kwargs):
        kwargs["on_submitted"](observation)
        return observation

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
    ):
        InboxService().deliver_pending("receiver")
    attempt = get_message_trace(message.id)["attempts"][0]
    assert attempt["evidence"]["screen_probe"] == ready_meta


def test_probe_02_grok_newer_completion_clears_stale_spinner():
    screen = [
        "Waiting for response…",
        "response text",
        "Turn completed in 1.5s.",
        "❯",
        "Grok 4.5 · always-approve · ctrl+o transcript",
    ]
    assert _grok().get_status_from_screen(screen) == TerminalStatus.COMPLETED


def test_probe_03_claude_full_screen_spinner_beats_older_response_and_chrome():
    screen = [
        "● Old response",
        "✻ Cultivating…",
        *[f"flow row {index}" for index in range(26)],
        "─" * 60,
        "❯",
        "─" * 60,
    ]
    assert _claude().get_status_from_screen(screen) == TerminalStatus.PROCESSING


def test_probe_04_codex_existing_fixture_corpus_is_unchanged():
    fixture_root = Path(__file__).parents[1] / "fixtures" / "codex_dialogs"
    corpus = {
        path.name: path.read_text(encoding="utf-8").splitlines()
        for path in sorted(fixture_root.glob("*.ansi.txt"))
    }
    corpus.update(
        {
            "synthetic:completed": ["› Fix", "• Fixed", "", "› ", "? for shortcuts"],
            "synthetic:working": [
                "› Fix",
                "• Working (5s • esc to interrupt)",
                "› ",
                "? for shortcuts",
            ],
            "synthetic:above_tail": [
                "› Fix",
                "• Working (5s • esc to interrupt)",
                *([""] * 26),
                "› Summarize recent commits",
                "? for shortcuts",
            ],
            "synthetic:blank": ["", "", ""],
        }
    )
    progress_dialog = list(corpus["model-picker.ansi.txt"])
    footer_index = next(
        index
        for index in range(len(progress_dialog) - 1, -1, -1)
        if codex.strip_terminal_escapes(progress_dialog[index]).strip()
    )
    progress_dialog.insert(footer_index, "• Working (3s • esc to interrupt)")
    corpus["synthetic:progress_plus_dialog"] = progress_dialog

    parent_results = {
        "command-approval.ansi.txt": "waiting_user_answer",
        "composer-draft.ansi.txt": "idle",
        "experimental-checkboxes.ansi.txt": "waiting_user_answer",
        "hooks-browser.ansi.txt": "waiting_user_answer",
        "idle.ansi.txt": "idle",
        "keymap-browser.ansi.txt": "waiting_user_answer",
        "memories-enable.ansi.txt": "waiting_user_answer",
        "model-picker.ansi.txt": "waiting_user_answer",
        "permissions-picker.ansi.txt": "waiting_user_answer",
        "quoted-trust-stall.ansi.txt": "processing",
        "skills-menu.ansi.txt": "waiting_user_answer",
        "synthetic:above_tail": "processing",
        "synthetic:blank": "processing",
        "synthetic:completed": "completed",
        "synthetic:progress_plus_dialog": "processing",
        "synthetic:working": "processing",
        "theme-picker.ansi.txt": "waiting_user_answer",
        "trust.ansi.txt": "waiting_user_answer",
        "usage-picker-no-reset.ansi.txt": "waiting_user_answer",
        "working.ansi.txt": "processing",
    }
    assert len(corpus) == 20
    assert {
        name: _codex().get_status_from_screen(rows).value for name, rows in corpus.items()
    } == parent_results


@pytest.mark.parametrize(
    ("provider", "row"),
    [
        (_grok, "⠴ Thinking Turn completed in 1.5s."),
        (_claude, "✻ Cultivating… then ✻ Baked for 1s"),
        (_codex, "• Working (0s • esc to interrupt)"),
    ],
)
def test_probe_05_equal_row_progress_wins(provider, row):
    assert provider().get_status_from_screen([row]) == TerminalStatus.PROCESSING


def test_probe_06_claude_background_wait_and_permission_precedence():
    assert (
        _claude().get_status_from_screen(
            ["● done", "✻ Waiting for 1 dynamic workflow to finish", "─" * 60, "❯", "─" * 60]
        )
        == TerminalStatus.PROCESSING
    )
    assert (
        _claude().get_status_from_screen(
            [
                "✻ Waiting for 1 dynamic workflow to finish",
                "❯ 1. Yes",
                "  2. No",
                "↑/↓ to navigate · Enter to select",
            ]
        )
        == TerminalStatus.WAITING_USER_ANSWER
    )


def test_probe_07_grok_historical_error_is_filtered_by_newer_completion():
    assert (
        _grok().get_status_from_screen(
            ["Error: historical", "Turn completed in 1.5s.", "❯", "always-approve"]
        )
        == TerminalStatus.COMPLETED
    )


def test_probe_08_typed_screen_probe_uses_one_frame_and_named_source_signal():
    monitor = StatusMonitor()
    backend = MagicMock()
    rows = ["Waiting for response…", "❯", "always-approve"]
    monitor._screens["receiver"] = (  # noqa: SLF001 - acceptance probes the lock-owned frame
        SimpleNamespace(display=rows, columns=220, lines=50),
        object(),
    )
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_grok(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
    ):
        status, meta = monitor.probe_screen_status("receiver")
    assert backend.mock_calls == []
    assert status == TerminalStatus.PROCESSING
    assert set(meta) == {
        "probed_at",
        "geometry",
        "frame_rows_hash",
        "frame_source",
        "result_status",
        "law_signal",
    }
    assert set(get_args(ScreenProbeFrameSource)) == {"incremental", "fresh_capture"}
    assert meta["frame_source"] == "incremental"
    assert meta["geometry"] == {"columns": 220, "rows": 50}
    assert len(meta["frame_rows_hash"]) == 64
    signal = meta["law_signal"]["provider_signal"]
    assert signal == "PROCESSING_PATTERN" and hasattr(grok_cli, signal)
    assert meta["result_status"] != TerminalStatus.RENDER_UNCERTAIN.value
    json.dumps(meta)

    codex_rows = ["› task", "assistant: done", "› ", "? for shortcuts"]
    monitor._screens["receiver"] = (  # noqa: SLF001 - same locked frame twice
        SimpleNamespace(display=codex_rows, columns=220, lines=50),
        object(),
    )
    backend.get_native_status.side_effect = [None, TerminalStatus.PROCESSING]
    backend.get_pane_size.return_value = (220, 50)
    backend.capture_viewport.return_value = "\n".join(codex_rows)
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_codex(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"tmux_session": "session", "tmux_window": "receiver"},
        ),
    ):
        first_status, first_meta = monitor.probe_screen_status("receiver")
    assert backend.get_native_status.call_count == 0
    backend.capture_viewport.assert_called_once_with("session", "receiver")
    assert first_status == TerminalStatus.COMPLETED
    assert first_meta["frame_source"] == "fresh_capture"
    assert set(first_meta) == set(meta)


@pytest.mark.parametrize(
    "reason",
    ["mismatch", "missing_env", "read_error", "pane_cardinality", "incarnation_changed"],
)
def test_wpq1_identity_failure_fences_ready_capture_and_projects_reason(reason):
    monitor = StatusMonitor()
    rows = ["› task", "assistant: done", "› ", "? for shortcuts"]
    screen = SimpleNamespace(display=rows, columns=220, lines=50)
    monitor._screens["receiver"] = (screen, object())  # noqa: SLF001
    backend = MagicMock(supports_identity_readback=True)
    backend.read_pane_identity.return_value = (
        PaneIdentityReadResult(identity="other")
        if reason == "mismatch"
        else PaneIdentityReadResult(reason=reason)
    )

    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_codex(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"tmux_session": "session", "tmux_window": "receiver"},
        ),
    ):
        status, meta = monitor.probe_screen_status("receiver")

    assert status == TerminalStatus.UNKNOWN
    assert meta["identity_proof_failure"] == reason
    assert meta["result_status"] == "unknown"
    backend.read_pane_identity.assert_called_once_with("session", "receiver")
    backend.capture_viewport.assert_not_called()
    assert screen.display == rows


@pytest.mark.parametrize(
    "reason",
    ["mismatch", "missing_env", "read_error", "pane_cardinality", "incarnation_changed"],
)
def test_wpq1_send_sink_identity_failure_precedes_all_mutation(reason):
    backend = MagicMock(supports_identity_readback=True)
    backend.read_pane_identity.return_value = (
        PaneIdentityReadResult(identity="other")
        if reason == "mismatch"
        else PaneIdentityReadResult(reason=reason)
    )
    with (
        patch.object(
            terminal_service_module,
            "get_terminal_metadata",
            return_value={"tmux_session": "session", "tmux_window": "receiver"},
        ),
        patch.object(terminal_service_module, "get_backend", return_value=backend),
        patch.object(terminal_service_module, "status_monitor") as monitor,
        patch.object(terminal_service_module, "provider_manager") as providers,
    ):
        with pytest.raises(PaneIdentityMismatchError) as caught:
            terminal_service_module.send_prepared_input("receiver", "payload")

    assert caught.value.reason == reason
    backend.read_pane_identity.assert_called_once_with("session", "receiver")
    backend.send_keys.assert_not_called()
    monitor.notify_input_sent.assert_not_called()
    monitor.clear_rolling_buffer.assert_not_called()
    providers.get_provider.assert_not_called()


def test_wpq1_three_preopen_identity_failures_notice_once_without_attempt(
    wave4_db,
):
    with wave4_db.begin() as db:
        db.get(database.TerminalModel, "receiver").caller_id = "sender"
    message = create_inbox_message("sender", "receiver", "identity fenced")
    meta = _probe_meta(TerminalStatus.UNKNOWN)
    meta["identity_proof_failure"] = "mismatch"
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = BoundaryObservation(
        "epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1
    )
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
    monitor.probe_screen_status.return_value = (TerminalStatus.UNKNOWN, meta)
    service = InboxService()

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as paste,
    ):
        for _ in range(4):
            service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == "pending"
    assert trace["attempts"] == []
    paste.assert_not_called()
    notices = database.get_inbox_messages("sender")
    authority = [item for item in notices if item.message.startswith("[identity-authority]")]
    assert len(authority) == 1
    assert "(mismatch, x3)" in authority[0].message


def _logical_identity_message(wave4_db, enqueue_generation):
    with wave4_db.begin() as db:
        db.add(
            database.MailboxModel(
                id="mb_identity",
                session_name="session",
                role="supervisor",
                current_terminal_id="receiver",
                generation=7,
                consumed_through_id=0,
            )
        )
        db.add(
            database.MailboxIncarnationModel(
                mailbox_id="mb_identity",
                generation=7,
                terminal_id="receiver",
            )
        )
        row = database.InboxModel(
            sender_id="sender",
            receiver_id="receiver",
            logical_receiver_id="mb_identity",
            enqueue_generation=enqueue_generation,
            message="logical identity fenced",
            orchestration_type="send_message",
            status="pending",
        )
        db.add(row)
        db.flush()
        return row.id


def _identity_monitor(reason="mismatch"):
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = BoundaryObservation(
        "epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1
    )
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
    meta = _probe_meta(TerminalStatus.UNKNOWN)
    meta["identity_proof_failure"] = reason
    monitor.probe_screen_status.return_value = (TerminalStatus.UNKNOWN, meta)
    return monitor


@pytest.mark.parametrize("enqueue_generation", [None, 7])
def test_wpq1_logical_proof1_token_equals_routed_generation_for_null_and_nonnull(
    wave4_db, monkeypatch, enqueue_generation
):
    message_id = _logical_identity_message(wave4_db, enqueue_generation)
    monkeypatch.setattr(mailbox_service_module, "SessionLocal", wave4_db)
    service = InboxService()
    monitor = _identity_monitor()

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
    ):
        service.deliver_pending("receiver")

    assert service._identity_authority[("receiver", "7")].count == 1
    assert get_message_trace(message_id)["attempts"] == []


def test_wpq1_logical_proof1_three_failures_use_one_routed_generation_episode(
    wave4_db, monkeypatch
):
    message_id = _logical_identity_message(wave4_db, None)
    with wave4_db.begin() as db:
        db.get(database.TerminalModel, "receiver").caller_id = "sender"
    monkeypatch.setattr(mailbox_service_module, "SessionLocal", wave4_db)
    service = InboxService()
    monitor = _identity_monitor("missing_env")

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
    ):
        for _ in range(3):
            service.deliver_pending("receiver")

    assert service._identity_authority[("receiver", "7")].count == 3
    assert get_message_trace(message_id)["attempts"] == []
    authority = [
        item
        for item in database.get_inbox_messages("sender")
        if item.message.startswith("[identity-authority]")
    ]
    assert len(authority) == 1


def test_wpq1_logical_proof2_three_real_settlements_keep_attempt_generation(wave4_db, monkeypatch):
    message_id = _logical_identity_message(wave4_db, 7)
    with wave4_db.begin() as db:
        db.get(database.TerminalModel, "receiver").caller_id = "sender"
    monkeypatch.setattr(mailbox_service_module, "SessionLocal", wave4_db)
    monitor = _identity_monitor()
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        _probe_meta(TerminalStatus.IDLE),
    )
    backend = MagicMock(supports_identity_readback=True)
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason="read_error")
    service = InboxService()

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch.object(terminal_service_module, "get_backend", return_value=backend),
    ):
        for _ in range(3):
            service.deliver_pending("receiver")

    trace = get_message_trace(message_id)
    assert len(trace["attempts"]) == 3
    assert {
        database.get_attempt_mailbox_authority(attempt["attempt_uuid"])["generation"]
        for attempt in trace["attempts"]
    } == {7}
    assert service._identity_authority[("receiver", "7")].count == 3
    backend.send_keys.assert_not_called()


def test_wpq1_three_postopen_identity_failures_stay_pending_and_bypass_caps(
    wave4_db,
):
    with wave4_db.begin() as db:
        db.get(database.TerminalModel, "receiver").caller_id = "sender"
    message = create_inbox_message("sender", "receiver", "identity fenced after open")
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = MagicMock()
    monitor.get_boundary_observation.return_value = observation
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.get_input_gen.return_value = monitor.get_status_gen.return_value = 1
    monitor.probe_screen_status.return_value = (
        TerminalStatus.IDLE,
        _probe_meta(TerminalStatus.IDLE),
    )
    service = InboxService()
    backend = MagicMock(supports_identity_readback=True)
    backend.read_pane_identity.return_value = PaneIdentityReadResult(reason="read_error")

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=MagicMock(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch.object(terminal_service_module, "get_backend", return_value=backend),
    ):
        for _ in range(4):
            service.deliver_pending("receiver")

    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == "pending"
    assert len(trace["attempts"]) == 4
    assert {item["reason"] for item in trace["attempts"]} == {"pane_identity_mismatch:read_error"}
    assert "attempt_cap" not in {item["reason"] for item in trace["attempts"]}
    assert all(item["outcome"] == "ambiguous" for item in trace["attempts"])
    assert all(item["evidence"]["identity_proof"] == "read_error" for item in trace["attempts"])
    backend.send_keys.assert_not_called()
    authority = [
        item
        for item in database.get_inbox_messages("sender")
        if item.message.startswith("[identity-authority]")
    ]
    assert len(authority) == 1
    assert "(read_error, x3)" in authority[0].message


@pytest.mark.parametrize(
    ("outcome", "notified", "calls_after_four"),
    [
        (NoticeInsertOutcome.INSERTED, True, 1),
        (NoticeInsertOutcome.FAILED_AFTER_COMMIT, True, 1),
        (NoticeInsertOutcome.UNCERTAIN_COMMIT, True, 1),
        (NoticeInsertOutcome.FAILED_BEFORE_COMMIT, False, 2),
    ],
)
def test_wpq1_identity_notice_outcome_state_machine(
    monkeypatch, outcome, notified, calls_after_four
):
    service = InboxService()
    batch = [SimpleNamespace(logical_receiver_id=None, enqueue_generation=None)]
    metadata = {"caller_id": "sender", "tmux_session": "session"}
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_terminal_metadata",
        lambda terminal_id: {"id": terminal_id},
    )
    insert = MagicMock(return_value=outcome)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.insert_identity_authority_notice",
        insert,
    )

    for _ in range(4):
        service._record_identity_authority_failure("receiver", batch, metadata, "read_error")

    assert insert.call_count == calls_after_four
    assert service._identity_authority[("receiver", "raw")].notified is notified


def test_wpq1_identity_episode_generation_change_and_teardown_evict(monkeypatch):
    service = InboxService()
    batch = [
        SimpleNamespace(
            logical_receiver_id="mb_authority",
            enqueue_generation=1,
        )
    ]
    service._record_identity_authority_failure("receiver", batch, {}, "missing_env")
    assert service._identity_authority[("receiver", "1")].count == 1

    batch[0].enqueue_generation = 2
    service._record_identity_authority_failure("receiver", batch, {}, "missing_env")
    assert ("receiver", "1") not in service._identity_authority
    assert service._identity_authority[("receiver", "2")].count == 1

    monkeypatch.setattr(inbox_service_module, "inbox_service", service)
    inbox_service_module.clear_terminal_delivery_state("receiver")
    assert service._identity_authority == {}


def test_probe_08_every_emitted_provider_signal_names_a_module_constant():
    expected = {
        grok_cli: {
            "WAITING_USER_ANSWER_PATTERN",
            "PROCESSING_PATTERN",
            "COMPLETION_PATTERN",
            "ERROR_PATTERN",
            "IDLE_PROMPT_PATTERN",
            "IDLE_FOOTER_PATTERN",
            "COMPOSER_PROMPT_PATTERN",
        },
        claude_code: {
            "WAITING_USER_ANSWER_PATTERN",
            "NEW_TUI_BOX_SPINNER_PATTERN",
            "PROCESSING_PATTERN",
            "BACKGROUND_WAIT_PATTERN",
            "GET_STATUS_COMPLETION_PATTERN",
            "EXTRACTION_RESPONSE_PATTERN",
            "NEW_TUI_BOX_PATTERN",
        },
        codex: {
            "TUI_PROGRESS_PATTERN",
            "TRUST_SELECTOR_PATTERN",
            "DIALOG_ACTION_FOOTER_PATTERN",
            "WAITING_PROMPT_PATTERN",
            "ERROR_PATTERN",
            "ASSISTANT_PREFIX_PATTERN",
            "IDLE_PROMPT_SCREEN_PATTERN",
            "SCREEN_FALLBACK_PROCESSING_PATTERN",
        },
    }
    providers = {
        grok_cli: GrokCliProvider,
        claude_code: ClaudeCodeProvider,
        codex: CodexProvider,
    }
    for module, provider in providers.items():
        tree = ast.parse(textwrap.dedent(inspect.getsource(provider.classify_screen)))
        emitted = {
            call.args[1].value
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "ScreenSignal"
            and len(call.args) >= 2
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
        }
        assert emitted == expected[module]
        assert all(hasattr(module, identifier) for identifier in emitted)


def test_probe_09_normalized_native_turn_confirms_banner_envelope_and_whitespace(tmp_path):
    core = "This payload is deliberately longer than forty eight characters for safe matching."
    wire = f"[redelivery of attempt deadbeef - prior delivery unconfirmed; ignore if already received]\n{core}"
    candidate = f"<user_query>\nThis payload is deliberately longer  than forty eight\ncharacters for safe matching.\n</user_query>"
    transcript = tmp_path / "trace.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": candidate}) + "\n")
    fingerprint = normalized_confirmation_fingerprint(wire)
    assert fingerprint is not None
    outcome, evidence = transcript_lookup(
        transcript,
        wire_hash(wire),
        normalized_payload_hash=fingerprint[0],
    )
    assert outcome == "hit"
    assert evidence["kind"] == "transcript_user_turn_normalized"


def test_probe_10_non_user_roles_and_tag_echo_never_confirm(tmp_path):
    payload = "A payload long enough to exceed the normalized confirmation safety floor by far."
    transcript = tmp_path / "trace.jsonl"
    records = [
        {"type": "assistant", "message": payload},
        {"type": "tool_result", "content": f"prefix {payload} suffix"},
        {"role": "assistant", "message": f"[redelivery of attempt deadbeef] {payload}"},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
    fingerprint = normalized_confirmation_fingerprint(payload)
    assert fingerprint is not None
    assert (
        transcript_lookup(transcript, wire_hash(payload), normalized_payload_hash=fingerprint[0])[0]
        == "absent"
    )


def test_probe_11_normalized_core_floor_is_strictly_48_characters():
    assert normalized_confirmation_fingerprint("x" * 47) is None
    assert normalized_confirmation_fingerprint("x" * 48) is not None


def test_probe_12_queue_operation_and_missing_oracle_remain_out_of_ladder(tmp_path):
    payload = "A queue payload long enough to exceed forty eight characters without ambiguity."
    transcript = tmp_path / "trace.jsonl"
    transcript.write_text(
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": payload}) + "\n"
    )
    fingerprint = normalized_confirmation_fingerprint(payload)
    assert fingerprint is not None
    assert (
        transcript_lookup(transcript, wire_hash(payload), normalized_payload_hash=fingerprint[0])[0]
        == "absent"
    )
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.resolve_session_transcript",
        return_value=None,
    ):
        assert confirm_delivery({}, wire_hash(payload), timeout=0)[0] == "unverified"


def test_probe_13_both_open_replay_kinds_use_one_provider_blind_engine():
    generic = AuthorizationFacts(
        ObservedFact(True, "prior-grok"), ObservedFact(False), False, True, False
    )
    binding = AuthorizationFacts(
        ObservedFact(True, "prior-claude"),
        ObservedFact(False),
        False,
        True,
        True,
        binding_authority=True,
        boundary_observation={"seq": 7},
    )
    assert (
        run_post_auth_engine(generic, ambiguous_count=1, exhausted_boundary_count=0).kind
        == "tagged_replay"
    )
    assert (
        run_post_auth_engine(binding, ambiguous_count=99, exhausted_boundary_count=1).kind
        == "inject"
    )
    assert "claude" not in inspect.getsource(ReplayPolicy).lower()
    assert "claude" not in inspect.getsource(run_post_auth_engine).lower()


def test_probe_14_cap_table_is_closed_and_per_kind():
    assert set(CAP_TABLE) == {"ordinary", "tagged_replay", "inject"}
    assert CAP_TABLE["ordinary"].counter == "ambiguous"
    assert CAP_TABLE["tagged_replay"].counter == "ambiguous"
    assert CAP_TABLE["inject"].counter == "exhausted_boundary"
    inject = AuthorizationFacts(
        ObservedFact(True, "prior"),
        ObservedFact(False),
        False,
        True,
        True,
        binding_authority=True,
        boundary_observation={"seq": 1},
    )
    assert (
        run_post_auth_engine(inject, ambiguous_count=3, exhausted_boundary_count=2).kind == "inject"
    )
    assert (
        run_post_auth_engine(inject, ambiguous_count=3, exhausted_boundary_count=3).kind == "stop"
    )


def test_probe_15_prior_hit_suppression_precedes_every_open_kind():
    facts = AuthorizationFacts(
        ObservedFact(True, "prior"),
        ObservedFact(True, "hit"),
        False,
        True,
        True,
        binding_authority=True,
        boundary_observation={"seq": 1},
    )
    assert ReplayPolicy.decide(facts).kind == "suppress"


def test_probe_16_frozen_drain_sql_fixture_ratios():
    root = Path(__file__).parents[3]
    command = [
        "sqlite3",
        "-cmd",
        ".read blueprints/wave4-drain-metric-fixture.sql",
        "-cmd",
        ".parameter init",
        "-cmd",
        ".parameter set :start '2026-07-15T00:00:00Z'",
        "-cmd",
        ".parameter set :end '2026-07-16T00:00:00Z'",
        ":memory:",
        ".read blueprints/wave4-drain-metric.sql",
    ]
    output = subprocess.run(command, cwd=root, check=True, text=True, capture_output=True).stdout
    assert "claude_code|2|1|1|1|0|0.5|0.5|0.5|0.0" in output
    assert "codex|1|0|0|0|0|0.0|0.0|0.0|0.0" in output
    assert "grok_cli|4|2|3|3|1|0.5|0.75|0.75|0.25" in output


def test_probe_17_lower_completion_beats_quoted_spinner_and_delivery_opens(wave4_db):
    screen = [
        "Report excerpt: • Working (0s • esc to interrupt)",
        "• Gate r5 complete",
        "› ",
        "? for shortcuts",
    ]
    classification = _codex().classify_screen(screen)
    create_terminal("codex_receiver", "session", "codex_receiver", "codex")
    message = create_inbox_message("sender", "codex_receiver", "deliver incident payload")
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = StatusMonitor()
    monitor._screens["codex_receiver"] = (  # noqa: SLF001 - acceptance frame
        SimpleNamespace(display=screen, columns=220, lines=50),
        object(),
    )
    monitor.get_boundary_observation = MagicMock(return_value=observation)
    monitor.get_status = MagicMock(return_value=TerminalStatus.IDLE)
    monitor.get_input_gen = MagicMock(return_value=1)
    monitor.get_status_gen = MagicMock(return_value=1)
    probe = MagicMock(wraps=monitor.probe_screen_status)
    monitor.probe_screen_status = probe
    backend = MagicMock()
    backend.get_pane_size.return_value = (220, 50)
    backend.capture_viewport.return_value = "\n".join(screen)

    def send(_terminal_id, _wire, **kwargs):
        kwargs["on_submitted"](observation)
        return observation

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=_codex(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
    ):
        InboxService().deliver_pending("codex_receiver")

    attempts = get_message_trace(message.id)["attempts"]
    assert len(attempts) == 1
    assert classification.status == TerminalStatus.COMPLETED
    assert classification.signal_class == "completion"
    assert classification.row_index == 1
    assert probe.call_count == 1
    backend.capture_viewport.assert_called_once_with("session", "codex_receiver")
    persisted_probe = attempts[0]["evidence"]["screen_probe"]
    assert persisted_probe["result_status"] == TerminalStatus.COMPLETED.value
    assert persisted_probe["frame_source"] == "fresh_capture"
    assert persisted_probe["law_signal"] == {
        "class": "completion",
        "provider_signal": "ASSISTANT_PREFIX_PATTERN",
        "row_index": 1,
    }


def test_livefix_p1_stale_incremental_ready_fresh_busy_defers_without_overwrite(wave4_db):
    message = create_inbox_message("sender", "receiver", "do not inject while busy")
    incremental_rows = ["❯", "Grok 4.5 · always-approve · ctrl+o transcript"]
    fresh_rows = [
        "⠹ Thinking… 1.1s",
        "Worked for 19s. 2 commands still running.",
        "❯",
        "Grok 4.5 · always-approve · ctrl+o transcript",
    ]
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = StatusMonitor()
    screen = SimpleNamespace(display=incremental_rows, columns=220, lines=50)
    monitor._screens["receiver"] = (screen, object())  # noqa: SLF001
    monitor.get_boundary_observation = MagicMock(return_value=observation)
    monitor.get_status = MagicMock(return_value=TerminalStatus.IDLE)
    monitor.get_input_gen = MagicMock(return_value=1)
    monitor.get_status_gen = MagicMock(return_value=1)
    backend = MagicMock()
    backend.get_pane_size.return_value = (220, 50)
    backend.capture_viewport.return_value = "\n".join(fresh_rows)

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=_grok(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as paste,
    ):
        InboxService().deliver_pending("receiver")

    backend.capture_viewport.assert_called_once_with("session", "receiver")
    assert paste.call_count == 0
    assert get_message_trace(message.id)["attempts"] == []
    assert screen.display == incremental_rows


@pytest.mark.parametrize(
    "capture_result", [RuntimeError("capture failed"), "", "unclassified viewport"]
)
def test_livefix_p2_fresh_capture_failure_fails_closed(wave4_db, capture_result):
    message = create_inbox_message("sender", "receiver", "defer on capture failure")
    incremental_rows = ["❯", "Grok 4.5 · always-approve · ctrl+o transcript"]
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = StatusMonitor()
    screen = SimpleNamespace(display=incremental_rows, columns=220, lines=50)
    monitor._screens["receiver"] = (screen, object())  # noqa: SLF001
    monitor.get_boundary_observation = MagicMock(return_value=observation)
    monitor.get_status = MagicMock(return_value=TerminalStatus.IDLE)
    backend = MagicMock()
    backend.get_pane_size.return_value = (220, 50)
    if isinstance(capture_result, Exception):
        backend.capture_viewport.side_effect = capture_result
    else:
        backend.capture_viewport.return_value = capture_result

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=_grok(),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input"
        ) as paste,
    ):
        InboxService().deliver_pending("receiver")

    assert backend.capture_viewport.call_count == 1
    assert paste.call_count == 0
    assert get_message_trace(message.id)["attempts"] == []
    assert screen.display == incremental_rows


def test_livefix_p3_grok_live_shape_completes_and_delivery_opens(wave4_db):
    root = Path(__file__).parents[3]
    screen = (
        (root / "tmp/orch/drain-2026-07-16-wave4/p17-pane-final.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    provider = _grok()
    classification = provider.classify_screen(screen)
    assert classification.status == TerminalStatus.COMPLETED
    assert classification.provider_signal == "COMPLETION_PATTERN"
    assert screen[classification.row_index or 0].strip() == "Worked for 1.6s."

    message = create_inbox_message("sender", "receiver", "deliver after grok completion")
    observation = BoundaryObservation("epoch", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    monitor = StatusMonitor()
    incremental_screen = SimpleNamespace(display=screen, columns=139, lines=61)
    monitor._screens["receiver"] = (incremental_screen, object())  # noqa: SLF001
    monitor.get_boundary_observation = MagicMock(return_value=observation)
    monitor.get_status = MagicMock(return_value=TerminalStatus.IDLE)
    monitor.get_input_gen = MagicMock(return_value=1)
    monitor.get_status_gen = MagicMock(return_value=1)
    backend = MagicMock()
    backend.get_pane_size.return_value = (139, 61)
    backend.capture_viewport.return_value = "\n".join(screen)

    def send(_terminal_id, _wire, **kwargs):
        kwargs["on_submitted"](observation)
        return observation

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=provider,
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.services.inbox_service.resolve_session_transcript",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.prepare_input",
            side_effect=lambda _terminal, value, _kind: value,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service.send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
    ):
        InboxService().deliver_pending("receiver")

    attempts = get_message_trace(message.id)["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["evidence"]["screen_probe"]["frame_source"] == "fresh_capture"
    assert attempts[0]["evidence"]["screen_probe"]["result_status"] == "completed"
    assert incremental_screen.display == screen


def test_livefix_p4_f16_live_pane_remains_processing():
    root = Path(__file__).parents[3]
    screen = (
        (root / "tmp/orch/drain-2026-07-16-wave4/f16-tmux-tail.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    classification = _grok().classify_screen(screen)
    assert classification.status == TerminalStatus.PROCESSING
    assert classification.provider_signal == "PROCESSING_PATTERN"
    assert not any(
        re.search(grok_cli.COMPLETION_PATTERN, row)
        for row in screen
        if "commands still running" in row
    )

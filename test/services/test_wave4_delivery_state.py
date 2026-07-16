"""Discriminating acceptance probes for frozen Wave 4 D1-D6."""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    create_inbox_message,
    create_terminal,
    get_message_trace,
)
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers import claude_code, codex, grok_cli
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services.inbox_service import InboxService
from cli_agent_orchestrator.services.message_trace_service import (
    confirm_delivery,
    normalized_confirmation_fingerprint,
    transcript_lookup,
    wire_hash,
)
from cli_agent_orchestrator.services.replay_policy import (
    CAP_TABLE,
    AuthorizationFacts,
    ObservedFact,
    ReplayPolicy,
    run_post_auth_engine,
)
from cli_agent_orchestrator.services.status_monitor import BoundaryObservation, StatusMonitor


def _grok() -> GrokCliProvider:
    return object.__new__(GrokCliProvider)


def _claude() -> ClaudeCodeProvider:
    return ClaudeCodeProvider("claude", "session", "window")


def _codex() -> CodexProvider:
    return CodexProvider("codex", "session", "window")


def _probe_meta(status: TerminalStatus, provider_signal: str | None = None) -> dict:
    signal_class = "chrome" if status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED} else "progress"
    return {
        "probed_at": "2026-07-16T00:00:00Z",
        "geometry": {"columns": 220, "rows": 50},
        "frame_rows_hash": "0" * 64,
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
    fixtures = [
        (["› Fix", "• Fixed", "› ", "? for shortcuts"], TerminalStatus.COMPLETED),
        (["› Fix", "• Working (5s • esc to interrupt)", "› "], TerminalStatus.PROCESSING),
        (["", "", ""], TerminalStatus.PROCESSING),
    ]
    assert [_codex().get_status_from_screen(rows) for rows, _ in fixtures] == [
        expected for _, expected in fixtures
    ]


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
    assert _claude().get_status_from_screen(
        ["● done", "✻ Waiting for 1 dynamic workflow to finish", "─" * 60, "❯", "─" * 60]
    ) == TerminalStatus.PROCESSING
    assert _claude().get_status_from_screen(
        [
            "✻ Waiting for 1 dynamic workflow to finish",
            "❯ 1. Yes",
            "  2. No",
            "↑/↓ to navigate · Enter to select",
        ]
    ) == TerminalStatus.WAITING_USER_ANSWER


def test_probe_07_grok_historical_error_is_filtered_by_newer_completion():
    assert _grok().get_status_from_screen(
        ["Error: historical", "Turn completed in 1.5s.", "❯", "always-approve"]
    ) == TerminalStatus.COMPLETED


def test_probe_08_typed_screen_probe_uses_one_frame_and_named_source_signal():
    monitor = StatusMonitor()
    rows = ["Waiting for response…", "❯", "always-approve"]
    monitor._screens["receiver"] = (  # noqa: SLF001 - acceptance probes the lock-owned frame
        SimpleNamespace(display=rows, columns=220, lines=50),
        object(),
    )
    with patch(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        return_value=_grok(),
    ):
        status, meta = monitor.probe_screen_status("receiver")
    assert status == TerminalStatus.PROCESSING
    assert set(meta) == {
        "probed_at", "geometry", "frame_rows_hash", "result_status", "law_signal"
    }
    assert meta["geometry"] == {"columns": 220, "rows": 50}
    assert len(meta["frame_rows_hash"]) == 64
    signal = meta["law_signal"]["provider_signal"]
    assert signal == "PROCESSING_PATTERN" and hasattr(grok_cli, signal)
    assert meta["result_status"] != TerminalStatus.RENDER_UNCERTAIN.value
    json.dumps(meta)


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
    assert transcript_lookup(
        transcript, wire_hash(payload), normalized_payload_hash=fingerprint[0]
    )[0] == "absent"


def test_probe_11_normalized_core_floor_is_strictly_48_characters():
    assert normalized_confirmation_fingerprint("x" * 47) is None
    assert normalized_confirmation_fingerprint("x" * 48) is not None


def test_probe_12_queue_operation_and_missing_oracle_remain_out_of_ladder(tmp_path):
    payload = "A queue payload long enough to exceed forty eight characters without ambiguity."
    transcript = tmp_path / "trace.jsonl"
    transcript.write_text(
        json.dumps({"type": "queue-operation", "operation": "enqueue", "content": payload})
        + "\n"
    )
    fingerprint = normalized_confirmation_fingerprint(payload)
    assert fingerprint is not None
    assert transcript_lookup(
        transcript, wire_hash(payload), normalized_payload_hash=fingerprint[0]
    )[0] == "absent"
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
    assert run_post_auth_engine(
        generic, ambiguous_count=1, exhausted_boundary_count=0
    ).kind == "tagged_replay"
    assert run_post_auth_engine(
        binding, ambiguous_count=99, exhausted_boundary_count=1
    ).kind == "inject"
    assert "claude" not in inspect.getsource(ReplayPolicy).lower()
    assert "claude" not in inspect.getsource(run_post_auth_engine).lower()


def test_probe_14_cap_table_is_closed_and_per_kind():
    assert set(CAP_TABLE) == {"ordinary", "tagged_replay", "inject"}
    assert CAP_TABLE["ordinary"].counter == "ambiguous"
    assert CAP_TABLE["tagged_replay"].counter == "ambiguous"
    assert CAP_TABLE["inject"].counter == "exhausted_boundary"
    inject = AuthorizationFacts(
        ObservedFact(True, "prior"), ObservedFact(False), False, True, True,
        binding_authority=True, boundary_observation={"seq": 1},
    )
    assert run_post_auth_engine(
        inject, ambiguous_count=3, exhausted_boundary_count=2
    ).kind == "inject"
    assert run_post_auth_engine(
        inject, ambiguous_count=3, exhausted_boundary_count=3
    ).kind == "stop"


def test_probe_15_prior_hit_suppression_precedes_every_open_kind():
    facts = AuthorizationFacts(
        ObservedFact(True, "prior"), ObservedFact(True, "hit"), False, True, True,
        binding_authority=True, boundary_observation={"seq": 1},
    )
    assert ReplayPolicy.decide(facts).kind == "suppress"


def test_probe_16_frozen_drain_sql_fixture_ratios():
    root = Path(__file__).parents[3]
    command = [
        "sqlite3",
        "-cmd", ".read blueprints/wave4-drain-metric-fixture.sql",
        "-cmd", ".parameter init",
        "-cmd", ".parameter set :start '2026-07-15T00:00:00Z'",
        "-cmd", ".parameter set :end '2026-07-16T00:00:00Z'",
        ":memory:", ".read blueprints/wave4-drain-metric.sql",
    ]
    output = subprocess.run(command, cwd=root, check=True, text=True, capture_output=True).stdout
    assert "claude_code|2|1|1|1|0|0.5|0.5|0.5|0.0" in output
    assert "codex|1|0|0|0|0|0.0|0.0|0.0|0.0" in output
    assert "grok_cli|4|2|3|3|1|0.5|0.75|0.75|0.25" in output


def test_probe_17_lower_completion_beats_quoted_codex_spinner_and_is_ready():
    screen = [
        "Report excerpt: • Working (0s • esc to interrupt)",
        "• Gate r5 complete",
        "› ",
        "? for shortcuts",
    ]
    classification = _codex().classify_screen(screen)
    assert classification.status == TerminalStatus.COMPLETED
    assert classification.signal_class == "completion"
    assert classification.row_index == 1

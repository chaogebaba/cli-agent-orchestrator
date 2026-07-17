import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxModel,
    TerminalModel,
    TranscriptBindingModel,
    recover_transcript_binding_if_current,
    settle_pending_receiver_gone_if_generation,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.providers.screen_classification import ScreenSignal
from cli_agent_orchestrator.services.message_trace_service import (
    _binding_staleness,
    clear_binding_staleness_state,
    observe_binding_absence,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


@pytest.fixture
def wpq2_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wpq2.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


def _provider(kind: str):
    provider_type = {
        "claude": ClaudeCodeProvider,
        "codex": CodexProvider,
        "grok": GrokCliProvider,
    }[kind]
    return provider_type("receiver", "session", "receiver")


@pytest.mark.parametrize(
    ("kind", "progress", "animated", "ready"),
    [
        (
            "claude",
            "✻ Cultivating…",
            "✽ Cultivating…",
            ["─" * 40, "❯", "─" * 40],
        ),
        (
            "codex",
            "• Working (5s • esc to interrupt)",
            "• Working (6s • esc to interrupt)",
            ["› ", "? for shortcuts"],
        ),
        ("grok", "⠹ Thinking… 1.1s", "⠸ Thinking… 1.2s", ["❯", "always-approve"]),
    ],
)
def test_temporal_corroboration_static_demotes_and_animated_stays_busy(
    kind, progress, animated, ready
):
    provider = _provider(kind)

    def run(captures):
        monitor = StatusMonitor()
        monitor._screens["receiver"] = (  # noqa: SLF001 - frozen seam simulation
            SimpleNamespace(display=[progress], columns=100, lines=20),
            object(),
        )
        backend = MagicMock(supports_identity_readback=False)
        backend.get_pane_size.return_value = (100, 20)
        backend.capture_viewport.side_effect = ["\n".join(frame) for frame in captures]
        with (
            patch(
                "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
                return_value=provider,
            ),
            patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={"tmux_session": "session", "tmux_window": "receiver"},
            ),
            patch("cli_agent_orchestrator.services.status_monitor.time.sleep"),
        ):
            return monitor.probe_screen_status("receiver"), backend

    (static_status, static_meta), static_backend = run([[progress], [progress], [progress, *ready]])
    assert static_status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
    assert static_meta["temporal_demotion"]["frames"] == 2
    assert len(static_meta["temporal_demotion"]["multiset_sha256"]) == 64
    assert static_backend.capture_viewport.call_count == 3

    (animated_status, animated_meta), animated_backend = run([[progress], [animated]])
    assert animated_status == TerminalStatus.PROCESSING
    assert "temporal_demotion" not in animated_meta
    assert animated_backend.capture_viewport.call_count == 2


def test_delayed_animation_on_final_frame_defers():
    progress = "⠹ Thinking… 1.1s"
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=False)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = [
        progress,
        progress,
        "⠸ Thinking… 1.2s\n❯\nalways-approve",
    ]
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_provider("grok"),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_metadata",
            return_value={"tmux_session": "session", "tmux_window": "receiver"},
        ),
        patch("cli_agent_orchestrator.services.status_monitor.time.sleep"),
    ):
        status, meta = monitor.probe_screen_status("receiver")
    assert status == TerminalStatus.PROCESSING
    assert meta["temporal_demotion"]["frames"] == 2
    assert meta["frame_rows_hash"]


def test_corroborable_signal_requires_row_bytes():
    with pytest.raises(ValueError, match="row_bytes"):
        ScreenSignal("progress", "spinner", 0, temporal_policy="corroborable")


def _record(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def test_binding_staleness_baseline_uses_full_candidate_set(tmp_path):
    terminal_id = "wpq2-baseline"
    bound = tmp_path / "bound.jsonl"
    live = tmp_path / "live.jsonl"
    future = tmp_path / "future.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    _record(live, {"sessionId": "new", "type": "user", "message": "before"})
    _record(future, {"sessionId": "decoy", "type": "user", "message": "decoy"})
    future_ns = live.stat().st_mtime_ns + 10_000_000
    os.utime(future, ns=(future_ns, future_ns))
    symlink = tmp_path / "linked.jsonl"
    symlink.symlink_to(live)
    binding = {"id": 7, "transcript_path": str(bound)}
    metadata = {"id": terminal_id}
    clear_binding_staleness_state(terminal_id)
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value=binding,
    ):
        first = observe_binding_absence(metadata)
        with live.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": "payload"}) + "\n")
        second = observe_binding_absence(metadata)
    assert first is not None and first.presumed_stale is False
    assert second is not None and second.presumed_stale is True
    assert second.candidates == (live.resolve(),)
    assert len(_binding_staleness) == 1
    clear_binding_staleness_state(terminal_id)


def test_binding_staleness_disables_fallback_above_4096(tmp_path):
    terminal_id = "wpq2-overflow"
    bound = tmp_path / "bound.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    for index in range(4097):
        (tmp_path / f"{index:04}.jsonl").touch()
    binding = {"id": 8, "transcript_path": str(bound)}
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value=binding,
    ):
        first = observe_binding_absence({"id": terminal_id})
        second = observe_binding_absence({"id": terminal_id})
    assert first is not None and first.presumed_stale is False
    assert second is not None and second.presumed_stale is True
    assert second.candidates == ()
    clear_binding_staleness_state(terminal_id)


@pytest.mark.parametrize(
    "raw",
    [
        "token = TOPSECRET",
        "secret = TOP SECRET PHRASE",
        "api_key = TOPSECRET",
        "Authorization: Bearer X",
        'Authorization: Digest username="user", response="TOPSECRET"',
    ],
)
def test_hook_deadletter_remainder_redactor(raw):
    from cli_agent_orchestrator.hooks.transcript_binding import _redact_error

    redacted = _redact_error(f"prefix\n{raw} suffix")
    assert "TOP" not in redacted and "Bearer X" not in redacted
    assert "\n" not in redacted
    assert redacted.endswith("[REDACTED]")


def test_hook_deadletter_permissions_and_line_bound(tmp_path):
    from cli_agent_orchestrator.hooks import transcript_binding

    with patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path):
        transcript_binding._deadletter(
            "receiver", "clear", "RuntimeError", "failed token = TOPSECRET"
        )
    data = tmp_path / "hook-deadletter.jsonl"
    lock = tmp_path / "hook-deadletter.lock"
    line = data.read_bytes()
    assert len(line) <= 1024
    assert b"TOPSECRET" not in line
    assert data.stat().st_mode & 0o777 == 0o600
    assert lock.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_server_recovery_binding_is_same_transaction_cas(wpq2_db, tmp_path):
    candidate = tmp_path / "candidate.jsonl"
    _record(candidate, {"sessionId": "new-session", "type": "user", "message": "payload"})
    with wpq2_db.begin() as db:
        stale = TranscriptBindingModel(
            terminal_id="receiver",
            session_id="old-session",
            transcript_path=str(tmp_path / "old.jsonl"),
            inode=1,
            source="clear",
        )
        db.add(stale)
        db.flush()
        stale_id = stale.id
    with patch(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.reset_binding_episodes"
    ) as reset:
        assert (
            recover_transcript_binding_if_current("receiver", stale_id, str(candidate))
            == "inserted"
        )
    with wpq2_db() as db:
        rows = (
            db.query(TranscriptBindingModel)
            .filter_by(terminal_id="receiver")
            .order_by(TranscriptBindingModel.id)
            .all()
        )
    assert [row.source for row in rows] == ["clear", "server_recovery"]
    assert rows[-1].session_id == "new-session"
    reset.assert_called_once_with("receiver")
    assert (
        recover_transcript_binding_if_current("receiver", stale_id, str(candidate))
        == "authority_changed"
    )


def test_server_recovery_rejects_duplicate_first_line_session_id(wpq2_db, tmp_path):
    candidate = tmp_path / "duplicate.jsonl"
    candidate.write_text('{"sessionId":"one","sessionId":"two","type":"user"}\n', encoding="utf-8")
    assert (
        recover_transcript_binding_if_current("receiver", 1, str(candidate)) == "invalid_session_id"
    )


def test_lifecycle_generation_cas_aborts_bump_and_settles_stable_row(wpq2_db):
    with wpq2_db.begin() as db:
        db.add(
            TerminalModel(
                id="receiver",
                tmux_session="session",
                tmux_window="receiver",
                provider="codex",
                lifecycle_generation=4,
            )
        )
        db.add(
            InboxModel(
                sender_id="missing-sender",
                receiver_id="receiver",
                message="pending",
                orchestration_type=OrchestrationType.SEND_MESSAGE.value,
                status=MessageStatus.PENDING.value,
            )
        )
    assert settle_pending_receiver_gone_if_generation("receiver", 3).settled_count == 0
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.PENDING.value
    assert settle_pending_receiver_gone_if_generation("receiver", 4).settled_count == 1
    with wpq2_db() as db:
        row = db.query(InboxModel).one()
        assert row.status == MessageStatus.DELIVERY_FAILED.value
        assert row.failure_reason == "receiver_gone"

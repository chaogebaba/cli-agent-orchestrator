import asyncio
import json
import os
import fcntl
import io
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.backends.base import PaneIdentityReadResult
from cli_agent_orchestrator.clients.database import (
    Base,
    InboxDeliveryAttemptModel,
    InboxModel,
    NoticeInsertOutcome,
    TerminalModel,
    TranscriptBindingModel,
    begin_delivery_attempt,
    count_ambiguous_attempts,
    create_transcript_binding,
    get_message_trace,
    recover_transcript_binding_if_current,
    settle_delivery_attempt,
    settle_pending_receiver_gone_if_generation,
)
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.providers.screen_classification import ScreenSignal
from cli_agent_orchestrator.services.message_trace_service import (
    BindingStalenessObservation,
    _binding_staleness,
    clear_binding_staleness_state,
    observe_binding_absence,
    scan_binding_candidates,
    wire_hash,
)
from cli_agent_orchestrator.services.inbox_service import (
    InboxService,
    clear_terminal_delivery_state,
    inbox_service as global_inbox_service,
)
from cli_agent_orchestrator.services.status_monitor import (
    BoundaryObservation,
    StatusMonitor,
    _frame_rows_hash,
    _row_multiset_hash,
)


@pytest.fixture
def wpq2_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wpq2.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    yield sessions
    engine.dispose()


@pytest.fixture(autouse=True)
def isolate_wpq2_process_state():
    _binding_staleness.clear()
    yield
    _binding_staleness.clear()


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

    (static_status, static_meta), static_backend = run(
        [[progress], [progress], [progress], [progress, *ready]]
    )
    assert static_status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}
    assert static_meta["temporal_demotion"]["frames"] == 2
    assert len(static_meta["temporal_demotion"]["multiset_sha256"]) == 64
    assert static_backend.capture_viewport.call_count == 4

    (animated_status, animated_meta), animated_backend = run([[progress], [animated]])
    assert animated_status == TerminalStatus.PROCESSING
    assert "temporal_demotion" not in animated_meta
    assert animated_backend.capture_viewport.call_count == 2


@pytest.mark.parametrize("changed_frame", [["❯", "always-approve"], ["⠸ Thinking… 1.2s"]])
def test_temporal_corroboration_changed_fresh_frame_never_opens(changed_frame):
    progress = "⠹ Thinking… 1.1s"
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=True)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = [progress, "\n".join(changed_frame)]
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
    assert "temporal_demotion" not in meta
    assert backend.capture_viewport.call_count == 2
    backend.read_pane_identity.assert_not_called()


def test_temporal_first_fresh_ready_frame_is_not_an_open_authority():
    progress = "⠹ Thinking… 1.1s"
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=True)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.return_value = "❯\nalways-approve"
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
    assert meta["frame_source"] == "fresh_capture"
    assert backend.capture_viewport.call_count == 1
    backend.read_pane_identity.assert_not_called()


def test_temporal_initial_sample_is_fresh_not_incremental():
    incremental = "⠹ Thinking… 1.1s"
    fresh = "⠸ Thinking… 1.2s"
    final_rows = [fresh, "❯", "always-approve"]
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[incremental], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=False)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = [fresh, fresh, fresh, "\n".join(final_rows)]
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
    assert status == TerminalStatus.IDLE
    assert meta["temporal_demotion"]["frames"] == 2


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


@pytest.mark.parametrize(
    ("kind", "progress", "animated", "ready"),
    [
        ("claude", "✻ Cultivating…", "✽ Cultivating…", ["─" * 40, "❯", "─" * 40]),
        (
            "codex",
            "• Working (5s • esc to interrupt)",
            "• Working (6s • esc to interrupt)",
            ["› ", "? for shortcuts"],
        ),
        ("grok", "⠹ Thinking… 1.1s", "⠸ Thinking… 1.2s", ["❯", "always-approve"]),
    ],
)
def test_delayed_final_animation_defers_for_every_provider(kind, progress, animated, ready):
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
        progress,
        "\n".join([animated, *ready]),
    ]
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_provider(kind),
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
    assert meta["frame_rows_hash"] == _frame_rows_hash([animated, *ready])


@pytest.mark.parametrize(
    ("kind", "rows", "provider_signal"),
    [
        ("claude", ["✻ Waiting for background tasks"], "BACKGROUND_WAIT_PATTERN"),
        ("grok", ["Waiting for response…"], "PROCESSING_PATTERN"),
        ("codex", ["plain output without prompt"], "SCREEN_FALLBACK_PROCESSING_PATTERN"),
    ],
)
def test_temporal_exempt_progress_never_enters_corroboration(kind, rows, provider_signal):
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=rows, columns=100, lines=20),
        object(),
    )
    backend = MagicMock()
    with (
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_provider(kind),
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
    ):
        status, meta = monitor.probe_screen_status("receiver")
    assert status == TerminalStatus.PROCESSING
    assert meta["law_signal"]["provider_signal"] == provider_signal
    assert "temporal_demotion" not in meta
    assert backend.mock_calls == []


def test_static_demotion_requires_identity_proof_before_final_capture():
    progress = "⠹ Thinking… 1.1s"
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=True)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = [progress, progress, progress]
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="replacement")
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
    assert status == TerminalStatus.UNKNOWN
    assert meta["identity_proof_failure"] == "mismatch"
    assert backend.capture_viewport.call_count == 3
    backend.read_pane_identity.assert_called_once_with("session", "receiver")


@pytest.mark.parametrize(
    "captures",
    [
        [RuntimeError("initial capture")],
        ["⠹ Thinking… 1.1s", RuntimeError("corroboration capture")],
        [
            "⠹ Thinking… 1.1s",
            "⠹ Thinking… 1.1s",
            "⠹ Thinking… 1.1s",
            RuntimeError("final capture"),
        ],
    ],
)
def test_temporal_capture_failure_is_fail_closed_processing(captures):
    progress = "⠹ Thinking… 1.1s"
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    backend = MagicMock(supports_identity_readback=False)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = captures
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
        status, _ = monitor.probe_screen_status("receiver")
    assert status == TerminalStatus.PROCESSING


def test_temporal_final_new_progress_row_defers_and_multiset_hash_is_ordered():
    progress = "⠹ Thinking… 1.1s"
    new_progress = "⠸ Responding… 1.2s"
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
        progress,
        f"{progress}\n{new_progress}\n❯\nalways-approve",
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
    assert meta["frame_rows_hash"] == _frame_rows_hash(
        [progress, new_progress, "❯", "always-approve"]
    )
    assert _row_multiset_hash(("b", "a", "a")) == _row_multiset_hash(("a", "b", "a"))
    assert _row_multiset_hash(("b", "a", "a")) != _row_multiset_hash(("b", "a"))


def test_grok_static_spinner_demotes_then_delivers_through_real_inbox_seam(wpq2_db, tmp_path):
    database.create_terminal("sender", "session", "sender", "codex")
    database.create_terminal("receiver", "session", "receiver", "grok_cli")
    message = database.create_inbox_message("sender", "receiver", "deliver after demotion")
    progress = "⠹ Thinking… 1.1s"
    final_rows = [progress, "❯", "always-approve"]
    monitor = StatusMonitor()
    monitor._screens["receiver"] = (  # noqa: SLF001
        SimpleNamespace(display=[progress], columns=100, lines=20),
        object(),
    )
    boundary = BoundaryObservation("wpq2", TerminalStatus.IDLE, 1, 1, 1, None, 1)
    backend = MagicMock(supports_identity_readback=True)
    backend.get_pane_size.return_value = (100, 20)
    backend.capture_viewport.side_effect = [
        progress,
        progress,
        progress,
        "\n".join(final_rows),
    ]
    backend.read_pane_identity.return_value = PaneIdentityReadResult(identity="receiver")
    sent = MagicMock()

    def send(_terminal_id, _wire, **kwargs):
        sent(_terminal_id, _wire)
        callback = kwargs.get("on_submitted")
        if callback is not None:
            callback(boundary)
        return boundary

    with (
        patch("cli_agent_orchestrator.services.inbox_service.status_monitor", monitor),
        patch.object(monitor, "get_boundary_observation", return_value=boundary),
        patch.object(monitor, "get_status", return_value=TerminalStatus.IDLE),
        patch.object(monitor, "get_input_gen", return_value=1),
        patch.object(monitor, "get_status_gen", return_value=1),
        patch(
            "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
            return_value=_provider("grok"),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.provider_manager.get_provider",
            return_value=_provider("grok"),
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
            "cli_agent_orchestrator.services.inbox_service.terminal_service." "send_prepared_input",
            side_effect=send,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.confirm_delivery",
            return_value=("unverified", {"kind": "send_returned_unverified"}),
        ),
        patch("cli_agent_orchestrator.services.status_monitor.time.sleep"),
    ):
        InboxService().deliver_pending("receiver")
    assert backend.capture_viewport.call_count == 4
    assert backend.read_pane_identity.call_count == 1
    assert sent.call_count == 1
    trace = get_message_trace(message.id)
    assert len(trace["attempts"]) == 1
    probe = trace["attempts"][0]["evidence"]["screen_probe"]
    assert probe["temporal_demotion"]["frames"] == 2
    assert probe["frame_rows_hash"] == _frame_rows_hash(final_rows)
    assert probe["frame_source"] == "fresh_capture"
    sent.assert_called_once_with("receiver", "deliver after demotion")
    backend.read_pane_identity.assert_called_once_with("session", "receiver")


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


def test_binding_candidate_survives_64_future_dated_unchanged_siblings(tmp_path):
    bound = tmp_path / "bound.jsonl"
    live = tmp_path / "live.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    _record(live, {"sessionId": "new", "type": "user", "message": "before"})
    live_ns = live.stat().st_mtime_ns
    os.utime(live, ns=(live_ns - 1, live_ns - 1))
    for index in range(65):
        decoy = tmp_path / f"future-{index:02}.jsonl"
        _record(decoy, {"sessionId": f"decoy-{index}"})
        future_ns = live_ns + 1_000_000 + index
        os.utime(decoy, ns=(future_ns, future_ns))
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 11, "transcript_path": str(bound)},
    ):
        first = observe_binding_absence({"id": "many-decoys"})
        assert first is not None and first.presumed_stale is False
        with live.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": "payload"}) + "\n")
        stale = observe_binding_absence({"id": "many-decoys"})
    assert stale is not None and stale.candidates == (live.resolve(),)
    clear_binding_staleness_state("many-decoys")


def test_binding_candidate_detects_inode_only_replacement(tmp_path):
    bound = tmp_path / "bound.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    _record(candidate, {"sessionId": "new", "message": "same-size"})
    original = candidate.stat()
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(candidate.read_bytes())
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 12, "transcript_path": str(bound)},
    ):
        observe_binding_absence({"id": "inode-replacement"})
        os.replace(replacement, candidate)
        os.utime(candidate, ns=(original.st_atime_ns, original.st_mtime_ns))
        stale = observe_binding_absence({"id": "inode-replacement"})
    assert stale is not None and stale.candidates == (candidate.resolve(),)
    clear_binding_staleness_state("inode-replacement")


def test_binding_candidate_created_after_baseline_can_be_backdated(tmp_path):
    bound = tmp_path / "bound.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 13, "transcript_path": str(bound)},
    ):
        observe_binding_absence({"id": "backdated-create"})
        candidate = tmp_path / "backdated.jsonl"
        _record(candidate, {"sessionId": "new", "message": "payload"})
        os.utime(candidate, ns=(1, 1))
        stale = observe_binding_absence({"id": "backdated-create"})
    assert stale is not None and stale.candidates == (candidate.resolve(),)
    clear_binding_staleness_state("backdated-create")


def test_binding_candidate_identical_triple_rewrite_is_out_of_scope(tmp_path):
    bound = tmp_path / "bound.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    candidate.write_text("aaaaaaaa\n", encoding="utf-8")
    original = candidate.stat()
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 14, "transcript_path": str(bound)},
    ):
        observe_binding_absence({"id": "identical-triple"})
        candidate.write_text("bbbbbbbb\n", encoding="utf-8")
        os.utime(candidate, ns=(original.st_atime_ns, original.st_mtime_ns))
        stale = observe_binding_absence({"id": "identical-triple"})
    assert stale is not None and stale.candidates == ()
    clear_binding_staleness_state("identical-triple")


def test_bound_file_content_hash_rebaselines_identical_inode_size_and_mtime(tmp_path):
    bound = tmp_path / "bound.jsonl"
    bound.write_text("aaaaaaaa\n", encoding="utf-8")
    original = bound.stat()
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 16, "transcript_path": str(bound)},
    ):
        observe_binding_absence({"id": "bound-content-hash"})
        bound.write_text("bbbbbbbb\n", encoding="utf-8")
        os.utime(bound, ns=(original.st_atime_ns, original.st_mtime_ns))
        observation = observe_binding_absence({"id": "bound-content-hash"})
    assert observation is not None and observation.presumed_stale is False
    clear_binding_staleness_state("bound-content-hash")


def test_candidate_scan_skips_vanished_file_and_requires_unique_hit(tmp_path):
    payload = "candidate payload"
    digest = wire_hash(payload)
    hit_one = tmp_path / "hit-one.jsonl"
    hit_two = tmp_path / "hit-two.jsonl"
    missing = tmp_path / "vanished.jsonl"
    _record(hit_one, {"sessionId": "one", "type": "user", "message": payload})
    observation = BindingStalenessObservation(1, tmp_path / "bound.jsonl", True, (missing, hit_one))
    result, _, candidate = scan_binding_candidates(observation, digest, None, {})
    assert result == "hit" and candidate == hit_one
    _record(hit_two, {"sessionId": "two", "type": "user", "message": payload})
    duplicate = BindingStalenessObservation(1, tmp_path / "bound.jsonl", True, (hit_one, hit_two))
    result, evidence, candidate = scan_binding_candidates(duplicate, digest, None, {})
    assert result == "unresolved"
    assert evidence == {"kind": "transcript_candidate_ambiguous"}
    assert candidate is None


def test_binding_candidate_selection_caps_at_newest_eight(tmp_path):
    bound = tmp_path / "bound.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    candidates = []
    for index in range(9):
        candidate = tmp_path / f"candidate-{index}.jsonl"
        _record(candidate, {"sessionId": str(index), "message": "before"})
        candidates.append(candidate)
    with patch(
        "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
        return_value={"id": 15, "transcript_path": str(bound)},
    ):
        observe_binding_absence({"id": "candidate-overflow"})
        for index, candidate in enumerate(candidates):
            with candidate.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "user", "message": "payload"}) + "\n")
            os.utime(candidate, ns=(index + 1, index + 1))
        stale = observe_binding_absence({"id": "candidate-overflow"})
    assert stale is not None and len(stale.candidates) == 8
    assert candidates[0].resolve() not in stale.candidates
    clear_binding_staleness_state("candidate-overflow")


def test_binding_staleness_keeps_one_bundle_across_many_epochs(tmp_path):
    bound = tmp_path / "bound.jsonl"
    _record(bound, {"sessionId": "old", "type": "user", "message": "old"})
    terminal_id = "many-epochs"
    for binding_id in range(1, 101):
        with patch(
            "cli_agent_orchestrator.services.message_trace_service.get_current_transcript_binding",
            return_value={"id": binding_id, "transcript_path": str(bound)},
        ):
            observation = observe_binding_absence({"id": terminal_id})
        assert observation is not None and observation.binding_id == binding_id
        assert len(_binding_staleness) == 1
    assert _binding_staleness[terminal_id].binding_id == 100
    clear_terminal_delivery_state(terminal_id)
    assert terminal_id not in _binding_staleness


def _seed_preopen_delivery(sessions, tmp_path, payload="callback payload"):
    database.create_terminal("sender", "session", "sender", "codex")
    database.create_terminal("receiver", "session", "receiver", "claude_code", caller_id="sender")
    message = database.create_inbox_message("sender", "receiver", payload)
    bound = tmp_path / "bound.jsonl"
    candidate = tmp_path / "candidate.jsonl"
    _record(bound, {"sessionId": "old-session", "type": "user", "message": "historical"})
    _record(candidate, {"sessionId": "new-session", "type": "assistant"})
    create_transcript_binding("receiver", "old-session", str(bound), bound.stat().st_ino, "clear")
    baseline = {
        "path": str(bound),
        "inode": bound.stat().st_ino,
        "size": bound.stat().st_size,
        "resolution_kind": "binding",
        "cursor_version": 1,
    }
    attempt_evidence = json.dumps({"resolution_kind": "binding", "last_observed_ref": baseline})
    attempt_uuid = begin_delivery_attempt(
        [message],
        "receiver",
        "claude_code",
        wire_hash(payload),
        len(payload.encode()),
        evidence=attempt_evidence,
    )
    settle_delivery_attempt(
        attempt_uuid,
        MessageStatus.PENDING,
        "ambiguous",
        reason="confirmation_timeout",
        evidence=attempt_evidence,
    )
    return message, attempt_uuid, bound, candidate


def _busy_boundary():
    return SimpleNamespace(
        status=TerminalStatus.PROCESSING,
        observation_epoch="wpq2",
        status_gen=1,
        input_gen=1,
        seq=1,
        last_non_ready_seq=1,
        last_ready_seq=None,
    )


def test_orch1_preopen_candidate_hit_confirms_without_attempt_or_paste(wpq2_db, tmp_path):
    payload = "callback payload"
    message, _, _, candidate = _seed_preopen_delivery(wpq2_db, tmp_path, payload)
    service = InboxService()
    sent = MagicMock()
    with (
        patch.object(service, "_commit_watchdog_ops"),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor."
            "get_boundary_observation",
            return_value=_busy_boundary(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor.get_status",
            return_value=TerminalStatus.PROCESSING,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service." "send_prepared_input",
            sent,
        ),
    ):
        service.deliver_pending("receiver")
        with candidate.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": payload}) + "\n")
        service.deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.DELIVERED.value
    assert len(trace["attempts"]) == 1
    assert count_ambiguous_attempts([message.id]) == 1
    sent.assert_not_called()


def test_orch1_preopen_no_hit_suppresses_and_notices_once(wpq2_db, tmp_path):
    message, _, _, candidate = _seed_preopen_delivery(wpq2_db, tmp_path)
    service = InboxService()
    sent = MagicMock()
    with (
        patch.object(service, "_commit_watchdog_ops"),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor."
            "get_boundary_observation",
            return_value=_busy_boundary(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor.get_status",
            return_value=TerminalStatus.PROCESSING,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.terminal_service." "send_prepared_input",
            sent,
        ),
    ):
        service.deliver_pending("receiver")
        with candidate.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": "not the payload"}) + "\n")
        for _ in range(4):
            service.deliver_pending("receiver")
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert len(trace["attempts"]) == 1
    assert count_ambiguous_attempts([message.id]) == 1
    sent.assert_not_called()
    with wpq2_db() as db:
        notices = (
            db.query(InboxModel)
            .filter(InboxModel.receiver_id == "sender")
            .filter(InboxModel.message.startswith("[binding-authority]"))
            .all()
        )
    assert len(notices) == 1
    assert notices[0].message == (
        "[binding-authority] transcript binding presumed stale for terminal receiver "
        "(binding 1): delivery confirmations unconfirmable; 3 cycles suppressed; "
        "awaiting binding recovery or a new session epoch"
    )


def test_recovery_authority_change_never_resurrects_old_binding_episode(wpq2_db, tmp_path):
    payload = "callback payload"
    message, _, _, candidate = _seed_preopen_delivery(wpq2_db, tmp_path, payload)
    winning = tmp_path / "winning-hook.jsonl"
    _record(winning, {"sessionId": "hook-session", "type": "user", "message": "other"})
    service = InboxService()
    service._record_binding_authority_failure(  # noqa: SLF001 - state-machine seam
        "receiver", 1, database.get_terminal_metadata("receiver")
    )

    def winning_hook(*_args):
        create_transcript_binding(
            "receiver", "hook-session", str(winning), winning.stat().st_ino, "clear"
        )
        service.reset_binding_episodes("receiver")
        return "authority_changed"

    with (
        patch.object(service, "_commit_watchdog_ops"),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor."
            "get_boundary_observation",
            return_value=_busy_boundary(),
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service.status_monitor.get_status",
            return_value=TerminalStatus.PROCESSING,
        ),
        patch(
            "cli_agent_orchestrator.services.inbox_service."
            "recover_transcript_binding_if_current",
            side_effect=winning_hook,
        ),
    ):
        service.deliver_pending("receiver")
        with candidate.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "user", "message": payload}) + "\n")
        service.deliver_pending("receiver")
    assert service._binding_authority == {}  # noqa: SLF001
    assert get_message_trace(message.id)["message"]["status"] == MessageStatus.PENDING.value


@pytest.mark.parametrize(
    "raw",
    [
        "token = TOPSECRET",
        "token: TOPSECRET",
        "secret = TOP SECRET PHRASE",
        'secret = "TOP SECRET PHRASE"',
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


def test_hook_deadletter_24_writer_contention_respects_rotation_cap(tmp_path):
    from cli_agent_orchestrator.hooks import transcript_binding

    data = tmp_path / "hook-deadletter.jsonl"
    data.write_bytes(b"x" * (transcript_binding._DEADLETTER_MAX_BYTES - 2048))
    data.chmod(0o666)
    barrier = threading.Barrier(24)

    def writer(index):
        barrier.wait()
        transcript_binding._deadletter(
            f"receiver-{index}", "clear", "RuntimeError", "failure " + "z" * 200
        )

    with patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path):
        with ThreadPoolExecutor(max_workers=24) as pool:
            list(pool.map(writer, range(24)))
    rotated = tmp_path / "hook-deadletter.jsonl.1"
    assert data.stat().st_size <= transcript_binding._DEADLETTER_MAX_BYTES
    assert data.stat().st_mode & 0o777 == 0o600
    if rotated.exists():
        assert rotated.stat().st_size <= transcript_binding._DEADLETTER_MAX_BYTES
        assert rotated.stat().st_mode & 0o777 == 0o600


def test_hook_deadletter_lock_contention_drops_without_unlocked_append(tmp_path):
    from cli_agent_orchestrator.hooks import transcript_binding

    lock = tmp_path / "hook-deadletter.lock"
    lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path):
            transcript_binding._deadletter("receiver", "clear", "RuntimeError", "drop")
    finally:
        os.close(lock_fd)
    assert not (tmp_path / "hook-deadletter.jsonl").exists()


def test_hook_deadletter_rotates_under_lock_and_repairs_rotated_mode(tmp_path):
    from cli_agent_orchestrator.hooks import transcript_binding

    data = tmp_path / "hook-deadletter.jsonl"
    rotated = tmp_path / "hook-deadletter.jsonl.1"
    old = b"x" * transcript_binding._DEADLETTER_MAX_BYTES
    data.write_bytes(old)
    data.chmod(0o666)
    rotated.write_bytes(b"old-rotation")
    rotated.chmod(0o777)
    with patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path):
        transcript_binding._deadletter("receiver", "clear", "RuntimeError", "rotate")
    assert rotated.read_bytes() == old
    assert rotated.stat().st_mode & 0o777 == 0o600
    assert data.stat().st_size < 1024
    assert data.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["replace", "write"])
def test_hook_deadletter_io_failures_warn_and_drop(tmp_path, capsys, failure):
    from cli_agent_orchestrator.hooks import transcript_binding

    data = tmp_path / "hook-deadletter.jsonl"
    if failure == "replace":
        data.write_bytes(b"x" * transcript_binding._DEADLETTER_MAX_BYTES)
        target = "os.replace"
    else:
        target = "os.write"
    with (
        patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path),
        patch(target, side_effect=OSError("forced failure")),
    ):
        transcript_binding._deadletter("receiver", "clear", "OSError", "failure")
    assert "WARNING: CAO transcript binding dead-letter failed: OSError" in capsys.readouterr().err


def test_hook_deadletter_main_uses_sentinels_on_preparse_failure(tmp_path, monkeypatch):
    from cli_agent_orchestrator.hooks import transcript_binding

    monkeypatch.setattr("sys.stdin", io.StringIO("not-json"))
    with patch.object(transcript_binding, "CAO_HOME_DIR", tmp_path):
        assert transcript_binding.main() == 0
    record = json.loads((tmp_path / "hook-deadletter.jsonl").read_text().strip())
    assert record["terminal_id"] == "unknown"
    assert record["event_source"] == "unparsed"


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


@pytest.mark.parametrize(
    "first_line",
    [
        '{"sessionId":"one","sessionId":"two","type":"user"}',
        '[["sessionId","array-is-not-an-object"],["type","user"]]',
        '[{"sessionId":"nested-object"}]',
        '"sessionId"',
        "42",
        "true",
        "null",
        "{}",
        '{"sessionId":null}',
        '{"sessionId":""}',
        '{"sessionId":17}',
    ],
)
def test_server_recovery_rejects_malformed_first_line_shapes(wpq2_db, tmp_path, first_line):
    candidate = tmp_path / "malformed.jsonl"
    candidate.write_text(first_line + "\n", encoding="utf-8")
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


def _seed_reconcile_receiver(sessions, generation=7):
    with sessions.begin() as db:
        db.add(
            TerminalModel(
                id="receiver",
                tmux_session="session",
                tmux_window="receiver",
                provider="codex",
                lifecycle_generation=generation,
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


def test_reconcile_foreign_same_name_window_defers_to_generation_cas(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)
    backend = MagicMock()
    backend.window_liveness.side_effect = ["gone", "gone", "gone", "live"]
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(3):
            service.reconcile_pending_orphans()
    with wpq2_db() as db:
        row = db.query(InboxModel).one()
        assert row.status == MessageStatus.DELIVERY_FAILED.value
        assert row.failure_reason == "receiver_gone"


def test_reconcile_cao_recreation_generation_bump_aborts(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)
    calls = 0

    def liveness(_session, _window):
        nonlocal calls
        calls += 1
        if calls == 4:
            with wpq2_db.begin() as db:
                db.query(TerminalModel).filter_by(id="receiver").update({"lifecycle_generation": 8})
            return "live"
        return "gone"

    backend = MagicMock()
    backend.window_liveness.side_effect = liveness
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(3):
            service.reconcile_pending_orphans()
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.PENDING.value


def test_reconcile_final_liveness_error_is_fail_closed(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)
    backend = MagicMock()
    backend.window_liveness.side_effect = ["gone", "gone", "gone", "error"]
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(3):
            service.reconcile_pending_orphans()
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.PENDING.value


def test_d17_identity_reason_attempts_never_trigger_receiver_gone_settlement(wpq2_db):
    database.create_terminal("sender", "session", "sender", "codex")
    database.create_terminal("receiver", "session", "receiver", "claude_code")
    message = database.create_inbox_message("sender", "receiver", "pending")
    for reason in ("mismatch", "missing_env", "read_error"):
        attempt = begin_delivery_attempt(
            [message], "receiver", "claude_code", wire_hash("pending"), len("pending")
        )
        settle_delivery_attempt(
            attempt,
            MessageStatus.PENDING,
            "ambiguous",
            reason=f"pane_identity_mismatch:{reason}",
        )
    backend = MagicMock()
    backend.window_liveness.return_value = "live"
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(3):
            service.reconcile_pending_orphans()
    trace = get_message_trace(message.id)
    assert trace["message"]["status"] == MessageStatus.PENDING.value
    assert len(trace["attempts"]) == 3


def test_reconcile_requires_exactly_three_consecutive_gone_cycles(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)
    backend = MagicMock()
    backend.window_liveness.side_effect = ["gone", "gone", "gone", "gone"]
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(2):
            service.reconcile_pending_orphans()
            with wpq2_db() as db:
                assert db.query(InboxModel).one().status == MessageStatus.PENDING.value
        service.reconcile_pending_orphans()
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.DELIVERY_FAILED.value


@pytest.mark.parametrize("reset_state", ["live", "error"])
def test_reconcile_present_or_unknown_resets_gone_streak(wpq2_db, reset_state):
    _seed_reconcile_receiver(wpq2_db)
    backend = MagicMock()
    backend.window_liveness.side_effect = [
        "gone",
        "gone",
        reset_state,
        "gone",
        "gone",
        "gone",
        "gone",
    ]
    service = InboxService()
    with patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend):
        for _ in range(5):
            service.reconcile_pending_orphans()
        with wpq2_db() as db:
            assert db.query(InboxModel).one().status == MessageStatus.PENDING.value
        service.reconcile_pending_orphans()
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.DELIVERY_FAILED.value


def test_reconcile_final_probe_runs_inside_delivery_then_authority_locks(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)

    class TrackingLock:
        def __init__(self):
            self.held = False

        def acquire(self, blocking=False):
            assert blocking is False
            self.held = True
            return True

        def release(self):
            assert self.held
            self.held = False

    delivery_lock = TrackingLock()
    authority_lock = TrackingLock()
    backend = MagicMock()
    calls = 0

    def liveness(_session, _window):
        nonlocal calls
        calls += 1
        if calls == 4:
            assert delivery_lock.held and authority_lock.held
        return "gone"

    backend.window_liveness.side_effect = liveness
    service = InboxService()
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.services.inbox_service.get_delivery_lock",
            return_value=delivery_lock,
        ),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock",
            return_value=authority_lock,
        ),
    ):
        for _ in range(3):
            service.reconcile_pending_orphans()
    assert calls == 4
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.DELIVERY_FAILED.value


def test_reconcile_publication_lock_race_aborts_before_final_probe(wpq2_db):
    _seed_reconcile_receiver(wpq2_db)
    backend = MagicMock()
    backend.window_liveness.return_value = "gone"
    authority_lock = MagicMock()
    authority_lock.acquire.return_value = False
    service = InboxService()
    with (
        patch("cli_agent_orchestrator.backends.registry.get_backend", return_value=backend),
        patch(
            "cli_agent_orchestrator.services.mailbox_service.get_mailbox_authority_lock",
            return_value=authority_lock,
        ),
    ):
        for _ in range(3):
            service.reconcile_pending_orphans()
    assert backend.window_liveness.call_count == 3
    authority_lock.release.assert_not_called()
    with wpq2_db() as db:
        assert db.query(InboxModel).one().status == MessageStatus.PENDING.value


def test_lifecycle_generation_migration_backfills_legacy_null(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
            "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, lifecycle_generation INTEGER)"
        )
        connection.execute(
            "INSERT INTO terminals VALUES ('receiver','session','receiver','codex',NULL)"
        )
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_terminals_schema()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT lifecycle_generation FROM terminals WHERE id='receiver'"
        ).fetchone()
        column = next(
            item
            for item in connection.execute("PRAGMA table_info(terminals)")
            if item[1] == "lifecycle_generation"
        )
    assert row == (0,)
    assert column[3] == 1 and column[4] == "0"


@pytest.mark.parametrize(
    ("outcome", "notified", "expected_calls"),
    [
        (NoticeInsertOutcome.INSERTED, True, 1),
        (NoticeInsertOutcome.FAILED_AFTER_COMMIT, True, 1),
        (NoticeInsertOutcome.UNCERTAIN_COMMIT, True, 1),
        (NoticeInsertOutcome.FAILED_BEFORE_COMMIT, False, 2),
    ],
)
def test_binding_notice_insert_outcomes_and_exact_text(outcome, notified, expected_calls):
    service = InboxService()
    metadata = {"caller_id": "sender", "tmux_session": "session"}
    with (
        patch.object(service, "_identity_notice_receiver", return_value="sender"),
        patch(
            "cli_agent_orchestrator.services.inbox_service." "insert_identity_authority_notice",
            return_value=outcome,
        ) as insert,
    ):
        for _ in range(3):
            service._record_binding_authority_failure("receiver", 17, metadata)  # noqa: SLF001
        service._record_binding_authority_failure("receiver", 17, metadata)  # noqa: SLF001
    key = ("receiver", "binding:17")
    assert service._binding_authority[key].notified is notified  # noqa: SLF001
    assert insert.call_count == expected_calls
    assert insert.call_args_list[0].args == (
        "message-trace:receiver",
        "sender",
        "[binding-authority] transcript binding presumed stale for terminal receiver "
        "(binding 17): delivery confirmations unconfirmable; 3 cycles suppressed; "
        "awaiting binding recovery or a new session epoch",
    )


def test_ordinary_success_identity_reset_does_not_clear_binding_family():
    service = InboxService()
    service._record_binding_authority_failure(  # noqa: SLF001
        "receiver", 2, {"tmux_session": "session"}
    )
    service._reset_identity_authority("receiver")  # noqa: SLF001
    assert ("receiver", "binding:2") in service._binding_authority  # noqa: SLF001


def test_identity_and_binding_authority_stores_are_isolated():
    service = InboxService()
    metadata = {"tmux_session": "session"}
    batch = [SimpleNamespace(logical_receiver_id=None)]
    service._record_identity_authority_failure(  # noqa: SLF001
        "receiver", batch, metadata, "mismatch"
    )
    service._record_binding_authority_failure("receiver", 3, metadata)  # noqa: SLF001
    service.reset_binding_episodes("receiver")
    assert ("receiver", "raw") in service._identity_authority  # noqa: SLF001
    service._record_binding_authority_failure("receiver", 3, metadata)  # noqa: SLF001
    service._clear_identity_authority("receiver")  # noqa: SLF001
    assert ("receiver", "binding:3") in service._binding_authority  # noqa: SLF001


def test_successful_recovery_cas_resets_whole_binding_family(wpq2_db, tmp_path):
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
    global_inbox_service.reset_binding_episodes("receiver")
    global_inbox_service._record_binding_authority_failure(  # noqa: SLF001
        "receiver", stale_id, {"tmux_session": "session"}
    )
    global_inbox_service._record_binding_authority_failure(  # noqa: SLF001
        "receiver", stale_id + 1, {"tmux_session": "session"}
    )
    assert recover_transcript_binding_if_current("receiver", stale_id, str(candidate)) == "inserted"
    assert not any(
        key[0] == "receiver" for key in global_inbox_service._binding_authority  # noqa: SLF001
    )


def test_genuine_hook_binding_insert_resets_whole_binding_family(wpq2_db, tmp_path):
    from cli_agent_orchestrator.api.main import TranscriptBindingRequest, bind_transcript

    projects = tmp_path / "projects"
    transcript = projects / "repo" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    _record(transcript, {"sessionId": "hook-session", "type": "user"})
    database.create_terminal("receiver", "session", "receiver", "claude_code")
    global_inbox_service.reset_binding_episodes("receiver")
    global_inbox_service._record_binding_authority_failure(  # noqa: SLF001
        "receiver", 1, {"tmux_session": "session"}
    )
    body = TranscriptBindingRequest(
        terminal_id="receiver",
        session_id="hook-session",
        transcript_path=str(transcript),
        source="clear",
    )
    with (
        patch(
            "cli_agent_orchestrator.api.main.get_terminal_metadata",
            return_value={"id": "receiver"},
        ),
        patch(
            "cli_agent_orchestrator.api.main.provider_home",
            return_value=SimpleNamespace(projects=projects),
        ),
    ):
        result = asyncio.run(bind_transcript("receiver", body, []))
    assert result["success"] is True
    assert not any(
        key[0] == "receiver" for key in global_inbox_service._binding_authority  # noqa: SLF001
    )


def test_terminal_teardown_evicts_both_authority_stores():
    metadata = {"tmux_session": "session"}
    batch = [SimpleNamespace(logical_receiver_id=None)]
    global_inbox_service._record_identity_authority_failure(  # noqa: SLF001
        "teardown", batch, metadata, "mismatch"
    )
    global_inbox_service._record_binding_authority_failure("teardown", 4, metadata)  # noqa: SLF001
    clear_terminal_delivery_state("teardown")
    assert not any(
        key[0] == "teardown" for key in global_inbox_service._identity_authority  # noqa: SLF001
    )
    assert not any(
        key[0] == "teardown" for key in global_inbox_service._binding_authority  # noqa: SLF001
    )

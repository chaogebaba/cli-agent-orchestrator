"""Acceptance coverage for the F10/F11 post-restart fix batch."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import stalled_callback_watchdog as watchdog_module
from cli_agent_orchestrator.services import terminal_guard_service
from cli_agent_orchestrator.services import terminal_service as terminals


def _row(source_terminal_id="base-source"):
    return {
        "id": 7,
        "name": "base",
        "kind": "base",
        "source_terminal_id": source_terminal_id,
        "session_uuid": "uuid",
        "cwd": "/repo",
        "git_sha": "old",
        "dirty_hashes": "{}",
    }


async def _inline(
    _terminal,
    _generation,
    _kind,
    _operation,
    function,
    *args,
    deadline=None,
    **kwargs,
):
    return function(*args, **kwargs), time.monotonic()


def _prepare_stale_refresh(monkeypatch, row):
    terminals._fork_refresh_locks.clear()
    monkeypatch.setattr(terminals, "_tracked_blocking", _inline)
    monkeypatch.setattr(
        terminals, "get_ready_provider_session", lambda _name: dict(row)
    )
    monkeypatch.setattr(
        terminals, "fork_staleness", lambda _row: (["changed.py"], "[STALE]")
    )


@pytest.mark.asyncio
async def test_f10_dangling_source_exits_before_dispatch_with_one_warning(
    monkeypatch, caplog
):
    row = _row()
    _prepare_stale_refresh(monkeypatch, row)
    dispatch = MagicMock()
    monkeypatch.setattr(terminals, "FORK_REFRESH_WAIT_BUDGET", 1.0)
    monkeypatch.setattr(
        terminals.status_monitor, "get_status", lambda _id: TerminalStatus.UNKNOWN
    )
    monkeypatch.setattr(terminals, "terminal_exists", lambda _id: False)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", dispatch)

    started = time.monotonic()
    result = await terminals._prepare_fork_refresh(
        "worker", "generation", "base", "[STALE]", None, {}
    )

    assert result == "[STALE]"
    assert time.monotonic() - started < 0.2
    dispatch.assert_not_called()
    warnings = [
        record.message
        for record in caplog.records
        if "Fork refresh source terminal is gone" in record.message
    ]
    assert len(warnings) == 1
    assert "base=base" in warnings[0]
    assert "source_terminal_id=base-source" in warnings[0]


@pytest.mark.asyncio
async def test_f10_deletion_during_post_dispatch_wait_skips_snapshot(
    monkeypatch, caplog
):
    row = _row()
    _prepare_stale_refresh(monkeypatch, row)
    statuses = iter([TerminalStatus.IDLE, TerminalStatus.UNKNOWN])
    dispatch = MagicMock(return_value=True)
    snapshot = MagicMock()
    snapshot_write = MagicMock()
    monkeypatch.setattr(
        terminals.status_monitor, "get_status", lambda _id: next(statuses)
    )
    monkeypatch.setattr(terminals.status_monitor, "get_input_gen", lambda _id: 1)
    monkeypatch.setattr(terminals, "terminal_exists", lambda _id: False)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", dispatch)
    monkeypatch.setattr(terminals, "fork_snapshot", snapshot)
    monkeypatch.setattr(terminals, "update_provider_session_snapshot", snapshot_write)

    result = await terminals._prepare_fork_refresh(
        "worker", "generation", "base", "[STALE]", None, {}
    )

    assert result == "[STALE]"
    dispatch.assert_called_once()
    snapshot.assert_not_called()
    snapshot_write.assert_not_called()
    warnings = [
        record.message
        for record in caplog.records
        if "Fork refresh source terminal is gone" in record.message
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_f10_live_unknown_source_recovers(monkeypatch):
    statuses = iter([TerminalStatus.UNKNOWN, TerminalStatus.IDLE])
    exists = MagicMock(return_value=True)
    monkeypatch.setattr(
        terminals.status_monitor, "get_status", lambda _id: next(statuses)
    )
    monkeypatch.setattr(terminals, "terminal_exists", exists)
    monkeypatch.setattr(asyncio, "sleep", MagicMock(return_value=asyncio.sleep(0)))

    assert await terminals._wait_for_base_ready(
        "base-source", time.monotonic() + 0.2
    )
    exists.assert_called_once_with("base-source")


@pytest.mark.asyncio
async def test_f10_live_unknown_source_remains_bounded(monkeypatch):
    monkeypatch.setattr(
        terminals.status_monitor, "get_status", lambda _id: TerminalStatus.UNKNOWN
    )
    monkeypatch.setattr(terminals, "terminal_exists", lambda _id: True)

    started = time.monotonic()
    assert not await terminals._wait_for_base_ready(
        "base-source", started + 0.02
    )
    assert time.monotonic() - started >= 0.01


@pytest.mark.asyncio
async def test_f10_busy_live_source_still_waits_for_budget(monkeypatch):
    exists = MagicMock(return_value=True)
    monkeypatch.setattr(
        terminals.status_monitor, "get_status", lambda _id: TerminalStatus.PROCESSING
    )
    monkeypatch.setattr(terminals, "terminal_exists", exists)

    started = time.monotonic()
    assert not await terminals._wait_for_base_ready(
        "base-source", started + 0.02
    )
    assert time.monotonic() - started >= 0.01
    exists.assert_not_called()


@pytest.mark.asyncio
async def test_f10_null_source_is_immediate_without_dead_source_warning(
    monkeypatch, caplog
):
    row = _row(source_terminal_id=None)
    _prepare_stale_refresh(monkeypatch, row)
    wait = MagicMock()
    dispatch = MagicMock()
    monkeypatch.setattr(terminals, "_wait_for_base_ready", wait)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", dispatch)

    started = time.monotonic()
    result = await terminals._prepare_fork_refresh(
        "worker", "generation", "base", "[STALE]", None, {}
    )

    assert result == "[STALE]"
    assert time.monotonic() - started < 0.2
    wait.assert_not_called()
    dispatch.assert_not_called()
    assert not any(
        "Fork refresh source terminal is gone" in record.message
        for record in caplog.records
    )


@pytest.fixture
def send_environment(monkeypatch):
    metadata = {
        "tmux_session": "cao-session",
        "tmux_window": "base-window",
        "agent_profile": "codex_base",
        "caller_id": "caller",
    }
    provider = MagicMock()
    provider.blocks_orchestrated_input_while_waiting_user_answer = False
    provider.composer_stash_keys = None
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.0
    backend = MagicMock()
    update_last_active = MagicMock()
    plugin_dispatch = MagicMock()

    monkeypatch.setattr(terminals, "get_terminal_metadata", lambda _id: metadata)
    monkeypatch.setattr(
        terminals.provider_manager, "get_provider", lambda _id: provider
    )
    monkeypatch.setattr(terminals, "get_backend", lambda: backend)
    monkeypatch.setattr(terminals, "update_last_active", update_last_active)
    monkeypatch.setattr(
        terminals, "preserve_draft_before_send", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        terminals, "_append_message_contract", lambda message, *_args: message
    )
    monkeypatch.setattr(
        terminals, "inject_memory_context", lambda message, *_args, **_kwargs: message
    )
    monkeypatch.setattr(terminals.status_monitor, "notify_input_sent", MagicMock())
    monkeypatch.setattr(terminals.status_monitor, "clear_rolling_buffer", MagicMock())
    monkeypatch.setattr(terminals, "dispatch_plugin_event", plugin_dispatch)
    return SimpleNamespace(
        metadata=metadata,
        provider=provider,
        backend=backend,
        update_last_active=update_last_active,
        plugin_dispatch=plugin_dispatch,
    )


def test_f11_dispatch_marks_refresh_as_not_expecting_callback(monkeypatch):
    guard = MagicMock()
    send = MagicMock(return_value=True)
    registry = MagicMock()
    monkeypatch.setattr(terminal_guard_service, "require_input_allowed", guard)
    monkeypatch.setattr(terminals, "send_input", send)

    assert terminals._dispatch_base_refresh(
        "base-source", "refresh", sender_id="caller", registry=registry
    )

    guard.assert_called_once_with("base-source", refresh_ingest=True)
    send.assert_called_once_with(
        "base-source",
        "refresh",
        registry=registry,
        sender_id="caller",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        expect_callback=False,
    )


def test_f11_refresh_preserves_sender_plugin_event_and_last_active(
    monkeypatch, send_environment
):
    watcher = MagicMock()
    watcher.has_episode.return_value = True
    create_inbox = MagicMock()
    registry = MagicMock()
    monkeypatch.setattr(watchdog_module, "stalled_callback_watchdog", watcher)
    monkeypatch.setattr(terminals, "create_inbox_message", create_inbox)

    assert terminals.send_input(
        "base-source",
        "refresh",
        registry=registry,
        sender_id="caller",
        orchestration_type=OrchestrationType.SEND_MESSAGE,
        expect_callback=False,
    )

    send_environment.update_last_active.assert_called_once_with("base-source")
    watcher.has_episode.assert_not_called()
    watcher.record_inbound_task.assert_not_called()
    watcher.record_callback_if_to_caller.assert_not_called()
    create_inbox.assert_not_called()
    send_environment.plugin_dispatch.assert_called_once()
    event = send_environment.plugin_dispatch.call_args.args[2]
    assert event.sender == "caller"
    assert event.receiver == "base-source"
    assert event.orchestration_type == OrchestrationType.SEND_MESSAGE


@pytest.mark.parametrize("callback_seen", [False, True])
def test_f11_refresh_keeps_existing_episode_unchanged(
    monkeypatch, send_environment, callback_seen
):
    watcher = watchdog_module.stalled_callback_watchdog
    watcher.clear_terminal("base-source")
    watcher.record_inbound_task("base-source", "caller", "codex_base")
    try:
        with watcher._lock:
            episode = watcher._episodes["base-source"]
            episode.callback_seen = callback_seen
            episode.idle_since = 0.0
            episode.last_screen_fp = "stable"

        terminals.send_input(
            "base-source",
            "refresh",
            sender_id="caller",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
            expect_callback=False,
        )

        with watcher._lock:
            assert watcher._episodes["base-source"] is episode
            assert episode.callback_seen is callback_seen
        if callback_seen:
            monkeypatch.setattr(
                watchdog_module, "get_terminal_metadata", lambda _id: send_environment.metadata
            )
            assert watcher.collect_due_notifications(now=watcher.grace_seconds + 1) == []
    finally:
        watcher.clear_terminal("base-source")


@pytest.mark.parametrize(
    ("orchestration_type", "sender_id", "has_episode", "should_arm"),
    [
        (OrchestrationType.ASSIGN, "caller", False, True),
        (OrchestrationType.SEND_MESSAGE, "caller", True, True),
        (OrchestrationType.SEND_MESSAGE, "peer", True, False),
    ],
)
def test_f11_default_callback_expectation_preserves_normal_arming(
    monkeypatch,
    send_environment,
    orchestration_type,
    sender_id,
    has_episode,
    should_arm,
):
    watcher = MagicMock()
    watcher.has_episode.return_value = has_episode
    monkeypatch.setattr(watchdog_module, "stalled_callback_watchdog", watcher)

    terminals.send_input(
        "base-source",
        "task",
        sender_id=sender_id,
        orchestration_type=orchestration_type,
    )

    if should_arm:
        watcher.record_inbound_task.assert_called_once_with(
            "base-source", "caller", "codex_base"
        )
    else:
        watcher.record_inbound_task.assert_not_called()

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.grok_cli import GrokCliProvider
from cli_agent_orchestrator.services import provider_rebind_service as service
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService
from cli_agent_orchestrator.services.inbox_service import InboxService, get_delivery_lock
from cli_agent_orchestrator.services.rebind_lease import (
    acquire_rebind_lease,
    release_rebind_lease,
    validate_rebind_lease,
)
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


def test_same_id_rebind_advances_lifecycle_generation(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.clients.database import Base, TerminalModel

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    with sessions.begin() as db:
        db.add(
            TerminalModel(
                id="worker",
                tmux_session="cao-test",
                tmux_window="worker",
                provider="codex",
                lifecycle_generation=1,
            )
        )
    assert database.settle_terminal_rebound("worker", "session", "zsh") == 2
    with sessions() as db:
        assert db.query(TerminalModel).filter_by(id="worker").one().lifecycle_generation == 2


def test_rebind_lease_is_non_reentrant_and_generation_bound():
    first = acquire_rebind_lease("lease-a")
    assert first is not None
    assert acquire_rebind_lease("lease-a") is None
    with pytest.raises(RuntimeError):
        validate_rebind_lease("lease-b", first)
    release_rebind_lease(first)
    second = acquire_rebind_lease("lease-a")
    assert second is not None and second.generation > first.generation
    with pytest.raises(RuntimeError):
        validate_rebind_lease("lease-a", first)
    release_rebind_lease(second)


@pytest.mark.asyncio
async def test_delivery_guard_cancelled_while_waiting_does_not_orphan_lock(monkeypatch):
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(service, "get_delivery_lock", lambda _terminal_id: lock)
    guard = service.DeliveryGuard("guard-a", asyncio.get_running_loop())
    task = asyncio.create_task(guard.acquire())
    await asyncio.sleep(0.02)
    task.cancel()
    lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.asyncio
async def test_duplicate_rebind_returns_deterministic_busy(monkeypatch):
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: {"tmux_session": "cao-test"})
    held = acquire_rebind_lease("busy-a")
    try:
        result = await service.rebind_terminal("busy-a")
    finally:
        release_rebind_lease(held)
    assert result["status"] == "skipped_busy"
    assert result["error_code"] == "rebind_in_progress"
    assert result["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "interrupt", "allowed"),
    [
        (TerminalStatus.IDLE, False, True),
        (TerminalStatus.COMPLETED, False, True),
        (TerminalStatus.PROCESSING, False, False),
        (TerminalStatus.PROCESSING, True, True),
        (TerminalStatus.UNKNOWN, True, False),
        (TerminalStatus.WAITING_USER_ANSWER, True, False),
    ],
)
async def test_quiescence_policy(monkeypatch, raw, interrupt, allowed):
    monkeypatch.setattr(
        service,
        "get_terminal_metadata",
        lambda _tid: {
            "id": "q-a",
            "recovery_state": None,
            "shell_command": None,
            "tmux_session": "cao-test",
        },
    )
    monkeypatch.setattr(service, "has_unsettled_delivery_attempt", lambda _tid: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.has_deferred_init",
        lambda _tid: False,
    )
    monkeypatch.setattr(service.status_monitor, "get_raw_status", lambda _tid: raw)
    monkeypatch.setattr(service.provider_manager, "get_provider", lambda _tid: None)
    monkeypatch.setattr(service.DeliveryGuard, "acquire", AsyncMock())
    monkeypatch.setattr(service.DeliveryGuard, "close", AsyncMock())
    result = await service.rebind_terminal("q-a", interrupt=interrupt)
    if allowed:
        assert result["status"] == "unresumable"
        assert result["error_code"] == "provider_unsupported"
    else:
        assert result["status"] == "skipped_busy"
    assert result["interrupted_turn"] is interrupt
    assert result["requires_supervisor_reconciliation"] is interrupt


@pytest.mark.asyncio
async def test_fleet_runs_stable_one_at_a_time_and_manifest_failure_is_separate(monkeypatch):
    monkeypatch.setattr(
        service,
        "list_terminals_by_session",
        lambda _name: [
            {"id": "b", "provider": "codex", "recovery_state": None},
            {"id": "excluded", "provider": "codex", "recovery_state": "fallback_ready"},
            {"id": "a", "provider": "codex", "recovery_state": "rebound"},
        ],
    )
    seen = []

    async def fake_rebind(terminal_id, interrupt=False, acknowledge_ownership=False):
        seen.append(terminal_id)
        return service._result(terminal_id, "rebound", interrupt=interrupt)

    monkeypatch.setattr(service, "rebind_terminal", fake_rebind)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_manifest_service.build_session_manifest",
        MagicMock(side_effect=RuntimeError("manifest-broken")),
    )
    result = await service.recover_provider_reauth("cao-test")
    assert seen == ["a", "b"]
    assert [row["status"] for row in result["results"]] == ["rebound", "rebound"]
    assert result["manifest_error"] == "manifest-broken"


@pytest.mark.asyncio
async def test_fleet_eligibility_is_exact_and_promotes_abandoned_midstate(monkeypatch):
    rows = [
        {"id": "null", "provider": "codex", "recovery_state": None},
        {"id": "rebound", "provider": "codex", "recovery_state": "rebound"},
        {"id": "failed", "provider": "codex", "recovery_state": "rebind_failed"},
        {"id": "mid", "provider": "codex", "recovery_state": "rebind_exiting"},
        {"id": "live-mid", "provider": "codex", "recovery_state": "rebind_starting"},
        {"id": "fallback", "provider": "codex", "recovery_state": "fallback_ready"},
        {"id": "unknown", "provider": "codex", "recovery_state": "future_state"},
    ]
    monkeypatch.setattr(service, "list_terminals_by_session", lambda _name: rows)
    promoted = []
    monkeypatch.setattr(
        service,
        "set_terminal_recovery_state",
        lambda tid, state, error=None: promoted.append((tid, state, error)) or True,
    )
    seen = []

    async def fake_rebind(terminal_id, interrupt=False, acknowledge_ownership=False):
        seen.append(terminal_id)
        return service._result(terminal_id, "rebound")

    monkeypatch.setattr(service, "rebind_terminal", fake_rebind)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_manifest_service.build_session_manifest",
        lambda _name: {},
    )
    held = acquire_rebind_lease("live-mid")
    try:
        await service.recover_provider_reauth("cao-test")
    finally:
        release_rebind_lease(held)
    assert seen == ["failed", "mid", "null", "rebound"]
    assert promoted == [("mid", "rebind_failed", "abandoned_mid_rebind")]


@pytest.mark.asyncio
async def test_wpd1_fleet_forwards_content_options_without_changing_legacy_calls(monkeypatch):
    monkeypatch.setattr(
        service,
        "list_terminals_by_session",
        lambda _name: [{"id": "worker", "provider": "codex", "recovery_state": None}],
    )
    calls = []

    async def fake_rebind(terminal_id, **kwargs):
        calls.append((terminal_id, kwargs))
        return service._result(terminal_id, "rebound")

    monkeypatch.setattr(service, "rebind_terminal", fake_rebind)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_manifest_service.build_session_manifest",
        lambda _name: {},
    )
    result = await service.recover_provider_reauth(
        "cao-test",
        reason="content-flag",
        content_options={"show": True, "force": False},
    )
    assert result["reason"] == "content-flag"
    assert calls == [("worker", {
        "interrupt": False,
        "acknowledge_ownership": False,
        "reason": "content-flag",
        "content_options": {"show": True, "force": False},
    })]


@pytest.mark.asyncio
async def test_tmux_backend_proof_uses_preexisting_fifo_without_reregistration(monkeypatch):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fifo_reader.fifo_manager.has_reader",
        lambda _tid: True,
    )
    generations = iter([10, 11])
    monkeypatch.setattr(
        service.status_monitor, "get_fifo_frame_gen", lambda _tid: next(generations)
    )
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    await service._wait_for_backend_proof("term", {}, MagicMock(), 10)
    backend.pipe_pane.assert_not_called()


@pytest.mark.asyncio
async def test_tmux_backend_proof_rejects_missing_existing_reader(monkeypatch):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fifo_reader.fifo_manager.has_reader",
        lambda _tid: False,
    )
    with pytest.raises(RuntimeError, match="fifo_reader_missing"):
        await service._wait_for_backend_proof("term", {}, MagicMock(), 0)


def test_fifo_frame_generation_ignores_input_and_reset(monkeypatch):
    monitor = StatusMonitor()
    provider = MagicMock(supports_screen_detection=False)
    provider.get_status.return_value = TerminalStatus.IDLE
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: provider,
    )
    assert monitor.get_fifo_frame_gen("term") == 0
    monitor.notify_input_sent("term")
    monitor.reset_buffer("term")
    assert monitor.get_fifo_frame_gen("term") == 0
    monitor._process_chunk("term", "real fifo bytes")
    assert monitor.get_fifo_frame_gen("term") == 1


@pytest.mark.asyncio
async def test_tmux_proof_waits_for_real_process_chunk_frame(monkeypatch):
    monitor = StatusMonitor()
    backend = MagicMock()
    backend.supports_event_inbox.return_value = False
    provider = MagicMock(supports_screen_detection=False)
    provider.get_status.return_value = TerminalStatus.IDLE
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service, "status_monitor", monitor)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fifo_reader.fifo_manager.has_reader",
        lambda _tid: True,
    )
    task = asyncio.create_task(service._wait_for_backend_proof("term", {}, provider, 0))
    await asyncio.sleep(0)
    assert not task.done()
    monitor._process_chunk("term", "candidate frame")
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_herdr_backend_proof_requires_new_native_event_and_both_maps(monkeypatch):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    backend.get_pane_id.return_value = "pane-new"
    inbox = MagicMock()
    inbox._terminal_to_pane = {}
    inbox._pane_to_terminal = {}
    generations = iter([4, 5])
    inbox.get_native_event_gen.side_effect = lambda _tid, _pane: next(generations)

    def register(terminal_id, pane_id, _is_kiro):
        inbox._terminal_to_pane[terminal_id] = pane_id
        inbox._pane_to_terminal[pane_id] = terminal_id

    inbox.register_terminal.side_effect = register
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_registry.get_herdr_inbox_service",
        lambda: inbox,
    )
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    await service._wait_for_backend_proof(
        "term", {"tmux_session": "s", "tmux_window": "w"}, MagicMock(), 0
    )
    inbox.register_terminal.assert_called_once_with("term", "pane-new", False)


@pytest.mark.asyncio
async def test_herdr_backend_proof_registers_under_real_held_delivery_guard(monkeypatch):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    backend.get_pane_id.return_value = "pane-new"
    inbox = HerdrInboxService(socket_path="/tmp/test-real-guard.sock")
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_registry.get_herdr_inbox_service",
        lambda: inbox,
    )
    guard = service.DeliveryGuard("term", asyncio.get_running_loop())
    await guard.acquire()
    try:
        proof = asyncio.create_task(
            service._wait_for_backend_proof(
                "term",
                {"tmux_session": "s", "tmux_window": "w"},
                MagicMock(),
                0,
                guard,
            )
        )
        await asyncio.sleep(0)
        assert inbox._terminal_to_pane["term"] == "pane-new"
        assert inbox._pane_to_terminal["pane-new"] == "term"
        with inbox._identity_guard:
            inbox._native_event_gen[("term", "pane-new")] = 1
        await asyncio.wait_for(proof, timeout=0.5)
    finally:
        await guard.close()
    assert guard.active is False


@pytest.mark.asyncio
async def test_herdr_proof_waits_for_exact_new_pane_native_event(monkeypatch):
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    backend.get_pane_id.return_value = "pane-new"
    inbox = HerdrInboxService(socket_path="/tmp/test.sock")
    inbox.register_terminal("term", "pane-old")
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.herdr_inbox_registry.get_herdr_inbox_service",
        lambda: inbox,
    )
    metadata = {"tmux_session": "s", "tmux_window": "w"}
    task = asyncio.create_task(service._wait_for_backend_proof("term", metadata, MagicMock(), 0))
    await asyncio.sleep(0)
    assert not task.done()
    event = (
        __import__("json")
        .dumps(
            {
                "event": "pane.agent_status_changed",
                "data": {"pane_id": "pane-new", "agent_status": "working"},
            }
        )
        .encode()
        + b"\n"
    )
    inbox._reader = AsyncMock()
    inbox._reader.readline.side_effect = [event, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await inbox._event_loop()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls", [CodexProvider, GrokCliProvider])
async def test_real_candidate_staged_init_uses_explicit_raw_seams(monkeypatch, provider_cls):
    terminal_id = "staged01"
    candidate = provider_cls(terminal_id, "cao-test", "worker", None)
    old = MagicMock()
    monkeypatch.setattr(service.provider_manager, "get_provider", lambda _tid: old)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _tid: {"recovery_state": "rebind_exiting"},
    )
    backend = MagicMock()
    backend.get_pane_current_command.return_value = "bash"
    monkeypatch.setattr(
        f"cli_agent_orchestrator.providers.{provider_cls.__module__.rsplit('.', 1)[-1]}.get_backend",
        lambda: backend,
    )
    module = __import__(provider_cls.__module__, fromlist=["x"])
    shell_wait = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "wait_for_shell", shell_wait)
    if hasattr(module, "asyncio"):
        monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    if provider_cls is CodexProvider:
        monkeypatch.setattr(candidate, "_build_codex_command", lambda: "codex resume uuid")
        monkeypatch.setattr(candidate, "_handle_trust_prompt", AsyncMock())
    else:
        monkeypatch.setattr(candidate, "_build_grok_command", lambda: "grok --resume uuid")
    raw_calls = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.status_monitor.get_backend",
        lambda: backend,
        raising=False,
    )
    monkeypatch.setattr(
        __import__(
            "cli_agent_orchestrator.services.status_monitor", fromlist=["status_monitor"]
        ).status_monitor,
        "get_raw_status",
        lambda tid, provider_override=None: raw_calls.append((tid, provider_override))
        or TerminalStatus.IDLE,
    )
    assert service.status_monitor.get_status(terminal_id) == TerminalStatus.ERROR
    assert await candidate.initialize(
        coordinates=("cao-test", "worker"), provider_override=candidate, raw_status=True
    )
    shell_wait.assert_awaited_once()
    assert shell_wait.call_args.kwargs["coordinates"] == ("cao-test", "worker")
    assert raw_calls == [(terminal_id, candidate)]


def test_real_monitor_projected_error_diverges_from_candidate_raw(monkeypatch):
    monitor = StatusMonitor()
    candidate = MagicMock()
    candidate.get_status.return_value = TerminalStatus.IDLE
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _tid: {"recovery_state": "rebind_failed"},
    )
    assert monitor.get_status("term") == TerminalStatus.ERROR
    assert monitor.get_raw_status("term", provider_override=candidate) == TerminalStatus.IDLE


@pytest.mark.asyncio
async def test_transaction_p12_uses_real_raw_monitor_under_error_overlay(monkeypatch):
    old, candidate, _states = _install_transaction_harness(monkeypatch)
    monitor = StatusMonitor()
    state = {"value": None}
    backend = MagicMock()
    backend.supports_event_inbox.return_value = True
    backend.get_pane_working_directory.return_value = "/tmp"
    old.get_status.return_value = TerminalStatus.IDLE
    candidate.get_status.return_value = TerminalStatus.IDLE
    candidate.initialize = AsyncMock()
    metadata = {
        "id": "txn",
        "recovery_state": None,
        "shell_command": "bash",
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    monkeypatch.setattr(service, "status_monitor", monitor)
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry.get_backend", lambda: backend)
    monkeypatch.setattr(
        service, "get_terminal_metadata", lambda _tid: metadata | {"recovery_state": state["value"]}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda _tid: {"recovery_state": state["value"]},
    )
    monkeypatch.setattr(
        service,
        "set_terminal_recovery_state",
        lambda _tid, value, error=None, **_kw: state.__setitem__("value", value) or True,
    )
    monkeypatch.setattr(
        service,
        "settle_terminal_rebound",
        lambda *_a: state.__setitem__("value", "rebound") or True,
    )
    monkeypatch.setattr(service, "_wait_for_backend_proof", AsyncMock())
    result = await service.rebind_terminal("txn")
    assert result["status"] == "rebound", result
    assert state["value"] == "rebound"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_marker", "expected"), [(object(), "exit_failed"), (None, "exit_uncertain")]
)
async def test_exit_classification_distinguishes_live_provider(monkeypatch, live_marker, expected):
    backend = MagicMock()
    backend.get_pane_current_command.return_value = "codex"
    provider = MagicMock()
    provider.provider_process_started_at.return_value = live_marker
    ticks = iter([0.0, 0.0, 16.0])
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(service.asyncio, "sleep", AsyncMock())
    metadata = {"tmux_session": "s", "tmux_window": "w"}
    assert await service._wait_for_shell_baseline(metadata, "bash", provider, 123) == expected


def _install_transaction_harness(monkeypatch, *, pause_error=None, resume_error=None):
    metadata = {
        "id": "txn",
        "recovery_state": None,
        "shell_command": "bash",
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    old = MagicMock(supports_reauth_rebind=True)
    old.validate_session_artifact.return_value = True
    candidate = MagicMock()
    candidate.initialize = AsyncMock(return_value=True)
    states = []
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    monkeypatch.setattr(service, "has_unsettled_delivery_attempt", lambda _tid: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.has_deferred_init", lambda _tid: False
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli", lambda _tid: None
    )
    monkeypatch.setattr(
        service.status_monitor, "get_raw_status", lambda *_a, **_k: TerminalStatus.IDLE
    )
    monkeypatch.setattr(service.status_monitor, "reset_buffer", lambda _tid: None)
    monkeypatch.setattr(service.status_monitor, "get_fifo_frame_gen", lambda _tid: 1)
    monkeypatch.setattr(service.provider_manager, "get_provider", lambda _tid: old)
    monkeypatch.setattr(service.provider_manager, "construct_provider", lambda *_a, **_k: candidate)
    monkeypatch.setattr(service.provider_manager, "commit_provider", lambda *_a, **_k: old)
    monkeypatch.setattr(service, "pane_pid", lambda *_a: 123)
    monkeypatch.setattr(service, "pane_launch_epoch", lambda _pid: 1.0)
    monkeypatch.setattr(service, "_launch_context", lambda _meta: None)
    monkeypatch.setattr(
        service, "_wait_for_shell_baseline", AsyncMock(return_value="exit_confirmed")
    )
    monkeypatch.setattr(service, "_wait_for_backend_proof", AsyncMock())
    monkeypatch.setattr(service, "settle_terminal_rebound", lambda *_a: True)
    monkeypatch.setattr(
        service,
        "fail_terminal_rebound",
        lambda _tid, _generation, error: states.append(("rebind_failed", error)) or 0,
    )
    monkeypatch.setattr(service, "_fallback", AsyncMock(return_value={"status": "respawned"}))
    monkeypatch.setattr(
        service,
        "set_terminal_recovery_state",
        lambda _tid, state, error=None, **_kw: states.append((state, error)) or True,
    )
    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/tmp"
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service.DeliveryGuard, "acquire", AsyncMock())
    monkeypatch.setattr(service.DeliveryGuard, "close", AsyncMock())
    if pause_error:
        monkeypatch.setattr(
            service.stalled_callback_watchdog, "pause_terminal", MagicMock(side_effect=pause_error)
        )
    else:
        monkeypatch.setattr(
            service.stalled_callback_watchdog, "pause_terminal", lambda _tid: (None, 0.0)
        )
    if resume_error:
        monkeypatch.setattr(
            service.stalled_callback_watchdog,
            "resume_terminal",
            MagicMock(side_effect=resume_error),
        )
    else:
        monkeypatch.setattr(service.stalled_callback_watchdog, "resume_terminal", lambda *_a: None)
    return old, candidate, states


@pytest.mark.asyncio
async def test_wpd1_scrub_runs_after_proven_death_and_before_candidate_initialize(monkeypatch):
    _old, candidate, _states = _install_transaction_harness(monkeypatch)
    from cli_agent_orchestrator.services import wpd1_decontam

    metadata = {
        "id": "txn",
        "recovery_state": None,
        "shell_command": "bash",
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
        "lifecycle_generation": 4,
        "caller_mailbox_id": "mb_owner",
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_current_mailbox_terminal",
        lambda _mailbox: "caller-live",
    )
    events = []
    prepared = SimpleNamespace()
    monkeypatch.setattr(
        wpd1_decontam,
        "prepare_content_recovery",
        lambda **kwargs: events.append(("scrub", kwargs)) or prepared,
    )
    candidate.initialize = AsyncMock(side_effect=lambda **_kwargs: events.append(("initialize", {})))
    monkeypatch.setattr(wpd1_decontam, "mark_recovery_complete", lambda _p: events.append(("complete", {})))
    monkeypatch.setattr(wpd1_decontam, "release_prepared_recovery", lambda _p: None)
    monkeypatch.setattr(
        wpd1_decontam,
        "public_scrub_summary",
        lambda _p, show: {"incident_path": "/tmp/incident", "show": show},
    )
    insert = MagicMock()
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.create_inbox_message", insert)

    result = await service.rebind_terminal(
        "txn",
        reason="content-flag",
        content_options={"show": True, "force": False, "use_cpa": False},
    )

    assert result["status"] == "rebound"
    assert [name for name, _details in events] == ["scrub", "initialize", "complete"]
    assert events[0][1]["caller_terminal_id"] == "caller-live"
    assert result["decontamination"]["show"] is True
    insert.assert_not_called()


@pytest.mark.asyncio
async def test_wpd1_resume_failure_disables_unsanitized_fallback(monkeypatch):
    _old, candidate, _states = _install_transaction_harness(monkeypatch)
    from cli_agent_orchestrator.services import wpd1_decontam

    metadata = {
        "id": "txn", "recovery_state": None, "shell_command": "bash",
        "provider_session_id": "uuid", "provider": "codex", "tmux_session": "cao-test",
        "tmux_window": "worker", "agent_profile": "dev", "allowed_tools": None,
        "lifecycle_generation": 4, "caller_mailbox_id": None,
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    prepared = SimpleNamespace()
    monkeypatch.setattr(wpd1_decontam, "prepare_content_recovery", lambda **_kwargs: prepared)
    monkeypatch.setattr(wpd1_decontam, "release_prepared_recovery", lambda _p: None)
    monkeypatch.setattr(wpd1_decontam, "post_initialize_failure", lambda *_a: True)
    monkeypatch.setattr(
        wpd1_decontam, "public_scrub_summary",
        lambda _p, show: {"incident_path": "/tmp/incident"},
    )
    candidate.initialize.side_effect = RuntimeError("candidate failed")
    fallback = AsyncMock()
    monkeypatch.setattr(service, "_fallback", fallback)

    result = await service.rebind_terminal("txn", reason="content-flag")

    assert result["status"] == "resume_failed"
    assert result["fallback"] is None
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_wpd1_watchdog_resume_failure_runs_post_append_authority_law(monkeypatch):
    _old, _candidate, _states = _install_transaction_harness(
        monkeypatch, resume_error=RuntimeError("watchdog")
    )
    from cli_agent_orchestrator.services import wpd1_decontam

    metadata = {
        "id": "txn", "recovery_state": None, "shell_command": "bash",
        "provider_session_id": "uuid", "provider": "codex", "tmux_session": "cao-test",
        "tmux_window": "worker", "agent_profile": "dev", "allowed_tools": None,
        "lifecycle_generation": 4, "caller_mailbox_id": None,
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    prepared = SimpleNamespace()
    monkeypatch.setattr(wpd1_decontam, "prepare_content_recovery", lambda **_kw: prepared)
    monkeypatch.setattr(wpd1_decontam, "release_prepared_recovery", lambda _p: None)
    post = MagicMock(return_value=True)
    monkeypatch.setattr(wpd1_decontam, "post_initialize_failure", post)
    monkeypatch.setattr(wpd1_decontam, "mark_recovery_failure", MagicMock())
    monkeypatch.setattr(
        wpd1_decontam, "public_scrub_summary",
        lambda _p, show: {"incident_path": "/tmp/incident"},
    )

    result = await service.rebind_terminal("txn", reason="content-flag")

    assert result["status"] == "resume_failed"
    assert result["error_code"] == "watchdog_resume_failed"
    post.assert_called_once_with(prepared, "settle")


@pytest.mark.asyncio
async def test_wpd1_guard_release_error_still_closes_incident_complete(monkeypatch):
    _old, _candidate, _states = _install_transaction_harness(monkeypatch)
    from cli_agent_orchestrator.services import wpd1_decontam

    metadata = {
        "id": "txn", "recovery_state": None, "shell_command": "bash",
        "provider_session_id": "uuid", "provider": "codex", "tmux_session": "cao-test",
        "tmux_window": "worker", "agent_profile": "dev", "allowed_tools": None,
        "lifecycle_generation": 4, "caller_mailbox_id": None,
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    prepared = SimpleNamespace()
    monkeypatch.setattr(wpd1_decontam, "prepare_content_recovery", lambda **_kw: prepared)
    monkeypatch.setattr(wpd1_decontam, "release_prepared_recovery", lambda _p: None)
    complete = MagicMock()
    monkeypatch.setattr(wpd1_decontam, "mark_recovery_complete", complete)
    monkeypatch.setattr(
        wpd1_decontam, "public_scrub_summary",
        lambda _p, show: {"incident_path": "/tmp/incident"},
    )
    monkeypatch.setattr(
        service.DeliveryGuard, "close", AsyncMock(side_effect=[RuntimeError("close"), None])
    )

    result = await service.rebind_terminal("txn", reason="content-flag")

    assert result["status"] == "rebound"
    assert result["error_code"] == "delivery_guard_release_failed"
    complete.assert_called_once_with(prepared)


@pytest.mark.asyncio
async def test_wpq11_reactivation_wake_runs_only_after_delivery_guard_release(monkeypatch):
    _old, _candidate, _states = _install_transaction_harness(monkeypatch)
    order: list[str] = []

    def settle(*_args):
        order.append("settle")
        return 1

    async def close(_self):
        order.append("guard-close")

    def deliver(terminal_id):
        order.append(f"deliver:{terminal_id}")

    monkeypatch.setattr(service, "settle_terminal_rebound", settle)
    monkeypatch.setattr(service.DeliveryGuard, "close", close)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        deliver,
    )

    result = await service.rebind_terminal("txn")

    assert result["status"] == "rebound"
    assert order == ["settle", "guard-close", "deliver:txn"]


@pytest.mark.asyncio
async def test_p6_pause_failure_restores_p1_state_without_exit(monkeypatch):
    _old, _candidate, states = _install_transaction_harness(
        monkeypatch, pause_error=RuntimeError("pause")
    )
    exit_cli = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli", exit_cli
    )
    result = await service.rebind_terminal("txn")
    assert result["error_code"] == "watchdog_pause_failed"
    assert states[-1] == (None, None)
    exit_cli.assert_not_called()


@pytest.mark.asyncio
async def test_capture_failed_vs_unresumable_are_p4_only(monkeypatch):
    old, _candidate, _states = _install_transaction_harness(monkeypatch)
    base = {
        "id": "txn",
        "recovery_state": None,
        "shell_command": "bash",
        "provider_session_id": None,
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: base.copy())
    old.capture_session_uuid.side_effect = RuntimeError("transient")
    captured = await service.rebind_terminal("txn")
    assert captured["status"] == "capture_failed"
    assert captured["retryable"] is True

    old.capture_session_uuid.side_effect = None
    old.capture_session_uuid.return_value = "uuid"
    old.validate_session_artifact.side_effect = ValueError("identity invalid")
    invalid = await service.rebind_terminal("txn")
    assert invalid["status"] == "unresumable"
    assert invalid["retryable"] is False


@pytest.mark.asyncio
async def test_abandoned_mid_rebind_promotes_when_lease_free(monkeypatch):
    _old, _candidate, states = _install_transaction_harness(monkeypatch)
    metadata = {
        "id": "txn",
        "recovery_state": "rebind_starting",
        "shell_command": None,
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    result = await service.rebind_terminal("txn")
    assert result["status"] == "unresumable"
    assert states[0] == ("rebind_failed", "abandoned_mid_rebind")


@pytest.mark.asyncio
async def test_p14_resume_failure_demotes_proven_candidate_without_fallback(monkeypatch):
    _old, _candidate, states = _install_transaction_harness(
        monkeypatch, resume_error=RuntimeError("resume watchdog")
    )
    fallback = AsyncMock()
    deliver = MagicMock()
    monkeypatch.setattr(service, "_fallback", fallback)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        deliver,
    )
    result = await service.rebind_terminal("txn")
    assert result["status"] == "resume_failed"
    assert result["error_code"] == "watchdog_resume_failed"
    assert states[-1] == ("rebind_failed", "watchdog_resume_failed")
    fallback.assert_not_awaited()
    deliver.assert_not_called()


def _install_real_reactivated_row(monkeypatch, tmp_path):
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.clients.database import Base, InboxModel, TerminalModel
    from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType

    engine = create_engine(
        f"sqlite:///{tmp_path / 'p14-repark.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(service, "settle_terminal_rebound", database.settle_terminal_rebound)
    monkeypatch.setattr(service, "fail_terminal_rebound", database.fail_terminal_rebound)
    with sessions.begin() as db:
        db.add(
            TerminalModel(
                id="txn",
                tmux_session="cao-test",
                tmux_window="worker",
                provider="codex",
                lifecycle_generation=3,
                init_state="ready",
            )
        )
        row = InboxModel(
            sender_id="99999999",
            receiver_id="txn",
            enqueue_generation=3,
            owner_receiver_id="txn",
            owner_generation=3,
            message="owned callback",
            orchestration_type=OrchestrationType.SEND_MESSAGE.value,
            status=MessageStatus.PARKED.value,
            created_at=datetime.now(),
        )
        db.add(row)
        db.flush()
        message_id = int(row.id)
    return engine, sessions, message_id


@pytest.mark.asyncio
async def test_p14_failure_atomically_reparks_reactivated_rows_without_wake(monkeypatch, tmp_path):
    _install_transaction_harness(monkeypatch, resume_error=RuntimeError("resume watchdog"))
    engine, sessions, message_id = _install_real_reactivated_row(monkeypatch, tmp_path)
    deliver = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        deliver,
    )

    result = await service.rebind_terminal("txn")

    assert result["status"] == "resume_failed"
    deliver.assert_not_called()
    with sessions() as db:
        from cli_agent_orchestrator.clients.database import InboxModel, TerminalModel
        from cli_agent_orchestrator.models.inbox import MessageStatus

        assert db.get(InboxModel, message_id).status == MessageStatus.PARKED.value
        terminal = db.get(TerminalModel, "txn")
        assert (terminal.recovery_state, terminal.recovery_error) == (
            "rebind_failed",
            "watchdog_resume_failed",
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_p14_cancellation_reparks_reactivated_rows_without_wake(monkeypatch, tmp_path):
    _install_transaction_harness(monkeypatch)
    engine, sessions, message_id = _install_real_reactivated_row(monkeypatch, tmp_path)
    resume = MagicMock(side_effect=[asyncio.CancelledError(), None])
    deliver = MagicMock()
    monkeypatch.setattr(service.stalled_callback_watchdog, "resume_terminal", resume)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        deliver,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.rebind_terminal("txn")

    deliver.assert_not_called()
    with sessions() as db:
        from cli_agent_orchestrator.clients.database import InboxModel, TerminalModel
        from cli_agent_orchestrator.models.inbox import MessageStatus

        assert db.get(InboxModel, message_id).status == MessageStatus.PARKED.value
        terminal = db.get(TerminalModel, "txn")
        assert (terminal.recovery_state, terminal.recovery_error) == (
            "rebind_failed",
            "rebind_cancelled",
        )
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_status", "expected_error", "fallback_expected", "exit_count", "retryable"),
    [
        ("p2", "resume_failed", "p2_request_failed", False, 0, True),
        ("p5", "resume_failed", "p5_request_failed", False, 0, True),
        ("p7_persist", "resume_failed", "p7_request_failed", False, 0, True),
        ("p7_send", "resume_failed", "exit_uncertain", False, 1, False),
        ("p7_death_raise", "resume_failed", "exit_uncertain", False, 1, False),
        ("p8", "resume_failed", "resume_failed", True, 1, True),
        ("p9", "resume_failed", "resume_failed", True, 2, True),
        ("p10", "resume_failed", "resume_failed", True, 2, True),
        ("p11", "resume_failed", "resume_failed", True, 2, True),
        ("p12", "resume_failed", "resume_failed", True, 2, True),
        ("p13", "resume_failed", "resume_failed", True, 2, True),
        ("p15", "rebound", "delivery_guard_release_failed", False, 1, False),
    ],
)
async def test_phase_failure_matrix(
    monkeypatch, phase, expected_status, expected_error, fallback_expected, exit_count, retryable
):
    _old, candidate, states = _install_transaction_harness(monkeypatch)
    exit_cli = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli", exit_cli
    )
    fallback = AsyncMock(return_value={"status": "respawned"})
    monkeypatch.setattr(service, "_fallback", fallback)
    if phase == "p2":
        monkeypatch.setattr(
            service.DeliveryGuard, "acquire", AsyncMock(side_effect=RuntimeError("p2"))
        )
    elif phase in {"p5", "p7_persist"}:
        target = "rebind_starting" if phase == "p5" else "rebind_exiting"
        original = service.set_terminal_recovery_state
        monkeypatch.setattr(
            service,
            "set_terminal_recovery_state",
            lambda tid, state, error=None, **kw: (
                False if state == target else original(tid, state, error, **kw)
            ),
        )
    elif phase == "p7_send":
        exit_cli.side_effect = RuntimeError("raised after backend send")
    elif phase == "p7_death_raise":
        monkeypatch.setattr(
            service,
            "_wait_for_shell_baseline",
            AsyncMock(side_effect=RuntimeError("pane probe failed")),
        )
    elif phase == "p8":
        monkeypatch.setattr(
            service.provider_manager,
            "construct_provider",
            MagicMock(side_effect=RuntimeError("p8")),
        )
    elif phase == "p9":
        candidate.initialize.side_effect = RuntimeError("p9")
    elif phase == "p10":
        monkeypatch.setattr(
            service.provider_manager, "commit_provider", MagicMock(side_effect=RuntimeError("p10"))
        )
    elif phase == "p11":
        monkeypatch.setattr(
            service, "_wait_for_backend_proof", AsyncMock(side_effect=RuntimeError("p11"))
        )
    elif phase == "p12":
        statuses = iter([TerminalStatus.IDLE, TerminalStatus.UNKNOWN])
        monkeypatch.setattr(
            service.status_monitor, "get_raw_status", lambda *_a, **_kw: next(statuses)
        )
    elif phase == "p13":
        monkeypatch.setattr(service, "settle_terminal_rebound", lambda *_a: False)
    elif phase == "p15":
        monkeypatch.setattr(
            service.DeliveryGuard,
            "close",
            AsyncMock(side_effect=[RuntimeError("p15"), None]),
        )
    result = await service.rebind_terminal("txn")
    assert result["status"] == expected_status
    assert result["error_code"] == expected_error
    assert result["retryable"] is retryable
    assert fallback.await_count == int(fallback_expected)
    assert exit_cli.call_count == exit_count
    assert service.DeliveryGuard.close.await_count >= 1
    reacquired = acquire_rebind_lease("txn")
    assert reacquired is not None
    release_rebind_lease(reacquired)
    if phase in {"p8", "p9", "p10", "p11", "p12", "p13"}:
        assert any(state == "rebind_failed" for state, _error in states)
    if phase == "p7_persist":
        assert states[-1] == (None, None)
    if phase in {"p7_send", "p7_death_raise"}:
        assert states[-1] == ("rebind_failed", "exit_uncertain")
    if phase == "p15":
        assert service.DeliveryGuard.close.await_count == 2
        assert not any(state == "rebind_failed" for state, _error in states)


@pytest.mark.asyncio
@pytest.mark.parametrize("death", ["exit_failed", "exit_uncertain"])
async def test_p7_death_poll_codes_are_nonretryable_after_exit_send(monkeypatch, death):
    _old, _candidate, states = _install_transaction_harness(monkeypatch)
    exit_cli = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli", exit_cli
    )
    monkeypatch.setattr(service, "_wait_for_shell_baseline", AsyncMock(return_value=death))
    result = await service.rebind_terminal("txn")
    assert result["status"] == "resume_failed"
    assert result["error_code"] == death
    assert result["retryable"] is False
    assert exit_cli.call_count == 1
    assert states[-1] == ("rebind_failed", death)


@pytest.mark.asyncio
async def test_p7_post_send_exception_and_retry_emit_exactly_one_exit(monkeypatch):
    _old, _candidate, _states = _install_transaction_harness(monkeypatch)
    durable = {"state": None, "error": None}
    base = {
        "id": "txn",
        "shell_command": "bash",
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    monkeypatch.setattr(
        service,
        "get_terminal_metadata",
        lambda _tid: base
        | {"recovery_state": durable["state"], "recovery_error": durable["error"]},
    )

    def persist(_tid, state, error=None, **_kwargs):
        durable.update(state=state, error=error)
        return True

    monkeypatch.setattr(service, "set_terminal_recovery_state", persist)
    sends = []

    def exit_after_send(_tid):
        sends.append("/exit")
        raise RuntimeError("post-send bookkeeping failed")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli",
        exit_after_send,
    )
    first = await service.rebind_terminal("txn")
    second = await service.rebind_terminal("txn")
    assert first["error_code"] == second["error_code"] == "exit_uncertain"
    assert first["retryable"] is second["retryable"] is False
    assert durable == {"state": "rebind_failed", "error": "exit_uncertain"}
    assert sends == ["/exit"]


def _install_ownership_state(monkeypatch, error="exit_uncertain"):
    _old, _candidate, _states = _install_transaction_harness(monkeypatch)
    durable = {"state": "rebind_failed", "error": error}
    base = {
        "id": "txn",
        "shell_command": "bash",
        "provider_session_id": "uuid",
        "provider": "codex",
        "tmux_session": "cao-test",
        "tmux_window": "worker",
        "agent_profile": "dev",
        "allowed_tools": None,
    }
    monkeypatch.setattr(
        service,
        "get_terminal_metadata",
        lambda _tid: base
        | {"recovery_state": durable["state"], "recovery_error": durable["error"]},
    )

    def persist(_tid, state, recovery_error=None, **_kwargs):
        durable.update(state=state, error=recovery_error)
        return True

    monkeypatch.setattr(service, "set_terminal_recovery_state", persist)
    return durable


@pytest.mark.asyncio
async def test_acknowledged_ownership_retry_runs_full_machine_to_rebound(monkeypatch):
    durable = _install_ownership_state(monkeypatch)
    result = await service.rebind_terminal("txn", acknowledge_ownership=True)
    assert result["status"] == "rebound"
    assert durable["error"] is None


@pytest.mark.asyncio
async def test_acknowledged_ownership_does_not_bypass_live_quiescence(monkeypatch):
    durable = _install_ownership_state(monkeypatch)
    monkeypatch.setattr(
        service.status_monitor, "get_raw_status", lambda *_a, **_kw: TerminalStatus.PROCESSING
    )
    exit_cli = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.exit_terminal_cli", exit_cli
    )
    result = await service.rebind_terminal("txn", acknowledge_ownership=True)
    assert result["status"] == "skipped_busy"
    assert result["error_code"] == "status_processing"
    assert durable == {"state": "rebind_failed", "error": None}
    exit_cli.assert_not_called()


@pytest.mark.asyncio
async def test_unacknowledged_ownership_retry_remains_fenced(monkeypatch):
    durable = _install_ownership_state(monkeypatch, error="exit_failed")
    result = await service.rebind_terminal("txn")
    assert result["status"] == "resume_failed"
    assert result["error_code"] == "exit_failed"
    assert result["retryable"] is False
    assert durable == {"state": "rebind_failed", "error": "exit_failed"}


@pytest.mark.asyncio
async def test_fleet_recover_cannot_acknowledge_ownership():
    with pytest.raises(ValueError, match="exactly one"):
        await service.recover_provider_reauth(
            "cao-test", acknowledge_ownership=True, terminal_ids=None
        )


@pytest.mark.asyncio
async def test_p15_first_close_failure_resignals_before_lease_release(monkeypatch):
    _install_transaction_harness(monkeypatch)
    events = []

    async def close_twice(_guard):
        count = sum(1 for event in events if event.startswith("close")) + 1
        events.append(f"close{count}")
        if count == 1:
            raise RuntimeError("ack uncertain")

    original_release = service.release_rebind_lease

    def release(token):
        events.append("lease_release")
        original_release(token)

    monkeypatch.setattr(service.DeliveryGuard, "close", close_twice)
    monkeypatch.setattr(service, "release_rebind_lease", release)
    result = await service.rebind_terminal("txn")
    assert result["status"] == "rebound"
    assert result["error_code"] == "delivery_guard_release_failed"
    assert events == ["close1", "close2", "lease_release"]


def test_raw_status_true_has_no_production_caller_outside_rebind():
    root = __import__("pathlib").Path(__file__).resolve().parents[2] / "src"
    callers = []
    for path in root.rglob("*.py"):
        if "raw_status=True" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    assert callers == ["provider_rebind_service.py"]


@pytest.mark.asyncio
async def test_p16_release_failure_is_loud_after_settlement(monkeypatch):
    _install_transaction_harness(monkeypatch)
    original_release = service.release_rebind_lease

    def release_then_fail(token):
        original_release(token)
        raise RuntimeError("p16")

    monkeypatch.setattr(service, "release_rebind_lease", release_then_fail)
    with pytest.raises(RuntimeError, match="p16"):
        await service.rebind_terminal("txn")


@pytest.mark.asyncio
async def test_success_order_is_initialize_then_cas_backend_raw_persist(monkeypatch):
    _old, candidate, _states = _install_transaction_harness(monkeypatch)
    order = []
    candidate.initialize = AsyncMock(side_effect=lambda **_kwargs: order.append("initialize"))
    monkeypatch.setattr(
        service.provider_manager,
        "commit_provider",
        lambda *_a, **_k: order.append("cas"),
    )

    async def backend_proof(*_a):
        order.append("backend")

    monkeypatch.setattr(service, "_wait_for_backend_proof", backend_proof)
    monkeypatch.setattr(
        service.status_monitor,
        "get_raw_status",
        lambda *_a, **kw: (
            order.append("raw-final") or TerminalStatus.IDLE
            if kw.get("provider_override")
            else TerminalStatus.IDLE
        ),
    )
    monkeypatch.setattr(
        service,
        "settle_terminal_rebound",
        lambda *_a: order.append("persist-rebound") or True,
    )
    result = await service.rebind_terminal("txn")
    assert result["status"] == "rebound", result
    assert order == ["initialize", "cas", "backend", "raw-final", "persist-rebound"]


@pytest.mark.asyncio
async def test_candidate_exit_uncertain_blocks_fallback(monkeypatch):
    _old, _candidate, states = _install_transaction_harness(monkeypatch)
    monkeypatch.setattr(
        service, "_wait_for_backend_proof", AsyncMock(side_effect=RuntimeError("proof"))
    )
    waits = AsyncMock(side_effect=["exit_confirmed", "exit_uncertain"])
    monkeypatch.setattr(service, "_wait_for_shell_baseline", waits)
    fallback = AsyncMock()
    monkeypatch.setattr(service, "_fallback", fallback)
    result = await service.rebind_terminal("txn")
    assert result["error_code"] == "exit_uncertain"
    assert result["retryable"] is False
    fallback.assert_not_awaited()
    assert states[-1] == ("rebind_failed", "exit_uncertain")


def test_eager_identity_helper_orders_capture_validate_then_atomic_persist(monkeypatch):
    order = []
    provider = MagicMock(supports_reauth_rebind=True, allocated_session_uuid=None)
    provider.shell_baseline = "bash"
    provider.resume_session_uuid.return_value = None
    provider.capture_session_uuid.side_effect = lambda *_a: order.append("capture") or "uuid"
    provider.validate_session_artifact.side_effect = lambda *_a: order.append("validate")
    monkeypatch.setattr(
        terminal_service,
        "get_terminal_metadata",
        lambda _tid: {"tmux_session": "s", "tmux_window": "w"},
    )
    monkeypatch.setattr(terminal_service, "pane_pid", lambda *_a: 1, raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_pid", lambda *_a: 1
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.fork_context_service.pane_launch_epoch", lambda _pid: 2
    )
    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/work"
    monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.update_terminal_runtime_identity",
        lambda *_a: order.append("persist") or True,
    )
    terminal_service._persist_provider_runtime_identity(provider, "term")
    assert order == ["capture", "validate", "persist"]


def test_public_delete_passes_current_lease_token_to_single_teardown_body(monkeypatch):
    seen = []
    monkeypatch.setattr(
        terminal_service,
        "_delete_terminal_under_lease",
        lambda terminal_id, token, registry=None: seen.append((terminal_id, token)) or True,
    )
    assert terminal_service.delete_terminal("delete-a") is True
    assert seen[0][0] == "delete-a"
    assert seen[0][1] is not None
    assert seen[0][1].terminal_id == "delete-a"


def test_many_terminal_delivery_defers_without_blocking_rebind_work(monkeypatch):
    inbox = InboxService()
    pending_read = MagicMock()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.get_pending_messages", pending_read
    )
    locks = [get_delivery_lock(f"delivery-{index}") for index in range(8)]
    for lock in locks:
        lock.acquire()
    progressed = []
    try:
        for index in range(8):
            inbox.deliver_pending(f"delivery-{index}")
            progressed.append(index)
    finally:
        for lock in locks:
            lock.release()
    assert progressed == list(range(8))
    pending_read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_cas", "after_cas_p11", "p12_p13"])
async def test_delete_is_busy_with_zero_teardown_at_commit_boundaries(monkeypatch, boundary):
    _old, candidate, _states = _install_transaction_harness(monkeypatch)
    teardown = MagicMock()
    monkeypatch.setattr(terminal_service, "_delete_terminal_under_lease", teardown)
    observed = []

    def assert_delete_blocked(label):
        with pytest.raises(RuntimeError, match="rebind_in_progress"):
            terminal_service.delete_terminal("txn")
        observed.append(label)
        teardown.assert_not_called()

    if boundary == "before_cas":

        async def initialize(**_kwargs):
            assert_delete_blocked("before_cas")

        candidate.initialize = initialize
    elif boundary == "after_cas_p11":

        async def backend_proof(*_args):
            assert_delete_blocked("after_cas_p11")

        monkeypatch.setattr(service, "_wait_for_backend_proof", backend_proof)
    else:
        raw_calls = 0

        def raw_status(*_args, **kwargs):
            nonlocal raw_calls
            raw_calls += 1
            if kwargs.get("provider_override"):
                assert_delete_blocked("p12")
            return TerminalStatus.IDLE

        monkeypatch.setattr(service.status_monitor, "get_raw_status", raw_status)

        def settle(*_args):
            assert_delete_blocked("p13")
            return True

        monkeypatch.setattr(service, "settle_terminal_rebound", settle)
    result = await service.rebind_terminal("txn")
    assert result["status"] == "rebound"
    assert observed == (["p12", "p13"] if boundary == "p12_p13" else [boundary])
    teardown.assert_not_called()

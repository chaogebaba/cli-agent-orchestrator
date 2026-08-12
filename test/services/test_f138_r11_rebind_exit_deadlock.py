"""F138 R11 — rebind exit self-deadlock fix.

Proves that exit_terminal_cli can succeed while recovery_state is set to a
blocking value (rebind_exiting / rebind_starting / rebind_failed), and that
public callers remain blocked during recovery.

The old code deadlocked because:
  rebind_terminal persists recovery_state='rebind_exiting' →
  calls exit_terminal_cli → calls send_input → send_input calls
  status_monitor.get_status() → reads recovery_state from DB →
  returns TerminalStatus.ERROR → raises TerminalInputBlockedError.

The fix: exit_terminal_cli passes _lifecycle_internal=True to send_input,
which bypasses the pre-send status check while preserving all other
send_input machinery.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import (
    TerminalInputBlockedError,
    exit_terminal_cli,
    send_input,
    send_special_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TERMINAL_ID = "deadlock-test-001"
METADATA = {
    "id": TERMINAL_ID,
    "tmux_session": "cao-test",
    "tmux_window": "worker-0",
    "provider": "codex",
    "recovery_state": "rebind_exiting",
    "shell_command": "bash",
    "agent_profile": "developer",
    "allowed_tools": None,
}


def _make_provider(exit_command="/exit"):
    """Build a mock provider with configurable exit_cli."""
    provider = MagicMock()
    provider.exit_cli.return_value = exit_command
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.0
    provider.blocks_orchestrated_input_while_waiting_user_answer = False
    provider.composer_stash_keys = None
    return provider


@pytest.fixture
def _mock_infra(monkeypatch):
    """Wire minimal mocks so exit_terminal_cli → send_input reaches the guard."""
    provider = _make_provider()
    backend = MagicMock()
    backend.send_keys.return_value = None
    backend.send_special_key.return_value = None

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
        lambda _tid: METADATA.copy(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_backend",
        lambda: backend,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.notify_input_sent",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.clear_rolling_buffer",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.bind_dispatch_provider",
        lambda _tid, _p: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.begin_dispatch",
        lambda _tid: MagicMock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.commit_dispatch",
        lambda _txn: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.update_last_active",
        lambda _tid: None,
    )
    # Preserve draft / stash — no-op
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.preserve_draft_before_send",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.inject_memory_context",
        lambda msg, _tid: msg,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service._append_message_contract",
        lambda msg, _meta, _orch: msg,
    )
    # Auto-responder mark
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.mark_exit_suppress",
        lambda _tid: None,
    )
    # Fixture override path — not a fixture provider
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service._fixture_send_input_override",
        lambda _p, _m: False,
    )
    return provider, backend


# ---------------------------------------------------------------------------
# Test 1: Old ordering self-deadlocks
# ---------------------------------------------------------------------------

class TestOldOrderingSelfDeadlocks:
    """Prove that without _lifecycle_internal, the status check blocks exit."""

    def test_send_input_blocked_during_recovery_state(self, monkeypatch, _mock_infra):
        """Public send_input MUST raise TerminalInputBlockedError when
        recovery_state is 'rebind_exiting'."""
        provider, _backend = _mock_infra
        # Simulate what get_status returns when recovery_state is blocking
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        with pytest.raises(TerminalInputBlockedError, match="ERROR state"):
            send_input(TERMINAL_ID, "/exit")

    def test_external_user_input_blocked_during_rebind(self, monkeypatch, _mock_infra):
        """External callers (assign, send_message) remain blocked during
        rebind — the fix does NOT weaken the public guard."""
        provider, _backend = _mock_infra
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        with pytest.raises(TerminalInputBlockedError, match="ERROR state"):
            send_input(TERMINAL_ID, "do some work", _lifecycle_internal=False)


# ---------------------------------------------------------------------------
# Test 2: Fixed exit_terminal_cli succeeds during recovery
# ---------------------------------------------------------------------------

class TestFixedExitTerminalCliSucceeds:
    """Prove that exit_terminal_cli with _lifecycle_internal=True bypasses
    the status check and delivers the exit command to the backend."""

    def test_exit_terminal_cli_succeeds_during_rebind_exiting(
        self, monkeypatch, _mock_infra
    ):
        """exit_terminal_cli must succeed even when recovery_state blocks
        public callers."""
        provider, backend = _mock_infra
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        # Should NOT raise — exit_terminal_cli passes _lifecycle_internal=True
        exit_terminal_cli(TERMINAL_ID)
        # Verify the exit command was actually sent via the backend
        backend.send_keys.assert_called_once()
        call_args = backend.send_keys.call_args
        assert call_args[0][2] == "/exit" or call_args[1].get("keys") == "/exit"

    @pytest.mark.parametrize("recovery_state", [
        "rebind_starting", "rebind_exiting", "rebind_failed",
        "fallback_starting",
    ])
    def test_exit_terminal_cli_succeeds_for_all_blocking_states(
        self, monkeypatch, _mock_infra, recovery_state
    ):
        """exit_terminal_cli must work regardless of which recovery_state
        is persisted."""
        provider, backend = _mock_infra
        meta_with_state = METADATA.copy()
        meta_with_state["recovery_state"] = recovery_state
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            lambda _tid: meta_with_state,
        )
        # get_status returns ERROR for all non-None/non-rebound recovery_states
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        exit_terminal_cli(TERMINAL_ID)
        assert backend.send_keys.called


# ---------------------------------------------------------------------------
# Test 3: Concurrent safety — external input still blocked
# ---------------------------------------------------------------------------

class TestConcurrentSafety:
    """Ensure external callers cannot sneak input during rebind."""

    def test_public_send_input_blocked_while_lifecycle_exit_succeeds(
        self, monkeypatch, _mock_infra
    ):
        """Simulate concurrent access: lifecycle exit passes, public blocked."""
        provider, backend = _mock_infra
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        # Lifecycle exit succeeds
        exit_terminal_cli(TERMINAL_ID)
        assert backend.send_keys.called

        # Public input blocked in same state
        with pytest.raises(TerminalInputBlockedError):
            send_input(TERMINAL_ID, "concurrent user task")

    def test_lifecycle_internal_kwarg_not_positional(self, monkeypatch, _mock_infra):
        """_lifecycle_internal must be keyword-only — cannot be passed
        positionally by external code that doesn't know about it."""
        import inspect

        sig = inspect.signature(send_input)
        param = sig.parameters["_lifecycle_internal"]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# Test 4: Rebind path integration (end-to-end flow)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rebind_terminal_exit_phase_does_not_deadlock(monkeypatch):
    """End-to-end: rebind_terminal's p7 phase calls exit_terminal_cli which
    now succeeds, allowing the rebind to proceed past the exit step."""
    from unittest.mock import AsyncMock

    from cli_agent_orchestrator.services import provider_rebind_service as service

    # Install mocks for the full rebind flow
    provider = _make_provider()
    provider.supports_reauth_rebind = True
    provider.validate_session_artifact.return_value = True
    provider.capture_session_uuid.return_value = "session-uuid"

    candidate = MagicMock()
    candidate.initialize = AsyncMock(return_value=True)

    backend = MagicMock()
    backend.get_pane_working_directory.return_value = "/tmp"
    backend.get_pane_current_command.return_value = "bash"
    backend.send_keys.return_value = None

    metadata = METADATA.copy()
    metadata["recovery_state"] = None  # starts clean
    states_persisted = []

    def track_set_recovery(tid, state, error=None, **kw):
        states_persisted.append((state, error))
        # Simulate the DB write: update our metadata view
        metadata["recovery_state"] = state
        return True

    monkeypatch.setattr(service, "get_terminal_metadata", lambda _tid: metadata.copy())
    monkeypatch.setattr(service, "has_unsettled_delivery_attempt", lambda _tid: False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.has_deferred_init",
        lambda _tid: False,
    )
    monkeypatch.setattr(service.status_monitor, "get_raw_status", lambda *_a, **_k: TerminalStatus.IDLE)
    monkeypatch.setattr(service.status_monitor, "reset_buffer", lambda _tid: None)
    monkeypatch.setattr(service.status_monitor, "get_fifo_frame_gen", lambda _tid: 1)
    monkeypatch.setattr(service.provider_manager, "get_provider", lambda _tid: provider)
    monkeypatch.setattr(service.provider_manager, "construct_provider", lambda *_a, **_k: candidate)
    monkeypatch.setattr(service.provider_manager, "commit_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(service, "pane_pid", lambda *_a: 123)
    monkeypatch.setattr(service, "pane_launch_epoch", lambda _pid: 1.0)
    monkeypatch.setattr(service, "_launch_context", lambda _meta: None)
    monkeypatch.setattr(service, "_wait_for_shell_baseline", AsyncMock(return_value="exit_confirmed"))
    monkeypatch.setattr(service, "_wait_for_backend_proof", AsyncMock())
    monkeypatch.setattr(service, "settle_terminal_rebound", lambda *_a: 2)
    monkeypatch.setattr(service, "set_terminal_recovery_state", track_set_recovery)
    monkeypatch.setattr(service, "get_backend", lambda: backend)
    monkeypatch.setattr(service.DeliveryGuard, "acquire", AsyncMock())
    monkeypatch.setattr(service.DeliveryGuard, "close", AsyncMock())
    monkeypatch.setattr(
        service.stalled_callback_watchdog, "pause_terminal", lambda _tid: (None, 0.0)
    )
    monkeypatch.setattr(service.stalled_callback_watchdog, "resume_terminal", lambda *_a: None)

    # Wire real exit_terminal_cli (NOT mocked) so we exercise the actual path
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
        lambda _tid: metadata.copy(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.provider_manager.get_provider",
        lambda _tid: provider,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_backend",
        lambda: backend,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.notify_input_sent",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.clear_rolling_buffer",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.bind_dispatch_provider",
        lambda _tid, _p: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.begin_dispatch",
        lambda _tid: MagicMock(),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.commit_dispatch",
        lambda _txn: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.update_last_active",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.preserve_draft_before_send",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.inject_memory_context",
        lambda msg, _tid: msg,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service._append_message_contract",
        lambda msg, _meta, _orch: msg,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.auto_responder.auto_responder.mark_exit_suppress",
        lambda _tid: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service._fixture_send_input_override",
        lambda _p, _m: False,
    )

    # The critical part: use a get_status that returns ERROR when recovery_state
    # is set (simulates the real status_monitor behavior)
    def _get_status_from_metadata(_tid):
        current_meta = metadata.copy()
        if current_meta.get("recovery_state") not in (None, "rebound"):
            return TerminalStatus.ERROR
        return TerminalStatus.IDLE

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
        _get_status_from_metadata,
    )

    # Run rebind
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.inbox_service.inbox_service.deliver_pending",
        lambda _tid: None,
    )

    result = await service.rebind_terminal(TERMINAL_ID)
    # If the deadlock existed, we'd get resume_failed/exit_uncertain at p7.
    # With the fix, rebind completes successfully.
    assert result["status"] == "rebound", f"Expected rebound, got {result}"
    # Verify exit command was actually sent
    assert backend.send_keys.called
    # Verify rebind_starting and rebind_exiting were persisted
    state_names = [s[0] for s in states_persisted]
    assert "rebind_starting" in state_names
    assert "rebind_exiting" in state_names


# ---------------------------------------------------------------------------
# Test 5: Mutation witnesses
# ---------------------------------------------------------------------------

class TestMutationWitness:
    """Confirm that removing _lifecycle_internal or weakening the guard
    produces test failures."""

    def test_removing_lifecycle_internal_from_exit_causes_deadlock(
        self, monkeypatch, _mock_infra
    ):
        """If exit_terminal_cli called send_input WITHOUT _lifecycle_internal,
        it would deadlock (raise TerminalInputBlockedError)."""
        provider, backend = _mock_infra
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        # Simulate old code: call send_input directly without _lifecycle_internal
        with pytest.raises(TerminalInputBlockedError):
            send_input(TERMINAL_ID, "/exit", _lifecycle_internal=False)

    def test_weakening_public_guard_allows_unauthorized_input(
        self, monkeypatch, _mock_infra
    ):
        """If we removed the status check entirely (not guarded by
        _lifecycle_internal), external callers could inject during recovery.
        This test would FAIL if the guard were removed."""
        provider, backend = _mock_infra
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        # Public caller MUST be blocked
        with pytest.raises(TerminalInputBlockedError):
            send_input(TERMINAL_ID, "malicious payload during recovery")


# ---------------------------------------------------------------------------
# Test 6: Bug-family — other lifecycle operations
# ---------------------------------------------------------------------------

class TestBugFamily:
    """Check that ALL internal lifecycle exits are safe, not just the
    primary rebind path."""

    @pytest.mark.parametrize("exit_command", ["/exit", "/quit", "exit"])
    def test_text_exit_commands_all_bypass_status_check(
        self, monkeypatch, _mock_infra, exit_command
    ):
        """All text-based exit commands route through send_input with
        _lifecycle_internal=True."""
        provider, backend = _mock_infra
        provider.exit_cli.return_value = exit_command
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        exit_terminal_cli(TERMINAL_ID)
        assert backend.send_keys.called

    def test_key_sequence_exit_bypasses_via_send_special_key(
        self, monkeypatch, _mock_infra
    ):
        """Providers using C-d (key sequence) already bypass because
        send_special_key has no status check. Confirm this path works."""
        provider, backend = _mock_infra
        provider.exit_cli.return_value = "C-d"
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.terminal_service.status_monitor.get_status",
            lambda _tid: TerminalStatus.ERROR,
        )
        exit_terminal_cli(TERMINAL_ID)
        backend.send_special_key.assert_called_once()

"""Tests for fx111 — dead-supervisor orphan reap + conflict detail."""

from __future__ import annotations

import logging
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.services.terminal_service import (
    _ACTIVE_RECOVERY_STATES,
    _owner_root_is_dead,
    delete_terminal,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_terminals(
    root_id: str = "aaaaaaaa",
    child_id: str = "bbbbbbbb",
    root_session: str = "cao_test",
    root_window: str = "win_root",
    root_recovery_state: str | None = None,
    child_caller_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build a minimal terminal list for testing."""
    if child_caller_id is None:
        child_caller_id = root_id
    return [
        {
            "id": root_id,
            "tmux_session": root_session,
            "tmux_window": root_window,
            "caller_id": None,
            "recovery_state": root_recovery_state,
        },
        {
            "id": child_id,
            "tmux_session": root_session,
            "tmux_window": "win_child",
            "caller_id": child_caller_id,
            "recovery_state": None,
        },
    ]


# ---------------------------------------------------------------------------
# Test 1: MCP delete 409 surfaces detail
# ---------------------------------------------------------------------------


def test_mcp_delete_409_surfaces_detail() -> None:
    """When server returns 409 with detail, MCP result message contains it."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_response.json.return_value = {"detail": "cascade_outside_caller_subtree"}
    mock_response.text = '{"detail": "cascade_outside_caller_subtree"}'
    mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
    ):
        mock_http.delete.return_value = mock_response

        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert not result["success"]
    assert "cascade_outside_caller_subtree" in result["message"]


# ---------------------------------------------------------------------------
# Test 2: Server logs conflict cause
# ---------------------------------------------------------------------------


def test_server_logs_conflict_cause(caplog: pytest.LogCaptureFixture) -> None:
    """TerminalProtectionError is logged at WARNING with terminal_id and detail (AC2)."""
    import asyncio

    from cli_agent_orchestrator.api.main import delete_terminal as api_delete_terminal
    from cli_agent_orchestrator.services.terminal_guard_service import TerminalProtectionError

    terminal_id = "deadbeef"
    caller_id = "cafecafe"

    def _raise_protection(*args: Any, **kwargs: Any) -> None:
        raise TerminalProtectionError("cascade_outside_caller_subtree")

    async def _run() -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await api_delete_terminal(
                request=MagicMock(),
                terminal_id=terminal_id,
                force=False,
                orphan=False,
                caller_id=caller_id,
            )
        assert exc_info.value.status_code == 409

    with (
        patch("cli_agent_orchestrator.api.main.require_delete_allowed"),
        patch(
            "cli_agent_orchestrator.api.main.asyncio.to_thread",
            side_effect=_raise_protection,
        ),
        patch("cli_agent_orchestrator.api.main.get_plugin_registry", return_value=MagicMock()),
        caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.api.main"),
    ):
        asyncio.run(_run())

    # Assert the WARNING log was emitted with terminal_id and detail
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        terminal_id in r.message and "cascade_outside_caller_subtree" in r.message
        for r in warning_records
    ), f"Expected WARNING with terminal_id and detail, got: {[r.message for r in warning_records]}"


# ---------------------------------------------------------------------------
# Test 3: Force-delete dead owner succeeds
# ---------------------------------------------------------------------------


def test_force_delete_dead_owner_succeeds() -> None:
    """force=True + dead root owner → no TerminalProtectionError."""
    from cli_agent_orchestrator.services.terminal_guard_service import TerminalProtectionError

    root_id = "aaaaaaaa"
    child_id = "bbbbbbbb"
    other_caller = "cccccccc"

    terminals = _make_terminals(root_id=root_id, child_id=child_id)

    mock_backend = MagicMock()
    mock_backend.window_liveness.return_value = "gone"

    with (
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value=terminals[1],
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.list_terminals_by_session",
            return_value=terminals,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            return_value="lease",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service._cascade_plan",
            return_value=([child_id], []),
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service._delete_terminal_under_lease",
            return_value={"id": child_id},
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.status_monitor",
        ) as mock_sm,
        patch(
            "cli_agent_orchestrator.services.rebind_lease.acquire_rebind_lease",
            return_value="token",
        ),
        patch(
            "cli_agent_orchestrator.services.rebind_lease.release_rebind_lease",
        ),
    ):
        mock_sm.get_boundary_observation.return_value = MagicMock(status=MagicMock(value="idle"))
        # Should NOT raise TerminalProtectionError
        try:
            result = delete_terminal(child_id, force=True, caller_id=other_caller)
        except TerminalProtectionError:
            pytest.fail("Should not raise TerminalProtectionError for dead owner with force=True")


# ---------------------------------------------------------------------------
# Test 4: Force-delete live owner still 409s
# ---------------------------------------------------------------------------


def test_force_delete_live_owner_409s() -> None:
    """force=True + live root owner → TerminalProtectionError."""
    from cli_agent_orchestrator.services.terminal_guard_service import TerminalProtectionError

    root_id = "aaaaaaaa"
    child_id = "bbbbbbbb"
    other_caller = "cccccccc"

    terminals = _make_terminals(root_id=root_id, child_id=child_id)

    mock_backend = MagicMock()
    mock_backend.window_liveness.return_value = "live"

    with (
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value=terminals[1],
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.list_terminals_by_session",
            return_value=terminals,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            return_value="lease",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ),
    ):
        with pytest.raises(TerminalProtectionError, match="cascade_outside_caller_subtree"):
            delete_terminal(child_id, force=True, caller_id=other_caller)


# ---------------------------------------------------------------------------
# Test 5: Dead owner bypass denied without force
# ---------------------------------------------------------------------------


def test_dead_owner_bypass_denied_without_force() -> None:
    """force=False + dead root owner → TerminalProtectionError (bypass requires force)."""
    from cli_agent_orchestrator.services.terminal_guard_service import TerminalProtectionError

    root_id = "aaaaaaaa"
    child_id = "bbbbbbbb"
    other_caller = "cccccccc"

    terminals = _make_terminals(root_id=root_id, child_id=child_id)

    mock_backend = MagicMock()
    mock_backend.window_liveness.return_value = "gone"

    with (
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value=terminals[1],
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.list_terminals_by_session",
            return_value=terminals,
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.quiesce_deferred_session_sync",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.acquire_session_lifecycle_exclusive",
            return_value="lease",
        ),
        patch(
            "cli_agent_orchestrator.services.session_lifecycle_lease.release_session_lifecycle_lease",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_guard_service.require_delete_allowed",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ),
    ):
        with pytest.raises(TerminalProtectionError, match="cascade_outside_caller_subtree"):
            delete_terminal(child_id, force=False, caller_id=other_caller)


# ---------------------------------------------------------------------------
# Test 6: _owner_root_is_dead error fails closed
# ---------------------------------------------------------------------------


def test_owner_root_is_dead_error_fails_closed() -> None:
    """window_liveness raising RuntimeError → _owner_root_is_dead returns False."""
    terminals = _make_terminals()

    mock_backend = MagicMock()
    mock_backend.window_liveness.side_effect = RuntimeError("tmux borked")

    with patch(
        "cli_agent_orchestrator.services.terminal_service.get_backend",
        return_value=mock_backend,
    ):
        assert _owner_root_is_dead(terminals, "bbbbbbbb") is False


# ---------------------------------------------------------------------------
# Test 7b: Restarting owner not reapable (MAJOR-1 fold)
# ---------------------------------------------------------------------------


def test_restarting_owner_not_reapable() -> None:
    """Root with active recovery_state + gone window → bypass denied."""
    for active_state in _ACTIVE_RECOVERY_STATES:
        terminals = _make_terminals(root_recovery_state=active_state)

        mock_backend = MagicMock()
        mock_backend.window_liveness.return_value = "gone"

        with patch(
            "cli_agent_orchestrator.services.terminal_service.get_backend",
            return_value=mock_backend,
        ):
            assert (
                _owner_root_is_dead(terminals, "bbbbbbbb") is False
            ), f"Expected False for active recovery_state={active_state!r}"


# ---------------------------------------------------------------------------
# Test 7: _owner_root_is_dead cycle fails closed
# ---------------------------------------------------------------------------


def test_owner_root_is_dead_cycle_fails_closed() -> None:
    """Circular caller_id chain → _owner_root_is_dead returns False."""
    # Build a cycle: a → b → c → a
    terminals: list[dict[str, Any]] = [
        {
            "id": "aaaaaaaa",
            "tmux_session": "s",
            "tmux_window": "w1",
            "caller_id": "cccccccc",
            "recovery_state": None,
        },
        {
            "id": "bbbbbbbb",
            "tmux_session": "s",
            "tmux_window": "w2",
            "caller_id": "aaaaaaaa",
            "recovery_state": None,
        },
        {
            "id": "cccccccc",
            "tmux_session": "s",
            "tmux_window": "w3",
            "caller_id": "bbbbbbbb",
            "recovery_state": None,
        },
    ]

    mock_backend = MagicMock()
    mock_backend.window_liveness.return_value = "gone"

    with patch(
        "cli_agent_orchestrator.services.terminal_service.get_backend",
        return_value=mock_backend,
    ):
        assert _owner_root_is_dead(terminals, "bbbbbbbb") is False

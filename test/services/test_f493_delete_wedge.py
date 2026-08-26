"""F493: delete_terminal wedge fix — regression tests.

Tests:
1. Provider-aware message in MCP server (kiro_cli terminal must never mention Grok)
2. Provider-aware message in API endpoint
3. force=True overrides cleanup_provider deferral
4. Non-force still defers (existing behavior preserved)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests


# ---------------------------------------------------------------------------
# Test 1: MCP server cleanup-deferred message names actual provider
# ---------------------------------------------------------------------------


def test_mcp_delete_cleanup_deferred_message_names_provider() -> None:
    """A kiro_cli terminal's deferral message must mention 'kiro_cli', never 'Grok'."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_response.json.return_value = {"detail": "cleanup deferred for terminal 'abcd1234'"}
    mock_response.text = '{"detail": "cleanup deferred"}'
    mock_response.raise_for_status.side_effect = requests.HTTPError(response=mock_response)

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "kiro_cli"},
        ),
    ):
        mock_http.delete.return_value = mock_response

        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert not result["success"]
    assert "kiro_cli" in result["message"]
    assert "Grok" not in result["message"]


def test_mcp_delete_cleanup_deferred_non_raise_path_names_provider() -> None:
    """The pre-raise_for_status 409 path also names the actual provider."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    mock_response = MagicMock()
    mock_response.status_code = 409
    # Detail doesn't contain protection indicators → deferral branch
    mock_response.json.return_value = {"detail": "cleanup deferred"}
    mock_response.raise_for_status = MagicMock()  # No exception raised

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "kiro_cli"},
        ),
    ):
        mock_http.delete.return_value = mock_response

        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert not result["success"]
    assert "kiro_cli" in result["message"]
    assert "Grok" not in result["message"]


def test_mcp_delete_payload_failure_names_provider() -> None:
    """When payload has success=False, message names the actual provider."""
    from cli_agent_orchestrator.mcp_server.server import delete_terminal as mcp_delete_terminal

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"success": False}
    mock_response.raise_for_status = MagicMock()

    with (
        patch("cli_agent_orchestrator.mcp_server.server.cao_http") as mock_http,
        patch("cli_agent_orchestrator.mcp_server.server._current_terminal_id", return_value=None),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"provider": "grok_cli"},
        ),
    ):
        mock_http.delete.return_value = mock_response

        result = mcp_delete_terminal(terminal_id="abcd1234", force=False, orphan=False)

    assert not result["success"]
    assert "grok_cli" in result["message"]
    # Must not say "the Grok process" (old hardcoded form)
    assert "the Grok process" not in result["message"]


# ---------------------------------------------------------------------------
# Test 2: API endpoint cleanup-deferred message names actual provider
# ---------------------------------------------------------------------------


def test_api_delete_terminal_deferred_names_provider() -> None:
    """The 409 detail must reference the terminal's actual provider, not hardcoded Grok."""
    import asyncio

    from cli_agent_orchestrator.api.main import delete_terminal as api_delete_terminal

    terminal_id = "abcd1234"

    async def _run() -> None:
        from fastapi import HTTPException

        with (
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
            patch("cli_agent_orchestrator.api.main.require_delete_allowed"),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"provider": "kiro_cli"},
            ),
        ):
            mock_svc.delete_terminal.return_value = False
            mock_request = MagicMock()
            mock_request.app = MagicMock()

            with pytest.raises(HTTPException) as exc_info:
                await api_delete_terminal(
                    request=mock_request,
                    terminal_id=terminal_id,
                    force=False,
                    orphan=False,
                    caller_id=None,
                    _scopes=["admin"],
                )

            assert exc_info.value.status_code == 409
            assert "kiro_cli" in exc_info.value.detail
            assert "Grok" not in exc_info.value.detail

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 3: force=True overrides cleanup_provider deferral
# ---------------------------------------------------------------------------


@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
@patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
@patch("cli_agent_orchestrator.backends.registry._backend")
@patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
def test_delete_under_lease_force_overrides_cleanup_deferral(
    mock_meta: MagicMock,
    mock_tmux: MagicMock,
    mock_pm: MagicMock,
    mock_db_del: MagicMock,
    mock_fifo: MagicMock,
    mock_status: MagicMock,
) -> None:
    """When force=True and cleanup_provider returns False, deletion proceeds."""
    from cli_agent_orchestrator.services.terminal_service import _delete_terminal_under_lease

    mock_meta.return_value = {
        "tmux_session": "cao-session",
        "tmux_window": "developer-abcd",
        "provider": "grok_cli",
    }
    mock_pm.cleanup_provider.return_value = False
    mock_db_del.return_value = {"terminal_deleted": True, "intent_deleted": True}
    mock_tmux.kill_window = MagicMock()
    mock_tmux.stop_pipe_pane = MagicMock()
    mock_fifo.stop_reader = MagicMock()

    # Use a dummy lease token
    with (
        patch(
            "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service",
        ) as mock_wt,
        patch(
            "cli_agent_orchestrator.services.auto_responder.auto_responder",
            MagicMock(),
        ),
    ):
        mock_wt.parse_worktree_path.return_value = None
        result = _delete_terminal_under_lease("test1234", "fake_lease", force=True)

    # Deletion must proceed despite cleanup_provider returning False
    assert result.get("cleanup_deferred") is not True
    assert result.get("terminal_deleted") is True


# ---------------------------------------------------------------------------
# Test 4: Non-force still defers (existing behavior)
# ---------------------------------------------------------------------------


@patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
@patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
@patch("cli_agent_orchestrator.services.terminal_service.delete_terminal_and_warm_intent")
@patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
@patch("cli_agent_orchestrator.backends.registry._backend")
@patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
def test_delete_under_lease_non_force_still_defers(
    mock_meta: MagicMock,
    mock_tmux: MagicMock,
    mock_pm: MagicMock,
    mock_db_del: MagicMock,
    mock_fifo: MagicMock,
    mock_status: MagicMock,
) -> None:
    """When force=False and cleanup_provider returns False, deletion is deferred."""
    from cli_agent_orchestrator.services.terminal_service import _delete_terminal_under_lease

    mock_meta.return_value = {
        "tmux_session": "cao-session",
        "tmux_window": "developer-abcd",
        "provider": "grok_cli",
    }
    mock_pm.cleanup_provider.return_value = False

    with (
        patch(
            "cli_agent_orchestrator.services.rebind_lease.validate_rebind_lease",
        ),
        patch(
            "cli_agent_orchestrator.services.terminal_service.worktree_service",
        ) as mock_wt,
        patch(
            "cli_agent_orchestrator.services.auto_responder.auto_responder",
            MagicMock(),
        ),
    ):
        mock_wt.parse_worktree_path.return_value = None
        result = _delete_terminal_under_lease("test1234", "fake_lease", force=False)

    assert result["cleanup_deferred"] is True
    assert result["terminal_deleted"] is False
    mock_db_del.assert_not_called()

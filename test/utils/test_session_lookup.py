"""Tests for shared CLI/MCP session resolution."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.utils import session_lookup


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_explicit_session_wins_without_reading_the_terminal(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    with patch.object(session_lookup.cao_http, "get") as get:
        assert session_lookup.resolve_session_name("explicit", timeout=10.0) == "explicit"
    get.assert_not_called()


def test_valid_terminal_resolves_with_the_caller_timeout(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    response = _response({"session_name": "cao-test"})
    with patch.object(session_lookup.cao_http, "get", return_value=response) as get:
        assert session_lookup.resolve_session_name(None, timeout=10.0) == "cao-test"
    get.assert_called_once_with("/terminals/11111111", timeout=10.0)


@pytest.mark.parametrize("terminal_id", [None, "", "ABCDEF12", "not-a-terminal"])
def test_unset_or_malformed_terminal_is_absent(monkeypatch, terminal_id):
    if terminal_id is None:
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    else:
        monkeypatch.setenv("CAO_TERMINAL_ID", terminal_id)
    with patch.object(session_lookup.cao_http, "get") as get:
        with pytest.raises(ValueError, match="session_name required outside a CAO terminal"):
            session_lookup.resolve_session_name(None, timeout=10.0)
    get.assert_not_called()


def test_missing_session_name_key_propagates(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    with patch.object(session_lookup.cao_http, "get", return_value=_response({"id": "11111111"})):
        with pytest.raises(KeyError, match="session_name"):
            session_lookup.resolve_session_name(None, timeout=10.0)


def test_ac10_mcp_resolution_calls_helper_then_fleet_with_concrete_timeout(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "11111111")
    terminal_response = _response({"session_name": "cao-test"})
    fleet_payload = {"session_name": "cao-test", "terminals": []}
    fleet_response = _response(fleet_payload)
    manager = Mock()
    with (
        patch.object(session_lookup.cao_http, "get", return_value=terminal_response) as helper_get,
        patch.object(server.cao_http, "get", return_value=fleet_response) as fleet_get,
        patch.object(server, "get_server_settings", return_value={"mcp_request_timeout": 7.5}),
    ):
        manager.attach_mock(helper_get, "resolve")
        manager.attach_mock(fleet_get, "fleet")
        result = asyncio.run(server.fleet())

    assert result == {"success": True, "fleet": fleet_payload}
    assert manager.mock_calls == [
        call.resolve("/terminals/11111111", timeout=7.5),
        call.fleet("/sessions/cao-test/fleet", timeout=7.5),
    ]


def test_ac10_mcp_absent_and_malformed_terminal_have_exact_error(monkeypatch):
    for terminal_id in (None, "not-a-terminal"):
        if terminal_id is None:
            monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        else:
            monkeypatch.setenv("CAO_TERMINAL_ID", terminal_id)
        result = asyncio.run(server.fleet())
        assert result == {
            "success": False,
            "error": "session_name required outside a CAO terminal",
        }


def test_ac10_malformed_terminal_does_not_block_explicit_mcp_session(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "not-a-terminal")
    payload = {"session_name": "explicit", "terminals": []}
    with (
        patch.object(server.cao_http, "get", return_value=_response(payload)) as get,
        patch.object(server, "get_server_settings", return_value={"mcp_request_timeout": 7.5}),
    ):
        result = asyncio.run(server.fleet("explicit"))
    assert result == {"success": True, "fleet": payload}
    get.assert_called_once_with("/sessions/explicit/fleet", timeout=7.5)


def test_ac10_other_current_terminal_callers_still_reject_malformed_env(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "not-a-terminal")
    with (
        patch.object(server, "strict_supervisor_cwd", return_value="/tmp"),
        patch.object(server, "_configured_default_fork_base", return_value=None),
    ):
        result = server._assign_impl("developer", "task")
    assert result == {
        "success": False,
        "terminal_id": None,
        "message": (
            "Assignment failed: Invalid CAO_TERMINAL_ID: expected an 8-character lowercase "
            "hexadecimal terminal ID"
        ),
    }

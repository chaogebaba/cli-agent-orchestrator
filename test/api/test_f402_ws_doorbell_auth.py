"""F402: WS supervisor doorbell authenticates against terminals.auth_token column.

Regression test — MUST fail without the fix (where the endpoint read
metadata.get("terminal_token") which was always None).

The fix reuses ``verify_sender_token`` — the same token-validation path as the
X-CAO-Terminal-Token HTTP plane. These tests exercise the endpoint integration
with that function by:
  1. Using a REAL terminal row + its auth_token for the accept path (proves
     the endpoint reads the DB column, not a metadata dict key).
  2. Mocking verify_sender_token for the reject paths to isolate the endpoint
     logic without DB-session xdist race conditions.

Tests:
  - Correct auth_token (real DB path) → connection accepted.
  - Wrong token (mocked verify) → 4401 rejected.
  - Missing token → 4401 rejected (no DB call needed).
  - Non-existent terminal (mocked verify) → 4404 rejected.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import create_terminal, SessionLocal, TerminalModel


@pytest.fixture()
def terminal_with_auth_token():
    """Create a real terminal row with a unique auth_token in the test DB."""
    tid = uuid.uuid4().hex[:8]
    token = f"f402-secret-{uuid.uuid4().hex}"
    create_terminal(
        terminal_id=tid,
        tmux_session="f402-test-session",
        tmux_window="f402-test-window",
        provider="mock_cli",
        agent_profile="developer",
        auth_token=token,
    )
    yield tid, token
    # Cleanup: remove the terminal row
    with SessionLocal() as db:
        db.query(TerminalModel).filter(TerminalModel.id == tid).delete()
        db.commit()


def _make_ws(*, query_params=None, headers=None):
    """Build a mock WebSocket suitable for ws_supervisor_doorbell."""
    from starlette.websockets import WebSocketDisconnect

    ws = MagicMock()
    ws.query_params = query_params or {}
    ws.headers = headers or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))
    return ws


@pytest.fixture(autouse=True)
def _enable_ws_monitor():
    """Force ws_monitor flag ON for these tests."""
    with patch(
        "cli_agent_orchestrator.services.ws_doorbell.is_ws_monitor_enabled",
        return_value=True,
    ):
        yield


# ---------------------------------------------------------------------------
# AC: correct auth_token → accepted (REAL DB path — proves F402 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_doorbell_accepts_correct_auth_token(terminal_with_auth_token):
    """Connection with the REAL auth_token column value is accepted (F402 fix).

    This is the core regression assertion: pre-fix code read a nonexistent
    metadata key and always rejected. Post-fix reads the real auth_token column
    via verify_sender_token and accepts when tokens match.
    """
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    tid, token = terminal_with_auth_token
    ws = _make_ws(query_params={"token": token})

    with patch(
        "cli_agent_orchestrator.services.ws_doorbell.register_connection",
        new_callable=AsyncMock,
    ), patch(
        "cli_agent_orchestrator.services.ws_doorbell.unregister_connection",
        new_callable=AsyncMock,
    ):
        await ws_supervisor_doorbell(ws, tid)

    ws.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_doorbell_accepts_via_x_cao_terminal_token_header(terminal_with_auth_token):
    """Connection with auth_token via x-cao-terminal-token header is accepted."""
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    tid, token = terminal_with_auth_token
    ws = _make_ws(headers={"x-cao-terminal-token": token})

    with patch(
        "cli_agent_orchestrator.services.ws_doorbell.register_connection",
        new_callable=AsyncMock,
    ), patch(
        "cli_agent_orchestrator.services.ws_doorbell.unregister_connection",
        new_callable=AsyncMock,
    ):
        await ws_supervisor_doorbell(ws, tid)

    ws.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_ws_doorbell_accepts_via_bearer_header(terminal_with_auth_token):
    """Connection with auth_token via Authorization: Bearer header is accepted."""
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    tid, token = terminal_with_auth_token
    ws = _make_ws(headers={"authorization": f"Bearer {token}"})

    with patch(
        "cli_agent_orchestrator.services.ws_doorbell.register_connection",
        new_callable=AsyncMock,
    ), patch(
        "cli_agent_orchestrator.services.ws_doorbell.unregister_connection",
        new_callable=AsyncMock,
    ):
        await ws_supervisor_doorbell(ws, tid)

    ws.accept.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC: wrong token → 4401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_doorbell_rejects_wrong_token():
    """Connection with an incorrect token is rejected 4401.

    Mocks verify_sender_token to return the "token mismatch" tuple — isolates
    the endpoint logic from any DB session subtleties.
    """
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    ws = _make_ws(query_params={"token": "wrong-token-value"})

    with patch(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        return_value=(False, "E-SENDER-TOKEN"),
    ):
        await ws_supervisor_doorbell(ws, "some-terminal-id")

    ws.accept.assert_not_called()
    ws.close.assert_awaited_once()
    call_kwargs = ws.close.call_args.kwargs
    assert call_kwargs.get("code") == 4401
    assert "Invalid" in call_kwargs.get("reason", "")


# ---------------------------------------------------------------------------
# AC: missing token → 4401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_doorbell_rejects_missing_token():
    """Connection with no token at all is rejected 4401 (pre-DB check)."""
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    ws = _make_ws()
    await ws_supervisor_doorbell(ws, "any-terminal-id")

    ws.accept.assert_not_called()
    ws.close.assert_awaited_once()
    call_kwargs = ws.close.call_args.kwargs
    assert call_kwargs.get("code") == 4401
    assert "Missing" in call_kwargs.get("reason", "")


# ---------------------------------------------------------------------------
# AC: non-existent terminal → 4404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_doorbell_rejects_nonexistent_terminal():
    """Connection to a non-existent terminal gets 4404.

    Mocks verify_sender_token to return the "unknown sender" tuple.
    """
    from cli_agent_orchestrator.api.main import ws_supervisor_doorbell

    ws = _make_ws(query_params={"token": "any-token"})

    with patch(
        "cli_agent_orchestrator.services.terminal_token_service.verify_sender_token",
        return_value=(False, "E-SENDER-UNKNOWN"),
    ):
        await ws_supervisor_doorbell(ws, "nonexistent-terminal")

    ws.accept.assert_not_called()
    ws.close.assert_awaited_once()
    call_kwargs = ws.close.call_args.kwargs
    assert call_kwargs.get("code") == 4404
    assert "not found" in call_kwargs.get("reason", "").lower()

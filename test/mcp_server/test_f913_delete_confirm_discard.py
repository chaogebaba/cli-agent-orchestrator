"""F913 (#765) — MCP delete_terminal tool: forward ``confirm_discard`` and
surface the typed refusal verbatim.

The service guard and HTTP boundary are proven elsewhere; here we pin the MCP
tool contract:
  * confirm_discard=True → forwarded as ``?confirm_discard=true`` query param;
  * confirm_discard default → NOT forwarded (default-off preserved);
  * a 409 refuse_discard_live_session detail is surfaced verbatim to the caller
    (passes through _classify_delete_409 rather than collapsing to the generic
    cleanup-deferred message).

RULE / MUTANT:
* mutant "swallow-the-refusal" (drop 'refuse_discard_live_session' from the
  passthrough indicators) → the caller would see the misleading generic
  cleanup-deferred message instead of the resume guidance →
  test_mutant_swallow_refusal_is_caught
"""

from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import delete_terminal

_REFUSAL_DETAIL = {
    "error": "refuse_discard_live_session",
    "how": "interrupt then delete without force, then assign(resume_from=abcd1234)",
    "terminal_id": "abcd1234",
    "provider": "codex",
    "reason": "resumable",
}


def _ok_json():
    return {
        "success": True,
        "reaped": [{"id": "t1", "status": "reaped"}],
        "skipped": [],
        "uncertain": [],
        "unattempted": [],
    }


@patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
def test_confirm_discard_true_is_forwarded(mock_delete):
    mock_delete.return_value.status_code = 200
    mock_delete.return_value.raise_for_status.return_value = None
    mock_delete.return_value.json.return_value = _ok_json()
    result = delete_terminal("t1", force=True, confirm_discard=True)
    assert result["success"] is True
    assert mock_delete.call_args.kwargs["params"] == {
        "force": True,
        "orphan": False,
        "confirm_discard": True,
    }


@patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
def test_confirm_discard_absent_is_not_forwarded(mock_delete):
    mock_delete.return_value.status_code = 200
    mock_delete.return_value.raise_for_status.return_value = None
    mock_delete.return_value.json.return_value = _ok_json()
    result = delete_terminal("t1", force=True)
    assert result["success"] is True
    # default-off: confirm_discard must NOT appear in the forwarded params.
    assert mock_delete.call_args.kwargs["params"] == {"force": True, "orphan": False}


@patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
def test_refusal_409_surfaced_verbatim(mock_delete):
    resp = MagicMock(status_code=409)
    resp.json.return_value = {"detail": _REFUSAL_DETAIL}
    mock_delete.return_value = resp
    result = delete_terminal("t1", force=True)
    assert result["success"] is False
    # verbatim passthrough — the resume guidance reaches the caller.
    assert "refuse_discard_live_session" in result["message"]
    assert "resume_from=abcd1234" in result["message"]


@patch("cli_agent_orchestrator.mcp_server.server.requests.delete")
def test_mutant_swallow_refusal_is_caught(mock_delete):
    """Mutant 'swallow-the-refusal' (drop the passthrough indicator): the 409
    would collapse to the generic 'cleanup pending, retry' message and the
    operator would never learn to resume. The correct code passes the refusal
    detail through; asserting the resume guidance is present kills the mutant.
    """
    resp = MagicMock(status_code=409)
    resp.json.return_value = {"detail": _REFUSAL_DETAIL}
    mock_delete.return_value = resp
    result = delete_terminal("t1", force=True)
    assert result["success"] is False
    assert "refuse_discard_live_session" in result["message"]
    # the generic cleanup-deferred fallback must NOT be what the caller sees.
    assert "cleanup is pending" not in result["message"]

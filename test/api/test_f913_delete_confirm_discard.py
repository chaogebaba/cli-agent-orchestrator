"""F913 (#765) — DELETE /terminals/{id} boundary: the typed refusal maps to a
structured 409 and ``confirm_discard`` is forwarded to the service.

The service-level guard is proven in test/services/test_f913_force_delete_guard.py.
Here we pin only the HTTP boundary contract (issue #765 deliverable 1):
  * a ``RefuseDiscardLiveSessionError`` from the service → 409 whose JSON detail
    carries {error:'refuse_discard_live_session', how:..., ...};
  * ``?confirm_discard=true`` is forwarded to
    ``terminal_service.delete_terminal(confirm_discard=True)``;
  * absent, it is NOT forwarded (default-off preserved).

RULE / MUTANT:
* RULE-1 (boundary) typed 409 →
    mutant "collapse-to-500" (let the RuntimeError subclass fall through to the
    generic handler → 500) → test_mutant_collapse_to_500_is_caught
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services.terminal_service import (
    RefuseDiscardLiveSessionError,
)

TID = "abcd1234"


@pytest.fixture()
def client():
    with patch(
        "cli_agent_orchestrator.services.terminal_guard_service."
        "get_ready_provider_session_by_source_terminal",
        lambda _terminal_id: None,
    ):
        app.state.plugin_registry = PluginRegistry()
        yield TestClient(app, headers={"Host": "localhost"})


def _ok_result():
    return {
        "reaped": [{"id": TID, "status": "reaped"}],
        "skipped": [],
        "uncertain": [],
        "unattempted": [],
    }


def test_force_delete_of_live_resumable_returns_typed_409(client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as service:
        service.delete_terminal.side_effect = RefuseDiscardLiveSessionError(
            TID, provider="codex", reason="resumable"
        )
        response = client.delete("/terminals/abcd1234", params={"force": "true"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "refuse_discard_live_session"
    assert f"assign(resume_from={TID})" in detail["how"]
    assert detail["provider"] == "codex"
    assert detail["reason"] == "resumable"


def test_confirm_discard_true_is_forwarded(client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as service:
        service.delete_terminal.return_value = _ok_result()
        response = client.delete(
            "/terminals/abcd1234",
            params={"force": "true", "confirm_discard": "true"},
        )

    assert response.status_code == 200
    _args, kwargs = service.delete_terminal.call_args
    assert kwargs.get("confirm_discard") is True
    assert kwargs.get("force") is True


def test_confirm_discard_absent_is_not_forwarded(client):
    with patch("cli_agent_orchestrator.api.main.terminal_service") as service:
        service.delete_terminal.return_value = _ok_result()
        response = client.delete("/terminals/abcd1234", params={"force": "true"})

    assert response.status_code == 200
    _args, kwargs = service.delete_terminal.call_args
    assert "confirm_discard" not in kwargs  # default-off: not forwarded


def test_mutant_collapse_to_500_is_caught(client):
    """Mutant 'collapse-to-500': removing the explicit
    `except RefuseDiscardLiveSessionError` lets the RuntimeError subclass fall
    through to the generic 500 handler. The correct boundary returns 409 with a
    dict detail; asserting 409 + dict kills the mutant (a 500 carries a string).
    """
    with patch("cli_agent_orchestrator.api.main.terminal_service") as service:
        service.delete_terminal.side_effect = RefuseDiscardLiveSessionError(
            TID, provider="codex", reason="resumable"
        )
        response = client.delete("/terminals/abcd1234", params={"force": "true"})
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], dict)

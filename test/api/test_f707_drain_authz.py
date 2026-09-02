"""F707 (#562): the inbox drain edges bind the caller to the route terminal.

Before this guard ``POST /terminals/{id}/inbox/drain`` authorized on token
SCOPE alone, so any write-scope holder could drain any terminal's inbox. The
caller must now present the ROUTE terminal's own F332 per-terminal token
(``X-CAO-Terminal-Token`` == ``$CAO_TERMINAL_TOKEN`` inside that terminal).
"""

from unittest.mock import patch

import pytest

_TERMINALS = ["e7070001", "e7070002"]


@pytest.fixture
def _clean_terminals():
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    yield
    with SessionLocal() as db:
        db.query(TerminalModel).filter(TerminalModel.id.in_(_TERMINALS)).delete(
            synchronize_session=False
        )
        db.commit()


@pytest.fixture
def client(_clean_terminals):
    from test.api.conftest import TestClientWithHost

    with patch("cli_agent_orchestrator.api.main.status_monitor"):
        from cli_agent_orchestrator.api.main import app

        return TestClientWithHost(app)


def _create_terminal(terminal_id: str, token: str) -> None:
    from cli_agent_orchestrator.clients.database import SessionLocal, TerminalModel

    with SessionLocal() as db:
        db.add(
            TerminalModel(
                id=terminal_id,
                tmux_session="test-session",
                tmux_window="test-window",
                provider="mock_cli",
                auth_token=token,
            )
        )
        db.commit()


def _body(terminal_id: str) -> dict:
    return {"terminal_id": terminal_id, "ts": "2026-09-02T00:00:00Z"}


# ---- drain -----------------------------------------------------------------


def test_drain_foreign_token_returns_403(client):
    """The #562 finding: a worker holding its OWN token cannot drain the seat."""
    _create_terminal("e7070001", "token_seat")
    _create_terminal("e7070002", "token_worker")

    with patch("cli_agent_orchestrator.api.main.inbox_service") as inbox:
        response = client.post(
            "/terminals/e7070001/inbox/drain",
            json=_body("e7070001"),
            headers={"X-CAO-Terminal-Token": "token_worker"},
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E-DRAIN-CALLER"
    inbox.deliver_pending.assert_not_called()


def test_drain_missing_token_returns_403(client):
    """Scope alone is no longer sufficient — the header is required."""
    _create_terminal("e7070001", "token_seat")

    with patch("cli_agent_orchestrator.api.main.inbox_service") as inbox:
        response = client.post("/terminals/e7070001/inbox/drain", json=_body("e7070001"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E-DRAIN-CALLER"
    inbox.deliver_pending.assert_not_called()


def test_drain_own_token_returns_200(client):
    _create_terminal("e7070001", "token_seat")

    with (
        patch("cli_agent_orchestrator.api.main.inbox_service") as inbox,
        patch("cli_agent_orchestrator.api.main.get_plugin_registry", return_value=None),
    ):
        response = client.post(
            "/terminals/e7070001/inbox/drain",
            json=_body("e7070001"),
            headers={"X-CAO-Terminal-Token": "token_seat"},
        )
    assert response.status_code == 200
    assert response.json() == {"success": True, "terminal_id": "e7070001", "op": "drain"}
    # The guard let the delivery seam run — the point of the 200 arm.
    inbox.deliver_pending.assert_called_once()


def test_drain_admin_bypass_only_when_auth_enabled(client):
    """SCOPE_ADMIN bypasses only against a real IdP.

    Default-off, every caller is handed the full scope set, so an unconditional
    admin bypass would disable the guard on exactly the deployment that needs
    it. The bypass is therefore gated on ``is_auth_enabled()``.
    """
    _create_terminal("e7070001", "token_seat")

    with patch("cli_agent_orchestrator.api.main.inbox_service"):
        # auth disabled (the local posture): no bypass, tokenless call refused
        with patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=False):
            refused = client.post("/terminals/e7070001/inbox/drain", json=_body("e7070001"))
        # auth enabled + admin scope: bypass allowed
        with patch("cli_agent_orchestrator.api.main.is_auth_enabled", return_value=True):
            allowed = client.post("/terminals/e7070001/inbox/drain", json=_body("e7070001"))
    assert refused.status_code == 403
    assert allowed.status_code == 200


# ---- preserved 400 / 404 ---------------------------------------------------


def test_drain_unknown_terminal_still_404(client):
    response = client.post(
        "/terminals/e7070001/inbox/drain",
        json=_body("e7070001"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 404


def test_drain_body_route_mismatch_still_400(client):
    _create_terminal("e7070001", "token_seat")
    _create_terminal("e7070002", "token_worker")

    response = client.post(
        "/terminals/e7070001/inbox/drain",
        json=_body("e7070002"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 400


# ---- drain-ack (same family) ----------------------------------------------


def test_drain_ack_foreign_token_returns_403(client):
    _create_terminal("e7070001", "token_seat")
    _create_terminal("e7070002", "token_worker")

    response = client.post(
        "/terminals/e7070001/inbox/drain-ack",
        json=_body("e7070001"),
        headers={"X-CAO-Terminal-Token": "token_worker"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "E-DRAIN-ACK-CALLER"


def test_drain_ack_own_token_returns_200(client):
    _create_terminal("e7070001", "token_seat")

    response = client.post(
        "/terminals/e7070001/inbox/drain-ack",
        json=_body("e7070001"),
        headers={"X-CAO-Terminal-Token": "token_seat"},
    )
    assert response.status_code == 200
    assert response.json()["op"] == "drain-ack"


# ---- the hooks present the header -----------------------------------------


def test_drain_hook_sends_terminal_token(monkeypatch):
    import io
    import json
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.hooks import supervisor_drain

    monkeypatch.setenv("CAO_TERMINAL_ID", "e7070001")
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "token_seat")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    with (
        patch.object(supervisor_drain, "get_local_bearer", return_value=None),
        patch.object(supervisor_drain.cao_http, "post", return_value=MagicMock()) as post,
        patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "SessionStart"}))),
    ):
        assert supervisor_drain.main() == 0
    assert post.call_args.kwargs["headers"]["X-CAO-Terminal-Token"] == "token_seat"


def test_ack_hook_sends_terminal_token(monkeypatch):
    import io
    import json
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.hooks import supervisor_ack

    monkeypatch.setenv("CAO_TERMINAL_ID", "e7070001")
    monkeypatch.setenv("CAO_TERMINAL_TOKEN", "token_seat")
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:9999")
    with (
        patch.object(supervisor_ack, "get_local_bearer", return_value=None),
        patch.object(supervisor_ack.cao_http, "post", return_value=MagicMock()) as post,
        patch("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop"}))),
    ):
        assert supervisor_ack.main() == 0
    assert post.call_args.kwargs["headers"]["X-CAO-Terminal-Token"] == "token_seat"

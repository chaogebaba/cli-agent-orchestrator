"""F634 (#489) D15 / AC20 + AC7c — create-route amendment at the HTTP boundary.

AC20: on BOTH ``POST /sessions`` and ``POST /sessions/{name}/terminals`` (the
route ``assign`` actually posts to):

* no ``terminal_id`` behaves byte-identically to today (server-side allocation
  — the create call carries ``terminal_id=None``);
* a caller-supplied ``terminal_id`` (and ``is_box_hosted``) is THREADED to the
  service create call;
* a ``TerminalIdConflict`` from the service maps to 409
  ``E-TERMINAL-ID-CONFLICT`` (an id the box already holds is refused, never
  re-used);
* third clause: ``POST /sessions/{name}/recover`` on a box-plane server ->
  typed 409 ``E-BOX-PLANE-NO-RECOVER``, NO replacement terminal created.

AC7c (transport half): the create RESPONSE carries ``provider_session_id`` on
the ``Terminal`` model (``POST /sessions`` and the terminals route both declare
``response_model=Terminal``). ``/sessions/start`` returns a dict and is NOT that
carrier — asserted so nobody binds AC7c to it.

The service ``create_terminal`` is mocked (as the existing create-route suite
does): these arms verify the ROUTE SURFACE — param threading, the 409 mappings,
and the response carrier. The conflict LOGIC itself (known id -> raise) and the
box-plane refusal-before-side-effect are unit-tested against the real service
in ``test/services/test_f634_server_plane_recovery.py`` and the shim spawn in
``test/services/test_f634_box_hosted_shim.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services.terminal_service import TerminalIdConflict
from cli_agent_orchestrator.utils.server_plane import BoxPlaneRecoveryRefused


def _mock_terminal(terminal_id: str = "abcd5678", provider_session_id=None) -> Terminal:
    return Terminal(
        id=terminal_id,
        name="w-1",
        session_name="cao-test-session",
        provider="claude_code",
        agent_profile="reviewer",
        provider_session_id=provider_session_id,
    )


# ---------------------------------------------------------------------------
# AC20 — terminals route (the assign path)
# ---------------------------------------------------------------------------


class TestTerminalsRouteAmendment:
    def test_absent_terminal_id_allocates_server_side(self, client: TestClient) -> None:
        """No terminal_id -> create call carries terminal_id=None (today's
        behaviour) and is_box_hosted defaults False."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal = AsyncMock(return_value=_mock_terminal())
            mock_svc.seed_resume_bootstrap = AsyncMock(return_value=None)
            resp = client.post(
                "/sessions/cao-test-session/terminals",
                params={"provider": "claude_code", "agent_profile": "reviewer"},
            )
        assert resp.status_code == 201
        kw = mock_svc.create_terminal.call_args.kwargs
        assert kw["terminal_id"] is None
        assert kw["is_box_hosted"] is False

    def test_supplied_terminal_id_and_box_flag_threaded(self, client: TestClient) -> None:
        """An unknown id + is_box_hosted=true are threaded to the service."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal = AsyncMock(return_value=_mock_terminal("deadbeef"))
            mock_svc.seed_resume_bootstrap = AsyncMock(return_value=None)
            resp = client.post(
                "/sessions/cao-test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "reviewer",
                    "terminal_id": "deadbeef",
                    "is_box_hosted": "true",
                },
            )
        assert resp.status_code == 201
        kw = mock_svc.create_terminal.call_args.kwargs
        assert kw["terminal_id"] == "deadbeef"
        assert kw["is_box_hosted"] is True

    def test_known_terminal_id_conflict_returns_409(self, client: TestClient) -> None:
        """A TerminalIdConflict from the service -> 409 E-TERMINAL-ID-CONFLICT."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.seed_resume_bootstrap = AsyncMock(return_value=None)
            mock_svc.create_terminal = AsyncMock(side_effect=TerminalIdConflict("deadbeef"))
            resp = client.post(
                "/sessions/cao-test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "reviewer",
                    "terminal_id": "deadbeef",
                },
            )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "E-TERMINAL-ID-CONFLICT"
        assert detail["terminal_id"] == "deadbeef"

    def test_invalid_terminal_id_shape_rejected_422(self, client: TestClient) -> None:
        """terminal_id is a TerminalId (^[a-f0-9]{8}$); a bad shape is 422 at the
        boundary before the service is reached."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.seed_resume_bootstrap = AsyncMock(return_value=None)
            mock_svc.create_terminal = AsyncMock(return_value=_mock_terminal())
            resp = client.post(
                "/sessions/cao-test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "reviewer",
                    "terminal_id": "NOT-HEX",
                },
            )
        assert resp.status_code == 422
        mock_svc.create_terminal.assert_not_called()

    def test_response_carries_provider_session_id_ac7c(self, client: TestClient) -> None:
        """AC7c transport half: the terminals-route response is the Terminal
        model and carries provider_session_id."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.seed_resume_bootstrap = AsyncMock(return_value=None)
            mock_svc.create_terminal = AsyncMock(
                return_value=_mock_terminal(provider_session_id="sess-uuid-123")
            )
            resp = client.post(
                "/sessions/cao-test-session/terminals",
                params={"provider": "claude_code", "agent_profile": "reviewer"},
            )
        assert resp.status_code == 201
        assert "provider_session_id" in resp.json()
        assert resp.json()["provider_session_id"] == "sess-uuid-123"


# ---------------------------------------------------------------------------
# AC20 — POST /sessions
# ---------------------------------------------------------------------------


class TestCreateSessionAmendment:
    def test_absent_terminal_id_allocates_server_side(self, client: TestClient) -> None:
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(return_value=_mock_terminal())
            resp = client.post(
                "/sessions",
                params={"agent_profile": "reviewer", "provider": "claude_code"},
            )
        assert resp.status_code == 201
        kw = mock_svc.create_session.call_args.kwargs
        assert kw["terminal_id"] is None
        assert kw["is_box_hosted"] is False

    def test_supplied_terminal_id_and_box_flag_threaded(self, client: TestClient) -> None:
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(return_value=_mock_terminal("deadbeef"))
            resp = client.post(
                "/sessions",
                params={
                    "agent_profile": "reviewer",
                    "provider": "claude_code",
                    "terminal_id": "deadbeef",
                    "is_box_hosted": "true",
                },
            )
        assert resp.status_code == 201
        kw = mock_svc.create_session.call_args.kwargs
        assert kw["terminal_id"] == "deadbeef"
        assert kw["is_box_hosted"] is True

    def test_known_terminal_id_conflict_returns_409(self, client: TestClient) -> None:
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(side_effect=TerminalIdConflict("deadbeef"))
            resp = client.post(
                "/sessions",
                params={
                    "agent_profile": "reviewer",
                    "provider": "claude_code",
                    "terminal_id": "deadbeef",
                },
            )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "E-TERMINAL-ID-CONFLICT"
        assert detail["terminal_id"] == "deadbeef"

    def test_response_carries_provider_session_id_ac7c(self, client: TestClient) -> None:
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(
                return_value=_mock_terminal(provider_session_id="sess-uuid-777")
            )
            resp = client.post(
                "/sessions",
                params={"agent_profile": "reviewer", "provider": "claude_code"},
            )
        assert resp.status_code == 201
        assert resp.json()["provider_session_id"] == "sess-uuid-777"


# ---------------------------------------------------------------------------
# AC20 third clause — /recover on a box-plane server
# ---------------------------------------------------------------------------


class TestRecoverBoxPlaneRefusal:
    def test_box_plane_epoch_recover_returns_typed_409(self, client: TestClient) -> None:
        with patch(
            "cli_agent_orchestrator.services.epoch_recovery_service.recover_epoch",
            new=AsyncMock(side_effect=BoxPlaneRecoveryRefused("epoch recovery")),
        ):
            resp = client.post("/sessions/cao-test/recover", json={"reason": "epoch"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "E-BOX-PLANE-NO-RECOVER"
        assert detail["reason"] == "epoch recovery"

    def test_box_plane_provider_reauth_returns_typed_409(self, client: TestClient) -> None:
        with patch(
            "cli_agent_orchestrator.services.provider_rebind_service.recover_provider_reauth",
            new=AsyncMock(side_effect=BoxPlaneRecoveryRefused("provider-reauth recovery")),
        ):
            resp = client.post("/sessions/cao-test/recover", json={"reason": "provider-reauth"})
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "E-BOX-PLANE-NO-RECOVER"
        assert detail["reason"] == "provider-reauth recovery"


# ---------------------------------------------------------------------------
# AC7c — /sessions/start is NOT the carrier
# ---------------------------------------------------------------------------


class TestSessionsStartNotAC7cCarrier:
    def test_start_returns_dict_not_terminal_model(self, client: TestClient) -> None:
        """/sessions/start returns start_session's dict (supervisor_terminal
        key), NOT the Terminal model — so AC7c must not bind to it. The dict
        still nests provider_session_id INSIDE supervisor_terminal, never at the
        top level as the create routes do."""
        start_dict = {
            "schema_version": "cao.session-start/v1",
            "session": {"name": "cao-test-session"},
            "supervisor_terminal": _mock_terminal(provider_session_id="nested-uuid").model_dump(
                mode="json"
            ),
            "bootstrap": {"mode": "not_applicable", "status": "not_required"},
            "manifest": None,
            "manifest_error": None,
        }
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.start_session = AsyncMock(return_value=start_dict)
            resp = client.post(
                "/sessions/start",
                params={"agent_profile": "reviewer", "provider": "claude_code"},
            )
        assert resp.status_code == 200
        body = resp.json()
        # Top-level shape is the start dict, NOT a Terminal.
        assert "supervisor_terminal" in body
        assert "provider_session_id" not in body
        # The two amended fields are threaded through to start_session.
        kw = mock_svc.start_session.call_args.kwargs
        assert kw["terminal_id"] is None
        assert kw["is_box_hosted"] is False

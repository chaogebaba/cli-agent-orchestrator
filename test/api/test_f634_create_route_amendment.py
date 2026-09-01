"""F634 (#489) D15 — the PUBLIC create-route amendment, and its recover clause.

AC20. ``terminal_id`` was a server-private parameter of
``terminal_service.create_terminal``; D11 needs the LAPTOP to allocate it so one
id namespace spans every host and a lane stays nameable while its box is
unreachable. These arms bind the three stated semantics on the two routes that
create a box lane, plus the third clause (recover on a box-plane server):

* absent -> today's server-side allocation, byte-identical to now;
* present and unknown -> adopted;
* present and already known on this server -> refused as a CONFLICT, never
  silently re-used, so a retried create is idempotent rather than id-stealing.

``POST /sessions/{session_name}/terminals`` is the route ``assign`` actually
posts to (``_assign_impl`` branches on the caller's ``CAO_TERMINAL_ID``, which a
supervisor always has), so an amendment that reached only ``POST /sessions``
would leave every box lane after a session's first falling back to box-side
allocation — the id-namespace fork D11 exists to prevent. Both routes are
therefore parametrized on the same arms.

``POST /sessions/start`` is amended for surface uniformity and needs no arm of
its own: it creates only a session's FIRST terminal, which under D10 is the
laptop-only supervisor seat, so no box lane is ever born there.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services.box_plane import CAO_SERVER_PLANE_ENV

# A well-formed id in generate_terminal_id() form that no server allocated.
LAPTOP_ALLOCATED_ID = "d15a51ce"

_TERMINALS_ROUTE = "/sessions/cao-f634/terminals"
_SESSIONS_ROUTE = "/sessions"


def _terminal(terminal_id: str = LAPTOP_ALLOCATED_ID) -> Terminal:
    return Terminal(
        id=terminal_id,
        name=f"developer-{terminal_id}",
        session_name="cao-f634",
        provider="claude_code",
        agent_profile="developer",
    )


def _post(client, route: str, **params):
    """POST one create route with the shared minimum params."""
    return client.post(
        route,
        params={"provider": "claude_code", "agent_profile": "developer", **params},
    )


def _patched_service(route: str):
    """Patch the service the given route calls, returning (ctx, attr_name).

    The two routes reach ``create_terminal`` through different services —
    ``/sessions`` via ``session_service.create_session``, the terminals route
    via ``terminal_service.create_terminal`` — so the arms assert on whichever
    one the route under test actually calls.
    """
    if route == _SESSIONS_ROUTE:
        mock = MagicMock()
        mock.create_session = AsyncMock(return_value=_terminal())
        return (
            patch("cli_agent_orchestrator.api.main.session_service", mock),
            mock,
            "create_session",
        )
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    return patch("cli_agent_orchestrator.api.main.terminal_service", mock), mock, "create_terminal"


ROUTES = [_SESSIONS_ROUTE, _TERMINALS_ROUTE]


class TestSuppliedTerminalId:
    """AC20 — the three id semantics, on BOTH routes that create a box lane."""

    @pytest.mark.parametrize("route", ROUTES)
    def test_absent_terminal_id_leaves_allocation_to_the_server(self, route, client):
        """No ``terminal_id`` -> byte-identical to today: the create call carries
        ``terminal_id=None`` (the server allocates), and the conflict lookup is
        never even consulted, so an unamended caller pays nothing."""
        ctx, mock, attr = _patched_service(route)
        with ctx, patch("cli_agent_orchestrator.api.main.terminal_exists") as exists:
            response = _post(client, route)

        assert response.status_code == 201
        assert getattr(mock, attr).call_args.kwargs["terminal_id"] is None
        exists.assert_not_called()

    @pytest.mark.parametrize("route", ROUTES)
    def test_unknown_terminal_id_is_adopted(self, route, client):
        """MUTANT SENTINEL (amend only POST /sessions). A well-formed id this
        server does not hold is ADOPTED — passed through to the create call
        unchanged. Dropping the field from the terminals route makes the
        terminals parametrization go RED while ``/sessions`` stays green, which
        is exactly the silent box-side-allocation fallback D11 forbids."""
        ctx, mock, attr = _patched_service(route)
        with (
            ctx,
            patch("cli_agent_orchestrator.api.main.terminal_exists", return_value=False) as exists,
        ):
            response = _post(client, route, terminal_id=LAPTOP_ALLOCATED_ID)

        assert response.status_code == 201
        assert getattr(mock, attr).call_args.kwargs["terminal_id"] == LAPTOP_ALLOCATED_ID
        exists.assert_called_once_with(LAPTOP_ALLOCATED_ID)

    @pytest.mark.parametrize("route", ROUTES)
    def test_known_terminal_id_is_refused_as_a_conflict(self, route, client):
        """MUTANT SENTINEL (drop the conflict refusal). An id this server
        already holds is refused with a typed 409 and NO create runs — a
        retried create is idempotent rather than id-stealing. Dropping the
        refusal adopts the id, the create fires, and this goes RED."""
        ctx, mock, attr = _patched_service(route)
        with ctx, patch("cli_agent_orchestrator.api.main.terminal_exists", return_value=True):
            response = _post(client, route, terminal_id=LAPTOP_ALLOCATED_ID)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "terminal_id_conflict"
        assert detail["terminal_id"] == LAPTOP_ALLOCATED_ID
        getattr(mock, attr).assert_not_called()

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize(
        "bad_id",
        [
            "DEADBEEF",  # uppercase: display forms and resolve_terminal_id are lowercase-hex
            "zzzzzzzz",  # not hex
            "d15a51c",  # too short
            "d15a51ce1",  # too long
            "d15a-1ce",  # tmux target/display-form delimiter smuggled into the id
            "",  # empty
        ],
    )
    def test_off_shape_terminal_id_is_refused_before_any_create(self, route, bad_id, client):
        """MUTANT SENTINEL (drop the shape check). Making the id public exposes
        the assumption every id resolver in the tree makes — ``resolve_terminal_id``
        fast-paths on ``[a-f0-9]{8}`` and extracts that same shape as the trailing
        segment of ``<profile>-<id>``, and ``generate_window_name`` builds the tmux
        window name from it. An off-shape id must be refused at the boundary with
        400; dropping the check adopts it and it becomes unresolvable later."""
        ctx, mock, attr = _patched_service(route)
        with ctx, patch("cli_agent_orchestrator.api.main.terminal_exists") as exists:
            response = _post(client, route, terminal_id=bad_id)

        assert response.status_code == 400
        assert "terminal_id" in str(response.json()["detail"])
        getattr(mock, attr).assert_not_called()
        exists.assert_not_called()


class TestIsBoxHostedFieldReachesTheService:
    """AC21's transport half (D16): the flag must survive the route.

    The shim decision itself is asserted through the real spawn path in
    ``test/services/test_f634_shim_host_awareness.py``; what this binds is that
    the route actually carries the field down to ``create_terminal``, since the
    decision executes inside the BOX server while the host catalog is
    laptop-side — the field IS the transport.
    """

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("supplied,expected", [(None, False), (True, True), (False, False)])
    def test_is_box_hosted_is_threaded_verbatim(self, route, supplied, expected, client):
        ctx, mock, attr = _patched_service(route)
        params = {} if supplied is None else {"is_box_hosted": supplied}
        with ctx, patch("cli_agent_orchestrator.api.main.terminal_exists", return_value=False):
            response = _post(client, route, **params)

        assert response.status_code == 201
        assert getattr(mock, attr).call_args.kwargs["is_box_hosted"] is expected


class TestBoxPlaneRecoveryRefusal:
    """AC20 third clause — ``POST /sessions/{name}/recover`` on a box-plane server.

    Both recover services re-create terminals, and neither is safe on a box:
    the epoch path threads no ``caller_id`` (the lane comes back UNSHIMMED) and
    the rebind path allocates the replacement id BOX-side, forking the single id
    namespace D11 depends on, while preserving ``caller_id`` so the replacement
    runs the F620 predicate hostless and lands on exit 97. Wave 1 refuses both,
    keyed on ``CAO_SERVER_PLANE=box`` — a SERVER-PLANE fact the refusing server
    can actually read.
    """

    @pytest.fixture
    def box_plane(self, monkeypatch):
        monkeypatch.setenv(CAO_SERVER_PLANE_ENV, "box")

    @pytest.mark.parametrize(
        "payload,guard_target",
        [
            (
                {"reason": "epoch"},
                "cli_agent_orchestrator.services.epoch_recovery_service.get_backend",
            ),
            (
                {"reason": "provider-reauth", "provider": "codex"},
                "cli_agent_orchestrator.services.provider_rebind_service."
                "list_terminals_by_session",
            ),
        ],
        ids=["epoch", "provider-reauth"],
    )
    def test_recover_is_refused_typed_and_creates_nothing(
        self, payload, guard_target, client, box_plane
    ):
        """MUTANT SENTINEL (launch the box server WITHOUT the plane marker, or
        drop the plane check). The refusal is TYPED (409 + code) so the caller
        can branch on it and relay, and it fires BEFORE the service touches a
        single terminal — ``guard_target`` is that service's first observable
        step, and it must never run. Without the marker the recovery door
        reopens and both wrong outcomes return."""
        with patch(guard_target) as guard:
            response = client.post("/sessions/cao-f634/recover", json=payload)

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "box_plane_recovery_refused"
        assert CAO_SERVER_PLANE_ENV in detail["message"]
        guard.assert_not_called()

    @pytest.mark.parametrize(
        "plane_value",
        ["", "laptop", "Box", "box "],
        ids=["absent-as-empty", "laptop", "wrong-case", "trailing-space-value"],
    )
    def test_laptop_plane_is_not_refused(self, plane_value, client, monkeypatch):
        """The refusal must be exact: only the literal ``box`` disarms recovery,
        so a typo can never silently take a LAPTOP server's recovery away. Each
        of these reaches the service and fails on its own terms (400/500), never
        with the box-plane code.

        ``"box "`` carries a trailing space and IS treated as box: the value is
        stripped before comparison, because an env export written by a shell
        script is the intended source of this key.
        """
        monkeypatch.setenv(CAO_SERVER_PLANE_ENV, plane_value)
        with patch("cli_agent_orchestrator.services.epoch_recovery_service.get_backend") as backend:
            backend.return_value.session_exists.return_value = False
            response = client.post("/sessions/cao-f634/recover", json={"reason": "epoch"})

        if plane_value == "box ":
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "box_plane_recovery_refused"
        else:
            assert response.status_code == 400
            assert response.json()["detail"] == "session_missing"

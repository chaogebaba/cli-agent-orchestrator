"""F829 A2 (r4) — PUBLIC create-route committed killers for M5, M7, M10, and
the empty/blank ``resume_from`` zero-spawn refusal.

The r1-r3 evidence for these three mutants was an OUT-OF-REPO HTTP probe: it
proved the production behaviour is correct, but an uncommitted probe does not
satisfy the brief's rule that every NAMED mutant needs a COMMITTED killer.
These arms drive ``POST /sessions/{name}/terminals`` — the route ``assign``
actually posts to — through the real assembled ASGI app (``client`` fixture,
``test/api/conftest.py``), so the mutant that survives committed selection now
FAILS a shipped regression guard.

Mutant map (each killed by ONE named test here):

* M5  — delete the create-route post-claim compensation
        (``except BaseException: … clear_resume_claim(event="resume_failed")``)
        → ``test_m5_post_claim_failure_compensates_the_claim``.
* M7  — let a COLD create (no ``resume_from``) enter resume admission
        → ``test_m7_cold_create_never_enters_admission``.
* M10 — map a seeded-uuid ``SeededSessionConflict`` to a generic 500 instead of
        the typed 409 ``session_identity_conflict`` envelope
        → ``test_m10_seeded_collision_returns_typed_409``.

Plus the fresh-adversary defect (codex r3): a present-but-blank ``resume_from``
must be a TYPED refusal with ZERO spawn, never a silent cold fallback
→ ``test_blank_resume_from_refused_zero_spawn`` (+ the MCP-shim sibling lives in
``test/mcp_server/test_assign_resume_from.py``).

And the r5 fresh-adversary defect (codex r4): an EXPLICIT JSON null
``{"resume_from": null}`` erased to the same ``None`` an omitted field
produces and cold-fell-back. The route now classifies actual request-field
presence from ``model_fields_set`` before the value is read
→ ``test_explicit_null_resume_from_refused_zero_spawn`` (with
``test_omitted_resume_from_is_cold_create`` as its boundary partner).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import RootAdmission, SeededSessionConflict
from cli_agent_orchestrator.models.terminal import Terminal

_ROUTE = "/sessions/cao-f829/terminals"


def _terminal(terminal_id: str = "res00001") -> Terminal:
    return Terminal(
        id=terminal_id,
        name=f"developer-{terminal_id}",
        session_name="cao-f829",
        provider="claude_code",
        agent_profile="developer",
    )


def _mock_terminal_service():
    """A terminal_service double whose create_terminal succeeds by default."""
    mock = MagicMock()
    mock.create_terminal = AsyncMock(return_value=_terminal())
    mock.seed_resume_bootstrap = AsyncMock(return_value=None)
    return mock


# ---------------------------------------------------------------------------
# M5 — post-claim compensation on the PUBLIC route.
# ---------------------------------------------------------------------------
def test_m5_post_claim_failure_compensates_the_claim(client):
    """MUTANT SENTINEL (delete the create-route ``except BaseException`` that
    releases the claim). Admission succeeds and takes the claim; the subsequent
    ``create_terminal`` fails. The route MUST release that claim with
    ``clear_resume_claim(event="resume_failed")`` so the conversation is not
    wedged as ``session_resume_in_progress`` until the TTL. Dropping the
    compensation leaves the claim held and this goes RED (clear never called).
    """
    claimed_key = "idk-claimed-m5"
    link = RootAdmission(mode="link", identity_key=claimed_key)

    svc = _mock_terminal_service()
    svc.create_terminal = AsyncMock(side_effect=RuntimeError("post-claim spawn boom"))

    with (
        # Admission runs to a successful CLAIM and hands back the claimed key.
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            AsyncMock(return_value=(link, claimed_key, {})),
        ),
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
        patch("cli_agent_orchestrator.clients.database.clear_resume_claim") as clear_claim,
    ):
        response = client.post(
            _ROUTE,
            params={
                "provider": "claude_code",
                "agent_profile": "developer",
                "caller_id": "abcd1234",
            },
            json={"resume_from": "old12345"},
        )

    # The failure surfaces (500 for a bare RuntimeError) — the POINT is the
    # compensation, which must have run on the way out.
    assert response.status_code >= 500
    clear_claim.assert_called_once_with(claimed_key, event="resume_failed")


# ---------------------------------------------------------------------------
# M7 — a cold create never enters admission.
# ---------------------------------------------------------------------------
def test_m7_cold_create_never_enters_admission(client):
    """MUTANT SENTINEL (let a cold create enter admission). A create with NO
    ``resume_from`` is a COLD spawn: ``_f829_admit_resume`` must NEVER run, and
    ``create_terminal`` must be called with ``root_admission=None``. The mutant
    that routes cold creates through admission trips ``admit.assert_not_called``.
    """
    svc = _mock_terminal_service()
    with (
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            AsyncMock(),
        ) as admit,
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
    ):
        response = client.post(
            _ROUTE,
            params={"provider": "claude_code", "agent_profile": "developer"},
        )

    assert response.status_code == 201
    admit.assert_not_called()
    assert svc.create_terminal.call_args.kwargs["root_admission"] is None


# ---------------------------------------------------------------------------
# M10 — seeded-uuid collision returns the typed 409, not a generic 500.
# ---------------------------------------------------------------------------
def test_m10_seeded_collision_returns_typed_409(client):
    """MUTANT SENTINEL (map ``SeededSessionConflict`` to a generic 500). A fresh
    seeded-uuid collision raised out of the create transaction must surface as a
    TYPED 409 ``resume_refused``/``session_identity_conflict`` envelope with
    ``retryable=false`` so the caller can branch on it. Mapping it to a generic
    500 (or letting it escape unhandled) makes the status/reason assertions RED.
    """
    svc = _mock_terminal_service()
    svc.create_terminal = AsyncMock(
        side_effect=SeededSessionConflict("idk-seed-m10", "idk-existing-m10")
    )
    with patch("cli_agent_orchestrator.api.main.terminal_service", svc):
        response = client.post(
            _ROUTE,
            params={"provider": "claude_code", "agent_profile": "developer"},
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "resume_refused"
    assert detail["reason"] == "session_identity_conflict"
    assert detail["retryable"] is False


# ---------------------------------------------------------------------------
# Fresh adversary — empty/blank resume_from is a typed refusal, ZERO spawn.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("blank", ["", " ", "\t", "   \n  "])
def test_blank_resume_from_refused_zero_spawn(client, blank):
    """codex r3 fresh adversary: ``{"resume_from": ""}`` (or all-whitespace) was
    accepted, ``_f829_admit_resume`` was NOT called, and ``create_terminal``
    ran with ``root_admission=None`` — an explicit malformed resume handle
    degrading SILENTLY to a cold create, against D3/AC2's no-cold-fallback rule.

    The route now classifies ``resume_from`` by PRESENCE, not truthiness: a
    present-but-blank handle is a TYPED ``resume_refused`` / ``resume_handle_blank``
    (missing=identity, retryable=false) with ZERO spawn. The mutant/regression
    that restores the truthiness gate lets cold-fallback back in and this goes
    RED (create fires, admission skipped, 201 returned).
    """
    svc = _mock_terminal_service()
    with (
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            AsyncMock(),
        ) as admit,
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
    ):
        response = client.post(
            _ROUTE,
            params={
                "provider": "claude_code",
                "agent_profile": "developer",
                "caller_id": "abcd1234",
            },
            json={"resume_from": blank},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "resume_refused"
    assert detail["reason"] == "resume_handle_blank"
    assert detail["retryable"] is False
    # ZERO spawn AND zero admission: neither the resume path nor the cold path ran.
    admit.assert_not_called()
    svc.create_terminal.assert_not_called()


# ---------------------------------------------------------------------------
# Fresh adversary (codex r4) — an EXPLICIT JSON null resume_from is a typed
# refusal, ZERO spawn. This is the presence-vs-value distinction the r4 gate
# missed: ``{"resume_from": null}`` erased to the SAME ``None`` an omitted
# field produces and cold-fell-back. The r5 route classifies actual field
# presence from ``model_fields_set`` BEFORE the value is read.
# ---------------------------------------------------------------------------
def test_explicit_null_resume_from_refused_zero_spawn(client):
    """codex r4 fresh adversary: ``{"resume_from": null}`` was accepted (201),
    ``_f829_admit_resume`` was NOT called, and ``create_terminal`` ran with
    ``root_admission=None`` — an EXPLICITLY supplied malformed resume field
    degrading SILENTLY to a cold create, the same D3/AC2 no-cold-fallback
    failure the empty/blank case was meant to close.

    Pydantic keeps the distinction the value cannot: an OMITTED field leaves
    ``model_fields_set`` empty (cold create), while an explicit ``null`` puts
    ``"resume_from"`` INTO ``model_fields_set``. The r5 route consults presence
    BEFORE erasing the value, so an explicit null is a TYPED ``resume_refused``
    / ``resume_handle_blank`` (missing=identity, retryable=false) with ZERO
    spawn. The regression that drops the presence check and classifies on the
    None value alone lets cold-fallback back in and this goes RED (create
    fires, admission skipped, 201 returned).
    """
    svc = _mock_terminal_service()
    with (
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            AsyncMock(),
        ) as admit,
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
    ):
        response = client.post(
            _ROUTE,
            params={
                "provider": "claude_code",
                "agent_profile": "developer",
                "caller_id": "abcd1234",
            },
            json={"resume_from": None},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "resume_refused"
    assert detail["reason"] == "resume_handle_blank"
    assert detail["retryable"] is False
    # ZERO spawn AND zero admission: neither the resume path nor the cold path ran.
    admit.assert_not_called()
    svc.create_terminal.assert_not_called()


# ---------------------------------------------------------------------------
# Companion — an OMITTED resume_from is still a genuine cold create (201),
# proving the presence gate refuses ONLY explicit nulls, not absence. This is
# the boundary partner of the explicit-null test: it pins that the r5 change
# did not turn every cold create into a refusal.
# ---------------------------------------------------------------------------
def test_omitted_resume_from_is_cold_create(client):
    """A request whose JSON body omits ``resume_from`` entirely (empty
    ``model_fields_set`` for that field) must remain a COLD create: 201, no
    admission, ``root_admission=None``. The mutant that refuses on ABSENCE
    rather than explicit presence breaks every cold create and trips this.
    """
    svc = _mock_terminal_service()
    with (
        patch(
            "cli_agent_orchestrator.api.main._f829_admit_resume",
            AsyncMock(),
        ) as admit,
        patch("cli_agent_orchestrator.api.main.terminal_service", svc),
    ):
        response = client.post(
            _ROUTE,
            params={
                "provider": "claude_code",
                "agent_profile": "developer",
                "caller_id": "abcd1234",
            },
            json={},
        )

    assert response.status_code == 201
    admit.assert_not_called()
    assert svc.create_terminal.call_args.kwargs["root_admission"] is None

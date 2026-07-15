"""HTTP contract tests for offline base registration and artifact-root validation."""

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import fork_context_service as svc


BODY = {
    "name": "offline",
    "provider": "codex",
    "session_uuid": "11111111-1111-4111-8111-111111111111",
    "cwd": "/repo",
    "profile": "codex_profile",
    "summary": "stored history",
}
ROW = {
    "name": "offline",
    "provider": "codex",
    "profile": "codex_profile",
    "cwd": "/repo",
    "session_uuid": BODY["session_uuid"],
    "kind": "base",
    "session_name": None,
    "source_terminal_id": None,
    "git_sha": "a" * 40,
    "dirty_hashes": "{}",
    "superseded": False,
}


@pytest.fixture(autouse=True)
def _clear_auth_override():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


def _scopes(values):
    async def dependency():
        return values

    return dependency


def test_registration_success_exact_projection(client, monkeypatch):
    register = patch.object(svc, "register_offline_base", return_value=ROW)
    with register as mocked:
        response = client.post("/bases/register", json=BODY)
    assert response.status_code == 200
    assert response.json() == ROW
    mocked.assert_called_once_with(
        name="offline", provider="codex", session_uuid=BODY["session_uuid"],
        cwd="/repo", agent_profile="codex_profile", summary="stored history",
    )


def test_registration_domain_reject_uses_stable_400_envelope(client):
    with patch.object(
        svc,
        "register_offline_base",
        side_effect=svc.OfflineBaseRegistrationError(
            "artifact_not_found", "provider artifact was not found"
        ),
    ):
        response = client.post("/bases/register", json=BODY)
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "artifact_not_found",
            "message": "provider artifact was not found",
        }
    }


def test_registration_malformed_body_remains_native_422(client):
    response = client.post("/bases/register", json={"name": "incomplete"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [([auth.SCOPE_READ], 403), ([auth.SCOPE_WRITE], 200), ([auth.SCOPE_ADMIN], 200)],
)
def test_registration_requires_write_or_admin_scope(
    client, monkeypatch, scopes, expected
):
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    app.dependency_overrides[auth.get_current_scopes] = _scopes(scopes)
    with patch.object(svc, "register_offline_base", return_value=ROW):
        response = client.post("/bases/register", json=BODY)
    assert response.status_code == expected


@pytest.mark.parametrize("route", ["/sessions", "/sessions/start"])
@pytest.mark.parametrize("value", ["", "tmp/orch"])
def test_launch_api_rejects_non_absolute_artifact_override(client, route, value):
    with patch(
        "cli_agent_orchestrator.services.session_service.create_session",
        new=AsyncMock(),
    ) as create:
        response = client.post(
            route,
            params={"agent_profile": "developer"},
            json={"env_vars": {"CAO_ARTIFACTS_DIR": value}},
        )
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "artifacts_dir_not_absolute",
            "message": "CAO_ARTIFACTS_DIR must be an absolute path",
        }
    }
    create.assert_not_awaited()

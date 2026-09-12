"""AC-31/32 connector contract and adversarial mutant coverage (Amendment D)."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cli_agent_orchestrator.api.routes_chatgpt_web_connector import (
    build_connector_app,
    connector_audit_projection,
)
from cli_agent_orchestrator.services.workspace_read import bind_attempt
from cli_agent_orchestrator.workspace_connector.auth_store import base64url_sha256
from cli_agent_orchestrator.workspace_connector.workspace_manager import (
    ACCESS_DENIED_SENSITIVE_FILE,
    PATH_NOT_IN_MANIFEST,
    PATH_OUTSIDE_WORKSPACE,
    Workspace,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "allowed.txt").write_text("canary\nsecond\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=do-not-disclose", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("not in manifest", encoding="utf-8")
    return tmp_path


def test_tool_list_is_exact_six(workspace: Path) -> None:
    server = bind_attempt(
        attempt_id="attempt-31",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".state",
    )
    names = {tool.name for tool in asyncio.run(server._mcp.list_tools())}  # type: ignore[attr-defined]
    assert names == {
        "workspace_info",
        "workspace_list_directory",
        "workspace_read_file",
        "workspace_search",
        "workspace_git_status",
        "workspace_git_diff",
    }


def test_containment_sensitive_and_manifest_mutants(workspace: Path) -> None:
    ws = Workspace(workspace, manifest=["allowed.txt", ".env.example"])
    with pytest.raises(Exception) as outside:
        ws.read_file("../outside.txt")
    assert outside.value.code == PATH_OUTSIDE_WORKSPACE
    with pytest.raises(Exception) as sensitive:
        ws.read_file(".env")
    assert sensitive.value.code == ACCESS_DENIED_SENSITIVE_FILE
    with pytest.raises(Exception) as unlisted:
        ws.read_file("outside.txt")
    assert unlisted.value.code == PATH_NOT_IN_MANIFEST
    assert ws.read_file(".env.example")["content"] == "SECRET="


def test_oauth_pkce_pairing_and_refresh_rotation(workspace: Path) -> None:
    server = bind_attempt(
        attempt_id="attempt-32",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".state",
    )
    oauth = server.oauth
    registration_status, registration = oauth.register(
        {"client_name": "test", "redirect_uris": ["http://127.0.0.1/callback"]}
    )
    assert registration_status == 201
    verifier = secrets.token_urlsafe(32)
    query = {
        "client_id": str(registration["client_id"]),
        "redirect_uri": "http://127.0.0.1/callback",
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": base64url_sha256(verifier),
        "scope": "workspace.read workspace.search git.read offline_access",
        "state": "s",
    }
    pairing = server.pairing.create()
    status, _, _ = oauth.authorize_get(query)
    assert status == 200
    request_id = next(iter(oauth.pending_requests))
    status, headers, _ = oauth.authorize_post(
        {"request_id": request_id, "pairing_code": str(pairing["code"])}
    )
    assert status == 302
    code = headers["location"].split("code=", 1)[1].split("&", 1)[0]
    status, tokens = oauth.token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": str(registration["client_id"]),
            "redirect_uri": "http://127.0.0.1/callback",
            "code_verifier": verifier,
        }
    )
    assert status == 200 and tokens["refresh_token"]
    replay_status, replay = oauth.token(
        {
            "grant_type": "refresh_token",
            "refresh_token": str(tokens["refresh_token"]),
            "client_id": str(registration["client_id"]),
        }
    )
    assert replay_status == 200 and replay["access_token"] != tokens["access_token"]
    replay_status, replay = oauth.token(
        {
            "grant_type": "refresh_token",
            "refresh_token": str(tokens["refresh_token"]),
            "client_id": str(registration["client_id"]),
        }
    )
    assert replay_status == 400 and replay["error"] == "invalid_grant"


def test_loopback_http_auth_and_repeated_access_calls(workspace: Path) -> None:
    server = bind_attempt(
        attempt_id="attempt-http",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".state",
    )
    tokens = server.store.issue_tokens(client_id="client", scopes=["workspace.read"])
    with TestClient(build_connector_app(server)) as client:
        unauth = client.post("/mcp", json={})
        assert unauth.status_code == 401
        auth = {"Authorization": f"Bearer {tokens['access_token']}"}
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        assert client.post("/mcp", json=init, headers=auth).status_code == 200
        for i in range(5):
            result = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": i + 2,
                    "method": "tools/call",
                    "params": {"name": "workspace_read_file", "arguments": {"path": "allowed.txt"}},
                },
                headers=auth,
            )
            assert result.status_code == 200
            assert "canary" in result.text
    projection = connector_audit_projection(server)
    assert projection and all("content" not in row for row in projection)

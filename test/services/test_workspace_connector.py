"""AC-31/32 connector contract and adversarial mutant coverage (Amendment D)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from test.fixtures.workspace_connector_loopback import CLIENT_SCRIPT, redacted_cases, run_listener

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


def test_manifest_binding_and_aggregate_budget(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHATGPT_PULL_ATTEMPT_BYTES", "350")
    server = bind_attempt(
        attempt_id="attempt-budget",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".budget-state",
    )
    token = server.store.issue_tokens(client_id="client", scopes=["workspace.read"])["access_token"]
    other = bind_attempt(
        attempt_id="other-attempt",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".other-state",
    )
    verdict = other.store.verify_access_token(token)
    assert verdict[0] is False
    first = server.tools.workspace_read_file("allowed.txt")
    assert not first.is_error
    second = server.tools.workspace_read_file("allowed.txt")
    assert second.is_error and second.error == "PULL_BUDGET_EXHAUSTED"


TOOL_SCOPE_CASES = (
    ("workspace_info", {}, "workspace.read"),
    ("workspace_list_directory", {"path": "."}, "workspace.read"),
    ("workspace_read_file", {"path": "allowed.txt"}, "workspace.read"),
    ("workspace_search", {"query": "canary"}, "workspace.search"),
    ("workspace_git_status", {}, "git.read"),
    ("workspace_git_diff", {}, "git.read"),
)


def _call_tool(
    client: TestClient,
    token: str,
    name: str,
    arguments: dict[str, object],
    request_id: int,
) -> dict[str, object]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(("tool_name", "arguments", "required_scope"), TOOL_SCOPE_CASES)
def test_http_tool_scope_matrix(
    workspace: Path,
    tool_name: str,
    arguments: dict[str, object],
    required_scope: str,
) -> None:
    """Every external wrapper propagates scopes: absent/wrong deny, exact scope permits."""
    server = bind_attempt(
        attempt_id=f"scope-{tool_name}",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / f".{tool_name}-state",
    )
    no_scope = server.store.issue_tokens(client_id="none", scopes=[])["access_token"]
    wrong_scope_name = {
        "workspace.read": "workspace.search",
        "workspace.search": "git.read",
        "git.read": "workspace.read",
    }[required_scope]
    wrong_scope = server.store.issue_tokens(client_id="wrong", scopes=[wrong_scope_name])[
        "access_token"
    ]
    right_scope = server.store.issue_tokens(client_id="right", scopes=[required_scope])[
        "access_token"
    ]

    with TestClient(build_connector_app(server)) as client:
        missing = _call_tool(client, no_scope, tool_name, arguments, 100)
        wrong = _call_tool(client, wrong_scope, tool_name, arguments, 101)
        allowed = _call_tool(client, right_scope, tool_name, arguments, 102)

    assert missing["result"]["isError"] is True
    assert required_scope in str(missing)
    assert wrong["result"]["isError"] is True
    assert required_scope in str(wrong)
    assert allowed["result"]["isError"] is False


def test_http_cross_attempt_token_is_rejected(workspace: Path) -> None:
    server = bind_attempt(
        attempt_id="expected-attempt",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".cross-attempt-state",
    )
    token = server.store.issue_tokens(
        client_id="other",
        scopes=["workspace.read"],
        attempt_id="other-attempt",
    )["access_token"]
    with TestClient(build_connector_app(server)) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workspace_info", "arguments": {}},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 403
    assert response.json()["error"] == "PATH_NOT_IN_MANIFEST"


def test_real_loopback_privilege_separated_bearer_guard(workspace: Path) -> None:
    """D9: a different OS UID reaches real HTTP but cannot bypass the first guard."""
    if os.name != "posix" or shutil.which("sudo") is None:
        pytest.skip("requires POSIX and sudo -n permission to run the client as nobody")
    client_command = ["sudo", "-n", "-u", "nobody", "/usr/bin/python3", "-I", "-c"]
    privilege_probe = subprocess.run(
        [*client_command, "import os; print(os.geteuid())"],
        cwd="/",
        capture_output=True,
        text=True,
        timeout=5,
    )
    if privilege_probe.returncode:
        pytest.skip("requires sudo -n permission to run the client as nobody")
    assert int(privilege_probe.stdout) != os.geteuid()
    server = bind_attempt(
        attempt_id="external-attempt",
        frozen_worktree=workspace,
        manifest=["allowed.txt"],
        state_dir=workspace / ".external-state",
    )
    tokens = {
        "read": server.store.issue_tokens(client_id="read", scopes=["workspace.read"])[
            "access_token"
        ],
        "empty": server.store.issue_tokens(client_id="empty", scopes=[])["access_token"],
        "search": server.store.issue_tokens(client_id="search", scopes=["workspace.search"])[
            "access_token"
        ],
        "attempt": server.store.issue_tokens(
            client_id="attempt", scopes=["workspace.read"], attempt_id="another-attempt"
        )["access_token"],
        "manifest": server.store.issue_tokens(
            client_id="manifest", scopes=["workspace.read"], manifest_digest="sha256:wrong"
        )["access_token"],
    }
    context = multiprocessing.get_context("fork")
    audit_reader, audit_writer = context.Pipe(duplex=False)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()
    assert host == "127.0.0.1"
    stop = context.Event()
    process = context.Process(target=run_listener, args=(server, listener, stop, audit_writer))
    process.start()
    listener.close()
    audit_writer.close()
    try:
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(f"http://{host}:{port}/mcp", timeout=0.5)
            except urllib.error.HTTPError as response:
                assert response.code == 401
                break
            except (OSError, TimeoutError):
                assert process.is_alive(), "loopback listener exited before becoming ready"
                assert time.monotonic() < deadline, "loopback listener did not become ready"
                time.sleep(0.05)
        external = subprocess.run(
            [*client_command, CLIENT_SCRIPT],
            input=json.dumps(
                {
                    "url": f"http://{host}:{port}/mcp/",
                    "source_path": str(workspace / "allowed.txt"),
                    "canary": "canary",
                    "cases": redacted_cases(tokens),
                }
            ),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=20,
        )
        if external.returncode:
            print("B2_LOOPBACK_CLIENT_FAILURE=" + external.stdout.strip())
        assert external.returncode == 0, "privilege-separated client failed"
        evidence = json.loads(external.stdout)
        print("B2_LOOPBACK_EXTERNAL=" + json.dumps(evidence, sort_keys=True))
        assert evidence["uid"] != os.geteuid()
        assert evidence["source_readable"] is False
        results = {row["name"]: row for row in evidence["results"]}
        for name in ("unauthenticated", "unauthenticated-malformed", "invalid-token"):
            assert results[name]["status"] == 401
            assert results[name]["challenge"] is True
            assert results[name]["canary"] is False
        for name in ("empty-scope", "wrong-scope"):
            assert results[name]["status"] == 200
            assert results[name]["is_error"] is True
            assert results[name]["scope_refusal"] is True
            assert results[name]["canary"] is False
        for name in ("wrong-attempt", "wrong-manifest"):
            assert results[name]["status"] == 403
            assert results[name]["error"] == PATH_NOT_IN_MANIFEST
            assert results[name]["canary"] is False
        assert results["unlisted-path"]["is_error"] is True
        assert results["unlisted-path"]["canary"] is False
        for index in range(5):
            assert results[f"read-{index}"]["status"] == 200
            assert results[f"read-{index}"]["is_error"] is False
            assert results[f"read-{index}"]["canary"] is True
    finally:
        stop.set()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
    assert process.exitcode == 0
    assert audit_reader.poll(5), "listener did not return its redacted audit projection"
    projection = audit_reader.recv()
    audit_reader.close()
    assert len(projection) == 8
    assert sum(row["result_digest"] is not None for row in projection) == 5
    assert sum(row["refusal_code"] == "INSUFFICIENT_SCOPE" for row in projection) == 2
    assert sum(row["refusal_code"] == PATH_NOT_IN_MANIFEST for row in projection) == 1
    print(
        "B2_LOOPBACK_AUDIT="
        + json.dumps(
            {
                "attempt_ids": sorted({row["attempt_id"] for row in projection}),
                "digests": sum(row["result_digest"] is not None for row in projection),
                "insufficient_scope": sum(
                    row["refusal_code"] == "INSUFFICIENT_SCOPE" for row in projection
                ),
                "path_not_in_manifest": sum(
                    row["refusal_code"] == PATH_NOT_IN_MANIFEST for row in projection
                ),
                "rows": len(projection),
            },
            sort_keys=True,
        )
    )
    serialized = json.dumps(projection)
    assert "canary" not in serialized and "SECRET" not in serialized
    assert all(token not in serialized for token in tokens.values())
    assert all(row["attempt_id"] == "external-attempt" for row in projection)

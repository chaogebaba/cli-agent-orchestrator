from unittest.mock import patch


def test_codex_review_endpoint_starts_async_review(client):
    with (
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as metadata,
        patch("cli_agent_orchestrator.api.main.codex_review_service.start_codex_review") as start,
    ):
        metadata.return_value = {"id": "deadbeef"}
        start.return_value = {
            "success": True,
            "review_id": "abc123ef",
            "terminal_id": "abc123ef",
            "findings_file": "/repo/tmp/orch/review-abc123ef.md",
            "command": ["codex", "review", "--uncommitted"],
        }

        response = client.post(
            "/codex-review",
            json={
                "requester_id": "deadbeef",
                "scope": "uncommitted",
                "cwd": "/repo",
            },
        )

    assert response.status_code == 200
    assert response.json()["review_id"] == "abc123ef"
    start.assert_called_once()
    assert start.call_args.kwargs["requester_id"] == "deadbeef"
    assert start.call_args.kwargs["instructions"] is None
    assert start.call_args.kwargs["scope"] == "uncommitted"
    assert start.call_args.kwargs["cwd"] == "/repo"


def test_codex_review_endpoint_rejects_unknown_requester(client):
    with patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as metadata:
        metadata.return_value = None

        response = client.post(
            "/codex-review",
            json={"requester_id": "deadbeef", "instructions": "focus"},
        )

    assert response.status_code == 404
    assert "Terminal 'deadbeef' not found" in response.json()["detail"]


def test_codex_review_endpoint_surfaces_contract_errors(client):
    with (
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as metadata,
        patch("cli_agent_orchestrator.api.main.codex_review_service.start_codex_review") as start,
    ):
        metadata.return_value = {"id": "deadbeef"}
        start.side_effect = ValueError("target is required when scope is 'base'")

        response = client.post(
            "/codex-review",
            json={"requester_id": "deadbeef", "instructions": "focus", "scope": "base"},
        )

    assert response.status_code == 400
    assert "target is required" in response.json()["detail"]


def test_codex_review_endpoint_surfaces_missing_cwd(client):
    with (
        patch("cli_agent_orchestrator.api.main.get_terminal_metadata") as metadata,
        patch("cli_agent_orchestrator.api.main.codex_review_service.start_codex_review") as start,
    ):
        metadata.return_value = {"id": "deadbeef"}
        start.side_effect = ValueError("cwd is required")

        response = client.post(
            "/codex-review",
            json={"requester_id": "deadbeef", "scope": "uncommitted"},
        )

    assert response.status_code == 400
    assert "cwd is required" in response.json()["detail"]

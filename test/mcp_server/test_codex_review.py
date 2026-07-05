from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.mcp_server import server


def test_codex_review_impl_posts_requester_and_required_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("CAO_TERMINAL_ID", "deadbeef")
    monkeypatch.setattr(server, "_mcp_timeout", lambda: 3.0)

    response = Mock()
    response.json.return_value = {
        "success": True,
        "review_id": "abc123ef",
        "terminal_id": "abc123ef",
        "findings_file": f"{tmp_path}/tmp/orch/review-abc123ef.md",
    }
    response.raise_for_status.return_value = None
    post = Mock(return_value=response)
    monkeypatch.setattr(server.requests, "post", post)

    result = server._codex_review_impl(scope="uncommitted", cwd=str(tmp_path))

    assert result["success"] is True
    post.assert_called_once_with(
        f"{server.API_BASE_URL}/codex-review",
        json={
            "requester_id": "deadbeef",
            "cwd": str(tmp_path),
            "scope": "uncommitted",
        },
        timeout=3.0,
    )


def test_codex_review_impl_requires_terminal_context(monkeypatch):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

    result = server._codex_review_impl("focus on X", cwd="/repo")

    assert result["success"] is False
    assert "CAO_TERMINAL_ID not set" in result["error"]


def test_codex_review_impl_requires_cwd(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "deadbeef")

    with pytest.raises(ValueError, match="cwd is required"):
        server._codex_review_impl(scope="uncommitted")

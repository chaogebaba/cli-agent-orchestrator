"""RESUME HOT-FIX — assign(resume_from=…) handler-level tests.

Verifies the assign handler:
* returns the ONE typed ``resume_refused`` dict on an unknown resume_from,
  WITHOUT creating any terminal (deliverable 4 + no-spawn on refusal);
* on a resolvable reaped identity, builds a ForkContext(mode="resume") and hands
  it to the create path with the resolved provider/cwd (deliverable 1);
* rejects resume_from + fork_from together.
"""

from types import SimpleNamespace
from unittest.mock import patch

from cli_agent_orchestrator.mcp_server import server


def _identity(**over):
    row = {
        "terminal_id": "old12345",
        "provider": "kiro_cli",
        "agent_profile": "kiro_dev",
        "cwd": "/repo/wt",
        "session_name": "cao-x",
        "provider_session_id": "sess_dead-0000-0000-0000-000000000001",
        "base_name": "old12345",
        "worktree_path": "/repo/wt",
        "created_at": None,
        "reaped_at": None,
        "lifecycle": "reaped",
    }
    row.update(over)
    return row


def test_unknown_resume_from_refuses_without_spawn(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_identity",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.clients.database."
            "get_terminal_identity_by_provider_session_id",
            return_value=None,
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_provider_session_by_uuid",
            return_value=None,
        ),
        patch.object(server, "_create_terminal") as create,
    ):
        result = server._assign_impl("kiro_dev", "task", resume_from="ghost-terminal")
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["missing"] == "identity"
    assert "how" in result
    create.assert_not_called()


def test_resume_from_and_fork_from_mutually_exclusive(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", fork_from="base", resume_from="old12345")
    assert result["success"] is False
    assert "mutually exclusive" in result["message"]
    create.assert_not_called()


def test_resume_from_builds_resume_fork_context(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_identity",
            return_value=_identity(),
        ),
        patch("os.path.isdir", return_value=True),
        patch.object(server, "_create_terminal", return_value=("new00001", None)) as create,
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"resolved_model": None},
        ),
        patch.object(server, "generate_window_name", return_value="w"),
        patch.object(server, "display_name", return_value="kiro_dev(new00001)"),
    ):
        result = server._assign_impl("kiro_dev", "task", resume_from="old12345")
    assert result["success"] is True
    assert result["terminal_id"] == "new00001"
    # A resume ForkContext was handed to the create path.
    fc = create.call_args.kwargs["fork_context"]
    assert fc is not None and fc.mode == "resume"
    assert fc.session_uuid == "sess_dead-0000-0000-0000-000000000001"
    assert fc.provider == "kiro_cli"
    # cwd resolved from the identity row.
    assert create.call_args.args[1] == "/repo/wt"
    assert result["forked_from"]["resumed_from"] == "old12345"

"""RESUME HOT-FIX — assign(resume_from=…) handler-level tests (+ addenda r1/r2).

Verifies the assign handler:
* returns the ONE typed ``resume_refused`` envelope (error/missing/how/reason/
  retryable) on an unknown resume_from, WITHOUT creating any terminal;
* on a resolvable reaped identity, builds ForkContext(mode="resume") and hands
  it to the create path with the resolved provider/cwd, and surfaces the r1 #8
  success line (resumed from <old> as <new>, worktree, pins_inherited);
* rejects resume_from + fork_from as an input CONFLICT (r1 #1);
* delegates legacy fork_from+resume=True into the SAME resume service (r1 #1);
* refuses resume=True alone (no handle).
"""

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
        "worktree_branch": "cao/old12345",
        "worktree_repo_root": "/repo",
        "git_sha": "a" * 40,
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
        patch.object(server, "_create_terminal") as create,
    ):
        result = server._assign_impl("kiro_dev", "task", resume_from="ghost-terminal")
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["missing"] == "identity"
    assert result["reason"] == "no_identity_match"
    assert result["retryable"] is False
    assert "how" in result
    create.assert_not_called()


def test_resume_from_plus_fork_from_is_input_conflict(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", fork_from="base", resume_from="old12345")
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_input_conflict"
    create.assert_not_called()


def test_resume_true_without_handle_refuses(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch.object(server, "_create_terminal") as create:
        result = server._assign_impl("kiro_dev", "task", resume=True)
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_true_without_handle"
    create.assert_not_called()


def _resume_patches():
    return (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_identity",
            return_value=_identity(),
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_frozen_pins",
            return_value=[],
        ),
        patch("os.path.isdir", return_value=True),
        patch.object(server, "_create_terminal", return_value=("new00001", None)),
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"resolved_model": None},
        ),
        patch.object(server, "generate_window_name", return_value="w"),
        patch.object(server, "display_name", return_value="kiro_dev(new00001)"),
    )


def test_resume_from_builds_resume_fork_context(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_id, p_pins, p_isdir, p_create, p_meta, p_win, p_dn = _resume_patches()
    with p_id, p_pins, p_isdir, p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl("kiro_dev", "task", resume_from="old12345")
    assert result["success"] is True
    assert result["terminal_id"] == "new00001"
    fc = create.call_args.kwargs["fork_context"]
    assert fc is not None and fc.mode == "resume"
    assert fc.session_uuid == "sess_dead-0000-0000-0000-000000000001"
    assert fc.provider == "kiro_cli"
    assert create.call_args.args[1] == "/repo/wt"  # cwd from identity
    # r1 #8 success line.
    assert result["resumed_from"] == "old12345"
    assert result["worktree"] == "/repo/wt"
    assert result["pins_inherited"] == 0
    assert "resumed from old12345 as new00001" in result["resume_line"]


def test_legacy_fork_from_resume_delegates_to_resume_service(monkeypatch):
    """r1 #1: fork_from + resume=True routes through the SAME resume service."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_id, p_pins, p_isdir, p_create, p_meta, p_win, p_dn = _resume_patches()
    with p_id, p_pins, p_isdir, p_create as create, p_meta, p_win, p_dn:
        result = server._assign_impl("kiro_dev", "task", fork_from="old12345", resume=True)
    assert result["success"] is True
    fc = create.call_args.kwargs["fork_context"]
    assert fc is not None and fc.mode == "resume"
    assert result["resumed_from"] == "old12345"


def test_inherit_pins_false_with_known_pins_refuses_profile(monkeypatch):
    """r1 #5: inherit_pins=False when the reaped terminal HAD pins and no
    replacement authority_files → resume_refused{missing:profile}."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.clients.database.get_terminal_identity",
            return_value=_identity(),
        ),
        patch(
            "cli_agent_orchestrator.clients.database.get_frozen_pins",
            return_value=[{"file_path": "/a/b.md", "sha256": "b" * 64}],
        ),
        patch("os.path.isdir", return_value=True),
        patch.object(server, "_create_terminal") as create,
    ):
        result = server._assign_impl("kiro_dev", "task", resume_from="old12345", inherit_pins=False)
    assert result["success"] is False
    assert result["error"] == "resume_refused"
    assert result["missing"] == "profile"
    assert result["reason"] == "pins_dropped_without_replacement"
    create.assert_not_called()

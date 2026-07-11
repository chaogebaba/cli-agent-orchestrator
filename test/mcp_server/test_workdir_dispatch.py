import asyncio
import inspect
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server import server


def _git(*args):
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


def _init_repo(path: Path, *, commit=False):
    _git("init", "-q", path)
    if commit:
        _git("-C", path, "config", "user.email", "test@example.com")
        _git("-C", path, "config", "user.name", "test")
        (path / "a").write_text("a")
        _git("-C", path, "add", "a")
        _git("-C", path, "commit", "-qm", "init")


def _fork_assign(row, requested=None):
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abcd1234"}), patch(
        "cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=row
    ), patch.object(server, "resolve_provider", return_value=row["provider"]), patch(
        "pathlib.Path.glob",
        return_value=[SimpleNamespace(name=f"rollout-{row['session_uuid']}.jsonl")],
    ), patch(
        "cli_agent_orchestrator.services.fork_context_service.staleness",
        return_value=([], "[FRESH]"),
    ) as stale, patch.object(server, "_create_terminal") as create:
        create.return_value = ("feed1234", row["provider"])
        result = server._assign_impl(
            "developer", "task", working_directory=requested, fork_from="base"
        )
    return result, create, stale


def test_assign_and_handoff_always_expose_working_directory():
    assert "working_directory" in inspect.signature(server.assign).parameters
    assert "working_directory" in inspect.signature(server.handoff).parameters


@pytest.mark.parametrize("mode", ["missing_env", "non_200", "empty", "request_error"])
def test_strict_supervisor_cwd_failure_modes(mode):
    env = {} if mode == "missing_env" else {"CAO_TERMINAL_ID": "abcd1234"}
    response = MagicMock()
    response.json.return_value = {"working_directory": None if mode == "empty" else "/repo"}
    if mode == "non_200":
        response.raise_for_status.side_effect = server.requests.HTTPError("404")
    side_effect = server.requests.ConnectionError("down") if mode == "request_error" else None
    with patch.dict(os.environ, env, clear=True), patch.object(
        server.requests, "get", return_value=response, side_effect=side_effect
    ):
        with pytest.raises(ValueError, match="supervisor_working_directory_unavailable"):
            server.strict_supervisor_cwd()


def test_assign_omitted_uses_strict_supervisor_cwd():
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abcd1234"}), patch.object(
        server, "strict_supervisor_cwd", return_value="/supervisor"
    ) as cwd, patch.object(server, "_create_terminal", return_value=("feed1234", "codex")) as create:
        result = server._assign_impl("developer", "task")
    assert result["success"] is True
    cwd.assert_called_once_with()
    assert create.call_args.args[1] == "/supervisor"


def test_handoff_omitted_uses_strict_supervisor_cwd():
    with patch.object(server, "strict_supervisor_cwd", return_value="/supervisor") as cwd, patch.object(
        server, "_resolve_handoff_provider", return_value=server.HandoffContext("kiro_cli", None, None, None)
    ), patch.object(server.requests, "post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "terminal_id": "feed1234", "last_message": "done", "status": "completed"
        }
        result = asyncio.run(server._handoff_impl("developer", "task"))
    assert result.success
    cwd.assert_called_once_with()
    assert post.call_args.kwargs["json"]["working_directory"] == "/supervisor"


def test_explicit_workdir_threads_through_assign_and_handoff(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abcd1234"}), patch.object(
        server, "strict_supervisor_cwd"
    ) as strict, patch.object(
        server, "_create_terminal", return_value=("feed1234", "codex")
    ) as create:
        assert server._assign_impl("developer", "task", str(alias))["success"]
    strict.assert_not_called()
    assert create.call_args.args[1] == str(alias)

    with patch.object(server, "strict_supervisor_cwd") as strict, patch.object(
        server, "_resolve_handoff_provider",
        return_value=server.HandoffContext("kiro_cli", None, None, None),
    ), patch.object(server.requests, "post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "terminal_id": "feed1234", "last_message": "done", "status": "completed"
        }
        assert asyncio.run(
            server._handoff_impl("developer", "task", working_directory=str(alias))
        ).success
    strict.assert_not_called()
    assert post.call_args.kwargs["json"]["working_directory"] == str(alias)


def test_mcp_invalid_workdir_surfaces_detail():
    detail = "invalid_working_directory: Working directory does not exist: /missing"
    with patch.dict(os.environ, {"CAO_TERMINAL_ID": "abcd1234"}), patch.object(
        server, "_create_terminal", side_effect=ValueError(detail)
    ):
        result = server._assign_impl("developer", "task", "/missing")
    assert result["message"] == f"Assignment failed: {detail}"

    response = MagicMock(status_code=400)
    response.json.return_value = {"detail": detail}
    with patch.object(
        server, "_resolve_handoff_provider",
        return_value=server.HandoffContext("kiro_cli", None, None, None),
    ), patch.object(server.requests, "post", return_value=response):
        result = asyncio.run(
            server._handoff_impl("developer", "task", working_directory="/missing")
        )
    assert result.message == f"Handoff failed: {detail}"


@pytest.mark.parametrize("mode", ["missing_env", "non_200", "empty", "request_error"])
@pytest.mark.parametrize("tool", ["assign", "handoff"])
def test_strict_failures_stop_each_tool_before_dispatch(mode, tool):
    env = {} if mode == "missing_env" else {"CAO_TERMINAL_ID": "abcd1234"}
    response = MagicMock()
    response.json.return_value = {"working_directory": None if mode == "empty" else "/repo"}
    if mode == "non_200":
        response.raise_for_status.side_effect = server.requests.HTTPError("404")
    get_effect = server.requests.ConnectionError("down") if mode == "request_error" else None
    with patch.dict(os.environ, env, clear=True), patch.object(
        server.requests, "get", return_value=response, side_effect=get_effect
    ), patch.object(server, "_create_terminal") as create, patch.object(
        server.requests, "post"
    ) as post:
        if tool == "assign":
            result = server._assign_impl("developer", "task")
            message = result["message"]
        else:
            result = asyncio.run(server._handoff_impl("developer", "task"))
            message = result.message
    assert "supervisor_working_directory_unavailable" in message
    create.assert_not_called()
    post.assert_not_called()


def test_fork_omitted_uses_base_and_never_supervisor_default(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    row = {
        "name": "base", "provider": "codex", "session_uuid": "uuid",
        "cwd": str(repo), "agent_profile": "developer",
    }
    with patch.object(server, "strict_supervisor_cwd") as strict:
        result, create, _ = _fork_assign(row)
    assert result["success"]
    strict.assert_not_called()
    assert create.call_args.args[1] == str(repo)


def test_grok_distinct_fork_workdir_rejected():
    row = {"provider": "grok_cli", "cwd": "/base"}
    with pytest.raises(ValueError, match="provider_unsupported"):
        server._resolve_fork_working_directory(row, "/target")


def test_codex_linked_worktree_allowed_and_preamble(tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    _init_repo(repo, commit=True)
    _git("-C", repo, "worktree", "add", "-q", worktree, "-b", "other")
    launch, line = server._resolve_fork_working_directory(
        {"provider": "codex", "cwd": str(repo)}, str(worktree)
    )
    assert launch == str(worktree)
    assert line == f"[WORKDIR] launched in {worktree}, base snapshot taken in {repo}."

    row = {
        "name": "base", "provider": "codex", "session_uuid": "uuid",
        "cwd": str(repo), "agent_profile": "developer",
    }
    result, create, stale = _fork_assign(row, str(worktree))
    assert result["success"]
    stale.assert_called_once_with(row)
    fork_context = create.call_args.kwargs["fork_context"]
    assert f"base snapshot taken in {repo}" in fork_context.initial_preamble


def test_git_identity_ignores_git_dir_environment(tmp_path, monkeypatch):
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    _init_repo(repo_a)
    _init_repo(repo_b)
    monkeypatch.setenv("GIT_DIR", str(repo_a / ".git"))
    with pytest.raises(ValueError, match="mismatch"):
        server._resolve_fork_working_directory(
            {"provider": "codex", "cwd": str(repo_a)}, str(repo_b)
        )


def test_git_identity_uses_absolute_common_dir_flag(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    real_run = server.subprocess.run
    with patch.object(server.subprocess, "run", wraps=real_run) as run:
        server._git_identity(str(repo))
    assert any("--path-format=absolute" in call.args[0] for call in run.call_args_list)


def test_different_repositories_error_names_both_paths_and_does_not_spawn(tmp_path):
    base, requested = tmp_path / "base", tmp_path / "requested"
    _init_repo(base)
    _init_repo(requested)
    row = {
        "name": "base", "provider": "codex", "session_uuid": "uuid",
        "cwd": str(base), "agent_profile": "developer",
    }
    result, create, _ = _fork_assign(row, str(requested))
    assert not result["success"]
    assert str(base) in result["message"]
    assert str(requested) in result["message"]
    create.assert_not_called()


@pytest.mark.parametrize("case", ["deleted_base", "nongit_target", "nongit_base"])
def test_unprovable_fork_identity_names_paths_and_does_not_spawn(tmp_path, case):
    base, requested = tmp_path / "base", tmp_path / "requested"
    base.mkdir()
    requested.mkdir()
    if case != "nongit_base":
        _git("init", "-q", base)
    if case != "nongit_target":
        _git("init", "-q", requested)
    if case == "deleted_base":
        shutil.rmtree(base)
    row = {
        "name": "base", "provider": "codex", "session_uuid": "uuid",
        "cwd": str(base), "agent_profile": "developer",
    }
    result, create, _ = _fork_assign(row, str(requested))
    assert not result["success"]
    assert str(base) in result["message"]
    assert str(requested) in result["message"]
    create.assert_not_called()


def test_rev_parse_execution_error_names_paths_and_does_not_spawn(tmp_path):
    base, requested = tmp_path / "base", tmp_path / "requested"
    base.mkdir()
    requested.mkdir()
    row = {
        "name": "base", "provider": "codex", "session_uuid": "uuid",
        "cwd": str(base), "agent_profile": "developer",
    }
    with patch.object(server.subprocess, "run", side_effect=OSError("git unavailable")):
        result, create, _ = _fork_assign(row, str(requested))
    assert not result["success"]
    assert str(base) in result["message"]
    assert str(requested) in result["message"]
    create.assert_not_called()

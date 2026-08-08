"""Tests for f100 Batch 2: fork dispatch provenance (A1, A2, A3) + StatusMonitor (B5)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.server import (
    _assign_impl,
    _CrossRepoBaseExclusion,
    _resolve_fork_working_directory,
)

ROW = {
    "name": "base-oracle",
    "provider": "codex",
    "session_uuid": "11111111-1111-4111-8111-111111111111",
    "cwd": "/repo",
    "agent_profile": "developer",
    "git_sha": "a" * 40,
    "dirty_hashes": "{}",
    "source_terminal_id": None,
}


# ---------------------------------------------------------------------------
# A1: assign result reports fork source
# ---------------------------------------------------------------------------


class TestForkedFromProvenance:
    """A1: assign response includes forked_from dict when forking."""

    @patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", return_value=("w1", "codex")
    )
    @patch(
        "cli_agent_orchestrator.mcp_server.server._resolve_fork_working_directory",
        return_value=("/repo", ""),
    )
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex")
    @patch("cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=ROW)
    @patch("cli_agent_orchestrator.services.fork_context_service.staleness")
    @patch("cli_agent_orchestrator.services.fork_context_service.validate_base_source")
    def test_forked_from_present_on_fork(
        self,
        _validate,
        mock_staleness,
        _resolve_base,
        _resolve_provider,
        _resolve_wd,
        _create,
        monkeypatch,
    ):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
        mock_staleness.return_value = SimpleNamespace(
            preamble="", changed_count=2, delta=SimpleNamespace(is_acquisition_failure=False)
        )
        mock_staleness.return_value.__bool__ = lambda self: True
        result = _assign_impl("developer", "task", fork_from="base-oracle")
        assert result["success"] is True
        assert result["forked_from"] is not None
        assert result["forked_from"]["name"] == "base-oracle"
        assert result["forked_from"]["cwd"] == "/repo"
        assert result["forked_from"]["git_sha"] == "a" * 40
        assert result["forked_from"]["staleness_count"] == 2

    @patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", return_value=("w2", "codex")
    )
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/work")
    def test_forked_from_null_on_cold_assign(self, _cwd, _create, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
        result = _assign_impl("developer", "task")
        assert result["success"] is True
        assert result["forked_from"] is None


# ---------------------------------------------------------------------------
# A2: Cross-repo base exclusion + explicit working_directory wins
# ---------------------------------------------------------------------------


class TestCrossRepoBaseExclusion:
    """A2: _resolve_fork_working_directory raises _CrossRepoBaseExclusion for different repos."""

    @patch("cli_agent_orchestrator.mcp_server.server._git_identity")
    def test_different_repos_raises_exclusion(self, mock_identity):
        mock_identity.side_effect = [
            ("/repo-a", "/common-a"),  # base
            ("/repo-b", "/common-b"),  # target
        ]
        row = {"cwd": "/repo-a/src", "provider": "codex"}
        with pytest.raises(_CrossRepoBaseExclusion) as exc_info:
            _resolve_fork_working_directory(row, "/repo-b/src")
        assert exc_info.value.base_top == "/repo-a"
        assert exc_info.value.target_top == "/repo-b"

    @patch("cli_agent_orchestrator.mcp_server.server._git_identity")
    def test_same_repo_returns_preamble(self, mock_identity):
        mock_identity.side_effect = [
            ("/repo", "/common"),  # base
            ("/repo", "/common"),  # target
        ]
        row = {"cwd": "/repo/sub-a", "provider": "kiro_cli"}
        cwd, preamble = _resolve_fork_working_directory(row, "/repo/sub-b")
        assert cwd == "/repo/sub-b"
        assert "[WORKDIR]" in preamble

    @patch("cli_agent_orchestrator.mcp_server.server._git_identity")
    def test_base_identity_failure_returns_warning(self, mock_identity):
        mock_identity.side_effect = ValueError("not a git repo")
        row = {"cwd": "/not-git", "provider": "codex"}
        cwd, preamble = _resolve_fork_working_directory(row, "/target")
        assert cwd == "/target"
        assert "warning" in preamble.lower()

    @patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", return_value=("w3", "codex")
    )
    @patch("cli_agent_orchestrator.mcp_server.server._resolve_fork_working_directory")
    @patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex")
    @patch("cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=ROW)
    @patch("cli_agent_orchestrator.services.fork_context_service.validate_base_source")
    def test_cross_repo_exclusion_falls_back_cold(
        self, _validate, _resolve_base, _resolve_provider, mock_resolve_wd, _create, monkeypatch
    ):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
        mock_resolve_wd.side_effect = _CrossRepoBaseExclusion("/repo-a", "/repo-b")
        result = _assign_impl(
            "developer", "task", fork_from="base-oracle", working_directory="/repo-b/src"
        )
        assert result["success"] is True
        assert result["forked_from"] is None
        # The CROSS-REPO preamble is injected into the worker_message (not the
        # result message to the caller), verified via the _create_terminal call:
        call_kwargs = _create.call_args
        worker_msg = call_kwargs.kwargs.get("initial_message") or call_kwargs[1].get(
            "initial_message", ""
        )
        if not worker_msg and len(call_kwargs.args) > 1:
            # positional
            pass
        assert "CROSS-REPO" in str(call_kwargs)


# ---------------------------------------------------------------------------
# A3: fork_from="none" escape hatch
# ---------------------------------------------------------------------------


class TestForkFromNone:
    """A3: fork_from='none' is treated as cold spawn."""

    @patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", return_value=("w4", "codex")
    )
    @patch("cli_agent_orchestrator.mcp_server.server.strict_supervisor_cwd", return_value="/work")
    def test_fork_from_none_spawns_cold(self, _cwd, _create, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
        with patch("cli_agent_orchestrator.services.fork_context_service.resolve_base") as resolve:
            result = _assign_impl("developer", "task", fork_from="none")
        resolve.assert_not_called()
        assert result["success"] is True
        assert result["forked_from"] is None

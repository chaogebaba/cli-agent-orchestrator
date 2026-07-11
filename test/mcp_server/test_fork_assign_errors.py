from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.services.fork_context_service import ForkContextError


ROW = {"name": "base", "provider": "codex", "session_uuid": "11111111-1111-4111-8111-111111111111",
       "cwd": "/repo", "agent_profile": "developer", "git_sha": "a" * 40,
       "dirty_hashes": "{}"}


@pytest.mark.parametrize("code", ["base_name_unknown", "base_not_registered", "base_session_unset"])
def test_resolution_errors_do_not_spawn(monkeypatch, code):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("cli_agent_orchestrator.services.fork_context_service.resolve_base",
               side_effect=ForkContextError(code)), patch(
               "cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl("developer", "task", fork_from="base")
    assert code in result["message"]
    create.assert_not_called()


def test_resume_requires_base_does_not_spawn(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl("developer", "task", resume=True)
    assert "resume_requires_fork_from" in result["message"]
    create.assert_not_called()


@pytest.mark.parametrize("code", ["provider_mismatch", "provider_lacks_fork_capability",
                                  "resume_profile_mismatch", "session_file_missing",
                                  "session_live_owned", "owner_probe_failed"])
def test_validation_errors_do_not_spawn(monkeypatch, code):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    row = dict(ROW)
    profile = "developer"
    resume = code in {"resume_profile_mismatch", "session_live_owned", "owner_probe_failed"}
    resolved = "codex"
    if code == "provider_mismatch":
        resolved = "grok_cli"
    if code == "provider_lacks_fork_capability":
        row["provider"] = resolved = "kiro_cli"
    if code == "resume_profile_mismatch":
        profile = "reviewer"
    owner_state = "live" if code == "session_live_owned" else "error"
    response = MagicMock()
    response.json.return_value = {"state": owner_state}
    with patch("cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=row), \
         patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value=resolved), \
         patch("pathlib.Path.glob", return_value=[] if code == "session_file_missing" else
               [SimpleNamespace(name=f"rollout-{row['session_uuid']}.jsonl")]), \
         patch("cli_agent_orchestrator.mcp_server.server.requests.get", return_value=response), \
         patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl(profile, "task", fork_from="base", resume=resume)
    assert code in result["message"]
    create.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "http", "malformed", "missing", "unknown"])
def test_owner_probe_protocol_failures_map_to_distinct_error(monkeypatch, failure):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    response = MagicMock()
    if failure == "timeout":
        get_effect = requests.Timeout("backend unreachable")
    else:
        get_effect = None
        if failure == "http":
            response.raise_for_status.side_effect = requests.HTTPError("503")
        elif failure == "malformed":
            response.json.side_effect = ValueError("bad json")
        elif failure == "missing":
            response.json.return_value = {}
        else:
            response.json.return_value = {"state": "maybe"}
    with patch("cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=ROW), \
         patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex"), \
         patch("pathlib.Path.glob", return_value=[SimpleNamespace(name=f"rollout-{ROW['session_uuid']}.jsonl")]), \
         patch("cli_agent_orchestrator.mcp_server.server.requests.get",
               side_effect=get_effect, return_value=response), \
         patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl("developer", "task", fork_from="base", resume=True)
    assert result["message"] == "Assignment failed: owner_probe_failed"
    create.assert_not_called()


def test_capability_attribute_owns_pre_spawn_check(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=ROW), \
         patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex"), \
         patch("cli_agent_orchestrator.providers.codex.CodexProvider.supports_fork_context", False), \
         patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl("developer", "task", fork_from="base")
    assert "provider_lacks_fork_capability" in result["message"]
    create.assert_not_called()

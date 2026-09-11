"""F970 (#819) — the workspace-read tool GROUP: gated off, and typed when on.

Two claims are tested here, both about the tool surface rather than the reads
(those live in test/services/test_f970_workspace_read.py):

1. **Off by default, and off means ABSENT.** The fork's rule (the
   ``_learning_tool`` precedent) is that a disabled feature costs no
   tool-surface context on every worker's every turn, so the flag decides
   REGISTRATION, not call-time behaviour.
2. **A refusal is data, never a traceback.** The model must be able to
   distinguish "denied by policy" from "the tool broke", and a denial must not
   hand back an exception string it can treat as a hint to try harder.
"""

from __future__ import annotations

import asyncio

import pytest

from cli_agent_orchestrator.mcp_server.server import (
    _workspace_read_enabled,
    _workspace_result,
    mcp,
)

pytestmark = pytest.mark.unit

_GROUP = {
    "workspace_info",
    "workspace_list_directory",
    "workspace_read_file",
    "workspace_search",
    "workspace_git_status",
    "workspace_git_diff",
}


def _registered_tool_names() -> set[str]:
    tools = asyncio.run(mcp.get_tools()) if hasattr(mcp, "get_tools") else {}
    return set(tools.keys()) if isinstance(tools, dict) else {t.name for t in tools}


def test_the_group_is_absent_unless_the_flag_is_set():
    """Registration-time gating: with CAO_WORKSPACE_READ_TOOLS unset (the test
    environment), none of these six names is on the surface at all."""
    assert _workspace_read_enabled() is False
    assert _GROUP.isdisjoint(_registered_tool_names())


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False)],
)
def test_the_flag_is_read_fail_closed(monkeypatch, value, expected):
    monkeypatch.setenv("CAO_WORKSPACE_READ_TOOLS", value)
    assert _workspace_read_enabled() is expected
    monkeypatch.delenv("CAO_WORKSPACE_READ_TOOLS")
    assert _workspace_read_enabled() is False


def test_a_policy_refusal_comes_back_as_typed_data():
    from cli_agent_orchestrator.services.workspace_read import CODE_SENSITIVE, WorkspaceError

    def _denied():
        raise WorkspaceError(CODE_SENSITIVE, "'.env' matches the sensitive-file policy")

    result = _workspace_result(_denied)
    assert result["ok"] is False
    assert result["error_code"] == CODE_SENSITIVE
    assert "Traceback" not in result["error"]


def test_a_successful_read_is_merged_into_an_ok_envelope():
    result = _workspace_result(lambda: {"path": "src/app.py", "content": "x"})
    assert result == {"ok": True, "path": "src/app.py", "content": "x"}


def test_the_group_never_contains_a_mutating_verb():
    """Read-only by construction is the security claim the connector story
    rests on: there must be no write/shell/commit tool to reach, ever."""
    for name in _GROUP:
        assert not any(verb in name for verb in ("write", "run", "exec", "commit", "delete"))

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.services.fork_context_service import ForkContextError

ROW = {
    "name": "base",
    "provider": "codex",
    "session_uuid": "11111111-1111-4111-8111-111111111111",
    "cwd": "/repo",
    "agent_profile": "developer",
    "git_sha": "a" * 40,
    "dirty_hashes": "{}",
}


@pytest.mark.parametrize("code", ["base_name_unknown", "base_not_registered", "base_session_unset"])
def test_resolution_errors_do_not_spawn(monkeypatch, code):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.resolve_base",
            side_effect=ForkContextError(code),
        ),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl("developer", "task", fork_from="base")
    assert code in result["message"]
    create.assert_not_called()


def test_resume_requires_base_does_not_spawn(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create:
        result = _assign_impl("developer", "task", resume=True)
    # RESUME HOT-FIX r1 #1: resume=True with no handle is now the ONE typed
    # refusal (was the fork-path string resume_requires_fork_from).
    assert result["error"] == "resume_refused"
    assert result["reason"] == "resume_true_without_handle"
    create.assert_not_called()


@pytest.mark.parametrize(
    "code", ["provider_mismatch", "provider_lacks_fork_capability", "session_file_missing"]
)
def test_fork_path_validation_errors_do_not_spawn(monkeypatch, code):
    """The FORK path (resume=False) keeps its original error strings (r1 #4
    scopes the typed refusal to the RESUME path only)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    row = dict(ROW)
    resolved = "codex"
    if code == "provider_mismatch":
        resolved = "grok_cli"
    if code == "provider_lacks_fork_capability":
        row["provider"] = resolved = "kiro_cli"
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=row
        ),
        patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value=resolved),
        patch(
            "pathlib.Path.glob",
            return_value=(
                []
                if code == "session_file_missing"
                else [SimpleNamespace(name=f"rollout-{row['session_uuid']}.jsonl")]
            ),
        ),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl("developer", "task", fork_from="base", resume=False)
    assert code in result["message"]
    create.assert_not_called()


@pytest.mark.parametrize("code", ["identity", "session_id"])
def test_legacy_fork_resume_delegates_to_resume_refusal(monkeypatch, code):
    """r1 #1: legacy fork_from+resume=True delegates into the resume service,
    so a base name that is not a resolvable identity yields resume_refused —
    NOT the old owner-probe/resume_profile_mismatch fork-path strings."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    from cli_agent_orchestrator.clients import database as _db

    if code == "identity":
        identity = None  # "base" resolves to nothing
    else:  # session_id: an identity exists but has no captured provider session
        identity = {
            "terminal_id": "base",
            "provider": "codex",
            "agent_profile": "developer",
            "cwd": "/repo",
            "provider_session_id": None,
            "worktree_path": None,
            "worktree_branch": None,
            "worktree_repo_root": None,
            "git_sha": None,
        }
    with (
        patch.object(_db, "get_terminal_identity", return_value=identity),
        patch.object(_db, "get_terminal_identity_by_provider_session_id", return_value=None),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl("developer", "task", fork_from="base", resume=True)
    assert result["error"] == "resume_refused"
    assert result["missing"] == code
    create.assert_not_called()


def test_capability_attribute_owns_pre_spawn_check(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.resolve_base", return_value=ROW
        ),
        patch("cli_agent_orchestrator.mcp_server.server.resolve_provider", return_value="codex"),
        patch("cli_agent_orchestrator.providers.codex.CodexProvider.supports_fork_context", False),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl("developer", "task", fork_from="base")
    assert "provider_lacks_fork_capability" in result["message"]
    create.assert_not_called()


# --- F497 D10 / AC11 — routing-flip fork-base tolerance ---------------------


def _cold_fallback_lines(message: str) -> list[str]:
    """Every ``[COLD-FALLBACK …]`` line in a worker message preamble."""
    return [ln for ln in message.splitlines() if ln.startswith("[COLD-FALLBACK")]


def test_ac11_defaulted_fork_provider_mismatch_degrades_and_spawns(monkeypatch):
    """AC11 (D10): a DEFAULTED fork base still registered under the OLD provider
    after a binding flip degrades to a cold spawn with a single ``[COLD-FALLBACK``
    preamble line (D10-alone form ``base=stale``), rather than the pre-D10 hard
    ``provider_mismatch`` no-spawn.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    row = dict(ROW)  # provider == "codex"
    captured = {}

    def _fake_create(*args, **kwargs):
        captured["initial_message"] = kwargs.get("initial_message")
        return ("worker99", "kiro_cli")

    with (
        patch(
            "cli_agent_orchestrator.mcp_server.server._configured_default_fork_base",
            return_value="base",
        ),
        patch(
            "cli_agent_orchestrator.services.fork_context_service.resolve_base",
            return_value=row,
        ),
        patch(
            # binding flipped: the profile now resolves to a DIFFERENT provider than
            # the base was registered under.
            "cli_agent_orchestrator.mcp_server.server.resolve_provider",
            return_value="kiro_cli",
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal",
            side_effect=_fake_create,
        ) as create,
    ):
        result = _assign_impl("developer", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    msg = captured["initial_message"]
    lines = _cold_fallback_lines(msg)
    # r11 S1: exactly ONE [COLD-FALLBACK line per spawn.
    assert len(lines) == 1, f"expected exactly one COLD-FALLBACK line, got {lines}"
    assert lines[0].startswith("[COLD-FALLBACK")
    # D10-alone grammar (D12 field order): base=stale present.
    assert "base=stale" in lines[0]


def test_ac11_explicit_fork_from_provider_mismatch_still_raises(monkeypatch):
    """AC11: an EXPLICIT ``fork_from=`` provider mismatch is NOT tolerated — it
    still raises ``provider_mismatch`` with no terminal created (the D10
    degradation is gated on ``defaulted_fork``). Mirrors the existing
    test_validation_errors_do_not_spawn provider_mismatch case at line 59.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    row = dict(ROW)
    with (
        patch(
            "cli_agent_orchestrator.services.fork_context_service.resolve_base",
            return_value=row,
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server.resolve_provider",
            return_value="grok_cli",
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal",
        ) as create,
    ):
        result = _assign_impl("developer", "task", fork_from="base")
    assert "provider_mismatch" in result["message"]
    create.assert_not_called()

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
    """F829 A2.1 (Option A): legacy fork_from+resume=True is pure syntax that
    FORWARDS the handle to the SERVER-SIDE resume admission (no client-side
    resolution anymore). The shim marks it a resume and passes resume_from to
    the create endpoint; the refusal (unknown handle / no captured session id)
    is a SERVER decision, covered by test_f829_a2 / test_f829_ac_matrix. Here we
    assert the shim FORWARDS rather than resolving the identity itself."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal",
            return_value=("new00001", "codex"),
        ) as create,
        patch(
            "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
            return_value={"resolved_model": None},
        ),
        patch("cli_agent_orchestrator.mcp_server.server.generate_window_name", return_value="w"),
        patch(
            "cli_agent_orchestrator.mcp_server.server.display_name",
            return_value="developer(new00001)",
        ),
    ):
        result = _assign_impl(
            "developer", "task", fork_from="base", resume=True, working_directory="/repo"
        )
    # The legacy form translated into a resume forward, not a client-side refusal.
    assert result.get("success") is True
    assert create.call_args.kwargs["resume_from"] == "base"
    assert create.call_args.kwargs["fork_context"] is None


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

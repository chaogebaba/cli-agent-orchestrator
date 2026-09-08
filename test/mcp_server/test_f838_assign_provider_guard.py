"""F838 (#695) — assign seam refuses (no spawn) on provider substitution.

A legacy alias stub (``pi_cli_empirical_reviewer_lite``) that declares a provider
but resolves to no valid provider must NOT spawn as the caller's provider. The
assign seam pre-resolves the provider strictly from the stub and, on failure or
mismatch, returns a typed refusal WITHOUT calling ``_create_terminal``.
"""

from __future__ import annotations

from unittest.mock import patch

from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.utils import agent_profiles
from cli_agent_orchestrator.utils.agent_profiles import (
    E_PROVIDER_UNRESOLVED,
    ProviderResolutionError,
)

_CREATE = "cli_agent_orchestrator.mcp_server.server._create_terminal"
_STUBSAFE = "cli_agent_orchestrator.utils.agent_profiles._stub_declared_provider_safe"
# The assign seam imports resolve_provider LOCALLY from agent_profiles, so patch
# it there (patching server.resolve_provider would not bind the local import).
_RESOLVE = "cli_agent_orchestrator.utils.agent_profiles.resolve_provider"


def _no_positions():
    return patch.object(agent_profiles, "_read_composition_store", side_effect=lambda *a, **k: None)


def test_assign_refuses_when_declared_stub_resolves_unresolved(monkeypatch):
    """Declaring stub + ProviderResolutionError from resolve_provider → the
    assign result is a typed refusal and NO terminal is created."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        _no_positions(),
        patch(_STUBSAFE, return_value=(True, "pi_cli")),
        patch(
            _RESOLVE,
            side_effect=ProviderResolutionError(
                E_PROVIDER_UNRESOLVED, "E-PROVIDER-UNRESOLVED: boom"
            ),
        ),
        patch(_CREATE) as create,
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "no spawn" in result["message"].lower()
    create.assert_not_called()


def test_assign_refuses_on_provider_mismatch(monkeypatch):
    """Declaring stub says pi_cli but resolution produced a DIFFERENT provider
    (the silent-substitution shape) → typed refusal, no spawn — even though the
    produced provider is itself valid."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        _no_positions(),
        patch(_STUBSAFE, return_value=(True, "pi_cli")),
        patch(_RESOLVE, return_value="claude_code"),
        patch(_CREATE) as create,
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "claude_code" in result["message"] and "pi_cli" in result["message"]
    create.assert_not_called()


def test_assign_validates_declared_provider_on_success(monkeypatch):
    """Healthy declaring stub → seam VALIDATES via resolve_provider (matches the
    declared provider) and lets the spawn proceed. _resolved_provider is NOT
    pinned (legacy passthrough), so _create_terminal is called and its own
    fail-closed resolve_provider derives the provider."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        captured["agent_profile"] = agent_profile
        return ("worker1", "pi_cli")

    with (
        _no_positions(),
        patch(_STUBSAFE, return_value=(True, "pi_cli")),
        patch(_RESOLVE, return_value="pi_cli"),
        patch(_CREATE, side_effect=fake_create) as create,
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "pi_cli_empirical_reviewer_lite"
    # Seam validates only; provider derivation stays with _create_terminal.
    assert captured["provider"] is None


def test_assign_legacy_no_intent_unchanged(monkeypatch):
    """A genuine legacy name that declares no provider/composition intent is not
    pre-resolved by the F838 seam — passthrough to _create_terminal unchanged
    (provider stays None; resolve_provider inside _create_terminal handles it)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        captured["agent_profile"] = agent_profile
        return ("worker1", "kiro_cli")

    with (
        _no_positions(),
        patch(_STUBSAFE, return_value=(False, None)),
        patch(_CREATE, side_effect=fake_create) as create,
    ):
        result = _assign_impl("developer", "task", working_directory="/repo")

    assert result["success"] is True
    assert captured["agent_profile"] == "developer"
    assert captured["provider"] is None  # not pre-resolved; legacy passthrough

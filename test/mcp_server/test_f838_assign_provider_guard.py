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
    declared provider) and lets the spawn proceed. r2 (codex Blocker 2): the
    guard-checked provider is CARRIED into _create_terminal (via the F613
    ``provider=`` seam) so creation uses the value validated here — not a second
    re-resolution from the mutable store. _resolved_provider is still NOT pinned
    (legacy passthrough), so no position D8/D9 machinery fires."""
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
    # r2: the guard-checked provider is carried into _create_terminal.
    assert captured["provider"] == "pi_cli"


def test_assign_carries_guard_checked_provider_across_store_replacement(monkeypatch):
    """codex Blocker 2 probe VERBATIM: a healthy ``pi_cli`` validation followed
    by store replacement between guard and create. The guard resolves pi_cli;
    then the store is swapped so a SECOND resolution would yield claude_code. The
    provider PASSED TO _create_terminal must equal the guard-checked value
    (pi_cli) and MUST NEVER become claude_code — proving creation does not
    re-resolve from the mutated store."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}
    calls = {"n": 0}

    def resolve_then_mutate(name, fallback_provider):
        # First call is the guard's validation → the healthy declared provider.
        # Any LATER call (i.e. a re-resolution at create time) would see the
        # mutated store and return the caller's provider. If the fix carries the
        # guard value forward, this second call never happens.
        calls["n"] += 1
        if calls["n"] == 1:
            return "pi_cli"
        return "claude_code"  # store replaced — the substitution shape

    def fake_create(agent_profile, *a, **k):
        captured["provider"] = k.get("provider")
        return ("worker1", "pi_cli")

    with (
        _no_positions(),
        patch(_STUBSAFE, return_value=(True, "pi_cli")),
        patch(_RESOLVE, side_effect=resolve_then_mutate),
        patch(_CREATE, side_effect=fake_create),
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is True
    # The decisive assertion: guard_checked pi_cli reaches create as pi_cli.
    assert captured["provider"] == "pi_cli"
    assert captured["provider"] != "claude_code"
    # The guard resolved exactly once; create did NOT re-resolve from disk.
    assert calls["n"] == 1


def test_assign_refuses_when_stub_disappears_between_guard_and_create(monkeypatch):
    """codex Blocker 2, disappearance case: the stub declared intent, but by the
    time the guard resolves it the store entry is gone/unreadable, so
    resolve_provider fails closed (E-PROVIDER-UNRESOLVED). The assign returns a
    typed refusal and _create_terminal is NEVER called — no fallback spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    with (
        _no_positions(),
        # Stub read at guard-classification time still shows declared intent...
        patch(_STUBSAFE, return_value=(True, "pi_cli")),
        # ...but resolution (single-read) now fails closed: entry vanished.
        patch(
            _RESOLVE,
            side_effect=ProviderResolutionError(
                E_PROVIDER_UNRESOLVED,
                "E-PROVIDER-UNRESOLVED: agent profile 'pi_cli_empirical_reviewer_lite' "
                "could not be read/parsed from the store (F838 #695)",
            ),
        ),
        patch(_CREATE) as create,
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    assert "no spawn" in result["message"].lower()
    create.assert_not_called()


def test_assign_refuses_on_unreadable_stub_after_parsed_none(monkeypatch):
    """codex Blocker 1 at the public seam: an UNKNOWN/unreadable stub is routed
    through resolve_provider by _stub_declared_provider_safe returning
    (True, None) — NOT treated as "no intent → passthrough". resolve_provider
    then fails closed, so the assign refuses with no spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    with (
        _no_positions(),
        # UNKNOWN read: the r2 _stub_declared_provider_safe reports (True, None)
        # so the guard does NOT fall through to the legacy passthrough.
        patch(_STUBSAFE, return_value=(True, None)),
        patch(
            _RESOLVE,
            side_effect=ProviderResolutionError(
                E_PROVIDER_UNRESOLVED,
                "E-PROVIDER-UNRESOLVED: provider undeterminable (F838 #695)",
            ),
        ),
        patch(_CREATE) as create,
    ):
        result = _assign_impl("pi_cli_empirical_reviewer_lite", "task", working_directory="/repo")

    assert result["success"] is False
    assert E_PROVIDER_UNRESOLVED in result["message"]
    create.assert_not_called()


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

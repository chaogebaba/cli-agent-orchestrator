"""F838 (#695) — fail-closed provider resolution for composition/alias stubs.

A ``pi_cli`` alias stub silently spawned as the supervisor's ``claude_code``/Opus
because a profile that DECLARED a provider/composition but resolved to no valid
provider fell back to the caller's provider. These arms pin the fail-closed
replacement: such a case raises ``ProviderResolutionError`` (E-PROVIDER-UNRESOLVED),
never a silent substitution — while a GENUINE legacy plain profile (declares no
provider/composition intent) keeps the caller-provider fallback.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import (
    E_PROVIDER_UNRESOLVED,
    ProviderResolutionError,
    resolve_provider,
)

_STUB = "cli_agent_orchestrator.utils.agent_profiles._stub_declared_provider"
_LOAD = "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile"


def test_healthy_stub_resolves_declared_provider():
    """A healthy alias stub (declares extends+provider, composes cleanly to
    pi_cli) resolves to pi_cli — never the caller's provider."""
    with (
        patch(
            _LOAD,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="pi_cli"
            ),
        ),
        patch(_STUB, return_value=(True, "pi_cli")),
    ):
        assert resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code") == "pi_cli"


def test_truncated_stub_declares_but_resolves_none_refuses():
    """The #695 bite: the stub declared a provider/composition but the loaded
    profile carries no provider (a truncated/mid-rewrite read composed to
    provider=None). MUST raise E-PROVIDER-UNRESOLVED, never fall back to
    claude_code."""
    # Loaded profile has no provider; the RAW stub declared one (pi_cli).
    with (
        patch(
            _LOAD, return_value=AgentProfile(name="pi_cli_empirical_reviewer_lite", description="d")
        ),
        patch(_STUB, return_value=(True, "pi_cli")),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)  # names the refused fallback


def test_declared_composition_fails_to_load_refuses():
    """A stub that declares intent but whose composition RAISES (RuntimeError
    from load_agent_profile) also fails closed rather than falling back."""
    with (
        patch(_LOAD, side_effect=RuntimeError("compose boom")),
        patch(_STUB, return_value=(True, None)),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_invalid_provider_on_declaring_stub_refuses():
    """A declaring stub whose provider is present but invalid must refuse, not
    silently fall back (the old path warned then returned the caller's provider)."""
    with (
        patch(
            _LOAD,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="claud_code"
            ),
        ),
        patch(_STUB, return_value=(True, "claud_code")),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_legacy_plain_profile_no_intent_keeps_fallback():
    """A GENUINE legacy plain profile (declares NO provider/composition intent)
    keeps today's caller-provider fallback — the behaviour pinned by
    test_returns_fallback_when_no_provider_key. Not every provider-less profile
    is a defect; only a DECLARING one is."""
    with (
        patch(_LOAD, return_value=AgentProfile(name="reviewer", description="d")),
        patch(_STUB, return_value=(False, None)),
    ):
        assert resolve_provider("reviewer", "kiro_cli") == "kiro_cli"


def test_absent_profile_keeps_fallback():
    """A truly absent name (FileNotFoundError) still falls back without raising —
    provider.initialize surfaces the real error later; nothing to contradict."""
    with patch(_LOAD, side_effect=FileNotFoundError("Agent profile not found: ghost")):
        assert resolve_provider("ghost", "kiro_cli") == "kiro_cli"


def test_stub_read_error_does_not_manufacture_refusal():
    """If the raw stub re-read itself errors while the loaded profile has no
    provider, we lack evidence of declared intent and must not manufacture a
    refusal — fall back (the _safe wrapper returns (False, None))."""
    with (
        patch(_LOAD, return_value=AgentProfile(name="x", description="d")),
        patch(_STUB, side_effect=OSError("read blew up")),
    ):
        # _stub_declared_provider_safe swallows the error -> (False, None) -> fallback
        assert resolve_provider("x", "kiro_cli") == "kiro_cli"

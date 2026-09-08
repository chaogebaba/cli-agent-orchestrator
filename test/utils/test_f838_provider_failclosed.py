"""F838 (#695) — fail-closed provider resolution for composition/alias stubs.

A ``pi_cli`` alias stub silently spawned as the supervisor's ``claude_code``/Opus
because a profile that DECLARED a provider/composition but resolved to no valid
provider fell back to the caller's provider. These arms pin the fail-closed
replacement: such a case raises ``ProviderResolutionError`` (E-PROVIDER-UNRESOLVED),
never a silent substitution — while a GENUINE legacy plain profile (declares no
provider/composition intent) keeps the caller-provider fallback.

r2 (codex EMPIRICAL-GATE-NO Blocker 1): the resolver now derives BOTH the
declared-intent decision AND the provider from a SINGLE immutable raw read of
the stub, and an UNKNOWN read (unreadable/unparseable/vanished-mid-flight) FAILS
CLOSED rather than being treated as "no declared intent → fall back". These arms
patch the single-read seams (``read_agent_profile_source`` and
``resolve_agent_profile``) so a raw-read error is exercised through the real
classification path, not a swallow-everything wrapper.
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

_MOD = "cli_agent_orchestrator.utils.agent_profiles"
_READ = f"{_MOD}.read_agent_profile_source"
_COMPOSE = f"{_MOD}.resolve_agent_profile"

# A raw stub that DECLARES an alias composition (extends + provider). The exact
# body is irrelevant — the resolver classifies intent from these bytes, then the
# patched ``resolve_agent_profile`` supplies the composed AgentProfile.
_DECLARING_STUB = "---\nextends: empirical_reviewer_lite\nprovider: pi_cli\n---\nbody\n"
# A raw stub that declares NO provider/composition intent (genuine legacy plain).
_PLAIN_STUB = "---\ndescription: a plain legacy profile\n---\nbody\n"


def test_healthy_stub_resolves_declared_provider():
    """A healthy alias stub (declares extends+provider, composes cleanly to
    pi_cli) resolves to pi_cli — never the caller's provider."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="pi_cli"
            ),
        ),
    ):
        assert resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code") == "pi_cli"


def test_truncated_stub_declares_but_resolves_none_refuses():
    """The #695 bite: the stub declared a provider/composition but the composed
    profile carries no provider. MUST raise E-PROVIDER-UNRESOLVED, never fall
    back to claude_code."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(name="pi_cli_empirical_reviewer_lite", description="d"),
        ),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)  # names the refused fallback


def test_declared_composition_fails_to_load_refuses():
    """A stub that declares intent but whose composition RAISES also fails
    closed rather than falling back."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(_COMPOSE, side_effect=RuntimeError("compose boom")),
    ):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED


def test_invalid_provider_on_declaring_stub_refuses():
    """A declaring stub whose provider is present but invalid must refuse, not
    silently fall back (the old path warned then returned the caller's provider)."""
    with (
        patch(_READ, return_value=_DECLARING_STUB),
        patch(
            _COMPOSE,
            return_value=AgentProfile(
                name="pi_cli_empirical_reviewer_lite", description="d", provider="claud_code"
            ),
        ),
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
        patch(_READ, return_value=_PLAIN_STUB),
        patch(_COMPOSE, return_value=AgentProfile(name="reviewer", description="d")),
    ):
        assert resolve_provider("reviewer", "kiro_cli") == "kiro_cli"


def test_absent_profile_keeps_fallback():
    """A truly absent name (FileNotFoundError from the raw read) still falls back
    without raising — provider.initialize surfaces the real error later; nothing
    to contradict."""
    with patch(_READ, side_effect=FileNotFoundError("Agent profile not found: ghost")):
        assert resolve_provider("ghost", "kiro_cli") == "kiro_cli"


def test_stub_read_error_refuses():
    """r2 INVERTED (was test_stub_read_error_does_not_manufacture_refusal):
    a raw-stub read error is an UNKNOWN store entry, not "no declared intent".
    A legacy alias whose stub cannot be read MUST be refused (fail closed), never
    inherit the caller's provider. This is codex Blocker 1's decisive assertion:
    uncertainty is a refusal, not a fallback."""
    with patch(_READ, side_effect=OSError("read blew up")):
        with pytest.raises(ProviderResolutionError) as ei:
            resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED
    assert "claude_code" in str(ei.value)  # names the refused fallback


def test_unparseable_stub_refuses():
    """A stub whose bytes are present but are not parseable frontmatter is also
    UNKNOWN — indeterminate intent — and MUST refuse rather than fall back."""
    # A bytes payload that frontmatter cannot parse into metadata cleanly.
    with patch(_COMPOSE, side_effect=AssertionError("should not compose on UNKNOWN")):
        with patch(_READ, side_effect=ValueError("not valid frontmatter")):
            # A raw-read that raises a non-FileNotFound error is UNKNOWN -> refuse
            # BEFORE compose is ever attempted.
            with pytest.raises(ProviderResolutionError) as ei:
                resolve_provider("pi_cli_empirical_reviewer_lite", "claude_code")
    assert ei.value.code == E_PROVIDER_UNRESOLVED

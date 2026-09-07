"""F786 (#643) stage 1 — fork side of position-only role names.

Unit + seam coverage for the FORK lane's D-rows. Each test names the AC it
covers (blueprint §4) and, where relevant, the mutant it kills (blueprint §4
mutant list). Live-fleet arms (AC1b) and the box-smoke cert arms (AC10 c) run
after the second redeploy and are not in this file.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils import routing_guard as rg
from cli_agent_orchestrator.utils.agent_profiles import (
    AssignmentResolutionError,
    _synthesise_position_profile_name,
    resolve_resume_effective_name,
    split_effective_name,
)

# The nine bound cells (blueprint D10), as (position, provider) pairs.
BOUND_CELLS = [
    ("general", "kiro_cli"),
    ("general", "codex"),
    ("secretary", "cline_cli"),
    ("empirical_reviewer", "codex"),
    ("dev", "kiro_cli"),
    ("grunt", "cline_cli"),
    ("oracle", "cline_cli"),
    ("doc_keeper", "cline_cli"),
    ("tester", "grok_cli"),
]


# ---------------------------------------------------------------------------
# AC1a — effective name <position>-<provider>, splitter round-trips every cell
# ---------------------------------------------------------------------------
class TestAC1aEffectiveName:
    def test_synthesis_is_position_hyphen_provider(self):
        assert _synthesise_position_profile_name("dev", "kiro_cli") == "dev-kiro_cli"
        assert (
            _synthesise_position_profile_name("empirical_reviewer", "codex")
            == "empirical_reviewer-codex"
        )

    def test_synthesis_never_reverts_to_provider_underscore_position(self):
        """MUTANT: synthesis reverts to <provider>_<position>. That name collides
        with a RETIRED_PROFILES entry, so AC2 fires — assert the new shape."""
        name = _synthesise_position_profile_name("empirical_reviewer", "codex")
        assert name == "empirical_reviewer-codex"
        assert name != "codex_empirical_reviewer"

    @pytest.mark.parametrize("position,provider", BOUND_CELLS)
    def test_split_round_trips_every_bound_cell(self, position, provider):
        name = _synthesise_position_profile_name(position, provider)
        assert split_effective_name(name) == (position, provider)

    def test_split_returns_none_for_legacy_flat_names(self):
        for legacy in ("kiro_dev", "codex_dev", "grok_oracle", "chao_supervisor"):
            assert split_effective_name(legacy) is None

    def test_split_only_accepts_real_provider_tokens(self):
        # a hyphen suffix that is not a provider value is not a split
        assert split_effective_name("dev-notaprovider") is None
        # every ProviderType value is a valid suffix
        for p in ProviderType:
            assert split_effective_name(f"dev-{p.value}") == ("dev", p.value)

    def test_window_name_validates(self):
        from cli_agent_orchestrator.utils.terminal import generate_window_name, validate_tmux_name

        validate_tmux_name("dev-kiro_cli", "window_name")
        assert generate_window_name("dev-kiro_cli", "1a2b3c4d") == "dev-kiro_cli-1a2b3c4d"

    def test_resolve_terminal_id_still_splits_trailing_id(self):
        """The trailing -<8hex> is extracted from a hyphenated composed name
        (it then validates existence; here we assert the extracted id, proving
        the hyphenated <position>-<provider> prefix did not confuse the split)."""
        from cli_agent_orchestrator.utils.terminal import resolve_terminal_id

        with pytest.raises(ValueError, match="1a2b3c4d"):
            resolve_terminal_id("dev-kiro_cli-1a2b3c4d")


# ---------------------------------------------------------------------------
# AC2 — RETIRED_PROFILES refusal (22 names) + negative arm
# ---------------------------------------------------------------------------
class TestAC2RetiredProfiles:
    def test_membership_is_exactly_22(self):
        assert len(rg.RETIRED_PROFILES) == 22

    def test_developer_opus_sonnet_are_retired_not_unmapped(self):
        assert rg.RETIRED_PROFILES["developer-opus"] == "dev"
        assert rg.RETIRED_PROFILES["developer-sonnet"] == "dev"
        assert "developer-opus" not in rg.UNMAPPED_BY_DESIGN
        assert "developer-sonnet" not in rg.UNMAPPED_BY_DESIGN

    @pytest.mark.parametrize("name", sorted(rg.RETIRED_PROFILES))
    def test_every_retired_name_is_refused_naming_position(self, name):
        """MUTANT: refusal disabled → returns None here."""
        refusal = rg.retired_profile_refusal(name)
        assert refusal is not None
        assert refusal.startswith("E-LEGACY-PROFILE-RETIRED")
        position = rg.RETIRED_PROFILES[name]
        if position is not None:
            assert position in refusal

    def test_grok_reviewer_maps_to_no_position_with_advice(self):
        assert rg.RETIRED_PROFILES["grok_reviewer"] is None
        refusal = rg.retired_profile_refusal("grok_reviewer")
        assert "tester" in refusal and "grunt" in refusal

    def test_negative_arm_non_retired_names_pass_through(self):
        """MUTANT: refusal fires for a flat non-table name. AC2 negative arm."""
        for keep in ("chao_supervisor", "codex_base", "grok_base", "claude_blueprint_maker"):
            assert rg.retired_profile_refusal(keep) is None

    def test_resolver_raises_typed_error_before_passthrough(self):
        from cli_agent_orchestrator.utils import agent_profiles

        with pytest.raises(AssignmentResolutionError) as exc:
            agent_profiles.resolve_assignment_target("kiro_dev", None)
        assert exc.value.code == agent_profiles.E_LEGACY_PROFILE_RETIRED

    def test_resolver_refuses_even_with_explicit_provider(self):
        from cli_agent_orchestrator.utils import agent_profiles

        with pytest.raises(AssignmentResolutionError) as exc:
            agent_profiles.resolve_assignment_target("codex_empirical_reviewer", "codex")
        assert exc.value.code == agent_profiles.E_LEGACY_PROFILE_RETIRED


# ---------------------------------------------------------------------------
# AC5 — providers.toml is position-keyed; legacy key is E-LEGACY-PROFILE-KEY
# ---------------------------------------------------------------------------
class TestAC5ProvidersTomlPositionKeyed:
    def test_legacy_key_raises_at_load(self):
        """MUTANT: legacy providers.toml key silently ignored."""
        from cli_agent_orchestrator.services.settings_service import (
            LegacyProfileKeyError,
            _reject_legacy_profile_keys,
        )

        with pytest.raises(LegacyProfileKeyError) as exc:
            _reject_legacy_profile_keys(
                {"codex": {"profiles": {"codex_empirical_reviewer": {"reasoning_effort": "high"}}}}
            )
        msg = str(exc.value)
        assert "E-LEGACY-PROFILE-KEY" in msg
        assert "empirical_reviewer" in msg  # names the position key to use

    def test_position_key_accepted(self):
        from cli_agent_orchestrator.services.settings_service import _reject_legacy_profile_keys

        _reject_legacy_profile_keys(
            {"codex": {"profiles": {"empirical_reviewer": {"reasoning_effort": "high"}}}}
        )

    def test_kept_profile_key_accepted(self):
        from cli_agent_orchestrator.services.settings_service import _reject_legacy_profile_keys

        _reject_legacy_profile_keys(
            {"claude_code": {"profiles": {"claude_blueprint_maker": {"reasoning_effort": "high"}}}}
        )

    def test_lookup_prefers_position_from_profile(self):
        """The effort lookup keys by AgentProfile.position, not the effective name."""
        from cli_agent_orchestrator.services.settings_service import get_provider_profile_defaults

        provider_defaults = {"profiles": {"empirical_reviewer": {"reasoning_effort": "high"}}}
        assert get_provider_profile_defaults(provider_defaults, "empirical_reviewer") == {
            "reasoning_effort": "high"
        }
        # the composed effective name is NOT the key
        assert get_provider_profile_defaults(provider_defaults, "empirical_reviewer-codex") == {}


# ---------------------------------------------------------------------------
# AC6 — fleet payload position field: column > split > raw
# ---------------------------------------------------------------------------
class TestAC6FleetPosition:
    def test_column_wins_even_when_name_tampered(self):
        """MUTANT: position field carries the whole effective name.
        A row whose column says dev but agent_profile is grunt-kiro_cli → dev."""
        from cli_agent_orchestrator.services.fleet_service import _fleet_position

        assert _fleet_position("dev", "grunt-kiro_cli") == "dev"

    def test_null_column_splits_composed_name(self):
        from cli_agent_orchestrator.services.fleet_service import _fleet_position

        assert _fleet_position(None, "empirical_reviewer-codex") == "empirical_reviewer"

    def test_null_column_legacy_renders_raw(self):
        from cli_agent_orchestrator.services.fleet_service import _fleet_position

        assert _fleet_position(None, "kiro_dev") == "kiro_dev"


# ---------------------------------------------------------------------------
# AC9 — grandfathering resume resolution (pure D9 resolver)
# ---------------------------------------------------------------------------
class TestAC9ResumeResolution:
    def test_retired_map_source(self):
        assert resolve_resume_effective_name("kiro_dev", "kiro_cli") == (
            "dev-kiro_cli",
            "dev",
            "retired_map",
        )

    def test_column_source_wins_over_unmapped_name(self):
        assert resolve_resume_effective_name("weird_name", "kiro_cli", "dev") == (
            "dev-kiro_cli",
            "dev",
            "column",
        )

    def test_unmapped_retired_name_refuses(self):
        """MUTANT: resume uses routing instead of the row's provider / guesses a lane."""
        with pytest.raises(AssignmentResolutionError) as exc:
            resolve_resume_effective_name("grok_reviewer", "grok_cli")
        assert exc.value.code == "E-LEGACY-PROFILE-RETIRED"

    def test_already_composed_name_resumes_as_itself(self):
        assert resolve_resume_effective_name("dev-kiro_cli", "kiro_cli") == (
            "dev-kiro_cli",
            "dev",
            "effective",
        )

    def test_missing_provider_is_hard_fail(self):
        with pytest.raises(AssignmentResolutionError):
            resolve_resume_effective_name("kiro_dev", None)

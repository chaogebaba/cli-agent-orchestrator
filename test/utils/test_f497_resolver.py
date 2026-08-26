"""F497 Phase 1 — resolver seam + AC1 byte-identical diff harness + AC2 guard.

Phase 1 (F497 migration step 1) lands the resolver ABOVE the provider layer,
the resolver-internal ``position`` model field, and the two safety
mechanisms that gate every later extraction:

  * AC1 (the safety story): every legacy profile composed THROUGH the resolver
    is field-for-field byte-identical to today's direct parse. Until Phase 2
    adds real composition, this is guaranteed by construction (the resolver
    delegates to ``parse_agent_profile_text`` for any profile with no
    composition key) — but the harness proves it against the LIVE corpus so it
    keeps running green after each family extraction.

  * AC2: the ``cao install`` fail-closed guard refuses a composition-bearing
    profile while the running server reports no resolver support, and the
    ``CAO_SKIP_RESOLVER_PROBE`` escape allows no-server environments.
"""

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import (
    PROFILE_COMPOSITION_KEYS,
    parse_agent_profile_text,
    profile_declares_composition,
    resolve_agent_profile,
)
from cli_agent_orchestrator.utils.env import resolve_env_vars

# --------------------------------------------------------------------------
# Legacy profile corpus discovery
# --------------------------------------------------------------------------
#
# The 22 legacy orchestrator profiles live in the sibling ``profiles/`` dir of
# the outer workspace (``<workspace>/profiles/`` beside
# ``<workspace>/cli-agent-orchestrator/`` — see test/cli/commands/test_redeploy.py).
# In a worktree the repo root is ``.cao/worktrees/<id>/``, so the corpus is not
# a fixed relative hop. Resolve it via an explicit env override, then a set of
# known-relative candidates; skip (do not fail) when the corpus is absent so
# the harness is portable to a clean checkout that has only the built-in store.

_ENV_OVERRIDE = "CAO_F497_PROFILES_DIR"


def _candidate_profile_dirs() -> list[Path]:
    here = Path(__file__).resolve()
    # repo root = .../cli-agent-orchestrator[/.cao/worktrees/<id>]
    candidates: list[Path] = []
    env = os.environ.get(_ENV_OVERRIDE, "").strip()
    if env:
        candidates.append(Path(env))
    # Walk up looking for a sibling ``profiles/`` dir containing chao_supervisor.md
    # (the canonical orchestrator supervisor profile). Cheap and worktree-safe.
    for ancestor in here.parents:
        sibling = ancestor / "profiles"
        if (sibling / "chao_supervisor.md").is_file():
            candidates.append(sibling)
    return candidates


def _legacy_profiles_dir() -> Path | None:
    for cand in _candidate_profile_dirs():
        if cand.is_dir() and any(cand.glob("*.md")):
            return cand
    return None


def _discover_legacy_profiles() -> list[Path]:
    d = _legacy_profiles_dir()
    if d is None:
        return []
    return sorted(d.glob("*.md"))


_LEGACY_PROFILES = _discover_legacy_profiles()
_PROFILE_IDS = [p.stem for p in _LEGACY_PROFILES]


# --------------------------------------------------------------------------
# AC1 — byte-identical diff harness over the live legacy corpus
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LEGACY_PROFILES,
    reason=(
        "legacy profile corpus not found; set CAO_F497_PROFILES_DIR to the "
        "orchestrator profiles/ dir to run the AC1 diff harness"
    ),
)
@pytest.mark.parametrize("profile_path", _LEGACY_PROFILES, ids=_PROFILE_IDS)
def test_ac1_legacy_profile_resolves_byte_identically(profile_path: Path):
    """Every legacy profile resolves field-for-field identically to today's parse.

    Both the resolver and the direct parse are handed the SAME env-resolved
    text, so any difference is attributable to the resolver alone (not env
    drift between two reads).
    """
    stem = profile_path.stem
    resolved_text = resolve_env_vars(profile_path.read_text(encoding="utf-8"))

    today = parse_agent_profile_text(resolved_text, stem)
    through_resolver = resolve_agent_profile(resolved_text, stem)

    # Model equality is field-for-field; model_dump gives a byte-level diff on
    # failure so a drift names the exact field.
    assert (
        through_resolver.model_dump() == today.model_dump()
    ), f"resolver output diverged from direct parse for '{stem}'"
    assert through_resolver == today


def test_ac1_corpus_is_present_when_expected():
    """Guard against a silently-empty harness in the fork worktree.

    When the corpus IS discoverable it must be non-trivial (the fork ships 20+
    orchestrator profiles). This does not run in a bare checkout (skipped
    corpus), but where profiles/ exists it asserts the harness actually
    exercised them rather than parametrizing over nothing.
    """
    d = _legacy_profiles_dir()
    if d is None:
        pytest.skip("legacy profile corpus not present in this checkout")
    assert len(_LEGACY_PROFILES) >= 20, (
        f"expected the full legacy corpus (>=20 profiles) in {d}, " f"found {len(_LEGACY_PROFILES)}"
    )


# --------------------------------------------------------------------------
# Resolver seam unit behaviour (independent of the live corpus)
# --------------------------------------------------------------------------


def _make_profile_text(frontmatter_lines: str, body: str = "Body") -> str:
    return f"---\n{frontmatter_lines}\n---\n{body}\n"


def test_resolver_passthrough_matches_direct_parse_for_synthetic_legacy():
    text = _make_profile_text("name: synthetic\ndescription: A synthetic legacy profile")
    assert resolve_agent_profile(text, "synthetic") == parse_agent_profile_text(text, "synthetic")


def test_resolver_leaves_position_field_none_for_legacy_profiles():
    text = _make_profile_text("name: legacy\ndescription: no composition keys")
    profile = resolve_agent_profile(text, "legacy")
    assert profile.position is None


def test_position_is_a_declared_model_field():
    # D6: position is a resolver-internal AgentProfile field, defaulting None.
    assert "position" in AgentProfile.model_fields
    assert AgentProfile(name="x", description="y").position is None


def test_extends_is_not_a_model_field():
    # D5: extends is a resolver meta-key, never an AgentProfile field.
    assert "extends" not in AgentProfile.model_fields


@pytest.mark.parametrize("key", list(PROFILE_COMPOSITION_KEYS))
def test_resolver_refuses_composition_bearing_profile_in_phase1(key: str):
    text = _make_profile_text(f"name: composed\ndescription: composed\n{key}: something")
    with pytest.raises(ValueError, match="Phase 2"):
        resolve_agent_profile(text, "composed")


@pytest.mark.parametrize("key", list(PROFILE_COMPOSITION_KEYS))
def test_profile_declares_composition_detects_each_key(key: str):
    assert profile_declares_composition({key: "value"}) is True
    assert profile_declares_composition({key: ""}) is True  # present-but-empty counts


def test_profile_declares_composition_false_for_legacy_metadata():
    assert profile_declares_composition({"name": "x", "description": "y"}) is False
    assert profile_declares_composition({}) is False

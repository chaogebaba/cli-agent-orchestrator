"""F497 D7 — assign(provider=) + position-name resolution + providers allowlist.

``agent_profile`` on an assign resolves as EITHER a legacy concrete name (a real
store profile — unchanged) OR a POSITION name (a positions/ file composed with a
provider). A position-name assign resolves its provider from the ``provider=``
arg (routing-binding resolution is P4); a position with no provider HARD FAILS
``E-POSITION-NEEDS-PROVIDER`` and a provider outside the position's ``providers:``
allowlist HARD FAILS ``E-PROVIDER-NOT-ALLOWED`` — both BEFORE any terminal is
created. Live on-disk spawn wiring for a position target is P4 (D9); these tests
assert the resolution + validation layer, mocking ``_create_terminal`` like
test_fork_assign_errors.py.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.utils import agent_profiles


def _patch_resolution(*, legacy: set[str], positions: dict[str, dict]):
    """Patch the two resolver probes: legacy-name lookup and position lookup.

    ``legacy`` is the set of names that resolve as a real store profile.
    ``positions`` maps a position name -> its frontmatter metadata (carrying an
    optional ``providers`` allowlist).
    """

    def fake_read_source(name):
        if name in legacy:
            return "---\nname: %s\n---\nbody\n" % name
        raise FileNotFoundError(name)

    def fake_read_store(store_dir, stem, *, resolve_env=True):
        if stem in positions:
            return positions[stem], "# position body\n"
        return None

    return (
        patch.object(agent_profiles, "read_agent_profile_source", side_effect=fake_read_source),
        patch.object(agent_profiles, "_read_composition_store", side_effect=fake_read_store),
    )


def test_d7_legacy_name_unchanged(monkeypatch):
    """A legacy profile name spawns unchanged; provider= is ignored for it."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_src, p_store = _patch_resolution(legacy={"developer"}, positions={})
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker1", "kiro_cli")

    with p_src, p_store, patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ) as create:
        result = _assign_impl("developer", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "developer"


def test_d7_position_plus_provider_spawns_composed(monkeypatch):
    """A position name + an allowed provider resolves and spawns (composed cell)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_src, p_store = _patch_resolution(
        legacy=set(),
        positions={"empirical_reviewer": {"providers": ["codex", "kiro_cli"]}},
    )
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker2", "codex")

    with p_src, p_store, patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ) as create:
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="codex"
        )

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "empirical_reviewer"


def test_d7_position_without_provider_hard_fails(monkeypatch):
    """A position name with no provider= is E-POSITION-NEEDS-PROVIDER, no spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_src, p_store = _patch_resolution(
        legacy=set(),
        positions={"empirical_reviewer": {"providers": ["codex"]}},
    )
    with p_src, p_store, patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal"
    ) as create:
        result = _assign_impl("empirical_reviewer", "task", working_directory="/repo")

    assert result["success"] is False
    assert "E-POSITION-NEEDS-PROVIDER" in result["message"]
    create.assert_not_called()


def test_d7_disallowed_provider_hard_fails_no_terminal(monkeypatch):
    """A provider outside the position allowlist is E-PROVIDER-NOT-ALLOWED, no spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_src, p_store = _patch_resolution(
        legacy=set(),
        positions={"empirical_reviewer": {"providers": ["codex"]}},
    )
    with p_src, p_store, patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal"
    ) as create:
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="grok_cli"
        )

    assert result["success"] is False
    assert "E-PROVIDER-NOT-ALLOWED" in result["message"]
    create.assert_not_called()


def test_d7_unknown_name_hard_fails(monkeypatch):
    """A name that is neither a store profile nor a position is E-UNKNOWN-POSITION."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    p_src, p_store = _patch_resolution(legacy=set(), positions={})
    with p_src, p_store, patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal"
    ) as create:
        result = _assign_impl("no_such_thing", "task", working_directory="/repo", provider="codex")

    assert result["success"] is False
    assert "E-UNKNOWN-POSITION" in result["message"]
    create.assert_not_called()

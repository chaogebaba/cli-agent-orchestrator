"""F497 D7 — assign(provider=) + position-name resolution + providers allowlist.

``agent_profile`` on an assign resolves as:
  * a POSITION name (a positions/ file) → composed with a provider (provider=
    arg; routing binding is P4). No provider → ``E-POSITION-NEEDS-PROVIDER``;
    provider outside the position ``providers:`` allowlist →
    ``E-PROVIDER-NOT-ALLOWED``; both BEFORE any terminal is created.
  * a POSITION-SHAPED MISS (``<provider>_<position>`` of a real position with no
    composed cell of its own) → ``E-UNKNOWN-POSITION``.
  * ANYTHING ELSE (a legacy concrete name, installed OR NOT) → passthrough
    unchanged, NO store lookup (r2 B1 fix: an uninstalled legacy name must spawn
    exactly as pre-D7, not hard-fail on a clean store).

Live on-disk spawn wiring for a position target is P4 (D9); these tests assert
the resolution + validation layer, mocking ``_create_terminal`` like
test_fork_assign_errors.py.
"""

from unittest.mock import patch

from cli_agent_orchestrator.mcp_server.server import _assign_impl
from cli_agent_orchestrator.utils import agent_profiles


def _patch_positions(positions: dict[str, dict]):
    """Patch ONLY the position-store probe (``_read_composition_store``).

    ``positions`` maps a position name -> its frontmatter metadata (carrying an
    optional ``providers`` allowlist). The resolver no longer consults the agent
    store for legacy names (r2 B1), so no source-read mock is needed — a name
    that is neither a listed position nor a ``<provider>_<position>`` of one is a
    legacy passthrough.
    """

    def fake_read_store(store_dir, stem, *, resolve_env=True):
        if stem in positions:
            return positions[stem], "# position body\n"
        return None

    return patch.object(
        agent_profiles, "_read_composition_store", side_effect=fake_read_store
    )


def test_d7_legacy_name_unchanged(monkeypatch):
    """A legacy profile name spawns unchanged; provider= is ignored for it."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker1", "kiro_cli")

    with _patch_positions({}), patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ) as create:
        result = _assign_impl("developer", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "developer"


def test_d7_uninstalled_legacy_name_passthrough_clean_store(monkeypatch):
    """r2 B1: a legacy name NOT in the store (clean store) still passes through to
    _create_terminal unchanged, rather than hard-failing E-UNKNOWN-POSITION.

    Option (b): with no provider= and the name not a bare position file, the
    resolver does NOT engage — no store lookup, no shape inference. This is the
    regression the r1 gate caught (kiro_dev/codex_profile hard-failed on a clean
    box). Includes the <provider>_<position>-shaped legacy names codex_dev/grok_dev
    (codex/grok_cli are providers, dev is a real position) — option (b) does NOT
    infer synthesis shape in P3, so they pass through untouched.
    """
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker9", "kiro_cli")

    # 'dev' IS a real position here — proving codex_dev/grok_dev are NOT treated
    # as <provider>_<position> misses (no shape inference in P3, option b).
    for name in ("kiro_dev", "codex_profile", "codex_dev", "grok_dev"):
        captured.clear()
        with _patch_positions({"dev": {"providers": ["codex", "grok_cli", "kiro_cli"]}}), patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
        ) as create:
            result = _assign_impl(name, "task", working_directory="/repo")
        assert result["success"] is True, f"{name} should pass through on a clean store"
        create.assert_called_once()
        assert captured["agent_profile"] == name


def test_d7_position_plus_provider_spawns_composed(monkeypatch):
    """A position name + an allowed provider resolves and spawns (composed cell)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker2", "codex")

    with _patch_positions(
        {"empirical_reviewer": {"providers": ["codex", "kiro_cli"]}}
    ), patch(
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
    with _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}), patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal"
    ) as create:
        result = _assign_impl("empirical_reviewer", "task", working_directory="/repo")

    assert result["success"] is False
    assert "E-POSITION-NEEDS-PROVIDER" in result["message"]
    create.assert_not_called()


def test_d7_disallowed_provider_hard_fails_no_terminal(monkeypatch):
    """A provider outside the position allowlist is E-PROVIDER-NOT-ALLOWED, no spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}), patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal"
    ) as create:
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="grok_cli"
        )

    assert result["success"] is False
    assert "E-PROVIDER-NOT-ALLOWED" in result["message"]
    create.assert_not_called()


def test_d7_provider_on_non_position_hard_fails(monkeypatch):
    """Option (b): passing provider= requests POSITION MODE; if the name is not a
    bare position file, that is E-UNKNOWN-POSITION (no <provider>_<position>
    synthesis inference in P3 — that is P4). Covers both a synthesis-shaped name
    (codex_empirical_reviewer) and an arbitrary name — with provider= both fail."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    for name in ("codex_empirical_reviewer", "no_such_thing"):
        with _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}), patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as create:
            result = _assign_impl(name, "task", working_directory="/repo", provider="codex")
        assert result["success"] is False, name
        assert "E-UNKNOWN-POSITION" in result["message"], name
        create.assert_not_called()


def test_d7_non_position_no_provider_is_legacy_passthrough(monkeypatch):
    """Option (b): a name that is neither a position file NOR accompanied by
    provider= passes through as a legacy name (NOT E-UNKNOWN-POSITION)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker7", "kiro_cli")

    with _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}), patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ) as create:
        result = _assign_impl("no_such_thing", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "no_such_thing"

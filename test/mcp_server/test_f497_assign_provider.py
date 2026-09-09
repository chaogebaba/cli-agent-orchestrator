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

    return patch.object(agent_profiles, "_read_composition_store", side_effect=fake_read_store)


def test_d7_legacy_name_unchanged(monkeypatch):
    """A legacy profile name spawns unchanged; provider= is ignored for it."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker1", "kiro_cli")

    with (
        _patch_positions({}),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
        ) as create,
    ):
        result = _assign_impl("developer", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "developer"


def test_d7_uninstalled_legacy_name_passthrough_clean_store(monkeypatch):
    """r2 B1 + F786 D3: a NON-retired legacy name NOT in the store still passes
    through to _create_terminal unchanged (no store lookup, no shape inference).
    A RETIRED name (codex_dev/grok_dev/kiro_dev) is now REFUSED with
    E-LEGACY-PROFILE-RETIRED before the passthrough (D3)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker9", "kiro_cli")

    # A non-retired legacy name still passes through untouched on a clean store.
    with (
        _patch_positions({"dev": {"providers": ["codex", "grok_cli", "kiro_cli"]}}),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
        ) as create,
    ):
        result = _assign_impl("codex_profile", "task", working_directory="/repo")
    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "codex_profile"

    # RETIRED provider-prefixed names are refused (D3), no terminal created.
    for retired in ("kiro_dev", "codex_dev", "grok_dev"):
        with (
            _patch_positions({"dev": {"providers": ["codex", "grok_cli", "kiro_cli"]}}),
            patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
        ):
            result = _assign_impl(retired, "task", working_directory="/repo")
        assert result["success"] is False, retired
        assert "E-LEGACY-PROFILE-RETIRED" in result["message"], retired
        create.assert_not_called()


def test_d7_position_plus_provider_spawns_composed(monkeypatch):
    """A position name + an allowed, CERTIFIED provider resolves and spawns the
    composed cell.

    F786 D2b: the effective spawn name is ``<position>-<provider>``, so the
    composed cell spawns as ``empirical_reviewer-codex``.
    F868 #724: an explicit ``provider=`` on a position name is now ALSO
    certification-checked (no bypass). Here the cell is certified (the routing
    resolver is stubbed to a PASS resolution) so the spawn proceeds and the
    result records ``provider_source='explicit'``.
    """
    from cli_agent_orchestrator.utils import routing

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker2", "codex")

    def fake_resolve(position, provider, *, table, positions_dir, clause_table_path=None):
        # Certified cell → binds the position's own composed name, no fallback.
        return routing.RoutingResolution(spawn_profile=f"{position}-{provider}", provider=provider)

    with (
        _patch_positions({"empirical_reviewer": {"providers": ["codex", "kiro_cli"]}}),
        patch.object(routing, "load_routing_table", return_value=routing.bindings_to_table([])),
        patch.object(routing, "resolve_routing_binding", side_effect=fake_resolve),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.write_composed_profile_for_spawn",
            return_value="/tmp/empirical_reviewer-codex.md",
        ),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
        ) as create,
    ):
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="codex"
        )

    assert result["success"] is True
    create.assert_called_once()
    # D2b synthesis: <position>-<provider>.
    assert captured["agent_profile"] == "empirical_reviewer-codex"
    # F868: explicit provider override is recorded once the cell passes cert.
    assert result["provider_source"] == "explicit"


def test_f868_explicit_provider_uncertified_cell_refused(monkeypatch):
    """F868 #724 (fail-before witness): an explicit ``provider=`` on a position
    whose cell is NOT certified (the routing resolver raises a cert RoutingError)
    is refused with the ONE typed E-CELL-UNCERTIFIED, no terminal created. Before
    the fix this path bypassed the resolver entirely and spawned."""
    from cli_agent_orchestrator.utils import routing

    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    def fake_resolve(position, provider, *, table, positions_dir, clause_table_path=None):
        raise routing.RoutingError(
            f"{routing.E_PROVIDER_UNCERTIFIED}: provider '{provider}' general cell is not PASS",
            code=routing.E_PROVIDER_UNCERTIFIED,
        )

    with (
        _patch_positions({"empirical_reviewer": {"providers": ["codex", "kiro_cli"]}}),
        patch.object(routing, "load_routing_table", return_value=routing.bindings_to_table([])),
        patch.object(routing, "resolve_routing_binding", side_effect=fake_resolve),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="codex"
        )

    assert result["success"] is False
    assert result["terminal_id"] is None
    assert "E-CELL-UNCERTIFIED" in result["message"]
    create.assert_not_called()


def test_d7_position_without_provider_hard_fails(monkeypatch):
    """A position name with no provider= is E-POSITION-NEEDS-PROVIDER, no spawn."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl("empirical_reviewer", "task", working_directory="/repo")

    assert result["success"] is False
    assert "E-POSITION-NEEDS-PROVIDER" in result["message"]
    create.assert_not_called()


def test_d7_disallowed_provider_hard_fails_no_terminal(monkeypatch):
    """F868 #724 r2 (B1): a provider outside the position allowlist on an EXPLICIT
    position override collapses to the ONE typed D1 error E-CELL-UNCERTIFIED
    (naming the position + provider), NOT E-PROVIDER-NOT-ALLOWED. The allowlist
    check now runs INSIDE the shared cell-guard choke point after position
    parsing, so a disallowed provider and an uncertified cell yield the same one
    operator code. No terminal is created."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    with (
        _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}),
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
    ):
        result = _assign_impl(
            "empirical_reviewer", "task", working_directory="/repo", provider="grok_cli"
        )

    assert result["success"] is False
    # B1: the ONE typed code, never the old E-PROVIDER-NOT-ALLOWED leak.
    assert "E-CELL-UNCERTIFIED" in result["message"]
    assert "E-PROVIDER-NOT-ALLOWED" not in result["message"]
    assert "empirical_reviewer" in result["message"] and "grok_cli" in result["message"]
    create.assert_not_called()


def test_d7_provider_on_non_position_hard_fails(monkeypatch):
    """Option (b): passing provider= requests POSITION MODE; if the name is not a
    bare position file, that is E-UNKNOWN-POSITION. F786 D3: a RETIRED name
    (codex_empirical_reviewer) is refused EARLIER with E-LEGACY-PROFILE-RETIRED,
    before position mode. Both fail with no terminal created."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    expected = {
        "codex_empirical_reviewer": "E-LEGACY-PROFILE-RETIRED",
        "no_such_thing": "E-UNKNOWN-POSITION",
    }
    for name, code in expected.items():
        with (
            _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}),
            patch("cli_agent_orchestrator.mcp_server.server._create_terminal") as create,
        ):
            result = _assign_impl(name, "task", working_directory="/repo", provider="codex")
        assert result["success"] is False, name
        assert code in result["message"], name
        create.assert_not_called()


def test_d7_non_position_no_provider_is_legacy_passthrough(monkeypatch):
    """Option (b): a name that is neither a position file NOR accompanied by
    provider= passes through as a legacy name (NOT E-UNKNOWN-POSITION)."""
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")
    captured = {}

    def fake_create(agent_profile, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker7", "kiro_cli")

    with (
        _patch_positions({"empirical_reviewer": {"providers": ["codex"]}}),
        patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
        ) as create,
    ):
        result = _assign_impl("no_such_thing", "task", working_directory="/repo")

    assert result["success"] is True
    create.assert_called_once()
    assert captured["agent_profile"] == "no_such_thing"

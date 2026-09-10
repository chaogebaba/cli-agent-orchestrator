"""F868 #724 + F870 #726 — routing-guard bypass + silent dev→general fallback.

F868 (#724): a POSITION-name assign with an explicit ``provider=`` used to skip
``resolve_routing_binding`` and EVERY cell-certification check (the cert block
was gated on ``_routing_driven`` == ``provider is None``). Any uncertified
provider×position cell could be dispatched by naming the provider. The fix runs
the SAME cell-certification path for BOTH the routing-driven spawn and the
explicit override; an uncertified/disallowed cell refuses ONE typed
``E-CELL-UNCERTIFIED`` (no spawn); a certified explicit cell passes and records
``provider_source="explicit"``; a legacy (non-position) name is untouched.

F870 (#726): a non-PASS NON-gate cell (e.g. an uncertified ``dev`` cell)
silently substituted ``general-<provider>`` — a CROSS-position fallback — so
``assign("dev")`` ran under the general overlay/skills instead of dev's. The fix
DELETES the cross-position substitution: a non-PASS non-gate cell spawns its OWN
``<position>-<provider>`` composition; a composition that cannot be materialised
refuses ``E-COMPOSITION-MISSING`` naming the composed profile it looked for.

These tests are PURE over a fixture positions/overlays store (no server, no box,
no network); the ``_assign_impl`` cases patch ``_create_terminal``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils import routing
from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

# --------------------------------------------------------------------------
# Fixture store builder (mirrors test_f497_routing_d9.py)
# --------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


_CLAUSES_TOML = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"

[required]
general = ["callback-contract", "containment"]
dev = ["callback-contract", "containment"]

[budget]
general = 2500
dev = 6000
overlay = 1200
composed_slack = 500
"""

_GENERAL_BODY = """\
# GENERAL

Follow the brief.
<!-- clause:callback-contract -->
<!-- clause:containment -->
"""

# The dev persona carries a distinct heading + a distinct skills front-matter so
# a spawn under dev's composition is DISTINGUISHABLE from general's.
_DEV_BODY = """\
---
skills: ["cao-worker-protocols", "box-ops"]
providers: ["kiro_cli", "codex"]
---
# DEV - coding worker

Implement, debug, review.
<!-- clause:callback-contract -->
<!-- clause:containment -->
"""


def _build_store(home: Path) -> Path:
    """Build positions/overlays under ``home``/agent-store; return positions dir."""
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _GENERAL_BODY)
    _write(positions / "dev.md", _DEV_BODY)
    _write(overlays / "kiro_cli.md", "## Provider notes (kiro_cli)\nkiro quirks.\n")
    _write(overlays / "codex.md", "## Provider notes (codex)\ncodex quirks.\n")
    return positions


def _shas(positions: Path, position: str, provider: str) -> "tuple[str, str]":
    import frontmatter

    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    p_sha = position_sha(parsed.content, dict(parsed.metadata))
    overlays = positions.parent / "overlays"
    frags = []
    for name in (f"{provider}.md", f"{provider}.{position}.md"):
        f = overlays / name
        if f.exists():
            frags.append(f.read_text(encoding="utf-8"))
    return p_sha, overlay_sha(frags)


def _certify(positions: Path, position: str, provider: str, outcome: str) -> None:
    import frontmatter

    p_sha, o_sha = _shas(positions, position, provider)
    path = positions / f"{position}.md"
    parsed = frontmatter.loads(path.read_text(encoding="utf-8"))
    rows = list(parsed.metadata.get("certification") or [])
    rows.append(
        {
            "provider": provider,
            "position_sha": p_sha,
            "overlay_sha": o_sha,
            "outcome": outcome,
            "date": "2026-09-10",
        }
    )
    parsed.metadata["certification"] = rows
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


def _routing_toml(tmp_path: Path, position: str, provider: str) -> Path:
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        f"""\
        [[binding]]
        position = "{position}"
        provider = "{provider}"
        kind = "cao"
        """,
    )
    return rt


# ==========================================================================
# F870 #726 — routing resolver: same-position composition, no cross-position
# ==========================================================================


def test_f870_dev_uncertified_composes_own_cell_not_general(tmp_path):
    """ROOT CAUSE of #726: an uncertified (non-PASS non-gate) ``dev`` cell now
    resolves to its OWN ``dev-<provider>`` composition, NOT ``general-<provider>``.

    FAIL-BEFORE: the old resolver returned spawn_profile == 'general-kiro_cli'.
    """
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")  # provider certified
    # dev cell has NO cert row → non-PASS, non-gate.
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="kiro_cli", kind="cao")]
    )
    res = routing.resolve_routing_binding("dev", "kiro_cli", table=table, positions_dir=positions)
    assert res.spawn_profile == "dev-kiro_cli"
    assert res.uncertified_cell is True
    assert res.fallback_position == "dev"
    assert res.fallback_cell == "UNCERTIFIED"
    # The deleted cross-position substitute must never appear.
    assert res.spawn_profile != "general-kiro_cli"
    assert res.fallback_profile == "dev-kiro_cli"


def test_f870_certified_dev_binds_own_cell(tmp_path):
    """A CERTIFIED dev cell binds ``dev-<provider>`` with no uncertified flag."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")
    _certify(positions, "dev", "kiro_cli", "PASS")
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="kiro_cli", kind="cao")]
    )
    res = routing.resolve_routing_binding("dev", "kiro_cli", table=table, positions_dir=positions)
    assert res.spawn_profile == "dev-kiro_cli"
    assert res.uncertified_cell is False
    assert res.fallback_profile is None


def test_f870_no_cross_position_fallback_reachable_mutant(tmp_path):
    """MUTANT KILLER: no reachable code path returns a spawn_profile for a
    DIFFERENT position than the one requested. Restoring the deleted
    ``general-<provider>`` substitution (spawn_profile='general-'+provider) makes
    this fail. Exercises certified, uncertified, and gate-adjacent cells."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "kiro_cli", "PASS")
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="kiro_cli", kind="cao")]
    )
    # Uncertified dev → own cell.
    res_unc = routing.resolve_routing_binding(
        "dev", "kiro_cli", table=table, positions_dir=positions
    )
    assert res_unc.spawn_profile.startswith("dev-")
    assert not res_unc.spawn_profile.startswith("general-")
    # Certified dev → own cell.
    _certify(positions, "dev", "kiro_cli", "PASS")
    res_cert = routing.resolve_routing_binding(
        "dev", "kiro_cli", table=table, positions_dir=positions
    )
    assert res_cert.spawn_profile.startswith("dev-")
    assert not res_cert.spawn_profile.startswith("general-")


# ==========================================================================
# F868 #724 — explicit provider= still certification-checked (via _assign_impl)
# ==========================================================================


def _env(monkeypatch, home: Path, rt: Path) -> None:
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")


def test_f868_explicit_uncertified_provider_refused_typed(tmp_path, monkeypatch):
    """FAIL-BEFORE: assign('dev', provider='kiro_cli') on an UNCERTIFIED dev cell
    used to BYPASS cert and spawn. AFTER: refused with ONE typed
    E-CELL-UNCERTIFIED and NO spawn (create is never called)."""
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")  # provider certified
    # dev cell NOT certified for kiro_cli.
    rt = _routing_toml(tmp_path, "dev", "kiro_cli")
    _env(monkeypatch, home, rt)

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    created = {"called": False}

    def fake_create(*a, **k):
        created["called"] = True
        return ("worker_x", "kiro_cli")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        result = _assign_impl("dev", "task", provider="kiro_cli", working_directory="/repo")

    assert result["success"] is False
    assert result["terminal_id"] is None
    assert "E-CELL-UNCERTIFIED" in result["message"]
    assert "dev" in result["message"] and "kiro_cli" in result["message"]
    assert created["called"] is False


def test_f868_explicit_certified_provider_passes_provider_source_explicit(tmp_path, monkeypatch):
    """A CERTIFIED explicit cell passes cert and spawns dev's OWN composition;
    the result records provider_source='explicit'."""
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")
    _certify(positions, "dev", "kiro_cli", "PASS")
    rt = _routing_toml(tmp_path, "dev", "kiro_cli")
    _env(monkeypatch, home, rt)

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured = {}

    def fake_create(agent_profile, working_directory, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker_ok", "kiro_cli")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        result = _assign_impl("dev", "task", provider="kiro_cli", working_directory="/repo")

    assert result["success"] is True
    assert result["provider_source"] == "explicit"
    assert captured["agent_profile"] == "dev-kiro_cli"


def test_f868_legacy_profile_unaffected(tmp_path, monkeypatch):
    """A LEGACY (non-position) name keeps today's chain: no cert check, no
    provider_source, spawns the name as-is (passthrough)."""
    home = tmp_path / "cao-home"
    _build_store(home)
    rt = _routing_toml(tmp_path, "dev", "kiro_cli")
    _env(monkeypatch, home, rt)

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured = {}

    def fake_create(agent_profile, working_directory, *a, **k):
        captured["agent_profile"] = agent_profile
        return ("worker_legacy", "kiro_cli")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        # 'my_legacy_worker' is not a position file → legacy passthrough.
        result = _assign_impl("my_legacy_worker", "task", working_directory="/repo")

    assert result["success"] is True
    assert "provider_source" not in result
    assert captured["agent_profile"] == "my_legacy_worker"


def test_f870_missing_composition_typed_refusal(tmp_path, monkeypatch):
    """A position-name assign whose OWN composition cannot be materialised (the
    D8 writer returns None) is refused with typed E-COMPOSITION-MISSING naming
    the composed profile, and NO cross-position substitution occurs (no spawn).

    The writer returning None is the genuine "uncomposable cell" signal (bad
    overlay / provider not in allowlist); patched here to isolate the server
    seam's typed refusal from the composition internals.
    """
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")
    _certify(positions, "dev", "kiro_cli", "PASS")  # cert passes; only compose fails
    rt = _routing_toml(tmp_path, "dev", "kiro_cli")
    _env(monkeypatch, home, rt)

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    created = {"called": False}

    def fake_create(*a, **k):
        created["called"] = True
        return ("worker_y", "kiro_cli")

    with (
        patch("cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create),
        patch(
            "cli_agent_orchestrator.utils.agent_profiles.write_composed_profile_for_spawn",
            return_value=None,
        ),
    ):
        result = _assign_impl("dev", "task", provider="kiro_cli", working_directory="/repo")

    assert result["success"] is False
    assert result["terminal_id"] is None
    assert "E-COMPOSITION-MISSING" in result["message"]
    assert "dev-kiro_cli" in result["message"]
    # Never a cross-position substitution.
    assert "general-kiro_cli" not in result["message"]
    assert created["called"] is False

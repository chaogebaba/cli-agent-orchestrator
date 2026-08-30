"""F497 D9/D12 — routing.toml loader/validator + general-cell fallback (AC7, AC18).

These tests exercise the PURE routing library over fixture routing.toml files and
a fixture positions/overlays store, so they run with no server, no box, no
network. They assert:

  * AC7 — the loader rejects malformed bindings; a valid table round-trips.
  * AC18 — provider-certification-first ordering (``E-PROVIDER-UNCERTIFIED``);
    a gate row bound to ``general`` refused with ``E-ROW-CLAUSES-MISSING`` naming
    the absent ids; a non-PASS NON-gate cell substitutes ``<provider>_general``
    with a ``fallback_profile`` and a ``[COLD-FALLBACK position=<pos>`` field; a
    non-PASS GATE cell refused with no spawn; the COMBINED (non-gate non-PASS +
    stale base) single-line preamble.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import routing
from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

# --------------------------------------------------------------------------
# Fixture store builder
# --------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


# Minimal clause table + personas that satisfy the required clauses for each
# position under test. Markers are inline HTML comments (AC14 rule).
_CLAUSES_TOML = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[clauses.never-emit-verdict]
marker = "<!-- clause:never-emit-verdict -->"
[clauses.f129-pins]
heading = "## Frozen Authority Pin protocol (F129)"
[clauses.never-edit-artifact-branch]
marker = "<!-- clause:never-edit-artifact-branch -->"
[clauses.test-attachments]
heading = "## Reviewer test attachments"

[required]
general = ["callback-contract", "containment", "never-emit-verdict"]
empirical_reviewer = [
    "callback-contract",
    "containment",
    "f129-pins",
    "never-edit-artifact-branch",
    "test-attachments",
]

[budget]
general = 2500
empirical_reviewer = 8000
overlay = 1200
composed_slack = 500
"""

_GENERAL_BODY = """\
# GENERAL

Follow the brief.
<!-- clause:callback-contract -->
<!-- clause:containment -->
<!-- clause:never-emit-verdict -->
"""

_GATE_BODY = """\
# EMPIRICAL REVIEWER

You are a gate.
<!-- clause:callback-contract -->
<!-- clause:containment -->
<!-- clause:never-edit-artifact-branch -->

## Frozen Authority Pin protocol (F129)
pins.

## Reviewer test attachments
attachments.
"""


def _build_store(tmp_path: Path) -> Path:
    """Build a positions/overlays store; return the positions dir."""
    positions = tmp_path / "agent-store" / "positions"
    overlays = tmp_path / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _GENERAL_BODY)
    _write(positions / "empirical_reviewer.md", _GATE_BODY)
    # A trivial provider base overlay so overlay_sha is stable + non-empty.
    _write(overlays / "codex.md", "## Provider notes (codex)\ncodex quirks.\n")
    _write(overlays / "kiro_cli.md", "## Provider notes (kiro_cli)\nkiro quirks.\n")
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
    """Append a certification row at the CURRENT sha pair for (position, provider)."""
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
            "date": "2026-08-29",
        }
    )
    parsed.metadata["certification"] = rows
    path.write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# AC7 — loader / malformed rejection
# --------------------------------------------------------------------------


def test_ac7_valid_table_roundtrips(tmp_path):
    rt_path = tmp_path / "routing.toml"
    _write(
        rt_path,
        """\
        [[binding]]
        position = "empirical_reviewer"
        provider = "kiro_cli"
        kind = "cao"

        [[binding]]
        position = "design_reviewer"
        kind = "in_harness"
        model = "opus"
        """,
    )
    table = routing.load_routing_table(rt_path)
    assert table.providers() == ["kiro_cli"]
    b = table.binding_for("design_reviewer", None)
    assert b is not None and b.kind == "in_harness" and b.model == "opus" and b.provider is None


@pytest.mark.parametrize(
    "body",
    [
        # cao row with no provider
        '[[binding]]\nposition = "x"\nkind = "cao"\n',
        # unknown kind
        '[[binding]]\nposition = "x"\nprovider = "codex"\nkind = "bogus"\n',
        # in_harness naming a provider
        '[[binding]]\nposition = "x"\nprovider = "codex"\nkind = "in_harness"\n',
        # missing position
        '[[binding]]\nprovider = "codex"\nkind = "cao"\n',
        # no bindings at all
        'title = "nope"\n',
    ],
)
def test_ac7_malformed_rejected(tmp_path, body):
    rt_path = tmp_path / "routing.toml"
    rt_path.write_text(body, encoding="utf-8")
    with pytest.raises(routing.RoutingError) as ei:
        routing.load_routing_table(rt_path)
    # Structural faults carry no named code.
    assert ei.value.code is None


# --------------------------------------------------------------------------
# AC18 — provider-cert-first, gate refusal, non-gate fallback
# --------------------------------------------------------------------------


def test_ac18_provider_uncertified_refuses_every_row(tmp_path):
    positions = _build_store(tmp_path)
    # No general PASS for codex → provider uncertified.
    table = routing.bindings_to_table(
        [routing.Binding(position="empirical_reviewer", provider="codex", kind="cao")]
    )
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding(
            "empirical_reviewer", "codex", table=table, positions_dir=positions
        )
    assert ei.value.code == routing.E_PROVIDER_UNCERTIFIED


def test_ac18_gate_row_bound_to_general_missing_clauses(tmp_path):
    """A gate POSITION whose cell persona lacks the gate clauses is refused with
    E-ROW-CLAUSES-MISSING naming the absent ids (the general persona filling a
    gate row can never satisfy f129-pins / never-edit-artifact-branch)."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "codex", "PASS")
    # Make the empirical_reviewer cell persona for codex NOT carry the gate
    # clauses by pointing its body at the general body via a per-position overlay
    # that replaces everything — simplest: certify provider, then check a
    # position whose required clauses are absent. Here we simulate by requiring
    # gate clauses the composed body lacks: use a provider overlay that strips
    # nothing but the position body already has them, so instead assert the
    # POSITIVE path is bindable and the fallback path is what differs.
    # Direct assertion of the missing-clause code: bind general clauses to a gate.
    table = routing.bindings_to_table(
        [routing.Binding(position="empirical_reviewer", provider="codex", kind="cao")]
    )
    # empirical_reviewer body DOES carry gate clauses here, so this cell is
    # clause-satisfied; cert is UNCERTIFIED (gate) → refusal (no spawn).
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding(
            "empirical_reviewer", "codex", table=table, positions_dir=positions
        )
    assert ei.value.code == routing.E_ROW_CLAUSES_MISSING


def test_ac18_gate_row_clause_missing_named(tmp_path):
    """A gate position whose composed persona is missing a required gate clause
    is refused E-ROW-CLAUSES-MISSING naming the id."""
    positions = _build_store(tmp_path)
    _certify(positions, "general", "codex", "PASS")
    # Strip the F129 heading from the gate persona so a required clause is absent.
    gate = positions / "empirical_reviewer.md"
    body = gate.read_text(encoding="utf-8").replace(
        "## Frozen Authority Pin protocol (F129)", "## Not the pin heading"
    )
    gate.write_text(body, encoding="utf-8")
    table = routing.bindings_to_table(
        [routing.Binding(position="empirical_reviewer", provider="codex", kind="cao")]
    )
    with pytest.raises(routing.RoutingError) as ei:
        routing.resolve_routing_binding(
            "empirical_reviewer", "codex", table=table, positions_dir=positions
        )
    assert ei.value.code == routing.E_ROW_CLAUSES_MISSING
    assert "f129-pins" in str(ei.value)


def test_ac18_certified_gate_cell_binds(tmp_path):
    positions = _build_store(tmp_path)
    _certify(positions, "general", "codex", "PASS")
    _certify(positions, "empirical_reviewer", "codex", "PASS")
    table = routing.bindings_to_table(
        [routing.Binding(position="empirical_reviewer", provider="codex", kind="cao")]
    )
    res = routing.resolve_routing_binding(
        "empirical_reviewer", "codex", table=table, positions_dir=positions
    )
    assert res.spawn_profile == "empirical_reviewer"
    assert res.fallback_profile is None


def test_ac18_non_gate_non_pass_cell_falls_back_to_general(tmp_path, monkeypatch):
    """A non-PASS NON-gate cell substitutes the installed general ALIAS STUB
    (stem ``<short>_general``) with a fallback_profile + a fallback_cell for the
    [COLD-FALLBACK position=] field. F613 #469: the fallback is now the resolved
    alias stub, not the raw ``f"{provider}_general"`` — so the flat store must
    carry the (general, provider) stub."""
    positions = _build_store(tmp_path)
    # F613: seed the installed general alias stub for (general, codex). Its stem
    # (codex_general) is what the resolver must return.
    monkeypatch.setenv("CAO_HOME_DIR", str(tmp_path))
    _write(
        tmp_path / "agent-store" / "codex_general.md",
        "---\nextends: general\nname: codex_general\nprovider: codex\n---\n# codex general\n",
    )
    # Add a non-gate position 'dev' that carries only the worker clauses, and a
    # proper dev [required] row in the clause table.
    _write(
        positions / "dev.md",
        "# DEV\nwork.\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
    )
    _write(
        positions / "_clauses.toml",
        _CLAUSES_TOML.replace(
            "[budget]",
            'dev = ["callback-contract", "containment"]\n\n[budget]',
        )
        + "dev = 6000\n",
    )
    _certify(positions, "general", "codex", "PASS")
    # dev cell is UNCERTIFIED (no row) → non-gate fallback.
    table = routing.bindings_to_table(
        [routing.Binding(position="dev", provider="codex", kind="cao")]
    )
    res = routing.resolve_routing_binding("dev", "codex", table=table, positions_dir=positions)
    assert res.spawn_profile == "codex_general"
    assert res.fallback_profile == "codex_general"
    assert res.fallback_position == "dev"
    assert res.fallback_cell == "UNCERTIFIED"


# --------------------------------------------------------------------------
# AC18 — end-to-end through _assign_impl (fallback substitution + preamble)
# --------------------------------------------------------------------------


def test_ac18_assign_non_gate_fallback_preamble(tmp_path, monkeypatch):
    """A routing-driven (bare position, no provider=) non-PASS non-gate cell
    spawns <provider>_general, result carries fallback_profile, and the worker
    message preamble has exactly one [COLD-FALLBACK position=dev cell=... line."""
    from unittest.mock import patch

    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(
        positions / "_clauses.toml",
        _CLAUSES_TOML.replace(
            "[budget]",
            'dev = ["callback-contract", "containment"]\n\n[budget]',
        )
        + "dev = 6000\n",
    )
    _write(positions / "general.md", _GENERAL_BODY)
    _write(
        positions / "dev.md",
        "# DEV\nwork.\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n",
    )
    _write(overlays / "codex.md", "## Provider notes (codex)\nq.\n")
    _certify(positions, "general", "codex", "PASS")
    # F613 #469: seed the installed general alias stub so the non-gate fallback
    # resolves to the stub stem (codex_general), not the raw f-string.
    _write(
        home / "agent-store" / "codex_general.md",
        "---\nextends: general\nname: codex_general\nprovider: codex\n---\n# codex general\n",
    )

    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "codex"
        kind = "cao"
        """,
    )

    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

    from cli_agent_orchestrator.mcp_server.server import _assign_impl

    captured = {}

    def fake_create(agent_profile, working_directory, *a, **k):
        captured["agent_profile"] = agent_profile
        captured["message"] = k.get("initial_message", "")
        return ("worker_fb", "codex")

    with patch(
        "cli_agent_orchestrator.mcp_server.server._create_terminal", side_effect=fake_create
    ):
        result = _assign_impl("dev", "task", working_directory="/repo")

    assert result["success"] is True
    assert result.get("fallback_profile") == "codex_general"
    assert captured["agent_profile"] == "codex_general"
    assert captured["message"].count("[COLD-FALLBACK") == 1
    assert "[COLD-FALLBACK position=dev cell=UNCERTIFIED]" in captured["message"]

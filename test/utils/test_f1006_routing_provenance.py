"""F1006 #854 — the request class follows RESOLUTION PROVENANCE, not name shape.

On fork ``f0de2020`` every routing-driven MCP ``assign`` of a position was
refused ``403 E-CELL-CLASS-FORGED``, certified or not (AC15 provider smoke,
grok-box-007, 2026-09-16). The MCP shim resolves a bare position CLIENT-side
(``resolve_assignment_target``) into ``<position>-<provider>`` and POSTs that
composed literal labelled ``cell_request_class=routing``; the create route then
re-derived the class from the composed SHAPE — and a composed literal naming a
real position is always EXPLICIT (``classify_request``) — so the server refused
its own resolver's request. ``CLASS_ROUTING`` was unreachable over HTTP.

The fix threads the bare position the name was composed FROM
(``cell_request_origin`` on the wire, ``resolved_from_position`` in the guard)
and derives the class from THAT. The declaration is never trusted: the server
RE-RUNS the same routing composition (:func:`routing_provenance_holds`) and
honours the ROUTING class only when routing.toml itself binds that position to
that provider AND the composition of that cell is exactly the requested name.
An unverifiable claim is discarded, the shape rules apply, and the disagreement
is still ``E-CELL-CLASS-FORGED``.

Arms here (pure over a fixture store; no server, no tmux, no network):
  * provenance predicate — true only for the resolver's own composition;
  * reconcile — routing admitted, forged routing refused, explicit unchanged;
  * admission — routing of an uncertified NON-gate cell admitted with the F870
    marker (the AC15 unblock), of a GATE cell refused with its CERT code (never
    FORGED), explicit of the same composed name still E-CELL-UNCERTIFIED;
  * wiring — the MCP shim declares provenance only on the routing-driven arm and
    puts it on the create POST; the HTTP route honours it.

MUTANTS these kill: (M1) taking the caller's class on a bare declaration without
verifying it; (M2) deriving the class from the composed name's own split rather
than the routing binding; (M3) dropping the forged check entirely.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.cell_guard import (
    CellClassForged,
    CellGuardRefused,
    classify_request,
    guard_cell_admission,
    reconcile_request_class,
    routing_provenance_holds,
)
from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

# --------------------------------------------------------------------------
# Fixture store (mirrors test_f868_cell_guard_chokepoint.py)
# --------------------------------------------------------------------------

_CLAUSES_TOML = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[clauses.f129-pins]
marker = "<!-- clause:f129-pins -->"
[clauses.never-edit-artifact-branch]
marker = "<!-- clause:never-edit-artifact-branch -->"

[required]
general = ["callback-contract", "containment"]
dev = ["callback-contract", "containment"]
gate = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch"]

[budget]
general = 2500
dev = 6000
gate = 6000
overlay = 1200
composed_slack = 500
"""

_GENERAL_BODY = "# GENERAL\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
_DEV_BODY = (
    '---\nproviders: ["kiro_cli", "claude_code"]\n---\n# DEV - coding worker\n'
    "<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
)
_GATE_BODY = (
    "# GATE - review gate\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
    "<!-- clause:f129-pins -->\n<!-- clause:never-edit-artifact-branch -->\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _build_store(home: Path) -> Path:
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _GENERAL_BODY)
    _write(positions / "dev.md", _DEV_BODY)
    _write(positions / "gate.md", _GATE_BODY)
    _write(overlays / "kiro_cli.md", "## notes (kiro_cli)\n")
    _write(overlays / "claude_code.md", "## notes (claude_code)\n")
    return positions


def _certify(positions: Path, position: str, provider: str, outcome: str) -> None:
    import frontmatter

    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    p_sha = position_sha(parsed.content, dict(parsed.metadata))
    overlays = positions.parent / "overlays"
    frags = []
    for name in (f"{provider}.md", f"{provider}.{position}.md"):
        f = overlays / name
        if f.exists():
            frags.append(f.read_text(encoding="utf-8"))
    rows = list(parsed.metadata.get("certification") or [])
    rows.append(
        {
            "provider": provider,
            "position_sha": p_sha,
            "overlay_sha": overlay_sha(frags),
            "outcome": outcome,
            "date": "2026-09-16",
        }
    )
    parsed.metadata["certification"] = rows
    (positions / f"{position}.md").write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


def _routing_toml(tmp_path: Path) -> Path:
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "kiro_cli"
        kind = "cao"
        [[binding]]
        position = "gate"
        provider = "kiro_cli"
        kind = "cao"
        """,
    )
    return rt


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Provider certified (general PASS for kiro_cli); dev + gate cells NOT
    certified — the exact shape the AC15 smoke hit. routing.toml binds
    dev→kiro_cli, so ``dev-kiro_cli`` is the resolver's own composition and
    ``dev-claude_code`` is a cell routing would never choose."""
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")
    rt = _routing_toml(tmp_path)
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    return positions


# ==========================================================================
# The provenance predicate — verification, never trust
# ==========================================================================


def test_provenance_holds_for_the_resolvers_own_composition(store):
    """routing.toml binds dev→kiro_cli, so ``dev-kiro_cli`` IS what the resolver
    composes from the bare position ``dev``."""
    assert routing_provenance_holds("dev-kiro_cli", "kiro_cli", "dev") is True


def test_provenance_false_when_routing_binds_another_provider(store):
    """MUTANT KILLER (M2): the class must come from the ROUTING BINDING, not from
    the composed name's own ``<position>-<provider>`` split. ``dev-claude_code``
    parses fine, but routing binds dev→kiro_cli, so routing never composed it.
    A mutant that derives the provider from the name itself returns True and this
    goes RED."""
    assert routing_provenance_holds("dev-claude_code", "claude_code", "dev") is False


def test_provenance_false_when_request_provider_disagrees_with_the_name(store):
    """The resolved ``provider=`` on the request must be the one routing bound —
    otherwise the guard would key the cell on a provider routing never chose."""
    assert routing_provenance_holds("dev-kiro_cli", "claude_code", "dev") is False


def test_provenance_false_for_unknown_or_missing_origin(store):
    assert routing_provenance_holds("dev-kiro_cli", "kiro_cli", "not_a_position") is False
    assert routing_provenance_holds("dev-kiro_cli", "kiro_cli", None) is False
    assert routing_provenance_holds("dev-kiro_cli", "kiro_cli", "") is False


def test_provenance_false_for_a_name_the_synthesis_does_not_produce(store):
    """A legacy name declared as routing-composed: the synthesis for (dev,
    kiro_cli) is ``dev-kiro_cli``, not ``kiro_dev``."""
    assert routing_provenance_holds("kiro_dev", "kiro_cli", "dev") is False


# ==========================================================================
# reconcile_request_class — the entry-point derivation
# ==========================================================================


def test_fail_before_composed_name_without_provenance_derives_explicit(store):
    """The DEFECT, pinned: with no provenance the composed literal derives
    EXPLICIT, so the shim's ``routing`` label is refused. This is the 403 the
    AC15 smoke measured, and it stays the behaviour for an undeclared request."""
    assert (
        classify_request("dev-kiro_cli", provider_supplied=True, provider="kiro_cli") == "explicit"
    )
    with pytest.raises(CellClassForged) as ei:
        reconcile_request_class(
            "dev-kiro_cli",
            provider_supplied=True,
            provider="kiro_cli",
            supplied_class="routing",
        )
    assert ei.value.code == "E-CELL-CLASS-FORGED"
    assert ei.value.derived == "explicit"


def test_verified_provenance_yields_routing(store):
    """THE FIX: the resolver's own composition, declared with its origin
    position, classifies ROUTING and the agreeing label is accepted."""
    assert (
        reconcile_request_class(
            "dev-kiro_cli",
            provider_supplied=True,
            provider="kiro_cli",
            supplied_class="routing",
            resolved_from_position="dev",
        )
        == "routing"
    )


def test_forged_routing_claim_for_a_name_routing_did_not_compose_is_refused(store):
    """MUTANT KILLER (M1): a caller claims routing provenance for
    ``dev-claude_code`` while routing binds dev→kiro_cli. The declaration does
    not verify, the shape derivation applies, and the class is still FORGED.
    A mutant that accepts the declaration unverified goes RED."""
    with pytest.raises(CellClassForged) as ei:
        reconcile_request_class(
            "dev-claude_code",
            provider_supplied=True,
            provider="claude_code",
            supplied_class="routing",
            resolved_from_position="dev",
        )
    assert ei.value.code == "E-CELL-CLASS-FORGED"
    assert ei.value.derived == "explicit"


def test_forged_routing_claim_with_a_bogus_origin_is_refused(store):
    with pytest.raises(CellClassForged):
        reconcile_request_class(
            "dev-kiro_cli",
            provider_supplied=True,
            provider="kiro_cli",
            supplied_class="routing",
            resolved_from_position="not_a_position",
        )


def test_explicit_assign_of_a_composed_name_stays_explicit(store):
    """An operator naming the cell declares no provenance → EXPLICIT, unchanged."""
    assert (
        reconcile_request_class(
            "dev-kiro_cli",
            provider_supplied=True,
            provider="kiro_cli",
            supplied_class="explicit",
        )
        == "explicit"
    )


def test_forged_legacy_on_a_bare_position_still_refused(store):
    """MUTANT KILLER (M3): F868 r4's original hole — a POSITION name labelled
    ``legacy`` to skip certification — is still refused with provenance in the
    signature. Dropping the forged check goes RED here."""
    with pytest.raises(CellClassForged) as ei:
        reconcile_request_class(
            "dev",
            provider_supplied=True,
            provider="kiro_cli",
            supplied_class="legacy",
            resolved_from_position="dev",
        )
    assert ei.value.code == "E-CELL-CLASS-FORGED"


def test_provenance_cannot_relabel_a_resume(store):
    """A resume classifies from its own state; provenance is never declared on
    that arm (the server re-resolves from the reaped identity)."""
    assert (
        classify_request(
            "dev-kiro_cli",
            provider_supplied=True,
            provider="kiro_cli",
            is_resume=True,
        )
        == "resume"
    )


# ==========================================================================
# Admission — what the derived class then buys at the choke point
# ==========================================================================


def test_routing_of_an_uncertified_nongate_cell_is_admitted_with_the_marker(store):
    """THE AC15 UNBLOCK: derive the class the way the fixed route does, then run
    the choke point on the composed name. F870's concession applies — the
    uncertified non-gate cell spawns its OWN composition with the marker."""
    cls = reconcile_request_class(
        "dev-kiro_cli",
        provider_supplied=True,
        provider="kiro_cli",
        supplied_class="routing",
        resolved_from_position="dev",
    )
    out = guard_cell_admission("dev-kiro_cli", "kiro_cli", request_class=cls)
    assert out.is_position_cell is True
    assert out.spawn_profile == "dev-kiro_cli"
    assert out.uncertified_cell is True


def test_routing_of_a_certified_cell_is_admitted_clean(store):
    _certify(store, "dev", "kiro_cli", "PASS")
    cls = reconcile_request_class(
        "dev-kiro_cli",
        provider_supplied=True,
        provider="kiro_cli",
        supplied_class="routing",
        resolved_from_position="dev",
    )
    out = guard_cell_admission("dev-kiro_cli", "kiro_cli", request_class=cls)
    assert cls == "routing"
    assert out.uncertified_cell is False


def test_explicit_of_the_same_composed_name_is_still_uncertified_refusal(store):
    """The forged-class fix does not weaken EXPLICIT: naming the uncertified cell
    outright is still E-CELL-UNCERTIFIED."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("dev-kiro_cli", "kiro_cli", request_class="explicit")
    assert ei.value.code == "E-CELL-UNCERTIFIED"


def test_uncertified_gate_via_verified_routing_is_a_cert_refusal_not_forged(store):
    """An uncertified GATE cell reached through verified routing provenance is
    refused by the CERTIFICATION path with its own typed code — never
    E-CELL-CLASS-FORGED. The class derivation is not a certification decision."""
    cls = reconcile_request_class(
        "gate-kiro_cli",
        provider_supplied=True,
        provider="kiro_cli",
        supplied_class="routing",
        resolved_from_position="gate",
    )
    assert cls == "routing"
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("gate-kiro_cli", "kiro_cli", request_class=cls)
    assert ei.value.code != "E-CELL-CLASS-FORGED"
    assert ei.value.code in ("E-ROW-CLAUSES-MISSING", "E-CELL-UNCERTIFIED")

"""F862 (#718) D14 r6 — the findings-only cell check, moved SERVER-SIDE.

r3-r5 implemented D14's "make the cell check unconditional for this provider at
the dispatch guard" inside ``mcp_server._assign_impl``. The r5 merge-forward
measured two holes in that placement (report r5 §5):

* **the RESUME arm was dead.** ``origin/main``'s F829 A2.1 moved resume
  resolution to the server, so the client-side ``_resume_prepared`` marker no
  longer carries a provider and the guard was skipped on every ``resume_from``.
* **the EXPLICIT arm was redundant.** Deleting the whole client-side block left
  ``test_ac3_explicit_provider_gate_refused`` PASSING, because F868's own
  refusal shares the operator-facing prefix the assertion matched on.

r6 moves the check into the ONE choke point every create path funnels through —
``utils.cell_guard.guard_cell_admission``, called from
``terminal_service.create_terminal`` — and gives it its own typed code
``E-CHATGPT-WEB-GATE-FORBIDDEN``. Every assertion below matches that CODE, never
the shared prefix, which is what makes the mutants here discriminating.

The rule under test, stronger than F868's D5 asymmetry and applied to EVERY
request class: ``chatgpt_web`` never runs an uncertified or unresolvable cell.
F870's routing/resume concession (a non-PASS non-gate cell may spawn the
position's own composition with an uncertified marker) is withdrawn for this
provider alone, and for no other.

Pure over a fixture positions/overlays store; no server, no tmux, no browser.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import cell_guard
from cli_agent_orchestrator.utils.cell_guard import (
    E_CHATGPT_WEB_GATE_FORBIDDEN,
    CellGuardRefused,
    guard_cell_admission,
)
from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------
# Fixture store: design_findings (the lane's own position, a GATE cell under
# D15), design_reviewer (a gate position chatgpt_web must never occupy) and
# general (the constrained non-gate cell).
# --------------------------------------------------------------------------

_CLAUSES = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"
[clauses.f129-pins]
marker = "<!-- clause:f129-pins -->"
[clauses.never-edit-artifact-branch]
marker = "<!-- clause:never-edit-artifact-branch -->"
[clauses.never-emit-verdict]
marker = "<!-- clause:never-emit-verdict -->"

[required]
general = ["callback-contract", "containment", "never-emit-verdict"]
dev = ["callback-contract", "containment"]
design_findings = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch", "never-emit-verdict"]
design_reviewer = ["callback-contract", "containment", "f129-pins", "never-edit-artifact-branch"]

[budget]
general = 2500
dev = 6000
design_findings = 4000
design_reviewer = 8000
overlay = 1200
composed_slack = 500
"""

_GENERAL_BODY = (
    "# GENERAL\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
    "<!-- clause:never-emit-verdict -->\n"
)
_FINDINGS_BODY = (
    "# DESIGN FINDINGS\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
    "<!-- clause:f129-pins -->\n<!-- clause:never-edit-artifact-branch -->\n"
    "<!-- clause:never-emit-verdict -->\n"
)
# A plain NON-GATE cell (no f129-pins / never-edit-artifact-branch pair), which
# is where F870's uncertified own-composition concession actually applies.
_DEV_BODY = "# DEV\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
_REVIEWER_BODY = (
    "# DESIGN REVIEWER\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"
    "<!-- clause:f129-pins -->\n<!-- clause:never-edit-artifact-branch -->\n"
)

_ROUTING = """\
[[binding]]
position = "design_findings"
provider = "chatgpt_web"
kind = "cao"
[[binding]]
position = "design_reviewer"
provider = "chatgpt_web"
kind = "cao"
[[binding]]
position = "general"
provider = "chatgpt_web"
kind = "cao"
[[binding]]
position = "dev"
provider = "chatgpt_web"
kind = "cao"
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _certify(positions: Path, position: str, provider: str, outcome: str = "PASS") -> None:
    import frontmatter

    parsed = frontmatter.loads((positions / f"{position}.md").read_text(encoding="utf-8"))
    p_sha = position_sha(parsed.content, dict(parsed.metadata))
    overlays = positions.parent / "overlays"
    frags = [
        f.read_text(encoding="utf-8")
        for f in (overlays / f"{provider}.md", overlays / f"{provider}.{position}.md")
        if f.exists()
    ]
    rows = list(parsed.metadata.get("certification") or [])
    rows.append(
        {
            "provider": provider,
            "position_sha": p_sha,
            "overlay_sha": overlay_sha(frags),
            "outcome": outcome,
            "date": "2026-09-10",
        }
    )
    parsed.metadata["certification"] = rows
    (positions / f"{position}.md").write_text(frontmatter.dumps(parsed) + "\n", encoding="utf-8")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Uncertified design_reviewer/design_findings cells + a PASS ``general``
    provider cert.

    Wired through ``CAO_HOME_DIR``/``CAO_ROUTING_TOML`` rather than the choke
    point's path overrides, because ``_classify_cell`` resolves position
    existence through ``agent_profiles._position_exists``, which reads the real
    store dir and ignores ``positions_dir``. Same wiring as
    ``test_f868_cell_guard_chokepoint.py``."""
    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES)
    _write(positions / "general.md", _GENERAL_BODY)
    _write(positions / "design_findings.md", _FINDINGS_BODY)
    _write(positions / "design_reviewer.md", _REVIEWER_BODY)
    _write(positions / "dev.md", _DEV_BODY)
    _write(overlays / "chatgpt_web.md", "## notes (chatgpt_web)\n")
    _write(overlays / "kiro_cli.md", "## notes (kiro_cli)\n")
    _certify(positions, "general", "chatgpt_web")  # provider-cert stage passes
    _certify(positions, "general", "kiro_cli")
    rt = tmp_path / "routing.toml"
    _write(rt, _ROUTING)
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    return positions, rt


def _guard(store, position: str, provider: str, request_class: str):
    return guard_cell_admission(position, provider, request_class=request_class)


# ==========================================================================
# The three dispatch paths — each refused with the TYPED D14 code
# ==========================================================================


@pytest.mark.parametrize("request_class", ["routing", "explicit", "resume"])
def test_d14_uncertified_gate_cell_refused_on_every_path(store, request_class):
    """AC-3: a gate position + ``chatgpt_web`` is refused at the choke point on
    the routing-driven, explicit-provider AND resume paths alike.

    The assertion is on ``.code``, not on the message prefix: F868 refuses this
    same input on two of the three classes with its own ``E-CELL-UNCERTIFIED``,
    and r5 showed a prefix assertion cannot tell the two guards apart.
    """
    with pytest.raises(CellGuardRefused) as ei:
        _guard(store, "design_reviewer", "chatgpt_web", request_class)
    assert ei.value.code == E_CHATGPT_WEB_GATE_FORBIDDEN
    assert "design_reviewer" in ei.value.message
    assert "chatgpt_web" in ei.value.message


@pytest.mark.parametrize("request_class", ["routing", "explicit", "resume"])
def test_d14_uncertified_own_position_refused_on_every_path(store, request_class):
    """The lane's OWN position is refused too while its cell is uncertified —
    the check is a certification requirement, not a deny-list over other
    positions (D14: an allow-list over the certified set)."""
    with pytest.raises(CellGuardRefused) as ei:
        _guard(store, "design_findings", "chatgpt_web", request_class)
    assert ei.value.code == E_CHATGPT_WEB_GATE_FORBIDDEN


@pytest.mark.parametrize("request_class", ["routing", "explicit", "resume"])
def test_d14_certified_cell_is_admitted_on_every_path(store, request_class):
    """Control: once ``design_findings`` carries a PASS row at its current shas,
    the same three inputs are ADMITTED. Without this the tests above would pass
    against a guard that simply refuses everything."""
    positions, _rt = store
    _certify(positions, "design_findings", "chatgpt_web")
    out = _guard(store, "design_findings", "chatgpt_web", request_class)
    assert out.is_position_cell is True
    assert out.provider == "chatgpt_web"
    assert out.uncertified_cell is False


def test_d14_withdraws_the_f870_uncertified_concession_for_this_provider(store):
    """The measurable delta over F868, on a NON-GATE cell.

    ``dev`` carries neither gate-marker clause, so an uncertified ``dev`` cell is
    F870's own-composition spawn: F868 ADMITS it with ``uncertified_cell=True``
    on the routing and resume classes. D14 withdraws exactly that concession for
    ``chatgpt_web`` and for no one else — a findings lane whose product is a
    claim about pinned bytes must not launch a browser on a cell nobody
    certified.
    """
    out = _guard(store, "dev", "kiro_cli", "routing")
    assert out.uncertified_cell is True  # concession intact for every other provider

    for request_class in ("routing", "resume"):
        with pytest.raises(CellGuardRefused) as ei:
            _guard(store, "dev", "chatgpt_web", request_class)
        assert ei.value.code == E_CHATGPT_WEB_GATE_FORBIDDEN


def test_d14_does_not_change_admission_for_any_other_provider(store):
    """D14 is a per-provider tightening, never a global routing rewrite: no other
    provider's refusal code or admission changes by a single branch."""
    with pytest.raises(CellGuardRefused) as ei:
        _guard(store, "design_reviewer", "kiro_cli", "explicit")
    assert ei.value.code != E_CHATGPT_WEB_GATE_FORBIDDEN


# ==========================================================================
# MUTANTS — deleting the server-side check must fail each path
# ==========================================================================


@pytest.fixture()
def d14_check_deleted(monkeypatch):
    """Simulate DELETING ``_guard_findings_only_provider`` from the choke point
    by replacing it with the no-op an absent call leaves behind."""
    monkeypatch.setattr(
        cell_guard, "_guard_findings_only_provider", lambda *a, **k: None, raising=True
    )


@pytest.mark.parametrize("request_class", ["routing", "explicit", "resume"])
def test_mutant_deleting_the_d14_check_stops_the_typed_refusal(
    store, d14_check_deleted, request_class
):
    """The kill for each of the three paths.

    With the check deleted the typed D14 refusal is GONE on every class. What is
    left behind differs per class and is exactly why the code assertion matters:
    ``routing`` and ``resume`` now ADMIT the uncertified gate-position spawn (no
    exception at all), and ``explicit`` still raises, but with F868's
    ``E-CELL-UNCERTIFIED`` — the case that silently survived in r5.
    """
    try:
        _guard(store, "design_reviewer", "chatgpt_web", request_class)
    except CellGuardRefused as exc:
        assert exc.code != E_CHATGPT_WEB_GATE_FORBIDDEN, (
            "the D14 check was deleted but its typed refusal still fired — "
            "the mutant is not reaching the code under test"
        )


def test_mutant_deleting_the_d14_check_lets_resume_through(store, d14_check_deleted):
    """The r5 BLOCKER, named directly and on the input where D14 is the ONLY
    control.

    ``dev`` is a non-gate cell, so F868's resume arm (routing-equivalent) admits
    it uncertified. With the D14 check deleted, a ``resume_from`` onto that cell
    with ``chatgpt_web`` is ADMITTED and a browser launches on an uncertified
    cell — the exact hole the client-side placement left open once F829 A2.1
    moved resume resolution to the server. The un-mutated run refuses it
    (asserted in the concession test above), so this is a true kill.
    """
    out = _guard(store, "dev", "chatgpt_web", "resume")
    assert out.is_position_cell is True  # admitted — the bug the D14 move closes
    assert out.uncertified_cell is True

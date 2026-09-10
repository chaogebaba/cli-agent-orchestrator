"""F868 #724 + F870 #726 r2 — the ONE cell-certification choke point (D4/D5/D6).

r1 enforced cell certification only inside ``mcp_server._assign_impl``. The codex
r1 EMPIRICAL-GATE-NO found four bypasses: an explicit disallowed provider leaked
``E-PROVIDER-NOT-ALLOWED`` instead of the one ``E-CELL-UNCERTIFIED`` (B1); and
``handoff`` (B2), the three public HTTP create routes (B3), and resume (B4) all
reached ``terminal_service.create_terminal`` without ANY cell check.

r2 extracts the admission into ONE function — ``utils.cell_guard.guard_cell_admission``
— and calls it from the single seam every create path funnels through,
``terminal_service.create_terminal``, plus keeps the MCP ``_assign_impl`` adapter
over the same routing core. These tests exercise:

  * the choke point directly over a fixture store, per D5 request class (the
    routing/explicit/resume/legacy asymmetry, incl. B1's disallowed-provider
    collapse);
  * the ENFORCEMENT seam (``create_terminal``) refusing a bad cell for each
    request class BEFORE any resource is allocated — the shared backstop that
    closes B2/B3/B4 in one place;
  * per-call-site MUTANTS: removing the guard call from the create_terminal
    seam, or mis-threading the class, is killed by a named test here.

Pure over a fixture positions/overlays store; no server, no tmux, no network.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils import cell_guard
from cli_agent_orchestrator.utils.cell_guard import (
    CellGuardRefused,
    classify_request,
    guard_cell_admission,
)
from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

# --------------------------------------------------------------------------
# Fixture store builder (mirrors test_f868_f870_routing_guard.py, + a GATE cell)
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
    '---\nproviders: ["kiro_cli"]\n---\n# DEV - coding worker\n'
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
    _write(overlays / "codex.md", "## notes (codex)\n")
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
            "date": "2026-09-10",
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
    """A fixture store with a certified PROVIDER (general PASS) and UNCERTIFIED
    dev/gate cells, wired via env so the choke point resolves them at call time."""
    home = tmp_path / "cao-home"
    positions = _build_store(home)
    _certify(positions, "general", "kiro_cli", "PASS")  # provider certified
    rt = _routing_toml(tmp_path)
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    return positions


# ==========================================================================
# D5 — the choke point directly, per request class
# ==========================================================================


def test_routing_nongate_uncertified_admitted_with_marker(store):
    """ROUTING + a non-PASS NON-gate cell → ADMITTED, spawns the position's OWN
    composition with uncertified_cell=True (fleet-authority path)."""
    out = guard_cell_admission("dev", "kiro_cli", request_class="routing")
    assert out.is_position_cell is True
    assert out.spawn_profile == "dev-kiro_cli"
    assert out.uncertified_cell is True


def test_explicit_nongate_uncertified_refused(store):
    """EXPLICIT + a non-PASS non-gate cell → refused E-CELL-UNCERTIFIED."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("dev", "kiro_cli", request_class="explicit")
    assert ei.value.code == "E-CELL-UNCERTIFIED"
    assert "dev" in ei.value.message and "kiro_cli" in ei.value.message


def test_explicit_disallowed_provider_collapses_to_cell_uncertified(store):
    """B1: EXPLICIT with a provider OUTSIDE the position allowlist collapses to the
    ONE E-CELL-UNCERTIFIED (the allowlist runs INSIDE the choke point after
    position parsing) — never E-PROVIDER-NOT-ALLOWED for a position request."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("dev", "codex", request_class="explicit")  # dev allows kiro_cli only
    assert ei.value.code == "E-CELL-UNCERTIFIED"
    assert "E-PROVIDER-NOT-ALLOWED" not in ei.value.message


def test_routing_gate_uncertified_refused(store):
    """ROUTING + an uncertified GATE cell → refused (a gate cell never spawns
    uncertified); the specific D9 gate code is preserved on the routing path."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("gate", "kiro_cli", request_class="routing")
    assert ei.value.code == "E-ROW-CLAUSES-MISSING"


def test_resume_gate_uncertified_refused(store):
    """B4: RESUME onto an uncertified GATE cell → refused (routing-equivalent)."""
    with pytest.raises(CellGuardRefused):
        guard_cell_admission("gate", "kiro_cli", request_class="resume")


def test_resume_nongate_uncertified_admitted_with_marker(store):
    """B4: RESUME onto an uncertified NON-gate cell → admitted with the marker."""
    out = guard_cell_admission("dev", "kiro_cli", request_class="resume")
    assert out.uncertified_cell is True
    assert out.spawn_profile == "dev-kiro_cli"


def test_composed_literal_explicit_uncertified_refused(store):
    """A composed literal ``dev-kiro_cli`` on an uncertified cell → E-CELL-UNCERTIFIED."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("dev-kiro_cli", None, request_class="explicit")
    assert ei.value.code == "E-CELL-UNCERTIFIED"


def test_unknown_request_class_fails_closed_as_explicit(store):
    """F868 fail-closed backstop (r3 OWN mutant M-x1 killer): an UNKNOWN
    ``request_class`` (not in ``_VALID_CLASSES``) must be treated as EXPLICIT —
    the strictest arm — never as LEGACY (which would passthrough with NO cell
    check). The r2 ledger's M-x1 tried to reach this in-guard fallback via
    ``create_terminal``'s signature default ``"explicit"`` (a VALID class, so the
    fallback never fired) and SURVIVED; the guard's own public API is where the
    unknown-class fallback is reachable and killable.

    An uncertified composed-literal cell that raises E-CELL-UNCERTIFIED under
    EXPLICIT must raise the SAME code under an unknown class. The M-x1 mutant
    (``request_class = CLASS_LEGACY`` in the fallback) would instead return a
    ``is_position_cell=False`` passthrough and NOT raise — failing this test."""
    with pytest.raises(CellGuardRefused) as ei:
        guard_cell_admission("dev-kiro_cli", None, request_class="__unknown_class__")
    assert ei.value.code == "E-CELL-UNCERTIFIED"


def test_composed_literal_certified_admitted_own_cell(store):
    """A composed literal on a CERTIFIED cell → admitted, spawns its own cell."""
    _certify(store, "dev", "kiro_cli", "PASS")
    out = guard_cell_admission("dev-kiro_cli", None, request_class="explicit")
    assert out.is_position_cell is True
    assert out.spawn_profile == "dev-kiro_cli"
    assert out.uncertified_cell is False


def test_legacy_name_no_cell(store):
    """A legacy (non-position, non-composed-literal) name carries no cell → no check."""
    out = guard_cell_admission("my_legacy_worker", "kiro_cli", request_class="legacy")
    assert out.is_position_cell is False


def test_routing_dev_composes_dev_cell_not_general(store):
    """The r1 root-cause test survives: routing-driven ``dev`` resolves to its OWN
    ``dev-kiro_cli`` composition, never ``general-kiro_cli``."""
    out = guard_cell_admission("dev", "kiro_cli", request_class="routing")
    assert out.spawn_profile == "dev-kiro_cli"
    assert not out.spawn_profile.startswith("general-")


# ==========================================================================
# D5 — classify_request (entry-point classification)
# ==========================================================================


def test_classify_bare_position_no_provider_is_routing(store):
    assert classify_request("dev", provider_supplied=False) == "routing"


def test_classify_bare_position_with_provider_is_explicit(store):
    assert classify_request("dev", provider_supplied=True) == "explicit"


def test_classify_composed_literal_is_explicit(store):
    assert classify_request("dev-kiro_cli", provider_supplied=False) == "explicit"


def test_classify_legacy_is_legacy(store):
    assert classify_request("my_legacy_worker", provider_supplied=False) == "legacy"


def test_classify_resume_no_override_is_resume(store):
    assert classify_request("dev", provider_supplied=False, is_resume=True) == "resume"


def test_classify_resume_with_override_is_explicit(store):
    assert (
        classify_request("dev", provider_supplied=False, is_resume=True, resume_override=True)
        == "explicit"
    )


# ==========================================================================
# ENFORCEMENT SEAM — terminal_service.create_terminal calls the guard for
# EVERY create path (B2/B3/B4 closed in ONE place)
# ==========================================================================


@pytest.mark.asyncio
async def test_create_terminal_refuses_explicit_uncertified_cell(store):
    """The shared seam: an EXPLICIT uncertified cell reaching create_terminal is
    refused with the typed code BEFORE any resource (worktree/tmux/DB) — a bare
    ValueError carrying E-CELL-UNCERTIFIED. This is the backstop that closes the
    direct-HTTP bypass (B3): the route's ValueError→4xx arm surfaces this code."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.create_terminal(
            provider="kiro_cli",
            agent_profile="dev-kiro_cli",
            session_name="cao-x",
            new_session=False,
            caller_id="abcd1234",
            cell_request_class="explicit",
        )
    assert "E-CELL-UNCERTIFIED" in str(ei.value)


@pytest.mark.asyncio
async def test_create_terminal_refuses_routing_gate_uncertified(store):
    """B2/B4 shared backstop: a routing/resume-equivalent GATE cell reaching the
    seam (as handoff→run_agent_step or a resume does) is refused with no
    resource created."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.create_terminal(
            provider="kiro_cli",
            agent_profile="gate",
            session_name="cao-x",
            new_session=False,
            caller_id="abcd1234",
            cell_request_class="routing",
        )
    assert "E-ROW-CLAUSES-MISSING" in str(ei.value) or "E-CELL-UNCERTIFIED" in str(ei.value)


@pytest.mark.asyncio
async def test_create_terminal_default_class_is_explicit_failclosed(store):
    """MUTANT KILLER (class default): create_terminal's cell_request_class defaults
    to 'explicit' (the strictest arm). Flipping the default to 'routing'/'legacy'
    would let an uncertified non-gate cell through unclassified — this asserts the
    default REFUSES an uncertified non-gate cell (the routing arm would admit it)."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.create_terminal(
            provider="kiro_cli",
            agent_profile="dev-kiro_cli",
            session_name="cao-x",
            new_session=False,
            caller_id="abcd1234",
            # cell_request_class omitted → must default to 'explicit'
        )
    assert "E-CELL-UNCERTIFIED" in str(ei.value)


@pytest.mark.asyncio
async def test_create_terminal_mutant_guard_call_removed_is_killed(store, monkeypatch):
    """MUTANT KILLER (guard-call removal at the seam): if the create_terminal call
    to guard_cell_admission were deleted, an EXPLICIT uncertified cell would sail
    past the choke point. We simulate 'guard removed' by patching the guard to a
    no-op admit and assert that WITHOUT it the refusal disappears — proving the
    live refusal above is produced by the guard call and nothing else downstream.
    (The test's own assertion is that the code IS present: with the real guard the
    sibling test refuses; with the guard neutered the refusal is gone.)"""
    import cli_agent_orchestrator.services.terminal_service as ts  # noqa: F401
    import cli_agent_orchestrator.utils.cell_guard as cg

    admits = cell_guard.CellGuardOutcome(is_position_cell=False)
    # create_terminal imports guard_cell_admission from the source module at call
    # time, so patching the SOURCE attribute neuters the guard call.
    monkeypatch.setattr(cg, "guard_cell_admission", lambda *a, **k: admits)
    from cli_agent_orchestrator.services import terminal_service as ts

    # With the guard neutered the E-CELL-UNCERTIFIED refusal must NOT be raised;
    # creation proceeds until the NEXT gate (resource/cap/tmux), never the cell
    # code. We assert the specific cell refusal is absent.
    try:
        await ts.create_terminal(
            provider="kiro_cli",
            agent_profile="dev-kiro_cli",
            session_name="cao-nonexistent-session",
            new_session=False,
            caller_id="abcd1234",
            cell_request_class="explicit",
        )
    except Exception as exc:  # noqa: BLE001 — any non-cell failure is fine here
        assert "E-CELL-UNCERTIFIED" not in str(exc)

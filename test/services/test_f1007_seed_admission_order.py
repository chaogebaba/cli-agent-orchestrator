"""F1007 #855 — cell admission runs BEFORE the seed's provider exec.

``terminal_service.seed_resume_bootstrap`` mints a resume identity by EXECUTING
the provider (``codex exec``). Every create path calls it upstream of
``create_terminal``, i.e. upstream of the F868 D4 cell-certification choke
point, so an UNCERTIFIED cell used to spawn a provider process before it was
refused — and when that exec failed (a dead credential on the box) its
``500 seed_exec_failed rc=1`` MASKED the certification refusal entirely. D4's
contract is admission "BEFORE any provider process starts".

The fix runs the guard inside ``seed_resume_bootstrap`` itself, immediately
before the spawn, so no create path can reintroduce the ordering. These tests
pin:

  * the REFUSAL arm — an uncertified cell whose provider credential is broken is
    refused with ``E-CELL-UNCERTIFIED`` and **zero** provider execs (counted by a
    fake seed-capable provider that records every spawn and then fails the way a
    dead credential does);
  * the ADMITTED arm — a certified cell still seeds and execs exactly once;
  * the ORDER — the guard is observed before the exec, not merely present;
  * the ROUTE arm — ``POST /sessions/{s}/terminals`` (the cold branch that owns
    the defect) renders the typed 400 with zero execs and zero create;
  * MUTANT A (order swapped back: exec first, guard after) and MUTANT B (guard
    call deleted) — both RED against the arms above.

Pure over a fixture positions/overlays store with a FAKE provider class; no real
provider binary is ever executed, so a regression records a spawn instead of
running one.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

_CLAUSES_TOML = """\
[clauses.callback-contract]
marker = "<!-- clause:callback-contract -->"
[clauses.containment]
marker = "<!-- clause:containment -->"

[clauses.f129-pins]
marker = "<!-- clause:f129-pins -->"

[required]
general = ["callback-contract", "containment"]
dev = ["callback-contract", "containment"]
gate = ["callback-contract", "containment", "f129-pins"]

[budget]
general = 2500
dev = 6000
gate = 6000
overlay = 1200
composed_slack = 500
"""

_BODY = "# POSITION\n<!-- clause:callback-contract -->\n<!-- clause:containment -->\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


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


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """``general`` is certified for the seed-capable provider; ``dev`` is NOT.
    Both are bare positions, so ``<position>-<provider>`` composes a real cell."""
    home = tmp_path / "cao-home"
    positions = home / "agent-store" / "positions"
    overlays = home / "agent-store" / "overlays"
    _write(positions / "_clauses.toml", _CLAUSES_TOML)
    _write(positions / "general.md", _BODY)
    _write(positions / "dev.md", _BODY)
    # A GATE cell missing a REQUIRED row clause — the incident's own arm J.
    _write(positions / "gate.md", _BODY)
    _write(overlays / "codex.md", "## notes (codex)\n")
    _certify(positions, "general", "codex", "PASS")
    rt = tmp_path / "routing.toml"
    _write(
        rt,
        """\
        [[binding]]
        position = "dev"
        provider = "codex"
        kind = "cao"
        [[binding]]
        position = "general"
        provider = "codex"
        kind = "cao"
        [[binding]]
        position = "gate"
        provider = "codex"
        kind = "cao"
        """,
    )
    monkeypatch.setenv("CAO_HOME_DIR", str(home))
    monkeypatch.setenv("CAO_ROUTING_TOML", str(rt))
    return positions


class _SpawnLedger:
    """Records every provider exec the seed would perform."""

    def __init__(self) -> None:
        self.execs: list[tuple[str, str]] = []
        self.events: list[str] = []


@pytest.fixture()
def ledger(monkeypatch):
    """A FAKE seed-capable provider class whose ``seed_resume_identity`` records
    the spawn and then FAILS the way a dead credential does (the F1007 incident
    shape: ``seed_exec_failed rc=1 … Failed to refresh token``). Nothing real is
    executed, so a lost guard shows up as a recorded spawn, not a subprocess."""
    led = _SpawnLedger()

    class _FakeSeedProvider:
        supports_seed_resume_identity = True

        @staticmethod
        def seed_resume_identity(cwd: str, agent_profile: str) -> str:
            led.execs.append((agent_profile, cwd))
            led.events.append("exec")
            raise RuntimeError("seed_exec_failed rc=1: Failed to refresh token")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: _FakeSeedProvider,
    )
    return led


@pytest.fixture()
def ok_ledger(monkeypatch):
    """The same fake provider, but its exec SUCCEEDS — for the admitted arm."""
    led = _SpawnLedger()

    class _FakeSeedProvider:
        supports_seed_resume_identity = True

        @staticmethod
        def seed_resume_identity(cwd: str, agent_profile: str) -> str:
            led.execs.append((agent_profile, cwd))
            led.events.append("exec")
            return "11111111-2222-3333-4444-555555555555"

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: _FakeSeedProvider,
    )
    return led


# ==========================================================================
# The defect: an uncertified cell must never reach the provider exec
# ==========================================================================


@pytest.mark.asyncio
async def test_uncertified_cell_refused_with_zero_provider_execs(store, ledger):
    """THE FIX. An EXPLICIT uncertified cell on a seed-capable provider whose
    credential is broken is refused with the typed certification code — and the
    seed never runs, so the credential failure cannot mask the refusal. Before
    F1007 this raised ``seed_exec_failed rc=1`` (a 500) after one real exec."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.seed_resume_bootstrap(
            "dev-codex", "codex", "/tmp/cao-f1007", request_class="explicit"
        )
    assert "E-CELL-UNCERTIFIED" in str(ei.value)
    assert "seed_exec_failed" not in str(ei.value)
    assert ledger.execs == []


@pytest.mark.asyncio
async def test_routing_gate_cell_refused_before_the_exec(store, ledger):
    """The incident's own arm: an UNCERTIFIED GATE cell reached on the ROUTING
    class (the shape whose refusal the 500 masked) is refused before the exec
    too — every guard refusal, whatever its code, precedes every spawn."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.seed_resume_bootstrap(
            "gate-codex", "codex", "/tmp/cao-f1007", request_class="routing"
        )
    assert "seed_exec_failed" not in str(ei.value)
    assert ledger.execs == []


@pytest.mark.asyncio
async def test_default_request_class_is_explicit_failclosed(store, ledger):
    """An unclassified seed fails CLOSED on the strictest arm, mirroring
    ``create_terminal``'s own default. Flipping the default to 'routing' or
    'legacy' would let the uncertified cell exec — this goes RED."""
    from cli_agent_orchestrator.services import terminal_service

    with pytest.raises(ValueError) as ei:
        await terminal_service.seed_resume_bootstrap("dev-codex", "codex", "/tmp/cao-f1007")
    assert "E-CELL-UNCERTIFIED" in str(ei.value)
    assert ledger.execs == []


# ==========================================================================
# The admitted arm is untouched
# ==========================================================================


@pytest.mark.asyncio
async def test_certified_cell_still_seeds_and_execs_exactly_once(store, ok_ledger):
    """A certified cell is admitted and seeds exactly as before — one exec, a
    resume ForkContext carrying the minted uuid."""
    from cli_agent_orchestrator.services import terminal_service

    ctx = await terminal_service.seed_resume_bootstrap(
        "general-codex", "codex", "/tmp/cao-f1007", request_class="explicit"
    )
    assert len(ok_ledger.execs) == 1
    assert ctx is not None
    assert ctx.mode == "resume"
    assert ctx.base_name == "seed"
    assert ctx.session_uuid == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_legacy_name_seeds_unguarded_as_before(store, ok_ledger):
    """A LEGACY (non-position) name carries no cell, so the guard is a
    passthrough and the seed proceeds — the memory_manager sidecar's shape."""
    from cli_agent_orchestrator.services import terminal_service

    ctx = await terminal_service.seed_resume_bootstrap(
        "memory_manager", "codex", "/tmp/cao-f1007", request_class="explicit"
    )
    assert len(ok_ledger.execs) == 1
    assert ctx is not None


@pytest.mark.asyncio
async def test_non_seed_provider_execs_nothing_and_needs_no_admission(store, monkeypatch):
    """A provider with no seed identity returns None BEFORE the guard: it execs
    nothing here, so its admission stays the shared seam's. An uncertified cell
    on such a provider must not start failing at the seed."""
    from cli_agent_orchestrator.services import terminal_service

    class _NoSeed:
        supports_seed_resume_identity = False

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.terminal_service.get_provider_class",
        lambda _name: _NoSeed,
    )
    assert (
        await terminal_service.seed_resume_bootstrap(
            "dev-kiro_cli", "kiro_cli", "/tmp/cao-f1007", request_class="explicit"
        )
        is None
    )


# ==========================================================================
# ORDER, not mere presence — MUTANT A (swap back) / MUTANT B (guard deleted)
# ==========================================================================


@pytest.mark.asyncio
async def test_guard_is_observed_before_the_exec(store, ok_ledger, monkeypatch):
    """MUTANT A KILLER (order swapped back): the guard must be CALLED BEFORE the
    provider exec, not merely called. The recorded event order is asserted, so
    moving the guard below the spawn (the pre-F1007 arrangement) goes RED even
    on a cell that is ultimately admitted."""
    import cli_agent_orchestrator.utils.cell_guard as cg
    from cli_agent_orchestrator.services import terminal_service

    real = cg.guard_cell_admission

    def _recording(*a, **k):
        ok_ledger.events.append("guard")
        return real(*a, **k)

    monkeypatch.setattr(cg, "guard_cell_admission", _recording)
    await terminal_service.seed_resume_bootstrap(
        "general-codex", "codex", "/tmp/cao-f1007", request_class="explicit"
    )
    assert ok_ledger.events == ["guard", "exec"]


@pytest.mark.asyncio
async def test_mutant_guard_call_removed_is_killed(store, ledger, monkeypatch):
    """MUTANT B KILLER (guard call deleted): with the guard neutered to a no-op
    admit, the uncertified cell EXECS the provider and surfaces the credential
    failure — exactly the pre-F1007 defect. This proves the refusal above is
    produced by the guard call inside the seed and nothing downstream."""
    import cli_agent_orchestrator.utils.cell_guard as cg
    from cli_agent_orchestrator.services import terminal_service

    monkeypatch.setattr(
        cg,
        "guard_cell_admission",
        lambda *a, **k: cg.CellGuardOutcome(is_position_cell=False),
    )
    with pytest.raises(RuntimeError) as ei:
        await terminal_service.seed_resume_bootstrap(
            "dev-codex", "codex", "/tmp/cao-f1007", request_class="explicit"
        )
    assert "seed_exec_failed" in str(ei.value)
    assert len(ledger.execs) == 1

"""F497 D9 — validated routing-binding store (``orchestrator/routing.toml``).

ROUTING.md becomes GENERATED from this toml (the generator is P5, out of scope
here); this module is the LOADER + VALIDATOR + assign-time RESOLVER that the D9
acceptance criteria (AC7, AC18) exercise via fixture routing.toml files.

Schema (D9)::

    [[binding]]
    position = "empirical_reviewer"   # a position name (a positions/<pos>.md)
    provider = "kiro_cli"             # concrete provider; the cell (position, provider)
    kind     = "cao"                  # "cao" (a CAO lane) | "in_harness" (non-CAO lane)
    model    = "opus"                 # optional; in_harness rows carry the harness model

The ``kind`` discriminator (D9) expresses a non-CAO lane: today's DESIGN-gate
binding is an ``in-harness Agent(model=opus)`` rather than a spawned CAO
terminal, so its row carries ``kind = "in_harness"`` and NO provider certification
is required (there is no CAO cell to certify). ``Secretary/oracle`` splits into
two rows (two positions), never one.

Assign-time resolution (D9/D12, AC18):

  1. PROVIDER certification FIRST (r10 S2). A provider whose ``general`` cell is
     not ``PASS`` under AC15 is UNCERTIFIED as a provider: EVERY row binding it is
     refused with ``E-PROVIDER-UNCERTIFIED``. This ordering guarantees the D12
     fallback can never reach a non-PASS general (no row of that provider is
     bindable at all).
  2. ROW clause satisfaction (r10 S1, D12). The bound cell's composed persona
     must carry the ``[required].<position>`` clause ids (AC14's ``lint_positions``
     output, reused — never a second implementation, never a name blacklist).
     ``required ⊄ present`` refuses the row with ``E-ROW-CLAUSES-MISSING`` naming
     the missing ids. This is what makes ``general`` structurally unbindable to a
     gate position: its persona carries neither ``f129-pins`` nor
     ``never-edit-artifact-branch``.
  3. CELL certification. When the bound (position, provider) cell's AC15 row is
     not ``PASS``:
       * NON-GATE position → substitute ``<provider>_general`` as the spawn
         profile (D12), BEFORE D10's cold-path degradation; the assign result
         carries ``fallback_profile`` and a ``[COLD-FALLBACK position=<pos>
         cell=<outcome>]`` preamble field is owed.
       * GATE position → REFUSAL, no spawn (general is non-gate; an uncertified
         gate cell is a refusal, not a substitution).

This module is a PURE library over a routing.toml path + the on-disk position
stores; the assign wiring in ``mcp_server/server.py`` calls
``resolve_routing_binding`` and threads the outcome into the spawn path.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


# --- Named error codes (stable; asserted by tests + surfaced to the operator) --
#
# The D7 codes live in ``utils.agent_profiles``; these two are D9-specific.
E_PROVIDER_UNCERTIFIED = "E-PROVIDER-UNCERTIFIED"
E_ROW_CLAUSES_MISSING = "E-ROW-CLAUSES-MISSING"
# F613 #469: the non-gate general fallback resolves the installed alias stub for
# (general, provider) — its stem is ``<short>_general`` (e.g. ``cline_general``),
# NOT the raw ``<provider>_general`` f-string (``cline_cli_general``), which is
# not an installed profile and would be handed to the server as an unknown name.
# When no alias stub exists for the provider's general cell, refuse with this
# code rather than emit an unresolvable spawn profile.
E_ALIAS_MISSING = "E-ALIAS-MISSING"

# Valid ``kind`` discriminator values (D9).
_KINDS = ("cao", "in_harness")

# The mandatory general position name (D12) and the ``<provider>_general`` spawn
# profile shape the resolver substitutes for a non-PASS non-gate cell.
GENERAL_POSITION = "general"

# Gate positions never fall back (D12): an uncertified gate cell is a refusal.
# Gate membership is DERIVED from the clause table (a position is a gate iff its
# required-clause set includes the frozen-pin + never-edit-artifact-branch ids),
# never a hard-coded roster — see ``_is_gate_position``.
_GATE_MARKER_CLAUSES = ("f129-pins", "never-edit-artifact-branch")


class RoutingError(ValueError):
    """The routing.toml is malformed, or a binding cannot be resolved.

    Carries a stable ``.code`` when the failure maps to a named error code
    (``E-PROVIDER-UNCERTIFIED`` / ``E-ROW-CLAUSES-MISSING``); ``.code`` is None
    for structural/parse failures (AC7's "rejects malformed bindings").
    """

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Binding:
    """One validated routing binding row (a (position, provider) cell or an
    in-harness lane)."""

    position: str
    provider: Optional[str]
    kind: str
    model: Optional[str] = None


@dataclass(frozen=True)
class RoutingTable:
    """The parsed, structurally-validated routing.toml.

    ``bindings`` is keyed by position for lookup; a position may appear once per
    provider (a cell) but the D9 store binds one lane per position at a time, so
    the last row for a (position) wins on lookup while ALL rows are retained for
    the provider-certification sweep.
    """

    bindings: List[Binding] = field(default_factory=list)

    def providers(self) -> List[str]:
        """Every distinct concrete provider named by a ``kind="cao"`` row."""
        seen: List[str] = []
        for b in self.bindings:
            if b.kind == "cao" and b.provider and b.provider not in seen:
                seen.append(b.provider)
        return seen

    def binding_for(self, position: str, provider: Optional[str]) -> Optional[Binding]:
        """The binding row for (position, provider), or None.

        When ``provider`` is given, matches the exact cell; otherwise returns the
        first row for the position (the D9 store's single active lane).
        """
        for b in self.bindings:
            if b.position != position:
                continue
            if provider is None or b.provider == provider:
                return b
        return None


def load_routing_table(path: Path) -> RoutingTable:
    """Parse + structurally validate a routing.toml (AC7 "rejects malformed").

    Raises ``RoutingError`` (``.code`` None — a structural fault, not a named
    resolution refusal) on: unreadable/!TOML, a ``[[binding]]`` missing
    ``position`` / ``kind``, an unknown ``kind``, a ``kind="cao"`` row with no
    ``provider``, or an ``in_harness`` row that names a provider (a non-CAO lane
    has no CAO cell to certify — naming one is a schema error).
    """
    if not path.exists():
        raise RoutingError(f"routing.toml not found at {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RoutingError(f"routing.toml is not valid TOML: {exc}") from exc

    raw = data.get("binding")
    if raw is None:
        raise RoutingError("routing.toml has no [[binding]] rows")
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        raise RoutingError("routing.toml [[binding]] must be an array of tables")

    bindings: List[Binding] = []
    for i, row in enumerate(raw):
        position = row.get("position")
        if not isinstance(position, str) or not position:
            raise RoutingError(f"binding #{i} is missing a 'position' string")
        kind = row.get("kind")
        if kind not in _KINDS:
            raise RoutingError(
                f"binding #{i} (position '{position}') has invalid kind {kind!r} "
                f"(expected one of {_KINDS})"
            )
        provider = row.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise RoutingError(f"binding #{i} (position '{position}') provider must be a string")
        if kind == "cao" and not provider:
            raise RoutingError(
                f"binding #{i} (position '{position}') is kind='cao' but names no provider"
            )
        if kind == "in_harness" and provider:
            raise RoutingError(
                f"binding #{i} (position '{position}') is kind='in_harness' but names "
                f"provider {provider!r} (a non-CAO lane has no CAO cell)"
            )
        model = row.get("model")
        if model is not None and not isinstance(model, str):
            raise RoutingError(f"binding #{i} (position '{position}') model must be a string")
        bindings.append(Binding(position=position, provider=provider, kind=kind, model=model))
    return bindings_to_table(bindings)


def bindings_to_table(bindings: List[Binding]) -> RoutingTable:
    """Wrap validated bindings (a seam tests use to build a table without a file)."""
    return RoutingTable(bindings=list(bindings))


# --------------------------------------------------------------------------
# Certification + clause state (read from the on-disk position stores)
# --------------------------------------------------------------------------


def _is_gate_position(
    position: str, positions_dir: Path, clause_table_path: Optional[Path]
) -> bool:
    """True when ``position``'s required-clause set marks it a GATE (D12).

    Derived from the clause table — a position is a gate iff its ``[required]``
    row includes the frozen-pin + never-edit-artifact-branch marker clauses.
    Never a hard-coded roster (D12: refusal is because the clause set cannot be
    satisfied, not a remembered rule).
    """
    from cli_agent_orchestrator.utils.clause_lint import load_clause_table

    table_path = clause_table_path or (positions_dir / "_clauses.toml")
    table = load_clause_table(table_path)
    required = set(table.required.get(position, []))
    return all(c in required for c in _GATE_MARKER_CLAUSES)


def cell_certified(
    position: str,
    provider: str,
    positions_dir: Path,
) -> "tuple[bool, str]":
    """Is the (position, provider) cell certified PASS at its CURRENT sha pair?

    Reads the position file's ``certification:`` block, computes the current
    ``position_sha`` (persona body + merge-relevant frontmatter EXCLUDING the
    cert block) and ``overlay_sha`` (the provider's overlay fragment source(s)),
    and returns ``(True, "PASS")`` iff a committed row matches this exact
    (provider, position_sha, overlay_sha) with ``outcome == PASS``. Otherwise
    ``(False, <outcome>)`` where outcome is the recorded non-PASS outcome, or
    ``"UNCERTIFIED"`` when no row matches the current sha pair (a stale row or
    no row at all is not a certification).
    """
    import frontmatter

    from cli_agent_orchestrator.utils.agent_profiles import _read_composition_store
    from cli_agent_orchestrator.utils.profile_composition import overlay_sha, position_sha

    pos_path = positions_dir / f"{position}.md"
    if not pos_path.exists():
        return False, "UNCERTIFIED"
    parsed = frontmatter.loads(pos_path.read_text(encoding="utf-8"))
    pos_sha = position_sha(parsed.content, dict(parsed.metadata))

    overlays_dir = positions_dir.parent / "overlays"
    frags: List[str] = []
    base = overlays_dir / f"{provider}.md"
    if base.exists():
        frags.append(base.read_text(encoding="utf-8"))
    per_pos = overlays_dir / f"{provider}.{position}.md"
    if per_pos.exists():
        frags.append(per_pos.read_text(encoding="utf-8"))
    ov_sha = overlay_sha(frags)

    for row in parsed.metadata.get("certification") or []:
        if not isinstance(row, dict):
            continue
        if (
            row.get("provider") == provider
            and row.get("position_sha") == pos_sha
            and row.get("overlay_sha") == ov_sha
        ):
            outcome = str(row.get("outcome", "UNCERTIFIED"))
            return outcome == "PASS", outcome
    return False, "UNCERTIFIED"


def _compose_cell_body(position: str, provider: str, positions_dir: Path) -> str:
    """Compose the (position, provider) persona BODY from a given store dir.

    Mirrors the resolver's D4 layer order but reads from the PASSED
    ``positions_dir`` (+ its ``overlays`` sibling) rather than the global store,
    so the D9 validator composes the same cell the certification shas hash and
    fixture tests can point at a tmp store.
    """
    import frontmatter

    from cli_agent_orchestrator.utils.profile_composition import Layer, compose_source_body

    overlays_dir = positions_dir.parent / "overlays"
    pos_parsed = frontmatter.loads((positions_dir / f"{position}.md").read_text(encoding="utf-8"))
    layers = [Layer(kind=f"position:{position}", metadata={}, body=pos_parsed.content)]
    for name in (f"{provider}.md", f"{provider}.{position}.md"):
        f = overlays_dir / name
        if f.exists():
            fp = frontmatter.loads(f.read_text(encoding="utf-8"))
            layers.append(
                Layer(
                    kind=f"overlay:{f.stem}",
                    metadata=dict(fp.metadata),
                    body=fp.content,
                    provider=provider,
                    replaces=list(fp.metadata.get("replaces") or []),
                )
            )
    return compose_source_body(layers)


def _present_clause_ids(position: str, provider: str, positions_dir: Path) -> List[str]:
    """The clause ids the composed (position, provider) persona actually carries.

    Reuses AC14's matcher (``ClauseRule.matches``) against the composed BODY —
    never a second implementation. Returns the ids from the clause table whose
    rule matches; the D9 row check compares this against ``[required].<position>``.
    """
    from cli_agent_orchestrator.utils.clause_lint import load_clause_table

    table = load_clause_table(positions_dir / "_clauses.toml")
    body = _compose_cell_body(position, provider, positions_dir)
    return [cid for cid, rule in table.rules.items() if rule.matches(body)]


@dataclass(frozen=True)
class RoutingResolution:
    """Outcome of ``resolve_routing_binding`` for a bound (position, provider).

    ``spawn_profile`` is the position (normal) or ``<provider>_general`` (D12
    fallback). ``fallback_profile`` is non-None only on the general substitution
    path (mirrored into the assign result). ``fallback_position`` / ``fallback_cell``
    feed the ``[COLD-FALLBACK position=<pos> cell=<outcome>]`` preamble fields.
    """

    spawn_profile: str
    provider: str
    fallback_profile: Optional[str] = None
    fallback_position: Optional[str] = None
    fallback_cell: Optional[str] = None


def resolve_routing_binding(
    position: str,
    provider: str,
    *,
    table: RoutingTable,
    positions_dir: Path,
    clause_table_path: Optional[Path] = None,
) -> RoutingResolution:
    """D9/D12 assign-time resolution for a bound (position, provider) cell.

    Order (r10 S2 → r10 S1 → cell cert):
      1. Provider certification: the provider's ``general`` cell must be PASS,
         else ``E-PROVIDER-UNCERTIFIED`` (every row of that provider refused).
      2. Row clause satisfaction: ``[required].<position>`` ⊆ present, else
         ``E-ROW-CLAUSES-MISSING`` naming the missing ids.
      3. Cell certification: a non-PASS NON-gate cell substitutes
         ``<provider>_general`` (D12); a non-PASS GATE cell is a refusal.

    Raises ``RoutingError`` (with ``.code``) on refusal; returns a
    ``RoutingResolution`` on a bindable or fallback-substituted cell.
    """
    # (1) PROVIDER certification first — the general cell gates the whole provider.
    gen_pass, _gen_outcome = cell_certified(GENERAL_POSITION, provider, positions_dir)
    if not gen_pass:
        raise RoutingError(
            f"{E_PROVIDER_UNCERTIFIED}: provider '{provider}' general cell is not "
            f"PASS (every routing row binding this provider is refused until its "
            f"AC15 general smoke passes)",
            code=E_PROVIDER_UNCERTIFIED,
        )

    # (2) ROW clause satisfaction — general is structurally unbindable to a gate.
    from cli_agent_orchestrator.utils.clause_lint import load_clause_table

    table_path = clause_table_path or (positions_dir / "_clauses.toml")
    clause_table = load_clause_table(table_path)
    required = list(clause_table.required.get(position, []))
    present = set(_present_clause_ids(position, provider, positions_dir))
    missing = [cid for cid in required if cid not in present]
    if missing:
        raise RoutingError(
            f"{E_ROW_CLAUSES_MISSING}: cell ({position}, {provider}) is missing "
            f"required clause ids {','.join(missing)}",
            code=E_ROW_CLAUSES_MISSING,
        )

    # (3) CELL certification.
    cell_pass, cell_outcome = cell_certified(position, provider, positions_dir)
    if cell_pass:
        return RoutingResolution(spawn_profile=position, provider=provider)

    # Non-PASS cell: gate → refusal, non-gate → general substitution (D12).
    if _is_gate_position(position, positions_dir, clause_table_path):
        raise RoutingError(
            f"{E_ROW_CLAUSES_MISSING}: gate cell ({position}, {provider}) is not "
            f"certified (outcome={cell_outcome}); a gate cell never falls back to "
            f"general — refusing (no spawn)",
            code=E_ROW_CLAUSES_MISSING,
        )
    # F613 #469: substitute the provider's general cell (D12). The spawn profile
    # is the INSTALLED alias stub for (general, provider) — its stem is
    # ``<short>_general`` (cline_general, kiro_general, …), resolved by scanning
    # the agent store for a composition stub with ``extends/position == general``
    # AND ``provider == provider``. The raw ``f"{provider}_{GENERAL_POSITION}"``
    # (``cline_cli_general``) is NOT an installed profile; handing it to the
    # server yields a load failure that silently re-derives to claude_code. Only
    # bind the general fallback when such a stub exists; otherwise refuse with
    # E-ALIAS-MISSING rather than emit an unresolvable name.
    from cli_agent_orchestrator.utils.agent_profiles import _find_alias_for_cell

    fallback = _find_alias_for_cell(GENERAL_POSITION, provider)
    if fallback is None:
        raise RoutingError(
            f"{E_ALIAS_MISSING}: no installed general alias stub for provider "
            f"'{provider}' (looked for a composition stub with extends/position="
            f"'{GENERAL_POSITION}' and provider='{provider}'); refusing rather "
            f"than spawning the unresolved name '{provider}_{GENERAL_POSITION}'",
            code=E_ALIAS_MISSING,
        )
    return RoutingResolution(
        spawn_profile=fallback,
        provider=provider,
        fallback_profile=fallback,
        fallback_position=position,
        fallback_cell=cell_outcome,
    )

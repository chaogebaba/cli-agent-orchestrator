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
       * NON-GATE position → spawn the position's OWN composed profile
         ``<position>-<provider>`` (F870 #726). The cross-position
         ``general-<provider>`` substitution is DELETED — a non-PASS non-gate
         cell never silently runs as another position. ``uncertified_cell`` + a
         ``[COLD-FALLBACK position=<pos> cell=<outcome>]`` preamble field surface
         that the cell is not smoke-certified; the server seam refuses
         ``E-COMPOSITION-MISSING`` (naming the composed profile) when the D8
         writer cannot materialise it.
       * GATE position → REFUSAL, no spawn (an uncertified gate cell is a
         refusal, never a substitution).

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
# F868 #724 — an explicit ``provider=`` override on a position-name assign names
# a SPECIFIC cell; if that cell is not certified PASS (or the provider is not in
# the position allowlist) the operator override is refused, never silently
# downgraded to general. ONE typed code carries position + provider + the
# certified cells so the operator can pick a bindable cell.
E_CELL_UNCERTIFIED = "E-CELL-UNCERTIFIED"
# F870 #726 — a position-name assign resolved to its OWN ``<position>-<provider>``
# composition that cannot be materialised (bad overlay, provider not in the
# position allowlist) is refused NAMING the composed profile it looked for —
# never silently substituting another position's profile (the deleted
# cross-position ``general-<provider>`` fallback).
E_COMPOSITION_MISSING = "E-COMPOSITION-MISSING"
# F613 #469: the non-gate general fallback resolves the installed alias stub for
# (general, provider) — its stem is ``<short>_general`` (e.g. ``cline_general``),
# NOT the raw ``<provider>_general`` f-string (``cline_cli_general``), which is
# not an installed profile and would be handed to the server as an unknown name.
# F786 D11 RETIRES this refusal: the non-gate general fallback now DERIVES
# ``general-<provider>`` purely and the D8 writer materialises it, so no stub
# scan and no E-ALIAS-MISSING path remain. The constant is kept (stable code,
# still imported by tests asserting the old arm is gone) but is never raised.
E_ALIAS_MISSING = "E-ALIAS-MISSING"
# WP-HERDR D9 — the BACKEND axis. herdr is a backend, not a provider, so a cell
# being certified for its provider says nothing about whether that cell has been
# proven under the herdr runtime. A routing row naming ``backend = "herdr"`` is
# refused unless the position file carries a PASS ``herdr_certification`` row for
# the bound provider at the CURRENT sha pair AND the recorded ``herdr_sha256``
# matches the herdr binary actually installed (D7's pin). Certification, not the
# backend setting, is what moves a terminal onto the herdr lifecycle source.
E_BACKEND_UNCERTIFIED = "E-BACKEND-UNCERTIFIED"

# Valid ``kind`` discriminator values (D9).
_KINDS = ("cao", "in_harness")

# Valid ``backend`` values (WP-HERDR D9). ``tmux`` is the default and the
# pre-herdr behaviour; a row omitting the key is a tmux row.
_BACKENDS = ("tmux", "herdr")
DEFAULT_BACKEND = "tmux"

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
    #: WP-HERDR D9 — which terminal backend this lane runs on. Defaults to
    #: ``"tmux"``, so every existing routing.toml keeps its exact meaning and the
    #: backend check below is unreachable until a row opts in.
    backend: str = DEFAULT_BACKEND


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
        backend = row.get("backend", DEFAULT_BACKEND)
        if backend not in _BACKENDS:
            raise RoutingError(
                f"binding #{i} (position '{position}') has invalid backend {backend!r} "
                f"(expected one of {_BACKENDS})"
            )
        if backend != DEFAULT_BACKEND and kind != "cao":
            raise RoutingError(
                f"binding #{i} (position '{position}') is kind={kind!r} but names "
                f"backend {backend!r}; a non-CAO lane runs no terminal and has no backend"
            )
        bindings.append(
            Binding(
                position=position,
                provider=provider,
                kind=kind,
                model=model,
                backend=backend,
            )
        )
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


#: The fields a ``herdr_certification`` row carries (WP-HERDR D9). ``provider``
#: plus the two shas are the MATCH key, exactly as the provider block's are;
#: ``herdr_version``/``herdr_sha256``/``protocol`` are D7's binary+protocol pin;
#: the rest is the audit trail a reviewer reads.
HERDR_CERT_FIELDS = (
    "provider",
    "herdr_version",
    "herdr_sha256",
    "protocol",
    "position_sha",
    "overlay_sha",
    "outcome",
    "date",
    "evidence",
)

#: Returned when the installed herdr binary cannot be found or hashed.
_HERDR_BINARY_UNKNOWN = "BINARY-UNKNOWN"


def installed_herdr_sha256() -> Optional[str]:
    """The sha256 of the ``herdr`` binary on PATH, or ``None`` when there is none.

    D7's pin, which the H1 plan recorded as unimplemented (N9): no herdr version
    or protocol constant existed anywhere in the fork. This is the smallest
    honest implementation — hash the file the backend would actually exec — and
    it is what makes the recorded ``herdr_sha256`` a claim about THIS machine
    rather than a note about the machine the certification ran on.

    ``None`` is a refusal, not a pass: see :func:`herdr_cell_certified`.
    """
    import hashlib
    import shutil

    path = shutil.which("herdr")
    if not path:
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def herdr_cell_certified(
    position: str,
    provider: str,
    positions_dir: Path,
    *,
    installed_sha256: Optional[str] = None,
    resolve_installed: bool = True,
) -> "tuple[bool, str]":
    """Is the (position, provider) cell certified PASS **for the herdr backend**?

    The backend axis of D9, and a strict addition to
    :func:`cell_certified` rather than a variant of it: a cell can be perfectly
    certified for its provider and wholly unproven under herdr, which is the
    normal state of every cell today.

    Match discipline is the provider block's, verbatim — a row counts only when
    it names this ``provider`` at the CURRENT ``position_sha``/``overlay_sha``
    pair, so a stale row is not a certification. On top of that, D7's pin: the
    row's ``herdr_sha256`` must equal the sha256 of the herdr binary installed
    here. Two ways that fails, both refusals rather than passes:

    * the binary is absent or unreadable (``BINARY-UNKNOWN``) — a row certifying
      a binary we cannot see is not evidence about this machine;
    * the binary is present and DIFFERENT (``BINARY-MISMATCH``) — the certified
      runtime is not the running one, which is the whole reason the pin exists.

    ``installed_sha256`` injects the answer (tests, and a caller that already
    hashed it); ``resolve_installed=False`` skips the pin entirely and is for
    callers that only want the sha-pair question answered.
    """
    import frontmatter

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

    for row in parsed.metadata.get("herdr_certification") or []:
        if not isinstance(row, dict):
            continue
        if (
            row.get("provider") == provider
            and row.get("position_sha") == pos_sha
            and row.get("overlay_sha") == ov_sha
        ):
            outcome = str(row.get("outcome", "UNCERTIFIED"))
            if outcome != "PASS":
                return False, outcome
            if not resolve_installed:
                return True, outcome
            live = installed_sha256 if installed_sha256 is not None else installed_herdr_sha256()
            if live is None:
                return False, _HERDR_BINARY_UNKNOWN
            if str(row.get("herdr_sha256") or "") != live:
                return False, "BINARY-MISMATCH"
            return True, outcome
    return False, "UNCERTIFIED"


def certified_cells_for_position(position: str, positions_dir: Path) -> List[str]:
    """The providers whose (position, provider) cell is certified PASS at the
    CURRENT sha pair — the operator-facing candidate list for a refused cell
    (F868 #724 ``E-CELL-UNCERTIFIED``). Returns sorted provider names; empty when
    no cell is certified. Never raises: an unreadable/absent position yields ``[]``.
    """
    import frontmatter

    pos_path = positions_dir / f"{position}.md"
    if not pos_path.exists():
        return []
    try:
        parsed = frontmatter.loads(pos_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    providers = {
        str(row["provider"])
        for row in (parsed.metadata.get("certification") or [])
        if isinstance(row, dict) and row.get("provider")
    }
    return sorted(p for p in providers if cell_certified(position, p, positions_dir)[0])


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

    ``spawn_profile`` is ALWAYS the position's OWN composed cell
    ``<position>-<provider>`` (F786 D2c). F870 #726 DELETES the cross-position
    ``general-<provider>`` substitution a non-PASS non-gate cell used to make:
    a non-PASS non-gate cell now spawns its OWN composition, and the server seam
    refuses ``E-COMPOSITION-MISSING`` (naming that composed profile) if it cannot
    be materialised — never another position's profile.

    ``uncertified_cell`` is True when the cell's AC15 row is not PASS (the
    non-gate arm that used to cross-substitute): the spawn is the SAME position's
    composition, and ``fallback_position`` / ``fallback_cell`` still feed the
    operator-visible ``[COLD-FALLBACK position=<pos> cell=<outcome>]`` preamble.
    ``fallback_profile`` is retained for the assign-result field but now names
    the SAME-position composed profile (never a cross-position substitute).
    """

    spawn_profile: str
    provider: str
    fallback_profile: Optional[str] = None
    fallback_position: Optional[str] = None
    fallback_cell: Optional[str] = None
    uncertified_cell: bool = False
    #: WP-HERDR D9 — the bound row's backend, and whether this cell is certified
    #: for it. ``herdr_certified`` is the runtime predicate §6(ii) and slice 3
    #: read, resolved ONCE here at terminal create and stored on the terminal
    #: record; §8 forbids re-deriving it mid-occupant, because that would move a
    #: live worker between truth sources halfway through a turn.
    backend: str = DEFAULT_BACKEND
    herdr_certified: bool = False


def resolve_routing_binding(
    position: str,
    provider: str,
    *,
    table: RoutingTable,
    positions_dir: Path,
    clause_table_path: Optional[Path] = None,
) -> RoutingResolution:
    """D9/D12 assign-time resolution for a bound (position, provider) cell.

    Order (r10 S2 → r10 S1 → cell cert → backend cert):
      1. Provider certification: the provider's ``general`` cell must be PASS,
         else ``E-PROVIDER-UNCERTIFIED`` (every row of that provider refused).
      2. Row clause satisfaction: ``[required].<position>`` ⊆ present, else
         ``E-ROW-CLAUSES-MISSING`` naming the missing ids.
      3. Cell certification: a non-PASS GATE cell is a refusal; a non-PASS
         NON-gate cell spawns its OWN ``<position>-<provider>`` composition
         (F870 #726 — the cross-position ``general-<provider>`` substitution is
         deleted) with ``uncertified_cell=True``.
      4. BACKEND certification (WP-HERDR D9): a row naming ``backend="herdr"``
         is refused ``E-BACKEND-UNCERTIFIED`` unless the cell carries a PASS
         ``herdr_certification`` row at the current sha pair whose recorded
         ``herdr_sha256`` matches the installed binary. A row on the default
         ``tmux`` backend never reaches this check.

    Raises ``RoutingError`` (with ``.code``) on refusal; returns a
    ``RoutingResolution`` on a bindable or same-position uncertified cell.
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
    # F786 D12b — a MISSING [required] row fails closed here (mirrors the
    # lint-time check clause_lint._position_required_ids), rather than the old
    # silent ``.get(position, [])`` that treated an absent row as an empty (i.e.
    # non-gate) clause set. A future rename cannot silently demote a gate.
    if position not in clause_table.required:
        raise RoutingError(
            f"{E_ROW_CLAUSES_MISSING}: cell ({position}, {provider}) has no "
            f"[required] row in the clause table (fail-closed: a position's first "
            f"commit must add its row) — {table_path}",
            code=E_ROW_CLAUSES_MISSING,
        )
    required = list(clause_table.required[position])
    present = set(_present_clause_ids(position, provider, positions_dir))
    missing = [cid for cid in required if cid not in present]
    if missing:
        raise RoutingError(
            f"{E_ROW_CLAUSES_MISSING}: cell ({position}, {provider}) is missing "
            f"required clause ids {','.join(missing)}",
            code=E_ROW_CLAUSES_MISSING,
        )

    # (3) CELL certification.
    from cli_agent_orchestrator.utils.agent_profiles import (
        _synthesise_position_profile_name,
    )

    cell_pass, cell_outcome = cell_certified(position, provider, positions_dir)
    own_cell = _synthesise_position_profile_name(position, provider)

    # (4) BACKEND certification (WP-HERDR D9). Fourth and last, so a backend
    # refusal is only ever reported for a row that already cleared provider
    # cert, row clauses and — for a gate — cell cert. A row that does not name
    # ``backend = "herdr"`` never reaches the check at all, which is what keeps
    # every existing routing.toml byte-identical in meaning.
    bound_row = table.binding_for(position, provider)
    backend = bound_row.backend if bound_row is not None else DEFAULT_BACKEND
    herdr_certified = False
    if backend == "herdr":
        herdr_pass, herdr_outcome = herdr_cell_certified(position, provider, positions_dir)
        if not herdr_pass:
            raise RoutingError(
                f"{E_BACKEND_UNCERTIFIED}: cell ({position}, {provider}) is bound to "
                f"backend 'herdr' but has no PASS herdr_certification row at the "
                f"current sha pair with a matching installed binary "
                f"(outcome={herdr_outcome}) — refusing (no spawn)",
                code=E_BACKEND_UNCERTIFIED,
            )
        herdr_certified = True

    if cell_pass:
        # F786 D2c — a certified cell resolves to the effective composed name
        # ``<position>-<provider>`` (was the bare position), which the D8 writer
        # materialises and the spawn loads. The bare-position emission is gone,
        # which is why the flat ``secretary.md`` becomes dead (D3 deletes it).
        return RoutingResolution(
            spawn_profile=own_cell,
            provider=provider,
            backend=backend,
            herdr_certified=herdr_certified,
        )

    # Non-PASS cell: gate → refusal (a gate cell is never spawned uncertified).
    if _is_gate_position(position, positions_dir, clause_table_path):
        raise RoutingError(
            f"{E_ROW_CLAUSES_MISSING}: gate cell ({position}, {provider}) is not "
            f"certified (outcome={cell_outcome}); a gate cell never falls back to "
            f"general — refusing (no spawn)",
            code=E_ROW_CLAUSES_MISSING,
        )

    # F870 #726 — a non-PASS NON-gate cell spawns its OWN composed profile
    # ``<position>-<provider>`` (SAME position), NOT the deleted cross-position
    # ``general-<provider>`` substitution that silently ran ``assign("dev")`` as
    # the general overlay. The provider itself is certified (step 1), the row
    # clauses are satisfied (step 2), and the composition is materialisable by
    # the D8 writer the server seam invokes; only its AC15 smoke row is not PASS.
    # We surface that via ``uncertified_cell`` + the ``fallback_position`` /
    # ``fallback_cell`` preamble fields so the operator sees the cell is not
    # smoke-certified, but the worker still runs under its OWN position's persona.
    # If the server seam's D8 writer cannot materialise ``own_cell`` (bad overlay,
    # provider not in the allowlist), it refuses ``E-COMPOSITION-MISSING`` — it
    # never substitutes another position's profile.
    return RoutingResolution(
        spawn_profile=own_cell,
        provider=provider,
        fallback_profile=own_cell,
        fallback_position=position,
        fallback_cell=cell_outcome,
        uncertified_cell=True,
        backend=backend,
        herdr_certified=herdr_certified,
    )

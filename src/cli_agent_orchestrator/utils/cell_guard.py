"""F868 #724 + F870 #726 r2 — the SINGLE cell-certification choke point (D4).

Every terminal-creating path must run the SAME (position, provider) cell
admission BEFORE any provider process starts. Before r2 the check lived inline
in ``mcp_server._assign_impl`` only, so ``handoff`` (→ ``/terminals/run-step`` →
``run_agent_step``), the three public HTTP create routes (``POST /sessions``,
``POST /sessions/start``, ``POST /sessions/{s}/terminals``) and the resume path
all reached ``terminal_service.create_terminal`` without it (codex r1 blockers
B2/B3/B4). r2 extracts the logic into :func:`guard_cell_admission` here and calls
it from the ONE seam every path funnels through — ``terminal_service.create_terminal``
— plus keeps the MCP ``_assign_impl`` adapter (typed assign-result envelopes)
over the same function so nothing changes for the caller. A mutant that removes
the call from ANY one path is killed by that path's committed test.

Classification (D5), decided HERE from what the caller supplied:

  * ``ROUTING`` — a bare position name, no explicit ``provider=``: the routing
    binding chose the cell. A non-PASS NON-gate cell is ALLOWED (spawns its own
    ``<position>-<provider>`` composition with the uncertified marker, F870); a
    non-PASS GATE cell is REFUSED. This is the fleet-authority path.
  * ``EXPLICIT`` — an operator named the cell: a bare position + ``provider=``,
    OR a composed literal ``<position>-<provider>`` (parsed by
    :func:`split_effective_name`) that names a known position and an
    allowlisted provider. It MUST be PASS-certified; anything else — uncertified
    non-gate, uncertified gate, provider not in the allowlist, or any
    RoutingError stage — collapses to the ONE typed ``E-CELL-UNCERTIFIED``
    (naming position, provider, and the currently-certified cells). The
    allowlist check runs INSIDE this function AFTER position parsing, so a
    disallowed provider yields ``E-CELL-UNCERTIFIED`` too — never
    ``E-PROVIDER-NOT-ALLOWED`` for a position request (closes codex r1 B1).
  * ``RESUME`` — a continuation of a prior spawn: re-classified as
    ROUTING-EQUIVALENT (non-gate uncertified allowed with the marker, gate
    uncertified refused). A caller-supplied bare-position OVERRIDE on resume is
    the caller's ``EXPLICIT`` choice and is treated as such by the entry point.
  * ``LEGACY`` — a non-position, non-composed-literal name: untouched (no cell,
    no check).

This module is a PURE library over the routing.toml path + on-disk position
stores; it imports no service/client. ``terminal_service`` catches
:class:`CellGuardRefused` and re-raises it as its own typed error; the MCP layer
renders it into the assign-result envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Request classes (D5). Kept as plain strings so they cross the MCP/HTTP wire
# and the create-route payload without a shared enum import on the client side.
CLASS_ROUTING = "routing"
CLASS_EXPLICIT = "explicit"
CLASS_RESUME = "resume"
CLASS_LEGACY = "legacy"
_VALID_CLASSES = (CLASS_ROUTING, CLASS_EXPLICIT, CLASS_RESUME, CLASS_LEGACY)

# F868 #724 r4 (codex Stage B r2 = EMPIRICAL-GATE-NO) — the request class is a
# SERVER-SIDE property of the request shape (position vs legacy vs composed
# literal, plus resume/override), NOT a value a public caller may assert. Before
# r4 the HTTP create surfaces trusted a ``cell_request_class`` query parameter:
# a caller could send a POSITION name with ``cell_request_class=legacy`` and the
# guard returned its legacy passthrough BEFORE position parsing, so the position
# reached ``create_terminal`` with no ``resolve_routing_binding`` and no
# certification check (same on a resume create). r4 derives the class here and
# REFUSES any caller-supplied class that disagrees with the derived one — a
# forged class can never enter the ``legacy`` (or any weaker) passthrough.
E_CELL_CLASS_FORGED = "E-CELL-CLASS-FORGED"

# F862 (#718) D14 r6 — the findings-only provider's own typed refusal, raised
# ONLY by :func:`_guard_findings_only_provider` below. It is deliberately a
# DISTINCT code from ``E-CELL-UNCERTIFIED``: r5 measured that F868's refusal and
# F862's shared the operator-facing prefix ``"Assignment refused (no spawn): "``,
# so a test asserting on that prefix passed with the D14 check DELETED — F868's
# refusal masked it. Every D14 assertion matches THIS code, never the prefix, so
# a mutant that removes the check below is killed by an unambiguous signal.
E_CHATGPT_WEB_GATE_FORBIDDEN = "E-CHATGPT-WEB-GATE-FORBIDDEN"

# The provider whose cell admission is unconditional (D14). Kept as a literal
# rather than importing ProviderType so this module stays a pure library over
# routing.toml + the position stores (see the module docstring).
_FINDINGS_ONLY_PROVIDER = "chatgpt_web"


class CellClassForged(Exception):
    """A caller-supplied ``cell_request_class`` disagreed with the class the
    server derives from the request shape (F868 #724 r4).

    Raised at the HTTP boundary BEFORE any terminal is created or any resume
    claim is taken, so a forged class produces a typed refusal and zero spawn.
    Carries the stable ``.code`` (``E-CELL-CLASS-FORGED``), the ``.derived`` and
    ``.supplied`` classes, and an operator-facing ``.message``.
    """

    def __init__(self, derived: str, supplied: str, message: str):
        super().__init__(message)
        self.code = E_CELL_CLASS_FORGED
        self.derived = derived
        self.supplied = supplied
        self.message = message


class CellGuardRefused(Exception):
    """A (position, provider) cell was refused admission at the choke point.

    Carries a stable ``.code`` (one of the routing/agent_profiles error codes,
    normally ``E-CELL-UNCERTIFIED`` or ``E-COMPOSITION-MISSING``) so every
    surface renders the same typed refusal. ``.message`` is the operator-facing
    text (already names position/provider/certified cells where relevant).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CellGuardOutcome:
    """The verdict for an ADMITTED create.

    ``is_position_cell`` is False for a legacy passthrough (no cell). For a
    position cell, ``position`` / ``provider`` are the parsed pair,
    ``uncertified_cell`` mirrors the resolver's non-PASS-non-gate marker (the
    routing/resume-equivalent path that is allowed to run its own composition),
    and ``spawn_profile`` is the effective ``<position>-<provider>`` name the
    resolver produced (or the input name when already composed).
    """

    is_position_cell: bool
    position: Optional[str] = None
    provider: Optional[str] = None
    spawn_profile: Optional[str] = None
    uncertified_cell: bool = False


def _certified_cells(position: str, positions_dir: Path) -> List[str]:
    from cli_agent_orchestrator.utils.routing import certified_cells_for_position

    try:
        return certified_cells_for_position(position, positions_dir)
    except Exception:
        return []


def _uncertified_refusal(
    position: str, provider: str, positions_dir: Path, detail: str
) -> CellGuardRefused:
    """Build the ONE typed ``E-CELL-UNCERTIFIED`` refusal (names certified cells)."""
    from cli_agent_orchestrator.utils.routing import E_CELL_UNCERTIFIED

    cells = _certified_cells(position, positions_dir)
    cells_txt = ", ".join(cells) if cells else "none"
    return CellGuardRefused(
        E_CELL_UNCERTIFIED,
        (
            f"{E_CELL_UNCERTIFIED}: position '{position}' provider '{provider}' "
            f"is not a certified cell (certified cells: {cells_txt}). {detail}"
        ),
    )


def _classify_cell(
    agent_profile: str,
    provider: Optional[str],
) -> "Optional[tuple[str, str]]":
    """Return ``(position, provider)`` when ``agent_profile`` denotes a position
    CELL, else ``None`` (a legacy name — no cell).

    A cell is either a bare position file (``positions/<name>.md`` exists) or a
    composed literal ``<position>-<provider>`` whose position half is a real
    position file. The provider used for the check is the RESOLVED ``provider``
    argument when given (routing/explicit), else the composed literal's own
    provider token.
    """
    from cli_agent_orchestrator.utils.agent_profiles import (
        _position_exists,
        split_effective_name,
    )

    # A non-str / empty name carries no cell (e.g. an operator-launched terminal
    # with agent_profile=None). Guard the type here so classification never
    # raises on a legacy/None name.
    if not isinstance(agent_profile, str) or not agent_profile:
        return None

    # Bare position file → cell keyed on the resolved provider.
    if _position_exists(agent_profile):
        if provider:
            return agent_profile, provider
        return None  # underspecified; resolution layer already handled provider

    # Composed literal <position>-<provider> → cell keyed on the parsed pair
    # (the resolved provider must agree when both are present; the literal's own
    # provider is authoritative for the position half).
    parsed = split_effective_name(agent_profile)
    if parsed is not None:
        position, literal_provider = parsed
        if _position_exists(position):
            return position, provider or literal_provider

    return None


def _guard_findings_only_provider(
    position: str,
    provider: str,
    positions_dir: Path,
    rt_path: Path,
) -> None:
    """F862 (#718) D14 r6 — the UNCONDITIONAL cell check for ``chatgpt_web``.

    D14: "make the cell check unconditional for this provider at the dispatch
    guard". r3 to r5 implemented that client-side in ``mcp_server._assign_impl``,
    which left two holes r5 measured on grok-box-005:

    * the RESUME arm was dead. ``origin/main``'s F829 A2.1 moved resume
      resolution to the server, so the client-side ``_resume_prepared`` marker
      carries no provider and the check was skipped on every ``resume_from``;
    * the EXPLICIT arm was redundant, fully masked by F868's own refusal.

    Moving the check HERE — inside the one choke point every create path funnels
    through, ``terminal_service.create_terminal`` — fixes both: the resume create
    reaches this function with a real provider, and the refusal carries its own
    typed code so it is distinguishable from F868's.

    The rule this enforces, which is STRICTLY STRONGER than F868's D5 asymmetry
    and applies to EVERY request class: this provider never runs an uncertified
    or unresolvable cell. F870's routing/resume concession — a non-PASS NON-gate
    cell may spawn the position's own composition with an uncertified marker — is
    withdrawn for this provider alone. A findings lane whose product is a claim
    about pinned bytes must not launch a browser on a cell nobody certified.

    Per D14 this is a per-provider tightening, never a global routing rewrite:
    the function returns immediately for every other provider, so no other
    provider's admission changes by a single branch.
    """
    from cli_agent_orchestrator.utils.routing import (
        RoutingError,
        load_routing_table,
        resolve_routing_binding,
    )

    if provider != _FINDINGS_ONLY_PROVIDER:
        return

    def _refuse(detail: str) -> CellGuardRefused:
        cells = _certified_cells(position, positions_dir)
        cells_txt = ", ".join(cells) if cells else "none"
        return CellGuardRefused(
            E_CHATGPT_WEB_GATE_FORBIDDEN,
            (
                f"{E_CHATGPT_WEB_GATE_FORBIDDEN}: provider "
                f"'{_FINDINGS_ONLY_PROVIDER}' may only run a CERTIFIED cell of "
                f"position '{position}' (certified cells: {cells_txt}); this is "
                f"unconditional on every create path including resume (F862 D14). "
                f"{detail}"
            ),
        )

    try:
        res = resolve_routing_binding(
            position,
            provider,
            table=load_routing_table(rt_path),
            positions_dir=positions_dir,
        )
    except RoutingError as exc:
        # Includes the uncertified/stale GATE refusal at routing.py:415-421 —
        # which is what a design_findings cell hits under D15 — and every
        # provider-cert / row-clause stage before it.
        raise _refuse(str(exc)) from exc

    if res.uncertified_cell:
        # F870's own-composition concession, withdrawn for this provider.
        raise _refuse(f"cell outcome={res.fallback_cell} is not certified")


def guard_cell_admission(
    agent_profile: str,
    provider: Optional[str],
    *,
    request_class: str,
    positions_dir: Optional[Path] = None,
    routing_toml_path_override: Optional[Path] = None,
) -> CellGuardOutcome:
    """The single cell-certification choke point (D4/D5).

    Runs the SAME provider-cert / row-clause / cell-cert admission for every
    create path. Raises :class:`CellGuardRefused` (typed ``.code``) on a refused
    cell; returns a :class:`CellGuardOutcome` for a legacy passthrough or an
    admitted cell (including the routing/resume-equivalent non-gate uncertified
    spawn, which is admitted WITH ``uncertified_cell=True``).

    ``request_class`` selects the D5 asymmetry (see the module docstring). An
    unknown class is treated as EXPLICIT (fail-closed: the strictest arm).
    """
    from cli_agent_orchestrator.constants import positions_store_dir, routing_toml_path
    from cli_agent_orchestrator.utils.routing import (
        RoutingError,
        RoutingResolution,
        load_routing_table,
        resolve_routing_binding,
    )

    if request_class not in _VALID_CLASSES:
        request_class = CLASS_EXPLICIT

    pos_dir = positions_dir or positions_store_dir()

    # LEGACY names and underspecified passthroughs carry no cell — no check.
    if request_class == CLASS_LEGACY:
        return CellGuardOutcome(is_position_cell=False)

    cell = _classify_cell(agent_profile, provider)
    if cell is None:
        return CellGuardOutcome(is_position_cell=False)
    position, cell_provider = cell

    # EXPLICIT: the operator named this exact cell, so the position allowlist is
    # enforced HERE (inside the choke point, AFTER position parsing) and a
    # disallowed provider collapses to the ONE E-CELL-UNCERTIFIED — never
    # E-PROVIDER-NOT-ALLOWED for a position request (codex r1 B1).
    is_explicit = request_class == CLASS_EXPLICIT
    if is_explicit and _provider_not_in_allowlist(position, cell_provider, pos_dir):
        raise _uncertified_refusal(
            position,
            cell_provider,
            pos_dir,
            f"provider '{cell_provider}' is not in position '{position}' allowlist",
        )

    rt_path = routing_toml_path_override or routing_toml_path()

    # F862 (#718) D14 r6 — the findings-only provider's UNCONDITIONAL check runs
    # BEFORE the class-dependent admission below, so no request class (routing,
    # explicit, resume, or a fail-closed unknown) can reach the F870 concession
    # with an uncertified cell. No-op for every other provider.
    _guard_findings_only_provider(position, cell_provider, pos_dir, rt_path)

    try:
        table = load_routing_table(rt_path)
        res: RoutingResolution = resolve_routing_binding(
            position,
            cell_provider,
            table=table,
            positions_dir=pos_dir,
        )
    except RoutingError as exc:
        # A routing-driven spawn keeps the specific D9 code (unchanged); an
        # EXPLICIT / (default) fail-closed override collapses to E-CELL-UNCERTIFIED.
        if is_explicit:
            raise _uncertified_refusal(position, cell_provider, pos_dir, str(exc)) from exc
        raise CellGuardRefused(exc.code or "E-ROUTING", str(exc)) from exc

    # A non-PASS NON-gate cell (resolver returned uncertified_cell=True): the
    # ROUTING and RESUME (routing-equivalent) paths run the position's OWN
    # composition with the marker; the EXPLICIT path refuses (the operator named
    # a specific cell, and an uncertified cell is never dispatched explicitly).
    if res.uncertified_cell and is_explicit:
        raise _uncertified_refusal(
            position, cell_provider, pos_dir, f"cell outcome={res.fallback_cell}"
        )

    return CellGuardOutcome(
        is_position_cell=True,
        position=position,
        provider=cell_provider,
        spawn_profile=res.spawn_profile,
        uncertified_cell=res.uncertified_cell,
    )


def _provider_not_in_allowlist(position: str, provider: str, positions_dir: Path) -> bool:
    """True when the position declares a non-empty ``providers:`` allowlist that
    omits ``provider`` (absent/empty allowlist = open). Never raises."""
    from cli_agent_orchestrator.utils.agent_profiles import _read_composition_store

    try:
        pos = _read_composition_store(positions_dir, position, resolve_env=False)
    except Exception:
        return False
    if pos is None:
        return False
    allow = pos[0].get("providers")
    return isinstance(allow, list) and bool(allow) and provider not in allow


def classify_request(
    agent_profile: str,
    *,
    provider_supplied: bool,
    is_resume: bool = False,
    resume_override: bool = False,
) -> str:
    """D5 classification from what an ENTRY POINT (MCP tool or HTTP route) got.

    * ``is_resume`` and NOT ``resume_override`` → ``RESUME`` (routing-equivalent
      continuation of a prior spawn).
    * ``is_resume`` and ``resume_override`` (caller passed a bare-position
      override on resume) → ``EXPLICIT`` (the caller's own cell choice).
    * a composed literal ``<position>-<provider>`` naming a real position →
      ``EXPLICIT`` (the caller named the cell).
    * a bare position file + ``provider_supplied`` → ``EXPLICIT`` (operator
      override).
    * a bare position file, no provider → ``ROUTING`` (routing binding chooses).
    * anything else (a non-position, non-composed-literal name) → ``LEGACY``.

    Fail-closed bias: an ambiguous position-shaped input defaults toward
    EXPLICIT (the strictest arm) rather than LEGACY.
    """
    from cli_agent_orchestrator.utils.agent_profiles import (
        _position_exists,
        split_effective_name,
    )

    if is_resume:
        return CLASS_EXPLICIT if resume_override else CLASS_RESUME

    if _position_exists(agent_profile):
        return CLASS_EXPLICIT if provider_supplied else CLASS_ROUTING

    parsed = split_effective_name(agent_profile)
    if parsed is not None and _position_exists(parsed[0]):
        return CLASS_EXPLICIT

    return CLASS_LEGACY


def reconcile_request_class(
    agent_profile: str,
    *,
    provider_supplied: bool,
    is_resume: bool = False,
    resume_override: bool = False,
    supplied_class: Optional[str] = None,
) -> str:
    """Derive the D5 request class SERVER-SIDE and refuse a forged override
    (F868 #724 r4).

    Computes the authoritative class from the request SHAPE via
    :func:`classify_request`, then:

    * if ``supplied_class`` is ``None`` (no caller value) → return the derived
      class. This is the normal HTTP path — the class is a pure function of the
      request the boundary already parsed.
    * if ``supplied_class`` EQUALS the derived class → return it (the trusted
      MCP ``_create_terminal`` client already classified identically; its value
      is accepted because it agrees).
    * otherwise → raise :class:`CellClassForged`. A public caller cannot label a
      POSITION request ``legacy`` (or any class weaker than the shape implies)
      to slip past the certification choke point.

    An unknown ``supplied_class`` (outside :data:`_VALID_CLASSES`) is treated as
    a disagreement unless it happens to equal the derived class, so garbage
    values are refused rather than silently coerced.

    The refusal is raised BEFORE any create call or resume claim, so a forged
    class yields a typed error and zero spawn.
    """
    derived = classify_request(
        agent_profile,
        provider_supplied=provider_supplied,
        is_resume=is_resume,
        resume_override=resume_override,
    )
    if supplied_class is None or supplied_class == derived:
        return derived
    raise CellClassForged(
        derived,
        supplied_class,
        (
            f"{E_CELL_CLASS_FORGED}: caller-supplied cell_request_class "
            f"'{supplied_class}' disagrees with the server-derived class "
            f"'{derived}' for agent_profile '{agent_profile}' — the request "
            "class is derived from the request shape and may not be overridden"
        ),
    )

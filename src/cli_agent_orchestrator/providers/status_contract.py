"""fx751 Slice A — the typed worker-status contract and its pure reducer.

This module is the single home of the *worker-status truth* contract the
blueprint ``orchestrator/blueprints/fx751-worker-status-truth.md`` (D1, D2,
D3, D4) describes. It defines:

* a :class:`StatusSample` envelope — everything one scheduled capture of one
  terminal produced this tick, with provenance (generations, sequence,
  monotonic capture window, native event cursor/coverage, the raw rendered
  frame *and* the separately-filtered pane fingerprint, the process
  observation, and the question/settlement facts);
* the typed *facts* D1 scopes authority to — ``HealthFact``, ``ActivityFact``,
  ``ReadinessFact``, ``SettlementFact``, ``ConditionFact`` — each carrying an
  ``evidence`` pointer back to what established it;
* a :class:`Candidate` — the reducer's proposal: the projected legacy status,
  the facts, the evidence, an optional condition, and a *proposed next
  context*. A ``Candidate`` never mutates anything and performs no I/O;
* :func:`reduce` — the **pure reducer**. It reads a ``StatusSample`` and a
  ``ReducerContext`` and returns a ``Candidate``. It performs no I/O, reads no
  clock (time is an input, carried on the sample), and mutates nothing. The
  monitor alone accepts a ``Candidate``, advances the generation and commits.

**Purity (AC-1).** Nothing here imports ``services``, ``backends``,
``clients`` or any I/O module; the only cross-package import is the legacy
``models.TerminalStatus`` enum this contract projects onto (D1's legacy-enum
projection). ``providers`` is a *legacy* package under the import-linter
contracts, so a new module inside it may import ``models`` freely and be
imported by ``services``/``providers`` without breaching
``new-code-never-imports-legacy`` (which constrains only ``core``/``app``/
``adapters``). The reducer is deliberately kept clock-free and store-free so
its zero-side-effect property is testable with no database, tmux or event loop
(D8) — which is also why it does **not** reuse ``app.worker_truth``'s
DB-backed, ``Clock``-reading projector for its lifecycle context (D2's "do NOT
stand up a parallel worker-truth store", recorded as decision D-A1).

**Migration status.** Only ``pi_cli`` and ``codex`` are migrated in Slice A;
every other provider stays on its legacy adapter and is marked
``MigrationState.LEGACY`` in :data:`MIGRATION_REGISTRY` (AC-10). Fact
*extraction* stays in the provider modules — each migrated provider builds a
``StatusSample`` from its own frame/process/event reads and calls
:func:`reduce`; this module owns only the typed shapes and the precedence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, Optional, Tuple

from cli_agent_orchestrator.models.terminal import TerminalStatus

__all__ = [
    "MigrationState",
    "SampleMode",
    "FactValue",
    "Evidence",
    "ProcessIdentity",
    "ProcessObservation",
    "HealthFact",
    "ActivityFact",
    "ReadinessFact",
    "SettlementFact",
    "ConditionFact",
    "StatusSample",
    "ReducerContext",
    "Candidate",
    "reduce",
    "MIGRATION_REGISTRY",
    "is_migrated",
    "SAMPLE_EXPIRY_S",
    "facts_from_legacy_status",
    "sample_from_legacy_status",
    "derive_status_from_legacy",
]


# The samples-expire budget from D4 / AC-25: a sample older than this (by the
# monotonic delta the *caller* passes on the envelope) is stale and can no
# longer confirm a lowering. Kept here as the contract's own constant so the
# reducer needs no config read (purity).
SAMPLE_EXPIRY_S: float = 10.0


class MigrationState(str, Enum):
    """Whether a provider is driven by the fx751 reducer or its legacy adapter."""

    MIGRATED = "migrated"
    LEGACY = "legacy"


class SampleMode(str, Enum):
    """How the frame on a sample was captured — pins the representation route.

    ``DIRECT_RENDERED`` is pi's route (a rendered pane read); ``SCREEN`` is the
    pyte-composited viewport route codex/grok/claude use; ``RAW`` is the raw
    pipe-pane byte stream. AC-2's representation-routing assertion is that a
    provider is fed the mode it declares and *rejects* a mode it does not — the
    reducer enforces that with :attr:`StatusSample.declared_modes`.
    """

    DIRECT_RENDERED = "direct_rendered"
    SCREEN = "screen"
    RAW = "raw"
    NATIVE = "native"


class FactValue(str, Enum):
    """Tri-state a typed fact can take. ``UNKNOWN`` means *no evidence*, which
    is distinct from a negative finding (``ABSENT``)."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    """A pointer back to what established a fact (D1: 'each typed fact carries
    its evidence pointer').

    ``kind`` is a stable identifier (e.g. ``"working_row"``, ``"native_event"``,
    ``"banner"``, ``"process"``); ``locator`` is a human/debug pointer such as a
    ``file:line`` region tag or an event id; ``detail`` is free-form. Frozen and
    hashable so a ``Candidate`` remains immutable.
    """

    kind: str
    locator: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ProcessIdentity:
    """The launched provider process, identified by ``(boot_id, pid,
    start_ticks)`` (D1/D5). ``None`` on any coordinate means *not established*;
    identity comparison requires all three to match."""

    boot_id: Optional[str] = None
    pid: Optional[int] = None
    start_ticks: Optional[int] = None

    def matches(self, other: "ProcessIdentity | None") -> bool:
        if other is None:
            return False
        if self.pid is None or other.pid is None:
            return False
        return (
            self.boot_id == other.boot_id
            and self.pid == other.pid
            and self.start_ticks == other.start_ticks
        )


@dataclass(frozen=True)
class ProcessObservation:
    """The typed process-observation fact that replaces F899's ambiguous
    ``child_proc_live`` boolean (D7).

    ``complete`` is False when the probe could not fully answer (procfs denied,
    pid gone, partial read) — an *incomplete* probe is never negative proof of
    idleness (D4 Do-NOT). ``owned_tool`` is True only when a foreground tool is
    attributed to the current turn's provider process; a merely-alive shell
    descendant is not ownership (D1: 'a shell or helper's existence or exit is
    never provider health')."""

    identity: ProcessIdentity = field(default_factory=ProcessIdentity)
    provider_alive: FactValue = FactValue.UNKNOWN
    owned_tool: FactValue = FactValue.UNKNOWN
    complete: bool = False
    evidence: Evidence = field(default_factory=lambda: Evidence(kind="process"))


@dataclass(frozen=True)
class HealthFact:
    """Provider *health* (D1): the launched provider process's liveness.

    ``PRESENT`` = provider confirmed running; ``ABSENT`` = verified unexpected
    exit or a verified fatal init failure (this projects ERROR); ``UNKNOWN`` =
    not established. A banner alone never sets health ABSENT — that is the
    ``ConditionFact`` / D3 banner-evidence path."""

    value: FactValue = FactValue.UNKNOWN
    init_failed: bool = False
    exited: bool = False
    evidence: Optional[Evidence] = None


@dataclass(frozen=True)
class ActivityFact:
    """Provider *activity* (D1): a fresh provider-owned Working/thinking region,
    a current provider event, or an attributed outstanding foreground tool.

    Pane repaint, composer animation and cursor blink are **not** activity
    (D1 Do-NOT / #767 / #485); a caller must not set ``PRESENT`` from those."""

    value: FactValue = FactValue.UNKNOWN
    owned_tool: bool = False
    evidence: Optional[Evidence] = None


@dataclass(frozen=True)
class ReadinessFact:
    """Provider *readiness* (D1): a positively-established ready composer or
    turn boundary with no unresolved foreground work and no blocking dialog."""

    value: FactValue = FactValue.UNKNOWN
    blocking_question: bool = False
    evidence: Optional[Evidence] = None


@dataclass(frozen=True)
class SettlementFact:
    """*Settlement* (D1): readiness plus a normally-settled latest assignment
    and no pending input/callback obligation. This is what separates COMPLETED
    from IDLE. ``dispatched`` records that an assignment was seen this epoch."""

    value: FactValue = FactValue.UNKNOWN
    dispatched: bool = False
    pending_obligation: bool = False
    evidence: Optional[Evidence] = None


@dataclass(frozen=True)
class ConditionFact:
    """An orthogonal *condition* (D1/D3): CAPPED and friends, with scope/reset
    metadata. Requires provider-owned, current banner evidence (D3). A bare
    token is never sufficient — the caller sets ``owned`` only when the banner
    grammar matched in the owned status/banner region. ``hint`` is the D3
    ambiguous-provenance case: emitted, but never delivered as a status or a
    ``[CONDITION]`` notice."""

    kind: Optional[str] = None
    owned: bool = False
    hint: bool = False
    scope: str = ""
    reset: str = ""
    evidence: Optional[Evidence] = None


@dataclass(frozen=True)
class StatusSample:
    """Everything one scheduled capture of one terminal produced this tick.

    The envelope is the reducer's *only* input beside the context; it carries
    time (``captured_at_monotonic`` / ``age_s``) rather than letting the reducer
    read a clock (purity). Generations and sequence let the reducer reject an
    off-generation or pre-input sample (D4).
    """

    terminal_id: str
    # ── provenance / lifecycle ────────────────────────────────────────────
    lifecycle_generation: int = 0
    process_identity: ProcessIdentity = field(default_factory=ProcessIdentity)
    input_generation: int = 0
    turn_generation: int = 0
    sequence: int = 0
    captured_at_monotonic: float = 0.0
    capture_end_monotonic: float = 0.0
    age_s: float = 0.0
    # Whether this sample was captured strictly AFTER the triggering input /
    # event. A pre-input sample can never confirm a lowering (D4).
    captured_after_trigger: bool = True
    # ── native event stream ───────────────────────────────────────────────
    native_event_cursor: Optional[int] = None
    native_coverage: bool = False
    native_end_event: bool = False
    native_start_event: bool = False
    native_failure_event: bool = False
    # ── frame representation ──────────────────────────────────────────────
    sample_mode: SampleMode = SampleMode.DIRECT_RENDERED
    # The representation routes this provider actually declares. AC-2: a sample
    # whose ``sample_mode`` is not in ``declared_modes`` is rejected (a raw-only
    # provider must reject a rendered-frame route).
    declared_modes: Tuple[SampleMode, ...] = (SampleMode.DIRECT_RENDERED,)
    # The RAW rendered rows, captured BEFORE _filtered_liveness_tail (AC-3).
    raw_frame: str = ""
    # True when the frame's widget boundaries could be located; False => the
    # frame is missing evidence, which is UNKNOWN, never IDLE (AC-3).
    frame_locatable: bool = True
    # The separately-filtered pane fingerprint (for freshness/dup detection).
    filtered_fingerprint: str = ""
    # ── typed facts (extracted by the provider adapter) ───────────────────
    health: HealthFact = field(default_factory=HealthFact)
    activity: ActivityFact = field(default_factory=ActivityFact)
    readiness: ReadinessFact = field(default_factory=ReadinessFact)
    settlement: SettlementFact = field(default_factory=SettlementFact)
    condition: ConditionFact = field(default_factory=ConditionFact)
    process: ProcessObservation = field(default_factory=ProcessObservation)


@dataclass(frozen=True)
class ReducerContext:
    """The confirmation/lifecycle state the reducer reads and proposes a
    successor for. Owned and persisted by the monitor (a StatusMonitor-keyed
    in-process map, keyed by terminal_id + lifecycle_generation + process
    identity — decision D-A1). The reducer never mutates it; it returns a
    proposed next context on the ``Candidate``.
    """

    terminal_id: str = ""
    lifecycle_generation: int = 0
    process_identity: ProcessIdentity = field(default_factory=ProcessIdentity)
    input_generation: int = 0
    # The last published legacy status (the state a lowering would move away
    # from), and the fingerprint/seq of the sample that produced it.
    last_status: TerminalStatus = TerminalStatus.UNKNOWN
    last_fingerprint: str = ""
    last_sequence: int = -1
    # A held short confirmation candidate (D4): the lowering target and the
    # fingerprint of the FIRST agreeing sample. A second, distinct, consecutive
    # sample agreeing confirms it. ``None`` => no candidate held.
    pending_lower_to: Optional[TerminalStatus] = None
    pending_lower_fingerprint: str = ""
    settlement_epoch: int = 0


@dataclass(frozen=True)
class Candidate:
    """The reducer's proposal. Never carries mutations or I/O (D2).

    ``status`` is the projected legacy enum; ``reason`` is a stable tag (e.g.
    ``"stale_frame"``, ``"health_dead"``, ``"activity"``, ``"confirmed_idle"``);
    ``last_known`` and ``last_known_at`` accompany an UNKNOWN projection so the
    TUI can show source age (D4). ``next_context`` is the proposed successor the
    monitor commits after accepting.
    """

    status: TerminalStatus
    reason: Optional[str] = None
    health: HealthFact = field(default_factory=HealthFact)
    activity: ActivityFact = field(default_factory=ActivityFact)
    readiness: ReadinessFact = field(default_factory=ReadinessFact)
    settlement: SettlementFact = field(default_factory=SettlementFact)
    condition: ConditionFact = field(default_factory=ConditionFact)
    evidence: Tuple[Evidence, ...] = ()
    last_known: Optional[TerminalStatus] = None
    last_known_at: Optional[float] = None
    next_context: Optional[ReducerContext] = None


# ── migration registry (AC-10) ────────────────────────────────────────────
# Only pi_cli and codex are migrated in Slice A. Every other provider key is
# LEGACY and its lane is driven by the legacy adapter + the untouched fusion
# arms (AC-5b). Keyed by ProviderType value.
MIGRATION_REGISTRY: Mapping[str, MigrationState] = {
    "pi_cli": MigrationState.MIGRATED,
    "codex": MigrationState.MIGRATED,
    "grok_cli": MigrationState.LEGACY,
    "claude_code": MigrationState.LEGACY,
    "kiro_cli": MigrationState.LEGACY,
}


def is_migrated(provider_key: Optional[str]) -> bool:
    """Return whether a provider is driven by the fx751 reducer (AC-10).

    Unknown keys default to LEGACY — a provider is migrated only by an explicit
    ``MIGRATED`` entry, so a new provider is never silently switched."""
    if provider_key is None:
        return False
    return MIGRATION_REGISTRY.get(provider_key, MigrationState.LEGACY) is MigrationState.MIGRATED


def _lowerable(status: TerminalStatus) -> bool:
    """Whether ``status`` is a *lowered* (delivery-eligible / terminal) state
    whose publication D4 gates behind fresh-evidence confirmation."""
    return status in (
        TerminalStatus.IDLE,
        TerminalStatus.COMPLETED,
        TerminalStatus.ERROR,
    )


def _unknown(
    sample: StatusSample,
    ctx: ReducerContext,
    reason: str,
    *,
    condition: Optional[ConditionFact] = None,
) -> Candidate:
    """Build an UNKNOWN candidate carrying last-known state, time and reason
    (D4: 'absent or contradictory evidence publishes UNKNOWN with last-known
    state, time and reason'). Clears any held lowering candidate — an
    unconfirmable sample is not a confirmation."""
    next_ctx = replace(
        ctx,
        lifecycle_generation=sample.lifecycle_generation,
        process_identity=sample.process_identity,
        input_generation=sample.input_generation,
        last_fingerprint=sample.filtered_fingerprint,
        last_sequence=sample.sequence,
        pending_lower_to=None,
        pending_lower_fingerprint="",
    )
    return Candidate(
        status=TerminalStatus.UNKNOWN,
        reason=reason,
        health=sample.health,
        activity=sample.activity,
        readiness=sample.readiness,
        settlement=sample.settlement,
        condition=condition if condition is not None else sample.condition,
        last_known=ctx.last_status,
        last_known_at=sample.captured_at_monotonic,
        next_context=next_ctx,
        evidence=_evidence_of(sample),
    )


def _evidence_of(sample: StatusSample) -> Tuple[Evidence, ...]:
    ev: list[Evidence] = []
    for fact in (sample.health, sample.activity, sample.readiness, sample.settlement):
        e = getattr(fact, "evidence", None)
        if e is not None:
            ev.append(e)
    if sample.condition.evidence is not None:
        ev.append(sample.condition.evidence)
    if sample.process.evidence is not None:
        ev.append(sample.process.evidence)
    return tuple(ev)


def _raise_context(
    sample: StatusSample, ctx: ReducerContext, status: TerminalStatus
) -> ReducerContext:
    """Successor context for a RAISE (never gated): clears any held lowering
    candidate and records the newly published status/fingerprint."""
    return replace(
        ctx,
        lifecycle_generation=sample.lifecycle_generation,
        process_identity=sample.process_identity,
        input_generation=sample.input_generation,
        last_status=status,
        last_fingerprint=sample.filtered_fingerprint,
        last_sequence=sample.sequence,
        pending_lower_to=None,
        pending_lower_fingerprint="",
    )


def reduce(sample: StatusSample, context: ReducerContext) -> Candidate:  # noqa: C901
    """The pure reducer (AC-1). No I/O, no clock read, no mutation.

    Precedence follows D1's legacy-enum order, but every *lowering* (to IDLE,
    COMPLETED or ERROR) additionally passes the D4 fresh-evidence transaction
    (AC-4): the sample must be same-generation, captured after the trigger, not
    expired, and either carry a current native end/failure event with a
    post-event matching observation, or agree with a held prior sample as the
    second of two distinct consecutive samples. A duplicate capture (same
    fingerprint/sequence as the last) can never be the second sample.

    Order, first match wins:

    1. Representation-routing guard (AC-2): a sample whose ``sample_mode`` is
       not declared by the provider is rejected -> UNKNOWN ``bad_route``.
    2. Off-generation / pre-trigger / expired / unlocatable-frame -> UNKNOWN
       with the specific reason (AC-3, AC-4, AC-25).
    3. Verified unexpected provider death or init failure -> ERROR
       ``health_dead`` (a lowering, but health-ABSENT is itself the current
       verified evidence, so it is admitted without the two-sample dance —
       D4's 'confirmed provider exit is the one exception').
    4. Current blocking question -> WAITING_USER_ANSWER ``question``.
    5. Current active turn or owned work (activity PRESENT) -> PROCESSING
       ``activity`` (a RAISE; never gated).
    6. Verified owned failure/cap banner (condition owned) -> the condition's
       status projection (CAPPED-as-status per D1/#767 belongs to Slice B's
       BannerEvidence; here an owned condition with no higher rung yields
       UNKNOWN carrying the condition until B wires the projection — recorded).
    7. Confirmed ready: settlement PRESENT -> COMPLETED, else readiness PRESENT
       -> IDLE, each behind D4 confirmation.
    8. Otherwise -> UNKNOWN ``no_evidence``.
    """
    # (1) representation route
    if sample.sample_mode not in sample.declared_modes:
        return _unknown(sample, context, "bad_route")

    # (2) provenance gates. Generation mismatch, a pre-trigger capture, an
    # expired sample or an unlocatable frame are all missing/!current evidence.
    if sample.lifecycle_generation != context.lifecycle_generation and context.last_sequence >= 0:
        # A sample from another lifecycle generation cannot repopulate this
        # context (D5 generation compare). Only guard once the context has been
        # seeded (last_sequence >= 0) so a first observation is admitted.
        return _unknown(sample, context, "stale_generation")
    if not sample.captured_after_trigger:
        return _unknown(sample, context, "pre_trigger")
    if sample.age_s > SAMPLE_EXPIRY_S:
        return _unknown(sample, context, "expired")
    if not sample.frame_locatable and sample.sample_mode is not SampleMode.NATIVE:
        # AC-3: a frame whose widget boundaries are unlocatable is missing
        # evidence -> UNKNOWN, never IDLE.
        return _unknown(sample, context, "unlocatable_frame")

    # (3) health: verified death / init failure. Current verified evidence, so
    # admitted immediately (D4 exception). A banner never reaches here.
    if sample.health.value is FactValue.ABSENT and (
        sample.health.exited or sample.health.init_failed
    ):
        return Candidate(
            status=TerminalStatus.ERROR,
            reason="health_dead",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=sample.condition,
            evidence=_evidence_of(sample),
            next_context=_raise_context(sample, context, TerminalStatus.ERROR),
        )

    # (4) blocking question (a RAISE into WAITING; never gated).
    if sample.readiness.blocking_question:
        return Candidate(
            status=TerminalStatus.WAITING_USER_ANSWER,
            reason="question",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=sample.condition,
            evidence=_evidence_of(sample),
            next_context=_raise_context(sample, context, TerminalStatus.WAITING_USER_ANSWER),
        )

    # (5) active turn / owned work. Pane churn is NOT activity — the adapter is
    # responsible for only ever setting activity PRESENT from a provider-owned
    # working region, a provider event, or an attributed foreground tool (D1
    # Do-NOT). This is a RAISE and is never gated by D4.
    if sample.activity.value is FactValue.PRESENT:
        return Candidate(
            status=TerminalStatus.PROCESSING,
            reason="activity",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=sample.condition,
            evidence=_evidence_of(sample),
            next_context=_raise_context(sample, context, TerminalStatus.PROCESSING),
        )

    # (6) owned failure / cap banner. The status PROJECTION of an owned cap
    # (#767, AC-18a) is wired in Slice B once BannerEvidence exists (AC-11); in
    # Slice A an owned condition with no higher rung has no active-turn
    # evidence, so D1 lands on confirmed-ready or UNKNOWN. We carry the owned
    # condition forward on the candidate but do not yet project it as status.
    owned_condition = sample.condition if sample.condition.owned else None

    # (7) confirmed ready — a lowering, gated by D4.
    wants_completed = sample.settlement.value is FactValue.PRESENT
    wants_idle = (not wants_completed) and sample.readiness.value is FactValue.PRESENT
    if wants_completed or wants_idle:
        target = TerminalStatus.COMPLETED if wants_completed else TerminalStatus.IDLE
        return _confirm_lowering(sample, context, target, owned_condition)

    # (8) nothing established.
    return _unknown(sample, context, "no_evidence", condition=owned_condition)


def _confirm_lowering(
    sample: StatusSample,
    ctx: ReducerContext,
    target: TerminalStatus,
    owned_condition: Optional[ConditionFact],
) -> Candidate:
    """D4 fresh-evidence transaction for a lowering to IDLE/COMPLETED (AC-4).

    Accept immediately when a current native end event is present with this
    (post-event) matching observation and native coverage. Otherwise require
    two distinct consecutive scheduled samples agreeing on the same target: the
    first arms a held candidate (publish UNKNOWN-holding = keep last status as
    a *hold*, reason ``awaiting_confirm``), the second — with a DIFFERENT
    fingerprint — confirms. A duplicate capture (same fingerprint as the held
    one, or as the last published one) never counts as the second sample.
    """
    cond = owned_condition if owned_condition is not None else sample.condition

    # Event-confirmed path: a current end event + this post-event observation.
    if sample.native_coverage and sample.native_end_event and sample.captured_after_trigger:
        return Candidate(
            status=target,
            reason="event_confirmed",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=cond,
            evidence=_evidence_of(sample),
            next_context=_raise_context(sample, ctx, target),
        )

    dup = sample.filtered_fingerprint != "" and (
        sample.filtered_fingerprint == ctx.last_fingerprint or sample.sequence == ctx.last_sequence
    )

    held_matches = (
        ctx.pending_lower_to is target
        and ctx.pending_lower_fingerprint != ""
        and ctx.pending_lower_fingerprint != sample.filtered_fingerprint
    )
    if held_matches and not dup:
        # Second distinct consecutive sample agrees -> confirm.
        return Candidate(
            status=target,
            reason="confirmed_idle" if target is TerminalStatus.IDLE else "confirmed_completed",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=cond,
            evidence=_evidence_of(sample),
            next_context=_raise_context(sample, ctx, target),
        )

    # First agreeing sample (or a duplicate that cannot confirm): arm/refresh
    # the held candidate and HOLD the last status rather than lowering. The
    # published status stays the last known one under an ``awaiting_confirm``
    # reason — never a premature lower.
    next_ctx = replace(
        ctx,
        lifecycle_generation=sample.lifecycle_generation,
        process_identity=sample.process_identity,
        input_generation=sample.input_generation,
        last_fingerprint=sample.filtered_fingerprint,
        last_sequence=sample.sequence,
        pending_lower_to=target,
        pending_lower_fingerprint=(
            sample.filtered_fingerprint if not dup else ctx.pending_lower_fingerprint
        ),
    )
    hold_status = ctx.last_status if ctx.last_status is not TerminalStatus.UNKNOWN else target
    if ctx.last_status is TerminalStatus.UNKNOWN:
        # No prior state to hold and not yet confirmed: publish UNKNOWN with the
        # target as last_known so the TUI shows the pending lower honestly.
        return Candidate(
            status=TerminalStatus.UNKNOWN,
            reason="awaiting_confirm",
            health=sample.health,
            activity=sample.activity,
            readiness=sample.readiness,
            settlement=sample.settlement,
            condition=cond,
            last_known=target,
            last_known_at=sample.captured_at_monotonic,
            evidence=_evidence_of(sample),
            next_context=next_ctx,
        )
    return Candidate(
        status=hold_status,
        reason="awaiting_confirm",
        health=sample.health,
        activity=sample.activity,
        readiness=sample.readiness,
        settlement=sample.settlement,
        condition=cond,
        last_known=ctx.last_status,
        last_known_at=sample.captured_at_monotonic,
        evidence=_evidence_of(sample),
        next_context=next_ctx,
    )


# ── legacy-verdict bridge (AC-2 thin routes) ───────────────────────────────
# The migrated providers keep their hardened text classifiers (codex's
# ScreenClassificationResult, pi's _live_working_spinner/_has_idle_chrome). AC-2
# makes the reducer the single AUTHORITY without rewriting those extractors: the
# provider runs its classifier, maps the verdict into typed facts, and routes
# through the reducer. The full generation/freshness envelope is supplied at the
# monitor fusion site (AC-5a); a BARE get_status call has no second sample, so
# the bridge below builds a self-consistent, immediately-projectable sample and
# seeds the context so no spurious two-sample hold is produced — preserving the
# legacy per-call contract ("always returns a valid status").


def facts_from_legacy_status(
    status: TerminalStatus,
    *,
    dispatched: bool = False,
    working_seen: bool = False,
) -> Tuple[HealthFact, ActivityFact, ReadinessFact, SettlementFact]:
    """Map a legacy per-call ``TerminalStatus`` verdict into typed facts.

    This is the projection SEAM for a thin route: the classifier already
    decided PROCESSING/IDLE/COMPLETED/WAITING/ERROR/UNKNOWN from the frame; we
    express that decision as the facts D1 ranks. A verdict is *this frame's*
    evidence, so it is marked PRESENT/ABSENT accordingly. UNKNOWN maps to all
    facts UNKNOWN (no evidence).
    """
    ev = Evidence(kind="legacy_verdict", detail=status.value)
    health = HealthFact()
    activity = ActivityFact()
    readiness = ReadinessFact()
    settlement = SettlementFact(dispatched=dispatched)

    if status is TerminalStatus.PROCESSING:
        activity = ActivityFact(value=FactValue.PRESENT, evidence=ev)
    elif status is TerminalStatus.WAITING_USER_ANSWER:
        readiness = ReadinessFact(blocking_question=True, evidence=ev)
    elif status is TerminalStatus.ERROR:
        health = HealthFact(value=FactValue.ABSENT, exited=True, evidence=ev)
    elif status is TerminalStatus.COMPLETED:
        readiness = ReadinessFact(value=FactValue.PRESENT, evidence=ev)
        settlement = SettlementFact(value=FactValue.PRESENT, dispatched=dispatched, evidence=ev)
    elif status is TerminalStatus.IDLE:
        readiness = ReadinessFact(value=FactValue.PRESENT, evidence=ev)
    # UNKNOWN / RENDER_UNCERTAIN → all facts UNKNOWN (no evidence).
    return health, activity, readiness, settlement


def sample_from_legacy_status(
    terminal_id: str,
    status: TerminalStatus,
    *,
    mode: SampleMode,
    declared_modes: Tuple[SampleMode, ...],
    frame_locatable: bool = True,
    condition: Optional[ConditionFact] = None,
    dispatched: bool = False,
) -> StatusSample:
    """Build a self-consistent ``StatusSample`` from a legacy verdict for the
    bare-call thin route (AC-2). A frame that produced UNKNOWN and whose
    boundaries were unlocatable sets ``frame_locatable=False`` so AC-3 holds."""
    health, activity, readiness, settlement = facts_from_legacy_status(
        status, dispatched=dispatched
    )
    return StatusSample(
        terminal_id=terminal_id,
        sample_mode=mode,
        declared_modes=declared_modes,
        frame_locatable=frame_locatable,
        captured_after_trigger=True,
        age_s=0.0,
        sequence=0,
        # A bare call is event-confirmed-equivalent for a READY verdict: the
        # legacy contract returns a valid status every call, so a lowering must
        # project immediately rather than hold. native_coverage+native_end_event
        # take the event-confirmed arm in the reducer for IDLE/COMPLETED.
        native_coverage=status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED),
        native_end_event=status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED),
        health=health,
        activity=activity,
        readiness=readiness,
        settlement=settlement,
        condition=condition if condition is not None else ConditionFact(),
    )


def derive_status_from_legacy(
    terminal_id: str,
    status: TerminalStatus,
    *,
    mode: SampleMode,
    declared_modes: Tuple[SampleMode, ...],
    frame_locatable: bool = True,
    condition: Optional[ConditionFact] = None,
    dispatched: bool = False,
) -> Candidate:
    """Thin-route convenience: build a legacy-verdict sample and reduce it with
    a fresh seeded context so the bare call projects immediately (AC-2)."""
    sample = sample_from_legacy_status(
        terminal_id,
        status,
        mode=mode,
        declared_modes=declared_modes,
        frame_locatable=frame_locatable,
        condition=condition,
        dispatched=dispatched,
    )
    ctx = ReducerContext(terminal_id=terminal_id, last_sequence=-1)
    return reduce(sample, ctx)

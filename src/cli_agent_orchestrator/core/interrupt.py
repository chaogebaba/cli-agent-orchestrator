"""D6b / A2.9 — the interrupt plane's vocabulary, as values with no I/O.

An ``interrupt`` is a supervisor DECISION, executed by a serialized per-session
state machine and priced in the journal.  It is never elected by the plane, and
nothing in this module decides anything: it holds the closed sets, the two
identity types and the one arithmetic rule that the store, the limiter and the
receiver task all have to agree about.

Three things are worth reading this module for, because each is a rule that has
already been got wrong once in review and each has exactly one line of defence
here:

* :class:`ActiveTurnHandle` carries **no queue identifiers**.  A2.9(iii) keeps
  receipt and active turn separate: ``complete_prompt`` closes N's queue attempt
  and appends its ``presented`` fact, while the session actor may go on holding a
  runtime handle for N's turn until the matching ``stopReason``.  Putting a
  ``msg_id``, ``claim_id`` or lease on the handle would re-attach the two and
  make an interrupt able to reopen a delivered row — the exact thing A2.3/I3
  forbids.  ``__post_init__`` refuses it rather than trusting the convention.

* :class:`CancelWindow` carries **two distinct instants**, and the store computes
  both.  ``deadline`` is the cancel-settle deadline; ``lease_until`` is I's lease
  expiry, strictly later by ``CANCEL_HOLD_MARGIN_S``.  Collapsing them (r18's
  named mutant "set deadline=lease_until") makes a cancel that settles at the
  last legal instant land on a lease that has just expired.

* :func:`effective_deadline` is A2.9(iv)'s canonical ``LIFETIME_LAW``, verbatim,
  in ONE place.  A caller-set expiry is **never** extended by busy credit — r3's
  B2 bug was a caller deadline swallowed by the credit — and the system bound is
  extended only by the CAPPED accumulation, because an uncapped accumulator lets
  one ledger error keep a row alive indefinitely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, TypeAlias

from cli_agent_orchestrator.core.timing import BUSY_CREDIT_CAP_S

__all__ = [
    "ActiveTurnHandle",
    "AdmissionOutcome",
    "AuditPhase",
    "CallerPrincipal",
    "CancelHandle",
    "CancelOutcome",
    "CancelRaceLost",
    "CancelSettlement",
    "CancelWindow",
    "ClaimedRow",
    "InterruptAdmission",
    "InterruptFence",
    "InterruptPhase",
    "InterruptPreparation",
    "InterruptRefusal",
    "InterruptStateRow",
    "LedgerWindow",
    "LimiterDecision",
    "MID_INTERRUPT_PHASES",
    "PreparationKind",
    "PrincipalOrigin",
    "Quota",
    "RecoveryWindow",
    "SessionState",
    "SettleKind",
    "SubmitEnvelope",
    "SubmitReceipt",
    "Urgency",
    "WINDOW_LOST",
    "WindowLost",
    "effective_deadline",
    "urgency_rank",
]


class Urgency(StrEnum):
    """A2.9(i)'s envelope field.  Two values, closed at admission.

    Part of A2.1's conflicting-reuse identity: the same ``callback_id`` with a
    different urgency is a REJECTED reuse, not a re-send.  ``normal`` takes D6's
    ladder exactly as written; ``interrupt`` is admitted only against a row whose
    rung-0 ``cancel`` cell is PASS.
    """

    NORMAL = "normal"
    INTERRUPT = "interrupt"


def urgency_rank(urgency: Urgency | str | None) -> int:
    """The claim ORDER BY's first term (A2.9(iv)): ``urgency_rank, available_at, msg_id``.

    Lower sorts first, so ``interrupt`` is 0.  A rank rather than a reversed sort
    on the string, because "interrupt" < "normal" lexicographically is a
    coincidence of spelling and would silently invert if either value were ever
    renamed.  ``None`` and anything unrecognised rank as ``normal``: an
    interrupt is an explicit decision, so the default must never be one.
    """
    return 0 if urgency == Urgency.INTERRUPT else 1


class InterruptPhase(StrEnum):
    """D6b(3)'s persisted phase, CASed on ``(terminal_id, phase, generation)``.

    The set is closed and the selection rule in ``claim_next`` is TOTAL over it
    (r14, review r13 B4) — a phase with no stated answer is how a second
    ``session/prompt`` gets issued against an ongoing turn.
    """

    NONE = "none"
    PENDING = "pending"
    CANCELLING = "cancelling"
    PROMPTING = "prompting"
    RECOVERING = "recovering"


#: The phases in which a terminal is NOT admissible for another interrupt and is
#: served NO row by ``claim_next``.  ``recovering`` is deliberately in the set:
#: r13 (review r12 B4) made the timeout path CAS ``cancelling -> recovering``
#: rather than ``-> none``, precisely so the terminal stays non-admissible until
#: recovery is durable.
MID_INTERRUPT_PHASES: Final = frozenset(
    {
        InterruptPhase.PENDING,
        InterruptPhase.CANCELLING,
        InterruptPhase.PROMPTING,
        InterruptPhase.RECOVERING,
    }
)


class SessionState(StrEnum):
    """What ``prepare_interrupt`` reports, and what the receipt OBSERVES.

    ``UNKNOWN`` exists because the receipt is taken at admission, before any
    probe: A2.9/D6b(5) makes ``observed_state`` nullable and explicitly
    non-authoritative, and a vocabulary with no way to say "not observed" would
    force the edge to guess ``idle``.
    """

    IDLE = "idle"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class InterruptRefusal(StrEnum):
    """Edge refusals (D6b(5)/(6)).  Every one of them admits NOTHING.

    The distinction that matters: ``INTERRUPT_IN_PROGRESS`` is the reservation
    CAS failing and is charged NO quota (r14, review r13 N2), while the two
    quota refusals are charged nothing because they never got past the bound.
    A refusal that left a ``delivery_msg`` row behind is AC-S1.22's fails-if.
    """

    IN_PROGRESS = "INTERRUPT_IN_PROGRESS"
    RATE_LIMITED = "INTERRUPT_RATE_LIMITED"
    BUDGET_EXHAUSTED = "INTERRUPT_BUDGET_EXHAUSTED"
    WINDOW_TOO_SHORT = "INTERRUPT_WINDOW_TOO_SHORT"
    UNAUTHENTICATED = "INTERRUPT_UNAUTHENTICATED"


class AuditPhase(StrEnum):
    """D6b(7)'s phase tags on the append-only interrupt audit object.

    ``ADMITTED`` is written on a fact that exists BEFORE any claim, which is what
    makes an admitted-but-never-claimed interrupt attributable at all (r11,
    review r10 B2).  The three no-dispatch terminals — ``PENDING_EXPIRED``,
    ``WINDOW_LOST``, ``CANCEL_TIMEOUT`` — ride ``kind=dead`` facts; ``admitted``
    is never rewritten, because A2.1a's trace is append-only.
    """

    ADMITTED = "admitted"
    CLAIMED = "claimed"
    IDLE = "idle"
    CANCELLED = "cancelled"
    CANCEL_TIMEOUT = "cancel_timeout"
    PROMPT_AMBIGUOUS = "prompt_ambiguous"
    PENDING_EXPIRED = "pending_expired"
    WINDOW_LOST = "window_lost"


class PrincipalOrigin(StrEnum):
    """Where a caller came from.  Part of the budget key, so it cannot be spoofed
    into another origin's quota."""

    TERMINAL = "terminal"
    VIEWER = "viewer"


@dataclass(frozen=True)
class CallerPrincipal:
    """D6b(6): issued BY THE SERVER, never supplied by a request.

    MCP callers are ``(terminal, terminal_id, lifecycle_generation)`` derived
    from the verified D22 token.  Viewer callers are ``(viewer, subject)`` where
    the subject is the verified OAuth ``sub`` when an identity provider is
    configured, and otherwise the shared local-installation principal.

    ``budget_key`` deliberately EXCLUDES ``lifecycle_generation`` and any
    attach/session id.  ``attach_session_id`` is audit metadata only, so
    re-attaching, reconnecting or restarting a viewer never resets quota — the
    forgery/reconnect arm of AC-S1.22(h) is exactly this property.  The
    generation is carried for attribution, not for accounting.
    """

    origin: PrincipalOrigin
    subject: str
    lifecycle_generation: int | None = None
    attach_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("CallerPrincipal.subject may not be empty")

    @property
    def budget_key(self) -> str:
        """``(origin, subject)`` — the ONLY thing the per-principal budget counts."""
        return f"{self.origin.value}:{self.subject}"


@dataclass(frozen=True)
class Quota:
    """What the admission receipt reports back about the two bounds (D6b(5))."""

    remaining_gap_s: float
    remaining_budget: int


@dataclass(frozen=True)
class ActiveTurnHandle:
    """A2.9(iii): the session actor's runtime handle on the turn now in flight.

    It names a TURN, not a queue row.  ``callback_id`` is the cut candidate's
    callback id and is carried for audit and for ``cancel_if_current``'s exact
    comparison; it is not a fence and it can never be used to re-lease, re-present
    or reclassify N.  ``__post_init__`` refuses queue identifiers outright rather
    than relying on nobody adding them, because "no queue ids in H" is one of
    AC-S1.28's static checks and a convention is not a check.
    """

    terminal_id: str
    lifecycle_generation: int
    session_id: str
    acp_request_id: str
    callback_id: str
    turn_seq: int

    def __post_init__(self) -> None:
        for forbidden in ("msg_id", "claim_id", "lease_expires_at", "lease_owner"):
            if hasattr(self, forbidden):
                raise TypeError(
                    f"ActiveTurnHandle may not carry the queue identifier {forbidden!r} "
                    "(A2.9(iii): receipt and active turn are separate)"
                )


@dataclass(frozen=True)
class CancelWindow:
    """What ``begin_cancel`` computed and PERSISTED, in one value (A2.9(iv)).

    Both fields are returned so the receiver task never recomputes either.  The
    named r18 mutants this shape kills: "caller supplies deadline/lease_until",
    "store resamples clock", "set deadline=lease_until", "await against
    recomputed time".  ``await_cancel`` consumes ``deadline``; a restart reads the
    same value back out of the row.
    """

    deadline: datetime
    lease_until: datetime

    def __post_init__(self) -> None:
        if self.lease_until <= self.deadline:
            raise ValueError(
                "CancelWindow.lease_until must be strictly later than .deadline "
                f"({self.lease_until} <= {self.deadline}); a cancel that settles at "
                "the last legal instant must still be owned by its claim"
            )


@dataclass(frozen=True)
class WindowLost:
    """``begin_cancel``'s other return: the fence or the lifetime was lost.

    A VALUE rather than an exception, for the reason ``core/switches.py`` gives:
    this is read on the delivery path of a server whose tick must not be
    self-inflicted-failed by a diagnosability outcome.  The receiver task
    terminalizes I ``INTERRUPT_WINDOW_LOST`` and leaves N and the actor untouched.
    """

    reason: str = "window_lost"


#: The single instance.  An identity comparison (``result is WINDOW_LOST``) then
#: reads the same at every call site, and no caller can construct a second one
#: carrying a different reason and mean something else by it.
WINDOW_LOST: Final = WindowLost()


def effective_deadline(
    *,
    dead_by: datetime,
    caller_set: bool,
    busy_accumulated_s: float,
) -> datetime:
    """A2.9(iv)'s ``LIFETIME_LAW``, verbatim and in exactly one place.

        caller-set: effective_deadline(row) = dead_by
        system:     effective_deadline(row) = dead_by + min(busy_accumulated_s,
                                                            BUSY_CREDIT_CAP_S)
        both claim and death expire iff effective_deadline(row) <= now

    Two properties, each of which was a named bug before it was a rule:

    * A CALLER-SET expiry is never extended.  r3's B2 bug had a caller's
      ``expire_after_s`` swallowed by accumulated busy credit, so a message the
      sender had deliberately time-boxed outlived its box.  The branch is on who
      set the deadline, not on how much credit exists.
    * The system extension is CAPPED.  An uncapped accumulator lets a single
      ledger error extend a row indefinitely (AC-S1.13's fails-if), and the cap
      is what keeps the row's wall-clock age inside ``IDLE_STALL_AGE_S``
      (AC-S1.17 clause 3).
    """
    if caller_set:
        return dead_by
    credit = min(max(busy_accumulated_s, 0.0), float(BUSY_CREDIT_CAP_S))
    return dead_by + timedelta(seconds=credit)


# ---------------------------------------------------------------------------
# The values D5's ports and D6b(3)'s aggregate exchange.
#
# All frozen, all without behaviour beyond a validating ``__post_init__``.  They
# are here rather than in ``core/ports.py`` for the reason ``core/delivery.py``
# holds the queue's values: a Protocol is a SHAPE, and a shape that carried its
# own payload types would make every adapter import the protocol module just to
# build a return value.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterruptFence:
    """What identifies I's claim, and the ONLY thing a transition may CAS on.

    ``(msg_id, claim_id, owner)`` is the queue fence; ``generation`` fences the
    phase row.  Both, because they can be lost independently: a lease can expire
    while the phase row is intact, and a phase row can be CASed by a restarted
    receiver task while the lease is still held.

    It is deliberately NOT an :class:`ActiveTurnHandle`.  A delivered row is
    never a fence (r18's "delivered row as fence" mutant) — N's dispatch
    eligibility was extinguished at its local receipt, before its turn could
    become an interrupt target at all.
    """

    terminal_id: str
    msg_id: str
    claim_id: int
    owner: str
    generation: int


@dataclass(frozen=True)
class SubmitEnvelope:
    """One envelope, carrying exactly one callback (AC-S1.25's multi-callback arm).

    The native carrier batches several entries per wake; on ACP one envelope is
    one callback (A2.9(vii)).  A batch would make "exactly one delivery per
    ``callback_id``" — AC-S1.8 clause 1 — unprovable from the journal, because a
    partial batch write has no honest per-callback outcome.
    """

    callback_id: str
    body: str
    urgency: Urgency = Urgency.NORMAL
    supersedes: tuple[str, ...] = ()
    causes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmitReceipt:
    """The LOCAL write receipt (D5's "accepted"), never an acknowledgement.

    ``write_flushed_at`` is when the bytes left; ``write_receipt_at`` is when the
    flush had survived ``ACP_WRITE_SETTLE_S`` without a locally observable
    failure.  ``ambiguous`` is the honest third answer: the write flushed and
    then the transport ended, so whether the agent saw it is unknowable from
    here and the attempt resolves ``SUBMISSION_UNCERTAIN`` rather than guessing.
    """

    accepted: bool
    write_flushed_at: datetime | None = None
    write_receipt_at: datetime | None = None
    ambiguous: bool = False
    detail: str = ""


class PreparationKind(StrEnum):
    """``prepare_interrupt``'s two answers.  There is no third: the driver is the
    only reader of its own stream, so it always knows which of these it is."""

    IDLE = "idle"
    CANCEL_REQUIRED = "cancel_required"


@dataclass(frozen=True)
class InterruptPreparation:
    """``IDLE``, or ``CANCEL_REQUIRED`` naming the exact turn to cut."""

    kind: PreparationKind
    active_turn: ActiveTurnHandle | None = None

    def __post_init__(self) -> None:
        if self.kind is PreparationKind.CANCEL_REQUIRED and self.active_turn is None:
            raise ValueError("CANCEL_REQUIRED must name the active turn it would cancel")
        if self.kind is PreparationKind.IDLE and self.active_turn is not None:
            raise ValueError("IDLE carries no active turn")


@dataclass(frozen=True)
class CancelHandle:
    """Proof that cancel bytes were written for exactly this turn."""

    active_turn: ActiveTurnHandle
    sent_at: datetime


@dataclass(frozen=True)
class CancelRaceLost:
    """The handle moved between deciding and writing, so NOTHING was written.

    ``observed`` is what the actor holds now — idle, or a new handle — and it is
    an observation for the next ``prepare_interrupt``, never a fence.
    """

    observed_state: SessionState
    observed_turn: ActiveTurnHandle | None = None


#: ``cancel_if_current`` returns one or the other.  A union rather than an
#: optional handle, because "no handle" and "raced" need different next moves:
#: a race re-prepares under the same reservation, and a failure does not.
CancelOutcome: TypeAlias = CancelHandle | CancelRaceLost


class SettleKind(StrEnum):
    """How ``await_cancel`` ended."""

    CANCELLED = "cancelled"
    UNSETTLED = "unsettled"


@dataclass(frozen=True)
class CancelSettlement:
    """What the wire did inside the persisted deadline.

    ``dead_tool_call_ids`` is rendered HONESTLY — zero, one or many — because
    AC-S1.23's fails-if includes "a multi-tool cancel is rendered as one".
    ``settle_ms`` is null on an unsettled cancel; the fields are nullable by
    phase, and a phase with no value for a field says so rather than zero.
    """

    kind: SettleKind
    cancelled_acp_request_id: str | None = None
    settle_ms: float | None = None
    dead_tool_call_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterruptAdmission:
    """One admission request, as the edge hands it to the aggregate."""

    terminal_id: str
    callback_id: str
    principal: CallerPrincipal
    envelope: SubmitEnvelope
    now: datetime
    observed_state: SessionState = SessionState.UNKNOWN
    cut_candidate: str | None = None
    force: bool = False
    #: The caller's own ``expire_after_s``, carried through unchanged.  When it
    #: is set the row's lifetime is CALLER-SET and busy credit may never extend
    #: it (A2.9(iv)); an interrupt time-boxed below the cancel window is refused
    #: ``INTERRUPT_WINDOW_TOO_SHORT`` pre-admission rather than quietly given
    #: more time.
    envelope_expire_after_s: int | None = None
    #: True when this viewer admission is only local by the operator's explicit
    #: ``CAO_VIEWER_LOCAL_PRINCIPAL_PROXIED`` opt-in.  Journalled on EVERY such
    #: admission (r12, review r11 N1), because an opt-in that is recorded once at
    #: boot cannot be audited per request.
    proxied_optin: bool = False


@dataclass(frozen=True)
class AdmissionOutcome:
    """D6b(5)'s receipt shape, refusal and success in ONE type.

    ``interrupt_id`` IS the callback id — there is no second identifier (r13,
    review r12 B5).  ``observed_state`` and ``cut_candidate`` are explicitly
    non-authoritative observations: the handle may move between admission and
    claim, and only the later exact handle from ``prepare_interrupt`` can
    authorize a cancel.  Nothing that looks like a disposition may appear here;
    the final cut, cancelled turn and settle time exist only in the journal fold.
    """

    refused: InterruptRefusal | None
    interrupt_id: str | None = None
    callback_id: str | None = None
    target: str | None = None
    observed_state: SessionState = SessionState.UNKNOWN
    cut_candidate: str | None = None
    reservation: str | None = None
    quota: Quota | None = None

    @property
    def admitted(self) -> bool:
        return self.refused is None


@dataclass(frozen=True)
class ClaimedRow:
    """One leased row plus the fence that owns it."""

    fence: InterruptFence
    callback_id: str
    envelope: SubmitEnvelope
    available_at: datetime
    effective_deadline: datetime


@dataclass(frozen=True)
class RecoveryWindow:
    """``begin_recovery``'s two derived instants.

    ``teardown_at`` is ``recovery_deadline - (DELIVERY_TICK_S +
    ACP_KILL_GRACE_S)``: recovery ATTEMPTS stop there so that one scan plus a
    full process-group teardown still fit inside the promised bound.  Derived in
    the store and returned, not recomputed by the caller, for the same reason the
    cancel deadline is.
    """

    recovery_deadline: datetime
    teardown_at: datetime

    def __post_init__(self) -> None:
        if self.teardown_at >= self.recovery_deadline:
            raise ValueError("teardown_at must leave room inside recovery_deadline")


@dataclass(frozen=True)
class InterruptStateRow:
    """D6b(3)'s persisted row, as a value.

    ``deadline`` always means the persisted CANCEL-SETTLE deadline and never I's
    lease expiry — the two are different instants by ``CANCEL_HOLD_MARGIN_S`` and
    conflating them is a named r18 mutant.  It is non-null in ``cancelling``, may
    be retained read-only in ``recovering`` for audit, and is cleared before
    ``prompting`` or ``none``.  ``active_turn_*`` is AUDIT-ONLY: it records which
    turn was cut, and nothing reads it back as authority.
    """

    terminal_id: str
    phase: InterruptPhase
    generation: int
    interrupt_msg_id: str | None = None
    interrupt_claim_id: int | None = None
    cut_callback_id: str | None = None
    active_turn_session_id: str | None = None
    active_turn_request_id: str | None = None
    active_turn_generation: int | None = None
    active_turn_seq: int | None = None
    deadline: datetime | None = None
    pending_deadline: datetime | None = None
    recovery_deadline: datetime | None = None
    cancel_sent: bool = False


@dataclass(frozen=True)
class LedgerWindow:
    """Everything the two bounds need, read ONCE inside the admission transaction.

    A value rather than a store handle, and that is what keeps D6b(6)'s "one
    atomic authority" true across the layer boundary: the limiter is application
    POLICY and may not touch SQLite, while the two bounds must be evaluated under
    the same write lock as the reservation CAS or two concurrent admissions at
    the budget edge can both pass (AC-S1.22(c)).  So the adapter reads the window
    inside its ``BEGIN IMMEDIATE`` and hands it over as data.

    ``principal_admissions`` holds the admitted instants inside the budget window
    for this principal ALONE — AC-S1.22(d)'s cross-surface arm is exactly the
    claim that an MCP principal's budget is consumed by its own calls only.
    ``last_terminal_admission`` is the most recent admission against this
    TERMINAL by anyone, because the gap is a property of the terminal and a
    per-principal gap would let a rotating loop through.
    """

    principal_admissions: tuple[datetime, ...]
    last_terminal_admission: datetime | None


@dataclass(frozen=True)
class LimiterDecision:
    """The limiter's answer, with the quota it would report either way.

    The quota is populated on a REFUSAL too.  A caller that is told only "no"
    has to guess whether to retry in a second or in ten minutes, and the two
    bounds have very different answers.
    """

    refused: InterruptRefusal | None
    quota: Quota

    @property
    def admitted(self) -> bool:
        return self.refused is None

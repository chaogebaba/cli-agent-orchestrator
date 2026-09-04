"""The delivery queue's pure domain (WP-ARCH phase 3, sub-phase 3a).

Everything here is a value or a total function over values.  No SQLite, no
clock, no environment — ``core-is-pure`` forbids the first and the other two are
arguments.  That is not tidiness: the two pieces of phase-3 logic most likely to
be got wrong are the boot guard's transition table and the once-only ``dead_by``
stamp, and both are decidable without a database.  Written here they are tested
by enumeration rather than by contriving a server state.

Four vocabularies, two functions and five row models:

* :class:`QueueMode`, :class:`MsgState`, :class:`DeadReason`,
  :class:`AttemptOutcome` — what a row can be and how it can end.
* :class:`SwitchPosition` and :func:`resolve_switch` — D9's guard, total over
  four requested positions and the queue conditions it is resolved against.
* :func:`compute_dead_by` — D12's deadline, folded with D8's caller expiry.
* :class:`EnqueueDraft`, :class:`QueueMessage`, :class:`DeliveryAttempt`,
  :class:`DeadLetter` and :class:`SeatDigest` — the audit §3.2 rows as values,
  so ``app`` can read and reason about a row without a database and the store
  adapter is the only module that knows they are SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.timing import DELIVERY_MAX_ATTEMPTS, DELIVERY_MAX_LIFETIME_S

__all__ = [
    "AttemptOutcome",
    "DeadLetter",
    "DeadReason",
    "DeliveryAttempt",
    "EnqueueDraft",
    "GuardOutcome",
    "MsgKind",
    "MsgState",
    "NON_DELIVERY_OUTCOMES",
    "QueueMessage",
    "QueueMode",
    "QueueOccupancy",
    "SeatDigest",
    "SwitchPosition",
    "TERMINAL_STATES",
    "compute_dead_by",
    "parse_switch",
    "resolve_switch",
]


class QueueMode(StrEnum):
    """Whether a row is an observational copy or the real thing.

    Set at enqueue and NEVER rewritten.  ``shadow`` from sub-phase 3a, ``live``
    from the write-through flip onward.

    The discriminator exists because 3a writes copies of messages the legacy
    path owns into the same table the queue will later serve from.  Two
    mechanisms depend on telling them apart, and the blueprint records why each
    needs it: D9's boot guard counts occupancy over LIVE rows only, or a bounced
    shadow deployment would resolve to ``drain`` and inject copies of messages
    already delivered (#506, reproduced by the guard added to prevent loss); and
    ``claim`` filters on it so a shadow row is undeliverable by construction
    rather than by each consumer remembering (B20).
    """

    SHADOW = "shadow"
    LIVE = "live"


class MsgState(StrEnum):
    """The state column of ``delivery_msg`` (audit §3.2, extended by the phase).

    ``superseded`` is the blueprint's addition to the audit's four: F578
    supersession (D8) and the flip's sweep of unresolved shadow rows both need
    an ending that is neither a delivery nor a death.  Folding it into ``dead``
    would make the dead-letter table mean two different things and would put
    ordinary supersession traffic in front of an operator reading for failures.

    ``dead`` is a state on the row AND a row in the separate ``delivery_dead``
    table (the audit adopted honker's separate-table decision), so a poisoned
    message stops occupying the reclaim loop and hiding live rows.
    """

    READY = "ready"
    LEASED = "leased"
    DELIVERED = "delivered"
    SUPERSEDED = "superseded"
    DEAD = "dead"


#: I1's terminal set.  An enqueued message ends in exactly one of these and does
#: not rest in a pending state without a deadline.  Read by D9's occupancy
#: predicate, by retention, and by the AC-3a agreement report.
TERMINAL_STATES = frozenset({MsgState.DELIVERED, MsgState.SUPERSEDED, MsgState.DEAD})


class DeadReason(StrEnum):
    """Why a row reached ``delivery_dead`` — a reason, never a fourth state.

    I1 fixes three terminal states, so a caller expiry must not add one (D8).
    All four are conditions that can only bring a row's death FORWARD; none can
    push it back past ``dead_by``.

    ``MAX_ATTEMPTS``  — the attempt budget ran out.  Spent by ``pane_absent``
                        and ``veto_unverified``, both of which retain the lease
                        so ``reclaim`` can count them (D12).
    ``VETO_CEILING``  — the row sat dialog-held for ``DELIVERY_VETO_CEILING_S``.
    ``MAX_LIFETIME``  — ``dead_by`` passed, whatever the row was doing.
    ``EXPIRED``       — the caller's own ``expire_after_s`` came first (D8).
    """

    MAX_ATTEMPTS = "max_attempts"
    VETO_CEILING = "veto_ceiling"
    MAX_LIFETIME = "max_lifetime"
    EXPIRED = "expired"


class AttemptOutcome(StrEnum):
    """What one delivery attempt did, recorded per ``(msg_id, claim_id, carrier)``.

    ``DELIVERED`` plus D12's three non-delivery outcomes.  All three of the
    latter KEEP the row leased with ``lease_expires_at`` unchanged, write their
    attempt row and return; lease retention is what makes an outcome observable
    by ``reclaim`` at all.  An earlier draft released the lease and left both
    rows in ``ready``, where ``reclaim`` (which selects ``state='leased' AND
    lease_expires_at<now``) could never see them, so ``delivery_dead`` was
    unreachable (#604).

    ``LEGACY_OTHER`` is sub-phase 3a ONLY, and is the honest way to record a
    legacy attempt whose outcome has no equivalent in this vocabulary — the
    mirror writer observes what legacy actually did, and legacy settles
    attempts with ``deferred``, ``ambiguous``, ``interrupted`` and
    ``unresolved`` as well.  Mapping those onto ``veto_dialog`` would invent a
    dialog gate that was never consulted and would corrupt the very comparison
    3a exists to make, so they are recorded under this value with the legacy
    outcome and reason verbatim in ``detail``.  Nothing in mode ``live`` ever
    writes it; a test asserts that.
    """

    DELIVERED = "delivered"
    VETO_DIALOG = "veto_dialog"
    VETO_UNVERIFIED = "veto_unverified"
    PANE_ABSENT = "pane_absent"
    LEGACY_OTHER = "legacy_other"


#: D12's three non-delivery outcomes, in one place so the retention rule and the
#: accounting split cannot drift from each other.
NON_DELIVERY_OUTCOMES = frozenset(
    {AttemptOutcome.VETO_DIALOG, AttemptOutcome.VETO_UNVERIFIED, AttemptOutcome.PANE_ABSENT}
)


class SwitchPosition(StrEnum):
    """``CAO_DELIVERY_QUEUE``, four positions (D9).

    A separate variable from phase 1's ingestion switch, sitting beside it in
    ``bootstrap.py`` and read once at boot in the same structural way.  It is a
    DIFFERENT switch rather than a second spelling of the same one: one master
    strangler flag would couple a phase-1 rollback to a phase-3 rollback.

    The fourth position is not decoration.  With dual-write excluded, a row
    enqueued while ``on`` exists in ``delivery_msg`` and nowhere else, so
    demoting straight to ``shadow`` would resume legacy inserts while leaving
    those rows in a table the seat is no longer served from — silent message
    loss on the one control the phase offers for backing out (#584).  ``drain``
    keeps serving the rows already enqueued while new traffic goes back to
    legacy, so the queue empties on its own budget.
    """

    OFF = "off"
    SHADOW = "shadow"
    DRAIN = "drain"
    ON = "on"


def parse_switch(value: str | None) -> SwitchPosition:
    """Read the environment variable's value.  Unknown or unset means ``off``.

    Deliberately permissive about case and surrounding whitespace and
    deliberately NOT permissive about anything else: an operator who typed
    ``true`` gets ``off``, and the boot guard's finding is what tells them the
    queue is not being served.  Guessing at an intended position would be a
    worse failure than the default, because the default is the safe one.
    """
    if value is None:
        return SwitchPosition.OFF
    try:
        return SwitchPosition(value.strip().lower())
    except ValueError:
        return SwitchPosition.OFF


@dataclass(frozen=True)
class QueueOccupancy:
    """What the boot guard resolves a requested position against.

    ``live_non_terminal`` counts rows that are BOTH ``mode='live'`` and outside
    :data:`TERMINAL_STATES`.  Both qualifications are load-bearing and each was
    a defect before it was a rule.  Not merely "at least one row", or a finished
    queue would pin the server in ``drain`` forever.  And live rows only,
    because 3a writes shadow copies into this same table: counting those would
    resolve a bounced shadow deployment into ``drain``, whose tick would then
    inject copies of messages the legacy path already delivered — a second
    carrier over one id, which is #506 reproduced by the guard, in the first
    sub-phase to ship.

    ``open_barrier_labels`` carries the labels rather than a count so the
    finding can name them; an operator holding a flip needs to know WHICH
    barrier, not how many.
    """

    live_non_terminal: int = 0
    open_barrier_labels: tuple[str, ...] = ()

    @property
    def occupied(self) -> bool:
        return self.live_non_terminal > 0


@dataclass(frozen=True)
class GuardOutcome:
    """The resolved position, and what the operator is told about it."""

    requested: SwitchPosition
    position: SwitchPosition
    finding: FindingCode | None = None
    detail: str = ""
    context: dict[str, str] = field(default_factory=dict)

    @property
    def demoted(self) -> bool:
        return self.position is not self.requested


def resolve_switch(requested: SwitchPosition, occupancy: QueueOccupancy) -> GuardOutcome:
    """D9's boot guard: total over four positions and the queue's condition.

    Defined here and nowhere else.  Every other statement of the rule in the
    blueprint — §6's drain window, the occupancy test, the drain tick's own
    ``mode`` condition — refers to this table rather than restating a procedure,
    because a procedure is something an operator can route around and a guard is
    not.

    ==============  ==============  ==================================
    Requested       Queue empty     Queue non-empty
    ==============  ==============  ==================================
    ``off``         ``off``         ``drain`` + ``DIAG-QUEUE-ORPHAN-GUARD``
    ``shadow``      ``shadow``      ``drain`` + the same finding
    ``on``          ``on``          ``on`` — the queue is being served
    ``drain``       ``shadow``      ``drain``
    ==============  ==============  ==================================

    Three properties are worth stating because a reader will look for them:

    * **``drain`` is the one cell that PROMOTES.**  It carries an operator's
      stated intent forward once the queue is empty; holding a drained
      deployment in ``drain`` would leave the delivery machinery running with
      nothing to deliver.  The asymmetry is deliberate.
    * **The guard overrides the default.**  A boot with the variable unset over
      a leftover queue runs in ``drain`` in a deployment that never opted in,
      and the finding is how an operator learns.  Silently orphaning the rows
      instead would be the failure class the phase exists to remove.
    * **It never refuses the boot.**  This ships into the server running the
      strangler work, so a self-inflicted boot failure would be worse than the
      condition it reports.  An operator whose only mistake was leaving a
      variable unset must not lose the server.

    A SECOND predicate guards the flip itself: a boot requesting ``on`` while
    any callback barrier is OPEN resolves to ``shadow`` with
    ``DIAG-BARRIER-OPEN-AT-FLIP``.  Every barrier opened AFTER the flip
    associates normally through the queue's own enqueue, so this covers the one
    case association cannot — a barrier already open at the moment of the flip,
    whose members would otherwise be split across the legacy inbox and the
    queue.  It is a guard rather than advice about flipping at a quiet moment,
    for the same reason the occupancy predicate is: an operator cannot be asked
    to check a condition the server can check itself.

    This is a BOOT-TIME resolution only.  There is no runtime transition: a
    position changes when the server restarts and at no other moment, so a
    reader should not go looking for one.
    """
    context = {
        "requested": requested.value,
        "outstanding": str(occupancy.live_non_terminal),
    }

    if requested is SwitchPosition.ON and occupancy.open_barrier_labels:
        labels = ", ".join(occupancy.open_barrier_labels)
        return GuardOutcome(
            requested=requested,
            position=SwitchPosition.SHADOW,
            finding=FindingCode.DIAG_BARRIER_OPEN_AT_FLIP,
            detail=(
                "held the write-through flip at shadow: callback barriers still "
                f"OPEN ({labels}); their members would be split across the legacy "
                "inbox and the queue"
            ),
            context={**context, "open_barriers": labels},
        )

    if requested is SwitchPosition.ON:
        # The queue is being served either way, so outstanding rows are not a
        # problem to report — they are the workload.
        return GuardOutcome(requested=requested, position=SwitchPosition.ON, context=context)

    if requested is SwitchPosition.DRAIN:
        if occupancy.occupied:
            return GuardOutcome(requested=requested, position=SwitchPosition.DRAIN, context=context)
        return GuardOutcome(
            requested=requested,
            position=SwitchPosition.SHADOW,
            finding=FindingCode.DIAG_QUEUE_ORPHAN_GUARD,
            detail=(
                "drain complete: no live non-terminal rows remain, so the "
                "requested drain resolved to shadow"
            ),
            context=context,
        )

    # off and shadow: identical treatment, since neither serves the queue.
    if occupancy.occupied:
        return GuardOutcome(
            requested=requested,
            position=SwitchPosition.DRAIN,
            finding=FindingCode.DIAG_QUEUE_ORPHAN_GUARD,
            detail=(
                f"resolved {requested.value} to drain: {occupancy.live_non_terminal} live "
                "non-terminal delivery_msg row(s) would otherwise be orphaned in a table "
                "nothing serves"
            ),
            context=context,
        )
    return GuardOutcome(requested=requested, position=requested, context=context)


def compute_dead_by(
    *,
    created_at: datetime,
    available_at: datetime,
    expire_after_s: int | None = None,
    max_lifetime_s: int = DELIVERY_MAX_LIFETIME_S,
) -> datetime:
    """D12's deadline, stamped ONCE at enqueue and never recomputed.

    ``max(created_at, available_at) + DELIVERY_MAX_LIFETIME_S``, or the earlier
    of that and the caller's own expiry when one was supplied (D8).

    **The once-only stamp is the load-bearing half of the rule**, and it is a
    property of the CALLER, not of this function: the formula names
    ``available_at``, a column ``reclaim`` rewrites on every re-offer, so
    recomputing the deadline from the current value would extend it unboundedly
    and the row would never die.  That is the mutant the empirical gate must
    kill, and it is cheap to write given how much of the design rests on the
    deadline being fixed.  Nothing here can prevent it; the store's ``UPDATE``
    statements are written so they never touch the column, and a test asserts
    the deadline is unchanged across a re-offer.

    Enqueue rather than first lease, because that is the origin the property is
    about.  The legacy notice runs on message AGE (``notice_due`` tests
    ``activity_age`` and ``newest_age``, both from creation), so a deadline
    running from first lease would bound a different quantity: the row's life
    from enqueue would be the delay to first lease plus the lifetime, against a
    margin of 100 seconds — less than a redeploy, and less than a claim backlog
    of the kind #604 itself produced with nine callbacks arriving together.
    Stamping at enqueue also gives a NEVER-LEASED row a deadline at all, which
    I1 requires.

    The ``max`` term is defensive.  R1 holds ``available_at == created_at`` at
    every phase-3 enqueue — the three enqueue points take no delay parameter, so
    the condition holds by construction rather than by discipline — and I4 is
    derived under that.  The term is present so a future delayed enqueue could
    not kill a message before it is due; it does not thereby PERMIT one.  A
    phase that introduces a delay must first add a bounded
    ``DELIVERY_MAX_ENQUEUE_DELAY_S`` and re-derive I4, because I4 is a relation
    over constants and a delay that is not a constant cannot violate it.  The
    property would otherwise be lost silently in a phase with no reason to
    revisit this invariant.
    """
    origin = max(created_at, available_at)
    deadline = origin + timedelta(seconds=max_lifetime_s)
    if expire_after_s is not None and expire_after_s > 0:
        # A caller expiry can only bring death FORWARD, exactly like the attempt
        # budget and the dialog ceiling, so I4's chain is unchanged.  Dropping
        # the parameter at the flip would have made every expiring message
        # non-expiring, delivered up to DELIVERY_MAX_LIFETIME_S late — which is
        # #435's incident reintroduced by the phase that closes it.
        deadline = min(deadline, created_at + timedelta(seconds=expire_after_s))
    return deadline


# ---------------------------------------------------------------------------
# The rows, as values.
#
# Pydantic models rather than dataclasses, matching ``core/events.py`` and
# ``core/findings.py``: the validators are what stop a naive datetime reaching a
# TEXT column that is compared as a string, and phase 1 learned that lesson in
# its own DDL.  Frozen and ``extra="forbid"`` throughout — a row that silently
# accepted an unknown keyword would let a typo'd column name vanish.
# ---------------------------------------------------------------------------


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("delivery timestamps must be timezone-aware (UTC)")
    return value


class MsgKind(StrEnum):
    """The audit's ``kind`` column: what sort of message this row carries.

    ``NOTE`` covers every server-generated message — the orphan sender notice,
    the barrier escalation, the deferred-init failure notice.  Sub-phase 3a
    mirrors ``send_message`` traffic only, so it writes ``CALLBACK`` for a
    message whose receiver is the sender's own caller and ``NOTE`` otherwise;
    ``ASSIGN`` and ``HANDOFF`` exist because the audit names them and because
    3b's write-through will not want to widen an enum to use them.
    """

    ASSIGN = "assign"
    CALLBACK = "callback"
    HANDOFF = "handoff"
    NOTE = "note"


class EnqueueDraft(BaseModel):
    """What a caller hands the queue.  The store mints ``msg_id`` and the times.

    ``idempotency_key`` is CALLER-SUPPLIED and identifies one logical send, per
    audit §3.2.  It is emphatically not derived from message content: an earlier
    draft proposed exactly that, letting a ``UNIQUE`` constraint do the
    deduplication, and it would have been silent message loss.  The constraint
    has neither a window nor any of F475's other conjuncts, so two identical
    sends more than a minute apart — ordinary traffic here — would collide, and
    ``INSERT … ON CONFLICT DO NOTHING`` would hand the caller the FIRST
    message's id while the second was never delivered.  Content dedup is a
    separate bounded window check over ``content_hash`` (D13), with all five of
    F475's conjuncts intact.

    In sub-phase 3a the key is derived from the legacy inbox row id, because
    there the enqueue is a mirror of a send that has already been identified —
    the legacy row id IS the caller-supplied identity, and deriving it that way
    makes the mirror hook idempotent under a retry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str = Field(min_length=1)
    receiver_id: str = Field(min_length=1)
    sender_id: str = ""
    kind: MsgKind = MsgKind.NOTE
    payload: str = ""
    mode: QueueMode = QueueMode.SHADOW
    max_attempts: int = Field(default=DELIVERY_MAX_ATTEMPTS, ge=1)
    expire_after_s: int | None = None
    supersede_key: str | None = None
    content_hash: str | None = None
    park_warm: bool = False
    barrier_id: int | None = None
    barrier_member_key: str | None = None
    enqueue_generation: int | None = None
    cancel_on_complete: bool = False
    is_notice: bool = False
    legacy_message_id: int | None = None


class QueueMessage(BaseModel):
    """One stored ``delivery_msg`` row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    msg_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload_digest: str = ""
    receiver_id: str = Field(min_length=1)
    sender_id: str = ""
    kind: MsgKind = MsgKind.NOTE
    payload: str = ""
    state: MsgState = MsgState.READY
    mode: QueueMode = QueueMode.SHADOW
    claim_id: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = DELIVERY_MAX_ATTEMPTS
    available_at: datetime
    #: D12's deadline.  Stamped once at enqueue; no store statement writes it.
    dead_by: datetime
    #: The dialog-hold clock (D12).  Set on the FIRST ``veto_dialog`` outcome and
    #: cleared by any other outcome, so a recovered row does not carry a stale
    #: one.  With ``dead_by`` fixed the clearing rule stops mattering for safety,
    #: which is why it can be this simple.
    held_since: datetime | None = None
    expire_after_s: int | None = None
    supersede_key: str | None = None
    content_hash: str | None = None
    park_warm: bool = False
    barrier_id: int | None = None
    barrier_member_key: str | None = None
    enqueue_generation: int | None = None
    cancel_on_complete: bool = False
    is_notice: bool = False
    legacy_message_id: int | None = None
    created_at: datetime
    terminated_at: datetime | None = None

    @field_validator("available_at", "dead_by", "created_at")
    @classmethod
    def _aware_required(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("lease_expires_at", "held_since", "terminated_at")
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class DeliveryAttempt(BaseModel):
    """One row of ``delivery_attempt`` — I5's "one query, full history".

    Keyed ``(msg_id, claim_id, carrier)``, so a row's whole delivery history is
    one indexed read rather than pane archaeology.  In sub-phase 3a the mirror
    writer uses the legacy attempt's ORDINAL as ``claim_id``: shadow rows are
    never leased, so no real claim ever issues one, and the ordinal is
    deterministic, which keeps the mirror idempotent when the same legacy
    attempt is observed twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    msg_id: str = Field(min_length=1)
    claim_id: int = 0
    carrier: str = Field(min_length=1)
    started_at: datetime
    outcome: AttemptOutcome
    detail: str = ""

    @field_validator("started_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class DeadLetter(BaseModel):
    """One row of ``delivery_dead``.

    A separate table rather than a status flag, so a poisoned message stops
    occupying the reclaim loop and hiding live rows.  ``mode`` is carried here
    although the audit's DDL omits it: without it a shadow row that mirrored a
    legacy expiry would be indistinguishable from a live dead-letter, and
    AC-3a's counting turns on exactly that distinction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    msg_id: str = Field(min_length=1)
    idempotency_key: str = ""
    receiver_id: str = ""
    payload: str = ""
    attempts: int = 0
    reason: DeadReason
    mode: QueueMode = QueueMode.SHADOW
    died_at: datetime

    @field_validator("died_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class SeatDigest(BaseModel):
    """One ``seat_digest`` row: the ids owed to one receiver in one epoch.

    ``msg_ids`` holds IDS, never bodies — a wake costs one line of seat context
    rather than N message bodies, which is the CLAUDE.md context-hygiene rule
    made structural.

    The array is immutable except on ONE event: re-parenting.  When a reap moves
    a row to a surviving ancestor's mailbox, the same transaction that moves
    ``receiver_id`` removes the id from the old receiver's open epoch and adds
    it to the new one.  Without that the moved id would stay listed in an epoch
    it no longer belongs to, never reach a terminal state there, and hold that
    epoch open forever while the tick re-woke it once per lease — an open digest
    that cannot close, which I6 forbids.

    ``receiver_id`` is the durable MAILBOX id, not a terminal id.  That is what
    makes #33 closeable: a fresh incarnation is a new generation of the same
    mailbox and inherits the pending rows and the open digest, rather than
    starting behind an empty registry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    receiver_id: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    msg_ids: tuple[str, ...] = ()
    built_at: datetime
    consumed_at: datetime | None = None
    consumed_via: str | None = None

    @field_validator("built_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("consumed_at")
    @classmethod
    def _aware_optional(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)

    @property
    def open(self) -> bool:
        return self.consumed_at is None

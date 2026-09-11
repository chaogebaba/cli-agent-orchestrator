"""The digest's two carriers, split by the receiver's role (D7 as amended, §A1).

**The split is by construction, not by a conditional inside one injector.**  The
tick resolves the digest's receiver before it emits and dispatches to one of two
functions:

* a **worker** receiver goes to :meth:`WakeService.inject_worker`, D7's seam
  unchanged — the composer paste, with legacy's vetoes in force;
* a **supervisor-seat** receiver goes to :meth:`WakeService.wake_seat`, the
  native cross-session channel, which never touches a pane.

:meth:`wake_seat` holds NO reference to the pane injector and :meth:`inject_worker`
holds none to the carrier.  That is what makes K8's kill a property of the call
graph rather than of a conditional a later edit can invert, and it is why the
build note says these are two functions and not one function with a flag.
``inject_worker`` re-asserts the role probe at entry and REFUSES a supervisor
target with ``paste_attempted`` rather than pasting, so a future caller that
routes wrongly is loud rather than silent.

**Why the split is role-shaped and not a global replacement.**  The native
channel is a Claude Code harness feature: a wake is a line written to the
session's ``messagingSocketPath``, and only Claude Code sessions publish one.
Codex, kiro, cline and grok workers have no such socket and no registry record,
so for them the composer IS the only channel — killing the paste path outright
would leave every worker unreachable.  The seat is also the one receiver where a
paste has a second cost: it lands in a composer a human types into, which is why
``draft_guard`` exists at all and why F210 already forbids composer injection to
supervisor-role targets on the rung-2 ladder.  A1 finishes that rule instead of
inventing one.

**What a failure means is decided by a table, not by the caller.**
:func:`~core.delivery.classify_wake_reason` is total over the carrier's closed
reason set, and every branch below reads it rather than re-deciding.  A phase
that adds a refusal string classifies it there before it ships.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from cli_agent_orchestrator.core.delivery import (
    CARRIER_PANE,
    CARRIER_SEAT_WAKE,
    SENDER_SUBSTITUTED,
    UNVERIFIED_STREAK_LEASES,
    WAKE_PANE_ABSENT,
    WAKE_PASTE_ATTEMPTED,
    WAKE_UNVERIFIED_STREAK,
    AttemptOutcome,
    DeliveryAttempt,
    QueueMessage,
    ReceiverResolution,
    SeatDigest,
    build_digest_line,
    classify_wake_reason,
    resolve_wake_sender,
)
from cli_agent_orchestrator.core.ports import (
    Clock,
    PaneInjector,
    QueueStore,
    ReceiverDirectory,
    SeatCarrier,
)

logger = logging.getLogger(__name__)

__all__ = ["WakeOutcomeReport", "WakeService"]


@dataclass(frozen=True)
class WakeOutcomeReport:
    """What one epoch's wake did, in the vocabulary the tick records.

    ``finding_reason`` is the reason ``DIAG-SEAT-WAKE-UNREACHABLE`` carries, or
    ``None`` when no finding is owed.  It is a separate field from ``outcome``
    because the two do not coincide: ``unverified_streak`` raises the finding
    while changing no bound and leaving the outcome an emitted one, and
    ``record_stale`` changes neither.
    """

    receiver_id: str
    epoch: int
    carrier: str
    outcome: AttemptOutcome
    emitted: bool = False
    #: Whether this report should produce ``delivery_attempt`` rows at all.
    #: False for the one case where no carrier ran — the epoch closed between
    #: the tick reading it and the emission — because an attempt row states
    #: what a carrier DID, and inventing one would put a delivery on the record
    #: that never happened.
    recordable: bool = True
    detail: str = ""
    finding_reason: str | None = None
    wake_count: int = 0
    line: str = ""
    annotations: tuple[str, ...] = field(default_factory=tuple)


class WakeService:
    """Dispatches one epoch's digest to the carrier its receiver's role dictates.

    Holds the two ports side by side and hands each row to exactly one of them.
    Nothing here reads a switch position: the muting of D6's surfaces is the
    tick's business and the paste ban is the ROLE's, which is what makes the ban
    hold under ``off``, ``drain`` and ``on`` alike (§A1.5).
    """

    def __init__(
        self,
        *,
        store: QueueStore,
        directory: ReceiverDirectory,
        carrier: SeatCarrier,
        injector: PaneInjector,
        clock: Clock,
    ) -> None:
        self._store = store
        self._directory = directory
        self._carrier = carrier
        self._injector = injector
        self._clock = clock

    # -- dispatch -----------------------------------------------------------

    def deliver(self, digest: SeatDigest, claimed: tuple[QueueMessage, ...]) -> WakeOutcomeReport:
        """Resolve the receiver's role, then emit through exactly one carrier.

        The role test is the one the codebase already trusts, reused rather than
        re-invented, and it is FAIL-CLOSED: an unanswerable probe routes the row
        to the seat carrier, because the cost of a wrong ``False`` is typing into
        the user's own pane.  The misclassification cost is stated rather than
        hidden — a worker wrongly read as a seat is never pasted and its rows die
        at ``dead_by`` with a finding, which is the safe direction and is why the
        probe fails this way.
        """
        resolution = self._directory.resolve(digest.receiver_id)
        if not resolution.live:
            # Zero live terminals: the digest STAYS OPEN, the attempt writes
            # pane_absent, and the rows age toward delivery_dead on their own
            # budget (D10).  Nothing is emitted, so the ordinal does not move —
            # a wake nobody could have received must not consume one of I3's
            # lease periods.
            return WakeOutcomeReport(
                receiver_id=digest.receiver_id,
                epoch=digest.epoch,
                carrier=CARRIER_PANE,
                outcome=AttemptOutcome.PANE_ABSENT,
                detail=WAKE_PANE_ABSENT,
            )

        line, wake_count = self._compose(digest, resolution)
        if wake_count == 0:
            # The epoch closed between the tick reading it and this emission.  A
            # wake for a consumed epoch is UNREACHABLE rather than suppressed
            # (I4), and there is nothing to record against the rows.
            #
            # ``recordable`` is false here and nowhere else. An attempt row says
            # what a CARRIER DID, and no carrier ran: writing ``delivered``
            # would tell ``cao diag <msg_id>`` that a wake landed when none was
            # composed, which is the pane-archaeology guesswork I5 exists to
            # end. It would also be an outcome that spends no attempt, so the
            # rows would re-offer to their deadline with a delivery on the
            # record and nothing delivered.
            return WakeOutcomeReport(
                receiver_id=digest.receiver_id,
                epoch=digest.epoch,
                carrier=CARRIER_SEAT_WAKE if resolution.is_supervisor else CARRIER_PANE,
                outcome=AttemptOutcome.DELIVERED,
                emitted=False,
                recordable=False,
                detail="epoch_closed",
            )

        if resolution.is_supervisor:
            return self.wake_seat(digest, resolution, line=line, wake_count=wake_count)
        return self.inject_worker(digest, resolution, line=line, wake_count=wake_count)

    # -- the seat's carrier -------------------------------------------------

    def wake_seat(
        self,
        digest: SeatDigest,
        resolution: ReceiverResolution,
        *,
        line: str,
        wake_count: int,
    ) -> WakeOutcomeReport:
        """Emit one wake over the native channel.  Never touches a pane.

        The sender is the WORKER and never the receiver, resolved by A1.1's
        three-case rule; the self-addressed case is a SUBSTITUTION and not a
        refusal, because refusing would strand a server-generated notice
        addressed to the seat behind a guard until its deadline.

        ``msg_id`` is deterministic over ``(receiver_id, epoch, wake_count)``, so
        a re-emission of one wake carries one id and a receiver can dedupe it.
        """
        senders = self._store.senders_of(digest.msg_ids)
        sender = resolve_wake_sender(senders, receiver_key=resolution.receiver_id)
        annotations: list[str] = []
        if sender.substituted:
            annotations.append(SENDER_SUBSTITUTED)

        emission = self._carrier.emit(
            terminal_id=resolution.terminal_id,
            line=line,
            sender_key=sender.key,
            sender_name=sender.name,
            msg_id=self._wake_msg_id(digest, wake_count),
        )
        annotations.extend(emission.annotations)

        reason = emission.reason
        if reason is None and not emission.verified:
            # Written, confirmation not obtained.  NOT a refusal: verify_wake
            # polls a timestamp Claude Code writes, so a seat that is compacting
            # or simply slow fails the poll with the message sitting in its
            # queue.  Counting it as unreachable would raise the finding against
            # every busy seat.
            reason = "wake_unverified"

        classification = classify_wake_reason(reason)
        finding_reason = classification.reason if classification.finding else None
        if classification.outcome is AttemptOutcome.EMITTED_UNVERIFIED:
            finding_reason = self._streak_reason(digest)

        detail = "|".join(filter(None, [classification.reason, *annotations, emission.detail]))
        return WakeOutcomeReport(
            receiver_id=digest.receiver_id,
            epoch=digest.epoch,
            carrier=CARRIER_SEAT_WAKE,
            outcome=classification.outcome,
            emitted=classification.emitted,
            detail=detail,
            finding_reason=finding_reason,
            wake_count=wake_count,
            line=line,
            annotations=tuple(annotations),
        )

    # -- the worker's seam --------------------------------------------------

    def inject_worker(
        self,
        digest: SeatDigest,
        resolution: ReceiverResolution,
        *,
        line: str,
        wake_count: int,
    ) -> WakeOutcomeReport:
        """Paste one digest line into a worker's composer.  D7's seam unchanged.

        The role probe is re-asserted here rather than trusted from the
        dispatch.  A seat row that reaches this function did so through a
        dispatch DEFECT, and the dispatch is deterministic — the next lease
        routes it identically — so it is refused with ``paste_attempted``,
        spends the attempt budget and dies at 325 s with the finding, on D12's
        own principle that a condition which cannot clear should die faster.
        """
        if resolution.is_supervisor:
            logger.error(
                "delivery: a supervisor-seat receiver reached the worker injector "
                "(receiver=%s epoch=%s) — refusing the paste",
                digest.receiver_id,
                digest.epoch,
            )
            classification = classify_wake_reason(WAKE_PASTE_ATTEMPTED)
            return WakeOutcomeReport(
                receiver_id=digest.receiver_id,
                epoch=digest.epoch,
                carrier=CARRIER_PANE,
                outcome=classification.outcome,
                emitted=False,
                detail=WAKE_PASTE_ATTEMPTED,
                finding_reason=WAKE_PASTE_ATTEMPTED,
                wake_count=wake_count,
            )

        result = self._injector.inject(terminal_id=resolution.terminal_id, line=line)
        return WakeOutcomeReport(
            receiver_id=digest.receiver_id,
            epoch=digest.epoch,
            carrier=CARRIER_PANE,
            outcome=result.outcome,
            emitted=result.outcome is AttemptOutcome.DELIVERED,
            detail=result.detail,
            wake_count=wake_count,
            line=line,
        )

    # -- internals ----------------------------------------------------------

    def _compose(self, digest: SeatDigest, resolution: ReceiverResolution) -> tuple[str, int]:
        """Advance the ordinal and build the line, in that order.

        The ordinal is bumped in the transaction that OPENS this lease's wake,
        and the line carries the current value.  Bumping after a successful write
        instead would make a refused wake re-send a byte-identical line on the
        next lease, which the transport's content window would then swallow —
        the silent failure §A1.2 exists to remove, reintroduced at the one point
        it is hardest to see.
        """
        wake_count = self._store.bump_wake_count(
            digest.receiver_id, digest.epoch, now=self._clock.now()
        )
        if wake_count == 0:
            return "", 0
        line = build_digest_line(
            epoch=digest.epoch,
            msgs=len(digest.msg_ids),
            wake=wake_count,
            msg_ids=digest.msg_ids,
        )
        return line, wake_count

    @staticmethod
    def _wake_msg_id(digest: SeatDigest, wake_count: int) -> str:
        """Deterministic over ``(receiver_id, epoch, wake_count)`` (§A1.1).

        NOT the legacy ``(worker, inbox_row_id, incarnation)`` key: the digest
        carries no row id, and a builder reusing the old tuple would key a wake
        on a row the design has replaced.  The wrapper supplies this key; the
        envelope and socket framing below it are untouched.
        """
        return f"{digest.receiver_id}:{digest.epoch}:{wake_count}"

    def _streak_reason(self, digest: SeatDigest) -> str | None:
        """``unverified_streak`` after three consecutive unconfirmed leases.

        DERIVED, not stored: the last three wake attempts for the epoch, which
        I5 already requires to exist, so this adds no column.  It changes no
        bound — the row stays on its ordinary lease, because the wake may well be
        landing — and it is the one place in A1 where a finding reports a
        suspicion rather than a settled outcome, which is why it takes a streak
        rather than a single sample.

        Three leases is 180 s or more, comfortably past a compaction, so the
        finding stays out of normal operation while a session that is
        dead-but-listening — writing no errno and moving no ``statusUpdatedAt`` —
        becomes diagnosable AS an unreachable seat.
        """
        recent = self._recent_wake_attempts(digest, limit=UNVERIFIED_STREAK_LEASES - 1)
        if len(recent) < UNVERIFIED_STREAK_LEASES - 1:
            return None
        if all(a.outcome is AttemptOutcome.EMITTED_UNVERIFIED for a in recent):
            return WAKE_UNVERIFIED_STREAK
        return None

    def _recent_wake_attempts(
        self, digest: SeatDigest, *, limit: int
    ) -> tuple[DeliveryAttempt, ...]:
        attempts: list[DeliveryAttempt] = []
        for msg_id in digest.msg_ids:
            attempts.extend(
                a for a in self._store.attempts_for(msg_id) if a.carrier == CARRIER_SEAT_WAKE
            )
        attempts.sort(key=lambda a: a.started_at, reverse=True)
        # One emission writes one attempt row PER message, so the same lease's
        # rows are identical in outcome and adjacent in time; de-duplicate by the
        # started_at stamp so "three leases" counts leases and not messages.
        seen: set[datetime] = set()
        distinct: list[DeliveryAttempt] = []
        for attempt in attempts:
            if attempt.started_at in seen:
                continue
            seen.add(attempt.started_at)
            distinct.append(attempt)
            if len(distinct) == limit:
                break
        return tuple(distinct)

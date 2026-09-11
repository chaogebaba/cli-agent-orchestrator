"""The delivery tick — §5c's polling safety net, and the only scheduled observer.

D2 makes the wake a HINT, which holds only if something observes the durable
rows on a schedule no wake path can suppress.  This is that something: one
asyncio task in the single process, on the pattern of the retention sweeper
phase 1 ships.

**Server-side is the load-bearing property, not a placement choice.**  The
safety net that failed in #604 was a client-side watcher armed by seat events,
which cannot cover an idle gap because an idle seat emits nothing to arm it.  A
poll inside the server process has no such precondition: it runs while the
server runs, and the server is what holds the rows.

Three steps per tick, in this order:

0. :meth:`DeliveryTick.adopt` — any PENDING legacy ``inbox`` row with no
   ``delivery_msg`` counterpart is enqueued and the legacy row retired, in one
   transaction on the legacy side.  It runs FIRST so a row adopted this tick is
   served by the same tick rather than waiting ten seconds for the next one.
   Before 3c such rows had two legacy carriers; that slice deletes both, and
   this is what replaces them.  A row reaching the inbox at ``on`` is a
   write-through that lost its ``BEGIN IMMEDIATE`` race, or a row that predates
   the flip — neither is hypothetical, and with no adoption neither has any
   carrier at all, which is #604.
1. :meth:`DeliveryTick.reclaim` — expired leases back to ``ready``, ``attempts``
   incremented for the outcomes on that budget alone, rows past a bound moved to
   ``delivery_dead``.  Every death is reported, never counted: a time-bound death
   raises ``DIAG-DELIVERY-TIME-BOUND`` and every non-notice death enqueues the
   seat-visible sender notice that replaces K7's escalation line, because "no row
   reaches ``delivery_dead`` silently" is the commitment (§13d, case 15).
2. :meth:`DeliveryTick.serve` — claim, open or extend the epoch, emit ONE wake
   through the carrier the receiver's role dictates, and close the epochs whose
   messages have all ended.

**One wake per epoch per lease period, and the mechanism is the claim.**  The
tick emits only when it actually claimed rows for that receiver, and a row stays
leased for ``DELIVERY_LEASE_S``, so a second tick inside one lease claims nothing
and emits nothing.  I3 is therefore a property of the claim statement rather than
a timestamp comparison someone has to remember to write, and the ordinal advances
"once per lease period in which the epoch is re-offered" by construction (§A1.2).

**One cadence rather than two.**  pg-boss slows its interval while notifications
are live, but that fast path is chosen by whether notifications arrive, and D2
declines to let a wake path change the safety net.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from cli_agent_orchestrator.app.delivery.wake import WakeOutcomeReport, WakeService
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    DeadRow,
    DeliveryAttempt,
    EnqueueDraft,
    LegacyAdoption,
    MsgKind,
    QueueMessage,
    QueueMode,
    SeatDigest,
    SwitchPosition,
    is_service_sender,
)
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ports import (
    Clock,
    FindingStore,
    LegacyInboxAdopter,
    QueueStore,
    ReceiverDirectory,
)
from cli_agent_orchestrator.core.timing import DELIVERY_TICK_S

logger = logging.getLogger(__name__)

__all__ = ["DeliveryTick", "TickReport", "TICK_LEASE_OWNER"]

#: The lease owner every tick claims under.  One process, one owner: the fencing
#: token is what separates two claims, not the owner string, and a per-tick uuid
#: would make a stolen lease indistinguishable from a restarted server.
TICK_LEASE_OWNER = "delivery-tick"

#: How many rows one claim takes for one receiver.  A bound rather than a
#: throughput knob: the digest is one line whatever ``k`` is, and an unbounded
#: claim would let one receiver's backlog hold the write lock for the whole
#: fleet.  Rows above it are claimed on the next tick, ten seconds later.
CLAIM_LIMIT = 64

#: How many orphaned legacy rows one tick adopts.  The same bound as
#: ``CLAIM_LIMIT`` and for the same reason: adoption writes, and an unbounded
#: pass over a large pre-flip backlog would hold the write lock for the fleet.
#: Rows above it are adopted on the next tick.
ADOPT_LIMIT = 64

#: The deaths that mean a TIME bound ended the row rather than an attempt bound.
#: ``max_attempts`` is deliberately absent: an operator reading
#: ``DIAG-DELIVERY-TIME-BOUND`` learns something specific from it, and a code that
#: fires for every death would say only "a row died".
_TIME_BOUND_REASONS = frozenset(
    {DeadReason.MAX_LIFETIME, DeadReason.VETO_CEILING, DeadReason.EXPIRED}
)


@dataclass
class TickReport:
    """What one tick did.  Returned so a test can assert rather than infer."""

    adopted: tuple[LegacyAdoption, ...] = ()
    reoffered: int = 0
    incremented: int = 0
    dead: tuple[DeadRow, ...] = ()
    notices_enqueued: int = 0
    epochs_opened: int = 0
    epochs_closed: int = 0
    wakes: tuple[WakeOutcomeReport, ...] = field(default_factory=tuple)
    pruned: int = 0

    @property
    def emitted(self) -> int:
        return sum(1 for wake in self.wakes if wake.emitted)


class DeliveryTick:
    """The queue's scheduled observer, and the epoch's lifecycle owner."""

    def __init__(
        self,
        *,
        store: QueueStore,
        wake: WakeService,
        directory: ReceiverDirectory,
        findings: FindingStore | None,
        clock: Clock,
        position: SwitchPosition,
        interval_s: float = DELIVERY_TICK_S,
        adopter: LegacyInboxAdopter | None = None,
    ) -> None:
        self._store = store
        self._wake = wake
        self._directory = directory
        self._findings = findings
        self._clock = clock
        self._position = position
        self._adopter = adopter
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._ticks = 0

    @property
    def ticks(self) -> int:
        """How many tick bodies have completed — the task's own liveness proof."""
        return self._ticks

    # -- the task -----------------------------------------------------------

    async def start(self) -> None:
        """Register the one asyncio task.  Idempotent."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="delivery-tick")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick that raises must not end the task
                # The safety net cannot have a liveness precondition of its own.
                # A tick body that dies on one bad row and takes the task with it
                # is the client-side watcher's failure mode reproduced inside the
                # server, which is the whole thing §5c exists to replace.
                logger.exception("delivery tick raised; the task continues")
            await asyncio.sleep(self._interval_s)

    # -- one tick -----------------------------------------------------------

    def run_once(self, *, now: datetime | None = None) -> TickReport:
        """Adopt, reclaim, then build or re-emit.  The order is the contract.

        **The stamp is taken AFTER the adoption, and that ordering is load
        bearing.** An adopted row is enqueued with the clock's reading at the
        moment of the enqueue, so its ``available_at`` is later than a stamp read
        at the top of the tick.  The claim filters on ``available_at <= now``, so
        a stamp taken first makes every row this tick just adopted invisible to
        the serve step that follows it — the row waits a full interval for a wake
        it could have had immediately, and a one-shot ``run_once`` in a test
        observes no emission at all.  Reading the clock after adoption keeps the
        stamp at least as late as the newest row the tick created.

        That holds only on the ``now is None`` branch.  A caller passing an
        explicit stamp MUST pass one at least as late as the adoption, or it gets
        the old behaviour back for its own rows; production never passes one
        (:meth:`_run` calls this bare), but tests do.
        """
        report = TickReport()
        self.adopt(report)
        stamp = now if now is not None else self._clock.now()
        self.reclaim(stamp, report)
        self.serve(stamp, report)
        self._ticks += 1
        return report

    def adopt(self, report: TickReport) -> None:
        """Pull orphaned legacy ``inbox`` rows into the queue (WP-ARCH 3c).

        The tick owns the schedule and the observability; the adopter owns the
        writes, because only legacy can read that table, and it enqueues BEFORE
        it retires so that a crash between the two leaves the row delivered once
        rather than owned by nobody (see
        :class:`~cli_agent_orchestrator.core.ports.LegacyInboxAdopter`).

        One INFO line per adopted row, naming BOTH ids, because the operator
        holding one of them has no way to reach the other by hand.  One finding
        per receiver, counted rather than accumulated: adoption is the FALLBACK,
        so the number is the signal — a count that climbs says the write-through
        keeps losing its race, which is a defect to fix rather than a steady
        state to tolerate.

        No adopter wired is not an error: a tick built for a position that does
        not serve, or by a test with doubles, simply has nothing to adopt.
        """
        if self._adopter is None:
            return
        try:
            adoptions = tuple(self._adopter.adopt_orphans(limit=ADOPT_LIMIT))
        except Exception:  # noqa: BLE001 — the net may not take the tick down
            # Deliberately broad, and for the same reason ``_run`` is: an adopter
            # that raises must cost this tick its adoptions, never its serve.
            logger.exception("delivery tick: legacy adoption failed")
            return
        report.adopted = adoptions
        for adoption in adoptions:
            logger.info(
                "delivery_adopt legacy_id=%s msg_id=%s receiver=%s",
                adoption.legacy_message_id,
                adoption.msg_id,
                adoption.receiver_id,
            )
        self._raise_adoption_findings(adoptions)

    def _raise_adoption_findings(self, adoptions: tuple[LegacyAdoption, ...]) -> None:
        """``DIAG-LEGACY-ROW-ADOPTED``, once per receiver per tick.

        The store dedupes on ``(code, terminal_id, dedupe_key)`` and increments,
        so a receiver adopted from on many ticks holds ONE finding whose count is
        the number of ticks that adopted for it. ``detail`` keeps the first
        batch's size and a sample legacy id — the first occurrence is the one
        whose surrounding timeline still explains anything.
        """
        if self._findings is None or not adoptions:
            return
        by_receiver: dict[str, list[LegacyAdoption]] = {}
        for adoption in adoptions:
            by_receiver.setdefault(adoption.receiver_id, []).append(adoption)
        for receiver_id, rows in by_receiver.items():
            self._findings.record(
                FindingCode.DIAG_LEGACY_ROW_ADOPTED,
                terminal_id=receiver_id,
                dedupe_key=receiver_id,
                detail=f"rows={len(rows)} first_legacy_id={rows[0].legacy_message_id}",
            )

    def reclaim(self, now: datetime, report: TickReport) -> None:
        result = self._store.reclaim(now=now)
        report.reoffered = result.reoffered
        report.incremented = result.incremented
        report.dead = result.dead
        for row in result.dead:
            self._announce_death(row, now, report)

    def serve(self, now: datetime, report: TickReport) -> None:
        for receiver_id in self._store.ready_receivers():
            try:
                self._serve_receiver(receiver_id, now, report)
            except Exception:  # noqa: BLE001 — one receiver must not stop the fleet
                logger.exception("delivery tick: receiver %s failed", receiver_id)
        self._close_finished_epochs(now, report)

    # -- per receiver -------------------------------------------------------

    def _serve_receiver(self, receiver_id: str, now: datetime, report: TickReport) -> None:
        claimed = tuple(
            self._store.claim(
                lease_owner=TICK_LEASE_OWNER,
                now=now,
                limit=CLAIM_LIMIT,
                receiver_id=receiver_id,
            )
        )
        digest = self._epoch_for(receiver_id, now, report)
        if digest is None:
            logger.debug("delivery_wake receiver=%s decision=no_open_epoch", receiver_id)
            return
        if not claimed:
            # Every row is still inside its lease, so this is not a new lease
            # period and I3 forbids a second wake.  Emitting here would also
            # re-send a byte-identical line, which the transport's content window
            # would swallow — a wake reported as sent and never written.
            logger.debug(
                "delivery_wake receiver=%s epoch=%s decision=inside_lease",
                receiver_id,
                digest.epoch,
            )
            return
        wake = self._wake.deliver(digest, claimed)
        self._log_wake(wake, len(claimed))
        report.wakes = (*report.wakes, wake)
        if wake.recordable:
            self._record_attempts(wake, claimed, now)
        self._raise_wake_finding(wake)

    @staticmethod
    def _log_wake(wake: WakeOutcomeReport, claimed: int) -> None:
        """One line per emission, and it is load-bearing rather than decorative.

        Every non-emitting outcome this path can take is SILENT otherwise: the
        carrier returns a refusal string, the attempt row records it, and nothing
        reaches the log. The first live round under ``on`` found the seat quiet
        and could not say why, because the only surviving evidence was an
        ``attempts`` counter that four different outcomes leave at zero (#741).
        A refusal is therefore logged at WARNING and an emission at INFO, both
        naming the outcome, the carrier and the detail, so "which of the four"
        is answered by reading the log rather than by reasoning about the schema.
        """
        level = logging.INFO if wake.emitted else logging.WARNING
        logger.log(
            level,
            "delivery_wake receiver=%s epoch=%s carrier=%s outcome=%s emitted=%s "
            "wake=%s claimed=%d detail=%s",
            wake.receiver_id,
            wake.epoch,
            wake.carrier,
            wake.outcome.value,
            wake.emitted,
            wake.wake_count,
            claimed,
            wake.detail or "-",
        )

    def _epoch_for(self, receiver_id: str, now: datetime, report: TickReport) -> SeatDigest | None:
        """The receiver's open epoch, opened or extended to cover what is owed."""
        ids = self._store.undelivered_ids(receiver_id)
        if not ids:
            return None
        open_digest = self._store.open_digest(receiver_id)
        if open_digest is None:
            report.epochs_opened += 1
            return self._store.build_digest(receiver_id, ids, now=now)
        extended = self._store.extend_digest(receiver_id, open_digest.epoch, ids, now=now)
        return extended if extended is not None else open_digest

    def _record_attempts(
        self, wake: WakeOutcomeReport, claimed: tuple[QueueMessage, ...], now: datetime
    ) -> None:
        """One attempt row per claimed message, all naming this emission.

        Per message rather than per epoch because I5's promise is that ONE QUERY
        returns a msg_id's full history, and an operator holding a msg_id must
        not have to find the epoch first to learn that its wake was refused.
        """
        for message in claimed:
            self._store.record_attempt(
                DeliveryAttempt(
                    msg_id=message.msg_id,
                    claim_id=message.claim_id,
                    carrier=wake.carrier,
                    started_at=now,
                    outcome=wake.outcome,
                    detail=wake.detail,
                )
            )
            if wake.outcome is AttemptOutcome.VETO_DIALOG:
                if message.held_since is None:
                    self._store.mark_dialog_hold(message.msg_id, held_since=now)
            elif message.held_since is not None:
                # Any other outcome clears the clock, so a recovered row does not
                # carry a stale one (D12).
                self._store.mark_dialog_hold(message.msg_id, held_since=None)

    # -- findings and notices ------------------------------------------------

    def _raise_wake_finding(self, wake: WakeOutcomeReport) -> None:
        """``DIAG-SEAT-WAKE-UNREACHABLE``, once per OPEN EPOCH (O2).

        Per epoch rather than per row: ``k`` messages behind one dead socket are
        one condition, not ``k``, and the alternative puts the duplicate family
        into the diagnosis surface instead of the delivery surface.  The finding
        store's own dedupe on ``(code, terminal_id, dedupe_key)`` is the
        mechanism, so a repeat increments the standing finding's count rather
        than opening a second.
        """
        if wake.finding_reason is None or self._findings is None:
            return
        self._findings.record(
            FindingCode.DIAG_SEAT_WAKE_UNREACHABLE,
            terminal_id=wake.receiver_id,
            dedupe_key=f"{wake.receiver_id}:{wake.epoch}",
            detail=f"reason={wake.finding_reason} carrier={wake.carrier} epoch={wake.epoch}",
        )

    def _announce_death(self, row: DeadRow, now: datetime, report: TickReport) -> None:
        """A dead-letter is loud: the finding AND the sender's notice (§13d).

        Today a message undelivered past the ladder's threshold produces a line a
        human reads in the seat.  Deleting K7 without an equivalent would replace
        a visible escalation with a row in a table nobody is watching, and
        ``cao diag <msg_id>`` is pull-based.  So the transition does both.

        And the notice is where the seat's own carrier failing still reaches a
        human: the sender of a supervisor-bound callback is a WORKER, whose
        composer is not killed, so the notice lands in the place someone is
        already looking (§A1.4).
        """
        if self._findings is not None and row.reason in _TIME_BOUND_REASONS:
            self._findings.record(
                FindingCode.DIAG_DELIVERY_TIME_BOUND,
                terminal_id=row.receiver_id,
                dedupe_key=row.msg_id,
                detail=f"reason={row.reason.value} attempts={row.attempts}",
            )
        if row.is_notice or is_service_sender(row.sender_id):
            # D14: a dead-letter notice is never itself dead-lettered into
            # another notice.  The flagged row records the finding ALONE and
            # enqueues nothing, so the chain is one notice deep BY CONSTRUCTION
            # rather than by rate — the rate argument bounds a loop only by how
            # long receivers keep disappearing, and the second-order notice tells
            # a reader nothing the first did not.
            #
            # #741 r3: the empty-sender case widened to every SERVICE sender.
            # The docstring's justification above — "the sender of a
            # supervisor-bound callback is a WORKER, whose composer is not
            # killed" — is TRUE of workers and false of `watchdog:<terminal>`,
            # `message-trace:<terminal>` and the `cao-` writers. Those ids own no
            # terminal and no mailbox, so the notice could never be carried; it
            # was claimed, refused `no_terminal`, and re-woken every lease period
            # because a refusal does not terminate the row. The r2d live round
            # produced 20 such refusals across three service ids, and they are
            # what made its remaining 40 unreadable as acceptance evidence.
            #
            # The finding above still fires, so the death is not silenced — only
            # the undeliverable notice is not written.
            logger.debug(
                "delivery: no sender notice for %s — sender=%s is not addressable",
                row.msg_id,
                row.sender_id or "<empty>",
            )
            return
        try:
            self._store.enqueue(
                EnqueueDraft(
                    idempotency_key=f"delivery-notice:{row.msg_id}",
                    receiver_id=row.sender_id,
                    sender_id="",
                    kind=MsgKind.NOTE,
                    payload=(
                        f"[cao] undelivered: message {row.msg_id} to {row.receiver_id} "
                        f"ended {row.reason.value} after {row.attempts} attempts. "
                        f"Detail: cao diag {row.msg_id}"
                    ),
                    mode=QueueMode.LIVE,
                    is_notice=True,
                )
            )
            report.notices_enqueued += 1
        except Exception:  # noqa: BLE001 — a notice that cannot be written is logged
            logger.warning("delivery: sender notice for %s could not be enqueued", row.msg_id)

    # -- epoch closure -------------------------------------------------------

    def _close_finished_epochs(self, now: datetime, report: TickReport) -> None:
        """I6: an open epoch whose messages have all ended must not stay open.

        The two closures mean different things and the column must not claim
        otherwise (D10).  ``cancelled`` is a healthy receiver whose rows ended —
        the ordinary cause being D8's completion-cancel superseding queued
        steers.  ``abandoned`` adds the second conjunct: the receiver has NO live
        incarnation.  That conjunct is EVIDENCED rather than assumed, from the
        liveness probe's own ``pane_present`` column, and it is evaluated at
        CLOSURE time rather than at injection — an epoch whose messages were all
        completion-cancelled while still ``ready`` reaches terminal state with
        nothing ever injected, so an injection-time check would not run for the
        very case D10 introduces.
        """
        for digest in self._store.open_digests():
            if not self._store.all_terminal(digest.msg_ids):
                continue
            resolution = self._directory.resolve(digest.receiver_id)
            via = "cancelled" if (resolution.live and resolution.pane_present) else "abandoned"
            if self._store.close_digest(digest.receiver_id, digest.epoch, via=via, now=now):
                report.epochs_closed += 1

    # -- retention -----------------------------------------------------------

    def prune(self, *, now: datetime | None = None) -> int:
        """Retention over the queue's tables (§13d, case 14).

        Nothing pruned the new tables before this, so post-flip they would grow
        unbounded on the single-writer file §10 names as a contention risk.  Two
        exemptions: an OPEN digest of any age, which is the record of what is
        owed while its messages live, and any row named by an open finding —
        a finding that points at a pruned row is a diagnosis with its evidence
        deleted, which is the pane archaeology I5 exists to end.
        """
        stamp = now if now is not None else self._clock.now()
        return self._store.prune(now=stamp, protected=self._protected_ids())

    def _protected_ids(self) -> frozenset[str]:
        if self._findings is None:
            return frozenset()
        protected: set[str] = set()
        for finding in self._findings.list_findings(state="open"):
            # The delivery findings key their dedupe on the msg_id or on
            # ``receiver:epoch``; only the first names a row, and taking both is
            # harmless — an id that matches nothing protects nothing.
            protected.add(finding.dedupe_key)
        return frozenset(protected)

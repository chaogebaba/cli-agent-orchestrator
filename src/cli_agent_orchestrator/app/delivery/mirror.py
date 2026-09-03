"""The shadow enqueue and the mirror writer (WP-ARCH phase 3a, §7a).

**Why a mirror writer exists at all.**  ``claim``, ``ack`` and ``reclaim`` are
sub-phase 3b items, so without this nothing in 3a would move a shadow row off
``ready``.  That side of AC-3a's comparison would be a constant, every id would
classify as a disagreement, and the criterion would be unsatisfiable — a green
run proving only that the queue does nothing.  The mirror writer observes what
the legacy path actually did and advances the shadow row to the same ending, so
the comparison has two moving sides.

**What it may and may not do.**  It writes only to the queue's own tables and
only for ``mode='shadow'`` rows.  It never injects, never wakes a seat, never
touches an inbox row.  And it never raises into its caller: every entry point
here is called from inside the legacy delivery path, and phase 1's rule that
ingestion does not break what it observes applies with more force here, because
the thing being observed is message delivery itself.

**Missed outcomes are expected and are not silently absorbed.**  A server
restarted mid-flight, or a legacy edge that never fired, leaves the shadow row
``ready`` with no terminal state.  It stays unclaimable through ``claim``'s
mode filter, and the write-through flip sweeps each survivor to ``superseded``
as its first act.  The sweep and the filter are independent: either alone
prevents a delivery, and together they also stop the row sitting open forever.
AC-3a counts those rows as disagreements rather than hiding them.
"""

from __future__ import annotations

import logging
from datetime import datetime

from cli_agent_orchestrator.app.delivery.facts import (
    LegacyAttempt,
    LegacyEnqueue,
    LegacyOutcome,
    LegacyVeto,
)
from cli_agent_orchestrator.core.delivery import (
    AttemptOutcome,
    DeadReason,
    DeliveryAttempt,
    EnqueueDraft,
    MsgKind,
    MsgState,
    QueueMessage,
    QueueMode,
)
from cli_agent_orchestrator.core.ports import Clock, QueueStore

logger = logging.getLogger(__name__)

__all__ = [
    "ATTEMPT_OUTCOME_MAP",
    "LEGACY_TERMINAL_MAP",
    "MirrorWriter",
    "SHADOW_KEY_PREFIX",
    "VETO_OUTCOME_MAP",
    "shadow_idempotency_key",
]

#: Sub-phase 3a's idempotency key namespace.
#:
#: The audit makes ``idempotency_key`` CALLER-SUPPLIED, identifying one logical
#: send.  ``send_message`` has no such parameter, but in 3a the enqueue is a
#: mirror of a send that has ALREADY been identified: the legacy row id is the
#: identity, assigned by the insert the mirror is copying.  Deriving the key
#: from it is therefore the audit's semantics rather than a deviation from them,
#: and it makes the hook idempotent — a retried or doubly-observed insert
#: returns the same row instead of writing a second one.  The prefix keeps the
#: namespace disjoint from 3b's caller-supplied keys, so no live send can ever
#: collide with a shadow row.
SHADOW_KEY_PREFIX = "legacy-inbox:"


def shadow_idempotency_key(legacy_message_id: int) -> str:
    return f"{SHADOW_KEY_PREFIX}{legacy_message_id}"


#: Legacy inbox status to the queue's terminal state (I1's three).
#:
#: Every cell is a judgement and each is argued here, because a wrong cell does
#: not fail — it quietly makes AC-3a's agreement rate mean something else.
#:
#: ``delivered``   the row reached the seat.  The only unambiguous cell.
#: ``digested``    the row reached the seat inside a digest, which is delivery by
#:                 a different carrier, not a failure.  Calling it dead would
#:                 report the phase's own target mechanism as a loss.
#: ``superseded``  F578's own ending, and the queue has the same one.
#: ``cancelled``   legacy cancels are supersession-shaped: a barrier owner gone,
#:                 a reaped terminal with no surviving ancestor, an auto-resume
#:                 superseding an earlier nudge.  None is a delivery failure and
#:                 none belongs in the dead-letter table an operator reads for
#:                 poison; the ``failure_reason`` rides in ``detail``.
#: ``expired``     the caller's own ``expire_after_s`` came first — D8's reason,
#:                 not a fourth state.
#: ``failed``      legacy gave up on the row.  ``max_attempts`` is the queue's
#:                 equivalent ending; the legacy reason is preserved in the
#:                 attempt row's detail so the mapping is auditable.
#:
#: Absent deliberately: ``pending``, ``held``, ``delivering``, ``parked`` and
#: ``delivery_failed``.  The last is the one worth naming — legacy retries it, so
#: it is an in-flight state despite the word "failed" in it, and treating it as
#: terminal would report a redelivery as a loss.
LEGACY_TERMINAL_MAP: dict[str, tuple[MsgState, DeadReason | None]] = {
    "delivered": (MsgState.DELIVERED, None),
    "digested": (MsgState.DELIVERED, None),
    "superseded": (MsgState.SUPERSEDED, None),
    "cancelled": (MsgState.SUPERSEDED, None),
    "expired": (MsgState.DEAD, DeadReason.EXPIRED),
    "failed": (MsgState.DEAD, DeadReason.MAX_ATTEMPTS),
}

#: Legacy attempt outcome to the queue's attempt vocabulary.
#:
#: Only two cells map cleanly.  ``confirmed`` is a delivery; ``interrupted`` with
#: a pane reason is D12's ``pane_absent``.  Everything else — ``deferred``,
#: ``ambiguous``, ``unresolved``, ``programming_error`` — has no equivalent, and
#: forcing one would be worse than recording the truth: mapping ``deferred`` onto
#: ``veto_dialog`` would invent a dialog gate that was never consulted, and 3a's
#: whole purpose is a faithful comparison.  Those land on ``legacy_other`` with
#: the legacy outcome and reason verbatim in ``detail``.
ATTEMPT_OUTCOME_MAP: dict[str, AttemptOutcome] = {
    "confirmed": AttemptOutcome.DELIVERED,
}

#: The pane-absence reasons legacy attaches to an ``interrupted`` settle.
_PANE_ABSENT_REASONS = frozenset({"pane_unresolvable", "proven_absent", "receiver_metadata_gone"})

#: Injection-veto reason to the queue's attempt vocabulary (§7a).
#:
#: ``waiting_gate`` and ``dialog_hazard`` are the dialog gate holding, which is
#: D12's ``veto_dialog`` — the outcome bounded by a DURATION rather than by the
#: attempt budget, because a worker behind an unknown-dialog episode is waiting
#: on a human.
#:
#: ``safety_unverified`` and ``identity_unverified`` are both a probe that could
#: not be verified, which D12 puts on the attempt budget: a probe that cannot be
#: verified is a failing delivery, not a deferral.
#:
#: ``waiting_status`` is deliberately absent.  It is the status precondition D1
#: REMOVES from delivery — the queue selects on ``state`` and ``available_at``
#: and has no status term — so it has no equivalent by design, and recording it
#: as one of the three would put a mechanism the phase deletes into the
#: vocabulary the phase ships.  It lands on ``legacy_other``.
VETO_OUTCOME_MAP: dict[str, AttemptOutcome] = {
    "waiting_gate": AttemptOutcome.VETO_DIALOG,
    "dialog_hazard": AttemptOutcome.VETO_DIALOG,
    "safety_unverified": AttemptOutcome.VETO_UNVERIFIED,
    "identity_unverified": AttemptOutcome.VETO_UNVERIFIED,
}

#: Where a veto's attempt row is filed.  Vetoes carry no legacy attempt ordinal
#: — no attempt was ever opened — so they would all collide on ``claim_id`` 0
#: with each other and with the first real attempt.  A negative, descending
#: counter keeps them distinct from attempts (which count up from 1) and stable
#: under re-observation, since the value is derived from the veto's own instant
#: rather than from a sequence.
_VETO_CLAIM_ID = -1


class MirrorWriter:
    """Writes and advances ``mode='shadow'`` rows from observed legacy facts.

    One object, three entry points, none of which raises.  It holds a
    ``QueueStore`` and a ``Clock`` and nothing else — no session, no connection,
    no knowledge of which tables legacy keeps its answers in.
    """

    def __init__(self, store: QueueStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, fact: LegacyEnqueue) -> QueueMessage:
        """Write the shadow row for one legacy inbox insert.

        Runs AFTER the legacy insert has committed, which is not an incidental
        detail.  The queue's store holds its own connection to the same SQLite
        file, so writing from inside the legacy transaction would contend with
        the write lock that transaction is holding — and would leave a shadow row
        behind for a legacy row that then rolled back.  Post-commit, the row this
        mirrors is durable.

        **The F475 dedup is inherited rather than reimplemented.**  A suppressed
        duplicate never reaches this method: legacy's window check runs before
        its insert, and a suppression returns the existing row without inserting,
        so no hook fires.  The shadow queue therefore reproduces legacy's dedup
        outcome exactly, for free, in the sub-phase where the queue is not yet
        the authority.  3b's write-through has to carry the check itself (D13),
        with all five conjuncts and the window intact, and that is where it
        belongs — implementing it here would give one behaviour two
        implementations while only one of them ran.
        """
        draft = EnqueueDraft(
            idempotency_key=shadow_idempotency_key(fact.legacy_message_id),
            receiver_id=fact.receiver_id,
            sender_id=fact.sender_id,
            kind=MsgKind.CALLBACK if fact.is_callback else MsgKind.NOTE,
            payload=fact.message,
            mode=QueueMode.SHADOW,
            expire_after_s=fact.expire_after_s,
            supersede_key=fact.supersede_key,
            content_hash=fact.content_hash,
            park_warm=fact.park_warm,
            barrier_id=fact.barrier_id,
            barrier_member_key=fact.barrier_member_key,
            enqueue_generation=fact.enqueue_generation,
            legacy_message_id=fact.legacy_message_id,
        )
        message = self._store.enqueue(draft)
        # A row can be born already terminal — the F578 supersession block runs
        # in the same insert, so a row superseded by a newer one with the same
        # key is SUPERSEDED before this hook sees it.  Settling here rather than
        # waiting for an edge is what stops that row sitting ready forever and
        # reading as a disagreement.
        self._settle_from_status(message, fact.status, None)
        return message

    # -- outcomes -----------------------------------------------------------

    def observe(self, outcome: LegacyOutcome) -> None:
        """Record legacy's attempts for one row and advance its shadow state.

        Idempotent by construction: attempt rows insert on conflict do nothing,
        and ``settle`` refuses an already-terminal row.  So observing the same
        message from two edges, or twice from one, converges rather than
        double-counting — which matters because several legacy writers can end a
        row and they do not fire in a guaranteed order.
        """
        message = self._lookup(outcome.legacy_message_id)
        if message is None:
            return
        for attempt in outcome.attempts:
            self._record(message, attempt)
        self._settle_from_status(message, outcome.status, outcome.failure_reason)

    def observe_veto(self, veto: LegacyVeto) -> None:
        """Record an injection the legacy path declined, for each message in it.

        The row is NOT advanced: a veto is not an ending.  In the live queue the
        row would keep its lease and ``reclaim`` would count it or the ceiling
        would bound it; in shadow mode there is no lease to keep, so the veto
        lives only in the attempt row, which is exactly where §7a puts it —
        "``available_at`` does not move in shadow mode, no scheduler reading it
        there; the veto's timing lives in the attempt row".
        """
        outcome = VETO_OUTCOME_MAP.get(veto.reason, AttemptOutcome.LEGACY_OTHER)
        detail = f"veto reason={veto.reason}"
        if veto.gate_episode:
            detail += f" gate={veto.gate_episode}"
        for legacy_id in veto.legacy_message_ids:
            message = self._lookup(legacy_id)
            if message is None or message.terminal:
                continue
            self._safely(
                self._store.record_attempt,
                DeliveryAttempt(
                    msg_id=message.msg_id,
                    claim_id=_VETO_CLAIM_ID - int(veto.at.timestamp()),
                    carrier="legacy-veto",
                    started_at=veto.at,
                    outcome=outcome,
                    detail=detail,
                ),
            )

    # -- internals ----------------------------------------------------------

    def _lookup(self, legacy_message_id: int) -> QueueMessage | None:
        """The shadow row for a legacy id, or ``None`` when there is not one.

        ``None`` is ordinary rather than exceptional: the switch may have been
        off when the row was inserted, or the row may predate the deployment, or
        it may be one of the enqueue paths 3a does not mirror.  A mirror that
        treated a missing row as an error would log once per message on any
        server that enabled the switch mid-session.
        """
        getter = getattr(self._store, "get_by_legacy_id", None)
        if getter is None:  # pragma: no cover — every real store has it
            return None
        result = getter(legacy_message_id)
        return result if isinstance(result, QueueMessage) else None

    def _record(self, message: QueueMessage, attempt: LegacyAttempt) -> None:
        outcome = self._map_attempt(attempt)
        detail = f"legacy outcome={attempt.outcome}"
        if attempt.reason:
            detail += f" reason={attempt.reason}"
        if attempt.error:
            detail += f" error={attempt.error}"
        self._safely(
            self._store.record_attempt,
            DeliveryAttempt(
                msg_id=message.msg_id,
                claim_id=attempt.ordinal,
                carrier=attempt.carrier,
                started_at=attempt.started_at,
                outcome=outcome,
                detail=detail,
            ),
        )

    @staticmethod
    def _map_attempt(attempt: LegacyAttempt) -> AttemptOutcome:
        mapped = ATTEMPT_OUTCOME_MAP.get(attempt.outcome)
        if mapped is not None:
            return mapped
        if attempt.outcome == "interrupted" and (attempt.reason or "") in _PANE_ABSENT_REASONS:
            return AttemptOutcome.PANE_ABSENT
        return AttemptOutcome.LEGACY_OTHER

    def _settle_from_status(
        self, message: QueueMessage, legacy_status: str, failure_reason: str | None
    ) -> None:
        mapping = LEGACY_TERMINAL_MAP.get(legacy_status)
        if mapping is None:
            return
        state, reason = mapping
        settled = self._safely(
            self._store.settle,
            message.msg_id,
            state=state,
            now=self._clock.now(),
            reason=reason,
        )
        if settled and failure_reason:
            # The legacy reason is worth keeping and has nowhere else to live:
            # the queue's four dead reasons are a closed vocabulary and widening
            # it to hold legacy's would put a legacy concept in the shipped
            # enum.  An attempt row carries it instead, which is also where a
            # reader of ``cao diag <msg_id>`` will look for it.
            self._safely(
                self._store.record_attempt,
                DeliveryAttempt(
                    msg_id=message.msg_id,
                    claim_id=0,
                    carrier="legacy-status",
                    started_at=self._clock.now(),
                    outcome=AttemptOutcome.LEGACY_OTHER,
                    detail=f"legacy status={legacy_status} failure_reason={failure_reason}",
                ),
            )

    @staticmethod
    def _safely(call: object, *args: object, **kwargs: object) -> bool:
        """Run one store call, swallowing ``Exception`` and returning success.

        ``BaseException`` is deliberately NOT swallowed: a ``KeyboardInterrupt``
        or a ``CancelledError`` arriving here belongs to the caller's control
        flow, and eating it would hang a shutdown.
        """
        try:
            result = call(*args, **kwargs)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 — the mirror may never break delivery
            logger.debug("delivery mirror write failed (delivery is unaffected)", exc_info=True)
            return False
        return result is not False

"""D6b(3) / A2.9 — the interrupt aggregate over the server's SQLite file.

**One operation, one ``BEGIN IMMEDIATE``, no composition.**  Every public method
here is a whole transition: it verifies a fence, computes whatever it has to
persist, writes every row the transition touches, and CASes the phase — all
inside a single write transaction.  Nothing here calls another store, and
nothing here performs I/O.  Those two prohibitions are not tidiness:

* A transition composed from two operations has a crash gap between them, and
  AC-S1.27 injects a death at each of twenty-two named points with "rollback
  exposes all-or-none" as the oracle.  Composition makes that oracle false by
  construction.
* An await inside the write lock serializes every other receiver behind one
  agent's wire latency, which is the head-of-line blocking AC-S1.29's four
  causal barriers exist to disprove.  The receiver task owns every await; this
  module owns every commit; neither does the other's job.

**The clock is an argument, never a member.**  ``begin_cancel`` takes the
receiver task's single ``now`` sample and derives both instants from it.  A store
that held a clock would be a second authority on when the cancel window opened,
and r18 lists "store resamples clock" as a mutant that must go RED.

**Timestamps are the fixed-width UTC strings ``connection.py`` renders**, because
every comparison here is a STRING comparison — the same rule ``queue.py`` states,
and for the same reason: a variable-width rendering makes ordering depend on
whether a microsecond happened to be zero.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from cli_agent_orchestrator.adapters.store.connection import (
    SqliteConnectionSource,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.delivery import DeadReason, MsgState, compute_dead_by
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.interrupt import (
    MID_INTERRUPT_PHASES,
    WINDOW_LOST,
    ActiveTurnHandle,
    AdmissionOutcome,
    CancelSettlement,
    CancelWindow,
    ClaimedRow,
    InterruptAdmission,
    InterruptFence,
    InterruptPhase,
    InterruptRefusal,
    InterruptStateRow,
    LedgerWindow,
    Quota,
    RecoveryWindow,
    SettleKind,
    SubmitEnvelope,
    SubmitReceipt,
    Urgency,
    WindowLost,
    effective_deadline,
    urgency_rank,
)
from cli_agent_orchestrator.core.ports import InterruptLimiterPort
from cli_agent_orchestrator.core.timing import (
    ACP_CANCEL_SETTLE_S,
    ACP_KILL_GRACE_S,
    CANCEL_HOLD_MARGIN_S,
    DELIVERY_LEASE_S,
    DELIVERY_TICK_S,
    INTERRUPT_BUDGET_WINDOW_S,
    INTERRUPT_MAX_LATENCY_S,
    RECOVERY_DEADLINE_S,
)

__all__ = ["SqliteInterruptStore"]


_STATE_COLUMNS = (
    "terminal_id, phase, generation, interrupt_msg_id, interrupt_claim_id, cut_callback_id, "
    "active_turn_session_id, active_turn_request_id, active_turn_generation, active_turn_seq, "
    "deadline, pending_deadline, recovery_deadline, cancel_sent"
)


def _row_to_state(row: sqlite3.Row) -> InterruptStateRow:
    return InterruptStateRow(
        terminal_id=row["terminal_id"],
        phase=InterruptPhase(row["phase"]),
        generation=int(row["generation"]),
        interrupt_msg_id=row["interrupt_msg_id"],
        interrupt_claim_id=(
            int(row["interrupt_claim_id"]) if row["interrupt_claim_id"] is not None else None
        ),
        cut_callback_id=row["cut_callback_id"],
        active_turn_session_id=row["active_turn_session_id"],
        active_turn_request_id=row["active_turn_request_id"],
        active_turn_generation=(
            int(row["active_turn_generation"])
            if row["active_turn_generation"] is not None
            else None
        ),
        active_turn_seq=(
            int(row["active_turn_seq"]) if row["active_turn_seq"] is not None else None
        ),
        deadline=parse_timestamp(row["deadline"]) if row["deadline"] else None,
        pending_deadline=(
            parse_timestamp(row["pending_deadline"]) if row["pending_deadline"] else None
        ),
        recovery_deadline=(
            parse_timestamp(row["recovery_deadline"]) if row["recovery_deadline"] else None
        ),
        cancel_sent=bool(row["cancel_sent"]),
    )


class SqliteInterruptStore:
    """``core.ports.InterruptStore`` over the same file the queue and journal use.

    The shared database is a PRECONDITION, not a convenience: D6b(3) says that if
    queue, state and journal do not share one database, the build stops.  Every
    transition below writes the state row AND I's queue row in one transaction,
    and SQLite has no cross-file transaction, so a split would silently degrade
    every CAS into a two-phase write with a crash gap in the middle.
    """

    def __init__(self, pool: SqliteConnectionSource, *, limiter: InterruptLimiterPort) -> None:
        self._pool = pool
        self._limiter = limiter

    # -- reads --------------------------------------------------------------

    def read_state(self, terminal_id: str) -> InterruptStateRow | None:
        row = (
            self._pool.connection()
            .execute(
                f"SELECT {_STATE_COLUMNS} FROM interrupt_state WHERE terminal_id = ?",
                (terminal_id,),
            )
            .fetchone()
        )
        return _row_to_state(row) if row is not None else None

    # -- admission ----------------------------------------------------------

    def admit_interrupt(self, request: InterruptAdmission) -> AdmissionOutcome:
        """CAS ``none -> pending(I)`` FIRST, then charge quota, then write I's row.

        The ordering carries the whole guarantee that a refusal costs nothing.
        The reservation CAS is the only bound ``force`` cannot waive, so it runs
        before the limiter is consulted at all; a caller refused
        ``INTERRUPT_IN_PROGRESS`` has spent no quota and left no row, which is
        AC-S1.22's fails-if stated as an ordering rather than as a cleanup.

        ``pending_deadline = now + INTERRUPT_MAX_LATENCY_S`` is persisted here,
        on the state row, because it is TERMINAL for the pending phase whether or
        not the row is ever claimed (r12, review r11 B3).  Re-nudging and
        resuming preparation are intermediate actions; the deadline is the rule.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(request.now)
        with immediate_transaction(conn):
            state = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM interrupt_state WHERE terminal_id = ?",
                (request.terminal_id,),
            ).fetchone()
            current = _row_to_state(state) if state is not None else None
            if current is not None and current.phase in MID_INTERRUPT_PHASES:
                return AdmissionOutcome(refused=InterruptRefusal.IN_PROGRESS)

            window = self._ledger_window(conn, request)
            decision = self._limiter.decide(
                principal=request.principal,
                terminal_id=request.terminal_id,
                now=request.now,
                window=window,
                force=request.force,
            )
            if decision.refused is not None:
                return AdmissionOutcome(refused=decision.refused, quota=decision.quota)

            # The lifetime check BEFORE anything is written: a row whose effective
            # deadline cannot cover cancel + margin would be admitted only to die
            # inside its own cancel window, so it is refused pre-admission with
            # no row and no quota (AC-S1.22(i)).
            dead_by = compute_dead_by(
                created_at=request.now,
                available_at=request.now,
                expire_after_s=request.envelope_expire_after_s,
            )
            needed = request.now + timedelta(seconds=ACP_CANCEL_SETTLE_S + CANCEL_HOLD_MARGIN_S)
            if dead_by < needed:
                return AdmissionOutcome(
                    refused=InterruptRefusal.WINDOW_TOO_SHORT, quota=decision.quota
                )

            msg_id = new_ulid()
            conn.execute(
                "INSERT INTO delivery_msg (msg_id, idempotency_key, payload_digest, receiver_id, "
                "sender_id, kind, payload, state, mode, claim_id, attempts, max_attempts, "
                "available_at, dead_by, expire_after_s, urgency, urgency_rank, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,?,?)",
                (
                    msg_id,
                    f"interrupt:{request.callback_id}",
                    "",
                    request.terminal_id,
                    request.principal.subject,
                    "callback",
                    request.envelope.body,
                    MsgState.READY.value,
                    "live",
                    5,
                    stamp,
                    render_timestamp(dead_by),
                    request.envelope_expire_after_s,
                    Urgency.INTERRUPT.value,
                    urgency_rank(Urgency.INTERRUPT),
                    stamp,
                ),
            )
            pending_deadline = render_timestamp(
                request.now + timedelta(seconds=INTERRUPT_MAX_LATENCY_S)
            )
            generation = (current.generation + 1) if current is not None else 1
            conn.execute(
                "INSERT INTO interrupt_state (terminal_id, phase, generation, interrupt_msg_id, "
                "pending_deadline, cut_callback_id, cancel_sent) VALUES (?,?,?,?,?,?,0) "
                "ON CONFLICT(terminal_id) DO UPDATE SET phase = excluded.phase, "
                "generation = excluded.generation, interrupt_msg_id = excluded.interrupt_msg_id, "
                "interrupt_claim_id = NULL, pending_deadline = excluded.pending_deadline, "
                "cut_callback_id = excluded.cut_callback_id, deadline = NULL, "
                "recovery_deadline = NULL, cancel_sent = 0, active_turn_session_id = NULL, "
                "active_turn_request_id = NULL, active_turn_generation = NULL, "
                "active_turn_seq = NULL",
                (
                    request.terminal_id,
                    InterruptPhase.PENDING.value,
                    generation,
                    msg_id,
                    pending_deadline,
                    request.cut_candidate,
                ),
            )
            # The ledger row is the CHARGE, and it is written in the same
            # transaction as the reservation.  A charge recorded outside it could
            # survive a rolled-back admission and bill a caller for an interrupt
            # that never existed.
            conn.execute(
                "INSERT INTO interrupt_ledger (interrupt_id, principal, terminal_id, "
                "admitted_at, forced, proxied_optin) VALUES (?,?,?,?,?,?)",
                (
                    request.callback_id,
                    request.principal.budget_key,
                    request.terminal_id,
                    stamp,
                    int(request.force),
                    int(request.proxied_optin),
                ),
            )
        return AdmissionOutcome(
            refused=None,
            interrupt_id=request.callback_id,
            callback_id=request.callback_id,
            target=request.terminal_id,
            observed_state=request.observed_state,
            cut_candidate=request.cut_candidate,
            reservation=InterruptPhase.PENDING.value,
            quota=decision.quota,
        )

    def _ledger_window(self, conn: sqlite3.Connection, request: InterruptAdmission) -> LedgerWindow:
        """Both bounds' evidence, read under the SAME write lock as the CAS.

        Read here rather than by the limiter because the limiter is application
        policy and may not touch SQLite, and read INSIDE the transaction because
        two concurrent admissions at the budget edge must serialize — AC-S1.22(c)
        is exactly the claim that one of them wins.
        """
        horizon = render_timestamp(request.now - timedelta(seconds=INTERRUPT_BUDGET_WINDOW_S))
        principal_rows = conn.execute(
            "SELECT admitted_at FROM interrupt_ledger WHERE principal = ? AND admitted_at > ? "
            "ORDER BY admitted_at",
            (request.principal.budget_key, horizon),
        ).fetchall()
        terminal_row = conn.execute(
            "SELECT admitted_at FROM interrupt_ledger WHERE terminal_id = ? "
            "ORDER BY admitted_at DESC LIMIT 1",
            (request.terminal_id,),
        ).fetchone()
        return LedgerWindow(
            principal_admissions=tuple(parse_timestamp(r["admitted_at"]) for r in principal_rows),
            last_terminal_admission=(
                parse_timestamp(terminal_row["admitted_at"]) if terminal_row is not None else None
            ),
        )

    # -- claim --------------------------------------------------------------

    def claim_next(self, receiver_id: str, *, now: datetime, lease_owner: str) -> ClaimedRow | None:
        """The ONLY claim path for an ACP receiver: exactly one row, phase-total.

        The ordering ``urgency_rank, available_at, msg_id`` lives in the SQL, not
        in the driver and not in prose — AC-S1.25's fails-if is precisely
        "ordering lives in prose or in the driver instead of the claim SQL", and
        its mutant drops ``urgency_rank`` from the ORDER BY and watches an older
        normal row win.

        The selection rule is TOTAL over the phase set:

            none        -> the next row, urgency-ordered, ONE per claim
            pending     -> I by id
            cancelling  -> nothing
            prompting   -> nothing
            recovering  -> nothing

        A phase with no stated answer is how a second ``session/prompt`` gets
        issued against an ongoing turn.  Every other row for this receiver stays
        *ready* rather than leased, so no attempt is spent while I is in flight
        and the three normal rows behind an interrupt are still there afterwards
        (r12/r13's isolation arm).
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        lease_until = render_timestamp(now + timedelta(seconds=DELIVERY_LEASE_S))
        with immediate_transaction(conn):
            state_row = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM interrupt_state WHERE terminal_id = ?",
                (receiver_id,),
            ).fetchone()
            state = _row_to_state(state_row) if state_row is not None else None
            phase = state.phase if state is not None else InterruptPhase.NONE

            if phase in (
                InterruptPhase.CANCELLING,
                InterruptPhase.PROMPTING,
                InterruptPhase.RECOVERING,
            ):
                return None

            if phase is InterruptPhase.PENDING:
                if state is None or state.interrupt_msg_id is None:
                    return None
                row = conn.execute(
                    "SELECT msg_id, payload, available_at, dead_by, expire_after_s, claim_id, "
                    "state, urgency FROM delivery_msg WHERE msg_id = ? AND state = 'ready'",
                    (state.interrupt_msg_id,),
                ).fetchone()
            else:
                # LIMIT 1 is not an optimisation: the batch claim never runs for
                # an ACP receiver (r14, review r13 B3), and AC-S1.25's isolation
                # mutant raises the limit to 64 and must go RED even though I is
                # still selected first.
                row = conn.execute(
                    "SELECT msg_id, payload, available_at, dead_by, expire_after_s, claim_id, "
                    "state, urgency FROM delivery_msg "
                    "WHERE receiver_id = ? AND state = 'ready' AND mode = 'live' "
                    "AND available_at <= ? AND dead_by > ? "
                    "ORDER BY urgency_rank, available_at, msg_id LIMIT 1",
                    (receiver_id, stamp, stamp),
                ).fetchone()
            if row is None:
                return None

            conn.execute(
                "UPDATE delivery_msg SET state = 'leased', claim_id = claim_id + 1, "
                "lease_owner = ?, lease_expires_at = ? WHERE msg_id = ? AND state = 'ready'",
                (lease_owner, lease_until, row["msg_id"]),
            )
            claim_id = int(row["claim_id"]) + 1
            if phase is InterruptPhase.PENDING:
                # The scheduler persists the claim id and writes NO dispatch
                # intent here (r11, review r10 B1): A2.3 puts that fact
                # immediately before the one adapter write, and no write happens
                # in this transaction.
                conn.execute(
                    "UPDATE interrupt_state SET interrupt_claim_id = ? WHERE terminal_id = ?",
                    (claim_id, receiver_id),
                )
            fence = InterruptFence(
                terminal_id=receiver_id,
                msg_id=row["msg_id"],
                claim_id=claim_id,
                owner=lease_owner,
                generation=state.generation if state is not None else 0,
            )
            return ClaimedRow(
                fence=fence,
                callback_id=row["msg_id"],
                envelope=SubmitEnvelope(
                    callback_id=row["msg_id"],
                    body=row["payload"],
                    urgency=Urgency(row["urgency"]),
                ),
                available_at=parse_timestamp(row["available_at"]),
                effective_deadline=effective_deadline(
                    dead_by=parse_timestamp(row["dead_by"]),
                    caller_set=row["expire_after_s"] is not None,
                    busy_accumulated_s=0.0,
                ),
            )

    # -- the cancel window --------------------------------------------------

    def begin_cancel(
        self, fence: InterruptFence, active_turn: ActiveTurnHandle, now: datetime
    ) -> CancelWindow | WindowLost:
        """A2.9(iv)'s exact cancel window, computed and persisted in ONE transaction.

        Everything the receiver task will later need is DERIVED HERE and RETURNED,
        so nothing downstream recomputes anything:

            deadline    = now + ACP_CANCEL_SETTLE_S      (the cancel-settle bound)
            lease_until = deadline + CANCEL_HOLD_MARGIN_S (I's lease, strictly later)

        Two distinct instants, and the distinctness is the point: a cancel that
        settles at the last legal instant must still be owned by the claim that
        issued it, so I's lease has to outlive its own cancel deadline.  ``now``
        is the receiver task's single sample; this method neither holds a clock
        nor takes a second one.

        The transaction verifies, in order: I's queue fence
        ``(msg_id, claim_id, owner)``; the phase-row generation; that I is still
        ``pending``; and that I's effective lifetime covers ``lease_until``.  Any
        failure returns :data:`WINDOW_LOST` atomically — no partial state, and N
        and the session actor untouched.
        """
        conn = self._pool.connection()
        deadline = now + timedelta(seconds=ACP_CANCEL_SETTLE_S)
        lease_until = deadline + timedelta(seconds=CANCEL_HOLD_MARGIN_S)
        with immediate_transaction(conn):
            msg = conn.execute(
                "SELECT msg_id, claim_id, lease_owner, state, dead_by, expire_after_s "
                "FROM delivery_msg WHERE msg_id = ?",
                (fence.msg_id,),
            ).fetchone()
            if (
                msg is None
                or int(msg["claim_id"]) != fence.claim_id
                or msg["lease_owner"] != fence.owner
                or msg["state"] != MsgState.LEASED.value
            ):
                return WINDOW_LOST
            state_row = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM interrupt_state WHERE terminal_id = ?",
                (fence.terminal_id,),
            ).fetchone()
            if state_row is None:
                return WINDOW_LOST
            state = _row_to_state(state_row)
            if (
                state.phase is not InterruptPhase.PENDING
                or state.generation != fence.generation
                or state.interrupt_msg_id != fence.msg_id
            ):
                return WINDOW_LOST
            # The caller-set expiry is never extended by busy credit (A2.9(iv)),
            # so an interrupt whose caller time-boxed it below the cancel window
            # is refused HERE rather than being quietly given more time.
            if (
                effective_deadline(
                    dead_by=parse_timestamp(msg["dead_by"]),
                    caller_set=msg["expire_after_s"] is not None,
                    busy_accumulated_s=0.0,
                )
                < lease_until
            ):
                return WINDOW_LOST
            conn.execute(
                "UPDATE delivery_msg SET lease_expires_at = ? WHERE msg_id = ?",
                (render_timestamp(lease_until), fence.msg_id),
            )
            conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = ?, active_turn_session_id = ?, active_turn_request_id = ?, "
                "active_turn_generation = ?, active_turn_seq = ?, cut_callback_id = ?, "
                "cancel_sent = 0 WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.CANCELLING.value,
                    render_timestamp(deadline),
                    active_turn.session_id,
                    active_turn.acp_request_id,
                    active_turn.lifecycle_generation,
                    active_turn.turn_seq,
                    active_turn.callback_id,
                    fence.terminal_id,
                    InterruptPhase.PENDING.value,
                    fence.generation,
                ),
            )
        return CancelWindow(deadline=deadline, lease_until=lease_until)

    def mark_cancel_sent(self, fence: InterruptFence, sent_at: datetime) -> bool:
        """Record that cancel bytes left.  Never consulted by the timeout path.

        Persisted for AUDIT, not for control: on restart a ``cancelling`` row
        takes the timeout path regardless of this flag (r13, review r12 B3),
        because the stream that would have settled the cancel died with the
        process and a sent-but-unconsumed cancel is indistinguishable from one
        that was never written.
        """
        del sent_at  # the instant is audit-grade only; the deadline is what binds
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE interrupt_state SET cancel_sent = 1 WHERE terminal_id = ? AND phase = ?",
                (fence.terminal_id, InterruptPhase.CANCELLING.value),
            )
            return cursor.rowcount > 0

    def race_lost(self, fence: InterruptFence) -> bool:
        """The handle moved before bytes: CAS back to ``pending`` and re-prepare.

        Clears BOTH the consumed cancel deadline and the active-turn audit
        fields.  Leaving the deadline behind would let a later restart read a
        cancel window that belongs to a cancel nobody ever issued.  The loop this
        allows is bounded by ``pending_deadline``, which is never rewritten.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL, cancel_sent = 0, active_turn_session_id = NULL, "
                "active_turn_request_id = NULL, active_turn_generation = NULL, "
                "active_turn_seq = NULL WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.PENDING.value,
                    fence.terminal_id,
                    InterruptPhase.CANCELLING.value,
                    fence.generation,
                ),
            )
            return cursor.rowcount > 0

    def settle_to_prompt(self, fence: InterruptFence, settle: CancelSettlement) -> bool:
        """CAS ``cancelling -> prompting``, recording the cut, WITHOUT touching N.

        N's delivery attempt stays ``DELIVERED`` and its presentation stays
        immutable.  A2.3/I3: N's dispatch eligibility was extinguished at its
        local receipt, before its turn could become an interrupt target, so
        there is nothing here to reopen.  The cut is recorded on I — that is what
        makes "the delivered cut row is never re-presented" a property of the
        write set rather than of the caller's restraint.
        """
        if settle.kind is not SettleKind.CANCELLED:
            return False
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL, cancel_sent = 0 "
                "WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.PROMPTING.value,
                    fence.terminal_id,
                    InterruptPhase.CANCELLING.value,
                    fence.generation,
                ),
            )
            return cursor.rowcount > 0

    def begin_prompt(self, fence: InterruptFence) -> bool:
        """CAS ``pending -> prompting`` for the IDLE branch (D6b(3)).

        The idle path's durable dispatch INTENT, and the reason it exists as its
        own operation rather than being folded into ``complete_prompt``: A2.3
        puts the intent immediately before the one adapter write, so a crash
        between the two leaves a ``prompting`` row that the restart oracle
        resolves ``SUBMISSION_UNCERTAIN`` — which is the honest answer, because
        the bytes may have left. Folding them would make that window invisible
        and every ambiguous write would look like one that never happened.

        No cancel is involved, so no deadline is set: ACP defines
        ``session/cancel`` for an ongoing turn only, and the idle branch has no
        turn to cut.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL "
                "WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.PROMPTING.value,
                    fence.terminal_id,
                    InterruptPhase.PENDING.value,
                    fence.generation,
                ),
            )
            return cursor.rowcount > 0

    def complete_prompt(self, fence: InterruptFence, receipt: SubmitReceipt) -> bool:
        """CAS ``prompting -> none`` and close I's attempt, in ONE transaction.

        THIS COMMIT IS THE DURABLE RECEIPT.  There is no separate marker and no
        replay: a kill after the flush but before this commit resolves
        ``SUBMISSION_UNCERTAIN`` on restart, honestly, because the ACP subprocess
        did not survive the restart and the turn is gone either way (r15, review
        r14 B2).  If this had committed, the row would not read ``prompting``.
        """
        if not receipt.accepted:
            return False
        conn = self._pool.connection()
        with immediate_transaction(conn):
            conn.execute(
                "UPDATE delivery_msg SET state = ?, terminated_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE msg_id = ? AND claim_id = ?",
                (
                    MsgState.DELIVERED.value,
                    render_timestamp(receipt.write_receipt_at or datetime.now(UTC)),
                    fence.msg_id,
                    fence.claim_id,
                ),
            )
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL, interrupt_msg_id = NULL, interrupt_claim_id = NULL, "
                "pending_deadline = NULL, cancel_sent = 0 "
                "WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.NONE.value,
                    fence.terminal_id,
                    InterruptPhase.PROMPTING.value,
                    fence.generation,
                ),
            )
            return cursor.rowcount > 0

    def fail_interrupt(self, fence: InterruptFence, reason: DeadReason, phase: str) -> bool:
        """Terminalize I and release the reservation, in one transaction.

        ``phase`` is the audit phase tag that rides the appended ``dead`` fact —
        ``pending_expired``, ``window_lost`` or ``cancel_timeout``.  The charge
        STANDS: quota was consumed at admission and an interrupt that was
        admitted and then died is exactly the thing the budget is meant to count.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            conn.execute(
                "UPDATE delivery_msg SET state = ?, terminated_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE msg_id = ?",
                (MsgState.DEAD.value, render_timestamp(datetime.now(UTC)), fence.msg_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO delivery_dead (msg_id, receiver_id, reason, mode, died_at) "
                "VALUES (?,?,?,?,?)",
                (
                    fence.msg_id,
                    fence.terminal_id,
                    reason.value,
                    "live",
                    render_timestamp(datetime.now(UTC)),
                ),
            )
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL, interrupt_msg_id = NULL, interrupt_claim_id = NULL, "
                "pending_deadline = NULL, cancel_sent = 0, cut_callback_id = ? "
                "WHERE terminal_id = ? AND generation = ?",
                (InterruptPhase.NONE.value, phase, fence.terminal_id, fence.generation),
            )
            return cursor.rowcount > 0

    # -- recovery -----------------------------------------------------------

    def begin_recovery(self, fence: InterruptFence, now: datetime) -> RecoveryWindow | None:
        """Cancel timed out: kill I and CAS ``cancelling -> recovering``, never ``none``.

        ``recovering`` keeps the terminal NON-ADMISSIBLE until recovery is
        durable (r13, review r12 B4) — admission refuses ``INTERRUPT_IN_PROGRESS``
        throughout.  Both instants are derived here and returned:

            recovery_deadline = now + RECOVERY_DEADLINE_S
            teardown_at       = recovery_deadline - (DELIVERY_TICK_S + ACP_KILL_GRACE_S)

        ``teardown_at`` is where recovery ATTEMPTS stop, and the subtraction is
        what reserves one post-crash scan plus a full process-group teardown
        inside the promised bound — the ordering U7 raises at import.
        """
        recovery_deadline = now + timedelta(seconds=RECOVERY_DEADLINE_S)
        teardown_at = recovery_deadline - timedelta(seconds=DELIVERY_TICK_S + ACP_KILL_GRACE_S)
        conn = self._pool.connection()
        with immediate_transaction(conn):
            conn.execute(
                "UPDATE delivery_msg SET state = ?, terminated_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE msg_id = ?",
                (MsgState.DEAD.value, render_timestamp(now), fence.msg_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO delivery_dead (msg_id, receiver_id, reason, mode, died_at) "
                "VALUES (?,?,?,?,?)",
                (
                    fence.msg_id,
                    fence.terminal_id,
                    DeadReason.INTERRUPT_CANCEL_TIMEOUT.value,
                    "live",
                    render_timestamp(now),
                ),
            )
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "recovery_deadline = ?, interrupt_msg_id = NULL, interrupt_claim_id = NULL, "
                "pending_deadline = NULL "
                "WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.RECOVERING.value,
                    render_timestamp(recovery_deadline),
                    fence.terminal_id,
                    InterruptPhase.CANCELLING.value,
                    fence.generation,
                ),
            )
            if cursor.rowcount == 0:
                return None
        return RecoveryWindow(recovery_deadline=recovery_deadline, teardown_at=teardown_at)

    def finish_recovery(self, terminal_id: str, expected_generation: int) -> bool:
        """CAS ``recovering -> none`` — only after close, re-session AND the bind."""
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE interrupt_state SET phase = ?, generation = generation + 1, "
                "deadline = NULL, recovery_deadline = NULL "
                "WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (
                    InterruptPhase.NONE.value,
                    terminal_id,
                    InterruptPhase.RECOVERING.value,
                    expected_generation,
                ),
            )
            return cursor.rowcount > 0

    def expire_recovery(
        self, terminal_id: str, expected_generation: int, expected_recovery_deadline: datetime
    ) -> bool:
        """Retire a terminal whose recovery never succeeded.  Called AFTER absence is proven.

        One short transaction, deliberately: recording the condition admits one
        durable condition generation, and delivering it to the viewer or the
        supervisor is later queue work.  I/O inside this transaction would put a
        socket write under the write lock.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "DELETE FROM interrupt_state WHERE terminal_id = ? AND phase = ? "
                "AND generation = ? AND recovery_deadline = ?",
                (
                    terminal_id,
                    InterruptPhase.RECOVERING.value,
                    expected_generation,
                    render_timestamp(expected_recovery_deadline),
                ),
            )
            return cursor.rowcount > 0

    def finalize_no_resume_exit(self, terminal_id: str, expected_generation: int) -> bool:
        """The no-close/no-resume seat's own finalizer (r16, review r15 B3).

        Separate from :meth:`expire_recovery` so neither can re-emit the other's
        condition.  A cold ``session/new`` is never attempted for such a seat:
        the killed subprocess IS the product context, so the seat is marked
        ``exited`` and launching a replacement is a human act.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "DELETE FROM interrupt_state WHERE terminal_id = ? AND phase = ? AND generation = ?",
                (terminal_id, InterruptPhase.RECOVERING.value, expected_generation),
            )
            return cursor.rowcount > 0

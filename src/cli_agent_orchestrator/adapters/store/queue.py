"""The delivery queue over SQLite (WP-ARCH phase 3, audit §3.2).

Derived from litequeue's schema and its four statements — MIT, and the audit
adopted them because they are the smallest correct expression of a leased queue
over SQLite: an insert, a claim that issues a fencing token, an ack that matches
on it, and a reclaim that returns expired leases.  The column set is NOT
litequeue's; it is the audit's row plus the columns the blueprint's decisions
add, enumerated in the migrator beside the DDL.

Three properties are worth reading this file for, because each is a mechanism the
design rests on and each has exactly one line of defence:

* **``mode='live'`` is a conjunct of the CLAIM statement itself**, not something
  a caller adds.  Every consumer of the queue inherits it that way — the boot
  occupancy test, the drain tick and the ordinary tick — and no future caller can
  forget it.  The rule was in force before there was anything for it to guard,
  which is the only ordering that could have caught it; it outlives the
  observational rows it was written for (#738).
* **No ``UPDATE`` in this module names ``dead_by``.**  The column is written once,
  by ``enqueue``, from :func:`~core.delivery.compute_dead_by`.  ``reclaim``
  rewrites ``available_at`` on every re-offer, so a deadline recomputed from the
  current value would extend unboundedly and the row would never die.  Grep this
  file for ``dead_by =`` and the only hit is the INSERT.
* **``reparent`` moves the row and rewrites the digests in ONE transaction.**
  Both, or neither.

Every timestamp column is TEXT in the fixed-width UTC rendering
``adapters/store/connection.py`` defines, because ``claim`` and ``reclaim``
compare them as STRINGS.  A variable-width rendering would make that ordering
depend on whether a microsecond happened to be zero.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from cli_agent_orchestrator.adapters.store.connection import (
    SqliteConnectionSource,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.delivery import (
    TERMINAL_STATES,
    AttemptOutcome,
    DeadLetter,
    DeadReason,
    DeadRow,
    DeliveryAttempt,
    EnqueueDraft,
    MsgKind,
    MsgState,
    QueueMessage,
    QueueMode,
    QueueOccupancy,
    ReclaimResult,
    SeatDigest,
    compute_dead_by,
    spends_attempt,
)
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.ports import Clock
from cli_agent_orchestrator.core.timing import (
    DELIVERY_BACKOFF_S,
    DELIVERY_LEASE_S,
    DELIVERY_MAX_LIFETIME_S,
    DELIVERY_RETENTION_DAYS,
    DELIVERY_VETO_CEILING_S,
)

__all__ = ["IdempotencyConflict", "SqliteQueueStore"]


class IdempotencyConflict(RuntimeError):
    """One idempotency key, two different payloads.

    A replay must return the existing message; a CHANGED body under the same key
    is a caller bug and fails loud (audit §3.2).  Silently returning the first
    message would hand the caller a success id for a message that was never
    enqueued, which is the silent-loss shape the whole phase exists to remove.
    """


_MSG_COLUMNS = (
    "msg_id, idempotency_key, payload_digest, receiver_id, sender_id, kind, payload, "
    "state, mode, claim_id, lease_owner, lease_expires_at, attempts, max_attempts, "
    "available_at, dead_by, held_since, expire_after_s, supersede_key, content_hash, "
    "park_warm, barrier_id, barrier_member_key, enqueue_generation, cancel_on_complete, "
    "is_notice, legacy_message_id, created_at, terminated_at"
)

_TERMINAL_VALUES = tuple(sorted(state.value for state in TERMINAL_STATES))


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _payload_digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SqliteQueueStore:
    """``core.ports.QueueStore`` over the server's SQLite file, WAL, one writer."""

    def __init__(self, pool: SqliteConnectionSource, *, clock: Clock | None = None) -> None:
        self._pool = pool
        self._clock = clock if clock is not None else _SystemClock()

    # -- enqueue ------------------------------------------------------------

    def enqueue(self, draft: EnqueueDraft) -> QueueMessage:
        """Insert one row, or return the existing one for a repeated key.

        ``INSERT … ON CONFLICT(idempotency_key) DO NOTHING`` then re-read, which
        is the audit's shape.  The re-read is not defensive tidiness: it is what
        makes the operation replay-safe under concurrency, since two callers can
        race the insert and exactly one wins, and both must come away with the
        same row.

        ``created_at`` and ``available_at`` are the same instant.  R1's
        no-delayed-enqueue rule is enforced HERE, by construction rather than by
        discipline: this method takes no delay parameter, so nothing can express
        one.  A phase that wants a delayed enqueue must change this signature,
        and the invariant that would break is named on
        :func:`~core.delivery.compute_dead_by`.
        """
        now = self._clock.now()
        digest = _payload_digest(draft.payload)
        conn = self._pool.connection()

        with immediate_transaction(conn):
            conn.execute(
                f"INSERT INTO delivery_msg ({_MSG_COLUMNS}) VALUES "
                "(" + ", ".join(["?"] * 29) + ") "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    new_ulid(),
                    draft.idempotency_key,
                    digest,
                    draft.receiver_id,
                    draft.sender_id,
                    draft.kind.value,
                    draft.payload,
                    MsgState.READY.value,
                    draft.mode.value,
                    0,
                    None,
                    None,
                    0,
                    draft.max_attempts,
                    render_timestamp(now),
                    render_timestamp(
                        compute_dead_by(
                            created_at=now,
                            available_at=now,
                            expire_after_s=draft.expire_after_s,
                        )
                    ),
                    None,
                    draft.expire_after_s,
                    draft.supersede_key,
                    draft.content_hash,
                    int(draft.park_warm),
                    draft.barrier_id,
                    draft.barrier_member_key,
                    draft.enqueue_generation,
                    int(draft.cancel_on_complete),
                    int(draft.is_notice),
                    draft.legacy_message_id,
                    render_timestamp(now),
                    None,
                ),
            )
            row = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE idempotency_key = ?",
                (draft.idempotency_key,),
            ).fetchone()

        if row is None:  # pragma: no cover — the insert either wrote or conflicted
            raise RuntimeError(f"delivery_msg row vanished for key {draft.idempotency_key!r}")
        message = _row_to_message(row)
        if message.payload_digest != digest:
            raise IdempotencyConflict(
                f"idempotency key {draft.idempotency_key!r} already holds a different payload"
            )
        return message

    # -- claim / ack / reclaim ---------------------------------------------

    def claim(
        self,
        *,
        lease_owner: str,
        now: datetime,
        limit: int = 1,
        receiver_id: str | None = None,
    ) -> list[QueueMessage]:
        """Lease deliverable rows and issue each a fresh fencing token.

        The ``mode='live'`` conjunct is in this statement and nowhere else.  A
        non-live row — one written by a build that still had the observational
        mode retired in #738 — is therefore unclaimable by construction, and the
        boot guard's occupancy test is redundant defence.  Removing the conjunct
        here makes such a row claimable, so the tick would inject a copy of a
        message the legacy path already delivered — a second carrier over one
        id.  That is the mutant the empirical gate kills.

        Two statements rather than a single ``UPDATE … RETURNING``: SQLite gained
        ``RETURNING`` in 3.35 and the fork supports older runtimes, so the
        selection and the lease are separated but run inside ONE
        ``BEGIN IMMEDIATE``, which gives the same guarantee — the write lock is
        held across both, so two claimers serialise here rather than discovering
        the conflict afterwards.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        lease_until = render_timestamp(
            datetime.fromtimestamp(now.timestamp() + DELIVERY_LEASE_S, tz=UTC)
        )
        claimed: list[QueueMessage] = []

        with immediate_transaction(conn):
            where = "state = 'ready' AND available_at <= ? AND mode = 'live' AND dead_by > ?"
            params: list[object] = [stamp, stamp]
            if receiver_id is not None:
                where += " AND receiver_id = ?"
                params.append(receiver_id)
            rows = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE {where} "
                "ORDER BY available_at, msg_id LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE delivery_msg SET state = 'leased', claim_id = claim_id + 1, "
                    "lease_owner = ?, lease_expires_at = ? "
                    "WHERE msg_id = ? AND state = 'ready'",
                    (lease_owner, lease_until, row["msg_id"]),
                )
                refreshed = conn.execute(
                    f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE msg_id = ?",
                    (row["msg_id"],),
                ).fetchone()
                claimed.append(_row_to_message(refreshed))
        return claimed

    def ack(self, msg_id: str, claim_id: int, *, now: datetime) -> bool:
        """Settle a delivered row.  False when the fencing token is stale."""
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE delivery_msg SET state = 'delivered', terminated_at = ?, "
                "lease_owner = NULL, lease_expires_at = NULL, held_since = NULL "
                "WHERE msg_id = ? AND claim_id = ? AND state = 'leased'",
                (stamp, msg_id, int(claim_id)),
            )
            return cursor.rowcount > 0

    def reclaim(self, *, now: datetime) -> ReclaimResult:
        """Return expired leases to ``ready``; dead-letter the exhausted.

        The statement the audit says replaces a 2,311-line watchdog service,
        plus the dead-letter move it names in the same row.  Note what is NOT in
        any ``SET`` clause here: ``dead_by``.  ``available_at`` moves by
        ``DELIVERY_BACKOFF_S`` on every re-offer and the deadline does not follow
        it, which is the whole of D12's once-only rule as code.

        **The increment is per outcome, not per re-offer** (D12's accounting
        column).  A blanket ``attempts = attempts + 1`` would put every outcome
        on one budget, and that is the r4 draft the design rejected: a worker
        behind an unknown-dialog episode, which waits on a human and routinely
        outlives five minutes, would dead-letter valid steers at 325 s, and after
        A1 a seat whose registry record is merely stale would lose its messages
        inside a window that heals on its own at 900 s.  So the row's LAST
        recorded outcome for the claim being reclaimed decides, through
        :func:`~core.delivery.spends_attempt`, and a lease that expired with
        nothing recorded spends one because nothing was observed.

        Three ways a row can die here, and each is reported rather than counted:

        * the attempt budget ran out — ``max_attempts``;
        * the dialog ceiling elapsed — ``veto_ceiling``, measured from
          ``held_since``, which the injector sets on the first ``veto_dialog``
          and any other outcome clears;
        * a time bound passed — ``max_lifetime``, or ``expired`` when the
          caller's own ``expire_after_s`` is what set the deadline.

        The caller raises the findings and enqueues the sender notice, because
        "no row reaches ``delivery_dead`` silently" is a commitment a count
        cannot keep (§13d, case 15).
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        backoff_until = render_timestamp(
            datetime.fromtimestamp(now.timestamp() + DELIVERY_BACKOFF_S, tz=UTC)
        )
        ceiling_before = render_timestamp(
            datetime.fromtimestamp(now.timestamp() - DELIVERY_VETO_CEILING_S, tz=UTC)
        )
        reoffered = 0
        incremented = 0
        dead: list[DeadRow] = []

        with immediate_transaction(conn):
            expired = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg "
                "WHERE state = 'leased' AND lease_expires_at < ?",
                (stamp,),
            ).fetchall()
            for row in expired:
                message = _row_to_message(row)
                outcome = self._last_outcome_in(conn, message.msg_id, message.claim_id)
                spends = spends_attempt(outcome)
                conn.execute(
                    "UPDATE delivery_msg SET state = 'ready', claim_id = claim_id + 1, "
                    "attempts = attempts + ?, available_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE msg_id = ? AND state = 'leased'",
                    (1 if spends else 0, backoff_until, message.msg_id),
                )
                reoffered += 1
                incremented += 1 if spends else 0

            # The dialog ceiling is a DURATION and is evaluated here rather than
            # by the injector: a row whose gate never clears is never injected
            # again, so a check that only ran on an injection would never fire
            # for the very case the ceiling exists to bound.
            held = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg "
                "WHERE state IN ('ready', 'leased') AND held_since IS NOT NULL "
                "AND held_since <= ?",
                (ceiling_before,),
            ).fetchall()
            for row in held:
                message = _row_to_message(row)
                self._kill(conn, message, reason=DeadReason.VETO_CEILING, now=now)
                dead.append(_dead_row(message, DeadReason.VETO_CEILING))

            exhausted = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg "
                "WHERE state = 'ready' AND (attempts >= max_attempts OR dead_by <= ?)",
                (stamp,),
            ).fetchall()
            for row in exhausted:
                message = _row_to_message(row)
                reason = _dead_reason_for(message, now=now)
                self._kill(conn, message, reason=reason, now=now)
                dead.append(_dead_row(message, reason))

        return ReclaimResult(reoffered=reoffered, incremented=incremented, dead=tuple(dead))

    @staticmethod
    def _last_outcome_in(
        conn: sqlite3.Connection, msg_id: str, claim_id: int
    ) -> AttemptOutcome | None:
        """The outcome recorded for THIS claim, or ``None`` if nothing was.

        Scoped to the claim rather than to the message: an earlier claim's
        ``pane_absent`` must not spend a second attempt for a lease that expired
        with the injector never running, and the fencing token is what separates
        the two.
        """
        row = conn.execute(
            "SELECT outcome FROM delivery_attempt WHERE msg_id = ? AND claim_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (msg_id, int(claim_id)),
        ).fetchone()
        if row is None:
            return None
        try:
            return AttemptOutcome(row["outcome"])
        except ValueError:  # pragma: no cover — an outcome this build cannot name
            return None

    # -- reads --------------------------------------------------------------

    def get(self, msg_id: str) -> QueueMessage | None:
        row = (
            self._pool.connection()
            .execute(f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE msg_id = ?", (msg_id,))
            .fetchone()
        )
        return None if row is None else _row_to_message(row)

    def get_by_idempotency_key(self, key: str) -> QueueMessage | None:
        row = (
            self._pool.connection()
            .execute(f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE idempotency_key = ?", (key,))
            .fetchone()
        )
        return None if row is None else _row_to_message(row)

    def attempts_for(self, msg_id: str) -> list[DeliveryAttempt]:
        rows = (
            self._pool.connection()
            .execute(
                "SELECT msg_id, claim_id, carrier, started_at, outcome, detail "
                "FROM delivery_attempt WHERE msg_id = ? ORDER BY claim_id, carrier",
                (msg_id,),
            )
            .fetchall()
        )
        return [
            DeliveryAttempt(
                msg_id=row["msg_id"],
                claim_id=row["claim_id"],
                carrier=row["carrier"],
                started_at=parse_timestamp(row["started_at"]),
                outcome=AttemptOutcome(row["outcome"]),
                detail=row["detail"] or "",
            )
            for row in rows
        ]

    def dead_letter(self, msg_id: str) -> DeadLetter | None:
        row = (
            self._pool.connection()
            .execute(
                "SELECT msg_id, idempotency_key, receiver_id, payload, attempts, reason, "
                "mode, died_at FROM delivery_dead WHERE msg_id = ?",
                (msg_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return DeadLetter(
            msg_id=row["msg_id"],
            idempotency_key=row["idempotency_key"] or "",
            receiver_id=row["receiver_id"] or "",
            payload=row["payload"] or "",
            attempts=row["attempts"],
            reason=DeadReason(row["reason"]),
            mode=QueueMode(row["mode"]),
            died_at=parse_timestamp(row["died_at"]),
        )

    def all_rows(self, *, mode: QueueMode | None = None) -> list[QueueMessage]:
        """Every row, for the AC-3a report.

        A full read is the right shape here and only here: the report compares
        the whole population against the whole legacy inbox, and paging it would
        let a row enqueued mid-report appear on one side and not the other.  No
        server path calls this — ``claim`` is how the tick reads rows.
        """
        where = "" if mode is None else " WHERE mode = ?"
        params = () if mode is None else (mode.value,)
        rows = (
            self._pool.connection()
            .execute(f"SELECT {_MSG_COLUMNS} FROM delivery_msg{where} ORDER BY msg_id", params)
            .fetchall()
        )
        return [_row_to_message(row) for row in rows]

    def occupancy(self) -> QueueOccupancy:
        """D9's two predicates, read in one place.

        The live non-terminal count, and the open-barrier labels.  The barrier
        half reads the LEGACY ``callback_barrier`` table, because that is where
        barrier state lives and phase 3 does not reproduce it — D13 carries the
        association into the queue's enqueue but leaves the barrier tables
        untouched.  A missing table is not an error here: on a deployment whose
        barrier schema predates this column set, "no barrier is open" is the
        honest reading and the alternative would be a boot that cannot resolve
        its own switch.
        """
        conn = self._pool.connection()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM delivery_msg WHERE mode = 'live' "
            f"AND state NOT IN ({', '.join('?' * len(_TERMINAL_VALUES))})",
            _TERMINAL_VALUES,
        ).fetchone()
        live = int(row["n"]) if row is not None else 0

        labels: tuple[str, ...] = ()
        try:
            barrier_rows = conn.execute(
                "SELECT label FROM callback_barrier WHERE state = 'OPEN' ORDER BY label"
            ).fetchall()
            labels = tuple(str(barrier["label"]) for barrier in barrier_rows)
        except sqlite3.Error:
            labels = ()
        return QueueOccupancy(live_non_terminal=live, open_barrier_labels=labels)

    def count(self, *, mode: QueueMode | None = None) -> int:
        conn = self._pool.connection()
        if mode is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM delivery_msg").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM delivery_msg WHERE mode = ?", (mode.value,)
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    # -- writes -------------------------------------------------------------

    def record_attempt(self, attempt: DeliveryAttempt) -> None:
        """Write one attempt row, idempotently on its primary key.

        ``ON CONFLICT DO NOTHING`` rather than an upsert: an attempt is a
        historical fact, and the mirror writer can legitimately observe the same
        legacy attempt twice (two edges fire for one settle).  Overwriting would
        let the second observation rewrite the first one's outcome.
        """
        self._pool.connection().execute(
            "INSERT INTO delivery_attempt "
            "(msg_id, claim_id, carrier, started_at, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                attempt.msg_id,
                attempt.claim_id,
                attempt.carrier,
                render_timestamp(attempt.started_at),
                attempt.outcome.value,
                attempt.detail,
            ),
        )

    def settle(
        self,
        msg_id: str,
        *,
        state: MsgState,
        now: datetime,
        reason: DeadReason | None = None,
        attempts: int | None = None,
    ) -> bool:
        """Move a row to a terminal state.  False when it was already terminal.

        Terminal states are final.  A late edge arriving after the row has
        ended must not rewrite the recorded outcome — the mirror writer observes
        several legacy edges per message and they do not arrive in a guaranteed
        order, so "first terminal observation wins" is the only rule that gives
        a stable comparison.
        """
        if state not in TERMINAL_STATES:
            raise ValueError(f"settle() takes a terminal state, not {state.value!r}")
        if state is MsgState.DEAD and reason is None:
            raise ValueError("a dead row needs a reason (I1: reason distinguishes the four)")

        conn = self._pool.connection()
        with immediate_transaction(conn):
            row = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            if row is None:
                return False
            message = _row_to_message(row)
            if message.terminal:
                return False
            if attempts is not None:
                conn.execute(
                    "UPDATE delivery_msg SET attempts = ? WHERE msg_id = ?",
                    (int(attempts), msg_id),
                )
                message = message.model_copy(update={"attempts": int(attempts)})
            if state is MsgState.DEAD:
                assert reason is not None  # narrowed above; kept for mypy --strict
                self._kill(conn, message, reason=reason, now=now)
            else:
                conn.execute(
                    "UPDATE delivery_msg SET state = ?, terminated_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, held_since = NULL "
                    "WHERE msg_id = ?",
                    (state.value, render_timestamp(now), msg_id),
                )
            return True

    def mark_dialog_hold(self, msg_id: str, *, held_since: datetime | None) -> None:
        """Set or clear the dialog-hold clock (D12)."""
        self._pool.connection().execute(
            "UPDATE delivery_msg SET held_since = ? WHERE msg_id = ?",
            (None if held_since is None else render_timestamp(held_since), msg_id),
        )

    def _kill(
        self,
        conn: sqlite3.Connection,
        message: QueueMessage,
        *,
        reason: DeadReason,
        now: datetime,
    ) -> None:
        """Move one row to ``dead`` and write its ``delivery_dead`` row.

        Called only from inside an open transaction, so the state change and the
        dead-letter row commit together: a dead row with no dead-letter entry
        would be a message whose ending exists in one table and not the other,
        and I5's "one query returns a msg_id's full history" would be false.
        """
        conn.execute(
            "UPDATE delivery_msg SET state = 'dead', terminated_at = ?, "
            "lease_owner = NULL, lease_expires_at = NULL WHERE msg_id = ?",
            (render_timestamp(now), message.msg_id),
        )
        conn.execute(
            "INSERT INTO delivery_dead "
            "(msg_id, idempotency_key, receiver_id, payload, attempts, reason, mode, died_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(msg_id) DO NOTHING",
            (
                message.msg_id,
                message.idempotency_key,
                message.receiver_id,
                message.payload,
                message.attempts,
                reason.value,
                message.mode.value,
                render_timestamp(now),
            ),
        )

    # -- the digest ---------------------------------------------------------

    def open_digest(self, receiver_id: str) -> SeatDigest | None:
        row = (
            self._pool.connection()
            .execute(
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, wake_count "
                "FROM seat_digest WHERE receiver_id = ? AND consumed_at IS NULL "
                "ORDER BY epoch DESC LIMIT 1",
                (receiver_id,),
            )
            .fetchone()
        )
        return None if row is None else _row_to_digest(row)

    def reparent(
        self,
        msg_id: str,
        *,
        new_receiver_id: str,
        now: datetime,
        message_prefix: str = "",
    ) -> bool:
        """Move an undelivered row to another mailbox, digests and all.

        ONE transaction.  The row's ``receiver_id`` moves, the id leaves the old
        receiver's open epoch, and it joins the new receiver's open epoch —
        opening one if none is open.  An epoch left empty by the move closes
        immediately as ``abandoned``: the trigger is a reap, so the old receiver
        has no live incarnation, and an empty set satisfies the
        every-message-terminal test vacuously, which makes ``abandoned`` the
        correct value rather than ``cancelled``.

        ``message_prefix`` carries legacy's ``[released from … terminal reaped]``
        marker, so the new reader can see where the row came from.  That is why
        reaping is CARRIED rather than left to the abandon rule: mailbox
        addressing spans incarnations of the SAME mailbox, but a reap moves rows
        to a DIFFERENT one so the caller learns what its worker was owed, and
        letting those rows die quietly at their deadline would lose that.

        Returns False for a row that does not exist or has already ended.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            row = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            if row is None:
                return False
            message = _row_to_message(row)
            if message.terminal:
                return False
            old_receiver = message.receiver_id
            if old_receiver == new_receiver_id:
                return False

            payload = f"{message_prefix}{message.payload}" if message_prefix else message.payload
            conn.execute(
                "UPDATE delivery_msg SET receiver_id = ?, payload = ?, payload_digest = ? "
                "WHERE msg_id = ?",
                (new_receiver_id, payload, _payload_digest(payload), msg_id),
            )

            old = self._open_digest_in(conn, old_receiver)
            if old is not None and msg_id in old.msg_ids:
                remaining = tuple(mid for mid in old.msg_ids if mid != msg_id)
                if remaining:
                    conn.execute(
                        "UPDATE seat_digest SET msg_ids = ? WHERE receiver_id = ? AND epoch = ?",
                        (json.dumps(list(remaining)), old.receiver_id, old.epoch),
                    )
                else:
                    conn.execute(
                        "UPDATE seat_digest SET msg_ids = ?, consumed_at = ?, consumed_via = ? "
                        "WHERE receiver_id = ? AND epoch = ?",
                        ("[]", render_timestamp(now), "abandoned", old.receiver_id, old.epoch),
                    )

            new = self._open_digest_in(conn, new_receiver_id)
            if new is None:
                next_epoch = self._next_epoch_in(conn, new_receiver_id)
                conn.execute(
                    "INSERT INTO seat_digest "
                    "(receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, "
                    "wake_count) VALUES (?, ?, ?, ?, NULL, NULL, 0)",
                    (
                        new_receiver_id,
                        next_epoch,
                        json.dumps([msg_id]),
                        render_timestamp(now),
                    ),
                )
            elif msg_id not in new.msg_ids:
                conn.execute(
                    "UPDATE seat_digest SET msg_ids = ? WHERE receiver_id = ? AND epoch = ?",
                    (json.dumps([*new.msg_ids, msg_id]), new.receiver_id, new.epoch),
                )
            return True

    def build_digest(
        self, receiver_id: str, msg_ids: tuple[str, ...], *, now: datetime
    ) -> SeatDigest:
        """Open an epoch holding ``msg_ids``.  Test and re-parent support in 3a.

        The tick that opens epochs from ``ready`` rows is a 3b item; this is the
        primitive it will call, present now because ``reparent`` needs to be
        testable against a real digest rather than against nothing.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            epoch = self._next_epoch_in(conn, receiver_id)
            conn.execute(
                "INSERT INTO seat_digest "
                "(receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, "
                "wake_count) VALUES (?, ?, ?, ?, NULL, NULL, 0)",
                (receiver_id, epoch, json.dumps(list(msg_ids)), render_timestamp(now)),
            )
        digest = self.open_digest(receiver_id)
        assert digest is not None  # just inserted, inside the same connection
        return digest

    def digest_at(self, receiver_id: str, epoch: int) -> SeatDigest | None:
        row = (
            self._pool.connection()
            .execute(
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, wake_count "
                "FROM seat_digest WHERE receiver_id = ? AND epoch = ?",
                (receiver_id, int(epoch)),
            )
            .fetchone()
        )
        return None if row is None else _row_to_digest(row)

    def extend_digest(
        self, receiver_id: str, epoch: int, msg_ids: tuple[str, ...], *, now: datetime
    ) -> SeatDigest | None:
        """Add ids to an OPEN epoch, strictly additively (§5 item 6).

        The array is otherwise immutable: nothing here removes an id or moves
        one, and the single event that rewrites a digest — a re-parent moving an
        id out of one epoch and into another — is :meth:`reparent`'s own
        transaction.  What this covers is the row that arrives WHILE an epoch is
        open, which would otherwise belong to no epoch at all until that epoch
        closed and would reach ``dead_by`` never having been woken about, behind
        a seat that is not acking.

        Returns the digest unchanged when it holds the ids already, and ``None``
        when the epoch is closed or absent — a closed epoch is terminal, so a
        later arrival opens a NEW one.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            row = conn.execute(
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, "
                "wake_count FROM seat_digest WHERE receiver_id = ? AND epoch = ? "
                "AND consumed_at IS NULL",
                (receiver_id, int(epoch)),
            ).fetchone()
            if row is None:
                return None
            digest = _row_to_digest(row)
            fresh = tuple(mid for mid in msg_ids if mid not in digest.msg_ids)
            if not fresh:
                return digest
            merged = (*digest.msg_ids, *fresh)
            conn.execute(
                "UPDATE seat_digest SET msg_ids = ? WHERE receiver_id = ? AND epoch = ?",
                (json.dumps(list(merged)), receiver_id, int(epoch)),
            )
            return digest.model_copy(update={"msg_ids": merged})

    def bump_wake_count(self, receiver_id: str, epoch: int, *, now: datetime) -> int:
        """Advance the wake ordinal for one open epoch and return its new value.

        Called ONCE PER LEASE PERIOD in which the epoch is re-offered, inside the
        transaction that opens that lease's wake — never once per emission.  The
        two readings differ observably (§A1.2): a re-emission inside one lease
        re-sends the identical line by design, so the transport's content window
        drops it, which is I3 enforced at the transport rather than asserted
        about; each NEW lease's wake carries a fresh ordinal, so it is a distinct
        hash and passes the window, and the window's 20-entry depth stops
        mattering.

        Returns 0 for an epoch that is closed or absent, which the caller reads
        as "do not emit": a wake for a consumed epoch is unreachable rather than
        suppressed (I4).
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE seat_digest SET wake_count = wake_count + 1 "
                "WHERE receiver_id = ? AND epoch = ? AND consumed_at IS NULL",
                (receiver_id, int(epoch)),
            )
            if cursor.rowcount == 0:
                return 0
            row = conn.execute(
                "SELECT wake_count FROM seat_digest WHERE receiver_id = ? AND epoch = ?",
                (receiver_id, int(epoch)),
            ).fetchone()
            return 0 if row is None else int(row["wake_count"])

    def close_digest(self, receiver_id: str, epoch: int, *, via: str, now: datetime) -> bool:
        """Close an OPEN epoch on consumption, cancellation or abandonment (D10).

        Open only, and that qualification is the whole of #568's fix here: a late
        tick must not overwrite a recorded ``mcp_ack`` with its own closure, and
        a consumed epoch is terminal, so a later arrival opens a NEW epoch rather
        than reopening this one.

        Returns False when the epoch was already closed or does not exist.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE seat_digest SET consumed_at = ?, consumed_via = ? "
                "WHERE receiver_id = ? AND epoch = ? AND consumed_at IS NULL",
                (render_timestamp(now), via, receiver_id, int(epoch)),
            )
            return cursor.rowcount > 0

    def open_digests(self) -> list[SeatDigest]:
        """Every open epoch, oldest first — the tick's re-emit and closure set."""
        rows = (
            self._pool.connection()
            .execute(
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, "
                "wake_count FROM seat_digest WHERE consumed_at IS NULL "
                "ORDER BY built_at, receiver_id, epoch"
            )
            .fetchall()
        )
        return [_row_to_digest(row) for row in rows]

    def ready_receivers(self) -> list[str]:
        """Receivers holding at least one claimable row, oldest arrival first.

        ``mode='live'`` here as well as in ``claim``: the filter's home is the
        claim statement, and this is the redundant defence D9 names rather than
        the enforcement.  A receiver whose only rows are non-live leftovers
        (#738) must not have an epoch opened for it, or the tick would wake a
        seat about messages the legacy path already delivered.
        """
        rows = (
            self._pool.connection()
            .execute(
                "SELECT receiver_id, MIN(created_at) AS first_at FROM delivery_msg "
                "WHERE state IN ('ready', 'leased') AND mode = 'live' "
                "GROUP BY receiver_id ORDER BY first_at, receiver_id"
            )
            .fetchall()
        )
        return [str(row["receiver_id"]) for row in rows]

    def undelivered_ids(self, receiver_id: str) -> tuple[str, ...]:
        """The receiver's non-terminal live ids, in arrival order.

        What an epoch is opened over.  ``leased`` counts as well as ``ready``:
        an epoch built from ``ready`` alone would drop a row the tick had just
        claimed, and the digest would then under-report what the receiver is
        owed for the life of that epoch.
        """
        rows = (
            self._pool.connection()
            .execute(
                "SELECT msg_id FROM delivery_msg WHERE receiver_id = ? AND mode = 'live' "
                "AND state IN ('ready', 'leased') ORDER BY created_at, msg_id",
                (receiver_id,),
            )
            .fetchall()
        )
        return tuple(str(row["msg_id"]) for row in rows)

    def senders_of(self, msg_ids: tuple[str, ...]) -> tuple[str, ...]:
        """The sender ids behind an epoch's messages, in arrival order.

        §A1.1's sender rule is a function of this tuple, and it is read from the
        rows rather than remembered on the digest so a re-parent or a supersede
        cannot leave the wake naming a sender the epoch no longer has.
        """
        if not msg_ids:
            return ()
        placeholders = ",".join("?" for _ in msg_ids)
        rows = (
            self._pool.connection()
            .execute(
                f"SELECT sender_id FROM delivery_msg WHERE msg_id IN ({placeholders}) "
                "ORDER BY created_at, msg_id",
                tuple(msg_ids),
            )
            .fetchall()
        )
        return tuple(str(row["sender_id"] or "") for row in rows)

    def all_terminal(self, msg_ids: tuple[str, ...]) -> bool:
        """True when every id has reached a terminal state (D10's first conjunct).

        An EMPTY set is terminal vacuously, which is what makes ``abandoned`` the
        right value for an epoch a re-parent emptied.
        """
        if not msg_ids:
            return True
        placeholders = ",".join("?" for _ in msg_ids)
        row = (
            self._pool.connection()
            .execute(
                f"SELECT COUNT(*) AS n FROM delivery_msg WHERE msg_id IN ({placeholders}) "
                f"AND state NOT IN ({','.join('?' for _ in _TERMINAL_VALUES)})",
                (*msg_ids, *_TERMINAL_VALUES),
            )
            .fetchone()
        )
        return row is None or int(row["n"]) == 0

    def cancel_on_complete(self, receiver_id: str, *, now: datetime) -> tuple[str, ...]:
        """D8's completion-cancel: supersede this receiver's flagged READY rows.

        Evaluated ONCE PER COMPLETION EVENT, not as a standing predicate over
        ``ready`` rows, and that is what keeps its limit true: a steer reclaimed
        to ``ready`` after the completion is not retroactively cancelled.
        ``ready`` only, so a steer already leased at completion still lands, and
        it stays diagnosable through ``cao diag <msg_id>``.

        This is the mechanism that actually reaches #435, where the aged steer is
        addressed to the worker and the completion callback to the supervisor, so
        no newer row lands in the worker's mailbox and ``supersede_key`` alone
        never fires.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        cancelled: list[str] = []
        with immediate_transaction(conn):
            rows = conn.execute(
                "SELECT msg_id FROM delivery_msg WHERE receiver_id = ? AND state = 'ready' "
                "AND cancel_on_complete = 1 AND mode = 'live'",
                (receiver_id,),
            ).fetchall()
            for row in rows:
                msg_id = str(row["msg_id"])
                conn.execute(
                    "UPDATE delivery_msg SET state = 'superseded', terminated_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, held_since = NULL "
                    "WHERE msg_id = ? AND state = 'ready'",
                    (stamp, msg_id),
                )
                cancelled.append(msg_id)
        return tuple(cancelled)

    def next_surrogate_id(self) -> int:
        """The integer handle a write-through row carries in place of an inbox id.

        §6 makes the legacy inbox READ-ONLY from the flip, so at ``on`` no
        ``inbox_messages`` row is written and its autoincrement never advances.
        The public surface is still integer-keyed — ``message_id`` in the HTTP
        response, ``up_to_id`` in ``ack_messages``, ``member.message_id`` on a
        barrier — so the queue mints the integer instead, and stores it in
        ``legacy_message_id`` where ``cao diag`` and the mirror already look.

        The floor is ``MAX`` over BOTH tables. Taking only the queue's own column
        would hand out an id a historical inbox row already used, and an
        ``ack_messages(up_to_id=N)`` would then settle across the boundary
        between the two eras. Allocated inside the caller's transaction, which is
        the single writer.
        """
        conn = self._pool.connection()
        high = 0
        row = conn.execute("SELECT MAX(legacy_message_id) AS v FROM delivery_msg").fetchone()
        if row is not None and row["v"] is not None:
            high = int(row["v"])
        try:
            # The legacy table shares this file but belongs to the other tree, so
            # it is read defensively: a deployment whose inbox has not been
            # created yet is a valid state, and the queue's own high-water is
            # then the whole floor.
            legacy = conn.execute("SELECT MAX(id) AS v FROM inbox").fetchone()
        except sqlite3.Error:
            legacy = None
        if legacy is not None and legacy["v"] is not None:
            high = max(high, int(legacy["v"]))
        return high + 1

    def find_recent_duplicate(
        self,
        *,
        sender_id: str,
        receiver_id: str,
        content_hash: str,
        window_s: int,
        now: datetime,
        park_warm: bool = False,
        barrier_id: int | None = None,
    ) -> QueueMessage | None:
        """D13's F475 window check, reproduced with all five conjuncts.

        Same sender, same receiver, matching content hash, ``park_warm`` not true
        and ``barrier_id`` null, inside a rolling window. Reproduced rather than
        approximated because the legacy predicate is what decides how many
        messages are delivered, and the queue's ``idempotency_key`` constraint
        has neither the window nor any conjunct: two identical sends more than a
        minute apart are ordinary traffic here and must both land.

        Returns the existing row so the caller can hand it back, which is what
        legacy returns today — never a fabricated success id for a message that
        was not enqueued.
        """
        if park_warm or barrier_id is not None or not content_hash:
            return None
        cutoff = render_timestamp(datetime.fromtimestamp(now.timestamp() - window_s, tz=UTC))
        row = (
            self._pool.connection()
            .execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg "
                "WHERE sender_id = ? AND receiver_id = ? AND content_hash = ? "
                "AND created_at >= ? AND park_warm = 0 AND barrier_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (sender_id, receiver_id, content_hash, cutoff),
            )
            .fetchone()
        )
        return None if row is None else _row_to_message(row)

    def pending_for_receiver(
        self, receiver_id: str, *, after_id: int = 0, limit: int = 25
    ) -> list[QueueMessage]:
        """The receiver's undelivered live rows, in surrogate-id order.

        What ``list_messages`` serves from once the queue owns new traffic. The
        ordering and the ``after_id`` cursor are the legacy call's, so the seat's
        drain loop is unchanged on the other side of the flip.
        """
        rows = (
            self._pool.connection()
            .execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE receiver_id = ? "
                "AND mode = 'live' AND state IN ('ready', 'leased') "
                "AND legacy_message_id > ? "
                "ORDER BY legacy_message_id LIMIT ?",
                (receiver_id, int(after_id), int(limit)),
            )
            .fetchall()
        )
        return [_row_to_message(row) for row in rows]

    def settle_through(self, receiver_id: str, *, up_to_id: int, now: datetime) -> tuple[str, ...]:
        """Mark the receiver's rows delivered up to a surrogate id (§5b's ack).

        The cursor semantics are legacy's, unchanged: everything at or below the
        id the seat names is settled, and the digest stamp is the caller's to
        write. Returns the ids settled so the caller can close the covering
        epoch with ``consumed_via='mcp_ack'``.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        settled: list[str] = []
        with immediate_transaction(conn):
            rows = conn.execute(
                "SELECT msg_id FROM delivery_msg WHERE receiver_id = ? AND mode = 'live' "
                "AND legacy_message_id IS NOT NULL AND legacy_message_id <= ? "
                f"AND state NOT IN ({','.join('?' for _ in _TERMINAL_VALUES)})",
                (receiver_id, int(up_to_id), *_TERMINAL_VALUES),
            ).fetchall()
            for row in rows:
                msg_id = str(row["msg_id"])
                conn.execute(
                    "UPDATE delivery_msg SET state = 'delivered', terminated_at = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, held_since = NULL "
                    "WHERE msg_id = ?",
                    (stamp, msg_id),
                )
                settled.append(msg_id)
        return tuple(settled)

    def prune(self, *, now: datetime, protected: frozenset[str] = frozenset()) -> int:
        """Retention over the new tables (§13d), returning rows removed.

        Terminal ``delivery_msg`` rows past ``DELIVERY_RETENTION_DAYS`` go with
        their ``delivery_attempt`` rows and their ``delivery_dead`` entry, and a
        CLOSED digest of the same age goes too.  Two things never go:

        * an **open** digest, whatever its age — that is the record of what is
          owed while its messages live, which is the correction to §5 item 1's
          blanket exclusion;
        * any row in ``protected``, the ids named by an OPEN finding.  Phase 1
          carries the same rule for events, and its docstring calls it keeping
          open evidence: a finding that points at a pruned row is a diagnosis
          with its evidence deleted, which is the pane archaeology I5 exists to
          end.
        """
        horizon = render_timestamp(
            datetime.fromtimestamp(now.timestamp() - DELIVERY_RETENTION_DAYS * 86400.0, tz=UTC)
        )
        conn = self._pool.connection()
        removed = 0
        with immediate_transaction(conn):
            rows = conn.execute(
                "SELECT msg_id FROM delivery_msg WHERE terminated_at IS NOT NULL "
                "AND terminated_at < ?",
                (horizon,),
            ).fetchall()
            for row in rows:
                msg_id = str(row["msg_id"])
                if msg_id in protected:
                    continue
                conn.execute("DELETE FROM delivery_attempt WHERE msg_id = ?", (msg_id,))
                conn.execute("DELETE FROM delivery_dead WHERE msg_id = ?", (msg_id,))
                conn.execute("DELETE FROM delivery_msg WHERE msg_id = ?", (msg_id,))
                removed += 1
            digests = conn.execute(
                "SELECT receiver_id, epoch, msg_ids FROM seat_digest "
                "WHERE consumed_at IS NOT NULL AND consumed_at < ?",
                (horizon,),
            ).fetchall()
            for row in digests:
                ids = json.loads(row["msg_ids"]) if row["msg_ids"] else []
                if any(str(value) in protected for value in ids):
                    continue
                conn.execute(
                    "DELETE FROM seat_digest WHERE receiver_id = ? AND epoch = ?",
                    (row["receiver_id"], row["epoch"]),
                )
                removed += 1
        return removed

    @staticmethod
    def _open_digest_in(conn: sqlite3.Connection, receiver_id: str) -> SeatDigest | None:
        row = conn.execute(
            "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via, wake_count "
            "FROM seat_digest WHERE receiver_id = ? AND consumed_at IS NULL "
            "ORDER BY epoch DESC LIMIT 1",
            (receiver_id,),
        ).fetchone()
        return None if row is None else _row_to_digest(row)

    @staticmethod
    def _next_epoch_in(conn: sqlite3.Connection, receiver_id: str) -> int:
        """One above the receiver's highest existing epoch (§5 item 1).

        A persisted per-receiver integer, and deliberately not reused after a
        digest closes: a consumed epoch is terminal, so a later arrival opens a
        NEW epoch rather than reopening the old one, which is what makes #568
        unreachable rather than filtered.
        """
        row = conn.execute(
            "SELECT MAX(epoch) AS high FROM seat_digest WHERE receiver_id = ?",
            (receiver_id,),
        ).fetchone()
        high = 0 if row is None or row["high"] is None else int(row["high"])
        return high + 1


def _row_to_message(row: sqlite3.Row) -> QueueMessage:
    return QueueMessage(
        msg_id=row["msg_id"],
        idempotency_key=row["idempotency_key"],
        payload_digest=row["payload_digest"] or "",
        receiver_id=row["receiver_id"],
        sender_id=row["sender_id"] or "",
        kind=MsgKind(row["kind"]),
        payload=row["payload"] or "",
        state=MsgState(row["state"]),
        mode=QueueMode(row["mode"]),
        claim_id=row["claim_id"],
        lease_owner=row["lease_owner"],
        lease_expires_at=_maybe_time(row["lease_expires_at"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=parse_timestamp(row["available_at"]),
        dead_by=parse_timestamp(row["dead_by"]),
        held_since=_maybe_time(row["held_since"]),
        expire_after_s=row["expire_after_s"],
        supersede_key=row["supersede_key"],
        content_hash=row["content_hash"],
        park_warm=bool(row["park_warm"]),
        barrier_id=row["barrier_id"],
        barrier_member_key=row["barrier_member_key"],
        enqueue_generation=row["enqueue_generation"],
        cancel_on_complete=bool(row["cancel_on_complete"]),
        is_notice=bool(row["is_notice"]),
        legacy_message_id=row["legacy_message_id"],
        created_at=parse_timestamp(row["created_at"]),
        terminated_at=_maybe_time(row["terminated_at"]),
    )


def _dead_reason_for(message: QueueMessage, *, now: datetime) -> DeadReason:
    """Which of I1's four reasons ended this row.

    The attempt budget first, because it is the one an operator can act on.
    Then the two time bounds, and the split between them is NOT "did the caller
    supply an expiry" but "did the caller's expiry SET the deadline": a message
    sent with ``expire_after_s`` longer than ``DELIVERY_MAX_LIFETIME_S`` dies on
    the lifetime, and reporting that as ``expired`` would tell a reader the
    caller asked for a death the caller did not ask for.
    """
    if message.attempts >= message.max_attempts:
        return DeadReason.MAX_ATTEMPTS
    if message.expire_after_s is not None and message.expire_after_s <= DELIVERY_MAX_LIFETIME_S:
        return DeadReason.EXPIRED
    return DeadReason.MAX_LIFETIME


def _dead_row(message: QueueMessage, reason: DeadReason) -> DeadRow:
    return DeadRow(
        msg_id=message.msg_id,
        receiver_id=message.receiver_id,
        sender_id=message.sender_id,
        reason=reason,
        is_notice=message.is_notice,
        attempts=message.attempts,
    )


def _row_to_digest(row: sqlite3.Row) -> SeatDigest:
    raw = json.loads(row["msg_ids"]) if row["msg_ids"] else []
    return SeatDigest(
        receiver_id=row["receiver_id"],
        epoch=row["epoch"],
        msg_ids=tuple(str(value) for value in raw),
        built_at=parse_timestamp(row["built_at"]),
        consumed_at=_maybe_time(row["consumed_at"]),
        consumed_via=row["consumed_via"],
        wake_count=int(row["wake_count"] or 0),
    )


def _maybe_time(value: str | None) -> datetime | None:
    return None if value is None else parse_timestamp(value)

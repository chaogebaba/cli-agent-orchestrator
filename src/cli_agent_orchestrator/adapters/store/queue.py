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
  forget it.  Sub-phase 3a writes shadow rows into this table before there is a
  claimer at all, so the rule is in force before there is anything for it to
  guard, which is the only ordering that could have caught it.
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
    DeliveryAttempt,
    EnqueueDraft,
    MsgKind,
    MsgState,
    QueueMessage,
    QueueMode,
    QueueOccupancy,
    SeatDigest,
    compute_dead_by,
)
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.ports import Clock
from cli_agent_orchestrator.core.timing import DELIVERY_BACKOFF_S, DELIVERY_LEASE_S

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
        shadow row is therefore unclaimable by construction: the boot guard's
        occupancy test and the drain tick's own mode condition are redundant
        defence, and the write-through flip's sweep of unresolved shadow rows is
        a third, independent one.  Removing the conjunct here makes a surviving
        shadow row claimable at the flip, so the tick would inject a copy of a
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

    def reclaim(self, *, now: datetime) -> tuple[int, int]:
        """Return expired leases to ``ready``; dead-letter the exhausted.

        The single statement the audit says replaces a 2,311-line watchdog
        service, plus the dead-letter move it names in the same row.  Note what
        is NOT in the ``SET`` clause: ``dead_by``.  ``available_at`` moves by
        ``DELIVERY_BACKOFF_S`` on every re-offer and the deadline does not
        follow it, which is the whole of D12's once-only rule as code.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        backoff_until = render_timestamp(
            datetime.fromtimestamp(now.timestamp() + DELIVERY_BACKOFF_S, tz=UTC)
        )
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE delivery_msg SET state = 'ready', claim_id = claim_id + 1, "
                "attempts = attempts + 1, available_at = ?, "
                "lease_owner = NULL, lease_expires_at = NULL "
                "WHERE state = 'leased' AND lease_expires_at < ?",
                (backoff_until, stamp),
            )
            reclaimed = cursor.rowcount
            exhausted = conn.execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg "
                "WHERE state = 'ready' AND (attempts >= max_attempts OR dead_by <= ?)",
                (stamp,),
            ).fetchall()
            for row in exhausted:
                message = _row_to_message(row)
                reason = (
                    DeadReason.MAX_ATTEMPTS
                    if message.attempts >= message.max_attempts
                    else (
                        DeadReason.EXPIRED
                        if message.expire_after_s is not None
                        else DeadReason.MAX_LIFETIME
                    )
                )
                self._kill(conn, message, reason=reason, now=now)
            return reclaimed, len(exhausted)

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

    def get_by_legacy_id(self, legacy_message_id: int) -> QueueMessage | None:
        """The shadow row mirroring one legacy inbox row (sub-phase 3a only)."""
        row = (
            self._pool.connection()
            .execute(
                f"SELECT {_MSG_COLUMNS} FROM delivery_msg WHERE legacy_message_id = ?",
                (int(legacy_message_id),),
            )
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
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via "
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
                    "(receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via) "
                    "VALUES (?, ?, ?, ?, NULL, NULL)",
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
                "(receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via) "
                "VALUES (?, ?, ?, ?, NULL, NULL)",
                (receiver_id, epoch, json.dumps(list(msg_ids)), render_timestamp(now)),
            )
        digest = self.open_digest(receiver_id)
        assert digest is not None  # just inserted, inside the same connection
        return digest

    def digest_at(self, receiver_id: str, epoch: int) -> SeatDigest | None:
        row = (
            self._pool.connection()
            .execute(
                "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via "
                "FROM seat_digest WHERE receiver_id = ? AND epoch = ?",
                (receiver_id, int(epoch)),
            )
            .fetchone()
        )
        return None if row is None else _row_to_digest(row)

    @staticmethod
    def _open_digest_in(conn: sqlite3.Connection, receiver_id: str) -> SeatDigest | None:
        row = conn.execute(
            "SELECT receiver_id, epoch, msg_ids, built_at, consumed_at, consumed_via "
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


def _row_to_digest(row: sqlite3.Row) -> SeatDigest:
    raw = json.loads(row["msg_ids"]) if row["msg_ids"] else []
    return SeatDigest(
        receiver_id=row["receiver_id"],
        epoch=row["epoch"],
        msg_ids=tuple(str(value) for value in raw),
        built_at=parse_timestamp(row["built_at"]),
        consumed_at=_maybe_time(row["consumed_at"]),
        consumed_via=row["consumed_via"],
    )


def _maybe_time(value: str | None) -> datetime | None:
    return None if value is None else parse_timestamp(value)

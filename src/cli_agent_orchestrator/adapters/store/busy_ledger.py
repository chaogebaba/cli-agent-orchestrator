"""D7.3 — the busy-paused lifetime clock, as two writes and one recompute.

An ACP agent that is mid-turn is not failing.  It is doing the thing it was asked
to do, and a turn routinely outlives the attempt budget's 325-second span.  So a
row waiting behind one may not be charged an attempt (that is
``ACP_BUSY_RETRY``'s accounting, in ``core/delivery.py``) and may not be killed
by a wall clock that counted the wait as idleness — which is what this module
fixes, by PAUSING the lifetime clock for the duration of the wait.

Three rules, each of which is an AC-S1.13 fails-if:

* **Closing an episode ADDS its elapsed interval.**  r2's bug discarded it: the
  marker was cleared and the accumulator was left alone, so a row that had waited
  four minutes across two episodes had credit for neither.  :func:`close_episode`
  is the only writer of the accumulator and it always adds.

* **The accumulator is CAPPED.**  ``BUSY_CREDIT_CAP_S`` bounds the total credit a
  row can ever hold, so one ledger error cannot extend a row indefinitely — and
  the cap is what keeps the row's wall-clock age inside ``IDLE_STALL_AGE_S``,
  which is AC-S1.17 clause 3 and the #568 non-overlap property.

* **No row survives with an uncleared ``busy_since`` past its lease.**  A lease
  that expires without a nudge closes the episode through the ORDINARY reclaim
  path (:func:`close_expired_episodes`), so a crashed driver leaves a stale
  marker for at most one lease rather than forever.

And one rule that is A2.9(iv)'s, enforced here because this is where the
recompute happens: **a caller-set expiry is never extended.**  ``expire_after_s``
means the sender time-boxed the message, and busy credit swallowing that box was
r3's B2 bug.  The branch lives in
:func:`~cli_agent_orchestrator.core.interrupt.effective_deadline`, which is the
single authority for the arithmetic; this module only supplies the inputs and
stores the answer.
"""

from __future__ import annotations

from datetime import datetime

from cli_agent_orchestrator.adapters.store.connection import (
    SqliteConnectionSource,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.interrupt import effective_deadline
from cli_agent_orchestrator.core.timing import BUSY_CREDIT_CAP_S

__all__ = [
    "BusyLedger",
]


class BusyLedger:
    """The ``busy_since`` / ``busy_accumulated_s`` pair over ``delivery_msg``."""

    def __init__(self, pool: SqliteConnectionSource) -> None:
        self._pool = pool

    def open_episode(self, msg_id: str, *, now: datetime) -> bool:
        """Mark the start of a busy wait.  Idempotent: a second open is a no-op.

        Idempotence matters more than it looks: the tick re-offers a busy row
        every cycle, and an open that overwrote the marker would restart the
        episode's clock each time, so a row waiting ten minutes would accumulate
        one tick of credit.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE delivery_msg SET busy_since = ? WHERE msg_id = ? AND busy_since IS NULL",
                (render_timestamp(now), msg_id),
            )
            return cursor.rowcount > 0

    def close_episode(self, msg_id: str, *, now: datetime) -> float:
        """Add the open episode's elapsed interval, clear the marker, recompute.

        Returns the accumulated total AFTER the close, capped.  One transaction:
        the add, the clear and the recomputed effective deadline are one fact
        about the row, and a crash between them would leave a row whose credit
        and whose deadline disagreed.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            row = conn.execute(
                "SELECT busy_since, busy_accumulated_s, dead_by, expire_after_s "
                "FROM delivery_msg WHERE msg_id = ?",
                (msg_id,),
            ).fetchone()
            if row is None:
                return 0.0
            accumulated = float(row["busy_accumulated_s"] or 0.0)
            if row["busy_since"]:
                elapsed = (now - parse_timestamp(row["busy_since"])).total_seconds()
                accumulated += max(0.0, elapsed)
            accumulated = min(accumulated, float(BUSY_CREDIT_CAP_S))
            conn.execute(
                "UPDATE delivery_msg SET busy_since = NULL, busy_accumulated_s = ?, "
                "effective_dead_by = ? WHERE msg_id = ?",
                (
                    accumulated,
                    render_timestamp(
                        effective_deadline(
                            dead_by=parse_timestamp(row["dead_by"]),
                            caller_set=row["expire_after_s"] is not None,
                            busy_accumulated_s=accumulated,
                        )
                    ),
                    msg_id,
                ),
            )
            return accumulated

    def close_expired_episodes(self, *, now: datetime) -> tuple[str, ...]:
        """Close every episode whose lease has expired, via the ordinary path.

        This is the "no row survives with an uncleared ``busy_since`` past its
        lease" clause, and it is a SWEEP rather than a timer for the reason the
        delivery tick itself is: the driver that opened the episode is exactly the
        component whose death leaves the marker behind, so it cannot be the one
        that closes it.
        """
        conn = self._pool.connection()
        stamp = render_timestamp(now)
        rows = conn.execute(
            "SELECT msg_id FROM delivery_msg WHERE busy_since IS NOT NULL "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) ORDER BY msg_id",
            (stamp,),
        ).fetchall()
        closed = tuple(str(row["msg_id"]) for row in rows)
        for msg_id in closed:
            self.close_episode(msg_id, now=now)
        return closed

    def read(self, msg_id: str) -> tuple[datetime | None, float, datetime | None]:
        """``(busy_since, busy_accumulated_s, effective_dead_by)`` for one row."""
        row = (
            self._pool.connection()
            .execute(
                "SELECT busy_since, busy_accumulated_s, effective_dead_by, dead_by "
                "FROM delivery_msg WHERE msg_id = ?",
                (msg_id,),
            )
            .fetchone()
        )
        if row is None:
            return (None, 0.0, None)
        return (
            parse_timestamp(row["busy_since"]) if row["busy_since"] else None,
            float(row["busy_accumulated_s"] or 0.0),
            parse_timestamp(row["effective_dead_by"] or row["dead_by"]),
        )

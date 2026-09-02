"""The ``worker_state_shadow`` projection store (WP-ARCH phase 1, AC6).

Lane A's migrator owns the DDL — phase 1 has one schema owner — so this module
only reads and writes the eleven columns it defines.  Two properties of that
table shape everything here:

* **Heartbeats are COLUMNS.**  r9 retired the per-tick event rows, so a 20-second
  fleet probe costs one ``UPDATE`` per terminal rather than an event per terminal
  per tick.  :meth:`SqliteStateStore.touch_probe` and
  :meth:`~SqliteStateStore.touch_source_probe` therefore never touch ``state``,
  ``since`` or ``last_event_seq``, and a projector bug can never be introduced by
  a probe.
* **A probe may arrive before the projector has ever seen the terminal.**  Both
  touch methods upsert, seeding ``state='starting'`` and ``since`` at the probe
  time.  The alternative — dropping the probe — would lose the liveness columns
  for exactly the terminals that never produced an event, which are the ones an
  operator is most likely to be asking about.

Timestamps go through ``connection.render_timestamp``/``parse_timestamp`` so the
fixed-width UTC rendering is shared with the event log; these columns are
compared as strings, and two renderings in one database would order wrongly the
moment a microsecond came out zero.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from cli_agent_orchestrator.adapters.store.connection import (
    ConnectionPool,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.ports import StateProjection
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

__all__ = ["SqliteStateStore", "StoredProjection"]

_COLUMNS = (
    "terminal_id",
    "state",
    "since",
    "last_event_seq",
    "degraded_reason",
    "prior_state",
    "last_probe_at",
    "last_source_probe_at",
    "pane_pid",
    "pane_present",
    "miss_count",
)


@dataclass
class StoredProjection:
    """One row, satisfying the :class:`~core.ports.StateProjection` Protocol."""

    terminal_id: str
    state: WorkerState
    since: datetime
    last_event_seq: int
    degraded_reason: DegradedReason | None
    prior_state: WorkerState | None
    last_probe_at: datetime | None
    last_source_probe_at: datetime | None
    pane_pid: int | None
    pane_present: bool
    miss_count: int


def _optional_stamp(value: object) -> datetime | None:
    return parse_timestamp(value) if isinstance(value, str) else None


def _optional_state(value: object) -> WorkerState | None:
    return WorkerState(value) if isinstance(value, str) else None


def _optional_reason(value: object) -> DegradedReason | None:
    return DegradedReason(value) if isinstance(value, str) else None


def _row_to_projection(row: sqlite3.Row) -> StoredProjection:
    return StoredProjection(
        terminal_id=row["terminal_id"],
        state=WorkerState(row["state"]),
        since=parse_timestamp(row["since"]),
        last_event_seq=int(row["last_event_seq"]),
        degraded_reason=_optional_reason(row["degraded_reason"]),
        prior_state=_optional_state(row["prior_state"]),
        last_probe_at=_optional_stamp(row["last_probe_at"]),
        last_source_probe_at=_optional_stamp(row["last_source_probe_at"]),
        pane_pid=row["pane_pid"],
        pane_present=bool(row["pane_present"]),
        miss_count=int(row["miss_count"]),
    )


class SqliteStateStore:
    """``core.ports.StateStore`` over ``worker_state_shadow``."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get(self, terminal_id: str) -> StateProjection | None:
        row = (
            self._pool.connection()
            .execute(
                f"SELECT {', '.join(_COLUMNS)} FROM worker_state_shadow WHERE terminal_id = ?",
                (terminal_id,),
            )
            .fetchone()
        )
        return _row_to_projection(row) if row is not None else None

    def upsert(self, projection: StateProjection) -> None:
        """Write the whole row.

        A full replace rather than a column-by-column update, because the
        projector hands back an immutable value it has already reasoned about as
        a whole; a partial write would let the stored row hold a combination of
        fields the projector never actually decided on — a ``degraded`` state
        with a stale ``prior_state``, say.
        """
        self._pool.connection().execute(
            """
            INSERT INTO worker_state_shadow
              (terminal_id, state, since, last_event_seq, degraded_reason, prior_state,
               last_probe_at, last_source_probe_at, pane_pid, pane_present, miss_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(terminal_id) DO UPDATE SET
              state = excluded.state,
              since = excluded.since,
              last_event_seq = excluded.last_event_seq,
              degraded_reason = excluded.degraded_reason,
              prior_state = excluded.prior_state,
              last_probe_at = excluded.last_probe_at,
              last_source_probe_at = excluded.last_source_probe_at,
              pane_pid = excluded.pane_pid,
              pane_present = excluded.pane_present,
              miss_count = excluded.miss_count
            """,
            (
                projection.terminal_id,
                projection.state.value,
                render_timestamp(projection.since),
                int(projection.last_event_seq),
                (
                    projection.degraded_reason.value
                    if projection.degraded_reason is not None
                    else None
                ),
                projection.prior_state.value if projection.prior_state is not None else None,
                (
                    render_timestamp(projection.last_probe_at)
                    if projection.last_probe_at is not None
                    else None
                ),
                (
                    render_timestamp(projection.last_source_probe_at)
                    if projection.last_source_probe_at is not None
                    else None
                ),
                projection.pane_pid,
                1 if projection.pane_present else 0,
                int(projection.miss_count),
            ),
        )

    def touch_probe(
        self,
        terminal_id: str,
        *,
        probed_at: datetime,
        pane_present: bool,
        pane_pid: int | None,
        miss_count: int,
    ) -> None:
        """Update the liveness COLUMNS only — never a state change, never a row."""
        stamp = render_timestamp(probed_at)
        self._pool.connection().execute(
            """
            INSERT INTO worker_state_shadow
              (terminal_id, state, since, last_probe_at, pane_present, pane_pid, miss_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(terminal_id) DO UPDATE SET
              last_probe_at = excluded.last_probe_at,
              pane_present = excluded.pane_present,
              pane_pid = excluded.pane_pid,
              miss_count = excluded.miss_count
            """,
            (
                terminal_id,
                WorkerState.STARTING.value,
                stamp,
                stamp,
                1 if pane_present else 0,
                pane_pid,
                int(miss_count),
            ),
        )

    def touch_source_probe(self, terminal_id: str, *, probed_at: datetime) -> None:
        """Bump ``last_source_probe_at``; called by an authoritative tailer's poll.

        Every rollout poll that stats the file lands here, so at
        ``ROLLOUT_POLL_MS`` this is the most frequent write in phase 1.  It is one
        column of one row, which is why the design can afford it.
        """
        stamp = render_timestamp(probed_at)
        self._pool.connection().execute(
            """
            INSERT INTO worker_state_shadow (terminal_id, state, since, last_source_probe_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(terminal_id) DO UPDATE SET
              last_source_probe_at = excluded.last_source_probe_at
            """,
            (terminal_id, WorkerState.STARTING.value, stamp, stamp),
        )

    def all_terminals(self) -> list[StateProjection]:
        """Every projected terminal — the sweep's input."""
        rows = self._pool.connection().execute(
            f"SELECT {', '.join(_COLUMNS)} FROM worker_state_shadow ORDER BY terminal_id"
        )
        return [_row_to_projection(row) for row in rows]

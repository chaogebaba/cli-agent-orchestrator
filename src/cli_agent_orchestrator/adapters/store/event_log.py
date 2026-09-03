"""The append-only worker event log (WP-ARCH phase 1, AC3).

One invariant carries this module, and it is worth stating before the code:

    **Per-terminal sequences are contiguous.  Gaps are illegal.**

B7 makes that a contract a consumer may rely on: given ``seq``, the next event
is ``seq + 1``, and a missing row means "not yet", never "lost".  A replay
consumer, the projector, and the SSE replay-from-seq of phase 7 all lean on it.

It holds because :meth:`SqliteEventStore.append` bumps ``worker_event_seq`` and
inserts the row inside ONE ``BEGIN IMMEDIATE`` transaction.  Split those into two
transactions and the invariant dies quietly: a crash, an exception, or a killed
process between them leaves a high-water mark claiming a sequence number no row
uses.  That split is a named phase-1 mutant, and ``_after_seq_bump`` exists so a
test can inject the crash that distinguishes the two implementations — without
it, correct and broken code produce identical output on a machine that never
fails.

Retention prunes by ingestion time but never touches an event named by an OPEN
finding's ``sample_event_id``: the evidence for an unresolved problem outlives
the horizon, which is what makes a finding worth reading a month later.  WAL
checkpoint only, never ``VACUUM`` (§4c / AC3).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from cli_agent_orchestrator.adapters.store.connection import (
    ConnectionPool,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.events import (
    AnyKind,
    Confidence,
    DecisionKind,
    EventDraft,
    Producer,
    WorkerEvent,
    parse_kind,
)
from cli_agent_orchestrator.core.findings import FindingState
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.ports import CheckRunner, Clock

__all__ = ["SqliteEventStore"]

_COLUMNS = (
    "event_id, terminal_id, seq, kind, producer, confidence, observed_at, "
    "ingested_at, payload, source_ref, run_id, msg_id, decision, evidence"
)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SqliteEventStore:
    """``core.ports.EventStore`` over SQLite, WAL, single writer (U5)."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Clock | None = None,
        check_runner: CheckRunner | None = None,
    ) -> None:
        self._pool = pool
        self._clock = clock if clock is not None else _SystemClock()
        self._check_runner = check_runner
        # Test seam only.  Called between the high-water bump and the insert so
        # a test can raise there and prove both statements roll back together.
        self._after_seq_bump: Callable[[], None] | None = None

    # -- write ------------------------------------------------------------

    def append(self, draft: EventDraft) -> WorkerEvent:
        """Mint ``event_id``/``seq``/``ingested_at`` and store the row.

        The whole point of this method is the transaction boundary.  Everything
        else here is bookkeeping.
        """
        ingested_at = self._clock.now()
        event_id = new_ulid()
        conn = self._pool.connection()

        with immediate_transaction(conn):
            row = conn.execute(
                "SELECT high_water FROM worker_event_seq WHERE terminal_id = ?",
                (draft.terminal_id,),
            ).fetchone()
            seq = (row["high_water"] if row is not None else 0) + 1
            conn.execute(
                "INSERT INTO worker_event_seq (terminal_id, high_water) VALUES (?, ?) "
                "ON CONFLICT(terminal_id) DO UPDATE SET high_water = excluded.high_water",
                (draft.terminal_id, seq),
            )
            if self._after_seq_bump is not None:
                self._after_seq_bump()
            conn.execute(
                f"INSERT INTO worker_event ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    draft.terminal_id,
                    seq,
                    draft.kind.value,
                    draft.producer.value,
                    draft.confidence.value,
                    render_timestamp(draft.observed_at),
                    render_timestamp(ingested_at),
                    json.dumps(draft.payload, sort_keys=True, default=str),
                    draft.source_ref,
                    draft.run_id,
                    draft.msg_id,
                    draft.decision.value if draft.decision is not None else None,
                    draft.evidence,
                ),
            )

        event = WorkerEvent(
            **draft.model_dump(),
            event_id=event_id,
            seq=seq,
            ingested_at=ingested_at,
        )
        self._run_checks(event)
        return event

    def _run_checks(self, event: WorkerEvent) -> None:
        """Run the AC8 checks outside the transaction, swallowing failures.

        Outside, because a check that blocked the write would let a diagnostic
        take the log down.  Swallowing, for the same reason: the ``CheckRunner``
        contract says implementations must not raise, and this is the belt to
        that suspenders.
        """
        if self._check_runner is None:
            return
        try:
            self._check_runner.on_append(event)
        except Exception:  # noqa: BLE001 — a diagnostic may never break an append
            pass

    # -- read -------------------------------------------------------------

    def read(
        self,
        terminal_id: str | None = None,
        *,
        since_seq: int = 0,
        since: datetime | None = None,
        kinds: frozenset[AnyKind] | None = None,
        limit: int | None = None,
    ) -> list[WorkerEvent]:
        """Read rows oldest-first.

        Ordering depends on the scope, and deliberately so: within one terminal
        the sequence IS the order, while a fleet-wide read has no single
        sequence and orders by ``ingested_at`` — which is also the ordering the
        agreement report (AC10) classifies disagreements by.
        """
        clauses: list[str] = []
        params: list[object] = []
        if terminal_id is not None:
            clauses.append("terminal_id = ?")
            params.append(terminal_id)
        if since_seq:
            clauses.append("seq > ?")
            params.append(since_seq)
        if since is not None:
            clauses.append("ingested_at >= ?")
            params.append(render_timestamp(since))
        if kinds is not None:
            placeholders = ", ".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(sorted(kind.value for kind in kinds))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "seq" if terminal_id is not None else "ingested_at, event_id"
        sql = f"SELECT {_COLUMNS} FROM worker_event{where} ORDER BY {order}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_event(row) for row in self._pool.connection().execute(sql, params)]

    def get(self, event_id: str) -> WorkerEvent | None:
        """One row by id — the evidence-chain lookup behind ``cao diag --why``."""
        row = (
            self._pool.connection()
            .execute(f"SELECT {_COLUMNS} FROM worker_event WHERE event_id = ?", (event_id,))
            .fetchone()
        )
        return _row_to_event(row) if row is not None else None

    def high_water(self, terminal_id: str) -> int:
        """Highest ``seq`` issued for ``terminal_id``; 0 when it has no rows."""
        row = (
            self._pool.connection()
            .execute(
                "SELECT high_water FROM worker_event_seq WHERE terminal_id = ?", (terminal_id,)
            )
            .fetchone()
        )
        return int(row["high_water"]) if row is not None else 0

    # -- retention --------------------------------------------------------

    def prune(self, older_than: datetime) -> int:
        """Delete events ingested before ``older_than``, keeping open evidence.

        The high-water marks are deliberately NOT reset.  Sequences must keep
        rising across a prune, or a consumer holding an old ``seq`` would be
        handed a reused one and silently reprocess.  Contiguity is a statement
        about ISSUED sequences, not about rows that still exist.
        """
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "DELETE FROM worker_event WHERE ingested_at < ? AND event_id NOT IN ("
                "  SELECT sample_event_id FROM finding "
                "  WHERE state = ? AND sample_event_id IS NOT NULL)",
                (render_timestamp(older_than), FindingState.OPEN.value),
            )
            deleted = cursor.rowcount
        self._pool.checkpoint()
        return deleted


def _row_to_event(row: sqlite3.Row) -> WorkerEvent:
    mapping = dict(row)
    kind: AnyKind = parse_kind(mapping["kind"])
    decision = mapping["decision"]
    return WorkerEvent(
        event_id=mapping["event_id"],
        terminal_id=mapping["terminal_id"],
        seq=mapping["seq"],
        kind=kind,
        producer=Producer(mapping["producer"]),
        confidence=Confidence(mapping["confidence"]),
        observed_at=parse_timestamp(mapping["observed_at"]),
        ingested_at=parse_timestamp(mapping["ingested_at"]),
        payload=json.loads(mapping["payload"]),
        source_ref=mapping["source_ref"],
        run_id=mapping["run_id"],
        msg_id=mapping["msg_id"],
        decision=DecisionKind(decision) if decision is not None else None,
        evidence=mapping["evidence"],
    )

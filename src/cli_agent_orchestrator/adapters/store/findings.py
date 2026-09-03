"""The ``finding`` table adapter (WP-ARCH phase 1, AC8).

Deduplication is the whole feature.  A projector that meets the same impossible
transition four hundred times must produce ONE row whose ``count`` is four
hundred — four hundred rows would bury the other findings and would make
``cao diag findings`` useless exactly when it matters most.

The kept sample is the FIRST, not the latest.  When a check fires repeatedly the
earliest occurrence is the one whose surrounding timeline still explains the
cause; by the four-hundredth the system has usually moved on.  Retention honours
that by never pruning an event named by an OPEN finding.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cli_agent_orchestrator.adapters.store.connection import (
    ConnectionPool,
    immediate_transaction,
    parse_timestamp,
    render_timestamp,
)
from cli_agent_orchestrator.core.findings import Finding, FindingCode, FindingState
from cli_agent_orchestrator.core.ids import new_ulid
from cli_agent_orchestrator.core.ports import Clock

__all__ = ["SqliteFindingStore"]

_COLUMNS = (
    "finding_id, code, terminal_id, dedupe_key, detail, sample_event_id, "
    "count, first_seen_at, last_seen_at, state"
)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SqliteFindingStore:
    """``core.ports.FindingStore`` over SQLite."""

    def __init__(self, pool: ConnectionPool, *, clock: Clock | None = None) -> None:
        self._pool = pool
        self._clock = clock if clock is not None else _SystemClock()

    def record(
        self,
        code: FindingCode,
        *,
        terminal_id: str = "",
        dedupe_key: str = "",
        detail: str = "",
        sample_event_id: str | None = None,
    ) -> Finding:
        """Insert, or increment the matching OPEN finding.

        ``terminal_id`` defaults to the empty string rather than ``NULL`` for
        fleet-wide findings: SQLite treats NULLs as distinct inside a UNIQUE
        index, so a nullable column here would silently defeat the very
        deduplication this method exists for.
        """
        now = render_timestamp(self._clock.now())
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE finding SET count = count + 1, last_seen_at = ?, detail = ? "
                "WHERE code = ? AND terminal_id = ? AND dedupe_key = ? AND state = ?",
                (now, detail, code.value, terminal_id, dedupe_key, FindingState.OPEN.value),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    f"INSERT INTO finding ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_ulid(),
                        code.value,
                        terminal_id,
                        dedupe_key,
                        detail,
                        sample_event_id,
                        1,
                        now,
                        now,
                        FindingState.OPEN.value,
                    ),
                )
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM finding "
                "WHERE code = ? AND terminal_id = ? AND dedupe_key = ? AND state = ?",
                (code.value, terminal_id, dedupe_key, FindingState.OPEN.value),
            ).fetchone()
        return _row_to_finding(row)

    def list_findings(
        self, *, state: str | None = None, code: FindingCode | None = None
    ) -> list[Finding]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if code is not None:
            clauses.append("code = ?")
            params.append(code.value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._pool.connection().execute(
            f"SELECT {_COLUMNS} FROM finding{where} ORDER BY first_seen_at, finding_id", params
        )
        return [_row_to_finding(row) for row in rows]

    def resolve(self, finding_id: str) -> bool:
        """Mark one finding resolved, releasing its sample event to retention."""
        conn = self._pool.connection()
        with immediate_transaction(conn):
            cursor = conn.execute(
                "UPDATE finding SET state = ? WHERE finding_id = ? AND state = ?",
                (FindingState.RESOLVED.value, finding_id, FindingState.OPEN.value),
            )
            changed = cursor.rowcount
        return changed > 0


def _row_to_finding(row: object) -> Finding:
    mapping = dict(row)  # type: ignore[call-overload]
    return Finding(
        finding_id=mapping["finding_id"],
        code=FindingCode(mapping["code"]),
        terminal_id=mapping["terminal_id"],
        dedupe_key=mapping["dedupe_key"],
        detail=mapping["detail"],
        sample_event_id=mapping["sample_event_id"],
        count=mapping["count"],
        first_seen_at=parse_timestamp(mapping["first_seen_at"]),
        last_seen_at=parse_timestamp(mapping["last_seen_at"]),
        state=FindingState(mapping["state"]),
    )

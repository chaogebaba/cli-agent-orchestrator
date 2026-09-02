"""In-memory implementations of the phase-1 ports (WP-ARCH, F725 #581).

Lane A owns the SQLite adapters; these exist so lane C's projector, checks,
agreement report and diag views can be driven at the speed of a unit test and
with a clock the test controls.  They are held to the same contracts the real
adapters are — contiguous per-terminal sequences, first-sample-wins finding
dedup, structural ``StateProjection`` rows — because a fake that is easier to
satisfy than the real thing tests nothing.

The store calls its :class:`~core.ports.CheckRunner` AFTER the append is
committed, never inside it, which is the ordering the real store must also use:
a diagnostic check that could roll back a write would turn a diagnosability
feature into an outage.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from cli_agent_orchestrator.core.events import AnyKind, EventDraft, WorkerEvent
from cli_agent_orchestrator.core.findings import Finding, FindingCode, FindingState
from cli_agent_orchestrator.core.ids import UlidFactory
from cli_agent_orchestrator.core.ports import CheckRunner, StateProjection
from cli_agent_orchestrator.core.states import DegradedReason, WorkerState

__all__ = [
    "FakeClock",
    "InMemoryEventStore",
    "InMemoryFindingStore",
    "InMemoryStateStore",
    "Shadow",
]


class FakeClock:
    """A clock the test moves by hand.

    Every horizon in phase 1 is measured in tens of seconds (``PANE_HEARTBEAT_S``
    is 20, ``NO_SIGNAL_S`` is 60).  Sleeping through them would make the suite
    unrunnable, and — worse — flaky on a loaded box.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment


class InMemoryEventStore:
    """Append-only log with contiguous per-terminal sequences (the AC3 contract)."""

    def __init__(self, clock: FakeClock, checks: CheckRunner | None = None) -> None:
        self._clock = clock
        self._checks = checks
        self._ulid = UlidFactory()
        self._rows: list[WorkerEvent] = []
        self._high_water: dict[str, int] = {}
        self._findings: "InMemoryFindingStore | None" = None

    def set_checks(self, checks: CheckRunner) -> None:
        """Wire the runner after construction — the checks need the store too."""
        self._checks = checks

    def append(self, draft: EventDraft) -> WorkerEvent:
        seq = self._high_water.get(draft.terminal_id, 0) + 1
        self._high_water[draft.terminal_id] = seq
        stored = WorkerEvent(
            **draft.model_dump(),
            event_id=self._ulid.new(),
            seq=seq,
            ingested_at=self._clock.now(),
        )
        self._rows.append(stored)
        if self._checks is not None:
            self._checks.on_append(stored)
        return stored

    def read(
        self,
        terminal_id: str | None = None,
        *,
        since_seq: int = 0,
        since: datetime | None = None,
        kinds: frozenset[AnyKind] | None = None,
        limit: int | None = None,
    ) -> list[WorkerEvent]:
        rows = [
            row
            for row in self._rows
            if (terminal_id is None or row.terminal_id == terminal_id)
            and row.seq > since_seq
            and (since is None or row.ingested_at >= since)
            and (kinds is None or row.kind in kinds)
        ]
        rows.sort(key=lambda row: (row.terminal_id, row.seq))
        return rows[:limit] if limit is not None else rows

    def get(self, event_id: str) -> WorkerEvent | None:
        for row in self._rows:
            if row.event_id == event_id:
                return row
        return None

    def high_water(self, terminal_id: str) -> int:
        return self._high_water.get(terminal_id, 0)

    def prune(self, older_than: datetime) -> int:
        keep_ids: set[str] = set()
        if self._findings is not None:
            keep_ids = {
                finding.sample_event_id
                for finding in self._findings.list_findings(state=FindingState.OPEN.value)
                if finding.sample_event_id
            }
        before = len(self._rows)
        self._rows = [
            row for row in self._rows if row.ingested_at >= older_than or row.event_id in keep_ids
        ]
        return before - len(self._rows)

    def bind_findings(self, findings: "InMemoryFindingStore") -> None:
        """Let :meth:`prune` honour open findings, as the real store must."""
        self._findings = findings


@dataclass
class Shadow:
    """A mutable ``worker_state_shadow`` row for the fake store.

    Mirrors AC6's column list in order.  The projector hands back frozen
    ``ShadowState`` values; this is what the store keeps.
    """

    terminal_id: str
    state: WorkerState = WorkerState.STARTING
    since: datetime | None = None
    last_event_seq: int = 0
    degraded_reason: DegradedReason | None = None
    prior_state: WorkerState | None = None
    last_probe_at: datetime | None = None
    last_source_probe_at: datetime | None = None
    pane_pid: int | None = None
    pane_present: bool = False
    miss_count: int = 0


class InMemoryStateStore:
    """``worker_state_shadow`` as a dictionary."""

    def __init__(self) -> None:
        self.rows: dict[str, Shadow] = {}

    def get(self, terminal_id: str) -> StateProjection | None:
        row = self.rows.get(terminal_id)
        return replace(row) if row is not None else None

    def upsert(self, projection: StateProjection) -> None:
        self.rows[projection.terminal_id] = Shadow(
            terminal_id=projection.terminal_id,
            state=projection.state,
            since=projection.since,
            last_event_seq=projection.last_event_seq,
            degraded_reason=projection.degraded_reason,
            prior_state=projection.prior_state,
            last_probe_at=projection.last_probe_at,
            last_source_probe_at=projection.last_source_probe_at,
            pane_pid=projection.pane_pid,
            pane_present=projection.pane_present,
            miss_count=projection.miss_count,
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
        row = self.rows.setdefault(terminal_id, Shadow(terminal_id=terminal_id))
        row.last_probe_at = probed_at
        row.pane_present = pane_present
        row.pane_pid = pane_pid
        row.miss_count = miss_count

    def touch_source_probe(self, terminal_id: str, *, probed_at: datetime) -> None:
        row = self.rows.setdefault(terminal_id, Shadow(terminal_id=terminal_id))
        row.last_source_probe_at = probed_at

    def all_terminals(self) -> list[StateProjection]:
        return [replace(row) for row in self.rows.values()]


class InMemoryFindingStore:
    """Deduplicated findings, first sample kept."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._ulid = UlidFactory()
        self._rows: dict[tuple[str, str, str], Finding] = {}
        self._order: list[tuple[str, str, str]] = []

    def record(
        self,
        code: FindingCode,
        *,
        terminal_id: str = "",
        dedupe_key: str = "",
        detail: str = "",
        sample_event_id: str | None = None,
    ) -> Finding:
        key = (code.value, terminal_id, dedupe_key)
        now = self._clock.now()
        existing = self._rows.get(key)
        if existing is not None and existing.state is FindingState.OPEN:
            # ``Finding`` is a frozen pydantic model, so a repeat is a copy with
            # a bumped count.  The FIRST ``sample_event_id`` is carried over
            # untouched: the earliest occurrence is the one whose surrounding
            # timeline still explains anything.
            updated = existing.model_copy(
                update={
                    "count": existing.count + 1,
                    "last_seen_at": now,
                    "detail": detail or existing.detail,
                }
            )
            self._rows[key] = updated
            return updated
        finding = Finding(
            finding_id=self._ulid.new(),
            code=code,
            terminal_id=terminal_id,
            dedupe_key=dedupe_key,
            detail=detail,
            sample_event_id=sample_event_id,
            count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        if key not in self._rows:
            self._order.append(key)
        self._rows[key] = finding
        return finding

    def list_findings(
        self, *, state: str | None = None, code: FindingCode | None = None
    ) -> list[Finding]:
        results = []
        for key in self._order:
            finding = self._rows[key]
            if state is not None and finding.state.value != state:
                continue
            if code is not None and finding.code is not code:
                continue
            results.append(finding)
        return results

    def resolve(self, finding_id: str) -> bool:
        for key, finding in self._rows.items():
            if finding.finding_id == finding_id:
                self._rows[key] = finding.model_copy(update={"state": FindingState.RESOLVED})
                return True
        return False

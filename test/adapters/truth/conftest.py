"""In-memory doubles for the phase-1 producer tests (WP-ARCH F725 #581, lane B).

Lane A owns the real SQLite store; these fakes implement the same
``core.ports`` Protocols so the producers can be tested before it lands and,
afterwards, without a database in the loop.  The fake store enforces the two
properties the producers are allowed to depend on — contiguous per-terminal
``seq`` starting at 1, and store-minted ``event_id``/``ingested_at`` — so a
producer that tried to choose its own would fail here rather than at integration.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest

from cli_agent_orchestrator.adapters.truth import (
    codex_rollout,
    legacy_egress,
    wiring,
)
from cli_agent_orchestrator.core.events import AnyKind, EventDraft, WorkerEvent
from cli_agent_orchestrator.core.findings import Finding, FindingCode, FindingState
from cli_agent_orchestrator.core.ids import UlidFactory


class FakeClock:
    """A clock the test drives.  Advances by a fixed step on every read.

    Advancing rather than freezing is deliberate: several assertions compare
    ``observed_at`` ordering between rows, and a frozen clock would let a
    producer that emitted rows in the wrong order still pass.
    """

    def __init__(self, start: datetime | None = None, step_s: float = 1.0) -> None:
        self._now = start or datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_s)

    def now(self) -> datetime:
        value = self._now
        self._now += self._step
        return value

    def peek(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeEventStore:
    """An in-memory ``EventStore`` with the store's own invariants enforced."""

    def __init__(self) -> None:
        self.rows: list[WorkerEvent] = []
        self._high_water: dict[str, int] = {}
        self._ulids = UlidFactory()
        self.fail_next = False

    def append(self, draft: EventDraft) -> WorkerEvent:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated store failure")
        seq = self._high_water.get(draft.terminal_id, 0) + 1
        self._high_water[draft.terminal_id] = seq
        event = WorkerEvent(
            **draft.model_dump(),
            event_id=self._ulids.new(),
            seq=seq,
            ingested_at=datetime.now(timezone.utc),
        )
        self.rows.append(event)
        return event

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
            for row in self.rows
            if (terminal_id is None or row.terminal_id == terminal_id)
            and row.seq > since_seq
            and (since is None or row.ingested_at >= since)
            and (kinds is None or row.kind in kinds)
        ]
        rows.sort(key=lambda row: (row.terminal_id, row.seq))
        return rows[:limit] if limit is not None else rows

    def get(self, event_id: str) -> WorkerEvent | None:
        return next((row for row in self.rows if row.event_id == event_id), None)

    def high_water(self, terminal_id: str) -> int:
        return self._high_water.get(terminal_id, 0)

    def prune(self, older_than: datetime) -> int:
        keep = [row for row in self.rows if row.ingested_at >= older_than]
        removed = len(self.rows) - len(keep)
        self.rows = keep
        return removed

    # -- assertion helpers used across the lane-B tests ----------------------

    def kinds(self, terminal_id: str | None = None) -> list[str]:
        return [row.kind.value for row in self.read(terminal_id)]

    def of_kind(self, kind: AnyKind, terminal_id: str | None = None) -> list[WorkerEvent]:
        return [row for row in self.read(terminal_id) if row.kind is kind]


class FakeStateStore:
    """Records column touches so a test can assert heartbeats never became rows."""

    def __init__(self) -> None:
        self.probe_touches: list[dict[str, Any]] = []
        self.source_touches: list[dict[str, Any]] = []

    def get(self, terminal_id: str) -> Any:
        return None

    def upsert(self, projection: Any) -> None:  # pragma: no cover - lane C's writer
        raise NotImplementedError

    def touch_probe(
        self,
        terminal_id: str,
        *,
        probed_at: datetime,
        pane_present: bool,
        pane_pid: int | None,
        miss_count: int,
    ) -> None:
        self.probe_touches.append(
            {
                "terminal_id": terminal_id,
                "probed_at": probed_at,
                "pane_present": pane_present,
                "pane_pid": pane_pid,
                "miss_count": miss_count,
            }
        )

    def touch_source_probe(self, terminal_id: str, *, probed_at: datetime) -> None:
        self.source_touches.append({"terminal_id": terminal_id, "probed_at": probed_at})

    def all_terminals(self) -> list[Any]:
        return []


class FakeFindingStore:
    def __init__(self) -> None:
        self.recorded: list[Finding] = []
        self._ulids = UlidFactory()

    def record(
        self,
        code: FindingCode,
        *,
        terminal_id: str = "",
        dedupe_key: str = "",
        detail: str = "",
        sample_event_id: str | None = None,
    ) -> Finding:
        now = datetime.now(timezone.utc)
        finding = Finding(
            finding_id=self._ulids.new(),
            code=code,
            terminal_id=terminal_id,
            dedupe_key=dedupe_key,
            detail=detail,
            sample_event_id=sample_event_id,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.recorded.append(finding)
        return finding

    def list_findings(
        self, *, state: str | None = None, code: FindingCode | None = None
    ) -> list[Finding]:
        return [
            f
            for f in self.recorded
            if (state is None or f.state == FindingState(state))
            and (code is None or f.code is code)
        ]

    def resolve(self, finding_id: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_producer_state() -> Iterator[None]:
    """Every test starts with ingestion OFF and no producer memory.

    Autouse and both-ended: the producers hold process-global edge state and
    file cursors by design (they are one-per-process singletons in the server),
    so a leak between tests would make an ON/OFF assertion pass for the previous
    test's reason.
    """
    wiring.reset()
    legacy_egress.reset_edges()
    codex_rollout.reset_sources()
    yield
    wiring.reset()
    legacy_egress.reset_edges()
    codex_rollout.reset_sources()


@pytest.fixture
def store() -> FakeEventStore:
    return FakeEventStore()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def state_store() -> FakeStateStore:
    return FakeStateStore()


@pytest.fixture
def ingest_on(
    store: FakeEventStore, clock: FakeClock, state_store: FakeStateStore
) -> FakeEventStore:
    """Arm ingestion the way ``bootstrap.py`` does when the switch is set."""
    wiring.install(wiring.TruthRuntime(store=store, clock=clock, state_store=state_store))
    return store

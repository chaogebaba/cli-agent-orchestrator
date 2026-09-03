"""Shared fixtures for the WP-ARCH phase-1 store tests (F725 #581)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core.events import Confidence, EventDraft, EventKind, Producer


class FakeClock:
    """A clock the test drives, so retention horizons need no sleeping."""

    def __init__(self, start: datetime | None = None) -> None:
        self.value = start if start is not None else datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


# Explicit, because the store requires it: there is no default busy timeout in
# adapters/store (§4c — the value belongs to the fork's constants, handed in by
# bootstrap.py).
TEST_BUSY_TIMEOUT_MS = 5000


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "worker-truth.db"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pool(db_path: Path) -> Iterator[ConnectionPool]:
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok, result
    assert pool is not None
    yield pool
    pool.close_all()


@pytest.fixture
def store(pool: ConnectionPool, clock: FakeClock) -> SqliteEventStore:
    return SqliteEventStore(pool, clock=clock)


@pytest.fixture
def findings(pool: ConnectionPool, clock: FakeClock) -> SqliteFindingStore:
    return SqliteFindingStore(pool, clock=clock)


def draft(terminal_id: str = "t-1", **overrides: object) -> EventDraft:
    """A minimal valid worker-truth draft."""
    fields: dict[str, object] = {
        "terminal_id": terminal_id,
        "kind": EventKind.TURN_STARTED,
        "producer": Producer.JSONL,
        "confidence": Confidence.AUTHORITATIVE,
        "observed_at": datetime(2026, 9, 2, 11, 59, tzinfo=UTC),
    }
    fields.update(overrides)
    return EventDraft(**fields)  # type: ignore[arg-type]

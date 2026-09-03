"""AC3/AC8 — findings, deduplication, and the retention sweep (WP-ARCH, F725 #581)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.retention import RetentionTask
from cli_agent_orchestrator.core.findings import FindingCode, FindingState
from cli_agent_orchestrator.core.timing import RETENTION_DAYS, RETENTION_SWEEP_S

from .conftest import FakeClock, draft


def test_repeat_increments_count_and_keeps_the_first_sample(
    findings: SqliteFindingStore, clock: FakeClock
) -> None:
    """400 identical breaches are one row with count 400 — not 400 rows."""
    first = findings.record(
        FindingCode.DIAG_BAD_TRANSITION,
        terminal_id="t-1",
        dedupe_key="capped->busy",
        detail="first",
        sample_event_id="01J000000000000000000FIRST",
    )
    clock.advance(minutes=5)
    for _ in range(9):
        findings.record(
            FindingCode.DIAG_BAD_TRANSITION,
            terminal_id="t-1",
            dedupe_key="capped->busy",
            detail="later",
            sample_event_id="01J0000000000000000000LATE",
        )

    rows = findings.list_findings()
    assert len(rows) == 1
    assert rows[0].count == 10
    assert rows[0].finding_id == first.finding_id
    # The FIRST sample survives: it is the one whose timeline still explains
    # anything.
    assert rows[0].sample_event_id == "01J000000000000000000FIRST"
    assert rows[0].first_seen_at < rows[0].last_seen_at


def test_dedupe_key_separates_distinct_breaches(findings: SqliteFindingStore) -> None:
    findings.record(FindingCode.DIAG_BAD_TRANSITION, terminal_id="t-1", dedupe_key="capped->busy")
    findings.record(FindingCode.DIAG_BAD_TRANSITION, terminal_id="t-1", dedupe_key="exited->idle")
    findings.record(FindingCode.DIAG_BAD_TRANSITION, terminal_id="t-2", dedupe_key="capped->busy")
    assert len(findings.list_findings()) == 3


def test_fleet_wide_findings_dedupe_on_the_empty_terminal_id(
    findings: SqliteFindingStore,
) -> None:
    """The empty string, never NULL — SQLite treats NULLs as distinct in a UNIQUE index."""
    findings.record(FindingCode.DIAG_MIGRATION_FAILED, dedupe_key="worker_event")
    findings.record(FindingCode.DIAG_MIGRATION_FAILED, dedupe_key="worker_event")
    rows = findings.list_findings()
    assert len(rows) == 1
    assert rows[0].count == 2
    assert rows[0].terminal_id == ""


def test_resolve_then_record_opens_a_new_finding(findings: SqliteFindingStore) -> None:
    """A resolved finding must not absorb a fresh recurrence."""
    first = findings.record(FindingCode.DIAG_GHOST_TRANSITION, terminal_id="t-1", dedupe_key="k")
    assert findings.resolve(first.finding_id) is True
    assert findings.resolve(first.finding_id) is False

    second = findings.record(FindingCode.DIAG_GHOST_TRANSITION, terminal_id="t-1", dedupe_key="k")
    assert second.finding_id != first.finding_id
    assert len(findings.list_findings()) == 2
    assert len(findings.list_findings(state=FindingState.OPEN.value)) == 1


def test_list_filters_by_code(findings: SqliteFindingStore) -> None:
    findings.record(FindingCode.DIAG_BAD_TRANSITION, dedupe_key="a")
    findings.record(FindingCode.DIAG_LEGACY_DISAGREE, dedupe_key="b")
    assert len(findings.list_findings(code=FindingCode.DIAG_BAD_TRANSITION)) == 1


@pytest.mark.integration
def test_prune_deletes_old_events_but_keeps_finding_referenced_ones(
    store: SqliteEventStore, findings: SqliteFindingStore, clock: FakeClock
) -> None:
    """The AC3 retention rule, and the reason a finding is worth reading a month later."""
    old_kept = store.append(draft("t-1"))
    old_dropped = store.append(draft("t-1"))
    findings.record(
        FindingCode.DIAG_BAD_TRANSITION,
        terminal_id="t-1",
        dedupe_key="capped->busy",
        sample_event_id=old_kept.event_id,
    )

    clock.advance(days=RETENTION_DAYS + 1)
    recent = store.append(draft("t-1"))

    deleted = store.prune(clock.now() - timedelta(days=RETENTION_DAYS))
    assert deleted == 1
    assert store.get(old_dropped.event_id) is None
    assert store.get(old_kept.event_id) is not None
    assert store.get(recent.event_id) is not None


@pytest.mark.integration
def test_prune_releases_the_sample_once_the_finding_is_resolved(
    store: SqliteEventStore, findings: SqliteFindingStore, clock: FakeClock
) -> None:
    sample = store.append(draft("t-1"))
    finding = findings.record(
        FindingCode.DIAG_BAD_TRANSITION,
        terminal_id="t-1",
        dedupe_key="capped->busy",
        sample_event_id=sample.event_id,
    )
    clock.advance(days=RETENTION_DAYS + 1)
    horizon = clock.now() - timedelta(days=RETENTION_DAYS)

    assert store.prune(horizon) == 0
    findings.resolve(finding.finding_id)
    assert store.prune(horizon) == 1


@pytest.mark.integration
def test_prune_does_not_reset_the_high_water_mark(
    store: SqliteEventStore, clock: FakeClock
) -> None:
    """Contiguity is about ISSUED sequences.

    Reusing a sequence after a prune would hand a replay consumer an old number
    and make it silently reprocess.
    """
    for _ in range(3):
        store.append(draft("t-1"))
    clock.advance(days=RETENTION_DAYS + 1)
    store.prune(clock.now() - timedelta(days=RETENTION_DAYS))

    assert store.read("t-1") == []
    assert store.high_water("t-1") == 3
    assert store.append(draft("t-1")).seq == 4


def test_retention_task_sweep_once_uses_the_configured_horizon(
    store: SqliteEventStore, clock: FakeClock
) -> None:
    store.append(draft("t-1"))
    task = RetentionTask(store, clock)
    assert task.sweep_once() == 0
    clock.advance(days=RETENTION_DAYS + 1)
    assert task.sweep_once() == 1


@pytest.mark.asyncio
async def test_retention_task_start_and_stop(store: SqliteEventStore, clock: FakeClock) -> None:
    """The task sleeps FIRST, so a restart loop never becomes a delete loop."""
    store.append(draft("t-1"))
    clock.advance(days=RETENTION_DAYS + 1)

    task = RetentionTask(store, clock)
    await task.start()
    assert task.running
    await asyncio.sleep(0)
    # Still present: the loop sleeps for a day before its first sweep.
    assert len(store.read("t-1")) == 1
    await task.stop()
    assert not task.running


@pytest.mark.asyncio
async def test_retention_task_start_is_idempotent(
    store: SqliteEventStore, clock: FakeClock
) -> None:
    task = RetentionTask(store, clock)
    await task.start()
    first = task._task
    await task.start()
    assert task._task is first
    await task.stop()


def test_sweep_period_comes_from_core_timing() -> None:
    """§4c: the number lives in ``core/timing.py``, not in the task."""
    assert RETENTION_SWEEP_S == 24 * 60 * 60

"""AC5 — the ingestion switch and the composition root (WP-ARCH, F725 #581).

The switch is what makes AC11's "no behaviour change" enforced rather than
asserted, so these tests are about what does NOT happen when it is off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.app.worker_truth.checks import CheckRegistry

from .conftest import FakeClock

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_runtime() -> AsyncIterator[None]:
    """Leave no runtime behind: the composition root keeps a module-level one."""
    yield
    await bootstrap.shutdown_worker_truth()


async def test_switch_defaults_off() -> None:
    """Default OFF, and strictly ``"1"`` — a switch nobody can misread."""
    assert bootstrap.ingest_enabled({}) is False
    assert bootstrap.ingest_enabled({"CAO_WORKER_TRUTH_INGEST": "0"}) is False
    assert bootstrap.ingest_enabled({"CAO_WORKER_TRUTH_INGEST": "true"}) is False
    assert bootstrap.ingest_enabled({"CAO_WORKER_TRUTH_INGEST": "yes"}) is False
    assert bootstrap.ingest_enabled({"CAO_WORKER_TRUTH_INGEST": "1"}) is True


async def test_switch_off_migrates_but_starts_nothing(db_path: Path, clock: FakeClock) -> None:
    """The tables exist and are inert: no store, no checks, no retention task.

    Nothing can contend for the single writer because nothing was built.
    """
    runtime = await bootstrap.start_worker_truth(db_path=db_path, clock=clock, env={})

    assert runtime.ingest_enabled is False
    assert runtime.migration.ok is True
    assert runtime.event_store is None
    assert runtime.finding_store is None
    assert runtime.checks is None
    assert runtime.retention is None

    tables = {
        row["name"]
        for row in runtime.pool.connection().execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"finding", "worker_event", "worker_event_seq", "worker_state_shadow"} <= tables


async def test_switch_on_wires_the_adapters_and_starts_retention(
    db_path: Path, clock: FakeClock
) -> None:
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )

    assert runtime.ingest_enabled is True
    assert isinstance(runtime.event_store, SqliteEventStore)
    assert isinstance(runtime.finding_store, SqliteFindingStore)
    assert isinstance(runtime.checks, CheckRegistry)
    assert runtime.retention is not None
    assert runtime.retention.running is True

    await bootstrap.shutdown_worker_truth()
    assert runtime.retention.running is False


async def test_migration_failure_disables_ingestion_but_still_returns(
    db_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5/N6: the server boots, the failure is recorded, ingestion stays off."""
    from cli_agent_orchestrator.adapters.store import migrator as migrator_module

    monkeypatch.setattr(
        migrator_module,
        "MIGRATION_STEPS",
        (("worker_event", ("CREATE TABLE not valid sql (",)),),
    )
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )

    assert runtime.ingest_enabled is False
    assert runtime.migration.ok is False
    assert runtime.retention is None
    rows = list(runtime.pool.connection().execute("SELECT code FROM finding"))  # type: ignore[union-attr]
    assert [row["code"] for row in rows] == ["DIAG-MIGRATION-FAILED"]


async def test_unresolvable_database_path_does_not_raise(tmp_path: Path, clock: FakeClock) -> None:
    """A database that cannot be opened leaves the server bootable."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    runtime = await bootstrap.start_worker_truth(
        db_path=blocker / "nested" / "db.sqlite",
        clock=clock,
        env={"CAO_WORKER_TRUTH_INGEST": "1"},
    )
    assert runtime.ingest_enabled is False
    assert runtime.migration.ok is False


async def test_current_runtime_tracks_the_last_bootstrap(db_path: Path, clock: FakeClock) -> None:
    assert bootstrap.current_runtime() is None
    runtime = await bootstrap.start_worker_truth(db_path=db_path, clock=clock, env={})
    assert bootstrap.current_runtime() is runtime
    await bootstrap.shutdown_worker_truth()
    assert bootstrap.current_runtime() is None


async def test_shutdown_is_safe_without_a_runtime() -> None:
    await bootstrap.shutdown_worker_truth()
    await bootstrap.shutdown_worker_truth()


async def test_check_registry_runs_checks_and_records_findings(
    db_path: Path, clock: FakeClock
) -> None:
    """AC8 skeleton: a registered check produces a deduplicated finding on append."""
    from cli_agent_orchestrator.app.worker_truth.checks import CheckOutcome
    from cli_agent_orchestrator.core.findings import FindingCode

    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )
    assert runtime.checks is not None
    assert runtime.event_store is not None
    assert runtime.finding_store is not None

    # The composition root now registers the two structural phase-1 checks
    # (lane C, AC8), so the registry is no longer empty at boot.  Asserting the
    # exact tuple would only assert that nobody has added a check yet, which is
    # the opposite of what this test wants to know; assert instead that
    # registration APPENDS to what bootstrap wired.
    assert FindingCode.DIAG_GHOST_TRANSITION in runtime.checks.registered_codes
    assert FindingCode.DIAG_BAD_TRANSITION in runtime.checks.registered_codes
    before = len(runtime.checks.registered_codes)

    runtime.checks.register(
        FindingCode.DIAG_GHOST_TRANSITION,
        lambda event: CheckOutcome(dedupe_key="no-evidence", detail=event.kind.value),
    )
    assert len(runtime.checks.registered_codes) == before + 1

    from .conftest import draft

    runtime.event_store.append(draft("t-1"))
    runtime.event_store.append(draft("t-1"))

    rows = runtime.finding_store.list_findings()
    assert len(rows) == 1
    assert rows[0].count == 2
    await bootstrap.shutdown_worker_truth()


async def test_a_raising_check_never_breaks_an_append(db_path: Path, clock: FakeClock) -> None:
    """A diagnostic that could take the event log down would invert its own purpose."""
    from cli_agent_orchestrator.core.findings import FindingCode

    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_INGEST": "1"}
    )
    assert runtime.checks is not None
    assert runtime.event_store is not None

    def explode(_event: object) -> None:
        raise RuntimeError("check is broken")

    runtime.checks.register(FindingCode.DIAG_BAD_TRANSITION, explode)

    from .conftest import draft

    event = runtime.event_store.append(draft("t-1"))
    assert event.seq == 1
    await bootstrap.shutdown_worker_truth()

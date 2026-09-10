"""§12's startup check — ``DIAG-STATUS-GUARD`` fires in each demoting cell.

The blueprint's coverage note asks for this shape by name: *"``DIAG-STATUS-GUARD``
→ a NEW STARTUP CHECK, since D9's resolution table raises it in several cells and
no case drove any — the builder boots each demoting cell once and asserts the
finding, cheaper as a startup test than as a session case."*

``test/core/test_status_cutover.py`` asserts the table as a pure function.  This
file asserts that the composition root actually consults it, records the notice,
and arms nothing sub-phase 2a has not built.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.core.findings import Finding, FindingCode
from cli_agent_orchestrator.core.status_cutover import StatusPosition

from .conftest import FakeClock

pytestmark = pytest.mark.asyncio

_ON = {"CAO_WORKER_TRUTH_INGEST": "1"}


@pytest_asyncio.fixture(autouse=True)
async def _clean_runtime() -> AsyncIterator[None]:
    yield
    await bootstrap.shutdown_worker_truth()


def _guard_findings(runtime: bootstrap.WorkerTruthRuntime, clock: FakeClock) -> list[Finding]:
    assert runtime.pool is not None
    findings: list[Finding] = SqliteFindingStore(runtime.pool, clock=clock).list_findings(
        code=FindingCode.DIAG_STATUS_GUARD
    )
    return findings


async def test_the_switch_defaults_off_and_records_nothing(db_path: Path, clock: FakeClock) -> None:
    """An operator who set nothing has made no mistake, so there is no notice.

    A guard that filed a finding on every default boot would make the three that
    matter unreadable.
    """
    runtime = await bootstrap.start_worker_truth(db_path=db_path, clock=clock, env=dict(_ON))

    assert runtime.status is not None
    assert runtime.status.position is StatusPosition.OFF
    assert _guard_findings(runtime, clock) == []


@pytest.mark.parametrize("requested", ["on"])
async def test_the_cutover_without_ingestion_demotes_and_files_the_notice(
    db_path: Path, clock: FakeClock, requested: str
) -> None:
    """Cell 2, and the reason the guard resolves BEFORE the ingestion-off
    early return: this is the one path on which an operator would otherwise learn
    nothing at all.
    """
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_STATUS": requested}
    )

    assert runtime.status is not None
    assert runtime.status.position is StatusPosition.OFF
    assert runtime.status.demoted
    findings = _guard_findings(runtime, clock)
    assert len(findings) == 1
    assert findings[0].dedupe_key == f"{requested}->off"


async def test_on_with_an_empty_allowlist_demotes_to_off_and_files_the_notice(
    db_path: Path, clock: FakeClock
) -> None:
    """Cell 3.  ``on`` with nothing to publish for is indistinguishable from a
    misconfiguration, and the finding is the notice.

    It lands on ``off``: with ``shadow`` retired (#738) there is no dark position
    left to hold a refused cutover at."""
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={**_ON, "CAO_WORKER_TRUTH_STATUS": "on"}
    )

    assert runtime.status is not None
    assert runtime.status.position is StatusPosition.OFF
    findings = _guard_findings(runtime, clock)
    assert len(findings) == 1
    assert findings[0].dedupe_key == "on->off"


async def test_a_retired_position_is_refused_and_arms_nothing(
    db_path: Path, clock: FakeClock
) -> None:
    """#738, at the composition root: ``shadow`` is not a cutover position.

    The position that sub-phase 2a was built around is gone, so the boot resolves
    to ``off``, arms nothing, and files NO guard finding — a refusal is not a
    demotion, and reporting it as one would tell an operator the guard resolved
    something it declined to resolve.  The ERROR line carries the fix.

    MUTANT: accept ``shadow`` again and ``runtime.status.position`` is
    ``StatusPosition.SHADOW``, which this fails on.
    """
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, clock=clock, env={**_ON, "CAO_WORKER_TRUTH_STATUS": "shadow"}
    )

    assert runtime.status is not None
    assert runtime.status.position is StatusPosition.OFF
    assert runtime.status.requested is StatusPosition.OFF
    assert not runtime.status.demoted
    assert _guard_findings(runtime, clock) == []
    assert {position.value for position in StatusPosition} == {"off", "on"}


async def test_a_repeated_bad_boot_leaves_one_row_with_a_count(
    db_path: Path, clock: FakeClock
) -> None:
    """A server rebooting ten times into the same misconfiguration must leave one
    row whose count is ten, not ten rows — the dedupe rule the finding model exists
    for, applied to the one code that fires at boot rather than in a session."""
    for _ in range(3):
        runtime = await bootstrap.start_worker_truth(
            db_path=db_path, clock=clock, env={"CAO_WORKER_TRUTH_STATUS": "on"}
        )
        await bootstrap.shutdown_worker_truth()

    result, pool = migrate(db_path, busy_timeout_ms=2000)
    assert result.ok and pool is not None
    findings = SqliteFindingStore(pool, clock=clock).list_findings(
        code=FindingCode.DIAG_STATUS_GUARD
    )
    assert len(findings) == 1
    assert findings[0].count == 3
    pool.close_all()


async def test_the_guard_never_refuses_the_boot(db_path: Path, clock: FakeClock) -> None:
    """This ships into the server running the strangler work.

    A self-inflicted boot failure would be worse than the condition it reports,
    and an operator whose only mistake was a mistyped variable must not lose the
    server over it.
    """
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path,
        clock=clock,
        env={**_ON, "CAO_WORKER_TRUTH_STATUS": "definitely-not-a-position"},
    )

    assert runtime.ingest_enabled is True
    assert runtime.status is not None
    assert runtime.status.position is StatusPosition.OFF

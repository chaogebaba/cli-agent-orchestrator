"""The delivery switch at boot (WP-ARCH phase 3a, F728 #584).

Against a real database and the real composition root, because the two things
being tested are properties of the WIRING: that the off arm writes nothing, and
that the guard resolves against the queue as it actually stands.  Both are the
kind of claim a double would happily confirm while the shipped bootstrap did
something else.

AC-3a's two switch criteria live here:

* **the off arm** — with the switch unset the count of ``delivery_msg`` rows is
  nil, and rows there are a failure rather than a curiosity;
* **the bounce** — a restart while the shadow deployment holds in-flight
  non-terminal rows resolves back to ``shadow``, writes no
  ``DIAG-QUEUE-ORPHAN-GUARD``, and attributes no injection to the queue.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.core.delivery import (
    EnqueueDraft,
    MsgState,
    QueueMode,
    SwitchPosition,
)
from cli_agent_orchestrator.core.findings import FindingCode

pytestmark = pytest.mark.asyncio

AT = "2026-09-03T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _disarmed() -> Iterator[None]:
    wiring.reset_delivery()
    yield
    wiring.reset_delivery()


def fact(legacy_id: int, receiver: str = "mb_supervisor") -> LegacyEnqueue:
    from datetime import UTC, datetime

    return LegacyEnqueue(
        legacy_message_id=legacy_id,
        receiver_id=receiver,
        sender_id="t-worker",
        message="m",
        status="pending",
        created_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        orchestration_type="send_message",
    )


async def boot(db_path: Path, position: str | None, clock: FakeClock) -> object:
    env = {} if position is None else {bootstrap.DELIVERY_ENV_VAR: position}
    return await bootstrap.start_worker_truth(
        db_path=db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS, clock=clock, env=env
    )


# ------------------------------------------------------------------ the off arm


async def test_with_the_switch_unset_the_hooks_write_nothing(
    db_path: Path, clock: FakeClock
) -> None:
    """AC-3a's off-arm criterion, through the real bootstrap.

    Not "few rows" and not "no visible change" — NIL rows.  Anything else in
    this arm means the switch is not the only thing standing between a hook and
    the queue.
    """
    runtime = await boot(db_path, None, clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.OFF
    assert wiring.queue_enabled() is False

    for legacy_id in range(1, 6):
        wiring.record_enqueue(fact(legacy_id))

    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 0

    await bootstrap.shutdown_worker_truth()


async def test_shadow_arms_the_hooks_and_rows_are_written_shadow(
    db_path: Path, clock: FakeClock
) -> None:
    """The other arm, so the off-arm assertion is a DIFFERENCE and not a tautology."""
    runtime = await boot(db_path, "shadow", clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.SHADOW
    assert wiring.queue_enabled() is True

    for legacy_id in range(1, 6):
        wiring.record_enqueue(fact(legacy_id))

    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 5
    assert runtime.queue_store.count(mode=QueueMode.LIVE) == 0

    await bootstrap.shutdown_worker_truth()


async def test_shutdown_disarms_the_hooks(db_path: Path, clock: FakeClock) -> None:
    """A hook firing while the pool closes would log a failure for a clean stop."""
    await boot(db_path, "shadow", clock)
    await bootstrap.shutdown_worker_truth()
    assert wiring.queue_enabled() is False


# --------------------------------------------------------------- the boot guard


async def test_a_leftover_live_queue_demotes_an_unset_switch_to_drain(
    db_path: Path, clock: FakeClock
) -> None:
    """D9's guard overriding the DEFAULT, which is the surprising cell.

    A boot with the variable unset over a leftover queue runs the delivery
    machinery in ``drain`` in a deployment that never opted in, and the finding
    is how an operator learns.  Silently orphaning those rows instead would be
    the failure class the phase exists to remove, arriving through the control
    the phase offers for backing out.
    """
    runtime = await boot(db_path, "shadow", clock)
    assert runtime.pool is not None
    SqliteQueueStore(runtime.pool, clock=clock).enqueue(
        EnqueueDraft(idempotency_key="live-1", receiver_id="mb_x", mode=QueueMode.LIVE)
    )
    await bootstrap.shutdown_worker_truth()

    runtime = await boot(db_path, None, clock)
    assert runtime.delivery is not None
    assert runtime.delivery.requested is SwitchPosition.OFF
    assert runtime.delivery.position is SwitchPosition.DRAIN
    assert runtime.delivery.finding is FindingCode.DIAG_QUEUE_ORPHAN_GUARD

    from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore

    assert runtime.pool is not None
    recorded = SqliteFindingStore(runtime.pool, clock=clock).list_findings(state="open")
    assert any(f.code is FindingCode.DIAG_QUEUE_ORPHAN_GUARD for f in recorded)

    await bootstrap.shutdown_worker_truth()


async def test_the_bounce_case_a_shadow_deployment_restarts_as_shadow(
    db_path: Path, clock: FakeClock
) -> None:
    """AC-3a's second case, and the defect an earlier draft would have shipped.

    A bounced sub-phase 3a deployment holds in-flight SHADOW rows.  Had the
    occupancy predicate counted them, this boot would resolve to ``drain``, whose
    tick would then inject copies of messages the legacy path already delivered —
    a second carrier over one id, which is #506 reproduced by the guard added to
    prevent loss, in the first sub-phase to ship.

    A run that resolves to ``drain``, or writes the finding, fails the case.
    """
    runtime = await boot(db_path, "shadow", clock)
    for legacy_id in range(1, 8):
        wiring.record_enqueue(fact(legacy_id))
    assert runtime.queue_store is not None
    outstanding = [row for row in _all_rows(runtime) if row.state is MsgState.READY]
    assert len(outstanding) == 7, "the bounce case needs in-flight rows to be a case"
    await bootstrap.shutdown_worker_truth()

    rebooted = await boot(db_path, "shadow", clock)
    assert rebooted.delivery is not None
    assert rebooted.delivery.position is SwitchPosition.SHADOW
    assert rebooted.delivery.finding is None
    assert rebooted.delivery.demoted is False

    assert rebooted.pool is not None
    from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore

    findings = SqliteFindingStore(rebooted.pool, clock=clock).list_findings(state="open")
    assert not [f for f in findings if f.code is FindingCode.DIAG_QUEUE_ORPHAN_GUARD]

    # And nothing was injected: no attempt row was created by the restart. In
    # shadow mode the only writer of attempt rows is the mirror observing legacy,
    # so a queue-attributable attempt here would mean the tick had run.
    assert rebooted.queue_store is not None
    for row in _all_rows(rebooted):
        assert rebooted.queue_store.attempts_for(row.msg_id) == []

    await bootstrap.shutdown_worker_truth()


async def test_a_drained_queue_promotes_drain_back_to_shadow(
    db_path: Path, clock: FakeClock
) -> None:
    """The one cell that promotes, carrying an operator's intent forward.

    Holding a drained deployment in ``drain`` would leave the delivery machinery
    running with nothing to deliver.
    """
    runtime = await boot(db_path, "drain", clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.SHADOW
    assert runtime.delivery.finding is FindingCode.DIAG_QUEUE_ORPHAN_GUARD
    assert "drain complete" in runtime.delivery.detail
    await bootstrap.shutdown_worker_truth()


@pytest.mark.parametrize("position", ["drain", "on"])
async def test_a_position_this_sub_phase_does_not_implement_arms_nothing(
    db_path: Path, clock: FakeClock, position: str
) -> None:
    """Not reinterpreted as ``shadow`` — refused, loudly.

    An operator who asked for write-through and silently got a shadow queue would
    believe the seat was being served from the queue when it was not, which is a
    worse outcome than a position that plainly does nothing yet.

    ``drain`` reaches this test with a non-empty live queue, since over an empty
    one the guard promotes it to ``shadow`` (asserted above).
    """
    runtime = await boot(db_path, "shadow", clock)
    assert runtime.pool is not None
    SqliteQueueStore(runtime.pool, clock=clock).enqueue(
        EnqueueDraft(idempotency_key="live-1", receiver_id="mb_x", mode=QueueMode.LIVE)
    )
    await bootstrap.shutdown_worker_truth()

    rebooted = await boot(db_path, position, clock)
    assert rebooted.delivery is not None
    assert rebooted.delivery.position.value == position
    assert wiring.queue_enabled() is False

    wiring.record_enqueue(fact(99))
    assert rebooted.queue_store is not None
    assert rebooted.queue_store.count(mode=QueueMode.SHADOW) == 0

    await bootstrap.shutdown_worker_truth()


async def test_the_delivery_switch_is_independent_of_the_ingestion_switch(
    db_path: Path, clock: FakeClock
) -> None:
    """Two strangler phases, two switches.

    Coupling them would make a phase-3 rollback need a phase-1 decision, and the
    composition root's own docstring already warns against a second spelling of
    one switch — which is precisely why this is a different variable rather than
    a second meaning of ``CAO_WORKER_TRUTH_INGEST``.
    """
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path,
        busy_timeout_ms=TEST_BUSY_TIMEOUT_MS,
        clock=clock,
        env={bootstrap.DELIVERY_ENV_VAR: "shadow"},
    )
    assert runtime.ingest_enabled is False
    assert runtime.delivery is not None and runtime.delivery.position is SwitchPosition.SHADOW
    assert wiring.queue_enabled() is True
    await bootstrap.shutdown_worker_truth()


def _all_rows(runtime: object) -> list:
    pool = runtime.pool  # type: ignore[attr-defined]
    store = SqliteQueueStore(pool)
    rows = []
    for row in pool.connection().execute("SELECT msg_id FROM delivery_msg"):
        message = store.get(row["msg_id"])
        if message is not None:
            rows.append(message)
    return rows

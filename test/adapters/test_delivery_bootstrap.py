"""The delivery switch at boot (WP-ARCH phase 3, F728 #584; #738).

Against a real database and the real composition root, because the two things
being tested are properties of the WIRING: that the off arm writes nothing, and
that the guard resolves against the queue as it actually stands.  Both are the
kind of claim a double would happily confirm while the shipped bootstrap did
something else.

Three switch criteria live here:

* **the off arm** — with the switch unset the count of ``delivery_msg`` rows is
  nil, and rows there are a failure rather than a curiosity;
* **the guard** — a leftover live queue demotes an unset switch to ``drain``,
  and a drained queue resolves ``drain`` back to ``off``;
* **the refusal (#738)** — ``CAO_DELIVERY_QUEUE=shadow`` names a RETIRED
  position, so the delivery subsystem does not start, the hooks stay disarmed,
  and the server still boots.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.core.delivery import (
    EnqueueDraft,
    QueueMode,
    SwitchPosition,
    WriteThroughDisposition,
)
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.switches import Rejected

pytestmark = pytest.mark.asyncio


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


def _seed_live_row(runtime: object, clock: FakeClock, key: str = "live-1") -> None:
    """Put one live non-terminal row in the queue, bypassing the switch.

    Written through the store rather than through a hook because the condition
    under test is the QUEUE'S CONTENT at the next boot, and the position that
    wrote it is not part of the condition.
    """
    pool = runtime.pool  # type: ignore[attr-defined]
    assert pool is not None
    SqliteQueueStore(pool, clock=clock).enqueue(
        EnqueueDraft(idempotency_key=key, receiver_id="mb_x", mode=QueueMode.LIVE)
    )


# ------------------------------------------------------------------ the off arm


async def test_with_the_switch_unset_the_hooks_write_nothing(
    db_path: Path, clock: FakeClock
) -> None:
    """The off-arm criterion, through the real bootstrap.

    Not "few rows" and not "no visible change" — NIL rows.  Anything else in
    this arm means the switch is not the only thing standing between a hook and
    the queue.
    """
    runtime = await boot(db_path, None, clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.OFF
    assert wiring.queue_enabled() is False

    for legacy_id in range(1, 6):
        assert (
            wiring.write_through(fact(legacy_id)).disposition
            is WriteThroughDisposition.NOT_ATTEMPTED
        )

    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 0

    await bootstrap.shutdown_worker_truth()


async def test_on_arms_the_hooks_so_the_off_arm_is_a_difference(
    db_path: Path, clock: FakeClock
) -> None:
    """The other arm, so the off-arm assertion is a DIFFERENCE and not a tautology."""
    runtime = await boot(db_path, "on", clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.ON
    assert wiring.queue_enabled() is True

    for legacy_id in range(1, 6):
        assert wiring.write_through(fact(legacy_id)).disposition is WriteThroughDisposition.ACCEPTED

    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 5
    assert runtime.queue_store.count(mode=QueueMode.LIVE) == 5

    await bootstrap.shutdown_worker_truth()


async def test_duplicate_write_through_still_returns_the_typed_acceptance(
    db_path: Path, clock: FakeClock
) -> None:
    runtime = await boot(db_path, "on", clock)
    first = wiring.write_through(replace(fact(101), content_hash="same-content"))
    duplicate = wiring.write_through(replace(fact(202), content_hash="same-content"))

    assert first.disposition is WriteThroughDisposition.ACCEPTED
    assert duplicate.disposition is WriteThroughDisposition.ACCEPTED
    assert duplicate.surrogate_id == first.surrogate_id
    assert duplicate.msg_id == first.msg_id
    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 1

    await bootstrap.shutdown_worker_truth()


async def test_shutdown_disarms_the_hooks(db_path: Path, clock: FakeClock) -> None:
    """A hook firing while the pool closes would log a failure for a clean stop."""
    await boot(db_path, "on", clock)
    await bootstrap.shutdown_worker_truth()
    assert wiring.queue_enabled() is False


# ------------------------------------------------------- #738 the retired value


async def test_a_retired_position_refuses_the_subsystem_and_not_the_boot(
    db_path: Path, clock: FakeClock
) -> None:
    """#738's boot contract, end to end through the composition root.

    ``CAO_DELIVERY_QUEUE=shadow`` was a working line in a shipped build, and the
    laptop's own systemd drop-in still carries it.  Three properties are asserted
    together because each alone is satisfiable by a wrong answer:

    * the SERVER still boots — a diagnosability feature that can stop the server
      has inverted its purpose, so the refusal costs the subsystem only;
    * the hooks are NOT armed — a mode that no longer exists must not run;
    * the answer is the typed :class:`Rejected`, not a silent coercion to
      ``off`` — coercing would run a deployment in a position nobody requested
      while its configuration still claimed otherwise.

    MUTANT: accept ``shadow`` again (restore the enum member and drop the
    retired-value branch) and ``runtime.delivery`` is a ``GuardOutcome``, which
    fails the isinstance assertion below.
    """
    runtime = await boot(db_path, "shadow", clock)

    assert runtime.delivery is not None
    assert isinstance(runtime.delivery, Rejected)
    assert runtime.delivery.value == "shadow"
    assert "738" in runtime.delivery.reason
    assert "off|drain|on" in runtime.delivery.hint

    assert wiring.queue_enabled() is False
    assert bootstrap.delivery_health_component() == "rejected/#738"

    for legacy_id in range(1, 4):
        assert (
            wiring.write_through(fact(legacy_id)).disposition
            is WriteThroughDisposition.NOT_ATTEMPTED
        )

    await bootstrap.shutdown_worker_truth()


async def test_the_refusal_logs_exactly_one_error_carrying_the_literal_fix(
    db_path: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal must TELL the operator what to type (#738, gate r1 H2).

    The parallel of ``test_a_retired_position_is_refused_and_arms_nothing`` on the
    status side, and it exists because the delivery side did not have it. The
    Stage B adjudication mutated this call site from ``requested.detail`` to
    ``requested.reason`` — which keeps the typed ``Rejected``, the non-start, the
    disarmed hooks, the ``rejected/#738`` health component and the untouched
    queue, and only drops ``set CAO_DELIVERY_QUEUE=off|drain|on`` from the one
    message an operator ever sees — and all 68 shipped tests in this file and
    ``test/core/test_delivery.py`` stayed green. That is the gap.

    Asserting ``Rejected.hint`` at the parser boundary is NOT this: the hint can
    be perfect while the composition root logs something that omits it. What is
    pinned here is the LOG LINE, at the composition root, in the rejected state:

    * exactly ONE ``delivery queue NOT started`` ERROR — a boot that logged the
      refusal twice would train an operator to skim it, and one that logged none
      leaves them with a subsystem that is simply absent;
    * it names ``#738``, so the reason is findable;
    * it carries the literal ``set CAO_DELIVERY_QUEUE=off|drain|on`` — a literal
      to paste, never a description of one. This is the load-bearing clause: the
      laptop's own drop-in still selects ``shadow``, so this message is the
      instruction that unblocks the next redeploy.

    MUTANT (the gate's, replayed): ``requested.detail`` -> ``requested.reason``
    at ``bootstrap.py`` in ``_start_delivery``. The message keeps ``#738`` and
    fails on the literal-fix assertion.
    """
    with caplog.at_level("ERROR", logger="cli_agent_orchestrator.bootstrap"):
        runtime = await boot(db_path, "shadow", clock)

    assert isinstance(runtime.delivery, Rejected)

    refusals = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "ERROR" and "delivery queue NOT started" in record.getMessage()
    ]
    assert len(refusals) == 1, caplog.text
    assert "#738" in refusals[0]
    assert "set CAO_DELIVERY_QUEUE=off|drain|on" in refusals[0]

    await bootstrap.shutdown_worker_truth()


async def test_the_refusal_leaves_a_leftover_queue_alone(db_path: Path, clock: FakeClock) -> None:
    """A refused boot resolves NOTHING, so it cannot drain or orphan rows.

    The alternative — coercing to ``off`` and letting the guard run — would
    resolve a leftover live queue to ``drain`` and start serving it in a
    deployment whose configuration names a mode that does not exist.  The rows
    are still there at the next boot, when the operator has fixed the variable.
    """
    runtime = await boot(db_path, "on", clock)
    _seed_live_row(runtime, clock)
    await bootstrap.shutdown_worker_truth()

    rebooted = await boot(db_path, "shadow", clock)
    assert isinstance(rebooted.delivery, Rejected)
    assert rebooted.delivery_tick is None
    assert rebooted.queue_store is None

    from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore

    assert rebooted.pool is not None
    findings = SqliteFindingStore(rebooted.pool, clock=clock).list_findings(state="open")
    assert not [f for f in findings if f.code is FindingCode.DIAG_QUEUE_ORPHAN_GUARD]

    await bootstrap.shutdown_worker_truth()

    fixed = await boot(db_path, None, clock)
    assert fixed.delivery is not None and not isinstance(fixed.delivery, Rejected)
    assert fixed.delivery.position is SwitchPosition.DRAIN
    await bootstrap.shutdown_worker_truth()


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
    runtime = await boot(db_path, "on", clock)
    _seed_live_row(runtime, clock)
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


async def test_a_drained_queue_resolves_drain_back_to_off(db_path: Path, clock: FakeClock) -> None:
    """The one cell that moves on its own, once the queue is empty.

    Holding a drained deployment in ``drain`` would leave the delivery machinery
    running with nothing to deliver.  Before #738 this cell landed on ``shadow``;
    ``off`` is what ``shadow`` meant for the served path anyway.
    """
    runtime = await boot(db_path, "drain", clock)
    assert runtime.delivery is not None
    assert runtime.delivery.position is SwitchPosition.OFF
    assert runtime.delivery.finding is FindingCode.DIAG_QUEUE_ORPHAN_GUARD
    assert "drain complete" in runtime.delivery.detail
    await bootstrap.shutdown_worker_truth()


@pytest.mark.parametrize("position", ["drain", "on"])
async def test_a_served_position_arms_the_hooks_and_registers_the_tick(
    db_path: Path, clock: FakeClock, position: str
) -> None:
    """Sub-phase 3b implements both served positions, and they differ (§6, §7b).

    * ``on`` owns NEW traffic, so a legacy enqueue becomes a ``mode='live'`` row;
    * ``drain`` accepts no new queue rows at all, which is the position's whole
      point — it finishes the rows already enqueued on their own budget while
      new traffic goes back to the legacy inbox, and that is the only way back
      out of ``on`` that does not orphan them.

    ``drain`` reaches this test with a non-empty live queue, since over an empty
    one the guard resolves it to ``off`` (asserted above).
    """
    runtime = await boot(db_path, "on", clock)
    _seed_live_row(runtime, clock)
    await bootstrap.shutdown_worker_truth()

    rebooted = await boot(db_path, position, clock)
    assert rebooted.delivery is not None
    assert rebooted.delivery.position.value == position
    assert wiring.queue_enabled() is True
    assert rebooted.delivery_tick is not None, "a served position must have an observer"

    assert rebooted.queue_store is not None
    before = rebooted.queue_store.count()
    if position == "on":
        assert wiring.queue_owns_new_traffic() is True
        assert (
            wiring.write_through(fact(101)).disposition is WriteThroughDisposition.ACCEPTED
        ), "the write-through is the path"
        assert rebooted.queue_store.count(mode=QueueMode.LIVE) == before + 1
    else:
        assert wiring.queue_owns_new_traffic() is False
        assert wiring.write_through(fact(101)).disposition is WriteThroughDisposition.NOT_ATTEMPTED
        assert rebooted.queue_store.count() == before

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
        env={bootstrap.DELIVERY_ENV_VAR: "on"},
    )
    assert runtime.ingest_enabled is False
    assert runtime.delivery is not None and runtime.delivery.position is SwitchPosition.ON
    assert wiring.queue_enabled() is True
    await bootstrap.shutdown_worker_truth()


# ------------------------------------------------------ #738 the word itself


async def test_no_live_subsystem_in_bootstrap_calls_itself_shadow() -> None:
    """The retirement is audited by grep, so the WORD is part of the contract.

    Gate r1 H4c: WP-ARCH slice 2a's gate wiring described itself as "SHADOW"
    meaning "built, but the supervisor loop does not call it". That is a real and
    useful state, and it is NOT the mode #738 retired — which was new machinery
    running beside the real path, writing observational copies of live traffic.
    Nothing was broken by the wording. What it broke was the AUDIT: the retirement
    of shadow-live mode is checked by sweeping for the word, and a second,
    innocent meaning in the composition root makes that sweep report a survivor
    that is not one, every time, forever.

    So the word is reserved. ``bootstrap.py`` may name ``shadow`` only where it
    RETIRES it — the refusal path and prose that says the mode is gone — and a
    subsystem that is merely uncalled says so in those words.

    MUTANT: describe any inert subsystem here as "shadow" again and this fails,
    naming the line.
    """
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")

    # The pattern is a subsystem LABELLING ITSELF with the retired mode's name —
    # "slice 2a is SHADOW", "because slice 2a is shadow". Matched narrowly on
    # purpose: this module must go on naming ``shadow`` freely in the refusal
    # path ("a REQUESTED ``shadow`` is refused", "an operator whose drop-in still
    # says ``shadow``"), and a keyword blocklist wide enough to catch the label
    # would catch those too and be turned off within a round.
    label = re.compile(r"\bis\s+(?:a\s+)?shadow\b", re.IGNORECASE)

    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if label.search(line)
    ]

    assert not offenders, (
        "bootstrap.py describes a subsystem as 'shadow'; if it is merely not "
        "wired to the supervisor loop, say that instead (#738):\n" + "\n".join(offenders)
    )

"""The delivery queue at boot (WP-ARCH phase 3, F728 #584; #738; 3c).

Against a real database and the real composition root, because what is being
tested is a property of the WIRING: that a boot arms the hooks and that a
shutdown disarms them, with no third state in between.  That is the kind of claim
a double would happily confirm while the shipped bootstrap did something else.

This file used to test a SWITCH.  ``CAO_DELIVERY_QUEUE`` had three positions
while the queue and the legacy inbox were both carriers — ``off`` was the
pre-flip default under which legacy delivered, ``drain`` was the way back out of
``on`` that kept serving the rows already enqueued instead of stranding them
(#584) — and D9's boot guard resolved them against the queue's own content so an
operator could not orphan rows by editing a variable.  WP-ARCH 3c deletes the
legacy carriers.  There is no carrier to roll back TO, so the switch, the guard
and the ladder go together, and what is left is a subsystem that either came up
or did not.

Two criteria live here now:

* **the armed arm** — a boot installs the runtime, the write-through lands
  ``mode='live'`` rows, and the tick is registered;
* **the disarmed arm** — after shutdown no hook reaches the queue, which is what
  makes the armed arm a DIFFERENCE rather than a tautology.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from test.adapters.conftest import TEST_BUSY_TIMEOUT_MS, FakeClock

import pytest

from cli_agent_orchestrator import bootstrap
from cli_agent_orchestrator.app.delivery import wiring
from cli_agent_orchestrator.app.delivery.facts import LegacyEnqueue
from cli_agent_orchestrator.core.delivery import QueueMode

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


async def boot(db_path: Path, clock: FakeClock) -> object:
    """Boot with ingestion OFF, which is the shipped default for this suite.

    No ``position`` argument any more: there is no variable to set.  The
    signature carried one while the switch existed, and every arm that passed a
    value other than "arm it" is deleted below with its reason.
    """
    return await bootstrap.start_worker_truth(
        db_path=db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS, clock=clock, env={}
    )


# ------------------------------------------------------------ armed / disarmed
#
# The OFF ARM is deleted.  It asserted that with ``CAO_DELIVERY_QUEUE`` unset the
# count of ``delivery_msg`` rows was nil, which measured the switch's default
# position.  There is no default position to measure: a boot that gets a queue
# store arms the hooks.  What the arm really protected — "there is no code path
# from a hook to the queue that does not pass the install guard" — is asserted by
# ``test_shutdown_disarms_the_hooks`` below, which is now the only reachable
# disarmed state and therefore the only place that property can be measured
# through the real composition root.
#
# The #738 REFUSAL arms (three of them) are deleted with their subject.  They
# asserted that ``CAO_DELIVERY_QUEUE=shadow`` named a retired position, so the
# subsystem declined to start while the server booted, and that the ERROR carried
# the literal fix line.  With no variable there is no value to type and nothing
# to refuse.  The contract those arms exercised is ``core/switches.py``'s
# ``Rejected``/``retired_position``, which SURVIVES for the phase-2 status switch
# and is still killed by ``test/core/test_status_cutover.py`` and
# ``test/adapters/test_status_cutover_bootstrap.py`` — including the
# one-ERROR-carrying-the-literal-fix shape.  It is the delivery switch's USE of
# that contract that died, not the contract.
#
# The BOOT GUARD arms (leftover-queue demotion, drain-resolves-to-off, the
# served-position parametrisation) are deleted for the reason in the module
# docstring: the guard resolved a choice between two carriers and there is one
# carrier left.


async def test_a_boot_arms_the_hooks_and_registers_the_tick(
    db_path: Path, clock: FakeClock
) -> None:
    """The armed arm, through the real bootstrap.

    Four claims together, because each alone is satisfiable by a wrong answer: a
    runtime is installed, the write-through actually lands rows, every row it
    lands is ``mode='live'``, and the tick exists — a queue with no observer
    holds rows that nothing will serve until the next restart.
    """
    runtime = await boot(db_path, clock)
    assert runtime.delivery is True
    assert wiring.queue_owns_new_traffic() is True
    assert wiring.queue_owns_delivery() is True
    assert runtime.delivery_tick is not None, "an armed queue must have an observer"

    for legacy_id in range(1, 6):
        assert wiring.write_through(fact(legacy_id)) is not None

    assert runtime.queue_store is not None
    assert runtime.queue_store.count() == 5
    assert runtime.queue_store.count(mode=QueueMode.LIVE) == 5

    await bootstrap.shutdown_worker_truth()


async def test_shutdown_disarms_the_hooks(db_path: Path, clock: FakeClock) -> None:
    """The disarmed arm, and what makes the armed one a DIFFERENCE.

    Two reasons to pin it.  A hook firing while the pool closes would log a
    failure for a clean stop.  And this is the only state in which a hook can be
    asked to reach the queue and must not: NIL rows, not "few rows", because
    anything else means the install guard is not the only thing standing between
    a hook and the queue.
    """
    runtime = await boot(db_path, clock)
    assert runtime.queue_store is not None
    await bootstrap.shutdown_worker_truth()

    assert wiring.queue_owns_new_traffic() is False
    assert wiring.queue_owns_delivery() is False
    for legacy_id in range(1, 6):
        assert wiring.write_through(fact(legacy_id)) is None
    assert runtime.queue_store.count() == 0


async def test_the_health_component_says_on_while_armed_and_off_once_stopped(
    db_path: Path, clock: FakeClock
) -> None:
    """``/health``'s ``components.delivery``, in both of its two states.

    It had four answers while the switch existed — the three positions plus
    ``rejected/#738``.  It has two, and ``off`` no longer names a position: it
    means the subsystem did not come up, which on a healthy server happens only
    when the migration failed or the queue store could not be opened.
    """
    await boot(db_path, clock)
    assert bootstrap.delivery_health_component() == "on"
    await bootstrap.shutdown_worker_truth()
    assert bootstrap.delivery_health_component() == "off"


async def test_the_delivery_queue_is_independent_of_the_ingestion_switch(
    db_path: Path, clock: FakeClock
) -> None:
    """Two strangler phases, and phase 3 does not wait on phase 1.

    Coupling them would make the delivery queue a subsystem with a second
    precondition — a safety net that runs only when worker-truth ingestion
    happens to be on.  This was a statement about two SWITCHES until 3c collapsed
    phase 3's; it is now a statement about the composition root, which arms the
    queue before it reads the ingestion switch and regardless of what it says.
    """
    runtime = await bootstrap.start_worker_truth(
        db_path=db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS, clock=clock, env={}
    )
    assert runtime.ingest_enabled is False
    assert runtime.delivery is True
    assert wiring.queue_owns_delivery() is True
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

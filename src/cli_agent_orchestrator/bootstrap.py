"""The composition root (WP-ARCH phase 1, AC5/AC9 — hook point 5).

This is the ONE module allowed to name both halves of the new tree.  The
``adapters-only-via-composition-root`` import-linter contract forbids ``app``,
``api``, ``mcp_server`` and ``cli`` from importing ``adapters`` at all; here the
adapters are constructed and handed to ``app`` as ``core.ports`` Protocols.  It
is also the only new module that reads the legacy ``constants`` — every adapter
receives its database path and busy timeout as an argument, so nothing under
``adapters/`` has an opinion about where the server keeps its files.

Two rules the rest of phase 1 leans on:

* **The migrator runs at EVERY boot**, whatever the switch says.  The DDL is
  additive and, with ingestion off, inert.  Running it unconditionally makes
  turning the switch on a one-variable change rather than a migration event —
  which matters because the phase-1 diagnostics have to be readable against a
  server that is already running.
* **Nothing else runs unless ``CAO_WORKER_TRUTH_INGEST=1``.**  No producer, no
  projector, no sweep, no retention task.  AC11's "no behaviour change with the
  switch off" is then true by construction: the code paths do not exist to be
  wrong.  The switch is read from the process environment ONCE per bootstrap
  call, which is what makes it a deployment decision rather than something that
  can flip mid-session.

Nothing here may raise into the server's lifespan.  A diagnosability feature that
can stop the server from booting has inverted its own purpose, so every failure
becomes a ``DIAG-MIGRATION-FAILED`` finding (or one structured log line) plus
ingestion disabled for the process.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cli_agent_orchestrator.adapters.clock import SystemClock
from cli_agent_orchestrator.adapters.store.connection import ConnectionPool
from cli_agent_orchestrator.adapters.store.event_log import SqliteEventStore
from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
from cli_agent_orchestrator.adapters.store.migrator import MigrationResult, migrate
from cli_agent_orchestrator.adapters.store.queue import SqliteQueueStore
from cli_agent_orchestrator.adapters.store.readonly import ReadOnlyPool
from cli_agent_orchestrator.adapters.store.retention import RetentionTask
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.adapters.truth import wiring as truth_wiring
from cli_agent_orchestrator.app.delivery import wiring as delivery_wiring
from cli_agent_orchestrator.app.delivery.tick import DeliveryTick
from cli_agent_orchestrator.app.delivery.wake import WakeService
from cli_agent_orchestrator.app.diag.report import DiagSources
from cli_agent_orchestrator.app.worker_truth.checks import (
    CheckRegistry,
    PaneDisagreementCheck,
    register_phase1_checks,
)
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.delivery import (
    GuardOutcome,
    QueueOccupancy,
    SwitchPosition,
    parse_switch,
    resolve_switch,
)
from cli_agent_orchestrator.core.ports import (
    Clock,
    EventStore,
    FindingStore,
    QueueStore,
    StateStore,
)
from cli_agent_orchestrator.core.status_cutover import (
    StatusGuardOutcome,
    StatusPosition,
    parse_providers,
    parse_status_switch,
    resolve_status_switch,
)
from cli_agent_orchestrator.core.switches import Rejected

logger = logging.getLogger(__name__)

__all__ = [
    "DELIVERY_ENV_VAR",
    "delivery_health_component",
    "INGEST_ENV_VAR",
    "STATUS_ENV_VAR",
    "STATUS_PROVIDERS_ENV_VAR",
    "WorkerTruthRuntime",
    "build_readonly_diag_stores",
    "current_runtime",
    "delivery_position",
    "ingest_enabled",
    "shutdown_worker_truth",
    "start_worker_truth",
    "status_position",
]

INGEST_ENV_VAR = "CAO_WORKER_TRUTH_INGEST"

#: The delivery queue's own switch (D9).  A SEPARATE variable from the ingestion
#: one, sitting beside it here and read once at boot in the same structural way.
#: One master strangler flag was rejected because it would couple a phase-1
#: rollback to a phase-3 rollback; this is a different switch, not the second
#: spelling this module's own docstring warns against.
DELIVERY_ENV_VAR = "CAO_DELIVERY_QUEUE"

#: The status cutover's own switch (phase 2, D9), and its per-provider allowlist.
#: A THIRD variable beside the other two, for the reason the second one exists:
#: one master strangler flag would couple a phase-1 rollback to a phase-2
#: rollback, and each phase must be backable out on its own.
STATUS_ENV_VAR = "CAO_WORKER_TRUTH_STATUS"
STATUS_PROVIDERS_ENV_VAR = "CAO_WORKER_TRUTH_STATUS_PROVIDERS"


def ingest_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``CAO_WORKER_TRUTH_INGEST=1`` is set in the process environment.

    Default OFF, and strictly ``"1"``: a switch that also accepted ``"true"``,
    ``"yes"`` and ``"on"`` would be a switch nobody could state the position of
    from a process listing.
    """
    source = os.environ if env is None else env
    return source.get(INGEST_ENV_VAR) == "1"


def delivery_position(env: dict[str, str] | None = None) -> SwitchPosition | Rejected:
    """The REQUESTED position, before D9's guard resolves it.

    Requested, not effective: the guard can demote ``off`` to ``drain`` over a
    non-empty queue, and resolve ``drain`` back to ``off`` over an empty one.  The
    effective position is on the runtime, and a caller that wants to know what the
    server is actually doing must read it there.

    A RETIRED position (#738) is not a position at all: the answer is
    :class:`~core.switches.Rejected`, and the delivery subsystem does not start.
    """
    source = os.environ if env is None else env
    return parse_switch(source.get(DELIVERY_ENV_VAR))


def status_position(env: dict[str, str] | None = None) -> StatusPosition | Rejected:
    """The REQUESTED status-cutover position, before D9's guard resolves it.

    Requested, not effective, for the same reason :func:`delivery_position` says
    so: the guard demotes ``on`` to ``off`` when ingestion is off or the allowlist
    is empty.  The effective position is on the runtime.  A retired position
    (#738) answers :class:`~core.switches.Rejected` and arms nothing.
    """
    source = os.environ if env is None else env
    return parse_status_switch(source.get(STATUS_ENV_VAR))


def delivery_health_component() -> str:
    """One string for ``/health``'s ``components.delivery``.

    An operator whose drop-in still says ``shadow`` learns it from the running
    server rather than from a log line they have to go looking for: the boot's
    ERROR scrolls past, this does not.  ``rejected/#738`` is deliberately short —
    the reason is in the log, and a health payload is a status board, not an
    explanation.
    """
    runtime = _runtime
    if runtime is None or runtime.delivery is None:
        return "off"
    if isinstance(runtime.delivery, Rejected):
        return "rejected/#738"
    return runtime.delivery.position.value


@dataclass
class WorkerTruthRuntime:
    """Everything phase 1 built, and whether ingestion is live.

    ``ingest_enabled`` false with ``migration.ok`` true is the normal state: the
    tables exist, nothing writes to them.  ``ingest_enabled`` false with
    ``migration.ok`` false is the degraded state — the server booted, the
    failure is recorded, and ingestion stays off for the life of the process.
    """

    ingest_enabled: bool
    migration: MigrationResult
    clock: Clock
    pool: ConnectionPool | None = None
    event_store: EventStore | None = None
    finding_store: FindingStore | None = None
    checks: CheckRegistry | None = None
    state_store: StateStore | None = None
    projector: Projector | None = None
    sources: StaticSourceRegistry | None = None
    retention: RetentionTask | None = None
    #: The delivery queue's RESOLVED position (D9), and the guard's reasoning.
    #: Present whatever the ingestion switch says: the two are independent, and
    #: a queue that only ran when worker-truth ingestion happened to be on would
    #: be a coupling neither blueprint asks for.
    delivery: GuardOutcome | Rejected | None = None
    queue_store: QueueStore | None = None
    #: The status cutover's RESOLVED position (phase 2, D9) and the guard's
    #: reasoning.  Present whatever the ingestion switch says, because the guard's
    #: whole job in the ingestion-off cells is to record that it demoted.
    status: StatusGuardOutcome | None = None
    #: §5c's tick, present only for a position that is SERVED (``on`` or
    #: ``drain``).  Held here so shutdown can stop it and so a test can drive
    #: ``run_once`` directly rather than waiting on a cadence.
    delivery_tick: DeliveryTick | None = None


_runtime: WorkerTruthRuntime | None = None


def current_runtime() -> WorkerTruthRuntime | None:
    """The runtime built by the last :func:`start_worker_truth`, if any."""
    return _runtime


def _default_db_path() -> Path:
    """Read the database path from the legacy constants.

    The single legacy read in the new tree, and it lives here on purpose: it is
    what keeps ``adapters/store/`` free of any knowledge of where the server puts
    its files.  Imported inside the function so a test can point the bootstrap
    somewhere else without importing the fork's whole constants module.
    """
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return Path(DATABASE_FILE)


def _default_busy_timeout_ms() -> int:
    from cli_agent_orchestrator.constants import CAO_DB_BUSY_TIMEOUT_MS

    return int(CAO_DB_BUSY_TIMEOUT_MS)


def _start_delivery(
    pool: ConnectionPool,
    clock: Clock,
    *,
    env: dict[str, str] | None = None,
) -> tuple[GuardOutcome | Rejected, QueueStore | None, DeliveryTick | None]:
    """Resolve ``CAO_DELIVERY_QUEUE`` through D9's guard and arm the hooks.

    Never raises.  Three things happen, in this order and for this reason:

    0. **A retired position is refused before anything else.**  ``shadow`` was a
       shipped position and #738 removed the mode, so an operator carrying it in a
       drop-in gets a loud ERROR naming the value and the line to type, and the
       delivery subsystem does NOT start.  It is refused rather than coerced to
       ``off`` because coercion would run a deployment in a position nobody
       requested while its configuration still claimed otherwise.  The refusal
       costs the subsystem, never the server: the boot continues.
    1. **The requested position is read once**, from the process environment,
       which makes it a deployment decision rather than something that can flip
       mid-session.
    2. **The guard resolves it against the queue.**  Boot-time only — no runtime
       transition exists, so a position changes when the server restarts and at
       no other moment.  The guard never refuses the boot: this ships into the
       server running the strangler work, so a self-inflicted boot failure would
       be worse than the condition it reports, and an operator whose only mistake
       was leaving a variable unset must not lose the server.
    3. **A demotion writes its finding**, which is how an operator learns.  The
       finding store is built here regardless of the INGESTION switch, because
       the ``finding`` table is created by step 0 of every migration and the
       guard's notice belongs to phase 3, not to phase 1.

    ``drain`` and ``on`` arm the hooks, and they arm different things.  ``on``
    writes ``mode='live'`` rows, mutes D6's surfaces and runs the tick.  ``drain``
    accepts NO new queue rows while the tick finishes delivering the ones already
    there, which is the only way back out of ``on`` that does not orphan them
    (§6).  ``off`` arms nothing, and there is no code path from a hook to the
    queue that does not pass the install guard in the wiring module.
    """
    requested = delivery_position(env)
    if isinstance(requested, Rejected):
        logger.error(
            "delivery queue NOT started: %s (%s=%s)",
            requested.detail,
            DELIVERY_ENV_VAR,
            requested.value,
        )
        return requested, None, None
    try:
        store: QueueStore = SqliteQueueStore(pool, clock=clock)
        occupancy = store.occupancy()
    except Exception as exc:  # noqa: BLE001 — a queue we cannot read must not block boot
        logger.error("delivery queue could not be opened: %r", exc)
        return GuardOutcome(requested=requested, position=SwitchPosition.OFF), None, None

    outcome = resolve_switch(requested, occupancy)

    if outcome.finding is not None:
        try:
            SqliteFindingStore(pool, clock=clock).record(
                outcome.finding,
                dedupe_key=f"{outcome.requested.value}->{outcome.position.value}",
                detail=outcome.detail,
            )
        except Exception:  # noqa: BLE001 — a notice that cannot be written is logged
            logger.warning(
                "delivery boot guard: %s (finding could not be recorded)",
                outcome.detail,
                exc_info=True,
            )

    if outcome.demoted:
        logger.warning(
            "delivery boot guard resolved %s=%s to %s: %s",
            DELIVERY_ENV_VAR,
            outcome.requested.value,
            outcome.position.value,
            outcome.detail,
        )

    if outcome.position is SwitchPosition.OFF:
        return outcome, store, None

    delivery_wiring.install_delivery(
        delivery_wiring.DeliveryRuntime(
            store=store,
            clock=clock,
            position=outcome.position,
        )
    )
    logger.info("delivery queue armed in %s mode (%s)", outcome.position.value, DELIVERY_ENV_VAR)

    findings: FindingStore | None
    try:
        findings = SqliteFindingStore(pool, clock=clock)
    except Exception:  # noqa: BLE001 — a tick without findings still delivers
        logger.warning("delivery: the finding store could not be built", exc_info=True)
        findings = None
    tick = _build_delivery_tick(store, clock, position=outcome.position, findings=findings)
    return outcome, store, tick


def _build_delivery_tick(
    store: QueueStore,
    clock: Clock,
    *,
    position: SwitchPosition,
    findings: FindingStore | None,
) -> DeliveryTick | None:
    """Assemble §5c's tick, or ``None`` for a position that is not served.

    The one place the legacy carrier bridge is NAMED, for the same reason this
    module is the one place an adapter is named: ``app`` may not import
    ``services``, so the tick depends on three Protocols and the composition root
    is what satisfies them.  Imported inside the function so a test can build the
    tick from doubles without pulling the legacy service tree in.
    """
    if position not in (SwitchPosition.ON, SwitchPosition.DRAIN):
        return None
    try:
        from cli_agent_orchestrator.services.queue_carrier import (
            LegacyReceiverDirectory,
            NativeSeatCarrier,
            PaneWorkerInjector,
        )

        directory = LegacyReceiverDirectory()
        wake = WakeService(
            store=store,
            directory=directory,
            carrier=NativeSeatCarrier(),
            injector=PaneWorkerInjector(),
            clock=clock,
        )
        return DeliveryTick(
            store=store,
            wake=wake,
            directory=directory,
            findings=findings,
            clock=clock,
            position=position,
        )
    except Exception:  # noqa: BLE001 — a tick that cannot be built must not block boot
        logger.error(
            "delivery tick could not be assembled; the queue holds rows nothing will "
            "serve until the next restart",
            exc_info=True,
        )
        return None


def _resolve_status_cutover(
    pool: ConnectionPool,
    clock: Clock,
    *,
    enabled: bool,
    env: dict[str, str] | None = None,
) -> StatusGuardOutcome:
    """Resolve ``CAO_WORKER_TRUTH_STATUS`` through D9's guard.  Never raises.

    The startup check §12 asks for.  D9's resolution table raises
    ``DIAG-STATUS-GUARD`` in three of its six cells and no acceptance criterion
    drove any of them, so the finding is asserted here — a boot per demoting cell
    — which is cheaper as a startup test than as a live session case.

    Sub-phase 2a implements ``off`` only, now that ``shadow`` is retired (#738):
    the publisher is D1's feed and lands in 2b.  A boot that resolves to ``on``
    therefore gets a loud warning and NO publisher, rather than being quietly
    reinterpreted as something that runs — the shape ``_start_delivery`` uses
    above, and for its reason: an operator who asked for the feed and silently got
    a different mode would believe consumers were reading the projection when they
    were not.

    A REQUESTED ``shadow`` is refused outright, exactly as ``_start_delivery``
    refuses it, and resolves to ``off`` with one ERROR line naming the fix.
    """
    source = os.environ if env is None else env
    requested_or_rejected = parse_status_switch(source.get(STATUS_ENV_VAR))
    if isinstance(requested_or_rejected, Rejected):
        logger.error(
            "status cutover NOT armed: %s (%s=%s)",
            requested_or_rejected.detail,
            STATUS_ENV_VAR,
            requested_or_rejected.value,
        )
        return StatusGuardOutcome(
            requested=StatusPosition.OFF,
            position=StatusPosition.OFF,
            providers=parse_providers(source.get(STATUS_PROVIDERS_ENV_VAR)),
        )
    requested = requested_or_rejected
    providers = parse_providers(source.get(STATUS_PROVIDERS_ENV_VAR))
    outcome = resolve_status_switch(requested, ingest_enabled=enabled, providers=providers)

    if outcome.finding is not None:
        try:
            SqliteFindingStore(pool, clock=clock).record(
                outcome.finding,
                dedupe_key=f"{outcome.requested.value}->{outcome.position.value}",
                detail=outcome.detail,
            )
        except Exception:  # noqa: BLE001 — a notice that cannot be written is logged
            logger.warning(
                "status cutover boot guard: %s (finding could not be recorded)",
                outcome.detail,
                exc_info=True,
            )

    if outcome.demoted:
        logger.warning(
            "status cutover boot guard resolved %s=%s to %s: %s",
            STATUS_ENV_VAR,
            outcome.requested.value,
            outcome.position.value,
            outcome.detail,
        )

    if outcome.position is StatusPosition.ON:
        logger.warning(
            "%s resolved to on, which sub-phase 2a does not implement: the "
            "projection is NOT being published and every consumer still reads the "
            "pane path. Unset %s until sub-phase 2b ships.",
            STATUS_ENV_VAR,
            STATUS_ENV_VAR,
        )
    return outcome


async def start_worker_truth(
    *,
    db_path: Path | None = None,
    busy_timeout_ms: int | None = None,
    clock: Clock | None = None,
    env: dict[str, str] | None = None,
) -> WorkerTruthRuntime:
    """Migrate, wire the adapters, and start the phase-1 tasks.  Never raises.

    Called once from the server lifespan (hook point 5).  Returns the runtime so
    a test can assert exactly what was and was not started.
    """
    global _runtime

    resolved_clock: Clock = clock if clock is not None else SystemClock()
    enabled = ingest_enabled(env)

    try:
        path = db_path if db_path is not None else _default_db_path()
        timeout = busy_timeout_ms if busy_timeout_ms is not None else _default_busy_timeout_ms()
    except Exception as exc:  # noqa: BLE001 — a config read must not block boot
        logger.error("worker-truth bootstrap could not resolve the database path: %r", exc)
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False,
            migration=MigrationResult(ok=False, failed_step="config", error=repr(exc)),
            clock=resolved_clock,
        )
        return _runtime

    result, pool = migrate(path, busy_timeout_ms=timeout)

    if not result.ok or pool is None:
        # Booted, failure recorded, ingestion off for the life of the process —
        # and the delivery queue off with it. Its tables are migration steps, so
        # a failed migration may well be the step that would have created them;
        # arming hooks against a schema that may not exist would turn a recorded
        # failure into a stream of caught exceptions on every message.
        delivery_wiring.reset_delivery()
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False, migration=result, clock=resolved_clock, pool=pool
        )
        return _runtime

    # The delivery switch is resolved whatever the INGESTION switch says: they
    # are two independent strangler phases and coupling them would mean a
    # phase-3 rollback needed a phase-1 decision.
    delivery, queue_store, delivery_tick = _start_delivery(pool, resolved_clock, env=env)
    if delivery_tick is not None:
        # Started HERE rather than behind the ingestion switch: the two are
        # independent strangler phases, and a queue that only ran when phase 1
        # happened to be ingesting would be a coupling neither blueprint asks
        # for — and, worse, a delivery safety net with a second precondition.
        await delivery_tick.start()
    # Resolved BEFORE the ingestion-off early return, because the ingestion-off
    # cells are two of the three the guard exists to report: an operator who set
    # the cutover without the ingestion gate learns it from the finding, and
    # returning early would be the one path on which they learn nothing.
    status = _resolve_status_cutover(pool, resolved_clock, enabled=enabled, env=env)

    if not enabled:
        # Tables exist and phase 1 is inert.  No event store, no tasks, nothing
        # of phase 1's contending for the single writer.
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False,
            migration=result,
            clock=resolved_clock,
            pool=pool,
            delivery=delivery,
            queue_store=queue_store,
            status=status,
            delivery_tick=delivery_tick,
        )
        return _runtime

    try:
        finding_store = SqliteFindingStore(pool, clock=resolved_clock)
        checks = register_phase1_checks(CheckRegistry(finding_store))
        event_store = SqliteEventStore(pool, clock=resolved_clock, check_runner=checks)
        state_store = SqliteStateStore(pool)
        # The adapters that declare an authoritative source register themselves
        # here as they start (lane B's rollout tailer).  Empty is the correct
        # starting point: with no tailer running, every terminal falls back to
        # the pane, which is what phase 1 wants until a source proves itself.
        sources = StaticSourceRegistry()
        projector = Projector(
            event_store,
            state_store,
            resolved_clock,
            sources,
            legacy_check=PaneDisagreementCheck(
                finding_store, event_store, state_store, resolved_clock
            ),
        )
        retention = RetentionTask(event_store, resolved_clock)
        await retention.start()
        # Arm the phase-1 PRODUCERS.  Until this line runs, the seven legacy hook
        # points are no-ops that cost one module-global lookup; after it, they
        # append.  That is the whole of AC5's enforcement, and it is why the
        # switch cannot be half-on: there is no other way for a hook to reach the
        # store.
        # ``state_store`` is what makes source-level precedence work end to end,
        # and it is the one argument the two lanes could each omit without
        # noticing.  Lane B's rollout tailer bumps ``last_source_probe_at``
        # through it on every poll; lane C's projector reads that column to
        # decide whether an authoritative source is healthy, and treats NULL as
        # unhealthy on purpose.  Leave it out and the column is never written,
        # so every codex terminal reads as having no live source, derived pane
        # events apply in full, and r9's source precedence silently never
        # engages — with both lanes' own tests still green, because each injects
        # its own double here.
        # WP-ARCH phase 2, A1: the projector built above is also HANDED IN, as
        # the ``StateFolder`` port, so ``emit`` folds every appended event. At
        # phase 1's anchor this line passed everything but the projector, so the
        # local was dropped and ``Projector.project`` had no call site anywhere —
        # the fold that writes ``status.transition`` never ran, and those rows
        # are the only proof the projector runs at all. The field is typed on the
        # Protocol, so nothing under ``adapters/`` names ``Projector``; this is
        # the one module allowed to know both halves.
        truth_wiring.install_producers(
            truth_wiring.ProducerRuntime(
                store=event_store,
                clock=resolved_clock,
                state_store=state_store,
                findings=finding_store,
                folder=projector,
            )
        )
    except Exception as exc:  # noqa: BLE001 — wiring must not block boot either
        logger.error("worker-truth bootstrap failed to wire adapters: %r", exc)
        truth_wiring.reset_producers()
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False,
            migration=result,
            clock=resolved_clock,
            pool=pool,
            delivery=delivery,
            queue_store=queue_store,
            status=status,
            delivery_tick=delivery_tick,
        )
        return _runtime

    _runtime = WorkerTruthRuntime(
        ingest_enabled=True,
        migration=result,
        clock=resolved_clock,
        pool=pool,
        event_store=event_store,
        finding_store=finding_store,
        checks=checks,
        state_store=state_store,
        projector=projector,
        sources=sources,
        retention=retention,
        delivery=delivery,
        queue_store=queue_store,
        status=status,
        delivery_tick=delivery_tick,
    )
    logger.info("worker-truth ingestion ENABLED (%s=1)", INGEST_ENV_VAR)
    return _runtime


async def shutdown_worker_truth() -> None:
    """Stop the phase-1 tasks and drop the runtime.  Never raises."""
    global _runtime

    runtime = _runtime
    _runtime = None
    # Disarm the producers FIRST: a hook that fires while the pool is closing
    # would log a failure for a shutdown that is going perfectly well.
    truth_wiring.reset_producers()
    delivery_wiring.reset_delivery()
    if runtime is None:
        return
    if runtime.delivery_tick is not None:
        try:
            await runtime.delivery_tick.stop()
        except Exception:  # noqa: BLE001
            logger.warning("delivery tick did not stop cleanly", exc_info=True)
    if runtime.retention is not None:
        try:
            await runtime.retention.stop()
        except Exception:  # noqa: BLE001
            logger.warning("worker-truth retention task did not stop cleanly", exc_info=True)
    if runtime.pool is not None:
        runtime.pool.close_all()


# ---------------------------------------------------------------------------
# Read-only wiring for ``cao diag`` (AC7).
#
# The CLI is a separate process and must not import ``adapters`` (AC9's fifth
# contract), so it asks here instead.  Everything below opens the LIVE database
# with a ``mode=ro`` URI: WAL permits the concurrent reader, and a diagnostic
# command that could take a write lock would be able to stall the very server it
# was called to diagnose.
# ---------------------------------------------------------------------------


def build_readonly_diag_stores(db_path: str | Path | None = None) -> DiagSources:
    """The three stores ``cao diag`` reads, opened read-only.

    The same store classes the server writes with, over a read-only connection.
    A second, read-only copy of each store would be two implementations of the
    same SELECTs, free to disagree about what a row means.
    """
    path = Path(db_path) if db_path is not None else _default_db_path()
    pool = ReadOnlyPool(path, busy_timeout_ms=_default_busy_timeout_ms())
    return DiagSources(
        events=SqliteEventStore(pool, clock=SystemClock()),
        states=SqliteStateStore(pool),
        findings=SqliteFindingStore(pool, clock=SystemClock()),
        queue=SqliteQueueStore(pool, clock=SystemClock()),
    )

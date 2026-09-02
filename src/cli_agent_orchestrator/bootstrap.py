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
  which matters because the AC10 agreement session has to be startable against a
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
from cli_agent_orchestrator.adapters.store.readonly import ReadOnlyPool
from cli_agent_orchestrator.adapters.store.retention import RetentionTask
from cli_agent_orchestrator.adapters.store.state import SqliteStateStore
from cli_agent_orchestrator.app.diag.report import DiagSources
from cli_agent_orchestrator.app.worker_truth.agreement import TerminalFacts
from cli_agent_orchestrator.app.worker_truth.checks import (
    CheckRegistry,
    LegacyDisagreementCheck,
    register_phase1_checks,
)
from cli_agent_orchestrator.app.worker_truth.projector import Projector, StaticSourceRegistry
from cli_agent_orchestrator.core.ports import Clock, EventStore, FindingStore, StateStore

logger = logging.getLogger(__name__)

__all__ = [
    "INGEST_ENV_VAR",
    "WorkerTruthRuntime",
    "build_readonly_diag_stores",
    "build_terminal_scope",
    "current_runtime",
    "ingest_enabled",
    "shutdown_worker_truth",
    "start_worker_truth",
]

INGEST_ENV_VAR = "CAO_WORKER_TRUTH_INGEST"


def ingest_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``CAO_WORKER_TRUTH_INGEST=1`` is set in the process environment.

    Default OFF, and strictly ``"1"``: a switch that also accepted ``"true"``,
    ``"yes"`` and ``"on"`` would be a switch nobody could state the position of
    from a process listing.
    """
    source = os.environ if env is None else env
    return source.get(INGEST_ENV_VAR) == "1"


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
        # Booted, failure recorded, ingestion off for the life of the process.
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False, migration=result, clock=resolved_clock, pool=pool
        )
        return _runtime

    if not enabled:
        # Tables exist and are inert.  No store, no tasks, nothing to contend
        # for the single writer.
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False, migration=result, clock=resolved_clock, pool=pool
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
            legacy_check=LegacyDisagreementCheck(
                finding_store, event_store, state_store, resolved_clock
            ),
        )
        retention = RetentionTask(event_store, resolved_clock)
        await retention.start()
    except Exception as exc:  # noqa: BLE001 — wiring must not block boot either
        logger.error("worker-truth bootstrap failed to wire adapters: %r", exc)
        _runtime = WorkerTruthRuntime(
            ingest_enabled=False, migration=result, clock=resolved_clock, pool=pool
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
    )
    logger.info("worker-truth ingestion ENABLED (%s=1)", INGEST_ENV_VAR)
    return _runtime


async def shutdown_worker_truth() -> None:
    """Stop the phase-1 tasks and drop the runtime.  Never raises."""
    global _runtime

    runtime = _runtime
    _runtime = None
    if runtime is None:
        return
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
    )


def build_terminal_scope(db_path: str | Path | None = None) -> dict[str, TerminalFacts]:
    """Session and provider per terminal, from the legacy ``terminals`` table.

    The agreement report (AC10) needs to scope by session and to know which
    terminals are codex, and neither fact is in the event log — ``tmux_session``
    and ``provider`` live on the legacy row.  Read here rather than in ``app``
    for the usual reason: this is the module allowed to know about both halves.

    Raw SQL rather than the SQLAlchemy model, because the model would pull the
    fork's whole ``clients.database`` import graph into a read-only CLI path, and
    because this connection is deliberately read-only while that module's engine
    is not.

    Returns an empty mapping when the table cannot be read.  A missing scope
    degrades the report to fleet-wide with codex detected from the producer
    column, which is a worse report but a real one; raising here would mean the
    agreement command failed on a database that is otherwise perfectly readable.
    """
    path = Path(db_path) if db_path is not None else _default_db_path()
    pool = ReadOnlyPool(path, busy_timeout_ms=_default_busy_timeout_ms())
    try:
        rows = pool.connection().execute("SELECT id, tmux_session, provider FROM terminals")
        return {
            row["id"]: TerminalFacts(
                session=row["tmux_session"] or "", provider=row["provider"] or ""
            )
            for row in rows
        }
    except Exception:  # noqa: BLE001 — a missing scope degrades the report, never fails it
        logger.warning("could not read the legacy terminals table for diag scope", exc_info=True)
        return {}
    finally:
        pool.close_all()

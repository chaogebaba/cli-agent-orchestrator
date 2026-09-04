"""The phase-1 migrator (WP-ARCH phase 1, AC3/AC5/AC8).

Runs at EVERY boot, whatever ``CAO_WORKER_TRUTH_INGEST`` says.  The DDL is
purely additive and, with ingestion off, entirely inert: three new tables and
four indexes that nothing reads.  Running it unconditionally is what makes
turning the switch on a one-variable change rather than a migration event, which
matters because the agreement session (AC10) has to be startable on a server
that is already up.

Two ordering rules, both from AC5 (N6):

1. ``finding`` is created FIRST, in its OWN transaction.  A migration failure is
   reported by writing a ``DIAG-MIGRATION-FAILED`` finding — so the table that
   records the failure must exist before anything that can fail.
2. A failure NEVER blocks boot.  It writes one finding row (or, if even that is
   impossible, one structured log line carrying the same fields), disables
   ingestion for the process, and returns.  The alternative — a server that
   refuses to start because a diagnostic table would not create — trades a
   diagnosability feature for an outage, which is the opposite of the point.

Self-hosting makes this concrete rather than theoretical: phase 1 ships into the
very server that runs the strangler work (§6).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool

logger = logging.getLogger(__name__)

__all__ = [
    "FINDING_DDL",
    "MIGRATION_STEPS",
    "MigrationResult",
    "migrate",
]

# ---------------------------------------------------------------------------
# DDL
#
# ``worker_event`` and ``worker_event_seq`` are the audit §3.1 statement,
# column for column, with ``IF NOT EXISTS`` added so a second boot is a no-op.
# §3.1 closes with "this is the single statement of the schema — §4 adds no
# columns", so any future column belongs in a later phase's own migration.
# ---------------------------------------------------------------------------

FINDING_DDL = """
CREATE TABLE IF NOT EXISTS finding (
  finding_id      TEXT PRIMARY KEY,
  code            TEXT NOT NULL,
  terminal_id     TEXT NOT NULL DEFAULT '',
  dedupe_key      TEXT NOT NULL DEFAULT '',
  detail          TEXT NOT NULL DEFAULT '',
  sample_event_id TEXT,
  count           INTEGER NOT NULL DEFAULT 1,
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  state           TEXT NOT NULL DEFAULT 'open',
  UNIQUE(code, terminal_id, dedupe_key, state))
"""

_WORKER_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS worker_event (
  event_id     TEXT PRIMARY KEY,
  terminal_id  TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  kind         TEXT NOT NULL,
  producer     TEXT NOT NULL,
  confidence   TEXT NOT NULL,
  observed_at  TEXT NOT NULL,
  ingested_at  TEXT NOT NULL,
  payload      TEXT NOT NULL,
  source_ref   TEXT,
  run_id       TEXT,
  msg_id       TEXT,
  decision     TEXT,
  evidence     TEXT,
  UNIQUE(terminal_id, seq))
"""

_WORKER_EVENT_SEQ_DDL = """
CREATE TABLE IF NOT EXISTS worker_event_seq (
  terminal_id TEXT PRIMARY KEY,
  high_water  INTEGER NOT NULL)
"""

# The projection AC6 writes.  It lives in this migrator, not in a second one,
# so phase 1 has exactly ONE place where its schema is stated.  Heartbeats are
# the four liveness COLUMNS here (``last_probe_at``, ``pane_pid``,
# ``pane_present``, ``miss_count``) — r9 retired the per-tick event rows, so a
# 20-second fleet probe costs column updates rather than an event per terminal
# per tick.
_WORKER_STATE_SHADOW_DDL = """
CREATE TABLE IF NOT EXISTS worker_state_shadow (
  terminal_id          TEXT PRIMARY KEY,
  state                TEXT NOT NULL,
  since                TEXT NOT NULL,
  last_event_seq       INTEGER NOT NULL DEFAULT 0,
  degraded_reason      TEXT,
  prior_state          TEXT,
  last_probe_at        TEXT,
  last_source_probe_at TEXT,
  pane_pid             INTEGER,
  pane_present         INTEGER NOT NULL DEFAULT 0,
  miss_count           INTEGER NOT NULL DEFAULT 0)
"""

# ---------------------------------------------------------------------------
# Phase 3 — the delivery queue (audit §3.2, blueprint §5).
#
# Added to THIS migrator rather than a second one.  D5 is explicit that phase 3
# reuses phase 1 rather than forking it: decision rows into ``worker_event``, DDL
# here, ids from ``core/ids.py``, findings into ``core/findings.py``.  A second
# migrator or a second ULID factory is a review-stopping defect, because it is
# how one schema comes to have two authorities.
#
# The column set is the audit's statement plus what the blueprint's decisions
# require, each named where it is decided:
#
#   mode                D9/B16 — the shadow/live discriminator the occupancy
#                       predicate and ``claim``'s filter both read.
#   dead_by             D12    — stamped ONCE at enqueue.  No UPDATE statement in
#                       the adapter names this column; that is the enforcement.
#   held_since          D12    — the dialog-hold clock.
#   expire_after_s      D8     — the caller's own expiry, folded into dead_by.
#   supersede_key       F578, carried from ``inbox:514``.
#   content_hash        D13    — the F475 window check's key, deliberately NOT
#                       ``idempotency_key``: an earlier draft conflated them and
#                       would have dropped legitimate repeats a minute apart.
#   park_warm           D13    — an F475 conjunct, so the check needs the column.
#   barrier_id,
#   barrier_member_key  D13    — carried association, so ``CallbackBarrierModel``
#                       sees its members whichever table carries them.
#   enqueue_generation  D13    — recorded for diagnosis.  Queue rows are
#                       addressed to the durable mailbox id and are NOT
#                       generation-gated (§13c), which is what lets a fresh
#                       incarnation inherit pending rows with no rewrite (#33).
#   cancel_on_complete  D8     — the completion-cancel flag that actually reaches
#                       #435, where supersede_key alone does not.
#   is_notice           D14    — a dead-letter notice is never itself
#                       dead-lettered into another notice.
#   legacy_message_id   3a     — the mirror writer's join back to the inbox row.
#   terminated_at       §13d   — retention needs to know when a row ended.
#
# ``superseded`` joins the audit's four states, since F578 supersession and the
# flip's sweep both need an ending that is neither a delivery nor a death.
# ---------------------------------------------------------------------------

_DELIVERY_MSG_DDL = """
CREATE TABLE IF NOT EXISTS delivery_msg (
  msg_id             TEXT PRIMARY KEY,
  idempotency_key    TEXT NOT NULL UNIQUE,
  payload_digest     TEXT NOT NULL DEFAULT '',
  receiver_id        TEXT NOT NULL,
  sender_id          TEXT NOT NULL DEFAULT '',
  kind               TEXT NOT NULL,
  payload            TEXT NOT NULL DEFAULT '',
  state              TEXT NOT NULL,
  mode               TEXT NOT NULL,
  claim_id           INTEGER NOT NULL DEFAULT 0,
  lease_owner        TEXT,
  lease_expires_at   TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  max_attempts       INTEGER NOT NULL DEFAULT 5,
  available_at       TEXT NOT NULL,
  dead_by            TEXT NOT NULL,
  held_since         TEXT,
  expire_after_s     INTEGER,
  supersede_key      TEXT,
  content_hash       TEXT,
  park_warm          INTEGER NOT NULL DEFAULT 0,
  barrier_id         INTEGER,
  barrier_member_key TEXT,
  enqueue_generation INTEGER,
  cancel_on_complete INTEGER NOT NULL DEFAULT 0,
  is_notice          INTEGER NOT NULL DEFAULT 0,
  legacy_message_id  INTEGER,
  created_at         TEXT NOT NULL,
  terminated_at      TEXT)
"""

_DELIVERY_ATTEMPT_DDL = """
CREATE TABLE IF NOT EXISTS delivery_attempt (
  msg_id     TEXT NOT NULL,
  claim_id   INTEGER NOT NULL,
  carrier    TEXT NOT NULL,
  started_at TEXT NOT NULL,
  outcome    TEXT NOT NULL,
  detail     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (msg_id, claim_id, carrier))
"""

# A separate table, not a status flag — the audit adopted honker's decision, so
# a poisoned message stops occupying the reclaim loop and hiding live rows.
# ``mode`` is carried beyond the audit's columns: without it a shadow row that
# mirrored a legacy expiry is indistinguishable from a live dead-letter, and
# AC-3a's counting turns on exactly that distinction.
_DELIVERY_DEAD_DDL = """
CREATE TABLE IF NOT EXISTS delivery_dead (
  msg_id          TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL DEFAULT '',
  receiver_id     TEXT NOT NULL DEFAULT '',
  payload         TEXT NOT NULL DEFAULT '',
  attempts        INTEGER NOT NULL DEFAULT 0,
  reason          TEXT NOT NULL,
  mode            TEXT NOT NULL,
  died_at         TEXT NOT NULL)
"""

# ``msg_ids`` is a JSON array of IDS, never bodies: a wake costs one line of seat
# context rather than N message bodies.  ``consumed_via`` is a column so which
# surface landed a digest stops being a hypothesis (#499).
_SEAT_DIGEST_DDL = """
CREATE TABLE IF NOT EXISTS seat_digest (
  receiver_id  TEXT NOT NULL,
  epoch        INTEGER NOT NULL,
  msg_ids      TEXT NOT NULL,
  built_at     TEXT NOT NULL,
  consumed_at  TEXT,
  consumed_via TEXT,
  PRIMARY KEY (receiver_id, epoch))
"""

# Ordered migration steps AFTER the finding table.  A tuple of (name, statements)
# so a test can substitute a failing step and watch boot survive it.
MIGRATION_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("worker_event", (_WORKER_EVENT_DDL,)),
    ("worker_event_seq", (_WORKER_EVENT_SEQ_DDL,)),
    (
        "worker_event_indexes",
        (
            "CREATE INDEX IF NOT EXISTS ix_worker_event_scan "
            "ON worker_event(terminal_id, seq DESC)",
            # Partial indexes: the vast majority of rows carry neither a run nor
            # a message, so a full index would be mostly NULLs paid for on every
            # insert.  ``cao diag <run_id>`` and ``cao diag <msg_id>`` are the
            # only readers.
            "CREATE INDEX IF NOT EXISTS ix_worker_event_run "
            "ON worker_event(run_id) WHERE run_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS ix_worker_event_msg "
            "ON worker_event(msg_id) WHERE msg_id IS NOT NULL",
            # Retention scans by ingestion time; without this it is a full scan
            # of the log every sweep.
            "CREATE INDEX IF NOT EXISTS ix_worker_event_ingested " "ON worker_event(ingested_at)",
        ),
    ),
    ("worker_state_shadow", (_WORKER_STATE_SHADOW_DDL,)),
    ("delivery_msg", (_DELIVERY_MSG_DDL,)),
    (
        "delivery_msg_indexes",
        (
            # The audit's index, and the one ``claim`` runs on.
            "CREATE INDEX IF NOT EXISTS ix_delivery_ready "
            "ON delivery_msg(receiver_id, state, available_at)",
            # The mirror writer's join key.  Sub-phase 3a looks a row up by the
            # legacy inbox id on every observed edge, so without this every
            # settle is a full scan of the queue.  Partial, because only shadow
            # rows carry one.
            "CREATE INDEX IF NOT EXISTS ix_delivery_legacy "
            "ON delivery_msg(legacy_message_id) WHERE legacy_message_id IS NOT NULL",
            # Retention scans terminal rows by when they ended.
            "CREATE INDEX IF NOT EXISTS ix_delivery_terminated "
            "ON delivery_msg(terminated_at) WHERE terminated_at IS NOT NULL",
        ),
    ),
    ("delivery_attempt", (_DELIVERY_ATTEMPT_DDL,)),
    ("delivery_dead", (_DELIVERY_DEAD_DDL,)),
    ("seat_digest", (_SEAT_DIGEST_DDL,)),
    (
        "seat_digest_indexes",
        (
            # "the receiver's OPEN epoch" is the only lookup the digest has, and
            # it is on the hot path of both the tick and the re-parent.
            "CREATE INDEX IF NOT EXISTS ix_seat_digest_open "
            "ON seat_digest(receiver_id, epoch) WHERE consumed_at IS NULL",
        ),
    ),
)


@dataclass(frozen=True)
class MigrationResult:
    """What the migrator did, and whether ingestion may proceed.

    ``ok`` false is not an error the caller raises on — it is the signal to run
    the server with ingestion disabled.
    """

    ok: bool
    steps_applied: tuple[str, ...] = ()
    failed_step: str | None = None
    error: str | None = None
    finding_table_ready: bool = False
    detail: dict[str, str] = field(default_factory=dict)


def _record_migration_failure(
    pool: ConnectionPool,
    *,
    failed_step: str,
    error: str,
    finding_table_ready: bool,
) -> None:
    """Write one ``DIAG-MIGRATION-FAILED`` row, or one structured log line.

    Imported lazily so this module does not depend on the finding store for the
    happy path, and wrapped so that a failure to record a failure still cannot
    reach the caller.
    """
    fields = {
        "code": "DIAG-MIGRATION-FAILED",
        "step": failed_step,
        "db_path": str(pool.db_path),
        "error": error,
    }
    if finding_table_ready:
        try:
            from cli_agent_orchestrator.adapters.store.findings import SqliteFindingStore
            from cli_agent_orchestrator.core.findings import FindingCode

            SqliteFindingStore(pool).record(
                FindingCode.DIAG_MIGRATION_FAILED,
                dedupe_key=failed_step,
                detail=error,
            )
            return
        except Exception as exc:  # noqa: BLE001 — recording a failure must not fail
            fields["record_error"] = repr(exc)
    logger.error("worker-truth migration failed: %s", fields)


def migrate(
    db_path: Path, *, busy_timeout_ms: int
) -> tuple[MigrationResult, ConnectionPool | None]:
    """Apply the phase-1 DDL.  Never raises.

    Returns the result and, when the database could be opened at all, the pool
    the caller should reuse — reopening would discard the WAL and busy-timeout
    pragmas this connection already set.
    """
    pool: ConnectionPool | None = None
    try:
        pool = ConnectionPool(db_path, busy_timeout_ms=busy_timeout_ms)
        conn = pool.connection()
    except Exception as exc:  # noqa: BLE001 — a database we cannot open must not block boot
        logger.error(
            "worker-truth migration could not open the database: %s",
            {"db_path": str(db_path), "error": repr(exc)},
        )
        return MigrationResult(ok=False, failed_step="connect", error=repr(exc)), None

    # Step 0, its own transaction: the table that records every other failure.
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(FINDING_DDL)
        conn.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        _record_migration_failure(
            pool, failed_step="finding", error=repr(exc), finding_table_ready=False
        )
        return MigrationResult(ok=False, failed_step="finding", error=repr(exc)), pool

    applied: list[str] = ["finding"]
    for name, statements in MIGRATION_STEPS:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                conn.execute(statement)
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            _record_migration_failure(
                pool, failed_step=name, error=repr(exc), finding_table_ready=True
            )
            return (
                MigrationResult(
                    ok=False,
                    steps_applied=tuple(applied),
                    failed_step=name,
                    error=repr(exc),
                    finding_table_ready=True,
                ),
                pool,
            )
        applied.append(name)

    return (
        MigrationResult(ok=True, steps_applied=tuple(applied), finding_table_ready=True),
        pool,
    )

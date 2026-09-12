"""The phase-1 migrator (WP-ARCH phase 1, AC3/AC5/AC8).

Runs at EVERY boot, whatever ``CAO_WORKER_TRUTH_INGEST`` says.  The DDL is
purely additive and, with ingestion off, entirely inert: three new tables and
four indexes that nothing reads.  Running it unconditionally is what makes
turning the switch on a one-variable change rather than a migration event, which
matters because the phase-1 diagnostics have to be startable on a server that
is already up.

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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cli_agent_orchestrator.adapters.store.connection import ConnectionPool, render_timestamp
from cli_agent_orchestrator.core.findings import FindingCode
from cli_agent_orchestrator.core.ids import new_ulid

logger = logging.getLogger(__name__)

__all__ = [
    "FINDING_DDL",
    "MIGRATION_STEPS",
    "MigrationResult",
    "MigrationStatement",
    "migrate",
]

#: A migration statement is SQL, or a callable that applies one.  The callable
#: form exists for exactly one thing: ``ALTER TABLE ... ADD COLUMN`` has no
#: ``IF NOT EXISTS`` in SQLite, and this migrator runs at EVERY boot.  A bare
#: ALTER would succeed once and fail forever after — and a failed step aborts the
#: whole migration and disables ingestion for the process, so the second boot
#: would silently turn phase 1 off.  The callable checks ``PRAGMA table_info``
#: first, which is what keeps an additive column additive.
MigrationStatement = str | Callable[[sqlite3.Connection], None]

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
  idempotency_key TEXT,
  UNIQUE(terminal_id, seq))
"""


def _add_worker_event_idempotency_key(conn: sqlite3.Connection) -> None:
    """WP-ARCH phase 2, D4 — the caller-supplied idempotency key column.

    Additive and idempotent.  A database created before phase 2 has the
    fourteen-column ``worker_event``; one created after has the column already,
    because it is in the DDL above.  Both reach this function, and it must be a
    no-op for the second.

    The key is NULLABLE and the index below is PARTIAL for the same reason:
    almost no row carries one.  Only the claude_code hook route supplies a key,
    because only a hook POST can be retried by a transport this server does not
    control.  A ``NOT NULL`` column would have forced every other producer to
    invent one, and a full unique index would have paid for a column of NULLs on
    every insert — and SQLite treats NULLs as distinct in a UNIQUE index anyway,
    so the constraint would have been vacuous where it was not expensive.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(worker_event)")}
    if "idempotency_key" in columns:
        return
    conn.execute("ALTER TABLE worker_event ADD COLUMN idempotency_key TEXT")


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
#   mode                D9/B16 — the live discriminator the occupancy predicate
#                       and ``claim``'s filter both read.  Every row this build
#                       writes is ``live``; the second value went with
#                       shadow-live mode (#738) and old rows still carry it.
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
# ``mode`` is carried beyond the audit's columns: it keeps a dead-letter written
# by a build that still had the retired observational mode (#738) distinguishable
# from a live one.
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
  wake_count   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (receiver_id, epoch))
"""

# ---------------------------------------------------------------------------
# WP-ARCH Amendment A, slice 2a — the gate record (blueprint §10.2).
#
# Added to THIS migrator rather than a second one, for the reason phase 3 gave:
# a second migrator is how one schema comes to have two authorities.  Every
# statement is ``CREATE TABLE IF NOT EXISTS`` so a second boot is a no-op, and
# the DDL is additive and, with nothing in the live supervisor loop calling the
# gate service (shadow mode, §10.7), entirely inert — the tables exist and
# nothing writes them until a caller does.
#
# The column set is §10.2's, field for field.  Field names match the value
# types in ``core/gate.py`` so the adapter is a straight row<->model map, and
# the two verification hashes are SEPARATE columns (``subject_sha``,
# ``report_bytes_sha``) because AC-A8 verifies each against its own authority.
# ``owner_conversation``/``owner_epoch`` fence the supervisor conversation (P2);
# ``row_version`` is optimistic concurrency; ``generation`` is the staleness
# test.  The question tables are the ROWS only — the question primitive and its
# MCP tools are slice 2b (§10.3) — but ``claim_ownership`` (2a) rewrites the
# PENDING/ESCALATED rows, so the table and its index must exist now.
# ---------------------------------------------------------------------------

_GATE_RUN_DDL = """
CREATE TABLE IF NOT EXISTS gate_run (
  run_id              TEXT PRIMARY KEY,
  wp                  TEXT NOT NULL,
  lane                TEXT NOT NULL,
  workflow_source_sha TEXT NOT NULL DEFAULT '',
  input_sha           TEXT NOT NULL DEFAULT '',
  owner_conversation  TEXT NOT NULL,
  owner_epoch         INTEGER NOT NULL,
  max_rounds          INTEGER NOT NULL,
  row_version         INTEGER NOT NULL DEFAULT 1,
  state               TEXT NOT NULL DEFAULT 'open',
  created_at          TEXT NOT NULL)
"""

# ``build_inputs`` and ``review_snapshot`` are JSON-serialised ArtifactManifests:
# a manifest is a versioned value the round is identified by, not a set of columns
# to query on, so it is stored whole and the artifact sha is computed from it on
# read.  ``review_snapshot`` is NULL until the freeze (P1) — a change to the
# reviewed bytes after the freeze opens a SUCCESSOR round, never a re-freeze, so
# the column moves NULL -> value exactly once.
_GATE_ROUND_DDL = """
CREATE TABLE IF NOT EXISTS gate_round (
  round_id             TEXT PRIMARY KEY,
  run_id               TEXT NOT NULL,
  round_no             INTEGER NOT NULL,
  predecessor_round_id TEXT,
  build_inputs         TEXT NOT NULL,
  review_snapshot      TEXT,
  execution_target     TEXT NOT NULL,
  test_command         TEXT NOT NULL DEFAULT '',
  evidence_tier        TEXT NOT NULL DEFAULT '',
  fixture_corpus_sha   TEXT,
  fixture_frame_count  INTEGER,
  subject_sha          TEXT,
  report_bytes_sha     TEXT,
  verdict_report_sha   TEXT,
  state                TEXT NOT NULL DEFAULT 'open',
  generation           INTEGER NOT NULL DEFAULT 0,
  row_version          INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT NOT NULL,
  closed_at            TEXT,
  UNIQUE(run_id, round_no))
"""

# An independent assignment aggregate (P1): ``round_id`` is NULLABLE so a non-gate
# lane's assignment uses the same row with no synthetic round.  ``pins`` is a JSON
# array of pin refs.  A terminal runs several assignments and an assignment
# outlives an incarnation, so nothing here is keyed on a terminal id.
_GATE_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS gate_dispatch (
  dispatch_id          TEXT PRIMARY KEY,
  round_id             TEXT,
  role                 TEXT NOT NULL,
  position             TEXT NOT NULL,
  routing_revision     TEXT NOT NULL DEFAULT '',
  conversation_id      TEXT,
  terminal_incarnation TEXT,
  request_id           TEXT NOT NULL,
  effect_id            TEXT,
  pins                 TEXT NOT NULL DEFAULT '[]',
  brief_blob_sha       TEXT NOT NULL DEFAULT '',
  state                TEXT NOT NULL DEFAULT 'prepared',
  outcome              TEXT)
"""

# Intent BEFORE the operation (P1); the adapter dedups on ``effect_id`` — the
# PRIMARY KEY is the dedup.  A crash between recording the intent and the operation
# settling is repaired from this row, not re-attempted.
_GATE_EFFECT_INTENT_DDL = """
CREATE TABLE IF NOT EXISTS gate_effect_intent (
  effect_id    TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  round_id     TEXT,
  dispatch_id  TEXT,
  approval_ref TEXT,
  requested_at TEXT NOT NULL)
"""

# The result is keyed on the intent's ``effect_id`` (one result per intent).
# ``settled_at`` is NULL while the outcome is UNCERTAIN and unreconciled (R30).
_GATE_EFFECT_RESULT_DDL = """
CREATE TABLE IF NOT EXISTS gate_effect_result (
  effect_id    TEXT PRIMARY KEY,
  outcome      TEXT NOT NULL,
  evidence_ref TEXT NOT NULL DEFAULT '',
  settled_at   TEXT)
"""

# ``statement`` is immutable once raised; identity travels round to round (AC-A2).
# A finding is OPEN until a disposition row names its killer.
_GATE_OPEN_FINDING_DDL = """
CREATE TABLE IF NOT EXISTS gate_open_finding (
  finding_id      TEXT PRIMARY KEY,
  raised_in_round TEXT NOT NULL,
  run_id          TEXT NOT NULL,
  severity        TEXT NOT NULL,
  statement       TEXT NOT NULL,
  created_at      TEXT NOT NULL)
"""

# Appended, never overwritten (P7): FIXED carries killer test/mutant AT the
# reviewed revision, WITHDRAWN an actor and reason.  ``seq`` orders the appends
# for one finding.  The service validates the evidence before a row lands here.
_GATE_DISPOSITION_DDL = """
CREATE TABLE IF NOT EXISTS gate_disposition (
  finding_id            TEXT NOT NULL,
  seq                   INTEGER NOT NULL,
  kind                  TEXT NOT NULL,
  reviewed_artifact_sha TEXT NOT NULL DEFAULT '',
  killer_test           TEXT,
  killer_mutant         TEXT,
  actor                 TEXT,
  reason                TEXT,
  at                    TEXT NOT NULL,
  PRIMARY KEY (finding_id, seq))
"""

# Revision-bound consumer coverage (AC-A9): ``consumers`` and ``unresolved_dynamic``
# are JSON arrays.  Bound to a round AND to the reviewed artifact sha, so it is not
# a standalone completeness claim.  One coverage per round.
_GATE_CONSUMER_COVERAGE_DDL = """
CREATE TABLE IF NOT EXISTS gate_consumer_coverage (
  round_id              TEXT PRIMARY KEY,
  xref_sha              TEXT NOT NULL,
  reviewed_artifact_sha TEXT NOT NULL,
  consumers             TEXT NOT NULL DEFAULT '[]',
  unresolved_dynamic    TEXT NOT NULL DEFAULT '[]')
"""

# ``dispatch_prior_state`` (B1 r2) is in BOTH this DDL and ``ADDITIVE_COLUMNS``,
# which is the pair this file's own note at the additive block describes and the
# ``seat_digest.wake_count`` precedent uses: the CREATE reaches a FRESH install,
# the ALTER reaches a deployment whose table already exists and for which
# ``CREATE TABLE IF NOT EXISTS`` is a no-op.  Either alone covers half the
# estate.
#
# The question store (§10.2, P2) — ROWS ONLY in 2a.  The question primitive
# (``ask_supervisor``) and its wait adapters are 2b; ``claim_ownership`` (2a)
# rewrites the PENDING/ESCALATED rows, so the table and the partial unique index
# that enforces "one open question per dispatch" must exist now.
_ROUND_QUESTION_DDL = """
CREATE TABLE IF NOT EXISTS round_question (
  question_id        TEXT PRIMARY KEY,
  dispatch_id        TEXT NOT NULL,
  round_id           TEXT,
  client_request_id  TEXT NOT NULL,
  owner_conversation TEXT NOT NULL,
  owner_epoch        INTEGER NOT NULL,
  continuation_kind  TEXT NOT NULL,
  continuation_ref   TEXT NOT NULL,
  asked_at           TEXT NOT NULL,
  expires_at         TEXT NOT NULL,
  question           TEXT NOT NULL,
  options_json       TEXT NOT NULL DEFAULT '[]',
  answer_schema      TEXT,
  default_policy     TEXT,
  blocking           INTEGER NOT NULL,
  state              TEXT NOT NULL CHECK (state IN ('PENDING','ESCALATED','ANSWERED','EXPIRED')),
  answer_event_id    TEXT,
  consumed_at        TEXT,
  user_prompt_id     TEXT,
  dispatch_prior_state TEXT NOT NULL DEFAULT '',
  row_version        INTEGER NOT NULL DEFAULT 1)
"""

# Append-only, one row per submitted answer; retries return the recorded row.
_QUESTION_ANSWER_DDL = """
CREATE TABLE IF NOT EXISTS question_answer (
  answer_event_id   TEXT PRIMARY KEY,
  question_id       TEXT NOT NULL,
  answer            TEXT NOT NULL,
  answered_by       TEXT NOT NULL,
  answered_at       TEXT NOT NULL,
  client_request_id TEXT NOT NULL)
"""

# Committed WITH the answer; consumption is a separate record, so ANSWERED is not
# proof of receipt (P2).
_ANSWER_DELIVERY_INTENT_DDL = """
CREATE TABLE IF NOT EXISTS answer_delivery_intent (
  answer_event_id TEXT PRIMARY KEY,
  state           TEXT NOT NULL CHECK (state IN ('PENDING','SENT','CONSUMED','FAILED')),
  settled_at      TEXT)
"""

# The notification intent, committed IN the ask transaction (slice B1; §10.2 P2).
#
# §10.2 requires the notice to commit with the question and the blueprint names
# no table for it, so this is B1's addition, recorded as a build-time amendment
# rather than smuggled.  Without it there is an instant in which a lane is
# suspended and the only record of the obligation to tell anybody is in the
# memory of the process that just committed — exactly the crash window the
# effect-intent pattern exists to close, one table further down.
#
# ``attempts`` and ``last_error`` are what make a FAILED notice retryable by a
# sweep instead of a row that merely records that something went wrong once.
_QUESTION_NOTICE_INTENT_DDL = """
CREATE TABLE IF NOT EXISTS question_notice_intent (
  question_id TEXT PRIMARY KEY,
  state       TEXT NOT NULL CHECK (state IN ('PENDING','SENT','FAILED')),
  msg_id      TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  settled_at  TEXT,
  claim_token TEXT NOT NULL DEFAULT '',
  claim_until TEXT NOT NULL DEFAULT '')
"""

# Append-only; one row per ACCEPTED claim (DESIGN r2 non-blocking 1).  Identical
# retries by ``client_request_id`` return this row.
_OWNERSHIP_TRANSFER_DDL = """
CREATE TABLE IF NOT EXISTS ownership_transfer (
  transfer_id         TEXT PRIMARY KEY,
  prior_conversation  TEXT NOT NULL,
  prior_epoch         INTEGER NOT NULL,
  new_conversation    TEXT NOT NULL,
  new_epoch           INTEGER NOT NULL,
  run_id              TEXT,
  runs_rewritten      INTEGER NOT NULL,
  questions_rewritten INTEGER NOT NULL,
  claimed_by          TEXT NOT NULL,
  claimed_at          TEXT NOT NULL,
  client_request_id   TEXT NOT NULL UNIQUE)
"""

# ---------------------------------------------------------------------------
# Additive columns, applied idempotently AFTER the create steps.
#
# ``CREATE TABLE IF NOT EXISTS`` is a no-op against a table 3a already created,
# so a column added to a DDL above reaches a fresh install and NOT a deployment
# that has been running the queue since 3a.  ``ALTER TABLE … ADD COLUMN`` is the
# other half, and it is not idempotent — a second boot raises "duplicate column
# name", which under this migrator's all-or-nothing step loop would fail the
# whole migration on every boot and take the delivery queue down with it.  So
# each entry is guarded by ``PRAGMA table_info`` and skipped when the column is
# already there.
#
# ``(table, column, definition)``.  Definitions must carry a DEFAULT: SQLite
# fills existing rows with it, and a NOT NULL column without one is refused.
# ---------------------------------------------------------------------------
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # A1: the wake ordinal.  Lease periods in which this epoch was woken — the
    # durable half of I3's enforcement, the transport's content window being the
    # half a server bounce clears.
    ("seat_digest", "wake_count", "INTEGER NOT NULL DEFAULT 0"),
    # B1 r2: the dispatch state an ask SUSPENDED, so releasing the question can
    # restore it instead of fabricating one.  Without it, a PREPARED dispatch
    # that asked a question came back DISPATCHED — a state it had never been in
    # — because the release paths hardcoded a value.
    ("round_question", "dispatch_prior_state", "TEXT NOT NULL DEFAULT ''"),
    # B2 r2: one durable claim excludes concurrent sweeps; the stable queue key
    # covers a worker crash after enqueue and before the CAS settlement.
    ("question_notice_intent", "claim_token", "TEXT NOT NULL DEFAULT ''"),
    ("question_notice_intent", "claim_until", "TEXT NOT NULL DEFAULT ''"),
)

# Ordered migration steps AFTER the finding table.  A tuple of (name, statements)
# so a test can substitute a failing step and watch boot survive it.


def _retire_shadow_delivery_rows(conn: sqlite3.Connection) -> None:
    """Delete delivery rows written in the retired ``shadow`` mode (#738 / F883).

    F883 shrank :class:`~cli_agent_orchestrator.core.delivery.QueueMode` to one
    member and left the on-disk rows to the ``mode='live'`` conjunct in ``claim``.
    That conjunct is NOT in ``reclaim``, the time-bound sweep or the digest
    reads, so the first tick after the flip to ``on`` hit a shadow row, raised
    ``ValueError: 'shadow' is not a valid QueueMode`` and every tick after it
    did the same (laptop, 2026-09-11: 1,162 ready + 440 delivered shadow rows,
    delivery dead on arrival).  Shadow rows were observational copies of
    messages the legacy inbox owned and delivered, so deleting them loses
    nothing; a finding records the counts so the deletion is auditable.
    Idempotent: a second boot finds nothing and writes nothing.
    """
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE mode != 'live'").fetchone()[
            "n"
        ]
        for table in ("delivery_msg", "delivery_dead")
    }
    if not any(counts.values()):
        return
    attempts = conn.execute(
        "DELETE FROM delivery_attempt WHERE msg_id IN "
        "(SELECT msg_id FROM delivery_msg WHERE mode != 'live' "
        "UNION SELECT msg_id FROM delivery_dead WHERE mode != 'live')"
    ).rowcount
    conn.execute("DELETE FROM delivery_msg WHERE mode != 'live'")
    conn.execute("DELETE FROM delivery_dead WHERE mode != 'live'")
    counts["delivery_attempt"] = attempts
    detail = " ".join(f"{k}={v}" for k, v in counts.items())
    now = render_timestamp(datetime.now(UTC))
    conn.execute(
        "INSERT INTO finding (finding_id, code, terminal_id, dedupe_key, detail, "
        "sample_event_id, count, first_seen_at, last_seen_at, state) "
        "VALUES (?, ?, '', ?, ?, NULL, 1, ?, ?, 'open')",
        (new_ulid(), FindingCode.DIAG_SHADOW_ROWS_RETIRED.value, now, detail, now, now),
    )
    logger.warning("delivery: retired shadow-mode rows (#738): %s", detail)


MIGRATION_STEPS: tuple[tuple[str, tuple[MigrationStatement, ...]], ...] = (
    ("worker_event", (_WORKER_EVENT_DDL,)),
    ("worker_event_seq", (_WORKER_EVENT_SEQ_DDL,)),
    # WP-ARCH phase 2, D4 / R1: the idempotency key goes into THIS migrator.  A
    # second migrator is how one schema comes to have two authorities, and both
    # phase 2 and phase 3 commit to not adding one.
    (
        "worker_event_idempotency_key",
        (
            _add_worker_event_idempotency_key,
            # The partial unique index is what makes the retried hook POST append
            # once.  Stripe's shape: "a client generates an idempotency key,
            # which is a unique key that the server uses to recognize subsequent
            # retries of the same request".  One divergence is deliberate — Stripe
            # replays a cached response and the duplicate leaves no row, whereas
            # here a duplicate OBSERVATION is data, and the ``producer`` column
            # exists to record that two sources saw one turn.  What must not
            # double is one producer's own retry, which is what this indexes.
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_worker_event_idempotency "
            "ON worker_event(idempotency_key) WHERE idempotency_key IS NOT NULL",
        ),
    ),
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
            # The legacy-id join key.  A row is looked up by its legacy inbox id
            # (the write-through's surrogate), so without this the lookup is a
            # full scan of the queue.  Partial, because not every row carries one.
            "CREATE INDEX IF NOT EXISTS ix_delivery_legacy "
            "ON delivery_msg(legacy_message_id) WHERE legacy_message_id IS NOT NULL",
            # Retention scans terminal rows by when they ended.
            "CREATE INDEX IF NOT EXISTS ix_delivery_terminated "
            "ON delivery_msg(terminated_at) WHERE terminated_at IS NOT NULL",
        ),
    ),
    ("delivery_attempt", (_DELIVERY_ATTEMPT_DDL,)),
    ("delivery_dead", (_DELIVERY_DEAD_DDL,)),
    # After both tables exist: rows from the retired shadow mode (#738) leave.
    ("delivery_retire_shadow_rows", (_retire_shadow_delivery_rows,)),
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
    # -- WP-ARCH Amendment A, slice 2a: the gate record (§10.2) ------------
    ("gate_run", (_GATE_RUN_DDL,)),
    ("gate_round", (_GATE_ROUND_DDL,)),
    (
        "gate_round_indexes",
        (
            # ``rounds_for_run`` and ``cao gate show`` list a run's rounds in
            # order; the round projection joins on run_id.
            "CREATE INDEX IF NOT EXISTS ix_gate_round_run "
            "ON gate_round(run_id, round_no)",
        ),
    ),
    ("gate_dispatch", (_GATE_DISPATCH_DDL,)),
    (
        "gate_dispatch_indexes",
        (
            # The round projection joins dispatches by round; partial because a
            # non-gate dispatch carries no round.
            "CREATE INDEX IF NOT EXISTS ix_gate_dispatch_round "
            "ON gate_dispatch(round_id) WHERE round_id IS NOT NULL",
        ),
    ),
    ("gate_effect_intent", (_GATE_EFFECT_INTENT_DDL,)),
    (
        "gate_effect_intent_indexes",
        (
            "CREATE INDEX IF NOT EXISTS ix_gate_effect_intent_round "
            "ON gate_effect_intent(round_id) WHERE round_id IS NOT NULL",
        ),
    ),
    ("gate_effect_result", (_GATE_EFFECT_RESULT_DDL,)),
    ("gate_open_finding", (_GATE_OPEN_FINDING_DDL,)),
    (
        "gate_open_finding_indexes",
        (
            # ``open_findings_for_run`` scans a run's findings to carry the still
            # open ones into the next brief (AC-A2).
            "CREATE INDEX IF NOT EXISTS ix_gate_open_finding_run "
            "ON gate_open_finding(run_id)",
        ),
    ),
    ("gate_disposition", (_GATE_DISPOSITION_DDL,)),
    ("gate_consumer_coverage", (_GATE_CONSUMER_COVERAGE_DDL,)),
    ("round_question", (_ROUND_QUESTION_DDL,)),
    (
        "round_question_indexes",
        (
            # AC-A10's "one open question per dispatch": ESCALATED still blocks and
            # holds the open slot, so both states are in the partial index.
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_question_open "
            "ON round_question(dispatch_id) WHERE state IN ('PENDING','ESCALATED')",
            # ``claim_ownership`` (2a) rewrites PENDING/ESCALATED rows by owner
            # conversation; without this the rewrite scans the whole table.
            "CREATE INDEX IF NOT EXISTS ix_question_owner "
            "ON round_question(owner_conversation) "
            "WHERE state IN ('PENDING','ESCALATED')",
        ),
    ),
    ("question_answer", (_QUESTION_ANSWER_DDL,)),
    ("answer_delivery_intent", (_ANSWER_DELIVERY_INTENT_DDL,)),
    ("ownership_transfer", (_OWNERSHIP_TRANSFER_DDL,)),
    (
        # Slice B1.  An ADDITIVE step appended to the list, never an edit of an
        # existing DDL string: a deployment that has already applied the steps
        # above gets this one and nothing else, which is the whole reason this
        # file's rule at the top of the step list exists.
        "question_notice_intent",
        (
            _QUESTION_NOTICE_INTENT_DDL,
            # The expiry sweep scans open questions by deadline every 30s (B2).
            # Partial on the two OPEN states so the index stays the size of the
            # working set rather than of all history.
            "CREATE INDEX IF NOT EXISTS ix_question_expiry "
            "ON round_question(state, expires_at) "
            "WHERE state IN ('PENDING','ESCALATED')",
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


def _pending_additive_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The ``ALTER TABLE`` statements this database still needs, in order.

    Read before the step runs rather than tolerated inside it: a step that
    swallowed "duplicate column name" would also swallow a genuine ALTER
    failure, and the migrator's whole contract is that a failed step is
    RECORDED.  A table that does not exist yet returns no rows from
    ``PRAGMA table_info`` and is skipped, because its CREATE step above already
    carries the column.
    """
    pending: list[str] = []
    for table, column, definition in ADDITIVE_COLUMNS:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:  # pragma: no cover — a pragma that cannot run
            continue
        if not rows:
            continue
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            pending.append(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return tuple(pending)


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

    steps: list[tuple[str, tuple[MigrationStatement, ...]]] = list(MIGRATION_STEPS)
    additive = _pending_additive_columns(conn)
    if additive:
        steps.append(("additive_columns", additive))

    applied: list[str] = ["finding"]
    for name, statements in steps:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                if callable(statement):
                    statement(conn)
                else:
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

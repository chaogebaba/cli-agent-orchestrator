"""AC3/AC5 — the phase-1 migrator (WP-ARCH, F725 #581).

Two things are being pinned: that the DDL is EXACTLY the audit §3.1 statement,
and that a migration failure cannot stop the server booting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store import migrator as migrator_module
from cli_agent_orchestrator.adapters.store.migrator import migrate

from .conftest import TEST_BUSY_TIMEOUT_MS

pytestmark = pytest.mark.integration

# The audit §3.1 DDL, column for column, transcribed independently of the module.
_AUDIT_WORKER_EVENT_COLUMNS = [
    "event_id",
    "terminal_id",
    "seq",
    "kind",
    "producer",
    "confidence",
    "observed_at",
    "ingested_at",
    "payload",
    "source_ref",
    "run_id",
    "msg_id",
    "decision",
    "evidence",
]


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_worker_event_columns_match_the_audit_ddl(db_path: Path) -> None:
    """All fourteen columns, in the audit's order — run_id/msg_id/decision/evidence included."""
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok
    assert pool is not None
    assert _columns(pool.connection(), "worker_event") == _AUDIT_WORKER_EVENT_COLUMNS
    pool.close_all()


def test_worker_event_seq_shape(db_path: Path) -> None:
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok
    assert pool is not None
    assert _columns(pool.connection(), "worker_event_seq") == ["terminal_id", "high_water"]
    pool.close_all()


def test_projection_table_has_every_ac6_column(db_path: Path) -> None:
    """``worker_state_shadow`` is created here so phase 1 has ONE schema owner.

    Lane C's projector writes it; the DDL lives with the rest of the phase-1
    tables rather than in a second migrator.
    """
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok
    assert pool is not None
    assert _columns(pool.connection(), "worker_state_shadow") == [
        "terminal_id",
        "state",
        "since",
        "last_event_seq",
        "degraded_reason",
        "prior_state",
        "last_probe_at",
        "last_source_probe_at",
        "pane_pid",
        "pane_present",
        "miss_count",
    ]
    pool.close_all()


def test_unique_and_partial_indexes_exist(db_path: Path) -> None:
    """The UNIQUE constraint and the two PARTIAL indexes the audit specifies."""
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok
    assert pool is not None
    conn = pool.connection()

    rows = {
        row["name"]: row["sql"]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'worker_event'"
        )
    }
    assert "ix_worker_event_scan" in rows
    assert "WHERE run_id IS NOT NULL" in rows["ix_worker_event_run"]
    assert "WHERE msg_id IS NOT NULL" in rows["ix_worker_event_msg"]

    # UNIQUE(terminal_id, seq) is enforced, not merely declared.
    conn.execute(
        "INSERT INTO worker_event (event_id, terminal_id, seq, kind, producer, confidence, "
        "observed_at, ingested_at, payload) VALUES ('a', 't', 1, 'k', 'p', 'c', 'o', 'i', '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO worker_event (event_id, terminal_id, seq, kind, producer, confidence, "
            "observed_at, ingested_at, payload) VALUES ('b', 't', 1, 'k', 'p', 'c', 'o', 'i', '{}')"
        )
    pool.close_all()


def test_wal_and_busy_timeout_are_set(db_path: Path) -> None:
    """27 of the fork's 30 raw connect sites set no busy timeout (r9). This one does."""
    result, pool = migrate(db_path, busy_timeout_ms=7777)
    assert result.ok
    assert pool is not None
    conn = pool.connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 7777
    pool.close_all()


def test_migration_is_idempotent(db_path: Path) -> None:
    """Runs at every boot, so a second run must be a no-op rather than an error."""
    first, pool_a = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert first.ok
    assert pool_a is not None
    pool_a.close_all()

    second, pool_b = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert second.ok
    assert second.steps_applied == first.steps_applied
    assert pool_b is not None
    pool_b.close_all()


def test_finding_table_is_created_first(db_path: Path) -> None:
    """AC5/N6: the table that records a migration failure precedes anything that can fail."""
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok
    assert result.steps_applied[0] == "finding"
    assert pool is not None
    pool.close_all()


def test_migration_failure_does_not_raise_and_records_a_finding(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later step raising leaves the server bootable, with the failure recorded.

    This is the AC5 promise made testable: the migrator returns ``ok=False``, the
    ``finding`` table (created first) holds one ``DIAG-MIGRATION-FAILED`` row, and
    nothing propagates to the caller.
    """
    monkeypatch.setattr(
        migrator_module,
        "MIGRATION_STEPS",
        (("worker_event", ("CREATE TABLE definitely not valid sql (",)),),
    )
    result, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)

    assert result.ok is False
    assert result.failed_step == "worker_event"
    assert result.finding_table_ready is True
    assert result.error is not None
    assert pool is not None

    rows = list(pool.connection().execute("SELECT code, dedupe_key, count FROM finding"))
    assert [(row["code"], row["dedupe_key"], row["count"]) for row in rows] == [
        ("DIAG-MIGRATION-FAILED", "worker_event", 1)
    ]
    pool.close_all()


def test_repeated_migration_failure_increments_rather_than_multiplies(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that restart-loops must not produce one finding row per boot."""
    monkeypatch.setattr(
        migrator_module,
        "MIGRATION_STEPS",
        (("worker_event", ("CREATE TABLE definitely not valid sql (",)),),
    )
    for _ in range(3):
        _, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
        assert pool is not None
        pool.close_all()

    _, pool = migrate(db_path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    row = pool.connection().execute("SELECT count FROM finding").fetchone()
    assert row["count"] == 4
    pool.close_all()


def test_unopenable_database_is_reported_not_raised(tmp_path: Path) -> None:
    """A path that cannot be opened at all still returns rather than raising."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    result, pool = migrate(
        blocker / "nested" / "worker-truth.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS
    )
    assert result.ok is False
    assert result.failed_step == "connect"
    assert pool is None

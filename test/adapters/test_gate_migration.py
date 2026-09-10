"""Gate-table migration is additive, idempotent and boot-safe (WP-ARCH A, 2a).

The 2a migration proof: the gate tables are created by the one migrator that runs
at every boot (up), a second boot is a no-op (idempotent), a database created
WITHOUT the gate tables gains them on the next migrate (the additive "up" over an
older schema), dropping them and re-migrating restores them (the "down"/re-up a
temp-DB-copy proof needs), and a failure at a gate step is RECORDED as a finding
and does not block boot — the migrator's whole contract.
"""

from __future__ import annotations

from pathlib import Path

from cli_agent_orchestrator.adapters.store import migrator as m
from cli_agent_orchestrator.adapters.store.migrator import migrate

TEST_BUSY_TIMEOUT_MS = 5000

_GATE_TABLES = {
    "gate_run",
    "gate_round",
    "gate_dispatch",
    "gate_effect_intent",
    "gate_effect_result",
    "gate_open_finding",
    "gate_disposition",
    "gate_consumer_coverage",
    "round_question",
    "question_answer",
    "answer_delivery_intent",
    "ownership_transfer",
}


def _tables(pool: object) -> set[str]:
    conn = pool.connection()  # type: ignore[attr-defined]
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_up_creates_gate_tables(tmp_path: Path) -> None:
    result, pool = migrate(tmp_path / "g.db", busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert result.ok and pool is not None
    assert _GATE_TABLES <= _tables(pool)


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "g.db"
    r1, p1 = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert r1.ok and p1 is not None
    p1.close_all()
    r2, p2 = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert r2.ok and p2 is not None
    assert _GATE_TABLES <= _tables(p2)


def test_migration_up_over_older_schema(tmp_path: Path) -> None:
    # Simulate a database created before the gate step: migrate with the gate
    # steps stripped, then migrate again with the full set. The gate tables must
    # appear on the second pass (additive up over an older schema).
    path = tmp_path / "g.db"
    full_steps = m.MIGRATION_STEPS
    try:
        m.MIGRATION_STEPS = tuple(  # type: ignore[misc]
            step
            for step in full_steps
            if not step[0].startswith("gate_")
            and step[0]
            not in {
                "round_question",
                "question_answer",
                "answer_delivery_intent",
                "ownership_transfer",
                "round_question_indexes",
            }
        )
        r1, p1 = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
        assert r1.ok and p1 is not None
        assert not (_GATE_TABLES & _tables(p1))  # no gate tables yet
        p1.close_all()
    finally:
        m.MIGRATION_STEPS = full_steps  # type: ignore[misc]
    r2, p2 = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert r2.ok and p2 is not None
    assert _GATE_TABLES <= _tables(p2)  # additive up


def test_migration_down_and_reup(tmp_path: Path) -> None:
    # "Down" for additive DDL = drop the tables; re-migrate restores them. This is
    # the temp-DB-copy up/down proof the brief asks for.
    path = tmp_path / "g.db"
    _r, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert pool is not None
    conn = pool.connection()
    for table in _GATE_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    assert not (_GATE_TABLES & _tables(pool))
    pool.close_all()
    r2, p2 = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    assert r2.ok and p2 is not None
    assert _GATE_TABLES <= _tables(p2)


def test_gate_step_failure_records_finding_and_survives_boot(tmp_path: Path) -> None:
    # A failure at a gate step must be recorded as DIAG-MIGRATION-FAILED and NOT
    # block boot (the migrator's contract). Inject a broken gate step.
    path = tmp_path / "g.db"
    full_steps = m.MIGRATION_STEPS
    try:
        m.MIGRATION_STEPS = full_steps + (  # type: ignore[misc]
            ("gate_broken", ("CREATE TABLE gate_broken (bad syntax here",)),
        )
        result, pool = migrate(path, busy_timeout_ms=TEST_BUSY_TIMEOUT_MS)
    finally:
        m.MIGRATION_STEPS = full_steps  # type: ignore[misc]
    assert not result.ok
    assert result.failed_step == "gate_broken"
    assert result.finding_table_ready  # the failure was recordable
    assert pool is not None  # boot survived: a pool came back
    rows = (
        pool.connection()
        .execute("SELECT code, dedupe_key FROM finding WHERE code = 'DIAG-MIGRATION-FAILED'")
        .fetchall()
    )
    assert any(row[1] == "gate_broken" for row in rows)

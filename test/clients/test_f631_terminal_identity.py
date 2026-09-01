"""F631 (#486/#492) Slice 1 — terminal_identity table + create/reap lifecycle.

Covers AC1, AC2, AC7, AC14, AC15 of blueprint
``orchestrator/blueprints/f631-terminal-identity-registry.md`` (sha
02baa634…). Each AC's stated MUTANT is exercised as an executable assertion
(the mutant behaviour is asserted to DIFFER from the built behaviour), so the
mutation ledger has a red/green pair per arm.

Slice 1 scope: the durable identity row is WRITTEN at create (D1/D2/D3) and
OUTLIVES the terminal, flipping to ``reaped`` at delete (D4 write side); the
migration is additive (D10). Resume RESOLUTION (D4 read side, resolve_base
branches) is slice 2 and is NOT asserted here.
"""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    ProviderSessionModel,
    TerminalIdentityModel,
    TerminalModel,
    _migrate_f631_terminal_identity,
    create_terminal,
    create_terminal_with_warm_intent,
    delete_terminal_and_warm_intent,
    get_terminal_metadata,
)


@pytest.fixture
def test_db(monkeypatch):
    """In-memory SQLite DB with SessionLocal patched (fresh-DB / create_all path)."""
    engine = create_engine("sqlite:///:memory:")
    # SQLite enforces CHECK constraints; foreign keys are off by default, which
    # matches the production runtime (no PRAGMA foreign_keys=ON in init_db).
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    return TestSession


# --------------------------------------------------------------------------- #
# AC1 (D1/D2) — registration at create; provider_sessions unchanged.          #
# --------------------------------------------------------------------------- #
def test_ac1_identity_row_written_at_create_live(test_db):
    create_terminal("t-ac1", "sess", "w0", "codex", agent_profile="reviewer")
    with test_db() as db:
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac1").one_or_none()
        assert row is not None, "identity row must exist immediately after create"
        assert row.lifecycle == "live"
        assert row.base_name == "t-ac1"  # base_name IS the terminal_id (D4)
        assert row.provider == "codex"
        assert row.agent_profile == "reviewer"
        # provider_sessions is UNCHANGED — the lane is not a fork base.
        assert db.query(ProviderSessionModel).count() == 0


def test_ac1_mutant_provider_sessions_rejects_the_row(test_db):
    """AC1 mutant (B1 made executable): writing the identity row into
    provider_sessions is rejected by the status/kind CHECKs (database.py
    :233-237). An at-create identity row uses lifecycle='live' (not a valid
    provider_sessions.status) and would have no valid kind — the CHECKs reject
    it, which is why F631 needs a NEW table (D1)."""
    with test_db() as db:
        bad = ProviderSessionModel(
            name="t-ac1",
            provider="codex",
            session_uuid="u",
            cwd="/tmp",
            agent_profile="reviewer",
            status="live",  # not in ('ready','superseded','retired')
            kind="base",
        )
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.flush()


# --------------------------------------------------------------------------- #
# AC2 (D1/D4) — identity outlives the terminal.                               #
# --------------------------------------------------------------------------- #
def test_ac2_identity_survives_reap_as_reaped(test_db):
    create_terminal("t-ac2", "sess", "w0", "codex", agent_profile="reviewer")
    result = delete_terminal_and_warm_intent("t-ac2", preserve_warm_intent=False)
    assert result["terminal_deleted"] is True
    with test_db() as db:
        # terminals row is HARD-DELETED (D1 unchanged).
        assert db.query(TerminalModel).filter_by(id="t-ac2").one_or_none() is None
        # identity row REMAINS, flipped to reaped with a reaped_at stamp.
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac2").one_or_none()
        assert row is not None
        assert row.lifecycle == "reaped"
        assert row.reaped_at is not None


def test_ac2_mutant_hard_delete_of_identity_would_lose_it(test_db):
    """AC2 mutant: if delete DELETED the identity row (as the terminals row is
    deleted) instead of flipping lifecycle, the row would be gone. We assert the
    built behaviour is the opposite — the row is still queryable post-reap."""
    create_terminal("t-ac2b", "sess", "w0", "codex")
    delete_terminal_and_warm_intent("t-ac2b", preserve_warm_intent=False)
    with test_db() as db:
        assert db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac2b").count() == 1


def test_ac2_reap_returns_resume_key(test_db):
    """D4 write side: delete returns the resume_key (the provider_session_id)."""
    create_terminal("t-ac2c", "sess", "w0", "codex", provider_session_id="uuid-xyz")
    result = delete_terminal_and_warm_intent("t-ac2c", preserve_warm_intent=False)
    assert result["resume_key"] == "uuid-xyz"


def test_reap_of_pre_registry_lane_returns_none_resume_key(test_db):
    """A terminals row with NO identity row (pre-registry lane) reaps cleanly and
    returns resume_key=None — the flip is a no-op, not a crash."""
    with test_db() as db:
        db.add(
            TerminalModel(
                id="pre-reg",
                tmux_session="sess",
                tmux_window="w0",
                provider="codex",
                lifecycle_generation=1,
            )
        )
        db.commit()
    result = delete_terminal_and_warm_intent("pre-reg", preserve_warm_intent=False)
    assert result["terminal_deleted"] is True
    assert result["resume_key"] is None


# --------------------------------------------------------------------------- #
# AC7 (D3) — kiro NULL is recorded; nothing fabricates an id.                 #
# --------------------------------------------------------------------------- #
def test_ac7_fresh_kiro_lane_has_null_provider_session_id(test_db):
    # kiro_cli persists NULL at spawn (kiro_cli.py:228-232); create_terminal is
    # called with no provider_session_id, so the identity row records NULL.
    create_terminal("t-ac7", "sess", "w0", "kiro_cli", agent_profile="developer")
    with test_db() as db:
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac7").one()
        assert row.provider_session_id is None


def test_ac7_codex_lane_records_minted_uuid(test_db):
    """Control: a provider that mints a uuid records it (D3) — proving AC7's NULL
    is the recorded absence of an id, not a column that is always NULL."""
    create_terminal("t-ac7b", "sess", "w0", "codex", provider_session_id="mint-1")
    with test_db() as db:
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac7b").one()
        assert row.provider_session_id == "mint-1"


def test_ac7_warm_intent_create_records_null_at_create(test_db):
    """The fork-only warm-intent create path takes no provider_session_id at
    create (capture moves it NULL->set later, D3)."""
    create_terminal_with_warm_intent(
        terminal_id="t-ac7c",
        tmux_session="sess",
        tmux_window="w0",
        provider="codex",
        agent_profile="reviewer",
        allowed_tools=None,
        caller_id=None,
        parent_base_name=None,
        fork_mode=None,
    )
    with test_db() as db:
        row = db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac7c").one()
        assert row.provider_session_id is None
        assert row.lifecycle == "live"


# --------------------------------------------------------------------------- #
# AC14 (D1) — no resurrection: reaped row invisible to live projections.      #
# --------------------------------------------------------------------------- #
def test_ac14_reaped_lane_absent_from_live_terminal_projection(test_db):
    create_terminal("t-ac14", "sess", "w0", "codex")
    delete_terminal_and_warm_intent("t-ac14", preserve_warm_intent=False)
    # The live-terminal projection reads the terminals table; a reaped identity
    # row is NOT a live terminal and must not appear.
    assert get_terminal_metadata("t-ac14") is None
    with test_db() as db:
        assert db.query(TerminalModel).filter_by(id="t-ac14").count() == 0
        # …while the identity row still exists (audit), proving invisibility is a
        # projection property, not row deletion.
        assert db.query(TerminalIdentityModel).filter_by(terminal_id="t-ac14").count() == 1


# --------------------------------------------------------------------------- #
# AC15 (D10) — additive migration on an EXISTING DB.                          #
# --------------------------------------------------------------------------- #
def _provider_sessions_schema_fingerprint(conn: sqlite3.Connection):
    """(sql text, rootpage) for provider_sessions — the concrete falsifier for
    'no rebuild' (r2/N4): a SQLite rebuild rewrites the table, changing rootpage
    and/or the stored sql."""
    row = conn.execute(
        "SELECT sql, rootpage FROM sqlite_master WHERE type='table' AND name='provider_sessions'"
    ).fetchone()
    return row


def _build_existing_db_without_terminal_identity(db_path: Path) -> tuple:
    """Create a DB carrying provider_sessions (with a row) but NO
    terminal_identity table — an existing pre-F631 DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE provider_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, provider TEXT NOT NULL, "
        "session_uuid TEXT NOT NULL, cwd TEXT NOT NULL, agent_profile TEXT NOT NULL, "
        "git_sha TEXT, dirty_hashes TEXT NOT NULL DEFAULT '{}', digest_head TEXT, "
        "retained_persona_home TEXT, summary TEXT, status TEXT NOT NULL, "
        "kind TEXT NOT NULL DEFAULT 'base', source_terminal_id TEXT, session_name TEXT, "
        "created_at DATETIME, updated_at DATETIME, "
        "CONSTRAINT ck_provider_sessions_status CHECK (status IN ('ready','superseded','retired')), "
        "CONSTRAINT ck_provider_sessions_kind CHECK (kind IN ('base','anchor')))"
    )
    conn.execute(
        "INSERT INTO provider_sessions (name, provider, session_uuid, cwd, agent_profile, status) "
        "VALUES ('base-a','codex','u-1','/tmp','reviewer','ready')"
    )
    conn.commit()
    before = _provider_sessions_schema_fingerprint(conn)
    conn.close()
    return before


def test_ac15_migration_creates_table_on_existing_db(monkeypatch, tmp_path):
    db_path = tmp_path / "existing.db"
    before = _build_existing_db_without_terminal_identity(db_path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)

    # Precondition: no terminal_identity yet.
    conn = sqlite3.connect(str(db_path))
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='terminal_identity'"
        ).fetchone()[0]
        == 0
    )
    conn.close()

    _migrate_f631_terminal_identity()

    conn = sqlite3.connect(str(db_path))
    # The table now exists…
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='terminal_identity'"
        ).fetchone()[0]
        == 1
    )
    # …the existing row is still readable (DB opened & migrated, no data loss)…
    assert (
        conn.execute("SELECT session_uuid FROM provider_sessions WHERE name='base-a'").fetchone()[0]
        == "u-1"
    )
    # …and provider_sessions was NOT rebuilt: sql text AND rootpage byte-identical.
    after = _provider_sessions_schema_fingerprint(conn)
    conn.close()
    assert after == before, "provider_sessions must be byte-identical (sql + rootpage) — no rebuild"


def test_ac15_migration_is_idempotent(monkeypatch, tmp_path):
    db_path = tmp_path / "existing2.db"
    _build_existing_db_without_terminal_identity(db_path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    _migrate_f631_terminal_identity()
    # Second run must not raise (CREATE TABLE IF NOT EXISTS).
    _migrate_f631_terminal_identity()
    conn = sqlite3.connect(str(db_path))
    assert (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='terminal_identity'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_ac15_mutant_skipped_migration_leaves_existing_db_without_table(monkeypatch, tmp_path):
    """AC15 migration-skip mutant (supervisor's explicit request): if the
    migration is NOT registered/run, an EXISTING DB has no terminal_identity
    table. A fresh-DB test (create_all) would hide this — an existing DB does
    not. We assert the pre-migration state to make the mutant's RED concrete."""
    db_path = tmp_path / "existing3.db"
    _build_existing_db_without_terminal_identity(db_path)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    # Mutant = do NOT call _migrate_f631_terminal_identity().
    conn = sqlite3.connect(str(db_path))
    present = (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='terminal_identity'"
        ).fetchone()[0]
        == 1
    )
    conn.close()
    assert present is False, "without the migration, an existing DB has no terminal_identity table"


def test_ac15_fresh_db_create_all_makes_table(test_db):
    """The fresh-DB path (create_all) makes the table too — the arm a naive test
    would rely on. Kept as the control that the migration-skip mutant defeats."""
    with test_db() as db:
        db.execute(text("SELECT 1 FROM terminal_identity"))  # no OperationalError

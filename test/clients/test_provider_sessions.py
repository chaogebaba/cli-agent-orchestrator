"""Provider-session retirement, migration, and tombstone resolution tests."""

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database


@pytest.fixture
def provider_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider.db'}")
    database.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    return engine


def values(
    name="base",
    session_uuid="11111111-1111-4111-8111-111111111111",
    source_terminal_id="term-1",
    **overrides,
):
    result = {
        "name": name,
        "provider": "codex",
        "session_uuid": session_uuid,
        "cwd": "/repo",
        "agent_profile": "developer",
        "git_sha": "a" * 40,
        "dirty_hashes": "{}",
        "summary": name,
        "source_terminal_id": source_terminal_id,
    }
    result.update(overrides)
    return result


def test_fresh_schema_accepts_retired(provider_db):
    row = database.register_provider_session(**values())
    retired = database.retire_provider_session("base")
    assert retired is not None
    assert retired["id"] == row["id"]
    assert retired["status"] == "retired"


def test_e3_anchor_kind_round_trips_and_cold_name_is_reserved(provider_db):
    anchor = database.register_provider_session(**values(name="root", kind="anchor"))
    assert anchor["kind"] == "anchor"
    assert database.get_ready_provider_session("root")["kind"] == "anchor"

    with pytest.raises(ValueError, match="base_name_reserved:cold"):
        database.register_provider_session(**values(name="cold"))


def test_e3_kind_migration_backfills_existing_rows_as_base(tmp_path, monkeypatch):
    db_file = tmp_path / "kind-legacy.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(
            """
            CREATE TABLE provider_sessions (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, provider TEXT NOT NULL, session_uuid TEXT NOT NULL,
                cwd TEXT NOT NULL, agent_profile TEXT NOT NULL, git_sha TEXT,
                dirty_hashes TEXT DEFAULT '{}' NOT NULL, summary TEXT, status TEXT NOT NULL,
                source_terminal_id TEXT, session_name TEXT, created_at DATETIME, updated_at DATETIME,
                CONSTRAINT ck_provider_sessions_status
                    CHECK (status IN ('ready','superseded','retired'))
            );
            INSERT INTO provider_sessions
                (name, provider, session_uuid, cwd, agent_profile, status)
                VALUES ('legacy', 'codex', 'legacy-uuid', '/repo', 'developer', 'ready');
            """
        )
    engine = create_engine(f"sqlite:///{db_file}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    database._migrate_provider_sessions_kind()
    database._migrate_provider_sessions_kind()

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT name, kind FROM provider_sessions")
        ).all() == [("legacy", "base")]
    from cli_agent_orchestrator.services.fork_context_service import resolve_base
    assert resolve_base("legacy")["kind"] == "base"


def test_legacy_schema_migrates_rows_index_and_actual_retire(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(
            """
            CREATE TABLE provider_sessions (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, provider TEXT NOT NULL, session_uuid TEXT NOT NULL,
                cwd TEXT NOT NULL, agent_profile TEXT NOT NULL, git_sha TEXT,
                dirty_hashes TEXT DEFAULT '{}' NOT NULL, summary TEXT, status TEXT NOT NULL,
                source_terminal_id TEXT, created_at DATETIME, updated_at DATETIME,
                CONSTRAINT ck_provider_sessions_status
                    CHECK (status IN ('ready','superseded'))
            );
            CREATE UNIQUE INDEX uq_provider_sessions_ready ON provider_sessions (name)
                WHERE status = 'ready';
            INSERT INTO provider_sessions
                (name, provider, session_uuid, cwd, agent_profile, dirty_hashes, status)
                VALUES ('legacy', 'codex', 'legacy-uuid', '/repo', 'developer', '{}', 'ready');
            """
        )
    engine = create_engine(f"sqlite:///{db_file}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    database._migrate_provider_sessions_status()
    database._migrate_provider_sessions_status()

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT name, status FROM provider_sessions")).all()
        table_sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_sessions'")
        ).scalar_one()
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("provider_sessions")}
    assert rows == [("legacy", "ready")]
    assert "'retired'" in table_sql
    assert indexes["uq_provider_sessions_ready"]["unique"] == 1
    assert database.retire_provider_session("legacy")["status"] == "retired"


def test_retire_transaction_unknown_and_reregister(provider_db):
    first = database.register_provider_session(**values())
    assert database.retire_provider_session("missing") is None
    retired = database.retire_provider_session("base")
    assert retired["id"] == first["id"]
    assert database.retire_provider_session("base") is None

    replacement = database.register_provider_session(**values(summary="replacement"))
    assert replacement["id"] != first["id"]
    assert replacement["status"] == "ready"
    with provider_db.connect() as connection:
        statuses = connection.execute(
            text("SELECT status FROM provider_sessions WHERE name='base' ORDER BY id")
        ).scalars().all()
    assert statuses == ["retired", "ready"]


def test_same_uuid_refresh_then_retire_is_tombstoned(provider_db):
    database.register_provider_session(**values())
    database.register_provider_session(**values(summary="refreshed"))
    database.retire_provider_session("base")
    assert database.get_provider_session_by_uuid("11111111-1111-4111-8111-111111111111") is None


def test_superseded_only_uuid_remains_resolvable_d17(provider_db):
    first = database.register_provider_session(**values(session_uuid="old-uuid"))
    database.register_provider_session(**values(session_uuid="new-uuid"))
    resolved = database.get_provider_session_by_uuid("old-uuid")
    assert resolved["id"] == first["id"]
    assert resolved["status"] == "superseded"


def test_ready_under_another_name_wins_over_retired_same_uuid(provider_db):
    database.register_provider_session(**values(name="old"))
    database.retire_provider_session("old")
    ready = database.register_provider_session(**values(name="new"))
    resolved = database.get_provider_session_by_uuid("11111111-1111-4111-8111-111111111111")
    assert resolved["id"] == ready["id"]
    assert resolved["status"] == "ready"


def test_retired_is_excluded_from_ready_list(provider_db):
    database.register_provider_session(**values(name="keep", session_uuid="keep-uuid"))
    database.register_provider_session(**values(name="drop", session_uuid="drop-uuid"))
    database.retire_provider_session("drop")
    assert [row["name"] for row in database.list_ready_provider_sessions()] == ["keep"]


def test_retire_blocks_resolution_by_name_uuid_and_source_terminal(provider_db):
    database.create_terminal("term-1", "session", "window", "codex", "developer")
    uuid = "11111111-1111-4111-8111-111111111111"
    database.update_terminal_provider_session_id("term-1", uuid)
    database.register_provider_session(**values())
    database.retire_provider_session("base")

    from cli_agent_orchestrator.services.fork_context_service import ForkContextError, resolve_base

    with pytest.raises(ForkContextError, match="base_name_unknown"):
        resolve_base("base")
    with pytest.raises(ForkContextError, match="base_not_registered"):
        resolve_base("term-1")
    with pytest.raises(ForkContextError, match="base_not_registered"):
        resolve_base(uuid)

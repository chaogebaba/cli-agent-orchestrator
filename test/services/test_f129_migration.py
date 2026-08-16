"""Tests for the F129 startup migration (_migrate_f129_frozen_authority).

Covers AC12, AC17, AC18; kills mutants M17-M22 from the blueprint.

Every behavioral assertion drives the real production code: either
`db_module._migrate_f129_frozen_authority()` or the real `db_module.init_db()`
entry path. The fixture only builds a genuine pre-F129 schema; it never
reimplements the migration.
"""

import pytest
from sqlalchemy import create_engine, text

import cli_agent_orchestrator.clients.database as db_module
from cli_agent_orchestrator.clients.database import init_db


@pytest.fixture
def legacy_engine(tmp_path):
    """A pre-F129 database: authority_pin and inbox without frozen/covering index."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE authority_pin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_key TEXT NOT NULL,
                file_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                registered_by TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(task_key, file_path, version)
            )
        """))
        conn.execute(text("""
            CREATE TABLE inbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            INSERT INTO authority_pin (task_key, file_path, sha256, version, registered_by)
            VALUES ('term1', '/path/blueprint.md', 'abc123', 1, 'sup1'),
                   ('term1', '/path/blueprint.md', 'def456', 2, 'sup1')
        """))
        conn.execute(text("""
            INSERT INTO inbox (sender_id, receiver_id, message, status)
            VALUES ('term1', 'sup1', 'task result payload', 'delivered')
        """))
    return engine


def _frozen_column(engine):
    """PRAGMA row for authority_pin.frozen, or None when absent."""
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(authority_pin)")).fetchall()
    matches = [r for r in rows if r[1] == "frozen"]
    assert len(matches) <= 1, f"frozen column duplicated: {len(matches)} occurrences"
    return matches[0] if matches else None


def _index_columns(engine, index_name="ix_inbox_sender_receiver"):
    """Ordered column names of an inbox index, or None when the index is absent."""
    with engine.connect() as conn:
        names = [r[1] for r in conn.execute(text("PRAGMA index_list(inbox)")).fetchall()]
        assert names.count(index_name) <= 1, "covering index duplicated"
        if index_name not in names:
            return None
        info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
    return [r[2] for r in info]


class TestMigrationFunction:
    """Direct invocations of the real _migrate_f129_frozen_authority()."""

    def test_adds_frozen_column_with_declared_type_notnull_default(
        self, legacy_engine, monkeypatch
    ):
        """M20: frozen arrives as BOOLEAN NOT NULL DEFAULT 0."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        assert _frozen_column(legacy_engine) is None

        db_module._migrate_f129_frozen_authority()

        col = _frozen_column(legacy_engine)
        assert col is not None, "frozen column must be added to authority_pin"
        assert col[2].upper() == "BOOLEAN"
        assert col[3] == 1, "frozen must be NOT NULL"
        assert col[4] == "0", "frozen DEFAULT must be 0"

    def test_existing_rows_are_unfrozen_and_still_mutable(self, legacy_engine, monkeypatch):
        """M20: legacy pins keep frozen=0 and still accept a new version."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        db_module._migrate_f129_frozen_authority()

        with legacy_engine.begin() as conn:
            existing = conn.execute(
                text("SELECT frozen FROM authority_pin ORDER BY version")
            ).fetchall()
            assert [r[0] for r in existing] == [0, 0], "both legacy rows survive as mutable"

            conn.execute(text("""
                INSERT INTO authority_pin
                    (task_key, file_path, sha256, version, registered_by, frozen)
                VALUES ('term1', '/path/blueprint.md', 'newsha', 3, 'sup1', 0)
            """))
            bumped = conn.execute(text(
                "SELECT version, frozen FROM authority_pin "
                "WHERE task_key='term1' ORDER BY version"
            )).fetchall()
        assert [tuple(r) for r in bumped] == [(1, 0), (2, 0), (3, 0)]

    def test_frozen_pin_round_trips_after_migration(self, legacy_engine, monkeypatch):
        """The migrated column stores frozen=1 pins alongside legacy mutable ones."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        db_module._migrate_f129_frozen_authority()

        with legacy_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO authority_pin
                    (task_key, file_path, sha256, version, registered_by, frozen)
                VALUES ('term2', '/path/schema.json', 'frozensha', 1, 'sup1', 1)
            """))
            frozen = conn.execute(text(
                "SELECT frozen FROM authority_pin WHERE task_key='term2'"
            )).scalar()
        assert frozen == 1

    def test_adds_covering_index_on_sender_receiver(self, legacy_engine, monkeypatch):
        """M21: ix_inbox_sender_receiver covers exactly (sender_id, receiver_id)."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        assert _index_columns(legacy_engine) is None

        db_module._migrate_f129_frozen_authority()

        assert _index_columns(legacy_engine) == ["sender_id", "receiver_id"]

    def test_idempotent_when_run_twice(self, legacy_engine, monkeypatch):
        """M18/M19/M21/M22: a second startup must not raise or duplicate schema."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        db_module._migrate_f129_frozen_authority()
        db_module._migrate_f129_frozen_authority()

        assert _frozen_column(legacy_engine) is not None
        assert _index_columns(legacy_engine) == ["sender_id", "receiver_id"]

    def test_noop_on_fresh_schema(self, tmp_path, monkeypatch):
        """M18/M19/M22: a fresh DB already has both; the migration must still succeed."""
        engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE authority_pin (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_key TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    registered_by TEXT NOT NULL,
                    frozen BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(task_key, file_path, version)
                )
            """))
            conn.execute(text("""
                CREATE TABLE inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT NOT NULL,
                    receiver_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text(
                "CREATE INDEX ix_inbox_sender_receiver ON inbox (sender_id, receiver_id)"
            ))

        monkeypatch.setattr(db_module, "engine", engine)
        db_module._migrate_f129_frozen_authority()

        assert _frozen_column(engine) is not None
        assert _index_columns(engine) == ["sender_id", "receiver_id"]


class TestInitDbLegacyUpgrade:
    """The production startup path applied to a legacy database."""

    @staticmethod
    def _noop_sibling_steps(monkeypatch):
        """Silence every init_db step except create_all and the F129 migration.

        The roster is read off init_db's own bytecode so a newly registered
        migration cannot silently run for real against the legacy engine.
        """
        siblings = [
            name
            for name in init_db.__code__.co_names
            if name.startswith(("_migrate_", "_bootstrap_", "_restrict_"))
            and name != "_migrate_f129_frozen_authority"
        ]
        assert len(siblings) > 20, f"unexpected init_db roster: {siblings}"
        for name in siblings:
            monkeypatch.setattr(db_module, name, lambda: None)

    def test_init_db_upgrades_legacy_schema(self, legacy_engine, monkeypatch):
        """M17/M20/M21: dropping the call from init_db leaves a legacy DB unmigrated.

        Base.metadata.create_all is deliberately left real — it creates missing
        tables but never alters an existing authority_pin or inbox, so the
        frozen column and covering index can only come from the registered
        _migrate_f129_frozen_authority() call.
        """
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        self._noop_sibling_steps(monkeypatch)

        init_db()

        col = _frozen_column(legacy_engine)
        assert col is not None, "M17: init_db must add frozen to an existing authority_pin"
        assert col[3] == 1, "frozen must be NOT NULL"
        assert col[4] == "0", "frozen DEFAULT must be 0"

        assert _index_columns(legacy_engine) == ["sender_id", "receiver_id"], (
            "M17/M21: init_db must add the covering index to an existing inbox"
        )

        with legacy_engine.connect() as conn:
            legacy_rows = conn.execute(text(
                "SELECT frozen FROM authority_pin WHERE task_key='term1'"
            )).fetchall()
        assert [r[0] for r in legacy_rows] == [0, 0], "legacy pins stay mutable"

    def test_init_db_is_idempotent_on_legacy_schema(self, legacy_engine, monkeypatch):
        """M17/M18/M19/M21/M22: a second server start over the same DB must not crash."""
        monkeypatch.setattr(db_module, "engine", legacy_engine)
        self._noop_sibling_steps(monkeypatch)

        init_db()
        init_db()

        assert _frozen_column(legacy_engine) is not None
        assert _index_columns(legacy_engine) == ["sender_id", "receiver_id"]

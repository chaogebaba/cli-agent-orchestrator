"""F618 (#474): migration for the D23 inbox delivery-control columns.

Slice B (F582 D23, merged ``e64684f9``) added ``InboxModel.expire_after_s`` /
``supersede_key`` to the mapped model and the inbox INSERT now names them, but
shipped no migration. ``create_all`` never alters an existing table, so on a
redeployed database every inbox INSERT 500s with::

    sqlite3.OperationalError: table inbox has no column named expire_after_s

These tests build a *legacy* inbox table WITHOUT the two columns, run the
migration(s), and assert the columns are added and an INSERT naming both
succeeds. The guard test additionally asserts that every mapped ``InboxModel``
column exists after the full migration runner (``init_db``) has run on a
legacy-schema database — this is the test that goes RED if the migration is
ever dropped from the runner registration.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import InboxModel

# A pre-D23 ("legacy") inbox schema: every column the model carried BEFORE
# slice B added expire_after_s / supersede_key. Faithfully missing exactly the
# two new columns so the migration has real work to do.
_LEGACY_INBOX_DDL = """
CREATE TABLE inbox (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    sender_id VARCHAR NOT NULL,
    receiver_id VARCHAR NOT NULL,
    logical_receiver_id VARCHAR,
    message VARCHAR NOT NULL,
    orchestration_type TEXT NOT NULL DEFAULT 'send_message',
    status VARCHAR NOT NULL,
    park_warm BOOLEAN,
    callback_dedup_key VARCHAR,
    failure_reason TEXT,
    digested_into INTEGER,
    enqueue_generation INTEGER,
    owner_receiver_id VARCHAR,
    owner_generation INTEGER,
    barrier_id INTEGER,
    barrier_member_key VARCHAR,
    created_at DATETIME
)
"""


def _inbox_columns(engine) -> set:
    with engine.begin() as connection:
        rows = connection.execute(text("PRAGMA table_info(inbox)")).mappings().all()
    return {row["name"] for row in rows}


@pytest.fixture
def legacy_inbox_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temp-file engine holding a legacy inbox table (no D23 columns).

    Mirrors the proven ``test_transcript_binding_nullable_inode_migration``
    pattern: build the legacy table on a dedicated file engine, then point the
    module-level ``engine`` (which the migration binds) at it.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_INBOX_DDL))
    monkeypatch.setattr(db_mod, "engine", engine)
    return engine


def test_f582_d23_migration_adds_columns_and_insert_succeeds(legacy_inbox_engine):
    engine = legacy_inbox_engine
    # Precondition: the legacy table is genuinely missing both new columns.
    before = _inbox_columns(engine)
    assert "expire_after_s" not in before
    assert "supersede_key" not in before

    db_mod._migrate_f582_d23_inbox_expiry()

    after = _inbox_columns(engine)
    assert "expire_after_s" in after
    assert "supersede_key" in after

    # An INSERT naming BOTH new columns now succeeds (this is the exact write
    # that 500'd before the migration).
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO inbox "
                "(sender_id, receiver_id, message, orchestration_type, status, "
                "expire_after_s, supersede_key) "
                "VALUES ('s', 'r', 'body', 'send_message', 'PENDING', 30, 'k1')"
            )
        )
        row = (
            connection.execute(text("SELECT expire_after_s, supersede_key FROM inbox"))
            .mappings()
            .one()
        )
    assert row["expire_after_s"] == 30
    assert row["supersede_key"] == "k1"


def test_f582_d23_migration_is_idempotent(legacy_inbox_engine):
    engine = legacy_inbox_engine
    db_mod._migrate_f582_d23_inbox_expiry()
    first = _inbox_columns(engine)
    # A second run on an already-migrated table is a no-op (guarded ADD COLUMN).
    db_mod._migrate_f582_d23_inbox_expiry()
    assert _inbox_columns(engine) == first


def test_f582_d23_migration_adds_only_missing_column(tmp_path, monkeypatch):
    """Each ADD COLUMN is independently guarded: a DB missing only one of the
    two columns gets exactly the missing one added, no duplicate-column error."""
    engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_INBOX_DDL))
        # Simulate a DB where only expire_after_s was hand-added (the live
        # workaround added both, but a partial state must not crash).
        connection.execute(text("ALTER TABLE inbox ADD COLUMN expire_after_s INTEGER"))
    monkeypatch.setattr(db_mod, "engine", engine)

    db_mod._migrate_f582_d23_inbox_expiry()

    cols = _inbox_columns(engine)
    assert "expire_after_s" in cols
    assert "supersede_key" in cols


def test_all_inbox_model_columns_present_after_migrations(tmp_path, monkeypatch):
    """Guard: after the FULL migration runner (``init_db``) has run on a
    legacy-schema database, every mapped ``InboxModel`` column exists in
    ``PRAGMA table_info(inbox)``.

    This is the mutant-catching test: dropping ``_migrate_f582_d23_inbox_expiry``
    from ``init_db``'s registration makes ``create_all`` skip the (already
    existing) inbox table, leaving it missing the two D23 columns and turning
    this assertion RED.
    """
    db_file = tmp_path / "cli-agent-orchestrator.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_INBOX_DDL))

    # Point BOTH the ORM engine (which the ALTER migrations bind) and the
    # DATABASE_FILE (which the self-connecting migrations open) at the same
    # legacy database, so init_db operates on one consistent file.
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=True)

    db_mod.init_db()

    present = _inbox_columns(engine)
    mapped = {column.name for column in InboxModel.__table__.columns}
    missing = mapped - present
    assert not missing, f"inbox table is missing mapped InboxModel columns: {sorted(missing)}"

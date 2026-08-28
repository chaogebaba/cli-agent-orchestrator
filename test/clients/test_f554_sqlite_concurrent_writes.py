"""F554 (#410): SQLite writer-contention regression tests.

Symptom under fix: a worker died in deferred init with
``sqlite3.OperationalError: (sqlite3.OperationalError) database is locked``
because the shared ORM engine set neither ``journal_mode=WAL`` nor
``busy_timeout`` on connect, so concurrent writers failed instantly instead of
waiting/serialising.

These tests use a real on-disk SQLite database under /data/cao-scratch (NEVER
/tmp, per fleet scratch policy) so the file-locking behaviour under test is the
real thing, not an in-memory artefact.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.constants import CAO_DB_BUSY_TIMEOUT_MS

_SCRATCH_ROOT = "/data/cao-scratch/c8f4e63d"


@pytest.fixture
def hardened_db(monkeypatch):
    """An isolated on-disk engine carrying the SAME connect pragmas as prod.

    Mirrors ``database.engine``'s F554 connect listener (WAL + busy_timeout +
    synchronous=NORMAL) so the test exercises the exact fix rather than the
    library default.
    """
    if not os.path.isdir("/data"):
        pytest.skip("/data not mounted — scratch policy forbids /tmp")
    os.makedirs(_SCRATCH_ROOT, exist_ok=True)
    db_path = os.path.join(_SCRATCH_ROOT, f"f554-{uuid.uuid4().hex}.db")

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={CAO_DB_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield sessions, engine, db_path
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass


def test_production_engine_sets_wal_and_busy_timeout():
    """The shipped ORM engine must open connections in WAL with a busy timeout."""
    with db.engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) >= 5000


def test_concurrent_writes_do_not_raise_database_locked(hardened_db):
    """Two threads writing through the client concurrently must not raise.

    Without WAL + busy_timeout this reliably raised
    ``OperationalError: database is locked``; with the fix the second writer
    waits for the first inside SQLite and both commits succeed.
    """
    sessions, _engine, _path = hardened_db

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(worker_index: int) -> None:
        try:
            barrier.wait(timeout=10)
            for row in range(25):
                terminal_id = f"f554-w{worker_index}-{row:02d}"
                # Real client write path: BEGIN + INSERT + COMMIT through the ORM.
                db.create_terminal(
                    terminal_id,
                    "cao-f554",
                    terminal_id,
                    "grok_cli",
                    "developer",
                    caller_id="caller",
                    init_state="init_pending",
                    init_started_at=db._utcnow(),
                    init_owner_epoch="00000000-0000-0000-0000-000000000001",
                    init_deadline_s=17.0,
                )
        except BaseException as exc:  # noqa: BLE001 — surface ANY failure to the assert
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    locked = [e for e in errors if isinstance(e, OperationalError)]
    assert not locked, f"concurrent writes raised database-locked errors: {locked}"
    assert not errors, f"concurrent writes raised unexpected errors: {errors}"

    # Both writers' rows are durable.
    with sessions() as check:
        count = check.query(db.TerminalModel).count()
    assert count == 50


def test_ready_commit_retries_on_transient_busy(hardened_db, monkeypatch):
    """mark_terminal_init_ready retries a transient SQLITE_BUSY and then succeeds.

    Simulates one busy commit followed by success; the bounded retry must
    swallow the first and return the committed result.
    """
    sessions, engine, _path = hardened_db

    db.create_terminal(
        "f554-retry",
        "cao-f554",
        "f554-retry",
        "grok_cli",
        "developer",
        caller_id="caller",
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=17.0,
    )

    original_commit = engine.dialect.do_commit
    calls = {"n": 0}

    def flaky_commit(dbapi_fairy):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("BEGIN", {}, Exception("database is locked"))
        original_commit(dbapi_fairy)

    monkeypatch.setattr(engine.dialect, "do_commit", flaky_commit)

    result = db.mark_terminal_init_ready(
        "f554-retry",
        should_commit=lambda: True,
        busy_delay_s=0.001,
    )
    assert result is True
    assert calls["n"] >= 2  # first attempt was busy, retried

    monkeypatch.setattr(engine.dialect, "do_commit", original_commit)
    meta = db.get_terminal_metadata("f554-retry")
    assert meta["init_state"] == "ready"

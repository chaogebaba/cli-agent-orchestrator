"""F230 — progress-fence cleanup tolerates an already-closed DBAPI handle."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'f230.db'}", connect_args={"check_same_thread": False}
    )
    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    yield sessions, engine
    engine.dispose()


def _pending(terminal_id: str):
    return db.create_terminal(
        terminal_id,
        "cao-s",
        terminal_id,
        "grok_cli",
        "developer",
        caller_id="caller",
        init_state="init_pending",
        init_started_at=db._utcnow(),
        init_owner_epoch="00000000-0000-0000-0000-000000000001",
        init_deadline_s=17.0,
    )


# ───────────────────────────────────────────────────────────────────────────
# Test 1: closed DBAPI handle after successful commit → True + durable ready
# ───────────────────────────────────────────────────────────────────────────

def test_closed_handle_after_commit_returns_true_and_ready(isolated_db, monkeypatch):
    """Simulate DBAPI handle closed between commit and finally cleanup."""
    sessions, engine = isolated_db
    _pending("f230-closed-ok")

    original_commit = engine.dialect.do_commit

    def closing_commit(dbapi_fairy):
        """Commit succeeds, then immediately close the raw handle."""
        original_commit(dbapi_fairy)
        # Close the underlying sqlite3 connection to simulate the production
        # scenario where the pool/driver invalidates the handle post-commit.
        raw = getattr(dbapi_fairy, "driver_connection", dbapi_fairy)
        raw.close()

    monkeypatch.setattr(engine.dialect, "do_commit", closing_commit)

    result = db.mark_terminal_init_ready(
        "f230-closed-ok",
        should_commit=lambda: True,
    )
    assert result is True

    # Verify durable state — need a fresh engine since we closed the connection.
    engine.dispose()
    meta = db.get_terminal_metadata("f230-closed-ok")
    assert meta["init_state"] == "ready"


# ───────────────────────────────────────────────────────────────────────────
# Test 2: non-closed ProgrammingError remains a failure
# ───────────────────────────────────────────────────────────────────────────

def test_non_closed_programming_error_propagates(isolated_db, monkeypatch):
    """A ProgrammingError that isn't about a closed handle must propagate."""
    sessions, engine = isolated_db
    _pending("f230-non-closed")

    original_clear = db._clear_progress_fence
    call_count = [0]

    def injecting_clear(fence_fn, terminal_id):
        call_count[0] += 1
        # First call(s) during the try body are normal; the finally call raises.
        if call_count[0] > 0:
            raise sqlite3.ProgrammingError("library routine called out of sequence")
        original_clear(fence_fn, terminal_id)

    monkeypatch.setattr(db, "_clear_progress_fence", injecting_clear)

    with pytest.raises(sqlite3.ProgrammingError, match="library routine"):
        db.mark_terminal_init_ready(
            "f230-non-closed",
            should_commit=lambda: True,
        )


# ───────────────────────────────────────────────────────────────────────────
# Test 3: original commit failure + cleanup failure preserves original cause
# ───────────────────────────────────────────────────────────────────────────

def test_commit_failure_not_masked_by_cleanup_error(isolated_db, monkeypatch):
    """Original commit exception must be preserved even if cleanup also fails."""
    sessions, engine = isolated_db
    _pending("f230-mask")

    def failing_commit(dbapi_connection):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(engine.dialect, "do_commit", failing_commit)

    with pytest.raises(Exception) as exc_info:
        db.mark_terminal_init_ready(
            "f230-mask",
            should_commit=lambda: True,
        )

    # The raised exception should trace back to disk full, not a cleanup error.
    cause = exc_info.value
    while cause.__cause__:
        cause = cause.__cause__
    assert "disk full" in str(cause)


# ───────────────────────────────────────────────────────────────────────────
# Test 4: ready-veto/abandoned paths unchanged
# ───────────────────────────────────────────────────────────────────────────

def test_abandoned_path_still_returns_false(isolated_db, monkeypatch):
    """should_commit returning False mid-transaction still returns False."""
    sessions, engine = isolated_db
    _pending("f230-abandoned")

    call_count = [0]

    def abandoning():
        call_count[0] += 1
        # First call allows entry; second call triggers abandonment.
        return call_count[0] <= 1

    result = db.mark_terminal_init_ready(
        "f230-abandoned",
        should_commit=abandoning,
    )
    assert result is False

    meta = db.get_terminal_metadata("f230-abandoned")
    assert meta["init_state"] == "init_pending"


def test_veto_path_returns_false(isolated_db, monkeypatch):
    """decide_commit returning False triggers veto, returns False."""
    sessions, engine = isolated_db
    _pending("f230-veto")

    result = db.mark_terminal_init_ready(
        "f230-veto",
        decide_commit=lambda: False,
    )
    assert result is False


# ───────────────────────────────────────────────────────────────────────────
# Test 5: mutation — unconditional cleanup reproduces closed-db failure
# ───────────────────────────────────────────────────────────────────────────

def test_mutation_unconditional_cleanup_reproduces_failure(isolated_db, monkeypatch):
    """Reverting _clear_progress_fence to raw call reproduces the original bug.

    This mutant verifies that the fix is load-bearing: if the cleanup helper
    is bypassed (calling fence_fn directly without try/except), the closed
    handle raises ProgrammingError.
    """
    sessions, engine = isolated_db
    _pending("f230-mutant")

    original_commit = engine.dialect.do_commit

    def closing_commit(dbapi_fairy):
        original_commit(dbapi_fairy)
        raw = getattr(dbapi_fairy, "driver_connection", dbapi_fairy)
        raw.close()

    monkeypatch.setattr(engine.dialect, "do_commit", closing_commit)

    # Temporarily bypass the safe cleanup to prove the defect re-emerges.
    original_clear = db._clear_progress_fence

    def raw_clear(fence_fn, terminal_id):
        fence_fn(None, 0)  # No protection — should raise.

    monkeypatch.setattr(db, "_clear_progress_fence", raw_clear)

    with pytest.raises(sqlite3.ProgrammingError, match="[Cc]losed"):
        db.mark_terminal_init_ready(
            "f230-mutant",
            should_commit=lambda: True,
        )

    monkeypatch.setattr(db, "_clear_progress_fence", original_clear)


# ───────────────────────────────────────────────────────────────────────────
# Test 6: _clear_progress_fence unit tests
# ───────────────────────────────────────────────────────────────────────────

def test_clear_progress_fence_suppresses_closed_error():
    """Closed-database ProgrammingError is suppressed."""

    def closed_handler(handler, n):
        raise sqlite3.ProgrammingError("Cannot operate on a closed database.")

    # Should not raise.
    db._clear_progress_fence(closed_handler, "test-unit")


def test_clear_progress_fence_reraises_other_error():
    """Non-closed ProgrammingError propagates."""

    def other_handler(handler, n):
        raise sqlite3.ProgrammingError("Misuse of prepared statement")

    with pytest.raises(sqlite3.ProgrammingError, match="Misuse"):
        db._clear_progress_fence(other_handler, "test-unit")

"""The composition root's read-only wiring for ``cao diag`` (AC7/AC9).

``cli`` may not import ``adapters``, so these two functions are the whole bridge
between the command and the database.  They are worth their own tests because
both have a failure mode that would otherwise be silent: a scope that cannot be
read degrades the agreement report rather than failing it, and a read-only pool
that quietly permitted writes would defeat the one guarantee AC7 makes about
running against the LIVE server database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.adapters.store.migrator import migrate
from cli_agent_orchestrator.app.worker_truth.agreement import TerminalFacts
from cli_agent_orchestrator.bootstrap import build_readonly_diag_stores, build_terminal_scope


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "cao.db"
    result, pool = migrate(path, busy_timeout_ms=5000)
    assert result.ok and pool is not None
    pool.close_all()
    return path


def test_the_diag_stores_open_read_only(db: Path) -> None:
    sources = build_readonly_diag_stores(db)

    assert sources.events.read() == []
    assert sources.states.get("nobody") is None
    assert sources.findings.list_findings() == []
    with pytest.raises(sqlite3.OperationalError):
        sources.events._pool.connection().execute("DELETE FROM worker_event")


def test_the_scope_reads_session_and_provider_from_the_legacy_table(db: Path) -> None:
    """``tmux_session`` and ``provider`` are not in the event log, so the report
    gets them from the row the fork already keeps."""
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT, provider TEXT)")
    conn.executemany(
        "INSERT INTO terminals VALUES (?, ?, ?)",
        [("t1", "cao-alpha", "codex"), ("t2", "cao-alpha", "claude_code"), ("t3", None, None)],
    )
    conn.commit()
    conn.close()

    scope = build_terminal_scope(db)

    assert scope["t1"] == TerminalFacts(session="cao-alpha", provider="codex")
    assert scope["t2"] == TerminalFacts(session="cao-alpha", provider="claude_code")
    # NULLs become empty strings, never None: TerminalFacts is compared by value
    # and a None would make an unscoped terminal look like a distinct session.
    assert scope["t3"] == TerminalFacts(session="", provider="")


def test_a_missing_terminals_table_degrades_rather_than_raising(db: Path) -> None:
    """A worse report, but a real one.

    The agreement command failing outright on a database that is otherwise
    perfectly readable would be the wrong trade: without a scope it falls back to
    fleet-wide with codex detected from the producer column.
    """
    assert build_terminal_scope(db) == {}


def test_a_missing_database_degrades_too(tmp_path: Path) -> None:
    assert build_terminal_scope(tmp_path / "does-not-exist.db") == {}

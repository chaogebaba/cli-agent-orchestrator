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
from cli_agent_orchestrator.bootstrap import build_readonly_diag_stores


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

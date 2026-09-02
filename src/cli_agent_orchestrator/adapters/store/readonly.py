"""Read-only access to the live server database for ``cao diag`` (AC7).

The blueprint's implicit decision 7: the CLI opens the LIVE database with a
``mode=ro`` URI, which WAL permits alongside the server's writer.  No snapshot,
no copy, no write path — so a diagnostic command cannot corrupt or stall the
server it was called to diagnose, however it is invoked.

:class:`ReadOnlyPool` exists so the three store adapters can be reused verbatim.
They all reach their connection through ``ConnectionPool.connection()``, so a
pool whose connection happens to be read-only gives ``cao diag`` the same query
code the server runs, with SQLite itself enforcing the read-only half.  Writing
a second, read-only copy of each store would be two implementations of the same
SELECTs, free to disagree about what a row means — which is the failure this
whole work package is about.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cli_agent_orchestrator.adapters.store.connection import read_only_connect

__all__ = ["ReadOnlyPool"]


class ReadOnlyPool:
    """A ``ConnectionPool``-shaped handle over one read-only connection.

    One connection, not one per thread: a CLI invocation is single-threaded and
    short-lived, and ``read_only_connect`` already passes
    ``check_same_thread=False``.
    """

    def __init__(self, db_path: Path, *, busy_timeout_ms: int) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = read_only_connect(self._db_path, busy_timeout_ms=self._busy_timeout_ms)
        return self._conn

    def checkpoint(self) -> None:
        """A no-op: a reader never checkpoints.

        Present so this stands in for a ``ConnectionPool`` wherever one is
        expected, and silent rather than raising, because a reader being asked to
        checkpoint is a caller's mistake that should not become the operator's
        traceback.
        """

    def close_all(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

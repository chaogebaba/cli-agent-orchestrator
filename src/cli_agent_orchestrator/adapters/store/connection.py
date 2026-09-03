"""SQLite connection policy for the new store (WP-ARCH phase 1, AC3).

Every connection this package opens is configured the same way, in one place,
because the fork's existing habits are the problem this replaces: 27 of its 30
raw ``sqlite3.connect`` sites set no busy timeout at all (r9), which under the
single-writer model (U5) turns a moment of contention into an immediate
``database is locked``.

Three settings, each load-bearing:

* ``journal_mode=WAL`` — readers never block the writer, which is what lets
  ``cao diag`` open the LIVE database read-only while the server is appending.
* ``busy_timeout`` — handed in from ``bootstrap.py`` (which reads the fork's
  ``CAO_DB_BUSY_TIMEOUT_MS``); adapters may not import ``constants``.
* ``isolation_level=None`` — autocommit, so this module issues its own
  ``BEGIN IMMEDIATE``.  Python's default implicit transaction handling opens a
  DEFERRED transaction, which upgrades to a write lock only at the first write
  and can therefore fail mid-transaction under contention.  AC3 needs the write
  lock taken UP FRONT so the high-water bump and the insert are genuinely
  atomic.

Connections are thread-local.  A ``sqlite3.Connection`` is not safe to share
across threads, and the server is one process with several (the FastAPI thread
pool, the tmux clients).  One connection per thread per database file is the
cheap, boring answer.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "ConnectionPool",
    "SqliteConnectionSource",
    "immediate_transaction",
    "parse_timestamp",
    "read_only_connect",
    "render_timestamp",
]

# There is deliberately NO default busy timeout here. The value belongs to the
# fork's ``constants.CAO_DB_BUSY_TIMEOUT_MS`` and arrives from ``bootstrap.py``;
# a fallback would be a second copy of a tunable that already exists, free to
# drift from the real one, and §4c forbids a duration literal outside
# ``core/timing.py``. Callers pass it explicitly or they do not open a
# connection.

# Canonical wire format for the DDL's TEXT timestamp columns: ISO-8601, UTC,
# always six fractional digits.  Fixed width matters — these columns are
# compared and ordered as STRINGS, and a variable-width rendering makes that
# ordering depend on whether a microsecond happened to be zero.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def render_timestamp(value: datetime) -> str:
    """Render an aware datetime as the canonical fixed-width UTC string."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamps stored by the worker event log must be timezone-aware")
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """Parse a stored timestamp back to an aware UTC datetime."""
    return datetime.strptime(value, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


@runtime_checkable
class SqliteConnectionSource(Protocol):
    """What the three store adapters actually need from a pool.

    Exactly two methods, which is the point.  :class:`ConnectionPool` and
    :class:`~adapters.store.readonly.ReadOnlyPool` are unrelated classes — one
    hands out thread-local read/write connections, the other a single ``mode=ro``
    connection for ``cao diag`` — and structural typing is what lets the same
    ``SqliteEventStore`` serve both without either inheriting from the other or
    the stores growing a second read-only implementation of the same SELECTs.

    Deliberately NOT ``db_path`` or ``close_all``: a Protocol should name what
    callers use, not everything the concrete classes happen to expose.  Widening
    it later is additive; narrowing it is not.

    The migrator keeps its concrete :class:`ConnectionPool` annotation and is not
    moved to this Protocol.  That is not an oversight — it writes DDL, so a
    read-only pool must remain a type error there.
    """

    def connection(self) -> sqlite3.Connection: ...

    def checkpoint(self) -> None: ...


class ConnectionPool:
    """Thread-local SQLite connections for one database file."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    def connection(self) -> sqlite3.Connection:
        """The calling thread's connection, opened on first use."""
        existing: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        with self._all_lock:
            self._all.append(conn)
        return conn

    def checkpoint(self) -> None:
        """Fold the WAL back into the main file.

        PASSIVE, and never ``VACUUM``: the blueprint is explicit that retention
        checkpoints only.  A ``VACUUM`` rewrites the whole database file and
        takes an exclusive lock, which on the live coordination database would
        stall every worker for the duration.
        """
        self.connection().execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close_all(self) -> None:
        """Close every connection this pool opened, from any thread."""
        with self._all_lock:
            connections = list(self._all)
            self._all.clear()
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one ``BEGIN IMMEDIATE`` transaction.

    The write lock is taken at ``BEGIN``, not at the first write, so two
    appenders serialise here rather than discovering the conflict halfway
    through.  Any exception rolls the WHOLE block back — which is precisely what
    makes a crash between the high-water bump and the insert leave no gap (B7).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    conn.execute("COMMIT")


def read_only_connect(db_path: Path, *, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open the LIVE database read-only via a ``mode=ro`` URI (AC7).

    ``cao diag`` uses this: WAL permits a concurrent reader, so the CLI needs
    neither a snapshot nor a write path, and cannot corrupt the server's
    database however it is invoked.
    """
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn

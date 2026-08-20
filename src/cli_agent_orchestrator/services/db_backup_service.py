"""F335: Periodic SQLite backup service.

Performs hourly ``.backup`` of the CAO database to
``~/.aws/cli-agent-orchestrator/db/backups/<UTC-timestamp>.db``,
keeping the newest 24 copies and deleting older ones.

Uses the SQLite backup API (``sqlite3.Connection.backup()``) which is safe
for live databases — never a raw file copy of a WAL-mode DB.

Designed to run as an in-server periodic asyncio task (preferred over
external timers per the issue spec).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Retention: keep the newest N backups
_MAX_BACKUPS = 24

# Interval between backups in seconds (1 hour)
_BACKUP_INTERVAL_S = 3600


def _backup_dir() -> Path:
    """Resolve the backup directory. Deferred to avoid import-time side effects."""
    from cli_agent_orchestrator.constants import DB_DIR

    return DB_DIR / "backups"


def _database_path() -> Path:
    """Resolve the live database file path."""
    from cli_agent_orchestrator.constants import DATABASE_FILE

    return DATABASE_FILE


def run_backup(
    *,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    max_backups: int = _MAX_BACKUPS,
) -> Path | None:
    """Execute one SQLite backup using the backup API.

    Args:
        db_path: Source database file. Defaults to the production DB.
        backup_dir: Destination directory. Defaults to db/backups/.
        max_backups: Number of backups to retain.

    Returns:
        Path to the new backup file, or None on failure.
    """
    if db_path is None:
        db_path = _database_path()
    if backup_dir is None:
        backup_dir = _backup_dir()

    if not db_path.exists():
        logger.warning("f335_backup: source DB does not exist: %s", db_path)
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = backup_dir / f"{timestamp}.db"

    try:
        # Use sqlite3 backup API — safe for live WAL-mode databases
        src_conn = sqlite3.connect(str(db_path), timeout=30)
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

        logger.info("f335_backup: created %s", dest_path)
    except Exception as e:
        logger.error("f335_backup_failed: %s", e)
        # Clean up partial file
        if dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                pass
        return None

    # Retention: delete oldest backups beyond max_backups
    _prune_old_backups(backup_dir, max_backups)

    return dest_path


def _prune_old_backups(backup_dir: Path, max_backups: int) -> int:
    """Delete the oldest backups exceeding max_backups. Returns count deleted."""
    # List all .db files sorted by name (timestamp-based, so alphabetical = chronological)
    backups = sorted(backup_dir.glob("*.db"))
    to_delete = backups[: max(0, len(backups) - max_backups)]
    deleted = 0
    for old in to_delete:
        try:
            old.unlink()
            deleted += 1
            logger.debug("f335_backup_pruned: %s", old.name)
        except OSError as e:
            logger.warning("f335_backup_prune_failed: %s: %s", old.name, e)
    return deleted


async def backup_daemon(
    *,
    interval_s: float = _BACKUP_INTERVAL_S,
    max_backups: int = _MAX_BACKUPS,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
) -> None:
    """Long-running async daemon that performs periodic backups.

    Runs an initial backup at startup, then repeats every ``interval_s``
    seconds. Designed to be launched via ``asyncio.create_task()`` in the
    server lifespan.
    """
    logger.info(
        "f335_backup_daemon: starting (interval=%ds, retain=%d)",
        interval_s,
        max_backups,
    )

    while True:
        try:
            await asyncio.to_thread(
                run_backup,
                db_path=db_path,
                backup_dir=backup_dir,
                max_backups=max_backups,
            )
        except Exception:
            logger.exception("f335_backup_daemon: unhandled error in backup cycle")

        await asyncio.sleep(interval_s)

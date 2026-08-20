"""Tests for F335 db_backup_service — periodic SQLite backup.

Isolation: creates a fresh SQLite DB in a temporary directory, never
touches the real DATABASE_FILE or HOME directory.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.db_backup_service import (
    _prune_old_backups,
    run_backup,
)


@pytest.fixture
def isolated_db(tmp_path: Path) -> Path:
    """Create a small test SQLite database in a temp dir."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO test_data VALUES (1, 'hello')")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    """Provide a clean backup directory."""
    d = tmp_path / "backups"
    d.mkdir()
    return d


class TestRunBackup:
    """Test the one-shot backup function."""

    def test_creates_backup_file(self, isolated_db: Path, backup_dir: Path):
        result = run_backup(db_path=isolated_db, backup_dir=backup_dir)
        assert result is not None
        assert result.exists()
        assert result.suffix == ".db"
        assert result.parent == backup_dir

    def test_backup_is_valid_sqlite(self, isolated_db: Path, backup_dir: Path):
        result = run_backup(db_path=isolated_db, backup_dir=backup_dir)
        assert result is not None

        conn = sqlite3.connect(str(result))
        rows = conn.execute("SELECT value FROM test_data WHERE id = 1").fetchall()
        conn.close()
        assert rows == [("hello",)]

    def test_backup_creates_dir_if_missing(self, isolated_db: Path, tmp_path: Path):
        target = tmp_path / "nested" / "backups"
        assert not target.exists()
        result = run_backup(db_path=isolated_db, backup_dir=target)
        assert result is not None
        assert target.exists()

    def test_returns_none_if_source_missing(self, tmp_path: Path):
        fake_db = tmp_path / "nonexistent.db"
        backup_dir = tmp_path / "backups"
        result = run_backup(db_path=fake_db, backup_dir=backup_dir)
        assert result is None

    def test_multiple_backups_have_unique_names(self, isolated_db: Path, backup_dir: Path):
        r1 = run_backup(db_path=isolated_db, backup_dir=backup_dir)
        # Slight delay to get a different timestamp
        time.sleep(1.1)
        r2 = run_backup(db_path=isolated_db, backup_dir=backup_dir)
        assert r1 is not None
        assert r2 is not None
        assert r1 != r2

    def test_backup_with_wal_mode(self, tmp_path: Path, backup_dir: Path):
        """Verify backup works with WAL-mode databases (production config)."""
        db_path = tmp_path / "wal_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()

        result = run_backup(db_path=db_path, backup_dir=backup_dir)
        assert result is not None

        conn = sqlite3.connect(str(result))
        rows = conn.execute("SELECT x FROM t").fetchall()
        conn.close()
        assert rows == [(42,)]


class TestPruneOldBackups:
    """Test the retention/pruning logic."""

    def test_prune_keeps_newest(self, backup_dir: Path):
        # Create 5 backups with sequential timestamps
        for i in range(5):
            (backup_dir / f"2026010{i}T000000Z.db").write_text("x")

        deleted = _prune_old_backups(backup_dir, max_backups=3)
        assert deleted == 2
        remaining = sorted(f.name for f in backup_dir.glob("*.db"))
        assert remaining == [
            "20260102T000000Z.db",
            "20260103T000000Z.db",
            "20260104T000000Z.db",
        ]

    def test_prune_noop_when_under_limit(self, backup_dir: Path):
        for i in range(2):
            (backup_dir / f"2026010{i}T000000Z.db").write_text("x")

        deleted = _prune_old_backups(backup_dir, max_backups=5)
        assert deleted == 0
        assert len(list(backup_dir.glob("*.db"))) == 2

    def test_prune_empty_dir(self, backup_dir: Path):
        deleted = _prune_old_backups(backup_dir, max_backups=24)
        assert deleted == 0

    def test_prune_exact_limit(self, backup_dir: Path):
        for i in range(3):
            (backup_dir / f"2026010{i}T000000Z.db").write_text("x")

        deleted = _prune_old_backups(backup_dir, max_backups=3)
        assert deleted == 0

    def test_integration_run_backup_prunes(self, isolated_db: Path, backup_dir: Path):
        """run_backup with max_backups=2 should prune old ones."""
        # Create 3 pre-existing backups
        for i in range(3):
            (backup_dir / f"2020010{i}T000000Z.db").write_text("x")

        # Now run a real backup with max=2
        result = run_backup(db_path=isolated_db, backup_dir=backup_dir, max_backups=2)
        assert result is not None

        # Should have the new backup + 1 old one = 2 total (pruned the 2 oldest)
        remaining = list(backup_dir.glob("*.db"))
        assert len(remaining) == 2

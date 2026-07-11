import json
import subprocess
from pathlib import Path

import pytest
from unittest.mock import patch

from cli_agent_orchestrator.services.fork_context_service import snapshot, staleness


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("base")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def row(repo: Path, sha, hashes):
    return {"name": "base", "cwd": str(repo), "git_sha": sha, "dirty_hashes": hashes}


def test_tracked_modified_after_mark_is_stale(repo: Path):
    sha, hashes = snapshot(str(repo))
    (repo / "tracked.txt").write_text("changed")
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE] 1 files")


def test_dirty_at_snapshot_untouched_is_fresh(repo: Path):
    (repo / "tracked.txt").write_text("dirty-at-mark")
    sha, hashes = snapshot(str(repo))
    assert json.loads(hashes)["tracked.txt"]
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == []
    assert preamble == "[FRESH] base 'base' snapshot current."


def test_clean_tree_is_fresh(repo: Path):
    sha, hashes = snapshot(str(repo))
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == []
    assert preamble.startswith("[FRESH]")


def test_deleted_at_snapshot_still_deleted_is_fresh(repo: Path):
    (repo / "tracked.txt").unlink()
    sha, hashes = snapshot(str(repo))
    assert json.loads(hashes)["tracked.txt"] is None
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == []
    assert preamble.startswith("[FRESH]")


def test_deleted_at_snapshot_then_recreated_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    sha, hashes = snapshot(str(repo))
    (repo / "tracked.txt").write_text("recreated")
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE] 1 files")


def test_deleted_at_snapshot_then_directory_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    sha, hashes = snapshot(str(repo))
    (repo / "tracked.txt").mkdir()
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE]")


def test_deleted_at_snapshot_then_symlink_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    sha, hashes = snapshot(str(repo))
    (repo / "tracked.txt").symlink_to("missing-target")
    changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE]")


def test_present_but_unreadable_hash_is_stale(repo: Path):
    (repo / "tracked.txt").write_text("dirty-at-mark")
    sha, hashes = snapshot(str(repo))
    with patch("cli_agent_orchestrator.services.fork_context_service._hash",
               side_effect=PermissionError("unreadable")):
        changed, preamble = staleness(row(repo, sha, hashes))
    assert changed == ["tracked.txt"]
    assert preamble.startswith("[STALE]")

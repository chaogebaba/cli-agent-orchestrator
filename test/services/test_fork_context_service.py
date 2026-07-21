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


def assert_fork_role_notice(preamble: str) -> None:
    notice = (
        "ROLE NOTICE: You are a newly forked worker, distinct from the base session whose "
        "transcript you inherit. Any role framing, read-only/do-not-edit constraints, or "
        "base-ready declarations inside the inherited transcript applied only to the "
        "original base. Your role, permissions, and constraints come solely from the "
        "dispatch message below."
    )
    assert notice in preamble
    assert "normal edit, commit, and test permissions" not in preamble


def test_tracked_modified_after_mark_is_stale(repo: Path):
    captured = snapshot(str(repo))
    (repo / "tracked.txt").write_text("changed")
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.changed_count == 1
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE] 1 files")
    assert_fork_role_notice(stale.preamble)


def test_dirty_at_snapshot_untouched_is_fresh(repo: Path):
    (repo / "tracked.txt").write_text("dirty-at-mark")
    captured = snapshot(str(repo))
    assert json.loads(captured.dirty_hashes())["tracked.txt"]
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.changed_count == 0
    assert stale.delta.entries == ()
    assert stale.preamble.startswith("[FRESH] base 'base' snapshot current.")
    assert_fork_role_notice(stale.preamble)


@pytest.mark.parametrize("sha, hashes", [(None, "{}"), ("invalid-sha", "{}")])
def test_stale_unknown_includes_fork_role_notice(repo: Path, sha, hashes):
    stale = staleness(row(repo, sha, hashes))
    assert stale.changed_count is None
    assert stale.preamble.startswith("[STALE-UNKNOWN]")
    assert_fork_role_notice(stale.preamble)


def test_clean_tree_is_fresh(repo: Path):
    captured = snapshot(str(repo))
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.changed_count == 0
    assert stale.preamble.startswith("[FRESH]")


def test_deleted_at_snapshot_still_deleted_is_fresh(repo: Path):
    (repo / "tracked.txt").unlink()
    captured = snapshot(str(repo))
    assert json.loads(captured.dirty_hashes())["tracked.txt"] is None
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.changed_count == 0
    assert stale.preamble.startswith("[FRESH]")


def test_deleted_at_snapshot_then_recreated_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    captured = snapshot(str(repo))
    (repo / "tracked.txt").write_text("recreated")
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.changed_count == 1
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE] 1 files")


def test_deleted_at_snapshot_then_directory_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    captured = snapshot(str(repo))
    (repo / "tracked.txt").mkdir()
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE]")


def test_deleted_at_snapshot_then_symlink_is_stale(repo: Path):
    (repo / "tracked.txt").unlink()
    captured = snapshot(str(repo))
    (repo / "tracked.txt").symlink_to("missing-target")
    stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE]")


def test_present_but_unreadable_hash_is_stale(repo: Path):
    (repo / "tracked.txt").write_text("dirty-at-mark")
    captured = snapshot(str(repo))
    with patch("cli_agent_orchestrator.services.fork_context_service._hash",
               side_effect=PermissionError("unreadable")):
        stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE]")

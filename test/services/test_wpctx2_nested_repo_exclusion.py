import hashlib
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services import base_digest_service
from cli_agent_orchestrator.services import fork_context_service as service
from cli_agent_orchestrator.services import terminal_service as terminals
from cli_agent_orchestrator.services.fork_context_service import SnapshotDelta

LOGGER_NAME = "cli_agent_orchestrator.services.fork_context_service"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("base", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def row(repo: Path, sha: str | None, hashes: str) -> dict[str, object]:
    return {"name": "base", "cwd": str(repo), "git_sha": sha, "dirty_hashes": hashes}


def test_t1_plain_nested_clone_is_excluded_from_snapshot_and_staleness(repo: Path) -> None:
    captured = service.snapshot(str(repo))
    (repo / "tracked.txt").write_text("changed", encoding="utf-8")
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "untracked.txt").write_text("nested", encoding="utf-8")

    current = service.snapshot(str(repo))
    stale = service.staleness(row(repo, captured.git_sha, captured.dirty_hashes()))

    assert current.paths == ("tracked.txt",)
    assert stale.delta.paths == ("tracked.txt",)
    artifact = base_digest_service.publish(
        base="base",
        cwd=str(repo),
        parent_artifact_sha=None,
        delta=stale.delta,
        body="nested clone excluded",
        round_number=1,
    )
    assert base_digest_service.covers(artifact, stale.delta)


def test_t2_git_file_marker_is_excluded(repo: Path) -> None:
    nested = repo / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: /missing/worktree-metadata\n", encoding="utf-8")
    (nested / "file.txt").write_text("nested", encoding="utf-8")

    assert service.snapshot(str(repo)).entries == ()


def test_t2a_tracked_file_inside_new_nested_repo_is_excluded(repo: Path) -> None:
    nested = repo / "nested"
    nested.mkdir()
    tracked = nested / "file.txt"
    tracked.write_text("outer", encoding="utf-8")
    git(repo, "add", "nested/file.txt")
    git(repo, "commit", "-qm", "track nested path")
    captured = service.snapshot(str(repo))

    git(nested, "init", "-q")
    tracked.write_text("inner", encoding="utf-8")

    assert service.snapshot(str(repo)).entries == ()
    assert (
        service.staleness(row(repo, captured.git_sha, captured.dirty_hashes())).changed_count == 0
    )


def test_t2b_tracked_gitlink_is_excluded(repo: Path) -> None:
    source = repo.parent / "submodule-source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "test@example.com")
    git(source, "config", "user.name", "Test")
    (source / "file.txt").write_text("base", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "nested")
    git(repo, "commit", "-qam", "add gitlink")
    captured = service.snapshot(str(repo))

    (repo / "nested" / "file.txt").write_text("changed", encoding="utf-8")
    git(repo / "nested", "config", "user.email", "test@example.com")
    git(repo / "nested", "config", "user.name", "Test")
    git(repo / "nested", "add", "file.txt")
    git(repo / "nested", "commit", "-qm", "advance gitlink")

    mode = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-s", "nested"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert mode.startswith("160000 ")
    assert service.snapshot(str(repo)).entries == ()
    assert (
        service.staleness(row(repo, captured.git_sha, captured.dirty_hashes())).changed_count == 0
    )


def test_t2c_legacy_nested_key_is_inert_but_markerless_key_is_absent(repo: Path) -> None:
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    manifest = json.dumps(
        {"gone/missing.txt": "a" * 64, "nested/missing.txt": "b" * 64},
        sort_keys=True,
    )

    stale = service.staleness(row(repo, service.snapshot(str(repo)).git_sha, manifest))

    assert stale.delta.paths == ("gone/missing.txt",)
    assert stale.delta.entries[0].state == "absent"


def test_t2d_symlink_and_marker_presence_semantics(repo: Path) -> None:
    plain = repo / "plain.txt"
    plain.write_text("target bytes", encoding="utf-8")
    git(repo, "add", "plain.txt")
    git(repo, "commit", "-qm", "plain target")

    target_repo = repo / "target-repo"
    target_repo.mkdir()
    git(target_repo, "init", "-q")
    (target_repo / "file.txt").write_text("nested", encoding="utf-8")
    (repo / "repo-link").symlink_to(target_repo.name)
    (repo / "file-link").symlink_to(plain.name)
    empty_marker = repo / "empty-marker"
    empty_marker.mkdir()
    (empty_marker / ".git").write_text("", encoding="utf-8")
    (empty_marker / "file.txt").write_text("nested", encoding="utf-8")

    captured = service.snapshot(str(repo))

    assert captured.paths == ("file-link",)
    assert captured.entries[0].state == "sha256"
    assert captured.entries[0].value == hashlib.sha256(b"target bytes").hexdigest()


@pytest.mark.asyncio
async def test_t2e_legacy_key_converges_only_after_covered_refresh(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "f.txt").write_text("legacy", encoding="utf-8")
    captured = service.snapshot(str(repo))
    legacy_manifest = json.dumps({"nested/f.txt": "a" * 64})
    provider_row: dict[str, object] = {
        "id": 9,
        "name": "base",
        "kind": "base",
        "source_terminal_id": "base-term",
        "session_uuid": "uuid",
        "cwd": str(repo),
        "git_sha": captured.git_sha,
        "dirty_hashes": legacy_manifest,
        "digest_head": None,
    }

    assert service.staleness(provider_row).changed_count == 0
    assert provider_row["dirty_hashes"] == legacy_manifest

    shutil.rmtree(nested)
    pending = service.staleness(provider_row)
    assert pending.delta.entries == (service.SnapshotEntry("nested/f.txt", "absent"),)
    assert isinstance(
        base_digest_service.evaluate(provider_row, pending.delta), base_digest_service.DigestPending
    )
    artifact = base_digest_service.publish(
        base="base",
        cwd=str(repo),
        parent_artifact_sha=None,
        delta=pending.delta,
        body="legacy key removed",
        round_number=1,
    )

    async def inline(
        _terminal: str,
        _generation: str,
        _kind: str,
        _operation: str,
        function,
        *args,
        deadline=None,
        **kwargs,
    ):
        return function(*args, **kwargs), time.monotonic()

    async def ready(*_args, **_kwargs) -> bool:
        return True

    def update(_row_id: int, *, git_sha: str, dirty_hashes: str, digest_head=None):
        provider_row.update(git_sha=git_sha, dirty_hashes=dirty_hashes, digest_head=digest_head)
        return dict(provider_row)

    terminals._fork_refresh_locks.clear()
    monkeypatch.setattr(terminals, "_tracked_blocking", inline)
    monkeypatch.setattr(terminals, "get_ready_provider_session", lambda _name: dict(provider_row))
    monkeypatch.setattr(terminals, "update_provider_session_snapshot", update)
    monkeypatch.setattr(terminals, "_dispatch_base_refresh", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(terminals, "_wait_for_base_ready", ready)
    monkeypatch.setattr(terminals.status_monitor, "get_input_gen", lambda _id: 1)

    preamble = await terminals._prepare_fork_refresh(
        "worker", "generation", "base", "[STALE]", None, {}
    )

    assert preamble.startswith("[FRESH]")
    assert provider_row["digest_head"] == artifact.artifact_sha
    assert "nested/f.txt" not in json.loads(str(provider_row["dirty_hashes"]))


def test_t2f_marker_oserror_fails_open_but_real_unhashable_still_blocks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "hashable.txt").write_text("hash me", encoding="utf-8")
    (repo / "loop").symlink_to("loop")
    manifest = json.dumps({"legacy/missing.txt": "a" * 64})
    original_stat = Path.stat

    def marker_stat(path: Path, *args, **kwargs):
        if path.name == ".git" and path.parent.name in {"legacy", "hashable.txt"}:
            raise OSError("marker unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", marker_stat)
    stale = service.staleness(row(repo, service.snapshot(str(repo)).git_sha, manifest))
    entries = {entry.path: entry for entry in stale.delta.entries}

    assert entries["legacy/missing.txt"].state == "absent"
    assert entries["hashable.txt"].state == "sha256"
    assert entries["loop"].state == "unhashable"
    coverable = SnapshotDelta(
        stale.delta.git_sha,
        tuple(entry for entry in stale.delta.entries if entry.path != "loop"),
    )
    artifact = base_digest_service.BaseDigestArtifact(
        path=repo / "digest.md",
        base="base",
        parent_artifact_sha="genesis",
        artifact_sha="b" * 64,
        entries=coverable.entries,
        body="coverable",
    )
    assert base_digest_service.covers(artifact, coverable)
    assert not base_digest_service.covers(artifact, stale.delta)


def test_t2g_lexical_intermediate_symlink_follows_marker_and_loop_fails_open(
    repo: Path,
) -> None:
    target = repo / "target"
    target.mkdir()
    git(target, "init", "-q")
    (target / "file.txt").write_text("nested", encoding="utf-8")
    (repo / "link").symlink_to(target.name)

    assert service._nested_repo(str(repo), "link/file.txt", {})

    (repo / "loopdir").symlink_to("loopdir")
    assert not service._nested_repo(str(repo), "loopdir/file.txt", {})
    assert service._entry_for_path(str(repo), "loopdir/file.txt").state == "unhashable"


def test_t3_hash_failure_stays_unhashable_and_blocks_coverage(repo: Path) -> None:
    (repo / "unreadable.txt").write_text("content", encoding="utf-8")
    with patch.object(service, "_hash", side_effect=OSError("unreadable")):
        captured = service.snapshot(str(repo))

    assert captured.entries == (service.SnapshotEntry("unreadable.txt", "unhashable"),)
    artifact = base_digest_service.BaseDigestArtifact(
        path=repo / "digest.md",
        base="base",
        parent_artifact_sha="genesis",
        artifact_sha="c" * 64,
        entries=captured.entries,
        body="cannot cover",
    )
    assert not base_digest_service.covers(artifact, captured)


def test_t4_exclusion_log_is_single_bounded_and_silent_otherwise(
    repo: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    nested = repo / "nested"
    nested.mkdir()
    (nested / ".git").write_text("", encoding="utf-8")
    candidates = [f"nested/file-{index:02d}.txt" for index in range(12)]
    for candidate in candidates:
        (repo / candidate).write_text("nested", encoding="utf-8")

    def git_bytes(_cwd: str, *args: str) -> bytes:
        if args[0] == "rev-parse":
            return b"a" * 40 + b"\n"
        if args[0] == "diff":
            return b"\0".join(path.encode() for path in candidates) + b"\0"
        return b""

    original_stat = Path.stat
    marker_calls = 0

    def counting_stat(path: Path, *args, **kwargs):
        nonlocal marker_calls
        if path == nested / ".git":
            marker_calls += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(service, "_run_git_bytes", git_bytes)
    monkeypatch.setattr(Path, "stat", counting_stat)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    assert service.snapshot(str(repo)).entries == ()
    records = [record.getMessage() for record in caplog.records if record.name == LOGGER_NAME]
    expected_paths = ", ".join(candidates[:10])
    assert records == [f"nested_repo_excluded count=12 paths={expected_paths}"]
    assert marker_calls == 1

    caplog.clear()
    candidates[:] = ["ordinary.txt"]
    (repo / "ordinary.txt").write_text("ordinary", encoding="utf-8")
    assert service.snapshot(str(repo)).paths == ("ordinary.txt",)
    assert not [record for record in caplog.records if record.name == LOGGER_NAME]

    caplog.clear()
    candidates[:] = ["opaque"]
    (repo / "opaque").mkdir()

    def failing_stat(path: Path, *args, **kwargs):
        if path == repo / "opaque" / ".git":
            raise OSError("marker unreadable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    opaque = service.snapshot(str(repo))
    assert opaque.entries == (service.SnapshotEntry("opaque", "unhashable"),)
    assert not [record for record in caplog.records if record.name == LOGGER_NAME]

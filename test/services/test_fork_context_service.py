import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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
    with patch(
        "cli_agent_orchestrator.services.fork_context_service._hash",
        side_effect=PermissionError("unreadable"),
    ):
        stale = staleness(row(repo, captured.git_sha, captured.dirty_hashes()))
    assert stale.delta.paths == ("tracked.txt",)
    assert stale.preamble.startswith("[STALE]")


def test_f80_mark_ready_warns_on_dirty_tree(repo: Path, caplog):
    """F80: mark_ready logs WARNING when tree is dirty."""
    import logging

    from cli_agent_orchestrator.services import fork_context_service as fcs

    (repo / "tracked.txt").write_text("dirty")
    with (
        patch.object(
            fcs,
            "get_terminal_metadata",
            return_value={
                "provider": "grok_cli",
                "provider_session_id": "session",
                "working_directory": str(repo),
                "agent_profile": "dev",
                "tmux_session": "cao-s",
                "tmux_window": "w",
            },
        ),
        patch.object(fcs, "register_provider_session", side_effect=lambda **kw: kw),
        patch.object(fcs, "update_terminal_provider_session_id", return_value=None),
        caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.fork_context_service"
        ),
    ):
        result = fcs.mark_ready("terminal", "base", None)

    assert result["_entry_count"] > 0
    assert any("base_registered_dirty" in rec.message for rec in caplog.records)


def test_f78_mark_ready_warns_on_manifest_near_cap(repo: Path, caplog):
    """F78: mark_ready logs WARNING when manifest is near budget cap."""
    import logging

    from cli_agent_orchestrator.services import fork_context_service as fcs
    from cli_agent_orchestrator.services.base_digest_service import MAX_DIGEST_BYTES

    # Create many dirty files to push manifest near cap
    for i in range(80):
        (repo / f"file_{i:04d}_with_a_longer_name_to_push_bytes.py").write_text(f"dirty{i}")

    with (
        patch.object(
            fcs,
            "get_terminal_metadata",
            return_value={
                "provider": "grok_cli",
                "provider_session_id": "session",
                "working_directory": str(repo),
                "agent_profile": "dev",
                "tmux_session": "cao-s",
                "tmux_window": "w",
            },
        ),
        patch.object(fcs, "register_provider_session", side_effect=lambda **kw: kw),
        patch.object(fcs, "update_terminal_provider_session_id", return_value=None),
        caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.fork_context_service"
        ),
    ):
        result = fcs.mark_ready("terminal", "base", None)

    # Check whether we hit the threshold — if manifest is >80% cap, warning is logged
    if result["_projected_manifest_bytes"] > MAX_DIGEST_BYTES * 0.8:
        assert any("base_manifest_near_cap" in rec.message for rec in caplog.records)


def test_f26_ac5_mark_ready_deleted_cwd_raises_directed_fork_context_error():
    """AC5: mark_ready with a deleted base cwd raises ForkContextError naming
    'worker deleted its own cwd' — never the opaque snapshot_git-failure."""
    from cli_agent_orchestrator.services import fork_context_service as fcs
    from cli_agent_orchestrator.services.fork_context_service import ForkContextError

    deleted = "/tmp/f26-definitely-not-a-real-dir"

    def fake_exists(path):
        return False

    with (
        patch.object(
            fcs,
            "get_terminal_metadata",
            return_value={
                "provider": "grok_cli",
                "provider_session_id": "session",
                "working_directory": None,
                "agent_profile": "dev",
                "tmux_session": "cao-s",
                "tmux_window": "w",
            },
        ),
        patch("cli_agent_orchestrator.backends.registry.get_backend") as mock_registry_backend,
        patch(
            "cli_agent_orchestrator.services.fork_context_service.os.path.exists",
            side_effect=fake_exists,
        ),
    ):
        mock_registry_backend.return_value.get_pane_working_directory.return_value = deleted
        try:
            fcs.mark_ready("terminal", "base", None)
        except ForkContextError as exc:
            assert "worker deleted its own cwd" in str(exc), str(exc)
            assert deleted in str(exc)
            assert "snapshot" not in str(exc)
            assert "git-failure" not in str(exc)
        else:
            pytest.fail("expected ForkContextError for deleted cwd")


# ===========================================================================
# F545 (#401): first_pane / pane_pid resolve the window's FIRST pane
# (lowest pane_index), NOT the active pane. Pre-fix, pane_pid() ran
# `display-message -t <session>:<window> '#{pane_pid}'`, which tmux resolves to
# the ACTIVE pane — so a split seat window with a focused second pane returned
# the wrong process tree.
# ===========================================================================


def _fake_list_panes(stdout: str):
    """Return a fake subprocess.run result carrying list-panes stdout."""

    def _run(argv, *args, **kwargs):
        assert "list-panes" in argv, f"expected list-panes invocation, got {argv}"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return _run


def test_first_pane_selects_lowest_index_not_active():
    """Second pane is active, but first_pane returns the lowest-index pane (%0)."""
    from cli_agent_orchestrator.services.fork_context_service import first_pane, pane_pid

    # list-panes emits both panes; pane index 1 (%18) is the active one.
    stdout = "1 %18 377867\n0 %17 377853\n"
    with patch(
        "cli_agent_orchestrator.services.fork_context_service.subprocess.run",
        side_effect=_fake_list_panes(stdout),
    ):
        assert first_pane("cao-test", "win-0") == ("%17", 377853)
        assert pane_pid("cao-test", "win-0") == 377853


def test_first_pane_single_pane():
    """Single-pane window returns that pane."""
    from cli_agent_orchestrator.services.fork_context_service import first_pane, pane_pid

    with patch(
        "cli_agent_orchestrator.services.fork_context_service.subprocess.run",
        side_effect=_fake_list_panes("0 %2 8071\n"),
    ):
        assert first_pane("cao-test", "win-0") == ("%2", 8071)
        assert pane_pid("cao-test", "win-0") == 8071


def test_first_pane_no_panes_raises():
    """Empty list-panes output raises ValueError (surfaces as pane_pid_failed upstream)."""
    from cli_agent_orchestrator.services.fork_context_service import first_pane

    with patch(
        "cli_agent_orchestrator.services.fork_context_service.subprocess.run",
        side_effect=_fake_list_panes(""),
    ):
        with pytest.raises(ValueError):
            first_pane("cao-test", "win-0")

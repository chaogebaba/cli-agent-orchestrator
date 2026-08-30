"""issue #100 Phase 1 -- worktree_service tests.

Covers:
- ``find_repo_root``: resolves from a subdirectory, raises outside a repo.
- ``create_worktree`` / ``remove_worktree``: real ``git worktree add``/
  ``remove``/branch-delete against a real local repo -- no subprocess mocking,
  same posture as ``test_project_identity.py``'s own real-git tests.
- ``remove_worktree`` tolerates uncommitted/untracked content left behind
  (agents commonly leave modified files -- ``--force`` is required for this).
- ``list_worktrees`` reflects real ``git worktree`` state, including entries
  ``create_worktree`` did not itself create.
- ``parse_worktree_path`` round-trips against paths ``worktree_path_for``
  produces, and rejects unrelated paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.worktree_service import (
    WorktreeError,
    branch_for,
    create_worktree,
    find_repo_root,
    list_worktrees,
    parse_worktree_path,
    remove_worktree,
    resolve_worktree_root,
    worktree_path_for,
)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _git_available(), reason="git executable required")


@pytest.fixture(autouse=True)
def _force_in_repo_worktree_root(monkeypatch):
    """Default every test to the in-repo worktree root (F620 #476).

    Root resolution now consults ``CAO_WORKTREE_ROOT`` / providers.toml /
    ``/data/cao-scratch`` writability, all of which are ambient on the host
    running the suite. Pin them off by default so ``worktree_path_for`` and the
    real-git create/list tests behave exactly as before F620 regardless of the
    box's environment. Tests that exercise the precedence opt back in with
    their own monkeypatches (which override these).
    """
    monkeypatch.delenv("CAO_WORKTREE_ROOT", raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
        lambda: None,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
        lambda: False,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=path, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestFindRepoRoot:
    def test_resolves_from_repo_root_itself(self, repo: Path) -> None:
        assert find_repo_root(str(repo)) == str(repo.resolve())

    def test_resolves_from_a_subdirectory(self, repo: Path) -> None:
        subdir = repo / "src" / "pkg"
        subdir.mkdir(parents=True)
        assert find_repo_root(str(subdir)) == str(repo.resolve())

    def test_raises_outside_any_git_repository(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError, match="is not inside a git repository"):
            find_repo_root(str(non_repo))

    def test_raises_worktree_error_not_a_raw_os_error_for_a_nonexistent_path(
        self, tmp_path: Path
    ) -> None:
        """Regression: a nonexistent start_path (e.g. a typo'd
        working_directory) must surface as the same clean WorktreeError as
        'exists but isn't a repo' -- not an uncaught FileNotFoundError from
        subprocess.run's own cwd resolution, which would reach the API
        boundary as an unhandled 500 instead of the intended 400."""
        nonexistent = tmp_path / "does" / "not" / "exist"
        with pytest.raises(WorktreeError):
            find_repo_root(str(nonexistent))

    def test_f26_ac6_deleted_cwd_raises_directed_worktree_error(self, tmp_path: Path) -> None:
        """AC6: find_repo_root with a deleted start_path raises WorktreeError
        naming 'worker deleted its own cwd' — NOT the generic
        'not inside a git repository'."""
        deleted = tmp_path / "f26" / "worker-deleted" / "cwd"
        with pytest.raises(WorktreeError, match="worker deleted its own cwd"):
            find_repo_root(str(deleted))

    def test_f26_ac7_no_rescue_never_substitutes_repo_root(self, tmp_path: Path) -> None:
        """AC7: on a deleted cwd, no fs mutation and no fallback path is
        returned — the directed error fires instead of a silent relocation."""
        from unittest.mock import patch

        deleted = tmp_path / "f26" / "no-rescue"
        with patch(
            "cli_agent_orchestrator.services.fork_context_service.os.path.exists",
            side_effect=lambda p: False,
        ):
            with pytest.raises(WorktreeError, match="worker deleted its own cwd"):
                find_repo_root(str(deleted))
        # nothing was created / no fallback returned: the dir still does not exist
        assert not deleted.exists()


class TestCreateAndRemoveWorktree:
    def test_create_worktree_produces_a_real_checkout_on_its_own_branch(self, repo: Path) -> None:
        terminal_id = "term_abc123"
        path = create_worktree(str(repo), terminal_id)

        assert Path(path) == Path(worktree_path_for(str(repo), terminal_id))
        assert (Path(path) / "README.md").is_file()  # real checkout of HEAD's tree

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert branch_result.stdout.strip() == branch_for(terminal_id)

        # git itself agrees this is a real worktree of `repo`.
        list_result = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True
        )
        assert path in list_result.stdout

    def test_create_worktree_raises_a_clear_error_outside_a_git_repository(
        self, tmp_path: Path
    ) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError):
            create_worktree(str(non_repo), "term_xyz")

    def test_create_worktree_gitignores_the_worktrees_subdir(self, repo: Path) -> None:
        # Without this, every `git status` in the main checkout shows the
        # provisioned worktree as an untracked/embedded-repo gitlink, and
        # `git add -A` there (agents do this constantly) stages it.
        create_worktree(str(repo), "term_first")

        gitignore = repo / ".cao" / "worktrees" / ".gitignore"
        assert gitignore.is_file()
        assert gitignore.read_text() == "*\n"

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
        )
        assert ".cao" not in status.stdout

    def test_create_worktree_does_not_clobber_an_existing_gitignore(self, repo: Path) -> None:
        create_worktree(str(repo), "term_first")
        gitignore = repo / ".cao" / "worktrees" / ".gitignore"
        gitignore.write_text("custom\n")

        create_worktree(str(repo), "term_second")

        assert gitignore.read_text() == "custom\n"

    def test_remove_worktree_deletes_the_directory_and_the_branch(self, repo: Path) -> None:
        terminal_id = "term_clean01"
        path = create_worktree(str(repo), terminal_id)

        remove_worktree(str(repo), terminal_id)

        assert not Path(path).exists()
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch_for(terminal_id)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert branch_result.stdout.strip() == ""

    def test_remove_worktree_retains_a_branch_with_unmerged_commits(self, repo: Path) -> None:
        """A worker that committed real work to its branch before completing
        must not have that history destroyed just because Phase 1 has no
        merge-back story -- the worktree's working-tree contents are force
        -discarded, but the branch itself is only safe-deleted (``git branch
        -d``), which refuses when there are commits that would be lost."""
        terminal_id = "term_committed01"
        path = create_worktree(str(repo), terminal_id)
        (Path(path) / "result.txt").write_text("important output\n")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "worker output"],
            cwd=path,
            check=True,
            capture_output=True,
        )

        remove_worktree(str(repo), terminal_id)  # must not raise

        assert not Path(path).exists()  # worktree checkout itself is gone
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch_for(terminal_id)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        # Branch survives -- a leak for Phase 3 to sweep, not data loss.
        assert branch_for(terminal_id) in branch_result.stdout

    def test_remove_worktree_force_removes_uncommitted_and_untracked_content(
        self, repo: Path
    ) -> None:
        """Agents commonly leave modified/untracked files behind -- a plain
        (non-force) ``git worktree remove`` refuses in that case; this must
        not surface as a failure to the caller (teardown paths call this
        best-effort and must never raise)."""
        terminal_id = "term_dirty01"
        path = create_worktree(str(repo), terminal_id)
        (Path(path) / "scratch.txt").write_text("uncommitted work\n")
        (Path(path) / "README.md").write_text("modified\n")

        remove_worktree(str(repo), terminal_id)  # must not raise

        assert not Path(path).exists()

    def test_remove_worktree_on_an_already_removed_worktree_does_not_raise(
        self, repo: Path
    ) -> None:
        terminal_id = "term_gone01"
        create_worktree(str(repo), terminal_id)
        remove_worktree(str(repo), terminal_id)

        remove_worktree(str(repo), terminal_id)  # second call: must not raise

    def test_remove_worktree_on_a_nonexistent_repo_root_does_not_raise(self) -> None:
        """Regression: this function's own docstring promises 'never raises'
        (both terminal_service.delete_terminal's teardown path and
        create_terminal's own failure-cleanup path call it with no
        try/except, relying on that contract). A repo_root that no longer
        exists on disk (e.g. the parent clone was itself deleted between
        worktree creation and teardown) previously raised an uncaught
        FileNotFoundError from subprocess.run's own cwd resolution."""
        remove_worktree("/definitely/not/a/real/repo/root/anywhere", "term_x")  # must not raise


class TestListWorktrees:
    def test_lists_the_main_checkout_and_every_created_worktree(self, repo: Path) -> None:
        create_worktree(str(repo), "term_one")
        create_worktree(str(repo), "term_two")

        entries = list_worktrees(str(repo))

        paths = {e["worktree"] for e in entries if "worktree" in e}
        assert str(repo.resolve()) in paths
        assert worktree_path_for(str(repo), "term_one") in paths
        assert worktree_path_for(str(repo), "term_two") in paths

    def test_raises_outside_a_git_repository(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError):
            list_worktrees(str(non_repo))


class TestParseWorktreePath:
    def test_round_trips_against_worktree_path_for(self) -> None:
        repo_root = "/home/user/myrepo"
        terminal_id = "term_9f8e7d"
        path = worktree_path_for(repo_root, terminal_id)

        parsed = parse_worktree_path(path)

        assert parsed == (repo_root, terminal_id)

    def test_returns_none_for_a_path_outside_any_worktree_subdir(self) -> None:
        assert parse_worktree_path("/home/user/myrepo/src/pkg") is None

    def test_returns_none_for_none(self) -> None:
        assert parse_worktree_path(None) is None

    def test_returns_none_for_the_worktrees_dir_itself_with_no_terminal_segment(self) -> None:
        assert parse_worktree_path("/home/user/myrepo/.cao/worktrees/") is None

    def test_accepts_a_subdirectory_of_the_worktree(self) -> None:
        # tmux reports the pane's CURRENT directory (pane_current_path); a
        # worker that `cd`s into a subdirectory of its own worktree (very
        # likely) must still be recognized as worktree-backed at teardown,
        # or the worktree/branch leaks silently.
        assert parse_worktree_path("/home/user/myrepo/.cao/worktrees/term_x/extra") == (
            "/home/user/myrepo",
            "term_x",
        )
        assert parse_worktree_path("/home/user/myrepo/.cao/worktrees/term_x/deeply/nested/dir") == (
            "/home/user/myrepo",
            "term_x",
        )

    def test_resolves_the_innermost_worktree_under_nesting(self) -> None:
        # A worktree-backed supervisor (terminal A) spawning a worktree-backed
        # worker (terminal B) nests B's worktree under A's:
        # <repo_root>/.cao/worktrees/A/.cao/worktrees/B. `rfind` (last
        # marker occurrence) must resolve to B's own (repo_root, terminal_id)
        # -- repo_root being A's worktree root -- not fail to parse (`find`,
        # first occurrence, would yield terminal_id "A/.cao/worktrees/B",
        # rejected for containing a path separator, leaking B).
        nested = "/home/user/myrepo/.cao/worktrees/term_a/.cao/worktrees/term_b"
        assert parse_worktree_path(nested) == (
            "/home/user/myrepo/.cao/worktrees/term_a",
            "term_b",
        )


class TestResolveWorktreeRoot:
    """F620 (#476): worktree root precedence — env > toml > /data default > in-repo."""

    _REPO = "/home/user/myrepo"

    def test_env_var_wins_over_everything(self, monkeypatch) -> None:
        # Case 1: CAO_WORKTREE_ROOT set — used verbatim, off-repo, even when
        # a toml root and a writable /data would otherwise apply.
        monkeypatch.setenv("CAO_WORKTREE_ROOT", "/custom/env/root")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: "/from/toml",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: True,
        )
        root, in_repo = resolve_worktree_root(self._REPO)
        assert root == "/custom/env/root"
        assert in_repo is False

    def test_providers_toml_wins_when_env_unset(self, monkeypatch) -> None:
        # Case 2: no env, providers.toml [worktrees] root set — used verbatim.
        monkeypatch.delenv("CAO_WORKTREE_ROOT", raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: "/from/toml",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: True,
        )
        root, in_repo = resolve_worktree_root(self._REPO)
        assert root == "/from/toml"
        assert in_repo is False

    def test_data_scratch_default_when_writable_and_nothing_configured(self, monkeypatch) -> None:
        # Case 3: no env, no toml, /data/cao-scratch writable — namespaced by
        # repo basename under /data/cao-scratch/worktrees.
        monkeypatch.delenv("CAO_WORKTREE_ROOT", raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: True,
        )
        root, in_repo = resolve_worktree_root(self._REPO)
        assert root == "/data/cao-scratch/worktrees/myrepo"
        assert in_repo is False

    def test_in_repo_fallback_when_data_absent_and_nothing_configured(self, monkeypatch) -> None:
        # Case 4: no env, no toml, /data/cao-scratch NOT writable — falls back
        # to the pre-F620 in-repo <repo>/.cao/worktrees (in_repo=True).
        monkeypatch.delenv("CAO_WORKTREE_ROOT", raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: False,
        )
        root, in_repo = resolve_worktree_root(self._REPO)
        assert root == "/home/user/myrepo/.cao/worktrees"
        assert in_repo is True

    def test_empty_env_var_is_ignored(self, monkeypatch) -> None:
        # A blank CAO_WORKTREE_ROOT must not be treated as a configured root.
        monkeypatch.setenv("CAO_WORKTREE_ROOT", "   ")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: False,
        )
        root, in_repo = resolve_worktree_root(self._REPO)
        assert in_repo is True


class TestOffRepoWorktreeLifecycle:
    """F620: a create+teardown cycle with the checkout physically off-repo."""

    def test_create_places_checkout_off_repo_and_teardown_uses_git_truth(
        self, repo: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # Point the root at an off-repo scratch dir (mimics /data/cao-scratch).
        scratch_root = tmp_path / "scratch" / "worktrees" / "myrepo"
        monkeypatch.setenv("CAO_WORKTREE_ROOT", str(scratch_root))

        terminal_id = "offrepo1"
        path = create_worktree(str(repo), terminal_id)

        # Checkout landed under the off-repo root, NOT inside the repo.
        assert path == str(scratch_root / terminal_id)
        assert Path(path).is_dir()
        assert not (repo / ".cao" / "worktrees").exists()  # no in-repo dir
        # No .gitignore is written for an off-repo root.
        assert not (scratch_root / ".gitignore").exists()

        # git's own bookkeeping (.git/worktrees) is the truth: the worktree
        # appears in `git worktree list` run from the real repo root.
        listed = [w.get("worktree") for w in list_worktrees(str(repo))]
        assert any(str(scratch_root / terminal_id) in str(p) for p in listed)

        # Teardown from the real repo root (passing the stored path) removes it.
        remove_worktree(str(repo), terminal_id, worktree_path=path)
        assert not Path(path).exists()
        listed_after = [w.get("worktree") for w in list_worktrees(str(repo))]
        assert not any(str(scratch_root / terminal_id) in str(p) for p in listed_after)

    def test_in_repo_case_still_writes_gitignore(self, repo: Path, monkeypatch) -> None:
        # Force in-repo resolution; the .gitignore write must still happen.
        monkeypatch.delenv("CAO_WORKTREE_ROOT", raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._providers_toml_worktree_root",
            lambda: None,
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.worktree_service._data_scratch_writable",
            lambda: False,
        )
        create_worktree(str(repo), "inrepo01")
        assert (repo / ".cao" / "worktrees" / ".gitignore").read_text() == "*\n"

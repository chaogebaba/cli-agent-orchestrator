"""Tests for the resolved-Python verification helper and worker contract."""

from __future__ import annotations

import filecmp
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_resolved_python.py"
CANONICAL_SKILL = REPO_ROOT / "skills" / "cao-worker-protocols" / "SKILL.md"
PACKAGE_SKILL = (
    REPO_ROOT / "src" / "cli_agent_orchestrator" / "skills" / "cao-worker-protocols" / "SKILL.md"
)
F26_PREVENTION = (
    "Never `cd` into a directory you may later delete; run cleanup from outside the "
    "disposable directory."
)
F26_REPORT = (
    "If every command fails with `getcwd`/`ENOENT`, stop issuing commands and report the cwd "
    "brick to your supervisor via `send_message` immediately; do not retry."
)
COMPILE_HELPER_REFERENCE = "python scripts/verify_resolved_python.py --all-changed"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_verify_resolved_python", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "wpq9@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "WPQ9 Test"], cwd=root, check=True)


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


@pytest.fixture
def helper(tmp_path, monkeypatch):
    module = _load_helper()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    return module


def test_passing_python_file(helper, tmp_path):
    path = tmp_path / "good.py"
    path.write_text("VALUE = 1\n")

    assert helper.main([str(path)]) == 0


def test_syntax_error_returns_nonzero_and_names_file(helper, tmp_path, capsys):
    path = tmp_path / "bad_syntax.py"
    path.write_text("VALUE = (\n")

    assert helper.main([str(path)]) != 0
    assert "bad_syntax.py" in capsys.readouterr().err


def test_collect_error_returns_nonzero_and_names_test_file(helper, tmp_path, capsys):
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    path = test_dir / "test_collect_error.py"
    path.write_text('raise RuntimeError("collect boom")\n')

    assert helper.main([str(path)]) != 0
    assert "test/test_collect_error.py" in capsys.readouterr().err


def test_conflict_marker_returns_nonzero_and_names_file(helper, tmp_path, capsys):
    path = tmp_path / "conflicted.py"
    path.write_text("<<<<<<< ours\nVALUE = 1\n=======\nVALUE = 2\n>>>>>>> theirs\n")

    assert helper.main([str(path)]) != 0
    assert "conflicted.py" in capsys.readouterr().err


def test_all_changed_includes_untracked_new_test(helper, tmp_path, capsys):
    _init_git_repo(tmp_path)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("VALUE = 1\n")
    _commit_all(tmp_path)
    test_dir = tmp_path / "test"
    test_dir.mkdir()
    untracked = test_dir / "test_untracked.py"
    untracked.write_text("VALUE = (\n")

    assert helper.main(["--all-changed"]) != 0
    assert "test/test_untracked.py" in capsys.readouterr().err


def test_all_changed_excludes_deleted_tracked_file(helper, tmp_path, capsys):
    _init_git_repo(tmp_path)
    deleted = tmp_path / "deleted.py"
    deleted.write_text("VALUE = 1\n")
    _commit_all(tmp_path)
    deleted.unlink()

    assert helper.main(["--all-changed"]) == 0
    output = capsys.readouterr()
    assert "deleted.py" not in output.out + output.err


def test_worker_skill_contains_f26_prevention_and_report_contract():
    text = CANONICAL_SKILL.read_text()

    assert F26_PREVENTION in text
    assert F26_REPORT in text


def test_worker_skill_references_compile_helper():
    assert COMPILE_HELPER_REFERENCE in CANONICAL_SKILL.read_text()


def test_worker_skill_package_mirror_is_byte_identical():
    assert filecmp.cmp(CANONICAL_SKILL, PACKAGE_SKILL, shallow=False)

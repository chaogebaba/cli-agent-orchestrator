"""F620 (#476) — laptop shims + worker-terminal PATH-injection seam.

Two layers under test:

1. The shell shims ``scripts/laptop-shims/{pytest,mypy,uv}`` — real subprocess
   invocations asserting deny (exit 97), LAPTOP_OK allow/passthrough, and uv's
   selective passthrough (sync/venv/run-pytest/run-mypy denied, everything else
   through). Unit-tier safe: no network, no venv, no real pytest/mypy run.

2. The Python seam ``laptop_shim.should_inject_shim`` / ``maybe_shim_env`` —
   the decision CAO makes when composing a worker terminal's env: worker vs
   supervisor, boxes.tsv present/active/absent, LAPTOP_OK override.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import laptop_shim

# Repo root: this file is <repo>/test/services/test_laptop_shim.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIM_DIR = _REPO_ROOT / "scripts" / "laptop-shims"

_DENY_MSG = "LAPTOP-DENIED: run on a grok box via scripts/box-run.sh (set LAPTOP_OK=1 to override)"


def _run_shim(name: str, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    shim = _SHIM_DIR / name
    full_env = dict(os.environ)
    # Strip any inherited LAPTOP_OK so the default (deny) is deterministic.
    full_env.pop("LAPTOP_OK", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [str(shim), *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Shim shell scripts (subprocess)
# ---------------------------------------------------------------------------


class TestPytestMypyShims:
    @pytest.mark.parametrize("name", ["pytest", "mypy"])
    def test_denies_by_default_exit_97(self, name: str) -> None:
        result = _run_shim(name, ["--version"])
        assert result.returncode == 97
        assert _DENY_MSG in result.stderr

    @pytest.mark.parametrize("name", ["pytest", "mypy"])
    def test_laptop_ok_passthrough_execs_real_binary(self, name: str, tmp_path: Path) -> None:
        # Provide a fake real binary on PATH (after the shim dir) so the shim's
        # PATH-stripping discovery finds it and execs it.
        real = tmp_path / name
        real.write_text('#!/usr/bin/env bash\necho REAL-$0-"$@"\nexit 0\n')
        real.chmod(0o755)
        env = {
            "LAPTOP_OK": "1",
            "PATH": f"{_SHIM_DIR}{os.pathsep}{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
        result = _run_shim(name, ["--flag"], env=env)
        assert result.returncode == 0
        assert "REAL-" in result.stdout
        assert _DENY_MSG not in result.stderr


class TestUvShim:
    @pytest.mark.parametrize("args", [["sync"], ["venv"], ["run", "pytest"], ["run", "mypy"]])
    def test_denies_heavy_subcommands_exit_97(self, args: list[str]) -> None:
        result = _run_shim("uv", args)
        assert result.returncode == 97, f"expected deny for uv {args}"
        assert _DENY_MSG in result.stderr

    def test_denies_heavy_subcommand_behind_global_flag(self) -> None:
        # `uv --quiet sync` must still be recognized as sync.
        result = _run_shim("uv", ["--quiet", "sync"])
        assert result.returncode == 97
        assert _DENY_MSG in result.stderr

    @pytest.mark.parametrize("args", [["pip", "list"], ["tree"], ["run", "echo", "hi"], ["lock"]])
    def test_passes_through_non_heavy_subcommands(self, args: list[str], tmp_path: Path) -> None:
        # Fake `uv` that prints its args; the shim must exec it unchanged.
        fake_uv = tmp_path / "uv"
        fake_uv.write_text('#!/usr/bin/env bash\necho PASSTHRU:"$@"\nexit 0\n')
        fake_uv.chmod(0o755)
        env = {"PATH": f"{_SHIM_DIR}{os.pathsep}{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin"}
        result = _run_shim("uv", args, env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("PASSTHRU:")
        assert _DENY_MSG not in result.stderr

    def test_laptop_ok_passes_heavy_subcommand_through(self, tmp_path: Path) -> None:
        fake_uv = tmp_path / "uv"
        fake_uv.write_text('#!/usr/bin/env bash\necho PASSTHRU:"$@"\nexit 0\n')
        fake_uv.chmod(0o755)
        env = {
            "LAPTOP_OK": "1",
            "PATH": f"{_SHIM_DIR}{os.pathsep}{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin",
        }
        result = _run_shim("uv", ["sync"], env=env)
        assert result.returncode == 0
        assert result.stdout.startswith("PASSTHRU:sync")


# ---------------------------------------------------------------------------
# Python decision seam (should_inject_shim / maybe_shim_env)
# ---------------------------------------------------------------------------


def _make_repo_with_boxes(tmp_path: Path, boxes_content: str | None) -> Path:
    """A fake repo root with scripts/laptop-shims and optional scripts/boxes.tsv."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "laptop-shims").mkdir(parents=True)
    if boxes_content is not None:
        (repo / "scripts" / "boxes.tsv").write_text(boxes_content)
    return repo


_ACTIVE_TSV = textwrap.dedent("""\
    # comment line
    box@grok-box-1\tfrozen\t2026-08-27\tin use elsewhere
    box@grok-box-2\tactive\t-
    """)
_ALL_FROZEN_TSV = textwrap.dedent("""\
    box@grok-box-1\tfrozen\t2026-08-27\tin use elsewhere
    """)


class TestShouldInjectShim:
    def test_worker_with_active_box_and_no_laptop_ok_injects(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        assert laptop_shim.should_inject_shim(is_worker=True, repo_root=str(repo), env={}) is True

    def test_supervisor_never_injected(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        assert laptop_shim.should_inject_shim(is_worker=False, repo_root=str(repo), env={}) is False

    def test_boxes_tsv_absent_no_injection(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, None)  # no boxes.tsv
        assert laptop_shim.should_inject_shim(is_worker=True, repo_root=str(repo), env={}) is False

    def test_all_frozen_no_active_row_no_injection(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, _ALL_FROZEN_TSV)
        assert laptop_shim.should_inject_shim(is_worker=True, repo_root=str(repo), env={}) is False

    def test_laptop_ok_set_no_injection(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        assert (
            laptop_shim.should_inject_shim(
                is_worker=True, repo_root=str(repo), env={"LAPTOP_OK": "1"}
            )
            is False
        )

    def test_no_repo_root_no_injection(self) -> None:
        assert laptop_shim.should_inject_shim(is_worker=True, repo_root=None, env={}) is False

    def test_missing_shim_dir_no_injection(self, tmp_path: Path) -> None:
        # boxes.tsv active but no laptop-shims dir on disk.
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "boxes.tsv").write_text(_ACTIVE_TSV)
        assert laptop_shim.should_inject_shim(is_worker=True, repo_root=str(repo), env={}) is False


class TestMaybeShimEnv:
    def test_worker_env_gets_shim_prefixed_path(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("LAPTOP_OK", raising=False)
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        extra_env: dict[str, str] = {}
        out = laptop_shim.maybe_shim_env(
            extra_env, is_worker=True, repo_root=str(repo), base_path="/usr/bin:/bin"
        )
        shim_dir = str(repo / "scripts" / "laptop-shims")
        assert out["PATH"] == f"{shim_dir}{os.pathsep}/usr/bin:/bin"

    def test_supervisor_env_unchanged_no_path_key(self, tmp_path: Path) -> None:
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        extra_env: dict[str, str] = {}
        out = laptop_shim.maybe_shim_env(
            extra_env, is_worker=False, repo_root=str(repo), base_path="/usr/bin:/bin"
        )
        assert "PATH" not in out

    def test_boxes_absent_env_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("LAPTOP_OK", raising=False)
        repo = _make_repo_with_boxes(tmp_path, None)
        out = laptop_shim.maybe_shim_env(
            {}, is_worker=True, repo_root=str(repo), base_path="/usr/bin:/bin"
        )
        assert "PATH" not in out

    def test_idempotent_no_double_prefix(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("LAPTOP_OK", raising=False)
        repo = _make_repo_with_boxes(tmp_path, _ACTIVE_TSV)
        shim_dir = str(repo / "scripts" / "laptop-shims")
        out1 = laptop_shim.maybe_shim_env(
            {}, is_worker=True, repo_root=str(repo), base_path="/usr/bin"
        )
        # Re-compose using the already-prefixed PATH as the base.
        out2 = laptop_shim.maybe_shim_env(
            {}, is_worker=True, repo_root=str(repo), base_path=out1["PATH"]
        )
        assert out2["PATH"] == f"{shim_dir}{os.pathsep}/usr/bin"

    def test_compose_shim_path_empty_base(self) -> None:
        assert laptop_shim.compose_shim_path("/shim", None) == "/shim"
        assert laptop_shim.compose_shim_path("/shim", "") == "/shim"


class TestBoxesTsvParsing:
    def test_active_row_detected(self, tmp_path: Path) -> None:
        p = tmp_path / "boxes.tsv"
        p.write_text(_ACTIVE_TSV)
        assert laptop_shim._boxes_tsv_has_active_row(str(p)) is True

    def test_all_frozen_returns_false(self, tmp_path: Path) -> None:
        p = tmp_path / "boxes.tsv"
        p.write_text(_ALL_FROZEN_TSV)
        assert laptop_shim._boxes_tsv_has_active_row(str(p)) is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert laptop_shim._boxes_tsv_has_active_row(str(tmp_path / "nope.tsv")) is False

    def test_comments_and_blanks_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "boxes.tsv"
        p.write_text("# header\n\n   \n# another\n")
        assert laptop_shim._boxes_tsv_has_active_row(str(p)) is False


class TestNestedBoxesTsvResolution:
    """F636 (#491): the roster lives in the ROOT repo; the shim dir in the
    nested FORK. ``_active_boxes_tsv_for`` must find the roster one level up,
    and ``should_inject_shim`` must fire for a worker whose ``repo_root`` is the
    fork (the production shape that F620's single-root suite never built)."""

    def _nested(self, tmp_path: Path, root_tsv: str | None) -> tuple[Path, Path]:
        """Return (root, fork). Root owns scripts/boxes.tsv; fork owns
        scripts/laptop-shims. Neither has the other's file."""
        root = tmp_path / "root"
        (root / "scripts").mkdir(parents=True)
        if root_tsv is not None:
            (root / "scripts" / "boxes.tsv").write_text(root_tsv)
        fork = root / "fork"
        (fork / "scripts" / "laptop-shims").mkdir(parents=True)
        return root, fork

    def test_resolver_finds_parent_repo_roster(self, tmp_path: Path) -> None:
        root, fork = self._nested(tmp_path, _ACTIVE_TSV)
        found = laptop_shim._active_boxes_tsv_for(str(fork))
        assert found == os.path.join(str(root), "scripts", "boxes.tsv")

    def test_resolver_prefers_own_root_when_present(self, tmp_path: Path) -> None:
        # Both levels have an active roster: the resolved root wins (checked
        # first), so a self-contained checkout never reaches up unnecessarily.
        root, fork = self._nested(tmp_path, _ACTIVE_TSV)
        (fork / "scripts" / "boxes.tsv").write_text(_ACTIVE_TSV)
        found = laptop_shim._active_boxes_tsv_for(str(fork))
        assert found == os.path.join(str(fork), "scripts", "boxes.tsv")

    def test_resolver_none_when_neither_active(self, tmp_path: Path) -> None:
        root, fork = self._nested(tmp_path, _ALL_FROZEN_TSV)
        assert laptop_shim._active_boxes_tsv_for(str(fork)) is None

    def test_resolver_walks_up_only_one_level(self, tmp_path: Path) -> None:
        # Roster two levels up (grandparent) must NOT satisfy the guard.
        grand = tmp_path / "grand"
        (grand / "scripts").mkdir(parents=True)
        (grand / "scripts" / "boxes.tsv").write_text(_ACTIVE_TSV)
        mid = grand / "mid"
        mid.mkdir()
        leaf = mid / "leaf"
        (leaf / "scripts" / "laptop-shims").mkdir(parents=True)
        assert laptop_shim._active_boxes_tsv_for(str(leaf)) is None

    def test_should_inject_shim_true_for_nested_fork_worker(self, tmp_path: Path) -> None:
        root, fork = self._nested(tmp_path, _ACTIVE_TSV)
        assert (
            laptop_shim.should_inject_shim(is_worker=True, repo_root=str(fork), env={})
            is True
        )

    def test_should_inject_shim_false_when_parent_all_frozen(self, tmp_path: Path) -> None:
        root, fork = self._nested(tmp_path, _ALL_FROZEN_TSV)
        assert (
            laptop_shim.should_inject_shim(is_worker=True, repo_root=str(fork), env={})
            is False
        )

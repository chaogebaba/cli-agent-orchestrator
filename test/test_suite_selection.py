"""Regression tests for the named contributor and CI Python suites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _collect(path: Path, *args: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-c",
            str(REPO_ROOT / "pyproject.toml"),
            "--collect-only",
            "-q",
            "--no-cov",
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_contributor_and_ci_suites_keep_their_intentional_difference(tmp_path: Path) -> None:
    """The fork's contributor and CI tiers keep their intentional difference.

    Fork tiering model (Makefile targets, see ``Makefile`` test-full/test-ci):
    default ``addopts`` is deliberately MARKER-FREE — separation lives in the
    Makefile marker expressions, not in ``addopts``:

      * contributor tier = ``make test-full`` -> ``-m "not live"``
      * CI tier          = ``make test-ci``   -> ``-m "not live and not e2e"``

    The intentional difference is that CI additionally drops ``e2e``; both drop
    ``live``. Upstream #687 (231de7a4, "centralize contributor suite selection")
    instead bakes ``-m 'not e2e and not integration'`` into ``addopts`` and this
    test previously encoded THAT premise. Per the fork's conflict-resolution
    decision for the 2026-08-29 upstream merge, the fork's Makefile-tier model is
    the contract and ``addopts`` stays marker-free, so this test is pinned to the
    fork's tier marker expressions rather than upstream's addopts-baked filter.
    """
    regression = tmp_path / "test_selection.py"
    regression.write_text(
        "import pytest\n\n"
        "def test_unit():\n"
        "    pass\n\n"
        "@pytest.mark.live\n"
        "def test_live():\n"
        "    pass\n\n"
        "@pytest.mark.e2e\n"
        "def test_e2e_outside_e2e_directory():\n"
        "    pass\n",
        encoding="utf-8",
    )

    # The two named fork tiers, by their Makefile marker expressions.
    contributor = _collect(regression, "-m", "not live")
    ci = _collect(regression, "-m", "not live and not e2e")

    # Both tiers keep unit tests and drop live.
    assert "test_unit" in contributor
    assert "test_unit" in ci
    assert "test_live" not in contributor
    assert "test_live" not in ci

    # The intentional difference: e2e survives the contributor tier but the CI
    # tier drops it.
    assert "test_e2e_outside_e2e_directory" in contributor
    assert "test_e2e_outside_e2e_directory" not in ci

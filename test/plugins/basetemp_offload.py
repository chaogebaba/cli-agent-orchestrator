"""F330 — Redirect pytest basetemp off tmpfs with bounded retention.

Problem: /tmp is RAM-backed tmpfs on this host; pytest's default basetemp
under /tmp/pytest-of-<user> accumulated 4.2 GiB across 15 run dirs, directly
eating RAM and triggering OOM kills.

Solution: Override basetemp to /data/cao-scratch/pytest-tmp (configurable via
CAO_PYTEST_BASETEMP env var) and prune old run dirs to keep at most N (default 3,
configurable via CAO_PYTEST_BASETEMP_KEEP).

This plugin hooks pytest_configure (early enough to set basetemp before tmp_path
is resolved) and pytest_sessionfinish (to prune after each run).

Registered via ``pytest_plugins`` in ``test/conftest.py``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_DEFAULT_BASETEMP = "/data/cao-scratch/pytest-tmp"
_DEFAULT_KEEP = 3


def _resolve_basetemp() -> Path:
    """Resolve the off-tmpfs basetemp directory."""
    return Path(os.environ.get("CAO_PYTEST_BASETEMP", _DEFAULT_BASETEMP))


def _keep_count() -> int:
    """How many recent run dirs to retain."""
    try:
        return max(1, int(os.environ.get("CAO_PYTEST_BASETEMP_KEEP", str(_DEFAULT_KEEP))))
    except (ValueError, TypeError):
        return _DEFAULT_KEEP


def pytest_configure(config: pytest.Config) -> None:
    """Set basetemp to the off-tmpfs path unless the user explicitly passed --basetemp."""
    # Do not override if the user explicitly passed --basetemp on the CLI.
    # The ini-option default is "" when unset.
    explicit = config.getoption("basetemp", default=None)
    if explicit:
        return

    basetemp = _resolve_basetemp()
    basetemp.mkdir(parents=True, exist_ok=True)

    # Override the internal config option so tmp_path / tmp_path_factory use it.
    config.option.basetemp = str(basetemp)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Prune old basetemp run directories, keeping only the most recent N."""
    basetemp = _resolve_basetemp()
    if not basetemp.is_dir():
        return

    keep = _keep_count()

    # pytest creates numbered subdirs like pytest-NNN or popen-gwN; identify
    # run dirs by their mtime and keep only the newest `keep` entries.
    try:
        subdirs = [d for d in basetemp.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except OSError:
        return

    if len(subdirs) <= keep:
        return

    # Sort by modification time (newest first), prune oldest.
    subdirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in subdirs[keep:]:
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            sys.stderr.write(f"[basetemp-offload] failed to prune {stale}: {exc}\n")

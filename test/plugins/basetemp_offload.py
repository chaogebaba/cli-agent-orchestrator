"""F330 / F766 — Redirect pytest basetemp off tmpfs with a per-run child dir.

Problem: /tmp is RAM-backed tmpfs on this host; pytest's default basetemp
under /tmp/pytest-of-<user> accumulated 4.2 GiB across 15 run dirs, directly
eating RAM and triggering OOM kills.

F330 solved the tmpfs problem by redirecting basetemp to
``/data/cao-scratch/pytest-tmp`` and pruning old run dirs to keep at most N.
That prune was **count-based across the shared root**: ``pytest_sessionfinish``
sorted every sibling directory by mtime and ``rmtree``'d all but the newest N.

F766 bug: that shared-root prune is a **cross-run data race**. When two test
runs share the basetemp root (parallel lanes, CI matrix, a developer running
the suite twice), the finishing run would ``rmtree`` a *concurrent live run's*
directories out from under it — silently corrupting the other run.

F766 fix: give each run its **own child** ``<base>/<pid>-<nonce>/`` and point
pytest's basetemp at that child. On ``pytest_sessionfinish`` a run removes
**only its own child**, then **age-sweeps** stale sibling run dirs whose mtime
is older than a TTL. It never removes a sibling by count and never removes a
sibling with a fresh mtime, so a concurrent live run is never touched.

Env configuration:
- ``CAO_PYTEST_BASETEMP``       shared base root (default ``/data/cao-scratch/pytest-tmp``)
- ``CAO_PYTEST_BASETEMP_TTL_S`` age-sweep TTL in seconds (default 21600 = 6h)

Registered via ``pytest_plugins`` in ``test/conftest.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

_DEFAULT_BASETEMP = "/data/cao-scratch/pytest-tmp"
_DEFAULT_TTL_S = 6 * 60 * 60  # 6 hours

# Per-run child dirs are named "<pid>-<16 hex nonce>". Only names matching this
# shape are eligible for the age-sweep, so an unrelated directory a user parks
# under the base root is never a sweep candidate.
_RUN_DIR_RE = re.compile(r"\A\d+-[0-9a-f]{16}\Z")

# The child minted for THIS session, recorded by pytest_configure and consumed
# by pytest_sessionfinish. None when we did not (or could not) redirect.
_run_child: Path | None = None


def _resolve_basetemp() -> Path:
    """Resolve the shared off-tmpfs basetemp root."""
    return Path(os.environ.get("CAO_PYTEST_BASETEMP", _DEFAULT_BASETEMP))


def _ttl_seconds() -> float:
    """Age-sweep TTL: sibling run dirs older than this may be swept."""
    try:
        return max(0.0, float(os.environ.get("CAO_PYTEST_BASETEMP_TTL_S", str(_DEFAULT_TTL_S))))
    except (ValueError, TypeError):
        return float(_DEFAULT_TTL_S)


def _mint_run_child(base: Path) -> Path:
    """Create a per-run child ``<base>/<pid>-<nonce>`` unique to this process."""
    child = base / f"{os.getpid()}-{os.urandom(8).hex()}"
    # exist_ok=False: the pid+nonce pair must be unique; a collision is a bug we
    # want to surface rather than silently share a dir with another run.
    child.mkdir(parents=True, exist_ok=False)
    return child


def _age_sweep(base: Path, own_child: Path, ttl_s: float, *, now: float | None = None) -> None:
    """Remove sibling run dirs whose mtime is older than ``ttl_s``.

    Only directories whose name matches ``_RUN_DIR_RE`` are candidates, our own
    child is always skipped, and a directory younger than the TTL is left alone.
    This is what makes a concurrent live run's dirs safe: a live run keeps its
    child's mtime fresh, so it is never old enough to sweep.
    """
    reference = time.time() if now is None else now
    try:
        siblings = list(base.iterdir())
    except OSError:
        return
    for sibling in siblings:
        if sibling == own_child:
            continue
        if not sibling.is_dir() or not _RUN_DIR_RE.match(sibling.name):
            continue
        try:
            age = reference - sibling.stat().st_mtime
        except OSError:
            continue
        if age <= ttl_s:
            # Fresh — a concurrent run may still own it. Never sweep.
            continue
        try:
            shutil.rmtree(sibling)
        except OSError as exc:
            sys.stderr.write(f"[basetemp-offload] failed to sweep {sibling}: {exc}\n")


def pytest_configure(config: pytest.Config) -> None:
    """Redirect basetemp to a per-run child under the shared off-tmpfs root.

    Skips redirection when the user explicitly passed ``--basetemp`` or the
    configured base is not creatable on this host (e.g. a CI runner without
    /data), falling back to pytest's default basetemp.
    """
    global _run_child

    # Do not override if the user explicitly passed --basetemp on the CLI.
    # The ini-option default is "" when unset.
    explicit = config.getoption("basetemp", default=None)
    if explicit:
        return

    base = _resolve_basetemp()
    try:
        base.mkdir(parents=True, exist_ok=True)
        child = _mint_run_child(base)
    except (PermissionError, OSError):
        # The configured path is not creatable on this host. Fall back to
        # pytest's default basetemp and record no child to clean up.
        _run_child = None
        return

    _run_child = child
    # Point tmp_path / tmp_path_factory at this run's private child.
    config.option.basetemp = str(child)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Remove this run's own child, then age-sweep stale sibling run dirs."""
    child = _run_child
    if child is None:
        return

    base = child.parent

    # 1. Remove only our own run child — never a count-based cross-run prune.
    try:
        shutil.rmtree(child)
    except FileNotFoundError:
        pass
    except OSError as exc:
        sys.stderr.write(f"[basetemp-offload] failed to remove own child {child}: {exc}\n")

    # 2. Age-sweep siblings older than the TTL. Fresh siblings (concurrent live
    #    runs) are left untouched.
    if base.is_dir():
        _age_sweep(base, child, _ttl_seconds())

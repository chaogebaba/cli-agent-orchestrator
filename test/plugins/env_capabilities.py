"""Environment-capability marker guard (WP-suite D2, AC2.1).

Tests marked with ``requires_*`` markers are auto-skipped at collection time
when the corresponding capability is absent from the host. Detectors are pure
functions — no network, no subprocess beyond the git check.

Registered via ``pytest_plugins`` in ``test/conftest.py``.
Follows the shape proven at ``test/plugins/local_fixture_guard.py``.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Detectors — each returns True when the capability IS present.
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _has_bwrap() -> bool:
    return shutil.which("bwrap") is not None


@functools.lru_cache(maxsize=1)
def _has_codex_auth() -> bool:
    return (Path.home() / ".codex" / "auth.json").exists()


@functools.lru_cache(maxsize=1)
def _has_herdr() -> bool:
    return shutil.which("herdr") is not None


@functools.lru_cache(maxsize=1)
def _has_tmux() -> bool:
    return shutil.which("tmux") is not None


_git_object_cache: dict[str, bool] = {}


def _has_git_object(oid: str) -> bool:
    if oid in _git_object_cache:
        return _git_object_cache[oid]
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{oid}^{{commit}}"],
            capture_output=True,
            timeout=5,
        )
        present = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        present = False
    _git_object_cache[oid] = present
    return present


# ---------------------------------------------------------------------------
# Marker → detector mapping.
# ---------------------------------------------------------------------------

_SIMPLE_MARKERS: dict[str, tuple[callable, str]] = {
    "requires_bwrap": (_has_bwrap, "bwrap not found on PATH"),
    "requires_codex_auth": (_has_codex_auth, "~/.codex/auth.json not found"),
    "requires_herdr": (_has_herdr, "herdr not found on PATH"),
    "requires_tmux": (_has_tmux, "tmux not found on PATH"),
}


# ---------------------------------------------------------------------------
# Hook.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for marker_name, (detector, reason) in _SIMPLE_MARKERS.items():
            if item.get_closest_marker(marker_name) and not detector():
                item.add_marker(pytest.mark.skip(reason=reason))

        # requires_git_object("<oid>") — parametric marker.
        git_marker = item.get_closest_marker("requires_git_object")
        if git_marker and git_marker.args:
            oid = git_marker.args[0]
            if not _has_git_object(oid):
                item.add_marker(
                    pytest.mark.skip(reason=f"git object {oid} not reachable")
                )

"""Self-tests for the F766 per-run basetemp offload plugin.

Proves the F766 fix: each run gets its own ``<base>/<pid>-<nonce>`` child, a
finishing run removes ONLY its own child, and the age-sweep removes stale
siblings while leaving a concurrent live run's (fresh-mtime) dir untouched.

The plugin's module-level ``_run_child`` is per-process state, so tests that
exercise ``pytest_configure`` / ``pytest_sessionfinish`` save and restore it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from test.plugins import basetemp_offload as bto


class _FakeConfig:
    """Minimal pytest.Config stand-in for the two hooks under test."""

    def __init__(self, explicit_basetemp: str | None = None) -> None:
        self._explicit = explicit_basetemp

        class _Opt:
            basetemp: str | None = None

        self.option = _Opt()

    def getoption(self, name: str, default: object = None) -> object:
        if name == "basetemp":
            return self._explicit if self._explicit is not None else default
        return default


@pytest.fixture(autouse=True)
def _isolate_run_child() -> Iterator[None]:
    """Save/restore the plugin's module-level per-run child pointer."""
    saved = bto._run_child
    bto._run_child = None
    try:
        yield
    finally:
        bto._run_child = saved


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared basetemp root pointed off the real /data path."""
    root = tmp_path / "pytest-tmp"
    monkeypatch.setenv("CAO_PYTEST_BASETEMP", str(root))
    return root


def _mk_sibling(base: Path, name: str, *, age_s: float) -> Path:
    """Create a run-shaped sibling dir with an mtime ``age_s`` in the past."""
    base.mkdir(parents=True, exist_ok=True)
    sib = base / name
    sib.mkdir()
    (sib / "marker").write_text("x", encoding="utf-8")
    past = time.time() - age_s
    os.utime(sib, (past, past))
    return sib


# 1 — configure mints a per-run child and repoints basetemp at it.
def test_configure_mints_per_run_child(base: Path) -> None:
    config = _FakeConfig()
    bto.pytest_configure(config)  # type: ignore[arg-type]

    child = bto._run_child
    assert child is not None
    assert child.parent == base
    assert child.is_dir()
    # basetemp is repointed at the child, not the shared root.
    assert config.option.basetemp == str(child)
    assert bto._RUN_DIR_RE.match(child.name), child.name


# 2 — an explicit --basetemp disables redirection entirely.
def test_explicit_basetemp_disables_redirection(base: Path) -> None:
    config = _FakeConfig(explicit_basetemp="/somewhere/else")
    bto.pytest_configure(config)  # type: ignore[arg-type]

    assert bto._run_child is None
    assert config.option.basetemp is None
    # No child was created under the shared root.
    assert not base.exists() or list(base.iterdir()) == []


# 3 — sessionfinish removes THIS run's own child.
def test_sessionfinish_removes_own_child(base: Path) -> None:
    config = _FakeConfig()
    bto.pytest_configure(config)  # type: ignore[arg-type]
    child = bto._run_child
    assert child is not None and child.is_dir()

    bto.pytest_sessionfinish(session=None, exitstatus=0)  # type: ignore[arg-type]

    assert not child.exists()


# 4 — WITNESS: a concurrent live sibling (fresh mtime) SURVIVES sessionfinish.
def test_live_sibling_not_deleted(base: Path) -> None:
    config = _FakeConfig()
    bto.pytest_configure(config)  # type: ignore[arg-type]
    own = bto._run_child
    assert own is not None

    # A concurrent run's child: run-shaped name, brand-new mtime.
    live = _mk_sibling(base, "999999-abcdef0123456789", age_s=0.0)

    bto.pytest_sessionfinish(session=None, exitstatus=0)  # type: ignore[arg-type]

    assert live.is_dir(), "a concurrent live run's dir must never be swept"
    assert (live / "marker").exists()
    assert not own.exists()


# 5 — age-sweep removes a stale sibling older than the TTL.
def test_age_sweep_removes_stale_sibling(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_PYTEST_BASETEMP_TTL_S", "60")
    config = _FakeConfig()
    bto.pytest_configure(config)  # type: ignore[arg-type]

    stale = _mk_sibling(base, "111111-0011223344556677", age_s=3600.0)  # 1h old, TTL 60s
    fresh = _mk_sibling(base, "222222-8899aabbccddeeff", age_s=0.0)

    bto.pytest_sessionfinish(session=None, exitstatus=0)  # type: ignore[arg-type]

    assert not stale.exists(), "a sibling older than the TTL should be swept"
    assert fresh.is_dir(), "a fresh sibling should survive"


# 6 — the age-sweep never touches non-run-shaped directories.
def test_age_sweep_ignores_foreign_dirs(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_PYTEST_BASETEMP_TTL_S", "60")
    config = _FakeConfig()
    bto.pytest_configure(config)  # type: ignore[arg-type]

    # Old, but NOT run-shaped — a directory a human parked under the base.
    foreign = _mk_sibling(base, "important-data", age_s=99999.0)

    bto.pytest_sessionfinish(session=None, exitstatus=0)  # type: ignore[arg-type]

    assert foreign.is_dir(), "non run-shaped dirs are never sweep candidates"


# 7 — an un-redirected run (no child) is a sessionfinish no-op.
def test_sessionfinish_noop_without_child(base: Path) -> None:
    # pytest_configure never ran / redirection was skipped.
    assert bto._run_child is None
    sibling = _mk_sibling(base, "333333-1122334455667788", age_s=99999.0)

    bto.pytest_sessionfinish(session=None, exitstatus=0)  # type: ignore[arg-type]

    # Nothing minted, nothing swept — the plugin stays hands-off.
    assert sibling.is_dir()

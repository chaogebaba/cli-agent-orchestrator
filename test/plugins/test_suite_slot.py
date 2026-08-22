"""Unit tests for the suite-slot lockfile plugin (F272, issue #182).

Tests:
1. Lock acquired and released correctly on uncontended path.
2. Contention with fail-fast (default) — clear error message with holder info.
3. Contention with wait mode (CAO_SUITE_SLOT_WAIT=1) — eventually acquires.
4. CI env var skips locking entirely.
5. xdist worker processes do not attempt locking.
"""

from __future__ import annotations

import fcntl
import multiprocessing
import multiprocessing.synchronize
import os
import subprocess
import sys
import time
from pathlib import Path
from test.plugins import suite_slot
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_lockfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the lockfile to a temp path so tests don't interfere with real runs."""
    lock_path = tmp_path / ".suite-slot.lock"
    monkeypatch.setattr(suite_slot, "_LOCK_PATH", lock_path)
    # Reset module state
    monkeypatch.setattr(suite_slot, "_lock_fd", None)
    # Ensure CI is not set (tests simulate non-CI)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CAO_SUITE_SLOT_WAIT", raising=False)


def _make_config(*, is_worker: bool = False) -> MagicMock:
    """Build a minimal pytest.Config mock."""
    config = MagicMock(spec=pytest.Config)
    if is_worker:
        config.workerinput = {"workerid": "gw0"}
    else:
        # Controller — no workerinput attribute
        del config.workerinput
    return config


class TestLockAcquireRelease:
    """Lock acquired on configure, released on unconfigure."""

    def test_lock_acquired_uncontended(self, tmp_path: Path) -> None:
        config = _make_config()
        suite_slot.pytest_configure(config)

        # Lock fd should be set
        assert suite_slot._lock_fd is not None

        # Lockfile should contain holder identity with pid
        lock_path = tmp_path / ".suite-slot.lock"
        content = lock_path.read_text()
        assert f"pid={os.getpid()}" in content
        assert "since=" in content

        # Release
        suite_slot.pytest_unconfigure(config)
        assert suite_slot._lock_fd is None

    def test_lock_released_on_unconfigure(self, tmp_path: Path) -> None:
        config = _make_config()
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is not None

        suite_slot.pytest_unconfigure(config)
        assert suite_slot._lock_fd is None

        # After release, another process can acquire the lock non-blocking
        lock_path = tmp_path / ".suite-slot.lock"
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # If we get here, lock was released properly
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class TestContentionFailFast:
    """Default behavior: fail fast with holder info on contention.

    These tests use subprocesses because pytest.exit() terminates the session.
    """

    def test_fail_fast_with_holder_message(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".suite-slot.lock"

        # Hold the lock from this process
        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holder_info = "pid=99999 terminal=test-holder since=2026-08-20T12:00:00Z"
        os.write(holder_fd, holder_info.encode())

        try:
            # Run a subprocess pytest that tries to acquire the same lock
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"""
import fcntl, os, sys
sys.path.insert(0, "{Path(__file__).resolve().parent.parent.parent}")
from test.plugins import suite_slot
suite_slot._LOCK_PATH = __import__("pathlib").Path("{lock_path}")

from unittest.mock import MagicMock
import pytest
config = MagicMock(spec=pytest.Config)
del config.workerinput

try:
    suite_slot.pytest_configure(config)
    print("ERROR: should have exited")
    sys.exit(99)
except SystemExit as e:
    sys.exit(e.code if isinstance(e.code, int) else 1)
""",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "CI": ""},
            )
            assert result.returncode == 1
            assert "suite-slot" in result.stderr
            assert "test-holder" in result.stderr
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

    def test_fail_fast_is_default(self, tmp_path: Path) -> None:
        """Without CAO_SUITE_SLOT_WAIT, contention = immediate exit."""
        lock_path = tmp_path / ".suite-slot.lock"

        holder_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder_fd, b"pid=12345 terminal=blocker since=2026-01-01T00:00:00Z")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"""
import fcntl, os, sys
sys.path.insert(0, "{Path(__file__).resolve().parent.parent.parent}")
from test.plugins import suite_slot
suite_slot._LOCK_PATH = __import__("pathlib").Path("{lock_path}")

from unittest.mock import MagicMock
import pytest
config = MagicMock(spec=pytest.Config)
del config.workerinput

try:
    suite_slot.pytest_configure(config)
    sys.exit(99)
except SystemExit as e:
    sys.exit(e.code if isinstance(e.code, int) else 1)
""",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                env={k: v for k, v in os.environ.items() if k not in ("CI", "CAO_SUITE_SLOT_WAIT")},
            )
            assert result.returncode == 1
            assert "BLOCKED" in result.stderr
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)


def _hold_lock_then_release(
    lock_path_str: str,
    ready_event: multiprocessing.synchronize.Event,
    release_event: multiprocessing.synchronize.Event,
) -> None:
    """Child process: hold lock, signal ready, wait for release signal."""
    fd = os.open(lock_path_str, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} terminal=child since=now".encode())
    ready_event.set()
    release_event.wait(timeout=10)
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


class TestContentionWaitMode:
    """CAO_SUITE_SLOT_WAIT=1 causes blocking wait instead of fail."""

    def test_wait_mode_acquires_after_release(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_WAIT", "1")
        lock_path = tmp_path / ".suite-slot.lock"

        ready = multiprocessing.Event()
        release = multiprocessing.Event()

        child = multiprocessing.Process(
            target=_hold_lock_then_release,
            args=(str(lock_path), ready, release),
        )
        child.start()
        ready.wait(timeout=5)

        # Release after a short delay so the configure can acquire
        import threading

        def _release_later() -> None:
            time.sleep(0.3)
            release.set()

        t = threading.Thread(target=_release_later)
        t.start()

        config = _make_config()
        suite_slot.pytest_configure(config)

        # Should have acquired
        assert suite_slot._lock_fd is not None
        content = lock_path.read_text()
        assert f"pid={os.getpid()}" in content

        suite_slot.pytest_unconfigure(config)
        t.join(timeout=5)
        child.join(timeout=5)


class TestSkipConditions:
    """Conditions where locking is skipped."""

    def test_ci_env_skips_locking(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        config = _make_config()
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is None

    def test_xdist_worker_skips_locking(self, tmp_path: Path) -> None:
        config = _make_config(is_worker=True)
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is None

    def test_serial_run_still_acquires_lock(self, tmp_path: Path) -> None:
        """Even -n 0 / single-test runs take the lock (no carve-outs)."""
        config = _make_config()
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is not None
        suite_slot.pytest_unconfigure(config)

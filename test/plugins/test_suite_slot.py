"""Unit tests for the suite-slot lockfile plugin (F272, issue #182).

Tests:
1. Lock acquired and released correctly on uncontended path.
2. Contention with fail-fast (default) — clear error message with holder info.
3. Contention with wait mode (CAO_SUITE_SLOT_WAIT=1) — eventually acquires.
4. CI env var skips locking entirely.
5. xdist worker processes do not attempt locking.
6. Watchdog env parsing (CAO_SUITE_SLOT_MAX_SECONDS) — default/blank/bad/<=0.
7. Arming decisions — armed only on real acquisition; not when disabled;
   cancelled on clean unconfigure; NOT armed on CI / xdist / contention.
8. Integration: a subprocess pytest run whose test sleeps past the bound
   has its whole process group SIGKILLed ~on time, with the diagnostic,
   and the slot lock is freed afterwards.
"""

from __future__ import annotations

import fcntl
import math
import multiprocessing
import multiprocessing.synchronize
import os
import signal
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
    monkeypatch.setattr(suite_slot, "_watchdog", None)
    # Ensure CI is not set (tests simulate non-CI)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("CAO_SUITE_SLOT_WAIT", raising=False)
    # Neutralize the wall-clock watchdog for tests that acquire the real lock:
    # disable it so an armed daemon Timer can't SIGKILL the pytest runner.
    monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "0")


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



# ---------------------------------------------------------------------------
# F437 (issue #292): self-destruct watchdog
# ---------------------------------------------------------------------------


class TestMaxSecondsParsing:
    """CAO_SUITE_SLOT_MAX_SECONDS env parsing (`_max_seconds`)."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CAO_SUITE_SLOT_MAX_SECONDS", raising=False)
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    def test_default_is_one_hour(self) -> None:
        assert suite_slot._DEFAULT_MAX_SECONDS == 3600.0

    def test_blank_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "   ")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    def test_explicit_positive_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "120")
        assert suite_slot._max_seconds() == 120.0

    def test_fractional_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "2.5")
        assert suite_slot._max_seconds() == 2.5

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """<= 0 is returned verbatim so the caller can decide not to arm."""
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "0")
        assert suite_slot._max_seconds() == 0.0

    def test_negative_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "-1")
        assert suite_slot._max_seconds() == -1.0

    def test_unparseable_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A garbage value uses the safe default rather than disabling."""
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "not-a-number")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    # -- Non-finite rejection (F437 R2 gate blocker) -----------------------
    # float() happily parses "nan"/"inf"/"-inf", and overflows "1e309" to inf.
    # nan would make the Timer fire immediately (SIGKILL on startup); inf would
    # crash the Timer thread (OverflowError) leaving NO wall-clock bound. All
    # must take the warned 3600 fallback, exactly like unparseable text.

    def test_nan_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "nan")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    def test_inf_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "inf")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    def test_negative_inf_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "-inf")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS

    def test_overflow_literal_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`1e309` overflows to inf in float() — must not survive as a bound."""
        # Guard: confirm the literal really does overflow to inf, so this test
        # exercises the isfinite() gate and not merely ValueError.
        assert float("1e309") == float("inf")
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "1e309")
        assert suite_slot._max_seconds() == suite_slot._DEFAULT_MAX_SECONDS


class TestNonFiniteArmingBehavior:
    """Behavioral proof the non-finite fix protects the watchdog (F437 R2).

    These go beyond `_max_seconds()`'s return value: they arm the real
    watchdog and observe that nan does NOT self-destruct immediately and
    inf/overflow arm a genuine ~3600s bound (asserted via the alive daemon
    timer's interval — never sleeping an hour).
    """

    def test_nan_does_not_fire_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With the fix, `nan` must arm a real 3600 bound, not fire at once.

        A pre-fix `_max_seconds()` returned nan → threading.Timer(nan, ...)
        fires on the next scheduler tick → the controller SIGKILLs its own
        PGID on startup. We arm, then confirm the timer is still alive after
        a short wait (it did NOT fire) and carries the fallback interval.
        """
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "nan")
        suite_slot._arm_watchdog()
        try:
            assert suite_slot._watchdog is not None
            # Give any (buggy) immediate fire a chance to happen.
            time.sleep(0.5)
            assert suite_slot._watchdog.is_alive(), "nan armed a firing timer"
            assert suite_slot._watchdog.interval == suite_slot._DEFAULT_MAX_SECONDS
        finally:
            suite_slot._cancel_watchdog()
            assert suite_slot._watchdog is None

    def test_inf_arms_real_bound_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`inf` must arm a genuine 3600 bound, not crash the timer thread.

        A pre-fix `_max_seconds()` returned inf → threading.Timer(inf, ...)
        raises OverflowError converting to the wait timeout → the daemon
        thread dies → NO wall-clock bound. We confirm a live daemon timer
        with the finite fallback interval instead.
        """
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "inf")
        suite_slot._arm_watchdog()
        try:
            assert suite_slot._watchdog is not None
            assert suite_slot._watchdog.daemon is True
            assert suite_slot._watchdog.is_alive()
            assert suite_slot._watchdog.interval == suite_slot._DEFAULT_MAX_SECONDS
            assert math.isfinite(suite_slot._watchdog.interval)
        finally:
            suite_slot._cancel_watchdog()
            assert suite_slot._watchdog is None

    def test_overflow_literal_arms_real_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`1e309` (→inf) arms a finite 3600 bound, same as inf."""
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "1e309")
        suite_slot._arm_watchdog()
        try:
            assert suite_slot._watchdog is not None
            assert suite_slot._watchdog.is_alive()
            assert suite_slot._watchdog.interval == suite_slot._DEFAULT_MAX_SECONDS
        finally:
            suite_slot._cancel_watchdog()
            assert suite_slot._watchdog is None


class TestArmingDecisions:
    """`_arm_watchdog` / `_cancel_watchdog` behavior (no real fire)."""

    def test_arm_creates_daemon_timer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "3600")
        try:
            suite_slot._arm_watchdog()
            assert suite_slot._watchdog is not None
            assert suite_slot._watchdog.daemon is True
            assert suite_slot._watchdog.is_alive()
        finally:
            suite_slot._cancel_watchdog()
            assert suite_slot._watchdog is None

    def test_disabled_when_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "0")
        suite_slot._arm_watchdog()
        assert suite_slot._watchdog is None

    def test_disabled_when_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "-5")
        suite_slot._arm_watchdog()
        assert suite_slot._watchdog is None

    def test_cancel_is_idempotent(self) -> None:
        suite_slot._cancel_watchdog()
        suite_slot._cancel_watchdog()
        assert suite_slot._watchdog is None

    def test_configure_arms_when_lock_acquired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The controller arms a live watchdog once it holds the lock."""
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "3600")
        config = _make_config()
        suite_slot.pytest_configure(config)
        try:
            assert suite_slot._lock_fd is not None
            assert suite_slot._watchdog is not None
            assert suite_slot._watchdog.is_alive()
        finally:
            suite_slot.pytest_unconfigure(config)
        # Clean unconfigure cancels + clears the watchdog.
        assert suite_slot._watchdog is None

    def test_configure_does_not_arm_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "0")
        config = _make_config()
        suite_slot.pytest_configure(config)
        try:
            assert suite_slot._lock_fd is not None
            assert suite_slot._watchdog is None
        finally:
            suite_slot.pytest_unconfigure(config)

    def test_no_arm_on_ci(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CI path skips acquisition — must never arm the self-destruct."""
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "3600")
        config = _make_config()
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is None
        assert suite_slot._watchdog is None

    def test_no_arm_on_xdist_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """xdist workers skip acquisition — must never arm the self-destruct."""
        monkeypatch.setenv("CAO_SUITE_SLOT_MAX_SECONDS", "3600")
        config = _make_config(is_worker=True)
        suite_slot.pytest_configure(config)
        assert suite_slot._lock_fd is None
        assert suite_slot._watchdog is None


class TestWatchdogIntegration:
    """End-to-end: a runaway run self-destructs and frees the slot.

    Spawns a real child pytest whose single test sleeps far past a 2s bound.
    The watchdog must SIGKILL the child's whole process group ~on time, emit
    the diagnostic, and leave the lock free for the next acquirer.
    """

    @pytest.mark.slow
    def test_runaway_run_self_destructs_and_frees_slot(self, tmp_path: Path) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock_path = tmp_path / ".suite-slot.lock"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # A conftest that repoints the plugin's lockfile and registers it,
        # so the child exercises the real acquire → arm → fire path.
        conftest = run_dir / "conftest.py"
        conftest.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from test.plugins import suite_slot\n"
            f"suite_slot._LOCK_PATH = pathlib.Path({str(lock_path)!r})\n"
            "pytest_plugins = ('test.plugins.suite_slot',)\n"
        )
        # A test that hangs well past the 2s bound (would run ~120s).
        (run_dir / "test_hang.py").write_text(
            "import time\n"
            "def test_sleep_forever():\n"
            "    time.sleep(120)\n"
        )

        env = {k: v for k, v in os.environ.items() if k not in ("CI", "CAO_SUITE_SLOT_WAIT")}
        env["CAO_SUITE_SLOT_MAX_SECONDS"] = "2"

        # start_new_session=True → child is its own process-group leader, so
        # the watchdog's killpg targets the child tree, not this test runner.
        start = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "-p", "no:libtmux", "-s", "test_hang.py"],
            cwd=str(run_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            pytest.fail(f"watchdog did not fire; child hung. Output:\n{out}")

        elapsed = time.monotonic() - start

        # Killed by watchdog → os._exit(137) from the process itself.
        # With start_new_session=True the child is its own pgid leader; the
        # watchdog kills descendants then os._exit(137).
        assert proc.returncode == 137, (
            f"expected exit 137 (watchdog os._exit), got returncode={proc.returncode}\n{out}"
        )
        # Fired ~on the 2s bound, comfortably under the 30s incident ceiling.
        assert elapsed < 20, f"took too long ({elapsed:.1f}s):\n{out}"
        # Loud diagnostic present.
        assert "SUITE-SLOT WATCHDOG" in out, out
        assert "SELF-DESTRUCT" in out, out

        # Slot freed: the lock is immediately re-acquirable non-blocking.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)



# ---------------------------------------------------------------------------
# F445 (issue #300): watchdog must never kill ancestors
# ---------------------------------------------------------------------------


class TestWatchdogAncestorSafety:
    """F445 regression: watchdog kills only the pytest subtree, ancestors survive.

    The pre-fix code used os.killpg(os.getpgrp(), SIGKILL) which, when pytest
    shared a process group with the agent TUI, killed the entire pane. The fix
    walks /proc descendants of os.getpid() only.
    """

    @pytest.mark.slow
    def test_ancestor_survives_watchdog_fire(self, tmp_path: Path) -> None:
        """Spawn a parent (simulating TUI ancestor) that forks a child running
        pytest with a short watchdog. The parent MUST survive after the child
        is killed by the watchdog — this is the core F445 invariant.
        """
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock_path = tmp_path / "slot.lock"
        marker_file = tmp_path / "ancestor_alive"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        # conftest that registers the plugin with repointed lockfile
        (run_dir / "conftest.py").write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from test.plugins import suite_slot\n"
            f"suite_slot._LOCK_PATH = pathlib.Path({str(lock_path)!r})\n"
            "pytest_plugins = ('test.plugins.suite_slot',)\n"
        )
        # test that hangs past the 2s watchdog
        (run_dir / "test_hang.py").write_text(
            "import time\n"
            "def test_hang():\n"
            "    time.sleep(60)\n"
        )

        # Parent script: spawns pytest as a child (same pgid — no
        # start_new_session), waits for it to die, then writes marker.
        parent_script = tmp_path / "parent.py"
        parent_script.write_text(
            "import os, sys, subprocess\n"
            f"env = {{**os.environ, 'CAO_SUITE_SLOT_MAX_SECONDS': '2',\n"
            f"        'CAO_SUITE_SLOT_LOCK': {str(lock_path)!r}}}\n"
            "env.pop('CI', None)\n"
            "env.pop('CAO_SUITE_SLOT_WAIT', None)\n"
            "child = subprocess.Popen(\n"
            f"    [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',\n"
            f"     '-p', 'no:libtmux', '-s', 'test_hang.py'],\n"
            f"    cwd={str(run_dir)!r},\n"
            "    env=env,\n"
            "    stdout=subprocess.PIPE,\n"
            "    stderr=subprocess.STDOUT,\n"
            ")\n"
            "child.wait()\n"
            "# If we reach here, we survived the watchdog!\n"
            f"open({str(marker_file)!r}, 'w').write(f'alive:{{os.getpid()}}')\n"
        )

        # Run the parent — do NOT use start_new_session so parent and child
        # share a pgid (the dangerous scenario the bug exploited).
        result = subprocess.run(
            [sys.executable, str(parent_script)],
            timeout=30,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"Parent died (rc={result.returncode}) — watchdog killed ancestor!\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert marker_file.exists(), (
            "Ancestor marker not written — ancestor was killed by watchdog"
        )
        content = marker_file.read_text()
        assert content.startswith("alive:"), f"Unexpected marker: {content}"

    @pytest.mark.slow
    def test_watchdog_exit_code_is_137(self, tmp_path: Path) -> None:
        """The watchdog-killed pytest process exits with code 137 (os._exit)."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock_path = tmp_path / "slot.lock"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        (run_dir / "conftest.py").write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from test.plugins import suite_slot\n"
            f"suite_slot._LOCK_PATH = pathlib.Path({str(lock_path)!r})\n"
            "pytest_plugins = ('test.plugins.suite_slot',)\n"
        )
        (run_dir / "test_hang.py").write_text(
            "import time\n"
            "def test_hang():\n"
            "    time.sleep(300)\n"
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CI", "CAO_SUITE_SLOT_WAIT")}
        env["CAO_SUITE_SLOT_MAX_SECONDS"] = "2"
        env["CAO_SUITE_SLOT_LOCK"] = str(lock_path)

        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "-p", "no:libtmux", "-s", "test_hang.py"],
            cwd=str(run_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("watchdog did not fire within 15s")

        assert proc.returncode == 137, (
            f"Expected 137 from os._exit, got {proc.returncode}"
        )


class TestWatchdogDisarmOnNormalCompletion:
    """F445 regression: normal test completion disarms the watchdog cleanly."""

    def test_clean_exit_no_watchdog_fire(self, tmp_path: Path) -> None:
        """A fast test run exits 0 — watchdog never fires."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock_path = tmp_path / "slot.lock"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        (run_dir / "conftest.py").write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from test.plugins import suite_slot\n"
            f"suite_slot._LOCK_PATH = pathlib.Path({str(lock_path)!r})\n"
            "pytest_plugins = ('test.plugins.suite_slot',)\n"
        )
        (run_dir / "test_fast.py").write_text(
            "def test_quick():\n"
            "    assert 1 + 1 == 2\n"
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CI", "CAO_SUITE_SLOT_WAIT")}
        env["CAO_SUITE_SLOT_MAX_SECONDS"] = "30"
        env["CAO_SUITE_SLOT_LOCK"] = str(lock_path)

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "-p", "no:libtmux", "-s", "test_fast.py"],
            cwd=str(run_dir),
            env=env,
            timeout=15,
            capture_output=True,
            text=True,
            start_new_session=True,
        )

        assert result.returncode == 0, (
            f"Expected clean exit 0, got rc={result.returncode}\n"
            f"output: {result.stdout}\n{result.stderr}"
        )

    def test_lock_freed_after_normal_run(self, tmp_path: Path) -> None:
        """After normal completion, the lock file is not held."""
        repo_root = Path(__file__).resolve().parent.parent.parent
        lock_path = tmp_path / "slot.lock"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        (run_dir / "conftest.py").write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from test.plugins import suite_slot\n"
            f"suite_slot._LOCK_PATH = pathlib.Path({str(lock_path)!r})\n"
            "pytest_plugins = ('test.plugins.suite_slot',)\n"
        )
        (run_dir / "test_pass.py").write_text(
            "def test_pass():\n"
            "    pass\n"
        )

        env = {k: v for k, v in os.environ.items()
               if k not in ("CI", "CAO_SUITE_SLOT_WAIT")}
        env["CAO_SUITE_SLOT_MAX_SECONDS"] = "30"
        env["CAO_SUITE_SLOT_LOCK"] = str(lock_path)

        subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
             "-p", "no:libtmux", "-s", "test_pass.py"],
            cwd=str(run_dir),
            env=env,
            timeout=15,
            capture_output=True,
            start_new_session=True,
        )

        # Lock must be free — acquire non-blocking
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            pytest.fail("Lock file still held after normal pytest exit")
        finally:
            os.close(fd)

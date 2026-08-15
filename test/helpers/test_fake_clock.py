"""Unit tests for FakeClock (AC3).

Also demonstrates the D6 daemon-thread + join-with-timeout reference pattern
and provides the AC10 timeout-fires canary.
"""

import threading
import time

import pytest

from test.helpers.fake_clock import FakeClock


class TestFakeClockBasics:
    """Core FakeClock operations."""

    def test_initial_value(self):
        clock = FakeClock(start=500.0)
        assert clock.monotonic() == 500.0

    def test_advance(self):
        clock = FakeClock()
        clock.advance(3.5)
        assert clock.monotonic() == 1003.5

    def test_sleep_advances(self):
        clock = FakeClock()
        clock.sleep(2.0)
        assert clock.monotonic() == 1002.0

    def test_patch_time_context(self):
        clock = FakeClock()
        with clock.patch_time("time.monotonic", "time.sleep"):
            assert time.monotonic() == 1000.0
            time.sleep(1.0)
            assert time.monotonic() == 1001.0
            clock.advance(5.0)
            assert time.monotonic() == 1006.0
        # After context exits, real time is restored
        assert time.monotonic() != 1006.0


class TestFakeClockDaemonThreadPattern:
    """D6 reference pattern: daemon threads + join-with-timeout at teardown.

    Shows how tests that spawn background threads should clean up without
    needing a global thread-leak fixture.
    """

    def test_daemon_thread_join_with_timeout(self):
        """Spawn a daemon thread, then join with a timeout to confirm it exits
        cleanly. This is the canonical D6 cleanup pattern."""
        result = []

        def worker():
            result.append("started")
            # Simulate some work
            result.append("done")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive(), "daemon thread did not exit within timeout"
        assert result == ["started", "done"]

    def test_fake_clock_with_polling_thread(self):
        """A thread polling time.monotonic() completes deterministically
        under FakeClock without real wall-clock waits."""
        clock = FakeClock()
        reached_deadline = threading.Event()

        def poller():
            # Poll until 5 seconds have elapsed on the fake clock
            while clock.monotonic() < 1005.0:
                clock.sleep(0.1)  # yields to fake clock
            reached_deadline.set()

        with clock.patch_time("time.monotonic", "time.sleep"):
            t = threading.Thread(target=poller, daemon=True)
            t.start()
            # Advance clock past the deadline
            clock.advance(6.0)
            t.join(timeout=3.0)
            assert not t.is_alive()
            assert reached_deadline.is_set()


class TestTimeoutFires:
    """AC10: pytest-timeout kills tests that exceed the configured limit.

    The canary runs a subprocess pytest invocation that deliberately exceeds
    its timeout, then asserts the subprocess exits non-zero with 'Timeout' in
    output.  This avoids crashing the xdist worker (thread-method timeout +
    xdist = worker crash) while still proving pytest-timeout is active.
    """

    @pytest.mark.slow
    def test_timeout_fires(self):
        """Prove pytest-timeout is installed and fires correctly.

        Spawns a subprocess pytest run of a helper that sleeps past its
        1-second timeout.  Asserts the run exits non-zero and reports
        'Timeout'.
        """
        import subprocess
        import sys
        import textwrap
        import tempfile
        import os

        # Write a tiny test file that deliberately exceeds its timeout.
        canary_src = textwrap.dedent("""\
            import time
            import pytest

            @pytest.mark.timeout(1)
            def test_canary_sleep():
                time.sleep(10)
        """)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="test_canary_", delete=False
        ) as f:
            f.write(canary_src)
            canary_path = f.name

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", canary_path, "-n", "0", "-x", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode != 0, (
                f"Expected non-zero exit but got {result.returncode};\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
            combined = result.stdout + result.stderr
            assert "Timeout" in combined, (
                f"Expected 'Timeout' in output but not found;\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        finally:
            os.unlink(canary_path)

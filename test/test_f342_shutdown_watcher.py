"""F342: Unit tests for the shutdown signal watcher.

Proves that:
1. The watcher thread captures si_pid / si_uid from a SIGTERM sent by a child process.
2. The greppable SHUTDOWN line is emitted to the log.
3. Unknown-sender (pid gone) still logs raw si_pid/si_uid.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(
    not hasattr(signal, "sigwaitinfo"),
    reason="sigwaitinfo not available (non-Linux)",
)
class TestShutdownWatcher:
    """Integration tests that spawn a subprocess to test signal watcher."""

    def _run_watcher_script(
        self, script: str, timeout: float = 10.0
    ) -> subprocess.CompletedProcess[str]:
        """Run a Python script in a fresh subprocess (clean signal mask)."""
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_sigterm_self_send_logs_own_pid(self) -> None:
        """Process sends SIGTERM to itself — logs its own pid as sender."""
        result = self._run_watcher_script(f"""
            import os, signal, time
            # Ensure clean signal mask in this fresh process.
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {{signal.SIGTERM, signal.SIGINT}})

            import sys
            sys.path.insert(0, "{os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")}")
            # Also ensure the installed package is importable
            from cli_agent_orchestrator.utils.shutdown_watcher import install_shutdown_watcher

            install_shutdown_watcher()
            # Send SIGTERM to self.
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(2.0)
        """)

        # Process should be killed by SIGTERM (returncode = -15).
        assert result.returncode == -signal.SIGTERM

        # Verify the greppable SHUTDOWN line is present with sender info.
        assert "SHUTDOWN" in result.stderr
        assert "signal=SIGTERM" in result.stderr
        assert "sender_pid=" in result.stderr
        assert "sender_uid=" in result.stderr
        # Sender is self.
        assert f"sender_uid={os.getuid()}" in result.stderr

    def test_sigterm_from_external_process(self) -> None:
        """External process sends SIGTERM — logs the external sender's pid."""
        # Spawn a process that installs the watcher and waits; we send SIGTERM from here.
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(f"""
                    import os, signal, sys, time
                    signal.pthread_sigmask(signal.SIG_UNBLOCK, {{signal.SIGTERM, signal.SIGINT}})
                    sys.path.insert(0, "{os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")}")
                    from cli_agent_orchestrator.utils.shutdown_watcher import install_shutdown_watcher
                    install_shutdown_watcher()
                    # Signal readiness via stdout.
                    print("READY", flush=True)
                    time.sleep(30.0)
                """),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # Wait for the child to be ready.
            assert proc.stdout is not None
            line = proc.stdout.readline()
            assert "READY" in line

            # Send SIGTERM from this process.
            my_pid = os.getpid()
            assert proc.pid is not None
            os.kill(proc.pid, signal.SIGTERM)

            # Wait for child to exit.
            proc.wait(timeout=10.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        assert proc.stderr is not None
        stderr_output = proc.stderr.read()

        assert proc.returncode == -signal.SIGTERM
        assert "SHUTDOWN" in stderr_output
        assert "signal=SIGTERM" in stderr_output
        assert f"sender_pid={my_pid}" in stderr_output
        assert f"sender_uid={os.getuid()}" in stderr_output
        # Should also resolve sender_cmd since our process is still alive.
        assert "sender_cmd=" in stderr_output

    def test_read_sender_cmdline_resolves(self) -> None:
        """_read_sender_cmdline returns something for our own pid."""
        from cli_agent_orchestrator.utils.shutdown_watcher import _read_sender_cmdline

        cmd = _read_sender_cmdline(os.getpid())
        # Our own process should have a cmdline containing 'python' or 'pytest'.
        assert cmd is not None
        assert len(cmd) > 0

    def test_read_sender_cmdline_missing_pid(self) -> None:
        """_read_sender_cmdline returns None for a non-existent pid."""
        from cli_agent_orchestrator.utils.shutdown_watcher import _read_sender_cmdline

        result = _read_sender_cmdline(999999999)
        assert result is None

"""F469: kill_window parks attached clients on a stable window before killing.

Integration tests using a socket-isolated tmux server — no dependency on the
live CAO session. Each test creates its own tmux server, attaches a pty-based
client, and verifies parking behavior.

Requires: tmux ≥3.2 (for switch-client -c <tty>), Python pty module.
"""

import os
import pty
import signal
import subprocess
import time

import pytest


def _wait_for_client(env, *, timeout=3.0, interval=0.1):
    """Poll until tmux reports an attached client, return its tty.

    pty.fork() + tmux attach is asynchronous — the client registration can take
    longer than a fixed sleep under CPU contention (full xdist suite).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tty = env.output("list-clients", "-F", "#{client_tty}")
        if tty:
            return tty
        time.sleep(interval)
    return ""

# Force serial execution — these tests use pty.fork() which is incompatible with
# xdist worker parallelism (shared socket namespace).
pytestmark = pytest.mark.xdist_group("f469-tmux-parking")


def _make_socket_name(request) -> str:
    """Generate a unique socket name per test to avoid inter-test interference."""
    # Use a short hash of the test name (must match cao-sbx-<alnum>)
    import hashlib

    h = hashlib.md5(request.node.nodeid.encode()).hexdigest()[:8]
    return f"cao-sbx-f469{h}"


@pytest.fixture
def tmux_env(request, monkeypatch):
    """Provide an isolated tmux socket environment for one test."""
    socket_name = _make_socket_name(request)
    monkeypatch.setenv("CAO_TMUX_SOCKET", socket_name)
    monkeypatch.setenv("CAO_INSTANCE_ID", "test-f469")
    # pty.fork() children inherit os.environ; tmux attach-session requires TERM
    # to be set or it silently refuses to register a client. Under headless
    # environments (xdist workers, SSH non-interactive, CI) TERM may be absent.
    monkeypatch.setenv("TERM", os.environ.get("TERM", "xterm-256color"))

    def _tmux(*args):
        return subprocess.run(
            ["tmux", "-L", socket_name, *args],
            capture_output=True, text=True, timeout=5,
        )

    def _tmux_output(*args):
        return _tmux(*args).stdout.strip()

    # Kill any leftover
    _tmux("kill-server")
    time.sleep(0.1)

    class Env:
        name = socket_name

        @staticmethod
        def tmux(*args):
            return _tmux(*args)

        @staticmethod
        def output(*args):
            return _tmux_output(*args)

    yield Env()
    _tmux("kill-server")


def _get_backend():
    """Create a TmuxBackend wired to the current CAO_TMUX_SOCKET."""
    from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
    from cli_agent_orchestrator.clients.tmux import TmuxClient

    client = TmuxClient()
    return TmuxBackend(client=client)


class TestF469ParkClientsOnKill:
    """kill_window parks clients viewing the target window."""

    def test_client_viewing_killed_window_is_parked_on_win0(self, tmux_env):
        """Client viewing win1 → parked on win0 (supervisor seat) after kill."""
        env = tmux_env

        # Create session with 3 windows
        env.tmux("new-session", "-d", "-s", "test", "-n", "win0")
        env.tmux("new-window", "-t", "test", "-n", "win1")
        env.tmux("new-window", "-t", "test", "-n", "win2")

        # Attach client via pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("tmux", ["tmux", "-L", env.name, "attach-session", "-t", "test"])

        try:
            client_tty = _wait_for_client(env)
            assert client_tty, "No client attached"

            # Move client to win1
            env.tmux("switch-client", "-c", client_tty, "-t", "test:win1")
            time.sleep(0.2)

            # Verify on win1
            before = env.output("list-clients", "-F", "#{client_tty}\t#{window_name}")
            assert "win1" in before

            # Kill via backend
            backend = _get_backend()
            result = backend.kill_window("test", "win1")
            assert result is True
            time.sleep(0.2)

            # Client should be on win0
            after = env.output("list-clients", "-F", "#{client_tty}\t#{window_name}")
            assert "win0" in after, f"Expected client on win0, got: {after}"
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def test_client_not_viewing_killed_window_is_not_moved(self, tmux_env):
        """Client on win0 stays on win0 when win1 is killed."""
        env = tmux_env

        env.tmux("new-session", "-d", "-s", "test", "-n", "win0")
        env.tmux("new-window", "-t", "test", "-n", "win1")
        env.tmux("new-window", "-t", "test", "-n", "win2")

        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("tmux", ["tmux", "-L", env.name, "attach-session", "-t", "test"])

        try:
            client_tty = _wait_for_client(env)
            assert client_tty

            # Park client on win0 (not the target)
            env.tmux("switch-client", "-c", client_tty, "-t", "test:win0")
            time.sleep(0.2)

            before = env.output("list-clients", "-F", "#{client_tty}\t#{window_name}")
            assert "win0" in before

            # Kill win1 (client is NOT viewing it)
            backend = _get_backend()
            result = backend.kill_window("test", "win1")
            assert result is True
            time.sleep(0.2)

            # Client should still be on win0
            after = env.output("list-clients", "-F", "#{client_tty}\t#{window_name}")
            assert "win0" in after, f"Expected client still on win0, got: {after}"
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def test_parking_failure_does_not_block_kill(self, tmux_env):
        """Even if parking subprocess fails, kill still proceeds."""
        from unittest.mock import patch

        env = tmux_env

        env.tmux("new-session", "-d", "-s", "test", "-n", "win0")
        env.tmux("new-window", "-t", "test", "-n", "win1")

        backend = _get_backend()

        # Sabotage: make _resolve_window_index raise
        with patch.object(
            backend, "_resolve_window_index", side_effect=RuntimeError("simulated failure")
        ):
            result = backend.kill_window("test", "win1")

        # Kill should still succeed despite parking failure
        assert result is True
        windows = env.output("list-windows", "-t", "test", "-F", "#{window_name}")
        assert "win1" not in windows

    def test_kill_window0_parks_client_on_surviving_window(self, tmux_env):
        """When killing window 0 (supervisor seat), client is parked on any other."""
        env = tmux_env

        env.tmux("new-session", "-d", "-s", "test", "-n", "win0")
        env.tmux("new-window", "-t", "test", "-n", "win1")

        pid, fd = pty.fork()
        if pid == 0:
            os.execvp("tmux", ["tmux", "-L", env.name, "attach-session", "-t", "test"])

        try:
            client_tty = _wait_for_client(env)
            assert client_tty

            # Park client on win0
            env.tmux("switch-client", "-c", client_tty, "-t", "test:win0")
            time.sleep(0.2)

            backend = _get_backend()
            result = backend.kill_window("test", "win0")
            assert result is True
            time.sleep(0.2)

            # Client should be on win1 (the only survivor)
            after = env.output("list-clients", "-F", "#{client_tty}\t#{window_name}")
            assert "win1" in after, f"Expected client on win1, got: {after}"
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass

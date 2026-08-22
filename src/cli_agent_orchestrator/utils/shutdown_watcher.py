"""F342: Signal watcher thread that logs WHO sent SIGTERM/SIGINT before the process exits.

Uses ``signal.pthread_sigmask`` + ``signal.sigwaitinfo`` (stdlib, no ctypes) to
receive siginfo metadata (si_pid, si_uid) that Python's normal signal handlers
discard.  Resolves the sender pid to a command-line when the process is still alive.

Design:
  1. ``install_shutdown_watcher()`` blocks SIGTERM/SIGINT in the calling (main) thread
     and resets their disposition to SIG_DFL.  Threads spawned later (including uvicorn
     workers) inherit the blocked mask.
  2. A dedicated daemon thread inherits the blocked mask and calls ``sigwaitinfo`` —
     which atomically dequeues a pending signal without requiring it to be unblocked.
  3. After logging sender info, the thread unblocks the signal in its own mask and
     re-raises it so the process terminates with the expected signal exit status
     (visible to systemd / the supervisor).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Greppable prefix — the spec says "SHUTDOWN signal=..." must be easily greppable.
_LOG_PREFIX = "SHUTDOWN"

_WATCHED_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def _read_sender_cmdline(pid: int) -> Optional[str]:
    """Best-effort read of /proc/<pid>/cmdline for the sender process."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if not raw:
            return None
        # cmdline is NUL-separated; join with spaces for readability.
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def _signal_watcher(ready_event: threading.Event) -> None:
    """Target for the watcher daemon thread.

    Blocks on ``sigwaitinfo`` until one of the watched signals arrives, logs sender
    info, then re-raises the signal with the default disposition so the process
    terminates with the correct signal exit code.
    """
    # Signal remains BLOCKED in this thread (inherited from main).
    # sigwaitinfo dequeues pending signals from the blocked set.
    ready_event.set()

    # Block until a signal arrives (indefinitely — daemon thread, killed at exit).
    try:
        siginfo = signal.sigwaitinfo(_WATCHED_SIGNALS)
    except OSError:
        # Unlikely; if the mask is disturbed externally just let the process die.
        return

    sig_name = signal.Signals(siginfo.si_signo).name  # e.g. "SIGTERM"
    sender_pid: int = siginfo.si_pid
    sender_uid: int = siginfo.si_uid

    # Resolve sender command-line (best-effort; pid may already be gone).
    sender_cmd = _read_sender_cmdline(sender_pid)
    cmd_part = f" sender_cmd={sender_cmd}" if sender_cmd else ""

    # Build the greppable message.
    shutdown_msg = (
        f"{_LOG_PREFIX} signal={sig_name} sender_pid={sender_pid}"
        f" sender_uid={sender_uid}{cmd_part}"
    )

    logger.critical(shutdown_msg)

    # Also emit to stderr directly via raw write — survives even if the logging
    # framework is already torn down or buffered.  os.write is atomic for small
    # payloads and immune to Python-level buffering.
    try:
        os.write(sys.stderr.fileno(), (shutdown_msg + "\n").encode())
    except OSError:
        pass

    # Re-raise with default disposition so the process terminates with the
    # expected signal exit status (e.g. 128+15 for SIGTERM).
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {siginfo.si_signo})
    os.kill(os.getpid(), siginfo.si_signo)


def install_shutdown_watcher() -> threading.Thread:
    """Block SIGTERM/SIGINT in the calling thread, then spawn a daemon watcher thread.

    Must be called from the main thread BEFORE ``uvicorn.run()`` so that:
    1. The main thread (and any children inheriting its mask) no longer receives
       these signals directly.
    2. The watcher thread inherits the blocked mask and uses ``sigwaitinfo`` to
       dequeue the pending signal, capturing si_pid/si_uid.
    3. After logging, the signal is re-raised with SIG_DFL so the process exits
       with the correct signal status.

    Returns the watcher thread (primarily for testing).
    """
    # Reset handlers to SIG_DFL so Python's internal signal machinery does not
    # intercept these signals via its own C-level trampoline.  Must happen BEFORE
    # blocking, and only from the main thread.
    for sig in _WATCHED_SIGNALS:
        signal.signal(sig, signal.SIG_DFL)

    # Block watched signals in the calling (main) thread.  Child threads inherit
    # the mask, which is exactly what sigwaitinfo needs — the signal must be
    # blocked in the thread calling sigwaitinfo.
    signal.pthread_sigmask(signal.SIG_BLOCK, _WATCHED_SIGNALS)

    ready = threading.Event()
    t = threading.Thread(
        target=_signal_watcher,
        args=(ready,),
        daemon=True,
        name="shutdown-watcher",
    )
    t.start()
    # Wait for the thread to be ready (entered sigwaitinfo).
    ready.wait(timeout=2.0)
    return t

"""terminal id -> the live ACP client for that seat.

The missing link the S1 review named as B1.1 and B1.3: ``AcpTransport`` was
constructed with ``sessions=None``, so every wake over the plane returned
``acp_session_unbound`` and nothing could be delivered. A carrier needs to reach
the subprocess that belongs to a terminal, and the only thing that knows which
subprocess that is, is whatever spawned it.

**A process-wide default instance, deliberately.** The spawn happens in
``terminal_service`` and the carrier is built in ``bootstrap``, and neither can
hand the other a parameter without threading one through the whole launch path.
A module-level registry is the smallest thing that lets both name the same
object, and it is honest about its scope: one server process, one map, cleared
when a terminal goes.

It holds CLIENTS and not sessions, transports or tasks. Those are cheap wrappers
over a client and are built where they are needed; the expensive, unique thing is
the subprocess, and that is what has to be found again.
"""

from __future__ import annotations

import threading

from cli_agent_orchestrator.adapters.acp.client import AcpClient

__all__ = ["AcpSessionRegistry", "acp_sessions"]


class AcpSessionRegistry:
    """Live ACP subprocesses, by terminal id.

    Guarded by a lock because the delivery tick reads it from its own thread
    while a launch writes it from a request thread, and a dict that is written
    during iteration is a failure nobody reproduces.
    """

    def __init__(self) -> None:
        self._clients: dict[str, AcpClient] = {}
        self._lock = threading.Lock()

    def bind(self, terminal_id: str, client: AcpClient) -> None:
        """Bind a terminal to its live client.

        A second bind REPLACES. That is the respawn case (D6b's recovery), and
        replacing is what makes the old client unreachable rather than leaving
        two objects both claiming the terminal.
        """
        with self._lock:
            self._clients[terminal_id] = client

    def get(self, terminal_id: str) -> AcpClient | None:
        """The live client, or ``None`` when this terminal has no ACP session.

        ``None`` rather than a raise: the carrier turns it into the typed
        ``acp_session_unbound`` reason, which is bounded by the row's own
        deadline and heals when the seat's session binds — a raise would make an
        ordinary startup race look like a transport fault.
        """
        with self._lock:
            return self._clients.get(terminal_id)

    def drop(self, terminal_id: str) -> AcpClient | None:
        """Forget a terminal, returning whatever was bound so a caller can tear it down."""
        with self._lock:
            return self._clients.pop(terminal_id, None)

    def terminals(self) -> tuple[str, ...]:
        """Every terminal with a live ACP session, in a stable order.

        Sorted, so a scheduler pass over them does not depend on insertion order
        — the kind of hidden input that makes an isolation arm pass on one
        machine and fail on another.
        """
        with self._lock:
            return tuple(sorted(self._clients))

    def clear(self) -> None:
        with self._lock:
            self._clients.clear()


#: The process-wide registry. Named rather than anonymous so a test can clear it
#: and a reader can see there is exactly one.
acp_sessions = AcpSessionRegistry()

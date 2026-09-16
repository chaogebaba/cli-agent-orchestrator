"""Backend registry — module-level backend singleton management.

This module provides get_backend() and set_backend() as the central access point
for the configured TerminalBackend. It has no dependencies on providers or services,
breaking the circular import chain.
"""

from typing import Any, Optional

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.core.transport import is_acp_terminal

# Module-level backend instance. Initialized lazily via get_backend().
_backend: Optional[TerminalBackend] = None


def get_backend() -> TerminalBackend:
    """Return the configured terminal backend (lazy-initialized via BackendFactory)."""
    global _backend
    if _backend is None:
        from cli_agent_orchestrator.backends.factory import BackendFactory

        _backend = BackendFactory.create()
    return _backend


def set_backend(backend: TerminalBackend) -> None:
    """Set the terminal backend (called at application startup)."""
    global _backend
    _backend = backend


def backend_for_terminal(row: Any) -> Optional[TerminalBackend]:
    """The terminal backend that owns this row's pane, or ``None`` for an ACP row.

    WP-ACP-PLANE D20/AC-S1.10.  ``get_backend()`` answers "what backend is this
    installation configured with", which every caller has always been able to
    ask.  This answers the different question D20 introduces — "does this
    TERMINAL have a pane backend at all" — and it is a separate function rather
    than a flag on the first because the two have different failure modes: the
    installation always has a backend, and an ACP terminal never has a pane.

    Returning ``None`` rather than raising is deliberate.  A caller that has no
    ACP-shaped branch yet gets an ordinary ``None`` it must handle, in the same
    shape as every other absent-resource answer in this tree, instead of an
    exception thrown from a lookup that used to be infallible.

    The branch is on ``transport``.  It is NEVER on ``tmux_session``/
    ``tmux_window`` being NULL: those are nullable for ACP rows, and reading
    their absence as a signal is the exact confusion AC-S1.10's grep fails.
    """
    if is_acp_terminal(row):
        return None
    return get_backend()
